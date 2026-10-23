"""The shape of `state.json`, versioned (W-04 step 3).

`schema_version` says which shape a state has: absent (0) in every state written before it
existed, SCHEMA_VERSION in what this runner writes. `Run.load` calls `migrate` once, so the
engine reads one shape only and the older ones are handled here, each by a rule that writes the
value meaning what the old shape meant, instead of a code path that tests for a missing key.

What 0 -> 1 does (each is what an older runner left; a current state has none of them):
- `unpriced_call_reserve` absent (before BUD-17): set to `budgets.SERIAL_RESERVE`, which keeps
  that run's unpriced calls reserving the whole remainder under the cap, one at a time.
- an agent intent without `token_reservation` (before BUD-12): 0, so its call counts nothing
  extra if its usage stays unknown, as before.
- a bare `apply_patch: true` (the request of an older `retry --apply-patch`): the queued `recover`
  it means, read from the task's set-aside record (derived from the pinned tree when the record
  was never published).
- `pending_*` keys outside the step they belong to (left by an older end of an attempt): dropped.
- a producer in an attempt, whose tries were charged before each call: `pending_calls` is that
  count; a settled answer is marked paid; a call still in flight has its try given back and is
  marked in flight (`pending_call`), so `resume` counts it as an interruption, as this runner does
  (protocol tries are charged on return).

What 1 -> 2 does: nothing. Version 2 marks the keys and job kinds a version-1 runner would
misread (`save_seq`, `pin_stale`, `agent_spans`, an intent's `token_remainder`, a `replay`
intent and panel job, `replay_passed`, a panel's `standing_rulings`, a panel job's `in_flight`
and `interruptions`, an outcome's `before_work`, a repair's `hints`
(`panel.jobs.*.result.repair`), a raised finding's `seat`), so a version-1 runner refuses the
state instead of resuming it: it would send a `replay` job down the check branch. A version-1
state has none of them and means what it meant. Version 2 guarantees nothing against a runner
that itself writes version 2: the first published one knew only these keys.

What 2 -> 3 does: nothing. Version 3 marks the keys added after the first version-2 runner,
which a version-2 runner older than the key would misread (REC-53): `cleanup_obligations`, with each entry's
`cleanup.status` (`open`, `closed`, `abandoned`) and an abandoned one's `ruling`; an outcome's
`cleanup` (in a panel job's `raw_outcome`, a task's `pending_author_result.result` and
`pending_provider_quota`); a panel job's `guard_problems`; an intent's `cancelled` (a reader
cancelled before release whose group outlived its stop, PROC-25: only the first version-3
runner wrote it; this one reads it as the open cleanup it records instead); a panel's
`standing_dropped_why` and the run's `standing_pending`. A version-2 runner refuses such a state
instead of resuming it: it would dispatch beside an open cleanup, use an answer whose reader
changed the record, settle a cancelled call as interrupted, and drop a queued standing export.
Which version-2 state holds which of them depends on its writer: one written by the first
version-2 runner has none of them; one written by a later version-2 runner may hold some, with
the same meaning — the runner that queued standing exports wrote `standing_pending` and a
panel's `standing_dropped_why`, the one that kept cleanup obligations wrote
`cleanup_obligations`, an outcome's `cleanup` and a panel job's `guard_problems`, and the last
version-2 runner an intent's `cancelled`. The no-op `_to_3` keeps each as it is, and this runner
reads it with that meaning (REC-53).

What 3 -> 4 does: nothing. Version 4 marks a queued standing entry's `file`, the rulings file
it was bound to when it was queued (RUN-76). A version-3 runner would ignore it and write the
key itself into the rulings file, which its reader then refuses at every `start`; so it refuses
the state instead. A version-3 state's entries have no `file` and are written to the workflow's
`rulings_file`, as they were.
"""

from . import transaction

SCHEMA_VERSION = 4


class NewerState(Exception):
    """The state was written by a newer runner, whose shape this one does not know."""


def migrate(state, run=None):
    """Bring `state` to SCHEMA_VERSION, in place. `run`, when given, is the `Run` it belongs to:
    a legacy `apply_patch` needs its set-aside record. Returns (from, to) when the state changed
    shape, None when it already had this one."""
    found = int(state.get("schema_version", 0))
    if found > SCHEMA_VERSION:
        raise NewerState(f"the run state has schema version {found}; this runner knows up to "
                         f"{SCHEMA_VERSION}. Use the runner that recorded it")
    if found == SCHEMA_VERSION:
        return None
    for version, step in MIGRATIONS:
        if found < version:
            step(state, run)
    state["schema_version"] = SCHEMA_VERSION
    return found, SCHEMA_VERSION


def _to_1(state, run):
    from . import budgets
    if "unpriced_call_reserve" not in state:
        state["unpriced_call_reserve"] = budgets.SERIAL_RESERVE
    in_flight = {}
    for it in state.get("intents", []):
        if it.get("kind") == "agent":
            it.setdefault("token_reservation", 0)
            in_flight[it.get("task")] = it.get("invocation_dir")
    for tid, st in state.get("tasks", {}).items():
        if st.get("apply_patch"):
            if not st.get("recover"):
                st["recover"] = _queued_recovery(run, tid)
            st.pop("apply_patch")
        for key in transaction.out_of_place(st):
            st.pop(key)
        if st.get("step") == "attempt" and "pending_calls" not in st:
            _charged_on_return(st, in_flight.get(tid))


def _charged_on_return(st, in_flight):
    """An attempt in progress, written when a try was charged before each call: the count was the
    calls started, the settled answer's try is already paid, and a call still in flight was
    charged for nothing it returned."""
    tries = st.get("pending_protocol_tries", 0)
    st["pending_calls"] = tries
    if st.get("pending_author_result"):
        st["pending_author_result"]["charged"] = True
    elif in_flight and tries:
        st["pending_protocol_tries"] = tries - 1
        st["pending_call"] = in_flight


def _queued_recovery(run, tid):
    """What an older runner's bare `apply_patch: true` asked for, as `retry --apply-patch` queues
    it now; the fields the request lacked are taken from the task's set-aside record."""
    rec = {}
    if run is not None:
        from . import engine, gitops
        try:
            rec = engine.set_aside_record(run, gitops.Git(run.info["git_toplevel"]), tid) or {}
        except (OSError, ValueError, KeyError, gitops.GitError):
            rec = {}
    return {"from": "set-aside", "attempt": rec.get("attempt"), "candidate": rec.get("candidate")}


def _to_2(state, run):
    """Nothing to change: version 2 only adds keys a version-1 state does not hold."""


def _to_3(state, run):
    """Nothing to change: version 3 only adds keys a version-2 state does not hold."""


def _to_4(state, run):
    """Nothing to change: version 4 only adds a key a version-3 state does not hold."""


MIGRATIONS = [(1, _to_1), (2, _to_2), (3, _to_3), (4, _to_4)]
