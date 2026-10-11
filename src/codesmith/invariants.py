"""Cross-field invariants of a run's state, checked on every save (W-04 step 2).

The per-task state is a free dictionary, so a field left set across a boundary is the bug class
the live runs kept finding. `check` names every rule the state breaks. It only reads; in a real
run a violation is recorded as an event and shown in STATUS.md, never raised, so the check can
never stop or change a run. Under the test suite (`CODE_SMITH_STRICT_INVARIANTS=1`) it raises, so
every scenario proves the rules hold at every save.

Each rule is one the code keeps today at every save, not one it ought to keep: an acceptance is
recorded (`record_acceptance`) one save before its bookkeeping clears the step, so an accepted
producer may still show step `commit` until `finalize_acceptance` runs.
"""

import os

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
