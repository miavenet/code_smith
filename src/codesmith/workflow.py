"""Load a workflow, its types and personas; apply precedence; expand panels; validate.

This module is pure apart from reading files and asking git three questions about the root:
is it a repository, which files are tracked, and which declared paths are ignored.
It never checks agent capabilities: `validate` must work with no agent installed.
"""

import heapq
import math
import os
import re
import shlex
import subprocess
import tomllib
from dataclasses import dataclass, field

from . import findings, patterns

BUILTIN_LIBRARY = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "library"))

KINDS = ("produce", "review", "check", "human")
CAPABILITIES = ("answer", "read", "execute", "write", "resume", "boundary")
BUILTIN_AGENTS = ("claude", "codex", "copilot")
BUILTIN_PROTECTED = ["**/.gitignore", "**/.gitattributes", "**/.gitmodules"]

ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")
MAX_ID = 64     # ids name directories and refs; a panel id adds `.review.` and a persona name
PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_.-]*)\}")

PLACEHOLDERS = {
    "task.id", "task.title", "task.prompt", "inputs", "outputs", "gates", "rules", "persona",
    "target", "diff", "findings", "attempt", "max_attempts", "result_schema",
}
REVIEW_ONLY_PLACEHOLDERS = {"persona", "target", "diff"}

BUILTIN_DEFAULTS = {
    "agent": "claude",
    "model": "",
    "max_attempts": 3,
    "timeout_min": 30,
    "gate_timeout_min": 20,
    "budget_usd": 5.0,
    "run_budget_usd": 50.0,
    "run_budget_tokens": 0,
    "unpriced_call_reserve": 0,
    "probe_reserve_tokens": 20000,
    "max_parallel": 4,
    "recheck_passed": "diff",
    "branch": "run",
    "commit_trailer": "",
    "protected": [],
    "diff_cap_bytes": 200000,
    "inputs_cap_bytes": 40000,
    "findings_cap_bytes": 60000,
    "status_refresh_s": 15,
    "reviewer_family": "different",
    "acceptance_replay": True,
    "gate_cache": [],
    "rulings_require_interactive": False,
    "replay_beside_panel": True,
}

STR, BOOL, INT, NUM, STRLIST, TABLE, ANY = "string", "boolean", "integer", "number", \
    "list of strings", "table", "any"

DEFAULTS_KEYS = {
    "agent": STR, "model": STR, "max_attempts": INT, "timeout_min": NUM, "gate_timeout_min": NUM,
    "budget_usd": NUM, "run_budget_usd": NUM, "run_budget_tokens": INT, "max_parallel": INT,
    "unpriced_call_reserve": INT, "probe_reserve_tokens": INT, "recheck_passed": STR,
    "branch": STR, "commit_trailer": STR, "protected": STRLIST, "diff_cap_bytes": INT,
    "inputs_cap_bytes": INT, "findings_cap_bytes": INT, "complexity": STR,
    "status_refresh_s": INT, "runs_dir": STR, "rules_file": STR, "rulings_file": STR,
    "reviewer_family": STR,
    "acceptance_replay": BOOL, "gate_cache": STRLIST, "rulings_require_interactive": BOOL,
    "replay_beside_panel": BOOL,
}
TOP_KEYS = {"name": STR, "root": STR, "library": STRLIST, "defaults": TABLE, "agents": TABLE,
            "task": ANY, "model_policy": TABLE}
AGENT_KEYS = {
    "kind": STR, "model": STR, "sandbox": STR, "review_mode": STR, "permission_mode": STR,
    "ignore_user_config": BOOL, "extra_args": STRLIST, "argv": STRLIST, "read_only_args": STRLIST,
    "price_per_mtok": TABLE, "estimated_counts": BOOL,
}
PRICE_KEYS = ("input", "cached_input", "output")
# Kinds whose calls report no dollar cost: the only ones a profile may price (05, Honest accounting).
UNPRICED_KINDS = ("codex", "copilot", "command")
TYPE_KEYS = {
    "name": STR, "kind": STR, "description": STR, "requires": STRLIST, "complexity": STR, "review_type": STR,
    "needs_run_dir": BOOL, "agent": STR, "model": STR, "gate": ANY, "protected": STRLIST,
    "prompt": STR, "params": TABLE, "max_attempts": INT, "timeout_min": NUM, "budget_usd": NUM,
    "max_changed_files": INT, "max_changed_lines": INT,
}
# The diff budget: on a produce task or its type, never in [defaults].
DIFF_BUDGET_KEYS = ("max_changed_files", "max_changed_lines")
PARAM_KEYS = {"default": ANY, "required": BOOL}
PERSONA_KEYS = {
    "name": STR, "code": STR, "title": STR, "agent": STR, "model": STR, "advisory": BOOL,
    "focus": STRLIST, "blocking": STRLIST, "out_of_scope": STRLIST, "voice": STR,
}

COMMON_TASK_KEYS = {"id": STR, "type": STR, "title": STR, "needs": STRLIST, "params": TABLE}
AGENT_TASK_KEYS = {"agent": STR, "model": STR, "timeout_min": NUM, "budget_usd": NUM,
                   "prompt": STR, "prompt_file": STR, "rules_file": STR, "recheck_passed": STR,
                   "fallback_agents": STRLIST, "complexity": STR}
TASK_KEYS = {
    "produce": {**COMMON_TASK_KEYS, **AGENT_TASK_KEYS, "max_attempts": INT, "outputs": ANY,
                "writes": STRLIST, "removes": STRLIST, "gate": ANY, "reviewers": ANY,
                "protected": STRLIST, "max_changed_files": INT, "max_changed_lines": INT,
                "review_diff_from": STR, "acceptance_replay": BOOL, "gate_cache": STRLIST},
    "review": {**COMMON_TASK_KEYS, **AGENT_TASK_KEYS, "reviews": STR, "perspective": STR,
               "advisory": BOOL, "protected": STRLIST},
    "check": {**COMMON_TASK_KEYS, "verifies": STR, "run": STRLIST, "read_only": BOOL,
              "restores": BOOL, "protected": STRLIST},
    "human": {**COMMON_TASK_KEYS, "verifies": STR},
}
ALL_TASK_KEYS = set().union(*TASK_KEYS.values())
PANEL_ENTRY_KEYS = {
    "perspective": STR, "advisory": BOOL, "agent": STR, "model": STR, "type": STR, "prompt": STR,
    "prompt_file": STR, "rules_file": STR, "recheck_passed": STR, "timeout_min": NUM, "budget_usd": NUM,
    "fallback_agents": STRLIST, "complexity": STR, "params": TABLE,
}
GATE_KEYS = {"run": STR, "new": BOOL, "fail_pattern": STR, "expect": STR}
OUTPUT_KEYS = {"path": STR, "may_be_empty": BOOL}

BYPASS_SANDBOXES = {"danger-full-access"}
BYPASS_PERMISSION_MODES = {"bypassPermissions"}
BYPASS_ARGS = {"--dangerously-skip-permissions", "--dangerously-bypass-approvals-and-sandbox",
               "--yolo", "--allow-all", "--allow-all-tools", "--allow-all-paths", "--allow-all-urls"}


class WorkflowError(Exception):
    """Raised by `load_or_raise` when a workflow has errors."""

    def __init__(self, errors, warnings=()):
        super().__init__("\n".join(errors))
        self.errors = list(errors)
        self.warnings = list(warnings)


@dataclass
class Workflow:
    name: str = ""
    workflow_file: str = ""
    root: str = ""
    git_toplevel: str = ""
    library_dirs: list = field(default_factory=list)
    defaults: dict = field(default_factory=dict)
    model_policy: dict = field(default_factory=dict)
    agents: dict = field(default_factory=dict)
    types: dict = field(default_factory=dict)
    personas: dict = field(default_factory=dict)
    tasks: list = field(default_factory=list)      # expanded, in execution order
    claims: list = field(default_factory=list)     # frozen outputs a later task will modify
    # (task, key, path, pattern): a literal `outputs`/`writes` path that lifts a protection
    protection_overrides: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    # Why the standing rulings file is not the committed copy ('' when it is); `start` refuses it
    rulings_problem: str = ""

    @property
    def ok(self):
        return not self.errors

    def task(self, task_id):
        for t in self.tasks:
            if t["id"] == task_id:
                return t
        raise KeyError(task_id)

    def expanded(self):
        """Everything after types, personas, panels and defaults are applied, as plain data."""
        return {
            "name": self.name,
            "paths": {"workflow_file": self.workflow_file, "root": self.root,
                      "git_toplevel": self.git_toplevel, "library": list(self.library_dirs)},
            "defaults": dict(self.defaults),
            "agents": {k: dict(v) for k, v in self.agents.items()},
            "tasks": [dict(t) for t in self.tasks],
            "claims": [dict(c) for c in self.claims],
        }


def load_or_raise(path):
    wf = load(path)
    if wf.errors:
        raise WorkflowError(wf.errors, wf.warnings)
    return wf


def load(path, rulings_at="HEAD"):
    """Load and validate. Never raises for a bad workflow: every problem is in `.errors`.
    `rulings_at`: the commit the standing rulings are read from (a replan's is the run's start)."""
    return _Loader(path, rulings_at).load()


# --------------------------------------------------------------------------------------------


def _type_ok(value, kind):
    if kind == ANY:
        return True
    if kind == STR:
        return isinstance(value, str)
    if kind == BOOL:
        return isinstance(value, bool)
    if kind == INT:
        return isinstance(value, int) and not isinstance(value, bool)
    if kind == NUM:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if kind == STRLIST:
        return isinstance(value, list) and all(isinstance(v, str) for v in value)
    if kind == TABLE:
        return isinstance(value, dict)
    return False


class _Loader:
    def __init__(self, path, rulings_at="HEAD"):
        self.wf = Workflow(workflow_file=os.path.abspath(path))
        self.base = os.path.dirname(self.wf.workflow_file)
        self.tracked = None
        self.rulings_at, self.rulings_protected = rulings_at, None

    def err(self, msg):
        if msg not in self.wf.errors:
            self.wf.errors.append(msg)

    def warn(self, msg):
        if msg not in self.wf.warnings:
            self.wf.warnings.append(msg)

    def check_keys(self, table, allowed, where):
        """Report unknown keys and wrong value types. Returns only the usable entries."""
        good = {}
        for key, value in table.items():
            if key not in allowed:
                self.err(f"{where}: unknown key '{key}'")
            elif not _type_ok(value, allowed[key]):
                self.err(f"{where}: '{key}' must be a {allowed[key]}")
            elif allowed[key] == NUM and (not math.isfinite(value) or value <= 0):
                self.err(f"{where}: '{key}' must be finite and greater than zero")
            else:
                good[key] = value
        return good

    # -- top level ---------------------------------------------------------------------------

    def load(self):
        wf = self.wf
        label = os.path.basename(wf.workflow_file)
        try:
            with open(wf.workflow_file, "rb") as fh:
                raw = tomllib.load(fh)
        except OSError as exc:
            self.err(f"{label}: cannot read: {exc.strerror or exc}")
            return wf
        except tomllib.TOMLDecodeError as exc:
            self.err(f"{label}: not valid TOML: {exc}")
            return wf

        top = self.check_keys(raw, TOP_KEYS, label)
        wf.name = top.get("name") or os.path.splitext(label)[0]
        if not ID_RE.match(wf.name):
            derived = "" if top.get("name") else " (no 'name' is set, so it was taken from the file name; set 'name')"
            self.err(f"{label}: 'name' must match [A-Za-z0-9][A-Za-z0-9_-]*, got '{wf.name}'{derived}")
        elif len(wf.name) > MAX_ID:
            self.err(f"{label}: 'name' is longer than {MAX_ID} characters")
        wf.root = os.path.normpath(os.path.join(self.base, top.get("root", ".")))

        defaults = dict(BUILTIN_DEFAULTS)
        given = self.check_keys(top.get("defaults", {}), DEFAULTS_KEYS, f"{label} [defaults]")
        self.given_defaults = given
        defaults.update(given)
        if defaults["recheck_passed"] not in ("diff", "never"):
            self.err(f"{label} [defaults]: 'recheck_passed' must be \"diff\" or \"never\"")
        if defaults["branch"] not in ("run", "current"):
            self.err(f"{label} [defaults]: 'branch' must be \"run\" or \"current\"")
        for key in ("max_attempts", "max_parallel"):
            if defaults[key] < 1:
                self.err(f"{label} [defaults]: '{key}' must be at least 1")
        if defaults["run_budget_tokens"] < 0:
            self.err(f"{label} [defaults]: 'run_budget_tokens' must be 0 (no cap) or more")
        if defaults["unpriced_call_reserve"] < 0:
            self.err(f"{label} [defaults]: 'unpriced_call_reserve' must be 0 (automatic) or more")
        if defaults["probe_reserve_tokens"] < 1:
            self.err(f"{label} [defaults]: 'probe_reserve_tokens' must be at least 1")
        if defaults["status_refresh_s"] < 0:
            self.err(f"{label} [defaults]: 'status_refresh_s' must be 0 (no refresh during a call) or more")
        if "runs_dir" in given and not given["runs_dir"].strip():
            self.err(f"{label} [defaults]: 'runs_dir' must not be empty")
        if defaults["reviewer_family"] not in ("different", "any"):
            self.err(f"{label} [defaults]: 'reviewer_family' must be \"different\" or \"any\"")
        for key in ("diff_cap_bytes", "inputs_cap_bytes", "findings_cap_bytes"):
            if defaults[key] < 1:
                self.err(f"{label} [defaults]: '{key}' must be at least 1")
        self.check_gate_cache(defaults["gate_cache"], f"{label} [defaults]")
        wf.defaults = defaults

        wf.model_policy = self.check_keys(top.get('model_policy', {}),
            {level: TABLE for level in ('mechanical', 'standard', 'high')}, f'{label} [model_policy]')
        for level, models in list(wf.model_policy.items()):
            wf.model_policy[level] = self.check_keys(models,
                {provider: STR for provider in ('claude', 'codex', 'copilot', 'command')}, f'model_policy.{level}')
            if any(not model.strip() for model in wf.model_policy[level].values()):
                self.err(f'model_policy.{level}: model names must not be empty')
        self.load_agents(top.get("agents", {}), label)
        self.check_root()
        if 'rulings_file' in defaults and wf.git_toplevel:
            self.load_rulings(defaults, label)
        self.load_library(top.get("library", []), label)

        raw_tasks = raw.get("task", [])
        if not isinstance(raw_tasks, list) or not all(isinstance(t, dict) for t in raw_tasks):
            self.err(f"{label}: tasks must be written as [[task]] tables")
            raw_tasks = []
        if not raw_tasks:
            self.err(f"{label}: the workflow has no tasks")

        tasks = self.resolve_tasks(raw_tasks)
        self.check_rulings_writers(tasks)
        self.check_patterns(tasks, defaults["protected"], label)
        self.check_references(tasks)
        self.check_verifiers(tasks)
        self.check_paths(tasks)
        has_cycle = self.check_cycles(tasks)
        if not has_cycle:
            self.check_writers(tasks)
        self.protect_executed_files(tasks)
        self.check_ignored(tasks)
        used_agents = sorted({t["agent"] for t in tasks if t["kind"] in ("produce", "review")})
        for name in used_agents:
            profile = wf.agents.get(name, {})
            if profile.get("kind", name) in ("command", "codex", "copilot"):
                cap = wf.defaults["run_budget_tokens"]
                self.warn(f"agent '{name}' reports no dollar cost: dollar limits do not bind on it; "
                          "time and attempts still apply, and usage is recorded as unpriced"
                          + (f"; run_budget_tokens stops the run once {cap} tokens were used"
                             if cap else "; set run_budget_tokens to cap its usage")
                          + ("; a command agent reports no usage, so each of its calls counts "
                             "at its reservation (at most unpriced_call_reserve, or the rest of the cap)" if cap and
                             profile.get("kind", name) == "command" else ""))
        wf.tasks = self.order(tasks, has_cycle)
        for n, t in enumerate(wf.tasks):
            t["order"] = n
        return wf

    def load_agents(self, table, label):
        wf = self.wf
        for name in BUILTIN_AGENTS:
            wf.agents[name] = {"kind": name}
        for name, profile in table.items():
            where = f"{label} [agents.{name}]"
            if not isinstance(profile, dict):
                self.err(f"{where}: must be a table")
                continue
            good = self.check_keys(profile, AGENT_KEYS, where)
            kind = good.get("kind", name if name in BUILTIN_AGENTS else "command")
            if kind not in BUILTIN_AGENTS + ("command",):
                self.err(f"{where}: 'kind' must be \"claude\", \"codex\", \"copilot\" or \"command\"")
            if kind == "copilot" and good.get("ignore_user_config"):
                self.err(f"{where}: 'ignore_user_config' is not supported for copilot: the CLI has no "
                         "flag to leave out user configuration")
            if kind == "copilot" and good.get("permission_mode") not in (None, *BYPASS_PERMISSION_MODES):
                self.err(f"{where}: 'permission_mode' on a copilot profile must be \"bypassPermissions\" "
                         "(all permissions for its writers) or left out")
            if kind == "command" and not good.get("argv"):
                self.err(f"{where}: a command agent needs 'argv'")
            self.check_prices(good, kind, where)
            mode = good.get("review_mode")
            if mode is not None and mode not in ("repository", "provided_context"):
                self.err(f"{where}: 'review_mode' must be \"repository\" or \"provided_context\"")
            good["kind"] = kind
            good["declared"] = True          # named by the owner, so part of the host's inventory
            wf.agents[name] = good
            words = set(good.get("extra_args", [])) | set(good.get("argv", []))
            if (good.get("sandbox") in BYPASS_SANDBOXES
                    or good.get("permission_mode") in BYPASS_PERMISSION_MODES
                    or words & BYPASS_ARGS):
                self.warn(f"agent profile '{name}' is configured to bypass its sandbox or "
                          "permissions")

    def check_prices(self, profile, kind, where):
        """`price_per_mtok = { input, cached_input, output }`, USD per million tokens, numbers of
        at least zero; `input` and `output` are required. Only for kinds that report no cost: a
        reported price is never replaced by an estimate. `estimated_counts` needs the rates."""
        rates = profile.get("price_per_mtok")
        if rates is not None:
            if kind not in UNPRICED_KINDS:
                self.err(f"{where}: 'price_per_mtok' is only for kinds that report no cost "
                         f"(codex, copilot, command); a {kind} profile reports its own")
            for key, value in rates.items():
                if key not in PRICE_KEYS:
                    self.err(f"{where}: price_per_mtok: unknown key '{key}'")
                elif (not isinstance(value, (int, float)) or isinstance(value, bool)
                      or not math.isfinite(value) or value < 0):
                    self.err(f"{where}: price_per_mtok.{key} must be a number of at least 0")
            for key in ("input", "output"):
                if key not in rates:
                    self.err(f"{where}: price_per_mtok needs '{key}'")
        if profile.get("estimated_counts") and rates is None:
            self.err(f"{where}: 'estimated_counts' needs 'price_per_mtok'")

    # -- git ---------------------------------------------------------------------------------

    def git(self, *args, stdin=None):
        cmd = ["git"]
        if self.wf.git_toplevel:
            # The owner pointed the runner at this root, so trust exactly this repository.
            cmd += ["-c", f"safe.directory={self.wf.git_toplevel}"]
        cmd += ["-C", self.wf.root, *args]
        return subprocess.run(cmd, input=stdin, capture_output=True)

    def load_rulings(self, defaults, label):
        """Standing rulings are owner input (V-01, V-02 of the K–N review): they are read from the
        copy committed on the run's starting commit, never from the work tree, and the file is
        protected in every task. An ignored location is refused, since a write there is in no
        diff; an untracked or edited copy is reported in `rulings_problem`, which `start` refuses."""
        wf, name = self.wf, defaults['rulings_file']
        try:
            path = findings.rulings_path(wf.git_toplevel, name)
        except ValueError as exc:
            self.err(f"{label}: rulings_file: {exc}")
            return
        link = self.rulings_link(name)
        if link:
            # Only the link's target would be protected; a writer of the link could send the
            # export to any file it names (RUN-79).
            self.err(f"{label}: rulings_file '{name}' passes through a symbolic link ({link}). "
                     "Standing rulings are owner input, written only as the file itself: "
                     "name the real file in rulings_file (no link on the way) and commit it, or "
                     "remove rulings_file")
            return
        here = os.path.relpath(path, os.path.realpath(wf.root))
        if not here.startswith('..'):
            self.rulings_protected = here.replace(os.sep, '/')
        rel = os.path.relpath(path, os.path.realpath(wf.git_toplevel)).replace(os.sep, '/')
        res = self.git("check-ignore", "-q", "--", here)
        if res.returncode == 0 or set(rel.split('/')) & {'.git', '.runs'}:
            self.err(f"{label}: rulings_file '{name}' is ignored by git (or under .git or .runs), and "
                     "a write there is in no diff a reviewer sees. Standing rulings are owner input: "
                     "move them to a tracked path and commit it, or remove rulings_file")
            return
        commit = self.git("rev-parse", "--verify", "-q", self.rulings_at + "^{commit}")
        commit = commit.stdout.decode().strip() if commit.returncode == 0 else ""
        blob = self.git("rev-parse", "--verify", "-q", f"{commit}:{rel}") if commit else None
        blob = blob.stdout.decode().strip() if blob is not None and blob.returncode == 0 else ""
        data = self.git("cat-file", "blob", blob).stdout if blob else b""
        if os.path.exists(path):
            found = self.git("hash-object", "--", here).stdout.decode().strip()
            if not blob:
                wf.rulings_problem = f"rulings_file '{name}' is not committed"
            elif found != blob:
                wf.rulings_problem = f"rulings_file '{name}' differs from its committed copy"
        elif blob:
            wf.rulings_problem = f"rulings_file '{name}' was removed from the work tree"
        if wf.rulings_problem:
            wf.rulings_problem += (f". A run reads the committed copy only: commit {name}, or "
                                   "remove rulings_file")
            self.warn(f"{label}: {wf.rulings_problem}")
        try:
            defaults['_standing_rulings'] = findings.parse_rulings(tomllib.loads(data.decode('utf-8')))
        except (ValueError, UnicodeDecodeError) as exc:
            self.err(f"{label}: rulings_file: {exc}")
            return
        defaults['_standing_rulings_blob'] = blob
        defaults['_standing_rulings_commit'] = commit

    def rulings_link(self, name):
        """'path (in the work tree)' or 'path (committed)' of the first part of the rulings path
        that is a symbolic link, in the work tree or on the commit the rulings are read from;
        '' when none is."""
        top = self.wf.git_toplevel
        link = findings.symlink_on_path(top, name)
        if link:
            return f"{link}, in the work tree"
        parts = [p for p in name.replace(os.sep, '/').split('/') if p not in ('', '.')]
        prefixes = ['/'.join(parts[:n]) for n in range(1, len(parts) + 1)]
        res = self.git("ls-tree", "-z", "--full-tree", self.rulings_at, "--", *prefixes)
        for entry in res.stdout.split(b"\0") if res.returncode == 0 else ():
            meta, _tab, rel = entry.partition(b"\t")
            if meta.split(b" ")[0] == b"120000":
                return f"{rel.decode('utf-8', 'replace')}, committed"
        return ''

    def check_rulings_writers(self, tasks):
        """A literal path in `writes` lifts a protection; the standing rulings file stays protected."""
        path = self.rulings_protected
        for t in tasks if path else ():
            if path in t.get("writes", []) or path in [o["path"] for o in t.get("outputs", [])]:
                self.err(f"task '{t['id']}' names the standing rulings file {path} in its writes or "
                         "outputs: it is owner input, and no task may write it")

    def check_root(self):
        wf = self.wf
        if not os.path.isdir(wf.root):
            self.err(f"root '{wf.root}' is not a directory")
            return
        top = wf.root
        while not os.path.exists(os.path.join(top, ".git")):
            parent = os.path.dirname(top)
            if parent == top:
                top = ""
                break
            top = parent
        wf.git_toplevel = top
        ok = False
        if top:
            try:
                res = self.git("rev-parse", "--is-inside-work-tree")
                ok = res.returncode == 0 and res.stdout.strip() == b"true"
                detail = res.stderr.decode(errors="replace").strip()
            except OSError as exc:
                detail = f"cannot run git: {exc}"
        else:
            detail = ""
        if not ok:
            wf.git_toplevel = ""
            self.err(f"root '{wf.root}' is not a git repository: snapshots, restores and commits "
                     "all need git" + (f" ({detail})" if detail else ""))
            return
        if os.path.realpath(top) != os.path.realpath(wf.root):
            self.warn(f"root '{wf.root}' is a subdirectory of the git repository at '{top}'; "
                      "snapshots and the clean-tree rule cover the whole repository")
        res = self.git("ls-files", "-z")
        if res.returncode == 0:
            self.tracked = set(p for p in res.stdout.decode(errors="surrogateescape").split("\0")
                               if p)

    # -- library -----------------------------------------------------------------------------

    def load_library(self, extra, label):
        wf = self.wf
        dirs = []
        for d in extra:
            full = os.path.normpath(os.path.join(self.base, d))
            if not os.path.isdir(full):
                self.err(f"{label}: library directory '{d}' not found (looked for {full})")
            else:
                dirs.append(full)
        dirs.append(BUILTIN_LIBRARY)
        wf.library_dirs = dirs
        for d in dirs:                       # earlier directories win
            self.load_dir(d, "types", TYPE_KEYS, wf.types)
            self.load_dir(d, "personas", PERSONA_KEYS, wf.personas)
        for name in sorted(wf.types):
            self.check_type(wf.types[name])
        codes = {}
        for name in sorted(wf.personas):
            p = wf.personas[name]
            where = p["_where"]
            for key in ("code", "title"):
                if not p.get(key):
                    self.err(f"{where}: '{key}' is required")
            code = p.get("code")
            if code:
                if not re.match(r"[A-Z][A-Z0-9]*\Z", code):
                    self.err(f"{where}: 'code' must be capital letters and digits, got '{code}'")
                if code in codes:
                    self.err(f"personas '{codes[code]}' and '{name}' both use code '{code}'")
                else:
                    codes[code] = name

    def load_dir(self, libdir, sub, allowed, into):
        folder = os.path.join(libdir, sub)
        if not os.path.isdir(folder):
            return
        for fname in sorted(os.listdir(folder)):
            if not fname.endswith(".toml"):
                continue
            stem = fname[:-5]
            if stem in into:
                continue                     # shadowed by an earlier library directory
            where = os.path.join(folder, fname)
            try:
                with open(where, "rb") as fh:
                    raw = tomllib.load(fh)
            except (OSError, tomllib.TOMLDecodeError) as exc:
                self.err(f"{where}: cannot load: {exc}")
                continue
            good = self.check_keys(raw, allowed, where)
            if good.get("name", stem) != stem:
                self.err(f"{where}: 'name' is '{good['name']}' but the file is '{fname}'")
            good["name"] = stem
            good["_where"] = where
            into[stem] = good

    def check_type(self, t):
        where = t["_where"]
        kind = t.get("kind")
        if kind not in ("produce", "review"):
            self.err(f"{where}: 'kind' must be \"produce\" or \"review\"; check and human tasks "
                     "need no type file")
        for cap in t.get("requires", []):
            if cap not in CAPABILITIES:
                self.err(f"{where}: 'requires' names unknown capability '{cap}'")
        params = {}
        for pname, spec in t.get("params", {}).items():
            if not isinstance(spec, dict):
                self.err(f"{where}: [params.{pname}] must be a table")
                continue
            spec = self.check_keys(spec, PARAM_KEYS, f"{where} [params.{pname}]")
            if spec.get("required") and "default" in spec:
                self.err(f"{where} [params.{pname}]: a required parameter cannot have a default")
            if not spec.get("required") and "default" not in spec:
                self.err(f"{where} [params.{pname}]: needs 'default', or 'required = true'")
            params[pname] = spec
        t["params"] = params
        if not t.get("prompt", "").strip():
            self.err(f"{where}: 'prompt' template is required")
        for name in PLACEHOLDER_RE.findall(t.get("prompt", "")):
            if name.startswith("param."):
                if name[6:] not in params:
                    self.err(f"{where}: template uses undeclared parameter '{{{name}}}'")
            elif name not in PLACEHOLDERS:
                self.err(f"{where}: template uses unknown placeholder '{{{name}}}'")
            elif name in REVIEW_ONLY_PLACEHOLDERS and kind == "produce":
                self.err(f"{where}: placeholder '{{{name}}}' is for review types only")
        rt = t.get("review_type")
        if rt:
            if kind != "produce":
                self.err(f"{where}: 'review_type' belongs on a produce type")
            elif rt not in self.wf.types:
                self.err(f"{where}: review_type '{rt}' not found in library")
            elif self.wf.types[rt].get("kind") != "review":
                self.err(f"{where}: review_type '{rt}' is not a review type")
        t["gate"] = self.gates(t.get("gate", []), where)
        for key in DIFF_BUDGET_KEYS:
            if key in t:
                if kind != "produce":
                    self.err(f"{where}: '{key}' belongs on a produce type")
                elif t[key] < 1:
                    self.err(f"{where}: '{key}' must be at least 1")

    def check_gate_cache(self, paths, where):
        """`gate_cache` names plain paths under the root, copied into the replay's checkout."""
        for path in paths:
            parts = path.rstrip("/").split("/")
            if not path.strip() or path.startswith("/") or patterns.has_wildcard(path) \
                    or any(p in ("", ".", "..", ".git") for p in parts):
                self.err(f"{where}: 'gate_cache' entry '{path}' must be a plain relative path "
                         "under the root, without wildcards, '..' or .git")

    # -- tasks -------------------------------------------------------------------------------

    def gates(self, raw, where):
        out = []
        if not isinstance(raw, list):
            self.err(f"{where}: 'gate' must be a list")
            return out
        for entry in raw:
            if isinstance(entry, str):
                gate = {"run": entry, "new": False, "fail_pattern": ""}
            elif isinstance(entry, dict):
                good = self.check_keys(entry, GATE_KEYS, f"{where}: gate")
                if "run" not in good:
                    self.err(f"{where}: a gate table needs 'run'")
                    continue
                gate = {"run": good["run"], "new": good.get("new", False),
                        "fail_pattern": good.get("fail_pattern", "")}
                expect = good.get("expect", "pass")
                if expect not in ("pass", "fail"):
                    self.err(f"{where}: a gate's 'expect' must be \"pass\" or \"fail\"")
                elif expect == "fail":
                    # Only added when set, so that the definitions of existing runs are unchanged.
                    gate["expect"] = "fail"
                    if gate["new"]:
                        self.err(f"{where}: a gate cannot be both 'new = true' and 'expect = \"fail\"'; "
                                 "a `new` gate must fail now and pass after the task, an expected "
                                 "failure must fail on the accepted candidate")
                    if not gate["fail_pattern"]:
                        self.err(f"{where}: a gate with 'expect = \"fail\"' needs 'fail_pattern': "
                                 "a failure with no stated reason proves nothing")
                if gate["new"] and not gate["fail_pattern"]:
                    self.warn(f"{where}: a gate marked 'new = true' has no 'fail_pattern'; "
                              "check-gates cannot confirm it fails for the intended reason")
                if gate["fail_pattern"]:
                    if not gate["new"] and expect != "fail":
                        self.err(f"{where}: 'fail_pattern' only applies to a gate marked "
                                 "'new = true' or 'expect = \"fail\"'")
                    try:
                        re.compile(gate["fail_pattern"])
                    except re.error as exc:
                        self.err(f"{where}: 'fail_pattern' is not a valid regular expression: "
                                 f"{exc}")
            else:
                self.err(f"{where}: a gate must be a string or a table")
                continue
            if not gate["run"].strip():
                self.err(f"{where}: a gate command is empty")
                continue
            out.append(gate)
        return out

    def kind_of(self, type_name):
        if type_name in ("check", "human"):
            return type_name, None
        t = self.wf.types.get(type_name)
        if t is None:
            return None, None
        return t.get("kind"), t

    def resolve_tasks(self, raw_tasks):
        tasks, seen, folded = [], set(), {}
        for n, raw in enumerate(raw_tasks, 1):
            tid = raw.get("id")
            where = f"task '{tid}'" if isinstance(tid, str) and tid else f"task #{n}"
            if not isinstance(tid, str) or not tid:
                self.err(f"{where}: 'id' is required")
                tid = None
            elif not ID_RE.match(tid):
                self.err(f"{where}: malformed id; ids match [A-Za-z0-9][A-Za-z0-9_-]* "
                         "(a dot is reserved for generated panel ids)")
            elif len(tid) > MAX_ID:
                self.err(f"{where}: id is longer than {MAX_ID} characters")
            elif tid in seen:
                self.err(f"{where}: duplicate id")
                tid = None
            elif tid.casefold() in folded:
                self.err(f"{where}: id differs from '{folded[tid.casefold()]}' only by case; "
                         "they would share a directory on a case-insensitive file system")
                tid = None
            type_name = raw.get("type")
            if not isinstance(type_name, str) or not type_name:
                self.err(f"{where}: 'type' is required")
                for key in raw:
                    if key not in ALL_TASK_KEYS:
                        self.err(f"{where}: unknown key '{key}'")
                continue
            kind, tdef = self.kind_of(type_name)
            if kind not in KINDS:
                if tdef is None:
                    self.err(f"{where}: type '{type_name}' not found in library")
                for key in raw:
                    if key not in ALL_TASK_KEYS:
                        self.err(f"{where}: unknown key '{key}'")
                continue
            for key in list(raw):
                if key not in ALL_TASK_KEYS:
                    self.err(f"{where}: unknown key '{key}'")
                elif key not in TASK_KEYS[kind]:
                    self.err(f"{where}: key '{key}' does not apply to a {kind} task")
            good = self.check_keys({k: v for k, v in raw.items() if k in TASK_KEYS[kind]},
                                   TASK_KEYS[kind], where)
            if tid is None:
                continue
            seen.add(tid)
            folded[tid.casefold()] = tid
            task = self.build(tid, kind, type_name, tdef, good, where, persona_entry=None)
            tasks.append(task)
            if kind == "produce":
                tasks.extend(self.expand_panel(task, tdef, raw.get("reviewers"), where))
        return tasks

    def setting(self, key, task, persona, tdef):
        """task, then persona, then type, then workflow [defaults], then built-in. '' is unset."""
        for source in (task, persona or {}, tdef or {}, self.given_defaults):
            value = source.get(key)
            if value is not None and value != "":
                return value
        return BUILTIN_DEFAULTS.get(key)

    def build(self, tid, kind, type_name, tdef, good, where, persona_entry):
        wf = self.wf
        task = {
            "id": tid, "kind": kind, "type": type_name, "title": good.get("title") or tid,
            "generated": False, "needs": list(good.get("needs", [])),
            "reviews": None, "verifies": None,
        }
        if len(set(task["needs"])) != len(task["needs"]):
            self.err(f"{where}: 'needs' lists a task twice")
        if tid in task["needs"]:
            self.err(f"{where}: a task cannot need itself")

        persona = None
        if kind == "review":
            pname = good.get("perspective")
            if not pname:
                self.err(f"{where}: a review task needs 'perspective'")
            elif pname not in wf.personas:
                self.err(f"{where}: persona '{pname}' not found in library")
            else:
                persona = wf.personas[pname]
            task["perspective"] = pname
            task["persona_code"] = (persona or {}).get("code")
            task["reviews"] = good.get("reviews")
            if not task["reviews"]:
                self.err(f"{where}: a review task needs 'reviews'")
            advisory = good.get("advisory")
            if advisory is None:
                advisory = bool((persona or {}).get("advisory", False))
            task["advisory"] = advisory

        if kind in ("produce", "review"):
            if "prompt" in good and "prompt_file" in good:
                self.err(f"{where}: 'prompt' and 'prompt_file' are both set; give one")
            task["prompt"] = good.get("prompt", "")
            task["prompt_file"] = None
            if "prompt_file" in good:
                full = os.path.normpath(os.path.join(self.base, good["prompt_file"]))
                task["prompt_file"] = full
                if not os.path.isfile(full):
                    self.err(f"{where}: prompt_file '{good['prompt_file']}' not found "
                             f"(looked for {full})")
            # The project rules every author and reviewer is shown: the task's, else [defaults]';
            # "" on a task opts out. Frozen with the briefs, so a reviewer and the author it judges
            # read the same rules all run long.
            task["rules_file"] = None
            rules = good["rules_file"] if "rules_file" in good else self.given_defaults.get("rules_file")
            if rules:
                full = os.path.normpath(os.path.join(self.base, rules))
                task["rules_file"] = full
                if not os.path.isfile(full):
                    self.err(f"{where}: rules_file '{rules}' not found (looked for {full})")
            agent = self.setting("agent", good, persona, tdef)
            if agent not in wf.agents:
                self.err(f"{where}: agent '{agent}' is not defined; built in are "
                         f"{', '.join(BUILTIN_AGENTS)}, others need an [agents.{agent}] table")
            task["agent"] = agent
            alternatives = good.get("fallback_agents", [])
            if kind == "produce" and not alternatives and wf.defaults["reviewer_family"] == "different" \
                    and wf.agents.get(agent, {}).get("kind") in BUILTIN_AGENTS:
                # An author with no fallbacks of its own may move to another family when the host
                # cannot run its first choice (design/reviewer-families.md).
                alternatives = self.other_families(wf.agents[agent]["kind"])
                task["agent_rule"] = "reviewer_family: the other model families as fallbacks"
            if alternatives:
                task["fallback_agents"] = alternatives
            if len(set([agent] + alternatives)) != len([agent] + alternatives):
                self.err(f"{where}: fallback_agents must be unique and exclude the primary agent")
            for alternative in alternatives:
                if alternative not in wf.agents:
                    self.err(f"{where}: fallback agent '{alternative}' is not defined")
            task["model"] = self.setting("model", good, persona, tdef) or ""
            complexity = self.setting('complexity', good, persona, tdef) or 'standard'
            if complexity not in ('mechanical', 'standard', 'high'):
                self.err(f"{where}: complexity must be mechanical, standard, or high")
            if wf.model_policy or self.setting('complexity', good, persona, tdef):
                task['complexity'] = complexity
            models = wf.model_policy.get(complexity, {})
            chosen = {}
            for name in [agent] + alternatives:
                profile = wf.agents.get(name, {})
                explicit_model = next((source['model'] for source in (good, persona or {}, tdef or {})
                                       if source.get('model')), '') if name == agent else ''
                model = (explicit_model or models.get(profile.get('kind'), '')
                         or (task['model'] if name == agent else '') or profile.get('model', ''))
                if models or alternatives:
                    chosen[name] = model
                if name != agent and profile.get('kind') != 'command' and not model:
                    self.err(f"{where}: fallback agent '{name}' needs a profile model or complexity model mapping")
                if name != agent:
                    primary = wf.agents.get(agent, {})
                    def bypass(p):
                        return (p.get('sandbox') in BYPASS_SANDBOXES
                                or p.get('permission_mode') in BYPASS_PERMISSION_MODES
                                or bool(set(p.get('extra_args', []) + p.get('argv', [])) & BYPASS_ARGS))
                    if bypass(profile) and not bypass(primary):
                        self.err(f"{where}: fallback '{name}' may not widen permission controls")
                    if profile.get('review_mode', 'repository') != primary.get('review_mode', 'repository'):
                        self.err(f"{where}: fallback '{name}' must preserve review_mode")
            if chosen:
                task['provider_models'] = chosen
            task["timeout_min"] = self.setting("timeout_min", good, None, tdef)
            task["budget_usd"] = self.setting("budget_usd", good, None, tdef)
            task["recheck_passed"] = self.setting("recheck_passed", good, None, None)
            if task["recheck_passed"] not in ("diff", "never"):
                self.err(f"{where}: 'recheck_passed' must be \"diff\" or \"never\"")
            task["requires"] = list((tdef or {}).get("requires", []))
            task["needs_run_dir"] = bool((tdef or {}).get("needs_run_dir", False))
            task["params"] = self.params(good.get("params", {}), tdef or {}, where)
        elif good.get("params"):
            self.err(f"{where}: a {kind} task has no type parameters")

        if kind == "produce":
            task["max_attempts"] = self.setting("max_attempts", good, None, tdef)
            if task["max_attempts"] < 1:
                self.err(f"{where}: 'max_attempts' must be at least 1")
            task["outputs"] = self.outputs(good.get("outputs"), where)
            paths = [o["path"] for o in task["outputs"]]
            task["writes"] = list(good["writes"]) if "writes" in good else list(paths)
            task["removes"] = list(good.get("removes", []))
            task["gates"] = (self.gates(good["gate"], where) if "gate" in good
                             else [dict(g) for g in (tdef or {}).get("gate", [])])
            task["gate_timeout_min"] = wf.defaults["gate_timeout_min"]
            for key in DIFF_BUDGET_KEYS:          # the task's own, else its type's; absent: no budget
                value = good.get(key, (tdef or {}).get(key))
                if value is not None:
                    if key in good and value < 1:     # a type's own value is checked with the type
                        self.err(f"{where}: '{key}' must be at least 1")
                    task[key] = value
            task["review_type"] = (tdef or {}).get("review_type") or None
            task["reviewers"] = []
            if good.get("review_diff_from"):
                # Set only when given, so a run expanded before the key existed compares equal.
                task["review_diff_from"] = good["review_diff_from"]
            # The acceptance replay's switch and cache: on the task only when given, as above;
            # otherwise the engine takes them from the frozen [defaults].
            for key in ("acceptance_replay", "gate_cache"):
                if key in good:
                    task[key] = good[key]
            if "gate_cache" in good:
                self.check_gate_cache(good["gate_cache"], where)
        if kind in ("check", "human"):
            task["verifies"] = good.get("verifies")
        if kind == "check":
            task["run"] = list(good.get("run", []))
            if not [c for c in task["run"] if c.strip()]:
                self.err(f"{where}: a check needs 'run'")
            elif not all(c.strip() for c in task["run"]):
                self.err(f"{where}: a check command is empty")
            task["read_only"] = good.get("read_only", False)
            task["restores"] = good.get("restores", False)
            if task["read_only"] and task["restores"]:
                self.err(f"{where}: 'read_only' and 'restores' cannot both be true")
            task["gate_timeout_min"] = wf.defaults["gate_timeout_min"]

        # `protected` is a union of every level and can never be narrowed
        protected = []
        for source in (BUILTIN_PROTECTED, wf.defaults["protected"],
                       (tdef or {}).get("protected", []), good.get("protected", []),
                       [self.rulings_protected] if self.rulings_protected else []):
            for p in source:
                if p not in protected:
                    protected.append(p)
        task["protected"] = protected
        task["_own_protected"] = list((tdef or {}).get("protected", [])) + \
            list(good.get("protected", []))
        return task

    def params(self, given, tdef, where):
        declared = tdef.get("params", {})
        out = {}
        for name in given:
            if name not in declared:
                self.err(f"{where}: type '{tdef.get('name')}' has no parameter '{name}'")
        for name, spec in declared.items():
            if name in given:
                out[name] = given[name]
            elif spec.get("required"):
                self.err(f"{where}: missing required parameter '{name}' of type "
                         f"'{tdef.get('name')}'")
            else:
                out[name] = spec.get("default")
        return out

    def outputs(self, raw, where):
        out = []
        if not isinstance(raw, list) or not raw:
            self.err(f"{where}: a produce task needs 'outputs'")
            return out
        for entry in raw:
            if isinstance(entry, str):
                out.append({"path": entry, "may_be_empty": False})
            elif isinstance(entry, dict):
                good = self.check_keys(entry, OUTPUT_KEYS, f"{where}: output")
                if "path" not in good:
                    self.err(f"{where}: an output table needs 'path'")
                    continue
                out.append({"path": good["path"], "may_be_empty": good.get("may_be_empty", False)})
            else:
                self.err(f"{where}: an output must be a string or a table")
        return out

    def expand_panel(self, producer, tdef, raw, where):
        members = []
        if raw is None:
            return members
        if not isinstance(raw, list):
            self.err(f"{where}: 'reviewers' must be a list")
            return members
        seen = set()
        # The reviewer-family rule (design/reviewer-families.md): a reviewer that names no agent
        # of its own defaults to a declared profile of another model kind than the author's, dealt
        # round-robin across the panel, with the other families and then the author's profile as
        # fallbacks; qualification and routing settle what the host can actually run.
        others = []
        author_profile = self.wf.agents.get(producer["agent"], {})
        if self.wf.defaults["reviewer_family"] == "different" and author_profile.get("kind") in BUILTIN_AGENTS:
            others = self.other_families(author_profile["kind"])
            if not others and raw:
                self.warn(f"{where}: every reviewer shares the author's model family "
                          f"({author_profile['kind']}): no runnable profile of another kind is defined. "
                          "Add an [agents.NAME] of kind codex or copilot (or a [model_policy] model for "
                          "it) to honour reviewer_family = \"different\", or set it to \"any\"")
        dealt = 0
        for entry in raw:
            if isinstance(entry, str):
                entry = {"perspective": entry}
            elif not isinstance(entry, dict):
                self.err(f"{where}: a reviewer must be a persona name or a table")
                continue
            ewhere = f"{where}: reviewer '{entry.get('perspective', '?')}'"
            good = self.check_keys(entry, PANEL_ENTRY_KEYS, ewhere)
            pname = good.get("perspective")
            if not pname:
                self.err(f"{where}: a reviewer table needs 'perspective'")
                continue
            if pname in seen:
                self.err(f"{where}: perspective '{pname}' is listed twice in 'reviewers'")
                continue
            seen.add(pname)
            rtype = good.get("type") or (tdef or {}).get("review_type")
            if not rtype:
                self.err(f"{where}: 'reviewers' is set but type '{producer['type']}' names no "
                         f"'review_type', and reviewer '{pname}' gives no 'type'")
                continue
            kind, rdef = self.kind_of(rtype)
            if kind != "review":
                if rdef is None and rtype not in ("check", "human"):
                    self.err(f"{ewhere}: type '{rtype}' not found in library")
                else:
                    self.err(f"{ewhere}: type '{rtype}' is not a review type")
                continue
            if not re.match(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z", pname):
                self.err(f"{ewhere}: malformed persona name")
                continue
            if len(pname) > MAX_ID:
                self.err(f"{ewhere}: persona name is longer than {MAX_ID} characters")
                continue
            rid = f"{producer['id']}.review.{pname}"
            fields = {k: v for k, v in good.items() if k != "type"}
            fields["reviews"] = producer["id"]
            if "recheck_passed" not in fields:
                fields["recheck_passed"] = producer["recheck_passed"]
            if "rules_file" not in fields and producer.get("rules_file"):
                fields["rules_file"] = os.path.relpath(producer["rules_file"], self.base)
            explicit = fields.get("agent") or self.wf.personas.get(pname, {}).get("agent") \
                or (rdef or {}).get("agent")
            if others and not explicit:
                pick = others[dealt % len(others)]
                dealt += 1
                fields["agent"] = pick
                chain = list(fields.get("fallback_agents", []))
                for name in [o for o in others if o != pick] + [producer["agent"]]:
                    if name not in chain and name != pick and self.runnable(name):
                        chain.append(name)
                fields["fallback_agents"] = chain
            member = self.build(rid, "review", rtype, rdef, fields, f"task '{rid}'", None)
            if others and not explicit:
                member["agent_rule"] = "reviewer_family: a different model family than the author's"
            member["generated"] = True
            member["title"] = f"{self.wf.personas.get(pname, {}).get('title', pname)} review of " \
                              f"{producer['id']}"
            members.append(member)
            producer["reviewers"].append(rid)
        return members

    def other_families(self, kind):
        """The inventory of the other model families, as profile names: the owner's declared
        profiles of each other kind (file order), then the built-in `claude`, `codex` or `copilot`
        profile of a kind nobody declared. Every family is a candidate; `doctor` says which the
        host can run, and routing falls back from the ones it cannot."""
        declared = [name for name, p in self.wf.agents.items()
                    if p.get("declared") and p["kind"] in BUILTIN_AGENTS and p["kind"] != kind
                    and self.runnable(name)]
        covered = {self.wf.agents[name]["kind"] for name in declared}
        # A built-in profile has no model of its own: it is in the inventory only when
        # [model_policy] names a model for its kind, which is what makes it runnable.
        return declared + [k for k in BUILTIN_AGENTS if k != kind and k not in covered and self.runnable(k)]

    def runnable(self, name):
        """A profile the runner could call as a fallback: it has a model, or [model_policy] maps
        its kind (a fallback without a model is an error)."""
        p = self.wf.agents[name]
        return bool(p.get("model")) or any(p["kind"] in table for table in self.wf.model_policy.values())

    # -- validation --------------------------------------------------------------------------

    def check_patterns(self, tasks, workflow_protected, label):
        for p in workflow_protected:
            for problem in patterns.validate_pattern(p):
                self.err(f"{label} [defaults]: protected pattern '{p}' {problem}")
        for name, t in sorted(self.wf.types.items()):
            for p in t.get("protected", []):
                for problem in patterns.validate_pattern(p):
                    self.err(f"{t['_where']}: protected pattern '{p}' {problem}")
        for t in tasks:
            sets = [("protected", t.pop("_own_protected", []))]
            if t["kind"] == "produce":
                sets += [("outputs", [o["path"] for o in t["outputs"]]),
                         ("writes", t["writes"]), ("removes", t["removes"])]
            bad = set()
            for key, plist in sets:
                for p in plist:
                    problems = patterns.validate_pattern(p)
                    for problem in problems:
                        self.err(f"task '{t['id']}': {key} pattern '{p}' {problem}")
                    if problems:
                        bad.add(p)
                    elif key != "protected" and set(p.split("/")) & {".git", ".runs"}:
                        self.err(f"task '{t['id']}': {key} path '{p}' is under .git or .runs, "
                                 "which no task may touch")
                        bad.add(p)
                    elif key in ("outputs", "writes") and not patterns.has_wildcard(p):
                        hit = next((q for q in t["protected"]
                                    if not patterns.validate_pattern(q) and patterns.matches(q, p)), None)
                        if hit:
                            self.warn(f"task '{t['id']}': {key} path '{p}' is named literally, which "
                                      f"lets it change although protected pattern '{hit}' matches it")
                            self.wf.protection_overrides.append((t["id"], key, p, hit))
            t["_bad_patterns"] = bad

    def check_references(self, tasks):
        by_id = {t["id"]: t for t in tasks}
        for t in tasks:
            where = f"task '{t['id']}'"
            for n in t["needs"]:
                if n not in by_id:
                    self.err(f"{where}: 'needs' names unknown task '{n}'")
            for key in ("reviews", "verifies"):
                target = t.get(key)
                if not target:
                    continue
                if target not in by_id:
                    self.err(f"{where}: '{key}' names unknown task '{target}'")
                elif by_id[target]["kind"] != "produce":
                    self.err(f"{where}: '{key}' must name a produce task; '{target}' is a "
                             f"{by_id[target]['kind']} task")
                elif target == t["id"]:
                    self.err(f"{where}: '{key}' names itself")
        for t in tasks:
            if t.get("review_diff_from"):
                self.check_review_diff_from(t, by_id, tasks)
        for producer in (t for t in tasks if t["kind"] == "produce"):
            seen = {}
            for r in tasks:
                if r["kind"] == "review" and r.get("reviews") == producer["id"] \
                        and r.get("perspective"):
                    if r["perspective"] in seen:
                        self.err(f"task '{producer['id']}': perspective '{r['perspective']}' "
                                 f"reviews it twice ('{seen[r['perspective']]}' and '{r['id']}')")
                    else:
                        seen[r["perspective"]] = r["id"]

    def check_review_diff_from(self, task, by_id, tasks):
        """`review_diff_from` starts the panel's first diff at the accepted commit of an earlier
        producer, so that producer must be accepted before this task can run: one of its needs,
        directly or through other needs."""
        where, source = f"task '{task['id']}'", task["review_diff_from"]
        if source == task["id"]:
            self.err(f"{where}: 'review_diff_from' names itself")
            return
        if source not in by_id:
            self.err(f"{where}: 'review_diff_from' names unknown task '{source}'")
            return
        if by_id[source]["kind"] != "produce":
            self.err(f"{where}: 'review_diff_from' must name a produce task; '{source}' is a "
                     f"{by_id[source]['kind']} task")
            return
        upstream, todo = set(), list(task["needs"])
        while todo:
            n = todo.pop()
            if n in upstream or n not in by_id:
                continue
            upstream.add(n)
            todo.extend(by_id[n]["needs"])
        if source not in upstream:
            self.err(f"{where}: 'review_diff_from' names '{source}', which is not among the tasks "
                     "it needs (directly or through other needs); the diff starts at that task's "
                     "accepted commit, so add it to 'needs'")
        if not any(r.get("reviews") == task["id"] for r in tasks):
            self.err(f"{where}: 'review_diff_from' sets what its review panel sees, and the task "
                     "has no reviewers")

    def check_verifiers(self, tasks):
        for p in (t for t in tasks if t["kind"] == "produce"):
            if p["gates"]:
                continue
            verifiers = [t for t in tasks
                         if t.get("reviews") == p["id"] or t.get("verifies") == p["id"]]
            if any(v["kind"] != "review" or not v.get("advisory") for v in verifiers):
                continue
            if verifiers:
                self.err(f"task '{p['id']}': its whole panel is advisory and it has no other "
                         "verifier; an advisory review cannot accept work")
            else:
                self.err(f"task '{p['id']}' has no gate, check, review or human verifier")

    def check_paths(self, tasks):
        for t in (t for t in tasks if t["kind"] == "produce"):
            bad = t["_bad_patterns"]
            writes = [w for w in t["writes"] if w not in bad]
            for o in t["outputs"]:
                path = o["path"]
                if path in bad:
                    continue
                if not patterns.covered_by(path, writes):
                    self.err(f"task '{t['id']}': output '{path}' is not covered by its 'writes'; "
                             "repeat the entry in 'writes'")
                for r in t["removes"]:
                    if r in bad:
                        continue
                    # a glob output beside a literal `removes` inside it is satisfiable
                    if r == path or (not patterns.has_wildcard(path) and patterns.matches(r, path)):
                        self.err(f"task '{t['id']}': '{path}' is in 'outputs' (must exist) and "
                                 f"matched by 'removes' entry '{r}' (must not exist)")
            for r in t["removes"]:
                if r not in bad and not patterns.covered_by(r, writes):
                    self.err(f"task '{t['id']}': removes path '{r}' is not covered by its "
                             "'writes'; a deletion is a change, and would be reverted")

    def check_cycles(self, tasks):
        """Needs cycles first; then cycles in the acceptance graph. True if any."""
        by_id = {t["id"]: t for t in tasks}
        needs = {t["id"]: [n for n in t["needs"] if n in by_id and n != t["id"]] for t in tasks}
        cycle = _find_cycle(list(needs), lambda n: needs[n])
        if cycle:
            # printed in dependency direction: a needs b needs a
            self.err("dependency cycle: " + " -> ".join(cycle))
            return True
        graph = _milestone_graph(tasks)
        cycle = _find_cycle(list(graph), lambda n: graph[n])
        if not cycle:
            return False
        ids = []
        for node in cycle:
            if not ids or ids[-1] != node[0]:
                ids.append(node[0])
        if len(ids) > 1 and ids[0] == ids[-1]:
            ids.pop()
        producer = next((n[0] for n in cycle if n[1] == "accepted"), ids[0])
        k = ids.index(producer)
        ring = ids[k:] + ids[:k]
        ring.append(ring[0])
        reasons = []
        for t in tasks:
            target = t.get("reviews") or t.get("verifies")
            if target == producer and t["id"] in ring:
                verb = "reviews" if t.get("reviews") else "verifies"
                if producer in t["needs"]:
                    reasons.append(f"'{t['id']}' {verb} '{producer}' and also needs it")
                else:
                    reasons.append(f"'{t['id']}' {verb} '{producer}' but needs work downstream "
                                   "of it")
        self.err(f"acceptance cycle: {' -> '.join(ring)}: " + "; ".join(reasons)
                 + f": '{producer}' can never be accepted")
        return True

    def check_writers(self, tasks):
        by_id = {t["id"]: t for t in tasks}
        closure = {}

        def upstream(tid):
            if tid not in closure:
                closure[tid] = set()
                for n in by_id[tid]["needs"]:
                    if n in by_id:
                        closure[tid].add(n)
                        closure[tid] |= upstream(n)
            return closure[tid]

        producers = [t for t in tasks if t["kind"] == "produce"]
        for i, a in enumerate(producers):
            for b in producers[i + 1:]:
                if a["id"] in upstream(b["id"]):
                    first, later = a, b
                elif b["id"] in upstream(a["id"]):
                    first, later = b, a
                else:
                    first = later = None
                shared = self.overlaps(a["writes"], b["writes"], a, b)
                if not shared:
                    continue
                if first is None:
                    self.err(f"'{b['id']}' and '{a['id']}' both write {', '.join(shared)} and "
                             "neither depends on the other")
                    continue
                claimed = self.overlaps([o["path"] for o in first["outputs"]], later["writes"],
                                        first, later)
                if claimed:
                    consumers = [t["id"] for t in tasks
                                 if first["id"] in t["needs"] and t["id"] != later["id"]]
                    self.wf.claims.append({"task": later["id"], "of": first["id"],
                                           "paths": claimed, "consumers": consumers})
                    self.warn(f"'{later['id']}' will modify outputs of accepted task "
                              f"'{first['id']}': {', '.join(claimed)}. Accepted consumers of it: "
                              + (", ".join(consumers) if consumers else "none"))

    def overlaps(self, left, right, a, b):
        shared = []
        bad = a["_bad_patterns"] | b["_bad_patterns"]
        for x in left:
            for y in right:
                if x in bad or y in bad:
                    continue
                if patterns.may_overlap(x, y):
                    label = x if x == y else f"{x} / {y}"
                    if label not in shared:
                        shared.append(label)
        return shared

    def protect_executed_files(self, tasks):
        """Files a gate or check executes are protected by default."""
        if self.tracked is None:
            return
        by_id = {t["id"]: t for t in tasks}
        for t in tasks:
            if t["kind"] == "produce":
                commands, guarded = [g["run"] for g in t["gates"]], [t]
            elif t["kind"] == "check":
                commands = t["run"]
                guarded = [t] + ([by_id[t["verifies"]]] if t.get("verifies") in by_id else [])
            else:
                continue
            for cmd in commands:
                try:
                    words = shlex.split(cmd)
                except ValueError:
                    self.err(f"task '{t['id']}': command cannot be parsed: {cmd}")
                    continue
                for word in words:
                    path = os.path.normpath(word).replace(os.sep, "/")
                    if path not in self.tracked:
                        continue
                    for g in guarded:
                        if path in g.get("writes", []):
                            self.warn(f"task '{g['id']}' lists '{path}' in 'writes', and "
                                      f"'{t['id']}' executes it: the task may edit a file its own "
                                      "verifier executes")
                        elif path not in g["protected"]:
                            g["protected"].append(path)

    def check_ignored(self, tasks):
        if not self.wf.git_toplevel:
            for t in tasks:
                t.pop("_bad_patterns", None)
            return
        probes = {}
        for t in tasks:
            bad = t.pop("_bad_patterns", set())
            if t["kind"] != "produce":
                continue
            declared = [("output", o["path"]) for o in t["outputs"]] + \
                [("writes path", w) for w in t["writes"]] + [("removes path", r) for r in t["removes"]]
            for key, p in declared:
                if p in bad:
                    continue
                if patterns.has_wildcard(p):
                    prefix = "/".join(patterns.literal_prefix(p))
                    if not prefix:
                        continue
                    probe = prefix + "/__code_smith_probe__"
                else:
                    probe = p
                probes.setdefault(probe, []).append((t["id"], key, p))
        if not probes:
            return
        res = self.git("check-ignore", "-v", "-z", "--stdin",
                       stdin="\0".join(probes).encode() + b"\0")
        if res.returncode not in (0, 1):
            self.err("git check-ignore failed: " + res.stderr.decode(errors="replace").strip())
            return
        fields = res.stdout.decode(errors="replace").split("\0")
        reported = set()
        for i in range(0, len(fields) - 3, 4):
            source, line, rule, path = fields[i:i + 4]
            if rule.startswith("!"):
                continue            # a negated rule says the path is NOT ignored
            for tid, key, p in probes.get(path, []):
                if (tid, p) in reported:
                    continue
                reported.add((tid, p))
                self.err(f"'{tid}' {key} {p} is ignored by {source}:{line} '{rule}'; ignored "
                         "work is invisible to snapshots, reviewers and commits")

    # -- order -------------------------------------------------------------------------------

    def order(self, tasks, has_cycle):
        """Topological over the milestone graph; ties broken by position in the workflow."""
        if has_cycle:
            return tasks
        index = {t["id"]: n for n, t in enumerate(tasks)}
        graph = _milestone_graph(tasks)            # node -> nodes that must come after it
        waiting = {n: 0 for n in graph}
        for n, after in graph.items():
            for m in after:
                waiting[m] += 1
        rank = {"candidate": 0, "done": 0, "accepted": 1}
        heap = [(index[n[0]], rank[n[1]], n) for n, c in waiting.items() if c == 0]
        heapq.heapify(heap)
        out = []
        while heap:
            _, _, node = heapq.heappop(heap)
            if node[1] != "accepted":
                out.append(tasks[index[node[0]]])
            for m in graph[node]:
                waiting[m] -= 1
                if waiting[m] == 0:
                    heapq.heappush(heap, (index[m[0]], rank[m[1]], m))
        return out


def _milestone_graph(tasks):
    """The expanded acceptance graph. Edges point from what comes first to what follows.

    A producer has a candidate and an accepted milestone. `needs` waits for accepted;
    `reviews` and `verifies` wait for the candidate; accepted waits for every verifier.
    """
    by_id = {t["id"]: t for t in tasks}

    def start(t):
        return (t["id"], "candidate" if t["kind"] == "produce" else "done")

    def end(t):
        return (t["id"], "accepted" if t["kind"] == "produce" else "done")

    graph = {}
    for t in tasks:
        graph.setdefault(start(t), [])
        if t["kind"] == "produce":
            graph.setdefault(end(t), [])
            graph[start(t)].append(end(t))
    for t in tasks:
        for n in t["needs"]:
            if n in by_id and n != t["id"]:
                graph[end(by_id[n])].append(start(t))
        target = t.get("reviews") or t.get("verifies")
        if target in by_id and by_id[target]["kind"] == "produce" and target != t["id"]:
            graph[start(by_id[target])].append(start(t))
            graph[end(t)].append(end(by_id[target]))
    return graph


def _find_cycle(nodes, successors):
    """Return one cycle as [a, b, ..., a], or None. Deterministic: follows the given order."""
    WHITE, GREY, BLACK = 0, 1, 2
    colour = {n: WHITE for n in nodes}
    for root in nodes:
        if colour[root] != WHITE:
            continue
        stack = [(root, iter(successors(root)))]
        path = [root]
        colour[root] = GREY
        while stack:
            node, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                colour[node] = BLACK
                stack.pop()
                path.pop()
            elif colour[nxt] == GREY:
                return path[path.index(nxt):] + [nxt]
            elif colour[nxt] == WHITE:
                colour[nxt] = GREY
                path.append(nxt)
                stack.append((nxt, iter(successors(nxt))))
    return None
