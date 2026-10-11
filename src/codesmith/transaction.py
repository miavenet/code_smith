"""The producer's state machine, explicit (W-04 step 1): its steps, the statuses of every task,
the moves between them that the runner makes, and the keys each boundary clears.

Every step and status write in the engine and the panels goes through `move` (a step, with a
status or not), `set_status` (a status alone), `open_` (the transaction opens) or `reset` (a task
put back to pending by `retry`). Each checks the
asked move against the table and raises `RunnerError`, naming the task, the current and the asked
state, before it changes anything. The table is what the code does, not what it ought to do: a
move the suite shows the runner making is in it, with the reason where that is not obvious. docs/05-architecture.md ("The producer's state machine") shows the same table.

Step-scoped keys, and the boundary that clears them:
- `pending_*` (the attempt directory, protocol tries, calls, interruptions, the call in flight,
  a settled answer, a repair, a quota outcome): built up inside one attempt; every step move
  clears them, so none survives the move that counts the attempt, sends it back or ends it.
- `pending_set_aside` (the tree being set aside): set inside step `set-aside`; the move out of it
  clears it, as every step move clears `pending_*`.
- `provider_switched`: set by a provider switch during the author's calls; cleared with `pending_*`.
- `recover` (a queued recovery), `git_repairs`, `ignored_since_base`: cleared when the
  transaction opens (`open_`). (An older runner's `apply_patch` is migrated to `recover`: `schema`.)
- `ruled_candidate`: cleared when the transaction opens, which installs it again from the
  `recover` intent; every move out of `attempt` clears it (the reverifying attempt consumes it).
- `pending_*`, `panel`, `block_kind`, `block_reviewers`, `recover`: cleared by
  `reset` (`retry`), which then queues a new `recover` itself when the work is to be put back.
Fields a boundary sets (step, status, candidate, final, feedback, session_id, ...) are passed to
the call explicitly, so each call site still says what the move writes.
"""

STEPS = ("attempt", "inspect", "verify", "panel", "escalation", "human", "commit", "set-aside")
STATUSES = ("pending", "running", "rework", "verifying", "waiting_human", "accepted", "objected",
            "blocked", "failed", "skipped")

# step -> the steps the producer may move to; None is "not in a transaction".
STEP_MOVES = {
    None: ("attempt",),                                   # the transaction opens
    "attempt": ("attempt", "inspect", "set-aside"),       # attempt -> attempt: sent back as counted
    "inspect": ("attempt", "verify", "set-aside"),
    "verify": ("attempt", "panel", "set-aside"),
    # panel -> verify: a reader wrote and only demoted checks did (RUN, `reader_wrote`)
    "panel": ("attempt", "verify", "escalation", "human", "set-aside"),
    "escalation": ("attempt", "human"),
    "human": ("attempt", "commit"),
    "commit": (None,),                                    # accepted; `finalize_acceptance`
    "set-aside": (None,),                                 # failed or blocked; `set_aside`
}

# status -> the statuses any task may move to. One table for every kind: producers, checks,
# reviewers and human tasks share the names and most of the moves.
STATUS_MOVES = {
    "pending": ("pending", "running", "waiting_human", "accepted", "objected", "skipped"),
    # running -> pending: an interrupted standalone check is put back by `settle`; running ->
    # skipped: a verifier `set_aside` finds running (it names that status)
    "running": ("running", "rework", "verifying", "accepted", "failed", "blocked", "pending",
                "skipped"),
    "rework": ("rework", "verifying", "failed", "blocked"),
    # verifying -> running: sent back while no attempt is counted (reverifying a ruled
    # candidate counts none); verifying -> accepted: `record_acceptance`
    "verifying": ("verifying", "waiting_human", "rework", "running", "accepted", "failed",
                  "blocked"),
    # waiting_human -> skipped: a human verifier whose producer ended (`set_aside`);
    # waiting_human -> running: a person rejected a candidate whose reverification counted no
    # attempt (a ruled, restored candidate), so the next attempt is the first (RUN-60)
    "waiting_human": ("waiting_human", "verifying", "rework", "running", "accepted", "objected",
                      "blocked", "skipped"),
    # a verifier judges every candidate again: accepted and objected move between themselves
    "accepted": ("accepted", "objected", "pending"),
    # objected -> waiting_human: a person who rejected one candidate is asked about the next (ACC-12)
    "objected": ("objected", "accepted", "pending", "waiting_human"),
    "blocked": ("pending",),                              # `retry`
    "failed": ("pending",),                               # `retry`
    "skipped": ("pending",),                              # `retry` of the task that skipped it
}

# Calls of one attempt that may be interrupted (`pause --now`, a kill) before the attempt stops
# for a person, blocked. Counted apart from the three protocol tries, which only a call that
# returned can use: five lost calls in one attempt mean something keeps stopping the runner.
MAX_INTERRUPTIONS = 5

PENDING = "pending_"
SWITCH_KEYS = ("provider_switched",)
OPEN_KEYS = ("recover", "ruled_candidate", "git_repairs", "ignored_since_base")
RESET_KEYS = ("panel", "block_kind", "block_reviewers", "recover")


class RunnerError(Exception):
    """A move the table does not allow: a bug in the runner, never a decision. Nothing changed."""


def _name(value):
    return "none" if value is None else repr(value)


def _check_status(st, tid, status):
    current = st.get("status")
    if status not in STATUS_MOVES.get(current, ()):
        raise RunnerError(f"'{tid}' cannot move from status {_name(current)} to {_name(status)}"
                          f" (step {_name(st.get('step'))})")


def _check_step(st, tid, step, allowed=None):
    current = st.get("step")
    if step not in (allowed if allowed is not None else STEP_MOVES.get(current, ())):
        raise RunnerError(f"'{tid}' cannot move from step {_name(current)} to {_name(step)}"
                          f" (status {_name(st.get('status'))})")


def _clear(st, keys):
    for key in keys:
        st.pop(key, None)


def attempt_keys(st):
    """The keys of `st` that live only inside an attempt (and `pending_set_aside`)."""
    return [k for k in st if k.startswith(PENDING)]


def move(st, tid, step, **fields):
    """Move the producer `tid` to `step`, with the other `fields` (a `status` among them is
    checked too). Clears what every step move clears. Raises RunnerError on a move the table does
    not allow, before anything is written."""
    _check_step(st, tid, step)
    if "status" in fields:
        _check_status(st, tid, fields["status"])
    if st.get("step") == "attempt":
        st.pop("ruled_candidate", None)
    _clear(st, attempt_keys(st) + list(SWITCH_KEYS))
    st.update(step=step, **fields)


def open_(st, tid, **fields):
    """The transaction opens: step `attempt` from no step, status `running`. Replayed over a half-
    installed opening (`_reconcile_recover` runs `open_transaction` again) the step may already be
    `attempt`, and that is the same move."""
    replay = st.get("step") == "attempt" and st.get("status") == "running"
    if not replay:
        _check_step(st, tid, "attempt", allowed=STEP_MOVES[None] if st.get("step") is None else ())
        _check_status(st, tid, "running")
    _clear(st, attempt_keys(st) + list(SWITCH_KEYS) + list(OPEN_KEYS))
    st.update(step="attempt", status="running", **fields)


def set_status(st, tid, status, **fields):
    """A status alone, with `fields` (a reason, a decision); the step is not touched."""
    _check_status(st, tid, status)
    st.update(status=status, **fields)


def reset(st, tid, **fields):
    """`retry`: a task outside any transaction goes back to `pending`, with what its last end and
    its last attempt left cleared."""
    _check_step(st, tid, None, allowed=(None,) if st.get("step") is None else ())
    _check_status(st, tid, "pending")
    _clear(st, attempt_keys(st) + list(RESET_KEYS))
    st.update(status="pending", step=None, **fields)


def out_of_place(st):
    """The step-scoped keys `st` holds outside the step they belong to, for the invariants:
    `pending_set_aside` belongs to step `set-aside`, every other `pending_*` key to `attempt`."""
    step = st.get("step")
    return [k for k in attempt_keys(st) if st[k]
            and step != ("set-aside" if k == "pending_set_aside" else "attempt")]
