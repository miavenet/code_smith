"""Cross-field invariants of a run's state, checked on every save (W-04 step 2).

The per-task state is a free dictionary, so a field left set across a boundary is the bug class
the live runs kept finding. `check` names every rule the state breaks. It only reads; in a real
run a violation is recorded as an event and shown in STATUS.md, never raised, so the check can
never stop or change a run. Under the test suite (`CODE_SMITH_STRICT_INVARIANTS=1`) it raises, so
every scenario proves the rules hold at every save.

Each rule is one the code keeps today at every save, not one it ought to keep: an acceptance is
recorded (`record_acceptance`) one save before its bookkeeping clears the step, so an accepted
producer may still show step `commit` until `finalize_acceptance` runs.

A second tier (C15) runs only under the strict flag: named rules (`@rule`), each derived from a
consequence a person would see (a ruling with nowhere to go, a ruling a command lost), never
from what the code does.
`at="save"` rules read the record after every `Run.save`; `at="boundary"` rules may read git and
the work tree, at the end of each CLI command (`cli.main`), after a review panel is applied and
in the tests' `check_invariants`. A broken one raises `Violation` naming the rule. In a real run
nothing of this tier runs: no read, no event, no key; `check` above is unchanged. They are
judges, not explorers: a rule fires only on a path some test drives, and a liveness property
("some command finishes") is a recovery-sequence test, never a rule.
"""

import json
import os
import subprocess
import tomllib

from .transaction import STATUSES, STEPS, MAX_INTERRUPTIONS, out_of_place

STRICT_ENV = "CODE_SMITH_STRICT_INVARIANTS"
ENDED = ("accepted", "objected", "blocked", "failed", "skipped")
MAX_PROTOCOL_TRIES = 3                      # one call and engine.PROTOCOL_RETRIES more


class Violation(AssertionError):
    """Raised only in strict mode."""


def strict():
    return os.environ.get(STRICT_ENV) == "1"


def _open_blockers(st):
    ledger = st.get("ledger") or {}
    return [f["id"] for f in ledger.get("findings", [])
            if f.get("severity") == "blocking" and f.get("status") in ("open", "disputed", "escalated")]


def check(state, definitions=None):
    """The rules `state` breaks, in words; [] when it keeps them all. `definitions`, the frozen
    task definitions by id, adds the rules that need the workflow (the attempt limit)."""
    problems = []
    tasks = state.get("tasks", {})
    active = state.get("active_producer")
    if active is not None:
        st = tasks.get(active)
        if st is None or st.get("kind") != "produce":
            problems.append(f"the active producer '{active}' is not a producer of this run")
        elif not st.get("step"):
            problems.append(f"the active producer '{active}' has no step")
        elif st.get("status") in ENDED:
            problems.append(f"the active producer '{active}' is {st['status']}")
    for tid, st in tasks.items():
        status, step = st.get("status"), st.get("step")
        if status not in STATUSES:
            problems.append(f"'{tid}' has the unknown status {status!r}")
        if step is not None and step not in STEPS:
            problems.append(f"'{tid}' has the unknown step {step!r}")
        if st.get("kind") != "produce":
            if step:
                problems.append(f"'{tid}' is a {st.get('kind')} task with step '{step}'")
            continue
        if step and tid != active and not (status == "accepted" and step == "commit"):
            problems.append(f"'{tid}' has step '{step}' but is not the active producer")
        for key in out_of_place(st):                        # the transaction table's scopes
            problems.append(f"'{tid}' holds a pending attempt at step '{step}'"
                            if key == "pending_attempt" else f"'{tid}' holds '{key}' at step '{step}'")
        if st.get("pending_protocol_tries", 0) > MAX_PROTOCOL_TRIES:
            problems.append(f"'{tid}' was charged {st['pending_protocol_tries']} protocol tries")
        if st.get("pending_interruptions", 0) > MAX_INTERRUPTIONS:
            problems.append(f"'{tid}' counted {st['pending_interruptions']} interrupted calls")
        if st.get("candidate") and not st.get("base"):
            problems.append(f"'{tid}' has a candidate but no base")
        if status == "accepted":
            if not st.get("commit"):
                problems.append(f"'{tid}' is accepted without a commit")
            if not st.get("candidate"):
                problems.append(f"'{tid}' is accepted without a candidate")
            blockers = _open_blockers(st)
            if blockers:
                problems.append(f"'{tid}' is accepted with blocking findings open: "
                                + ", ".join(blockers))
        limit = (definitions or {}).get(tid, {}).get("max_attempts")
        if tid == active and limit is not None and st.get("attempts_used", 0) > limit:
            problems.append(f"'{tid}' used {st['attempts_used']} attempts of {limit}")
    ops = [it.get("op") for it in state.get("intents", [])]
    if len(ops) != len(set(ops)):
        problems.append("two open intents share an operation id")
    return problems


# -- the strict-only tier (C15) ----------------------------------------------------------------

RULES = []                                  # (name, at, fn); `at` is "save" or "boundary"
OPENED = {}                                 # strict only: run path -> the Run this command saved


def rule(name, at="save"):
    """A strict-only rule: `fn(run, before)` returns the problems it finds, in words. `before` is
    the state at the previous strict save of that Run (None at the first, and at a boundary).
    A rule only reads; it never writes the record, the tree or git."""
    def add(fn):
        RULES.append((name, at, fn))
        return fn
    return add


def strict_check(run, at, before=None):
    """Raise `Violation` naming every rule of tier `at` that `run` breaks."""
    found = [f"{name}: {p}" for name, where, fn in RULES if where == at for p in fn(run, before)]
    if found:
        raise Violation("; ".join(found))


def strict_boundary():
    """At the end of a CLI command: the boundary rules over every run the command saved."""
    runs = list(OPENED.values())
    OPENED.clear()
    for run in runs:
        strict_check(run, "boundary")


def _frozen_defaults(run):
    """The `[defaults]` of the run's frozen workflow (`workflow.expanded.json`); {} when there is
    none to read."""
    try:
        with open(os.path.join(run.path, "workflow.expanded.json"), encoding="utf-8") as fh:
            return json.load(fh).get("defaults") or {}
    except (OSError, ValueError, AttributeError):
        return {}


@rule("queued-entry-has-file")                                    # I3, RUN-76
def _queued_entry_has_file(run, before):
    """A standing ruling the owner made has somewhere to go: every entry of `standing_pending`
    names its rulings file, or the run's frozen workflow names one (`rulings_file`) for an entry
    queued before entries were bound. Without one the ruling can never be written."""
    loose = [e for e in run.state.get("standing_pending", []) if not e.get("file")]
    if not loose or _frozen_defaults(run).get("rulings_file"):
        return []
    return [f"{e.get('finding', '?')} names no rulings file and the run's workflow sets no "
            "rulings_file" for e in loose]


LOST_AT_OPEN = {}                           # strict only: run path -> written entries missing at open


def _rulings_at_tip(top, name):
    """The rulings in `name` as the branch's last commit holds it; [] when it holds none."""
    from . import findings
    got = subprocess.run(["git", "show", f"HEAD:{name}"], cwd=top, capture_output=True)
    if got.returncode:
        return []
    try:
        return findings.parse_rulings(tomllib.loads(got.stdout.decode("utf-8")))
    except (UnicodeDecodeError, ValueError):
        return []


def _lost(run):
    """[(entry, file)]: the entries marked written that their rulings file (`file`, or the frozen
    `rulings_file`) holds neither in the work tree nor in the branch's last commit."""
    from . import findings
    written = [e for e in run.state.get("standing_pending", []) if e.get("written")]
    if not written:
        return []
    top = run.info["git_toplevel"]
    default = _frozen_defaults(run).get("rulings_file")
    lost = []
    for e in written:
        name = e.get("file") or default
        try:
            present = findings.read_rulings(findings.rulings_path(top, name))
        except (OSError, ValueError, TypeError):
            present = []
        if not any(findings.same_ruling(e, p) for p in present) and not any(
                findings.same_ruling(e, p) for p in _rulings_at_tip(top, name or "")):
            lost.append((e, name))
    return lost


def _key(e):
    return (e.get("run"), e.get("finding"), e.get("ruled_at"))


def note_open(run):
    """At a command's first load of a run (`Run.load`, strict only): the written entries already
    missing then. Reads only; cleared by `cli.main` at the next command."""
    if run.path not in LOST_AT_OPEN:
        try:
            LOST_AT_OPEN[run.path] = {_key(e) for e, _n in _lost(run)}
        except (OSError, ValueError, KeyError):
            LOST_AT_OPEN[run.path] = set()


@rule("written-ruling-kept", at="boundary")                       # I1, RUN-81
def _written_ruling_kept(run, before):
    """A runner command never loses a ruling the run says it wrote: an entry marked `written`
    that its rulings file held (in the work tree or the branch's last commit) when the command
    opened the run, or that the command marked written, is still held when it ends. Stated over
    the command, not one state: an entry already missing at the open was taken away by a person
    between commands, which the runner refuses and restores (RUN-87, RUN-88) or takes as a
    withdrawal after done (RUN-89); read on one state, the rule condemned those valid journeys."""
    was = LOST_AT_OPEN.get(run.path) or set()
    return [f"{e.get('finding', '?')} is marked written to {name}, which holds it neither in the "
            "work tree nor in the branch's last commit" for e, name in _lost(run)
            if _key(e) not in was]
