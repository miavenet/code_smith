"""The producer's state machine made explicit (W-04): the transition table and its clearing rules
(RUN-51), the invariants that use it (RUN-52), the schema version and the migration of older
states (RUN-53, RUN-56), and interrupted calls counted apart from protocol tries (RUN-54, RUN-55)."""

import json
import os
import unittest
from unittest import mock

from helpers import EngineCase, HERE, done

from codesmith import budgets, invariants, record, schema, transaction
from codesmith.transaction import RunnerError

ONE = '''
[[task]]
id = "make"
type = "implement"
prompt = "Make src/a.txt say good."
outputs = ["src/a.txt"]
max_attempts = 1
gate = ["grep -q good src/a.txt"]
'''
GOOD = {"write": {"src/a.txt": "good\n"}, "answer": done()}


class Killed(BaseException):
    pass


def kill_in_call(point):
    if point == "agent:running":
        raise Killed()


class TransitionTable(unittest.TestCase):
    def test_the_table_refuses_and_clears_by_rule(self):
        """run: the transition table refuses a move it does not allow and clears step-scoped keys
        by rule (RUN-51)"""
        st = {"kind": "produce", "status": "verifying", "step": "verify", "candidate": "c",
              "pending_attempt": [1, "a"], "pending_protocol_tries": 1, "provider_switched": True}
        before = dict(st)
        with self.assertRaises(RunnerError) as ctx:
            transaction.move(st, "make", "commit")
        self.assertEqual(str(ctx.exception),
                         "'make' cannot move from step 'verify' to 'commit' (status 'verifying')")
        self.assertEqual(st, before)                          # nothing written
        with self.assertRaises(RunnerError) as ctx:
            transaction.move(st, "make", "panel", status="pending")
        self.assertIn("from status 'verifying' to 'pending'", str(ctx.exception))
        self.assertEqual(st, before)
        transaction.move(st, "make", "panel")
        self.assertEqual(st, {"kind": "produce", "status": "verifying", "step": "panel",
                              "candidate": "c"})
        # Moving out of `attempt` consumes a ruled candidate; the set-aside tree ends with its step.
        st = {"status": "running", "step": "attempt", "ruled_candidate": "t"}
        transaction.move(st, "make", "inspect")
        self.assertNotIn("ruled_candidate", st)
        st = {"status": "failed", "step": "set-aside", "pending_set_aside": "t"}
        transaction.move(st, "make", None)
        self.assertNotIn("pending_set_aside", st)
        # The transaction opens: a queued recovery and the last transaction's notes go.
        st = {"status": "pending", "recover": {"attempt": 1}, "ruled_candidate": "t",
              "git_repairs": [1], "ignored_since_base": {}, "pending_protocol_error": "x"}
        transaction.open_(st, "make", base="b")
        self.assertEqual(st, {"status": "running", "step": "attempt", "base": "b"})
        with self.assertRaises(RunnerError):
            transaction.open_({"status": "verifying", "step": "verify"}, "make")
        # `retry`: back to pending, with the last end's labels and attempt keys cleared.
        st = {"status": "blocked", "step": None, "panel": {}, "block_kind": "protocol",
              "block_reviewers": [], "recover": {}, "pending_attempt": [1, "a"], "base": "b"}
        transaction.reset(st, "make", reason="")
        self.assertEqual(st, {"status": "pending", "step": None, "reason": "", "base": "b"})
        with self.assertRaises(RunnerError):
            transaction.reset({"status": "running", "step": "attempt"}, "make")
        with self.assertRaises(RunnerError):
            transaction.set_status({"status": "failed"}, "c", "running")

    def test_the_architecture_document_shows_the_table(self):
        """run: the transition table in 05-architecture is the one the code holds (RUN-51)"""
        path = os.path.join(HERE, "..", "docs", "05-architecture.md")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        section = text[text.index("### The producer's state machine"):]

        def table(header):
            rows = section.split(header, 1)[1].strip("\n").split("\n\n")[0].splitlines()[1:]
            name = lambda cell: None if cell == "(none)" else cell.strip("`")
            cells = [[c.strip() for c in row.strip("|").split("|")] for row in rows]
            return {name(a): {name(t.strip()) for t in b.split(",")} for a, b in cells}
        self.assertEqual(table("| From step | To steps |"),
                         {k: set(v) for k, v in transaction.STEP_MOVES.items()})
        self.assertEqual(table("| From status | To statuses |"),
                         {k: set(v) for k, v in transaction.STATUS_MOVES.items()})

    def test_the_invariants_use_the_table(self):
        """run: a step-scoped key outside its step, an unknown step and too many interruptions
        are invariant violations (RUN-52)"""
        def state(**design):
            st = {"kind": "produce", "status": "running", "step": "attempt", "base": "b"}
            st.update(design)
            return {"active_producer": "design", "intents": [], "tasks": {"design": st}}
        self.assertEqual(invariants.check(state(pending_protocol_error="x", pending_calls=2)), [])
        self.assertEqual(invariants.check(state(step="set-aside", pending_set_aside="t")), [])
        cases = [
            (state(step="verify", status="verifying", pending_protocol_error="x"),
             "'design' holds 'pending_protocol_error' at step 'verify'"),
            (state(pending_set_aside="t"), "'design' holds 'pending_set_aside' at step 'attempt'"),
            (state(step="panel", status="verifying", provider_switched=True, pending_call="i"),
             "'design' holds 'pending_call' at step 'panel'"),
            (state(step="dance"), "'design' has the unknown step 'dance'"),
            (state(pending_interruptions=transaction.MAX_INTERRUPTIONS + 1),
             f"'design' counted {transaction.MAX_INTERRUPTIONS + 1} interrupted calls"),
        ]
        for st, rule in cases:
            with self.subTest(rule=rule):
                self.assertIn(rule, invariants.check(st))


class Interruptions(EngineCase):
    def events(self, name):
        with open(os.path.join(self.the_run().path, "events.jsonl"), encoding="utf-8") as fh:
            return [e for e in map(json.loads, fh) if e["event"] == name]

    def kill_calls(self, times):
        for n in range(times):
            self.cli.CRASH = kill_in_call
            with self.assertRaises(Killed):
                self.start() if n == 0 else self.resume()
            self.cli.CRASH = None

    def test_interrupted_calls_spend_no_try_and_no_attempt(self):
        """run: three calls interrupted inside one attempt spend no protocol try and no attempt;
        the attempt continues on resume (RUN-54)"""
        self.workflow(ONE)
        self.script([GOOD] * 4)
        self.kill_calls(3)
        st = self.the_run().state["tasks"]["make"]
        self.assertEqual(st["attempts_used"], 0)
        self.assertNotIn("pending_protocol_tries", st)
        self.assertEqual(self.resume(), 0, self.output)
        st = self.the_run().state["tasks"]["make"]
        self.assertEqual((st["status"], st["attempts_used"]), ("accepted", 1))
        self.assertTrue(os.path.isdir(self.task_file("make", "attempt-1", "invocation-4")))
        self.assertFalse(os.path.exists(self.task_file("make", "attempt-2")))
        self.assertEqual(self.read_json("make", "attempt-1", "result.json")["agent_calls"], 4)
        self.assertEqual([e["count"] for e in self.events("call-interrupted")], [1, 2, 3])
        self.check_invariants()

    def test_too_many_interruptions_stop_the_attempt_for_a_person(self):
        """run: an attempt whose calls are interrupted five times stops blocked, naming the count,
        with no attempt used (RUN-55)"""
        self.workflow(ONE)
        self.script([GOOD] * 6)
        self.kill_calls(transaction.MAX_INTERRUPTIONS)
        self.assertEqual(self.resume(), 255, self.output)
        st = self.the_run().state["tasks"]["make"]
        self.assertEqual((st["status"], st["attempts_used"]), ("blocked", 0))
        self.assertIn("5 calls of attempt 1 were interrupted before they returned", st["reason"])
        self.assertTrue(os.path.isdir(self.task_file("make", "attempt-1", "invocation-5")))
        self.assertFalse(os.path.exists(self.task_file("make", "attempt-1", "invocation-6")))
        self.assertFalse([k for k in st if k.startswith("pending_")])
        self.check_invariants()

    def test_an_older_runners_call_in_flight_is_counted_as_an_interruption(self):
        """run: a state an older runner left during a call (its try charged before the call) is
        migrated so that resume counts the call as an interruption (RUN-56)"""
        self.workflow(ONE)
        self.script([GOOD] * 2)
        self.kill_calls(1)
        run = self.the_run()
        st = run.state["tasks"]["make"]
        del st["pending_call"], st["pending_calls"]
        st["pending_protocol_tries"] = 1                     # charged before the call, as then
        del run.state["schema_version"]
        run.save()
        self.assertEqual(self.resume(), 0, self.output)
        self.assertEqual(self.the_run().state["tasks"]["make"]["attempts_used"], 1)
        self.assertEqual(self.read_json("make", "attempt-1", "result.json")["agent_calls"], 2)
        self.assertEqual([e["count"] for e in self.events("call-interrupted")], [1])
        self.assertEqual([(e["from_version"], e["to_version"]) for e in self.events("state-migrated")],
                         [(0, schema.SCHEMA_VERSION)])
        self.check_invariants()


class Migration(EngineCase):
    def test_an_older_state_is_migrated_on_load(self):
        """run: a state without a schema version is brought to the current shape on load; the
        first save records the migration once, a command that only reads writes nothing, and a
        newer shape is refused (RUN-53)"""
        self.workflow(ONE)
        self.script([GOOD])
        self.assertEqual(self.start(), 0, self.output)
        run = self.the_run()
        self.assertEqual(run.state["schema_version"], schema.SCHEMA_VERSION)
        del run.state["schema_version"], run.state["unpriced_call_reserve"]
        run.state["tasks"]["make"]["pending_protocol_error"] = "left by an older end"
        run.state["intents"].append({"op": "op-9999-old", "kind": "agent", "task": "make"})
        state_path = os.path.join(run.path, "state.json")
        record.write_durable(state_path, record.dump_json(run.state))     # as an older runner did
        events_path = os.path.join(run.path, "events.jsonl")
        with open(state_path, "rb") as fh:
            old = fh.read()
        size = os.path.getsize(events_path)

        loaded = record.Run.load(run.path)
        self.assertEqual(loaded.state["schema_version"], schema.SCHEMA_VERSION)
        self.assertEqual(loaded.state["unpriced_call_reserve"], budgets.SERIAL_RESERVE)
        self.assertFalse(budgets.bounded(loaded.state))       # serial, the whole remainder
        self.assertNotIn("pending_protocol_error", loaded.state["tasks"]["make"])
        self.assertEqual(loaded.state["intents"][-1]["token_reservation"], 0)
        with open(state_path, "rb") as fh:
            self.assertEqual(fh.read(), old)                  # loading alone writes nothing
        self.assertEqual(os.path.getsize(events_path), size)

        loaded.save()
        loaded.save()
        with open(events_path, encoding="utf-8") as fh:
            migrated = [e for e in map(json.loads, fh) if e["event"] == "state-migrated"]
        self.assertEqual([(e["from_version"], e["to_version"]) for e in migrated],
                         [(0, schema.SCHEMA_VERSION)])
        self.assertEqual(record.read_json(state_path)["schema_version"], schema.SCHEMA_VERSION)
        self.assertIsNone(schema.migrate(record.read_json(state_path)))     # current: unchanged

        newer = dict(record.read_json(state_path), schema_version=schema.SCHEMA_VERSION + 1)
        record.write_durable(state_path, record.dump_json(newer))
        with self.assertRaises(record.RecordError) as ctx:
            record.Run.load(run.path)
        self.assertIn(f"schema version {schema.SCHEMA_VERSION + 1}", str(ctx.exception))


    def test_a_version_1_runner_refuses_a_state_with_the_new_keys(self):
        """rec: a state holding every key and job kind added since version 1 is written as
        the current version and loads here unchanged; a version-1 runner refuses it, a version-2
        runner refuses one holding a key added since the first version-2 runner, a version-3
        runner one holding a queued standing entry's `file`, and a version-1, 2 or 3 state
        migrates with nothing changed but its version (REC-53)"""
        self.workflow(ONE)
        self.script([GOOD])
        self.assertEqual(self.start(), 0, self.output)
        run = self.the_run()
        state_path = os.path.join(run.path, "state.json")
        self.assertEqual(schema.SCHEMA_VERSION, 4)
        for version in (1, 2, 3):
            old = dict(record.read_json(state_path), schema_version=version)
            migrated = json.loads(json.dumps(old))
            self.assertEqual(schema.migrate(migrated), (version, schema.SCHEMA_VERSION))
            self.assertEqual(migrated, dict(old, schema_version=schema.SCHEMA_VERSION))

        state = record.read_json(state_path)
        self.assertEqual(state["schema_version"], 4)
        for path, value in SINCE_2 + SINCE_3 + SINCE_4:
            put(state, path, json.loads(json.dumps(value)))
        record.write_durable(state_path, record.dump_json(state))
        loaded = record.Run.load(run.path)
        self.assertIsNone(loaded._migrated)
        for path, value in SINCE_2 + SINCE_3 + SINCE_4:
            self.assertEqual(get(loaded.state, path), get(state, path), path)
        for path, value in SINCE_4:
            self.assertEqual(get(loaded.state, path), value, path)
        for older in (1, 2, 3):
            with self.subTest(runner_version=older), mock.patch.object(schema, "SCHEMA_VERSION", older):
                with self.assertRaises(schema.NewerState):
                    schema.migrate(json.loads(json.dumps(state)))
                with self.assertRaises(record.RecordError) as ctx:
                    record.Run.load(run.path)
                self.assertIn(f"schema version 4; this runner knows up to {older}", str(ctx.exception))


# Every key and job kind added to the state since version 1 and known to the first version-2
# runner, at a place it lives, with a value it takes (REC-53).
SINCE_2 = (
    (("save_seq",), 7),
    (("pin_stale",), {"since": "2026-10-10T00:00:00Z", "error": "the ref moved"}),
    (("agent_spans",), {"summed_s": 1.0, "folded_s": 0.0, "open": []}),
    (("intents", 0, "token_remainder"), 5000),
    (("intents", 1, "kind"), "replay"),
    (("tasks", "make", "replay_passed"), {"candidate": "c", "head": "h"}),
    (("tasks", "make", "panel", "standing_rulings"), []),
    (("tasks", "make", "panel", "jobs", 0, "kind"), "replay"),
    (("tasks", "make", "panel", "jobs", 1, "in_flight"), True),
    (("tasks", "make", "panel", "jobs", 1, "interruptions"), 1),
    (("tasks", "make", "panel", "jobs", 1, "raw_outcome", "before_work"), True),
    (("tasks", "make", "ledger", "findings", 0, "history", 0, "seat"), "advisory"),
    (("tasks", "make", "panel", "jobs", 2, "result", "repair", "hints"), ["a hint"]),
)

# Every key added since the first version-2 runner, which version 3 marks; the threads that add
# more extend this tuple, and a key an older runner would misread bumps the version (REC-53).
SINCE_3 = (
    (("standing_pending",), [{"run": "r", "finding": "make/PE-1", "written": False}]),
    (("tasks", "make", "panel", "standing_dropped_why"), ["the brief changed (project rules)"]),
    (("cleanup_obligations",), [{"task": "make", "invocation": "attempt-1", "cleanup": {"status": "open"}},
                                {"task": "make", "invocation": "attempt-2", "cleanup": {"status": "abandoned"},
                                 "ruling": {"by": "owner", "by_source": "env", "interactive": True,
                                            "agent_markers": [], "at": "2026-10-10T00:00:00+00:00"}}]),
    (("tasks", "make", "panel", "jobs", 1, "guard_problems"), ["state.json was changed"]),
    (("tasks", "make", "panel", "jobs", 1, "raw_outcome", "cleanup"), {"status": "open", "error": "could not stop"}),
    (("tasks", "make", "pending_author_result", "result", "cleanup"), {"status": "open", "error": "could not stop"}),
    (("tasks", "make", "pending_provider_quota", "cleanup"), {"status": "open", "error": "could not stop"}),
    (("intents", 0, "cancelled"), {"status": "not-started", "before_work": True, "seconds": 0.0,
                                   "cancelled": True, "error": "cancelled before release"}),
)


# Every key added since the last version-3 runner, which version 4 marks (RUN-76): a version-3
# runner would write a queued entry's `file` into the rulings file, whose reader refuses it.
SINCE_4 = (
    (("standing_pending", 0, "file"), "rulings.toml"),
)


def put(state, path, value):
    """Set `value` at `path`, making the dicts and list entries on the way."""
    node = state
    for key, following in zip(path, path[1:]):
        if isinstance(key, int):
            while len(node) <= key:
                node.append({})
        elif not isinstance(node.get(key), (dict, list)):
            node[key] = [] if isinstance(following, int) else {}
        node = node[key]
    node[path[-1]] = value


def get(state, path):
    for key in path:
        state = state[key]
    return state


RULED = """
[[task]]
id = "make"
type = "implement"
prompt = "Make the work"
outputs = ["src/a"]
max_attempts = 1
gate = ["test -f src/a"]
reviewers = ["principled-priya"]

[[task]]
id = "look"
type = "human"
verifies = "make"
"""


class RuledSendBack(EngineCase):
    HEADER = EngineCase.HEADER + 'read_only_args = ["--read-only"]\n'

    def test_a_person_rejects_a_ruled_restored_candidate(self):
        """run: a person rejects a ruled, restored candidate and the producer goes to its first
        counted attempt (RUN-60)"""
        from test_findings import finding, review
        self.workflow(RULED)
        reviewer = "make.review.principled-priya"
        good = {"write": {"src/a": "good\n"}, "answer": done()}
        polite = {"write": {"src/a": "good, and polite\n"}, "answer": done()}
        self.script({"make": [good, polite],
                     reviewer: [{"answer": review([finding()])}, {"answer": review()}]})
        self.assertEqual(self.start(), 255, self.output)            # attempts used, set aside
        self.assertEqual(self.status("make"), "blocked")
        run = self.the_run().name
        self.assertEqual(self.runner("resolve", run, "make/PE-1", "--as", "advisory",
                                     "--note", "Owner decision", "-C", self.root), 0, self.output)
        self.assertEqual(self.runner("retry", run, "make", "--apply-patch", "-C", self.root),
                         0, self.output)
        self.assertEqual(self.resume(), 255, self.output)           # reverified, at the person
        st = self.the_run().state["tasks"]["make"]
        self.assertEqual((st["status"], st["attempts_used"]), ("waiting_human", 0))
        self.assertEqual(self.runner("reject", run, "look", "-m", "Say it politely.",
                                     "-C", self.root), 0, self.output)
        self.assertEqual(self.resume(), 255, self.output)           # one fresh author call
        self.assertEqual(self.calls_of("make"), 2)
        self.assertEqual(self.runner("approve", run, "look", "-C", self.root), 0, self.output)
        self.assertEqual(self.resume(), 0, self.output)
        st = self.the_run().state["tasks"]["make"]
        self.assertEqual((st["status"], st["attempts_used"]), ("accepted", 1))
        self.assertEqual(self.calls_of("make"), 2)
        with open(os.path.join(self.root, "src", "a"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "good, and polite\n")
        self.check_invariants()

    def calls_of(self, tid):
        with open(f"{self.script_path}.{tid}.counter", encoding="utf-8") as fh:
            return int(fh.read())


if __name__ == "__main__":
    unittest.main()
