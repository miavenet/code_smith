"""Commands, exit codes, printing. Exit 0: fine. 2: something is wrong. 255: a person is needed."""

import argparse
import datetime
import math
import os
import subprocess
import sys
import signal
import time
from types import SimpleNamespace
import uuid

from . import __version__, budgets, engine, gitops, record, workflow, qualification, preflight, agents, replan, activity, templates, prompts, findings
from .status import export_live, export_next_steps, export_ways_on

EXIT_OK, EXIT_FAILED, EXIT_HUMAN = 0, 2, 255
HOOK_KINDS = ("claude", "codex", "copilot")          # agent kinds whose calls emit hook telemetry



def main(argv=None):
    previous = signal.getsignal(signal.SIGTERM)
    def interrupted(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    try:
        return _main(argv)
    except KeyboardInterrupt:
        return fail("interrupted; child processes stopped. Continue with runner resume")
    except (record.RecordError, gitops.GitError, gitops.RevertConflict, agents.InvocationError) as exc:
        return fail(str(exc))
    except OSError as exc:
        return fail(f"{exc.strerror}: {exc.filename}" if exc.strerror and exc.filename else str(exc))
    finally:
        signal.signal(signal.SIGTERM, previous)


def _main(argv=None):
    parser = argparse.ArgumentParser(prog="runner", description="Run a workflow of tasks to "
                                     "completion with headless coding agents.")
    parser.add_argument("--version", action="version", version=f"runner {__version__}")
    parser.add_argument("--runs-dir", metavar="DIR", help="keep the record of runs in DIR instead "
                        "of .runs/ at the top of the repository (relative to the top of the "
                        "repository); it comes before the command. Every later command on those "
                        f"runs needs it too; exporting {record.RUNS_DIR_ENV} does the same. A "
                        "workflow's [defaults] runs_dir is used when neither is given")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    p = sub.add_parser("validate", help="check everything; print the expanded DAG in execution "
                       "order")
    p.add_argument("workflow")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("new", help="write a workflow file from a template in library/workflows/")
    p.add_argument("template", nargs="?")
    p.add_argument("-o", "--output", metavar="FILE")
    p.add_argument("--params", metavar="FILE", help="a TOML file of parameter values (lists go here)")
    p.add_argument("--set", action="append", default=[], metavar="NAME=VALUE",
                   help="a string or integer parameter; overrides --params")
    p.add_argument("--library", action="append", default=[], metavar="DIR", help="a library "
                   "searched before the built-in one (repeatable, earlier wins); written as the "
                   "workflow's 'library'")
    p.add_argument("--root", metavar="DIR", help="the repository the workflow works on (default: "
                   "the git top level holding the output)")
    p.add_argument("--force", action="store_true", help="replace an existing output")
    p.add_argument("--list", action="store_true", help="list the templates")
    p.add_argument("--describe", action="store_true", help="list the template's parameters")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("graph", help="write the DAG as Graphviz DOT")
    p.add_argument("workflow")
    p.add_argument("-o", "--output", metavar="FILE")
    p.set_defaults(func=cmd_graph)

    p = sub.add_parser("doctor", help="qualify the workflow's agent profiles in scratch repositories")
    p.add_argument("workflow")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("check-gates", help="test every gate/check on disposable copies of the untouched tree")
    p.add_argument("workflow")
    p.set_defaults(func=cmd_check_gates)

    p = sub.add_parser("start", help="create a run and execute it")
    p.add_argument("workflow")
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("status", help="print STATUS.md; --rebuild regenerates all derived files")
    p.add_argument("run", nargs="?", default="latest", help="a directory name, a UUID prefix, "
                   "or 'latest'")
    p.add_argument("--rebuild", action="store_true")
    p.add_argument("--watch", nargs="?", type=float, const=5.0, metavar="N", help="print it again "
                   "whenever it changes (checked every N seconds, default 5) until interrupted")
    p.add_argument("--open", action="store_true", help="open STATUS.html with the platform's "
                   "opener (open, xdg-open), or print its path")
    p.add_argument("-C", dest="where", default=".", metavar="DIR", help="a directory inside the "
                   "repository (default: the current one)")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("activity", help="show recent native hook events while headless agents work")
    p.add_argument("run", nargs="?", default="latest")
    p.add_argument("--task")
    p.add_argument("--tail", type=int, default=20)
    p.add_argument("-C", dest="where", default=".", metavar="DIR")
    p.set_defaults(func=cmd_activity)

    p = sub.add_parser("runs", help="list runs with status, cost and date")
    p.add_argument("workflow", nargs="?", help="a workflow file, or the name of a workflow "
                   "(default: every run in the runs directory)")
    p.add_argument("-C", dest="where", default=".", metavar="DIR")
    p.set_defaults(func=cmd_runs)

    p = sub.add_parser("prune", help="delete the pinned refs of finished runs")
    p.add_argument("-C", dest="where", default=".", metavar="DIR")
    p.add_argument("--orphans", action="store_true",
                   help="also delete the refs of runs whose directory is not found in the runs "
                   "directory in use (wrong --runs-dir, or the directory was deleted)")
    p.set_defaults(func=cmd_prune)

    p = sub.add_parser("resume", help="reconcile, then continue a run (default: the latest "
                       "unfinished one)")
    p.add_argument("run", nargs="?", default="latest")
    p.add_argument("--stop-orphans", action="store_true", help="stop an agent that a dead runner "
                   "left running, instead of refusing to continue beside it")
    p.add_argument("--abandon-cleanup", action="store_true", help="close an open cleanup "
                   "without stopping its group: your ruling, recorded with your name, that its "
                   "processes are gone or may be left (when --stop-orphans keeps failing); with "
                   "--stop-orphans too, the groups of interrupted calls are stopped as well")
    p.add_argument("--by", dest="by", default="", metavar="NAME", help="who rules with "
                   "--abandon-cleanup; without it the name comes from the environment ($USER)")
    p.add_argument("-C", dest="where", default=".", metavar="DIR")
    p.add_argument("--add-budget", type=float, default=0, metavar="USD")
    p.add_argument("--add-tokens", type=int, default=0, metavar="N", help="raise the run's token "
                   "cap for agents that report no cost (run_budget_tokens)")
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("repair-record", help="put back a run's decision files that were changed "
                       "or deleted (git clean, an edit) from the copy pinned in git")
    p.add_argument("run", nargs="?", default="latest")
    p.add_argument("--dry-run", action="store_true", help="only say what is wrong; change nothing")
    p.add_argument("--accept-older", action="store_true", help="put back the pinned state.json and "
                   "integrity.json even when the state on disk is a later one (the last resort)")
    p.add_argument("-C", dest="where", default=".", metavar="DIR")
    p.set_defaults(func=cmd_repair_record)

    p = sub.add_parser("pause", help="stop a running run at the next safe point (before its next "
                       "call), or --now; prints the resume command")
    p.add_argument("run", nargs="?", default="latest")
    p.add_argument("--now", action="store_true", help="interrupt the call in flight instead of "
                   "waiting for it; that call's work and spend are lost")
    p.add_argument("--wait", type=float, default=60, metavar="MIN",
                   help="how long to wait for the runner to reach a safe point (default 60)")
    p.add_argument("-C", dest="where", default=".", metavar="DIR")
    p.set_defaults(func=cmd_pause)

    p = sub.add_parser("resolve", help="rule on an open blocking finding of a stopped task",
                       epilog=RULING_NOTE_HELP)
    p.add_argument("run")
    p.add_argument("finding")
    p.add_argument("--as", dest="decision", required=True, choices=("resolved", "advisory", "upheld"))
    p.add_argument("-m", "--note", dest="note", default="", help="the ruling, free text (see below)")
    p.add_argument("--by", dest="by", default="", metavar="NAME", help="who rules; without it the "
                   "name comes from the environment ($USER) and the record says so")
    p.add_argument("-C", dest="where", default=".", metavar="DIR")
    p.add_argument('--standing', action='store_true',
                   help='also append an advisory/resolved ruling to defaults.rulings_file')
    p.set_defaults(func=cmd_resolve)

    for name, text in (("approve", "a person approves a human task"),
                       ("reject", "a person rejects; the note becomes feedback")):
        p = sub.add_parser(name, help=text)
        p.add_argument("run")
        p.add_argument("task")
        p.add_argument("-m", dest="note", default="", metavar="NOTE", required=(name == "reject"))
        p.add_argument("--by", dest="by", default="", metavar="NAME", help="who decides; without "
                       "it the name comes from the environment ($USER) and the record says so")
        p.add_argument("-C", dest="where", default=".", metavar="DIR")
        p.set_defaults(func=cmd_decide, decision=name)

    p = sub.add_parser("export-rulings", help="write the standing rulings a run queued and "
                       "will not write itself (a run where nothing can touch the tree again)")
    p.add_argument("run")
    p.add_argument("-C", dest="where", default=".", metavar="DIR")
    p.set_defaults(func=cmd_export_rulings)

    p = sub.add_parser("retry", help="fresh attempts for a failed or blocked task")
    p.add_argument("run")
    p.add_argument("task")
    p.add_argument("--apply-patch", action="store_true", help="put the set-aside work back first, "
                   "if its base is still the accepted tree")
    p.add_argument("-C", dest="where", default=".", metavar="DIR")
    p.set_defaults(func=cmd_retry)

    p = sub.add_parser("replan", help="install an edited workflow; reopen accepted work with revert commits")
    p.add_argument("run", nargs="?", default="latest")
    p.add_argument("--reopen", action="append", default=[], metavar="TASK")
    p.add_argument("--workflow", metavar="FILE", help="revised workflow (default: the original source)")
    p.add_argument("-C", dest="where", default=".", metavar="DIR")
    p.set_defaults(func=cmd_replan)

    argv = sys.argv[1:] if argv is None else list(argv)
    misplaced = _misplaced_runs_dir(argv, set(sub.choices))
    if misplaced:
        return fail(f"--runs-dir is an option of the runner, not of '{misplaced}': put it before "
                    f"the command, as in `runner --runs-dir DIR {misplaced} ...`, or export "
                    f"{record.RUNS_DIR_ENV}=DIR")
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help(sys.stderr)
        return EXIT_FAILED
    # The location of the record is set in the environment for this command only (the workflow's
    # runs_dir, a remembered one, or the option), and given back afterwards: tests and callers
    # that run several commands in one process must not inherit it.
    previous = os.environ.get(record.RUNS_DIR_ENV)
    if args.runs_dir:
        os.environ[record.RUNS_DIR_ENV] = os.path.expanduser(args.runs_dir)
    try:
        return args.func(args)
    finally:
        if previous is None:
            os.environ.pop(record.RUNS_DIR_ENV, None)
        else:
            os.environ[record.RUNS_DIR_ENV] = previous


def _misplaced_runs_dir(argv, commands):
    """The command that `--runs-dir` was written after, or None. argparse would only say
    "unrecognized arguments"."""
    command = next((a for a in argv if a in commands), None)
    if command is None:
        return None
    after = argv[argv.index(command) + 1:]
    return command if any(a == "--runs-dir" or a.startswith("--runs-dir=") for a in after) else None


def load_workflow(path, remember=False):
    """Load a workflow and point this command at its record: `--runs-dir` or
    CODE_SMITH_RUNS_DIR if given, else the workflow's `[defaults] runs_dir`, else `.runs/`, all
    relative to the repository top. With `remember` (start), a location taken from the workflow
    is kept in the repository's git config so commands on a RUN find it; any other is forgotten."""
    wf = workflow.load(path)
    if wf.errors:
        return wf
    given = wf.defaults.get("runs_dir")
    overridden = bool(os.environ.get(record.RUNS_DIR_ENV))
    if not overridden:
        os.environ[record.RUNS_DIR_ENV] = given or record.RUNS_DIR
    if remember:
        try:
            top = gitops.Git(wf.root).top
        except gitops.GitError:
            return wf
        record.remember_runs_dir(top, record.resolve_runs_dir(top, given)
                                 if given and not overridden else None)
    return wf


def report_problems(wf, out):
    for w in wf.warnings:
        print(f"warning: {w}", file=out)
    for e in wf.errors:
        print(f"error: {e}", file=out)
    if wf.errors:
        n = len(wf.errors)
        print(f"{wf.workflow_file}: {n} error{'s' if n != 1 else ''}", file=out)


def cmd_validate(args):
    wf = load_workflow(args.workflow)
    report_problems(wf, sys.stderr)
    if wf.errors:
        return EXIT_FAILED
    print(format_dag(wf))
    return EXIT_OK


def format_dag(wf):
    lines = [f"workflow {wf.name}: {len(wf.tasks)} tasks, in execution order",
             f"root {wf.root}", ""]
    width = max(len(t["id"]) for t in wf.tasks)
    for n, t in enumerate(wf.tasks, 1):
        kind_type = t["kind"] if t["type"] == t["kind"] else f"{t['kind']}/{t['type']}"
        parts = []
        if t["needs"]:
            parts.append("needs " + ", ".join(t["needs"]))
        if t.get("reviews"):
            parts.append("reviews " + t["reviews"] + (" (advisory)" if t.get("advisory") else ""))
        if t.get("verifies"):
            parts.append("verifies " + t["verifies"])
        if t["kind"] == "check":
            mode = "read-only" if t["read_only"] else "restores" if t["restores"] else "writer"
            parts.append(mode)
        if t["kind"] in ("produce", "review"):
            parts.append("agent " + t["agent"] + (f" ({t['model']})" if t["model"] else "")
                         + (f", then {', '.join(t['fallback_agents'])}" if t.get("fallback_agents") else "")
                         + (" [by reviewer_family]" if t.get("agent_rule") else ""))
        lines.append(f"{n:>3}  {t['id']:<{width}}  {kind_type:<22} " + "; ".join(parts))
        if t["kind"] == "produce":
            lines.append(f"     {'':<{width}}  outputs: " + ", ".join(o["path"] for o in t["outputs"]))
            if t["writes"] != [o["path"] for o in t["outputs"]]:
                lines.append(f"     {'':<{width}}  writes:  " + ", ".join(t["writes"]))
            if t["removes"]:
                lines.append(f"     {'':<{width}}  removes: " + ", ".join(t["removes"]))
            for g in t["gates"]:
                lines.append(f"     {'':<{width}}  gate{' (new)' if g['new'] else ''}: "
                             + prompts.gate_line(g))
    lines.append("")
    if wf.claims:
        lines.append("claims on frozen outputs:")
        for c in wf.claims:
            consumers = ", ".join(c["consumers"]) if c["consumers"] else "none"
            lines.append(f"  {c['task']} will modify {', '.join(c['paths'])} of {c['of']} "
                         f"(accepted consumers: {consumers})")
    else:
        lines.append("claims on frozen outputs: none")
    return "\n".join(lines)


def cmd_new(args):
    try:
        if args.list:
            for name, description, path in templates.available(args.library):
                builtin = os.path.dirname(os.path.dirname(path)) == os.path.realpath(workflow.BUILTIN_LIBRARY)
                print(f"{name:<16} {description}" + ("" if builtin else f"  [{path}]"))
            return EXIT_OK
        if not args.template:
            return fail("new: give a TEMPLATE, or --list")
        t = templates.load(templates.find(args.template, args.library))
        if args.describe:
            print(templates.describe(t))
            return EXIT_OK
        if not args.output:
            return fail("new: give -o FILE for the generated workflow")
        given = templates.read_params(args.params) if args.params else {}
        values = templates.resolve_params(t, given, args.set)
        wf = templates.write(t, values, args.output, force=args.force, root=args.root,
                             library_dirs=args.library)
    except templates.TemplateError as exc:
        for w in exc.warnings:
            print(f"warning: {w}", file=sys.stderr)
        for e in exc.errors:
            print(f"error: {e}", file=sys.stderr)
        print("runner: new: nothing was written", file=sys.stderr)
        return EXIT_FAILED
    for w in wf.warnings:
        print(f"warning: {w}", file=sys.stderr)
    print(f"wrote {wf.workflow_file}: workflow {wf.name}, {len(wf.tasks)} tasks. "
          f"Next: runner validate {args.output}")
    return EXIT_OK


def cmd_graph(args):
    wf = load_workflow(args.workflow)
    report_problems(wf, sys.stderr)
    if wf.errors:
        return EXIT_FAILED
    dot = format_dot(wf)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(dot)
    else:
        sys.stdout.write(dot)
    return EXIT_OK


def _q(text):
    return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"') + '"'


SHAPES = {"produce": "box", "review": "ellipse", "check": "hexagon", "human": "octagon"}


def format_dot(wf):
    """One node per expanded task; an edge per `needs` (solid), `reviews` (dashed), `verifies` (dotted)."""
    lines = [f"digraph {_q(wf.name)} {{", "  rankdir=LR;", "  node [fontname=\"Helvetica\"];"]
    for t in wf.tasks:
        label = t["id"] + "\\n" + (t["type"] if t["type"] != t["kind"] else t["kind"])
        style = ', style="dashed"' if t.get("advisory") else ""
        lines.append(f"  {_q(t['id'])} [label=\"{label}\", shape={SHAPES[t['kind']]}{style}];")
    for t in wf.tasks:
        for n in t["needs"]:
            lines.append(f"  {_q(n)} -> {_q(t['id'])} [label=\"needs\"];")
        if t.get("reviews"):
            lines.append(f"  {_q(t['reviews'])} -> {_q(t['id'])} [label=\"reviews\", style=dashed];")
        if t.get("verifies"):
            lines.append(f"  {_q(t['verifies'])} -> {_q(t['id'])} [label=\"verifies\", style=dotted];")
    lines.append("}")
    return "\n".join(lines) + "\n"


# -- runs ---------------------------------------------------------------------------------------

def _who():
    """Who decided, for the record: the login name, or the numeric user when the environment
    names nobody (a container started with a bare uid recorded rulings by nobody)."""
    return os.environ.get("USER") or os.environ.get("LOGNAME") or f"uid {os.getuid()}"


# Variables an agent's session sets in the commands it runs. Their names are recorded with a
# ruling, never their values: a ruling typed inside such a session may be the agent's, not the
# owner's (review V-05 of the v4 runs).
AGENT_SESSION_MARKERS = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CODEX_SANDBOX",
                         "CODEX_SANDBOX_NETWORK_DISABLED", "CODE_SMITH_TASK")

RULING_NOTE_HELP = (
    "The note stays free text and is kept as written. A ruling that travels to later tasks reads "
    "best in four parts, each optional: Decision (what is decided), Accepted scope (what the "
    "owner accepts the work without), Evidence (what was shown: a command and its output, a "
    "quoted requirement), Unresolved claims (what was asserted and not shown). A scope exception "
    "is not proof that the original requirement is impossible.")


def ruling_context(by=""):
    """(who, how) of a person's decision: the name and how it was given (`by_source` "flag" for
    `--by`, "env" for the environment), whether standard input is a terminal (`interactive`), and
    the agent-session variables present (`agent_markers`, names only)."""
    try:
        interactive = bool(sys.stdin and sys.stdin.isatty())
    except (AttributeError, ValueError, OSError):
        interactive = False
    how = {"by_source": "flag" if by else "env", "interactive": interactive,
           "agent_markers": [name for name in AGENT_SESSION_MARKERS if os.environ.get(name)]}
    return (by or _who()), how


class _RulingRefused(Exception):
    """`resume --abandon-cleanup` refused as a ruling (`refuse_ruling`); nothing was recorded."""


def refuse_ruling(eng, how):
    """Why a workflow with `rulings_require_interactive` refuses this ruling, or None. One check
    for `resolve` (with `--standing` too), `approve` and `reject` (review V-04): standard input
    must be a terminal and no agent-session variable may be set. `--by` names who rules and is
    recorded; it never stands in for the terminal. A speed bump against an agent ruling in the
    owner's name, not authentication: a caller that fakes a terminal gets past it."""
    if not eng.defaults.get("rulings_require_interactive"):
        return None
    if how["agent_markers"]:
        why = "agent session variables are set (" + ", ".join(how["agent_markers"]) + ")"
    elif not how["interactive"]:
        why = "standard input is not a terminal"
    else:
        return None
    return ("this workflow takes rulings from a person at a terminal (rulings_require_interactive): "
            f"{why}. Run the command yourself in a terminal, outside an agent session; --by names "
            "who rules but does not replace the terminal. Nothing was recorded")


def fail(message):
    print(f"runner: {message}", file=sys.stderr)
    return EXIT_FAILED


def check_capabilities(wf):
    """Qualify for a new run, held to the run's limits: no run exists yet, so a failed
    qualification's probe spend stays in .runs/qualification.json and the doctor directory."""
    guard = qualification.RunBudget(wf.defaults["run_budget_usd"], wf.defaults["run_budget_tokens"],
                                    call_reserve=wf.defaults.get("unpriced_call_reserve", 0),
                                    probe_reserve=wf.defaults["probe_reserve_tokens"])
    wf.qualification_report = qualification.check_workflow(wf, guard=guard)
    problems = wf.qualification_report["problems"]
    if wf.qualification_report.get("budget_stop"):
        problems = problems + ["No run was created. Raise run_budget_usd or run_budget_tokens in the "
                               "workflow to cover qualification, or run `runner doctor` first"]
    return problems


def cmd_doctor(args):
    wf = load_workflow(args.workflow)
    report_problems(wf, sys.stderr)
    if wf.errors:
        return EXIT_FAILED
    report = qualification.check_workflow(wf, force=args.force)
    for entry in report["profiles"].values():
        meta = entry["metadata"]
        mode = "read-only" if meta["read_only"] else "writer"
        print(f"{meta['profile']['kind']} {meta['model'] or '(default model)'} {mode}: "
              + (", ".join(entry["capabilities"]) or "no capabilities qualified")
              + (" (cached)" if entry["cached"] else ""))
        observed = entry.get("observed_activity", [])
        if observed:
            print("  observed activity: " + ", ".join(observed))
        elif entry.get("activity_cause"):
            print("  observed activity: none; " + entry["activity_cause"]["message"])
        elif meta["profile"]["kind"] in HOOK_KINDS:
            print("  observed activity: none; check hook configuration/trust and stdout.log")
        else:
            print("  observed activity: none expected (a command agent has no hooks)")
        for note in entry.get("notes", []):
            print("  note: " + note)
        if entry["orphan_detection"].startswith("weaker"):
            print("warning: orphan detection is weaker on this host")
    print("qualification: " + os.path.join(record.runs_dir_for(gitops.Git(wf.root).top), "qualification.json"))
    for problem in report["problems"]:
        print(problem, file=sys.stderr)
    return EXIT_FAILED if report["problems"] else EXIT_OK


def cmd_check_gates(args):
    wf = load_workflow(args.workflow)
    report_problems(wf, sys.stderr)
    if wf.errors:
        return EXIT_FAILED
    report = preflight.check_gates(wf)
    for result in report["results"]:
        text = preflight.describe(result)
        print(f"{result['id']}: {text}" +
              (f" (same command as {result['same_as']})" if result.get("same_as") else "") +
              ("; changed paths: " + ", ".join(result["changed_paths"]) if result["changed_paths"] else ""))
    print("record: " + report["directory"])
    return EXIT_OK if report["ok"] else EXIT_FAILED


# Tests replace this to stop the runner at a named point, as a kill would.
CRASH = None


def execute(run, git):
    """Run the workflow as far as it goes. The engine works from the run's frozen copy."""
    run.crash = CRASH or run.crash
    code = engine.Engine(run, git, crash=CRASH).execute()
    status_md = os.path.join(run.path, "STATUS.md")
    print(f"run {run.name}: {run.state['status']}. See {status_md}", file=sys.stderr)
    return code


def cmd_start(args):
    wf = load_workflow(args.workflow, remember=True)
    report_problems(wf, sys.stderr)
    if wf.errors:
        return EXIT_FAILED
    git = gitops.Git(wf.root)
    original = git.current_branch()
    current_mode = wf.defaults["branch"] == "current"
    if current_mode and original is None:
        return fail("HEAD is detached, and branch = \"current\" needs a branch to commit on. "
                    "Check out a branch first")
    if wf.rulings_problem:
        return fail(wf.rulings_problem + ". Nothing was changed")
    dirty = git.dirty_paths()
    if dirty:
        shown = "".join(f"\n  {d}" for d in dirty[:20])
        return fail("the work tree is not clean, and a run starts only from a clean tree: a commit "
                    "limited to a task's paths would still take your uncommitted edits in those "
                    "files. Commit or remove these first; nothing was changed:" + shown)
    problems = check_capabilities(wf)
    if problems:
        return fail("\n".join(problems))

    runs_dir = record.ensure_runs_dir(git.top)
    lock = record.Lock(runs_dir, tree=record.tree_lock_path(git), top=git.top)
    run_id = str(uuid.uuid4())
    try:
        lock.acquire(run_id)
    except record.LockHeld as exc:
        return fail(str(exc))
    with lock:
        branch = original if current_mode else f"run/{wf.name}-{run_id[:8]}"
        # The branch intent is in the run's first state: interrupted before the branch exists,
        # the run is resumed by creating it.
        run = record.Run.create(wf, git, branch, original, run_id=run_id,
                                branch_intent=not current_mode)
        qualification.attach_run(run, wf.qualification_report)
        run.event("runner-source", **run.info["runner_source"])
        for op in [i["op"] for i in run.state["intents"] if i["kind"] == "branch"]:
            git.create_and_checkout_run_branch(branch)
            run.finish(op, branch=branch)
        print(f"run {run.name}  ({run_id})")
        print(f"record  {run.path}")
        print(f"branch  {branch}" + ("" if current_mode else f"  (checked out; was {original})"))
        return execute(run, git)


def _refuse_if_held(where):
    """A runner at work whose record was removed under it (`git clean -fdx`) still holds the work
    tree's lock, in the git directory: name it, not a missing directory or run (K9)."""
    top = gitops.find_toplevel(os.path.abspath(where))
    if top:
        git = gitops.Git(top)
        holder = record.tree_lock_holder(git)
        if holder:
            raise record.LockHeld(holder, record.tree_lock_path(git))


def _runs_dir(where):
    path = record.find_runs_dir(os.path.abspath(where))
    if not path:
        _refuse_if_held(where)
        raise record.RecordError(f"no runs directory in the repository that holds '{where}'. If the "
                                 f"runs were started with --runs-dir (or {record.RUNS_DIR_ENV}), "
                                 "give the same directory again, before the command")
    # Found by memory or by default: the rest of this command (snapshots skip it) agrees.
    os.environ.setdefault(record.RUNS_DIR_ENV, path)
    return path


def cmd_status(args):
    if args.watch is not None and not (math.isfinite(args.watch) and args.watch > 0):
        return fail("--watch takes a number of seconds greater than zero")
    try:
        runs_dir = _runs_dir(args.where)
        run = record.Run.load(record.resolve_run(runs_dir, args.run))
    except record.RecordError as exc:
        return fail(str(exc))
    code, page = _status_page(args, runs_dir, run)
    if code != EXIT_OK:
        return code
    if args.open:
        return _open_page(os.path.join(run.path, "STATUS.html"))
    sys.stdout.write(page)
    sys.stdout.flush()
    if args.watch is not None:
        return _watch(os.path.join(run.path, "STATUS.md"), args.watch)
    return EXIT_OK


def _opener():
    """The platform's command that opens a file in its default application, or None."""
    if sys.platform == "darwin":
        return ["open"]
    if sys.platform.startswith("linux"):
        return ["xdg-open"]
    return None


def _open_page(path):
    opener = _opener()
    if opener:
        try:
            if subprocess.run(opener + [path], capture_output=True).returncode == 0:
                print(f"opened {path}")
                return EXIT_OK
        except OSError:
            pass
    print(path)                                  # no opener here: the path, to open by hand
    return EXIT_OK


def _mtime(path):
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def _watch(path, interval):
    """Print the page again each time it is rewritten, until interrupted (Ctrl-C, or SIGTERM,
    which `main` turns into the same). Watching only reads; it never regenerates."""
    seen = _mtime(path)
    try:
        while True:
            time.sleep(interval)
            now = _mtime(path)
            if now is not None and now != seen:
                seen = now
                try:
                    with open(path, encoding="utf-8") as fh:
                        text = fh.read()
                except OSError:
                    continue
                sys.stdout.write("\n" + "=" * 72 + "\n" + text)
                sys.stdout.flush()
    except KeyboardInterrupt:
        return EXIT_OK


def _render_unguarded(run, full):
    """Render the derived pages for `status` with no guard, so a person sees a render error (C12):
    as a runner error naming the page, "status: <page>: <type>: <message>" (exit 2), never a
    traceback (REC-45). None when every page was written."""
    try:
        run.regenerate(full=full)
    except Exception as exc:                                # noqa: BLE001 - reported, not hidden
        page = getattr(run, "_render_target", {}).get("page", "?")
        return f"status: {page}: {type(exc).__name__}: {exc}"
    return None


def _status_page(args, runs_dir, run):
    """(exit code, the text of STATUS.md), regenerating the derived files where that is safe."""
    status_md = os.path.join(run.path, "STATUS.md")
    # A finished run has no runner writing its record, and what became of its branch changes
    # outside the runner (a merge, a push), so its derived files are refreshed on every look.
    finished = run.state["status"] in record.FINISHED_STATUSES
    # An unfinished run with no live runner (killed, or the machine rebooted) has a page that
    # still shows its last call in flight; regenerating it from outside names what was left behind.
    if args.rebuild or finished or not run.runner_alive() or not os.path.exists(status_md):
        # Derived pages share temporary paths, so rebuilding is done under the lock and
        # never beside a runner of this run. A lock held for another run is no obstacle: no runner
        # can be working on this one meanwhile.
        lock = None
        try:
            lock = record.Lock(runs_dir, top=run.info.get("git_toplevel")).acquire(
                "status " + run.state["run_id"])
        except record.LockHeld as exc:
            if exc.holder.get("run_id") in (None, run.state["run_id"], "status " + run.state["run_id"]):
                if args.rebuild:
                    return fail(f"{exc}. A runner is working on this run and keeps its files "
                                "current; --rebuild is refused while it works. Nothing was changed"), ""
                if not os.path.exists(status_md):
                    return EXIT_OK, record.render_run_status(
                        run.info, run.state, None, now=datetime.datetime.now(datetime.timezone.utc),
                        run_path=run.path, alive=True)
            else:
                error = _render_unguarded(run, args.rebuild)
                if error:
                    return fail(error), ""
        if lock:
            with lock:
                run = record.Run.load(run.path)
                error = _render_unguarded(run, args.rebuild)
            if error:
                return fail(error), ""
    with open(status_md, encoding="utf-8") as fh:
        return EXIT_OK, fh.read()


def cmd_runs(args):
    name = args.workflow
    if name and os.path.isfile(name):
        wf = load_workflow(name)
        name, where = wf.name, (wf.root if os.path.isdir(wf.root) else args.where)
    else:
        where = args.where
    try:
        runs_dir = _runs_dir(where)
        runs = record.list_runs(runs_dir, name)
    except record.RecordError as exc:
        return fail(str(exc))
    if not runs:
        return fail(f"no runs of workflow '{name}'" if name else f"no runs under {runs_dir}")
    width = max(28, *(len(r[1]) for r in runs))
    print(f"{'run':<{width}} {'status':<12} {'known spend':>11}  started")
    for _wf, run_name, path in runs:
        run = record.Run.load(path)
        spend = run.state['spend']
        unsettled = (spend.get('unsettled') or {}).get('usd', 0.0)
        print(f"{run_name:<{width}} {run.state['status']:<12} "
              f"{'$%.2f' % spend['known_usd']:>11}  {run.info['started']}"
              + (f"  (+${unsettled:.2f} unsettled)" if unsettled else "")
              + (f"  (≈${spend['estimated_usd']:.2f} estimated)" if 'estimated_usd' in spend else "")
              + "".join(f"  {w}" for w in record.window_phrases(spend)))
    return EXIT_OK


def cmd_prune(args):
    """Delete refs/code-smith/<run>/ of every run that is done. A run that is unfinished keeps its
    refs, and so does one whose set-aside work has lost its patch. A run whose directory is not
    found may live under another --runs-dir, so its refs go only with --orphans."""
    try:
        git = gitops.Git(os.path.abspath(args.where))
    except gitops.GitError as exc:
        return fail(str(exc))
    runs_dir = record.runs_dir_for(git.top)
    known = {name: path for _wf, name, path in record.list_runs(runs_dir)}
    for name in git.pinned_runs():
        if name not in known:
            if not args.orphans:
                print(f"kept    {name}: no run directory in {runs_dir}; if it was started with "
                      "another --runs-dir, prune with that one, or pass --orphans to delete its refs")
                continue
            reason = "its directory is not found"
        else:
            run = record.Run.load(known[name])
            if run.state["status"] not in record.FINISHED_STATUSES:
                print(f"kept    {name}: the run is {run.state['status']}")
                continue
            missing = [t for t, st in run.state["tasks"].items()
                       if st["status"] in ("failed", "blocked")
                       and not os.path.exists(os.path.join(run.task_dir(t), "failed.patch"))]
            if missing:
                print(f"kept    {name}: no failed.patch for {', '.join(missing)}")
                continue
            reason = "the run is done"
        refs = git.unpin_run(name)
        print(f"pruned  {name}: {len(refs)} ref(s), {reason}")
    return EXIT_OK


def _open_run(args, unfinished_only=False, integrity=True):
    """(run, git, lock) for a command that changes a run. The caller releases the lock. The state
    is read only once the lock is held, so it is never one a runner was still changing. A record
    that fails its integrity check, or whose state.json was edited, is refused before anything
    is written into it, since the next save would pin the edit as the runner's own (K6).
    `resume` passes `integrity=False`: it reconciles first (an interrupted decision leaves a file
    ahead of its manifest entry), and `reconcile` checks the whole record before any of its
    effects; an edit no kill can make (state.json, the pinned history of events.jsonl) is refused
    here all the same. An open cleanup is refused after them, with its own remedy (PROC-23):
    the record is intact, so `repair-record` is never named for it."""
    runs_dir = _runs_dir(args.where)
    _refuse_if_held(args.where)
    path = record.resolve_run(runs_dir, args.run, unfinished_only=unfinished_only
                              and args.run in (None, "", "latest"))
    identity = record.read_json(os.path.join(path, "run.json"))
    git = gitops.Git(identity["git_toplevel"])
    # The work tree's lock too: a runner of another runs directory may be working on this checkout.
    lock = record.Lock(runs_dir, tree=record.tree_lock_path(git),
                       top=git.top).acquire(identity["run_id"])
    try:
        run = record.Run.load(path)
        problems = (run.record_problems() if integrity
                    else run.state_changed() + run.events_changed())
        if problems:
            raise record.RecordError("the run record was changed: " + "; ".join(problems) + ". "
                                     + record.repair_hint(run.name))
        if integrity and run.cleanup_problems():
            raise record.RecordError(record.cleanup_refusal(run))
    except BaseException:
        lock.release()
        raise
    return run, git, lock


def cmd_pause(args):
    """A pause is addressed to the runner through its own lock, never by process name."""
    runs_dir = _runs_dir(args.where)
    try:
        path = record.resolve_run(runs_dir, args.run, unfinished_only=args.run in (None, "", "latest"))
        run = record.Run.load(path)
    except record.RecordError as exc:
        return fail(str(exc))
    holder = record.Lock(runs_dir, top=run.info.get("git_toplevel")).holder() or {}
    identity = holder.get("process") or {}
    if holder.get("run_id") != run.state["run_id"] or not record.is_alive(identity):
        run.clear_pause()
        print(f"run {run.name} is {run.state['status']}; no runner is working on it, nothing to pause",
              file=sys.stderr)
        return EXIT_OK
    pid = identity["pid"]
    if args.now:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass        # it exited between the liveness check and now: the wait below sees that
        deadline = time.monotonic() + 120
        how = "interrupted"
    else:
        run.request_pause()
        deadline = time.monotonic() + args.wait * 60
        how = "paused"
        print(f"pause requested; the runner (pid {pid}) stops before its next call. In flight now:",
              file=sys.stderr)
        for intent in run.state.get("intents", []):
            if intent.get("kind") in ("agent", "command"):
                print(f"  {intent['kind']} of '{intent.get('task', '?')}' since {intent.get('at', '?')}",
                      file=sys.stderr)
    while record.is_alive(identity):
        if time.monotonic() > deadline:
            if args.now:
                return fail(f"the runner (pid {pid}) did not exit within 2 minutes of SIGTERM; "
                            "look at it before resuming")
            return fail(f"the runner (pid {pid}) has not reached a safe point in {args.wait:g} min; "
                        "the request stays in place and it will stop at the next one, or pass "
                        "--now to interrupt the call in flight")
        time.sleep(0.5)
    run = record.Run.load(path)
    # The runner is gone; its last STATUS.md still shows its call as in flight. Rewrite the page
    # from outside, where an open intent is one left behind, so it says so before the resume.
    # Under the lock, as `status` does: a resume that took it at once keeps the page itself.
    try:
        lock = record.Lock(runs_dir, top=run.info.get("git_toplevel")).acquire(
            "status " + run.state["run_id"])
    except record.LockHeld:
        lock = None
    if lock:
        with lock:
            run = record.Run.load(path)
            run.regenerate()
    print(f"run {run.name}: {how}; {run.state['status']}"
          + (f" ({run.state['stop_reason']})" if run.state.get("stop_reason") else ""), file=sys.stderr)
    print(f"Continue with: runner resume {run.name}", file=sys.stderr)
    return EXIT_OK


def cmd_repair_record(args):
    """The way back from "the run record was changed" or "was removed" (W-02): the decision files
    are put back from refs/code-smith/<run>/_record, under the lock, with a `record-repaired`
    event; `resume` then continues from the restored state."""
    try:
        git = gitops.Git(os.path.abspath(args.where))
        runs_dir = record.runs_dir_for(git.top)
        path = record.find_run(runs_dir, git, args.run)
        name = os.path.basename(path)
        if args.dry_run:
            report = record.repair_record(path, git, dry_run=True, accept_older=args.accept_older)
        else:
            os.makedirs(runs_dir, exist_ok=True)
            if os.path.realpath(runs_dir).startswith(git.top + os.sep):
                record.dress_runs_dir(runs_dir)
            # The lock names the run by the pinned identity, never by the state being repaired,
            # which may not even parse (K5).
            run_id = record.pinned_run_id(git, name) or name.rsplit("-", 1)[-1]
            with record.Lock(runs_dir, tree=record.tree_lock_path(git), top=git.top).acquire(run_id):
                report = record.repair_record(path, git, accept_older=args.accept_older)
    except (record.RecordError, gitops.GitError) as exc:
        return fail(str(exc))
    for line in report["kept"]:
        print(f"kept      {line}")
    for w in report["wrong"]:
        print(f"{'would restore' if args.dry_run else 'restored'}  {w['path']}: {w['was']} "
              f"(recorded sha256 {w['recorded_sha256'][:12]}"
              + (f", found {w['found_sha256'][:12]})" if w["found_sha256"] else ")"))
    for rel in report["lost"]:
        print(f"lost      {rel}: removed with its logs (prompts, output); not restorable. The "
              "attempt's outcome is in the state and the intents, so `resume` goes on from there")
    for rel in report["rebuilt"]:
        print(f"rebuilt   {rel} from workflow.expanded.json")
    if report["problems"]:
        return fail("the record still fails its integrity check, and these have no pinned copy: "
                    + "; ".join(report["problems"]))
    if not report["wrong"] and not report["lost"]:
        print(f"run {name}: the record matches its pinned copy; nothing to repair")
    elif args.dry_run:
        print(f"Nothing was changed. Repair with: runner repair-record {name}")
    else:
        print(f"Continue with: runner resume {name}")
    return EXIT_OK


def _obligations_changed(before, run):
    """The cleanup obligations a failed `reconcile` recorded before its error, in words: each
    one opened or closed or abandoned since `before` (their statuses then)."""
    return [f"{entry['invocation']}: cleanup {entry['cleanup']['status']}"
            for i, entry in enumerate(run.state.get('cleanup_obligations', []))
            if (before[i] if i < len(before) else None) != entry['cleanup']['status']]


def cmd_resume(args):
    if not math.isfinite(args.add_budget) or args.add_budget < 0:
        return fail("--add-budget must be a finite nonnegative amount")
    if args.add_tokens < 0:
        return fail("--add-tokens must be a nonnegative number of tokens")
    # A flag that would be ignored is refused before anything (PROC-27). --abandon-cleanup and
    # --stop-orphans never target one group: the first closes the open cleanups, the second then
    # stops only the groups of interrupted calls, so together they are one resume (PROC-28).
    if args.by and not args.abandon_cleanup:
        return fail("--by needs --abandon-cleanup; nothing was recorded")
    try:
        run, git, lock = _open_run(args, unfinished_only=True, integrity=False)
    except (record.RecordError, gitops.GitError) as exc:
        return fail(str(exc))
    with lock:
        if run.state["status"] == "done":
            print(f"run {run.name} is done; nothing to resume")
            return EXIT_OK
        branch = run.info["branch"]
        if git.current_branch() != branch and not any(i["kind"] == "branch"
                                                      for i in run.state["intents"]):
            return fail(f"reconciliation error: the run works on branch '{branch}', but "
                        f"'{git.current_branch()}' is checked out. Check out '{branch}' first")
        abandon = None
        if args.abandon_cleanup:
            if not run.cleanup_problems():
                return fail("--abandon-cleanup: no cleanup is open in this run; nothing was recorded")

            def ruling():
                # Called by `reconcile` after its integrity check, so the workflow it reads
                # (`rulings_require_interactive`) is the verified one (PROC-27).
                who, how = ruling_context(args.by)
                refused = refuse_ruling(engine.Engine(run, git), how)
                if refused:
                    raise _RulingRefused(refused)
                return dict(by=who, **how, at=datetime.datetime.now(datetime.timezone.utc).isoformat())
            abandon = ruling
        before = [e['cleanup']['status'] for e in run.state.get('cleanup_obligations', [])]
        try:
            for line in record.reconcile(run, git, stop_orphans=args.stop_orphans,
                                         crash=CRASH or record._no_crash, abandon=abandon):
                print(f"reconciled: {line}")
        except _RulingRefused as exc:
            return fail(str(exc))
        except record.CleanupOpen as exc:
            return fail(f"reconciliation error: {exc}")
        except record.ReconcileError as exc:
            recorded = _obligations_changed(before, run)
            if recorded:
                # A cleanup closed or abandoned before the error stays so: say it (PROC-28).
                return fail(f"reconciliation error: {exc}. Recorded before it: "
                            + "; ".join(recorded) + ". Put back what the error names, then "
                            "`runner resume`")
            if args.abandon_cleanup and isinstance(exc, record.OrphanAlive):
                # Refused before the ruling was recorded (PROC-28).
                return fail(f"reconciliation error: {exc}. Nothing was recorded; `runner resume "
                            f"{run.name} --abandon-cleanup --stop-orphans` abandons the cleanup "
                            "and stops it in one resume")
            return fail(f"reconciliation error: {exc}. Nothing was changed; put it back as it "
                        "was, then `runner resume`")
        except gitops.RestoreError as exc:
            return fail(f"environment failure: {exc}")
        # A run recorded before its frozen definitions were protected gets them registered as
        # they are now; from here on a change to them stops the run.
        run.protect_definitions(missing_only=True)
        run.event("runner-source", **record.runner_source())     # which source continues it (RUN-49)
        if args.add_budget:
            if not math.isfinite(run.state["run_budget_usd"] + args.add_budget):
                return fail("the resulting run budget must be finite")
            run.state["run_budget_usd"] += args.add_budget
            run.save()
            run.event("budget-added", amount_usd=args.add_budget, budget_usd=run.state["run_budget_usd"])
        if args.add_tokens:
            if not run.state.get("run_budget_tokens"):
                return fail("this run has no token cap (run_budget_tokens is 0 in its workflow), "
                            "so there is nothing to add to")
            run.state["run_budget_tokens"] += args.add_tokens
            run.save()
            run.event("budget-added", amount_tokens=args.add_tokens,
                      budget_tokens=run.state["run_budget_tokens"])
        if any(st["kind"] in ("produce", "review") for st in run.state["tasks"].values()):
            frozen = record.read_json(os.path.join(run.path, "workflow.expanded.json"))
            wf = SimpleNamespace(root=frozen["paths"]["root"], tasks=frozen["tasks"], agents=frozen["agents"])
            # A run frozen before probe_reserve_tokens existed takes the built-in bound (BUD-24).
            reserve = frozen.get("defaults", {}).get("probe_reserve_tokens",
                                                     budgets.PROBE_RESERVE_TOKENS)
            report = qualification.check_workflow(
                wf, locked=True, guard=qualification.RunBudget.of_run(run.state, reserve))
            if report.get("budget_stop"):
                # The probes made are charged; the run stops like any budget stop.
                qualification.charge_run(run, report)
                stop = budgets.Exhausted(report["budget_stop"]["reason"], hint=report["budget_stop"]["hint"])
                return engine.Engine(run, git).budget_stop(stop)
            if report["problems"]:
                qualification.charge_run(run, report)
                return fail("\n".join(report["problems"]))
            qualification.attach_run(run, report)
            run.state.pop("needs_qualification", None)
            run.save()
        return execute(run, git)


def cmd_decide(args):
    try:
        run, git, lock = _open_run(args)
    except (record.RecordError, gitops.GitError) as exc:
        return fail(str(exc))
    with lock:
        who, how = ruling_context(getattr(args, "by", ""))
        eng = engine.Engine(run, git)
        refused = refuse_ruling(eng, how)
        if refused:
            return fail(refused)
        try:
            engine.decide(run, eng, args.task, args.decision, args.note, who=who, how=how)
        except engine.Refused as exc:
            return fail(str(exc))
    done = "approved" if args.decision == "approve" else "rejected"
    print(f"{args.task}: {done}. Continue with: runner resume {run.name}")
    return EXIT_OK


def cmd_retry(args):
    try:
        run, git, lock = _open_run(args)
    except (record.RecordError, gitops.GitError) as exc:
        return fail(str(exc))
    with lock:
        try:
            done = engine.retry(run, engine.Engine(run, git), git, args.task, args.apply_patch)
        except engine.Refused as exc:
            return fail(str(exc))
    if done["recovering"] is not None:
        how = f", continuing from the set-aside work of attempt {done['recovering']}"
        how += ("; the human rulings allow inspection and gates without another author call"
                if done.get("ruled") else " and the panel's open findings, which the author answers first"
                if done.get("continued") else "")
    elif done["set_aside"] is not None:
        how = (f", starting clean. The set-aside work of attempt {done['set_aside']} stays in "
               "failed.patch and will not be put back")
    else:
        how = ", starting clean"
    print(f"{args.task}: fresh attempts{how}. Continue with: runner resume {run.name}")
    return EXIT_OK


def cmd_resolve(args):
    try:
        run, git, lock = _open_run(args)
    except (record.RecordError, gitops.GitError) as exc:
        return fail(str(exc))
    with lock:
        who, how = ruling_context(args.by)
        eng = engine.Engine(run, git)
        refused = refuse_ruling(eng, how)
        if refused:
            return fail(refused)
        try:
            if args.standing:
                name = eng.defaults.get('rulings_file')
                if not name:
                    raise engine.Refused('--standing requires [defaults] rulings_file')
                if args.decision == 'upheld' or not args.note.strip():
                    raise engine.Refused('--standing needs advisory/resolved and a nonempty note')
                tid = args.finding.split('/', 1)[0]
                found = next((f for f in run.state['tasks'].get(tid, {}).get('ledger', {}).get('findings', [])
                              if f['id'] == args.finding), None)
                if found is None:
                    raise engine.Refused(f'{args.finding} is not a known finding')
                # Escape glob metacharacters: exporting an exact location must not broaden it.
                location = ''.join({'[': '[[]', '*': '[*]', '?': '[?]'}.get(c, c)
                                   for c in found['location']) or '*'
                effective = eng.effective_brief(eng.tasks[tid])
                # Queued in the state, which `engine.resolve` saves with the ruling: one durable
                # write holds both. The file in the tree is written when the run is done (V-01),
                # to the file named now (`file`, RUN-76), and only an entry the file's own reader
                # accepts is queued (RUN-78).
                entry = dict(
                    task=tid, finding_title=found['title'], location_glob=location,
                    decision=args.decision, note=args.note,
                    ruled_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    by=who, **{k: how[k] for k in findings.PROVENANCE if k in how},
                    run=run.name, finding=args.finding,
                    brief_sha256=findings.brief_hash(effective),
                    brief_parts=findings.brief_parts(effective), file=name)
                try:
                    findings.check_entry(entry)
                except ValueError as exc:
                    raise engine.Refused(f"--standing: the entry for {args.finding} would be "
                                         f"refused by the rulings file's reader ({exc}); nothing "
                                         "was recorded") from exc
                run.state.setdefault('standing_pending', []).append(entry)
            engine.resolve(run, eng, args.finding, args.decision, args.note, who=who, how=how)
        except (engine.Refused, OSError, ValueError) as exc:
            return fail(str(exc))
    tid = args.finding.split("/", 1)[0]
    next_step = (f"runner retry {run.name} {tid} --apply-patch, then "
                 if run.state["tasks"][tid]["status"] == "blocked" else "")
    print(f"{args.finding}: {args.decision}. Continue with: {next_step}runner resume {run.name}")
    if args.standing:
        print(f"The standing ruling is queued in the run record; it is written to "
              f"{eng.defaults['rulings_file']} when the run is done, for you to commit")
    return EXIT_OK


EXPORTABLE = ("stopped", "failed", "needs_human")


def cmd_export_rulings(args):
    """The standing rulings of a run that never reaches `done` (RUN-77): written as `done` would
    write them (the same idempotence, the same event), into the files they were bound to. Only
    when nothing in the run can touch the work tree again (RUN-81): every task terminal, no
    producer's transaction, no operation to reconcile, no open cleanup; and with its branch
    checked out and the tree and tip as the stop left them. A running check would restore its
    base over the file on `resume`, and a producer left starts only from a clean tree. The
    remembered tree then takes the written file, and the owner commits it: `resume` and `replan`
    take that commit, and `retry` adopts it."""
    try:
        run, git, lock = _open_run(args)
    except (record.RecordError, gitops.GitError) as exc:
        return fail(str(exc))
    with lock:
        state = run.state
        queued = [e for e in state.get("standing_pending", []) if not e.get("written")]
        if not queued:
            print(f"run {run.name} has no standing ruling waiting to be written; nothing to do")
            return EXIT_OK
        if state["status"] not in EXPORTABLE:
            return fail(f"run {run.name} is {state['status']}"
                        + ("; `runner resume` reconciles it first" if state["status"] == "running"
                           else "") + ". Nothing was written")
        live = export_live(state)
        if live:
            ways = export_ways_on(run.name, state)
            return fail(f"run {run.name} can still change the work tree ({', '.join(live)}), and "
                        f"a file written now would be undone or stop it. {ways[0].upper()}"
                        f"{ways[1:]}. Nothing was written")
        if git.current_branch() != run.info["branch"]:
            return fail(f"the run works on branch '{run.info['branch']}'; check it out first. "
                        "Nothing was written")
        eng = engine.Engine(run, git)
        expect = state.get("expect")
        # A tip moved only by the owner's commit of an earlier export is the stop's (RUN-81).
        if expect and ((git.head() != expect["tip"]
                        and not record._export_committed(run, git, expect["tip"], git.head()))
                       or eng.snapshot() != expect["tree"]):
            return fail("the branch or the work tree changed since the run stopped; put it back "
                        f"as it was (`runner resume {run.name}` refuses it too). Nothing was written")
        try:
            done = engine.export_queued(run, git.top, eng.defaults.get("rulings_file"),
                                        CRASH or engine._env_crash)
        except engine.StandingExportError as exc:
            return fail(f"{exc}. Fix it, then `runner export-rulings {run.name}`")
        if expect:
            # The written file is the only change (RUN-77), and nothing in the run can touch the
            # tree again (RUN-81): `resume` re-renders with it in the tree, or after its commit.
            expect["tree"] = eng.snapshot()
        run.save()
        run.regenerate_safely()
    for name, count in done:
        print(f"{count} standing ruling(s) written to {name}; "
              + export_next_steps(run.name, run.state))
    return EXIT_OK


def cmd_replan(args):
    try:
        run, git, lock = _open_run(args)
    except (record.RecordError, gitops.GitError) as exc:
        return fail(str(exc))
    with lock:
        source = args.workflow or run.state.get("workflow_file") or run.info["workflow_file"]
        try:
            directory, plan = replan.prepare(run, git, source, args.reopen)
            print("replan changes: " + (", ".join(plan["changes"]) or "none"))
            print("affected tasks: " + (", ".join(plan["affected"]) or "none"))
            print("revert commits: " + (", ".join(c[:7] for c in plan["commits"]) or "none"))
            print(replan.execute(run, git, directory, CRASH or record._no_crash))
            for line in replan.queued(run, plan["affected"]):
                print(line)
        except (replan.Refused, gitops.GitError, gitops.RevertConflict) as exc:
            return fail(str(exc))
    print(f"Continue with: runner resume {run.name}")
    return EXIT_OK


def cmd_activity(args):
    if args.tail < 1 or args.tail > 1000:
        return fail("--tail must be between 1 and 1000")
    try:
        path = record.resolve_run(_runs_dir(args.where), args.run)
    except record.RecordError as exc:
        return fail(str(exc))
    sys.stdout.write(activity.render(path, args.task, args.tail))
    return EXIT_OK
