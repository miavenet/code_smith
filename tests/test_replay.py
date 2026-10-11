"""The acceptance replay (W-03): a producer's own gates run once more on a clean checkout of
exactly the candidate, so a file git ignores cannot make a gate pass that a fresh clone fails."""
import json
import os
import subprocess
import sys
import time
import types
import unittest

from helpers import RUNNER, EngineCase, done
from codesmith import engine, workflow

TASK = '''
[[task]]
id = "make"
type = "implement"
prompt = "Make src/a.txt say good."
outputs = ["src/a.txt"]
writes = ["src/a.txt", "src/table.txt"]
{extra}gate = [{gate}]
'''
GATE = '"grep -q good src/a.txt && (test -f gen/table.txt || test -f src/table.txt)"'


class Replay(EngineCase):
    def setUp(self):
        super().setUp()
        self.write(".gitignore", "gen/\nbuild/\nscratch/\n")
        self.commit()

    def flow(self, gate=GATE, extra="", defaults=""):
        self.workflow(TASK.format(gate=gate, extra=extra), defaults=defaults)

    def verification(self, n=1):
        return self.read_json("make", f"attempt-{n}", "verification.json")

    def test_an_ignored_file_a_gate_needs_sends_the_work_back(self):
        """acc: a gate that passes only through an ignored file fails the acceptance replay (ACC-33)"""
        self.flow()
        self.script([{"write": {"src/a.txt": "good\n", "gen/table.txt": "t\n"}, "answer": done()},
                     {"write": {"src/table.txt": "t\n"}, "answer": done()}])
        self.assertEqual(self.start(), 0, self.output)
        first = self.verification(1)
        self.assertEqual([r["result"] for r in first["results"]], ["pass"])   # the live gate
        self.assertEqual(first["result"], "fail")
        self.assertEqual(first["replay"]["result"], "fail")
        self.assertEqual(first["replay"]["tree"], first["candidate"])
        self.assertEqual(first["replay"]["ignored_in_work_tree"], ["gen/"])
        self.assertFalse(os.path.exists(first["replay"]["dir"]))
        self.assertEqual(self.read_json("make", "attempt-1", "outputs.json")["ignored_since_base"],
                         {"paths": ["gen/"], "count": 1})
        feedback = self.prompt(2)
        self.assertIn("passed in the work tree but did not pass (fail) on a clean checkout", feedback)
        self.assertIn("- gen/", feedback)
        second = self.verification(2)
        self.assertEqual((second["result"], second["replay"]["result"]), ("pass", "pass"))
        self.assertEqual(self.git_out("show", "HEAD:src/table.txt"), "t")
        self.check_invariants()

    def test_an_honest_task_is_accepted_with_one_replay(self):
        """acc: an honest task is accepted after one replay of its gates (ACC-34)"""
        self.flow(gate='"grep -q good src/a.txt", "test -f src/a.txt"')
        self.script([{"write": {"src/a.txt": "good\n"}, "answer": done()}])
        self.assertEqual(self.start(), 0, self.output)
        replay = self.verification()["replay"]
        self.assertEqual([(r["verifier"], r["result"]) for r in replay["results"]],
                         [("gate:1", "pass"), ("gate:2", "pass")])
        self.assertEqual((replay["result"], replay["cache"]), ("pass", []))
        self.assertFalse(os.path.exists(replay["dir"]))
        with open(os.path.join(self.the_run().path, "events.jsonl"), encoding="utf-8") as fh:
            kinds = [e.get("kind") for e in map(json.loads, fh) if e.get("event") == "outcome"]
        self.assertEqual(kinds.count("replay"), 1)
        self.assertNotIn("ignored_since_base", self.read_json("make", "attempt-1", "outputs.json"))
        self.check_invariants()

    def test_the_switch(self):
        """acc: acceptance_replay turns the replay off or on, per workflow and per task (ACC-35)"""
        cases = [("acceptance_replay = false", "", False),
                 ("acceptance_replay = false", "acceptance_replay = true\n", True),
                 ("", "acceptance_replay = false\n", False)]
        for defaults, extra, replayed in cases:
            with self.subTest(defaults=defaults, extra=extra):
                self.setUp()
                self.flow(extra=extra, defaults=defaults)
                self.script([{"write": {"src/a.txt": "good\n", "gen/table.txt": "t\n"},
                              "answer": done()}, {"write": {"src/table.txt": "t\n"}, "answer": done()}])
                self.assertEqual(self.start(), 0, self.output)
                self.assertEqual("replay" in self.verification(1), replayed)
                self.assertEqual(self.calls(), 2 if replayed else 1)
                self.check_invariants()
                self.doCleanups()

    def test_a_run_frozen_before_the_replay_replays_nothing(self):
        """acc: a frozen workflow without the keys replays nothing; a new one replays by default (ACC-35)"""
        old = types.SimpleNamespace(defaults={"gate_timeout_min": 20})
        task = {"id": "make", "gates": [{"run": "true"}]}
        self.assertEqual(engine.Engine.replay_settings(old, task), (False, []))
        self.assertEqual(engine.Engine.replay_settings(old, dict(task, acceptance_replay=True)),
                         (True, []))
        self.flow()
        wf = workflow.load(self.wf_path)
        self.assertEqual((wf.defaults["acceptance_replay"], wf.defaults["gate_cache"]), (True, []))
        self.assertNotIn("acceptance_replay", wf.tasks[0])       # a replan still compares equal
        bad = self.load(TASK.format(gate='"true"', extra='gate_cache = ["../x", "b*/"]\n'))
        self.assertError(bad, "'gate_cache' entry '../x'")
        self.assertError(bad, "'gate_cache' entry 'b*/'")

    def test_the_gate_cache_is_copied_into_the_checkout(self):
        """acc: gate_cache paths are copied into the replay's checkout first (ACC-36)"""
        os.makedirs(os.path.join(self.root, "build"))
        self.write("build/out.txt", "built\n")                  # ignored, there before the run
        self.flow(gate='"grep -q built build/out.txt && grep -q good src/a.txt"',
                  defaults='gate_cache = ["build/"]')
        self.script([{"write": {"src/a.txt": "good\n"}, "answer": done()}])
        self.assertEqual(self.start(), 0, self.output)
        replay = self.verification()["replay"]
        self.assertEqual((replay["result"], replay["cache"]), ("pass", ["build/"]))
        self.check_invariants()
        # without the cache the same gate fails on the clean checkout, naming the build directory
        self.setUp()
        os.makedirs(os.path.join(self.root, "build"))
        self.write("build/out.txt", "built\n")
        self.flow(gate='"grep -q built build/out.txt && grep -q good src/a.txt"',
                  extra="max_attempts = 1\n")
        self.script([{"write": {"src/a.txt": "good\n"}, "answer": done()}])
        self.assertEqual(self.start(), 2, self.output)
        self.assertEqual(self.verification()["replay"]["ignored_in_work_tree"], ["build/"])

    def test_an_expected_failure_keeps_its_meaning(self):
        """acc: an expect = "fail" gate must fail as required on the clean checkout too (ACC-37)"""
        honest = '{ run = "sh tests/repro.sh", expect = "fail", fail_pattern = "SYMPTOM" }'
        script = "echo SYMPTOM-42; exit 1\n"
        self.workflow(TASK.format(gate=honest, extra="").replace(
            'writes = ["src/a.txt", "src/table.txt"]', 'writes = ["src/a.txt", "tests/repro.sh"]'))
        self.script([{"write": {"src/a.txt": "x\n", "tests/repro.sh": script}, "answer": done()}])
        self.assertEqual(self.start(), 0, self.output)
        [entry] = self.verification()["replay"]["results"]
        self.assertEqual((entry["expect"], entry["result"]), ("fail", "pass"))
        self.check_invariants()
        self.setUp()
        hidden = '{ run = "sh scratch/repro.sh", expect = "fail", fail_pattern = "SYMPTOM" }'
        self.flow(gate=hidden, extra="max_attempts = 1\n")
        self.script([{"write": {"src/a.txt": "x\n", "scratch/repro.sh": script}, "answer": done()}])
        self.assertEqual(self.start(), 2, self.output)
        verification = self.verification()
        self.assertEqual(verification["results"][0]["result"], "pass")        # live: reproduces
        [entry] = verification["replay"]["results"]
        self.assertEqual((entry["result"], entry["note"]), ("fail", "could not run (exit 127)"))
        self.assertEqual(verification["replay"]["ignored_in_work_tree"], ["scratch/"])

    def test_a_kill_during_the_replay_resumes_cleanly(self):
        """rec: a kill during the acceptance replay leaves no checkout behind after resume (REC-24)"""
        self.flow()
        self.script([{"write": {"src/a.txt": "good\n", "src/table.txt": "t\n"}, "answer": done()}])
        env = dict(os.environ, CODE_SMITH_CRASH_AT="replay:running")
        res = subprocess.run([sys.executable, RUNNER, "start", self.wf_path], env=env,
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(res.returncode, 70, res.stderr)
        intents = self.the_run().state["intents"]
        [replay] = [i for i in intents if i["kind"] == "replay"]
        self.assertTrue([i for i in intents if i["kind"] == "command" and i.get("replay")])
        self.assertTrue(os.path.isdir(replay["dir"]))
        self.assertEqual(self.resume("--stop-orphans"), 0, self.output)
        self.assertFalse(os.path.exists(replay["dir"]))
        self.assertIn("acceptance replay of 'make': its checkout is removed", self.output)
        self.assertEqual(self.calls(), 1)                          # the author is not called again
        self.assertEqual(self.verification()["replay"]["result"], "pass")
        self.assertEqual(self.git_out("rev-list", "--count", "main..HEAD"), "1")
        self.check_invariants()


class ReplayReview(EngineCase):
    HEADER = EngineCase.HEADER + 'read_only_args = ["--read-only"]\n'

    def test_reviewers_see_the_ignored_files(self):
        """acc: the review target names the ignored paths that appeared since the base (ACC-38)"""
        from test_findings import review
        self.write(".gitignore", "gen/\n")
        self.commit()
        self.workflow(TASK.format(gate='"grep -q good src/a.txt"', extra="")
                      + 'reviewers = ["principled-priya"]\n')
        rid = [t["id"] for t in workflow.load(self.wf_path).tasks if t["kind"] == "review"][0]
        self.script({"make": [{"write": {"src/a.txt": "good\n", "gen/x.bin": "x"}, "answer": done()}],
                     rid: [{"answer": review()}]})
        self.assertEqual(self.start(), 0, self.output)
        with open(self.task_file(rid, "round-1", "prompt.md"), encoding="utf-8") as fh:
            prompt = fh.read()
        self.assertIn('"ignored_since_base": {\n    "paths": [\n      "gen/"\n    ],\n    "count": 1',
                      prompt)
        self.check_invariants()


# Only the replay's checkout lies under a `code-smith-replay-` directory: the live gate is quick.
IN_REPLAY = 'case "$PWD" in */code-smith-replay-*) {wait} ;; esac; '


class ReplayBesidePanel(EngineCase):
    """N3: the acceptance replay runs as a job beside the panel's readers."""
    HEADER = EngineCase.HEADER + 'read_only_args = ["--read-only"]\n'

    def setUp(self):
        super().setUp()
        self.write(".gitignore", "gen/\n")
        self.commit()

    def flow(self, gate, defaults="", extra=""):
        self.workflow(TASK.format(gate=json.dumps(gate), extra=extra)
                      + 'reviewers = ["principled-priya"]\n', defaults=defaults)
        self.rid = [t["id"] for t in workflow.load(self.wf_path).tasks if t["kind"] == "review"][0]

    def events(self):
        with open(os.path.join(self.the_run().path, "events.jsonl"), encoding="utf-8") as fh:
            return [json.loads(line) for line in fh]

    def reviewer_calls(self):
        with open(self.script_path + "." + self.rid + ".counter", encoding="utf-8") as fh:
            return int(fh.read())

    def author_prompt(self, n):
        with open(os.path.join(self.script_path + ".make.prompts", f"{n}.md"), encoding="utf-8") as fh:
            return fh.read()

    @staticmethod
    def at(event):
        import datetime
        return datetime.datetime.strptime(event["at"], "%Y-%m-%dT%H:%M:%S.%fZ").timestamp()

    def test_the_replay_and_the_reviewer_overlap(self):
        """acc: the acceptance replay runs beside the reviewers (ACC-49)"""
        from test_findings import review
        self.flow(IN_REPLAY.format(wait="sleep 1") + "grep -q good src/a.txt")
        self.script({"make": [{"write": {"src/a.txt": "good\n"}, "answer": done()}],
                     self.rid: [{"sleep_s": 1, "answer": review()}]})
        self.assertEqual(self.start(), 0, self.output)
        events = self.events()
        intents = {e["op"]: e for e in events if e["event"] == "intent"}
        outcomes = {e["op"]: e for e in events if e["event"] == "outcome"}
        [replay] = [op for op, e in intents.items() if e["kind"] == "replay"]
        review_op = [op for op, e in intents.items() if e["kind"] == "agent"][-1]
        starts = [self.at(intents[op]) for op in (replay, review_op)]
        ends = [self.at(outcomes[op]) for op in (replay, review_op)]
        # Each began before the other ended (on an idle machine the intents fall in one second;
        # each intent is a durable save, slower on a loaded one).
        self.assertLess(starts[1], ends[0])
        self.assertLess(starts[0], ends[1])
        # Each takes a second or more. Run one after the other, the pair would take their sum;
        # side by side it takes about the longer one (1.8 s on an idle machine; the margin to the
        # sum is what a loaded machine cannot take away).
        spans = [end - start for start, end in zip(starts, ends)]
        self.assertGreaterEqual(min(spans), 1.0)
        self.assertLess(max(ends) - min(starts), sum(spans) - 0.5)
        verification = self.read_json("make", "attempt-1", "verification.json")
        self.assertEqual((verification["result"], verification["replay"]["result"]), ("pass", "pass"))
        self.assertTrue(verification["replay"]["beside_panel"])
        self.assertFalse(os.path.exists(verification["replay"]["dir"]))
        self.assertEqual(self.status("make"), "accepted")
        self.assertGreaterEqual(self.the_run().state["replay_seconds"], 1.0)
        self.check_invariants()

    def test_a_failing_replay_voids_the_panel(self):
        """acc: a failing replay beside the panel voids its verdicts and sends the attempt back (ACC-50)"""
        from test_findings import review
        finding = dict(id="F1", severity="blocking", title="The table is wrong",
                       evidence="src/a.txt:1", remedy="Fix it.", location="src/a.txt:1")
        self.flow("grep -q good src/a.txt && (test -f gen/table.txt || test -f src/table.txt)")
        self.script({"make": [{"write": {"src/a.txt": "good\n", "gen/table.txt": "t\n"},
                               "answer": done()},
                              {"write": {"src/table.txt": "t\n"}, "answer": done()}],
                     self.rid: [{"answer": review([finding])}, {"answer": review()}]})
        self.assertEqual(self.start(), 0, self.output)
        first = self.read_json("make", "attempt-1", "verification.json")
        self.assertEqual((first["result"], first["replay"]["result"]), ("fail", "fail"))
        self.assertEqual(first["replay"]["ignored_in_work_tree"], ["gen/"])
        self.assertTrue(self.read_json(self.rid, "round-1", "verdict.json")["void"])
        feedback = self.author_prompt(2)
        self.assertIn("passed in the work tree but did not pass (fail) on a clean checkout", feedback)
        self.assertIn("its verdicts on this candidate were set aside", feedback)
        self.assertNotIn("The table is wrong", feedback)          # the void verdict is not told
        # One call per candidate: the reviewer judged the second candidate afresh and passed it;
        # the void blocker never reached the ledger.
        self.assertEqual(self.reviewer_calls(), 2)
        self.assertEqual(self.the_run().state["tasks"]["make"]["ledger"]["findings"], [])
        second = self.read_json("make", "attempt-2", "verification.json")
        self.assertEqual((second["result"], second["replay"]["result"]), ("pass", "pass"))
        self.assertEqual(self.status("make"), "accepted")
        self.check_invariants()

    def test_a_kill_during_the_concurrent_replay(self):
        """acc: a kill during the replay beside the panel keeps the saved answer and replays once more (ACC-51)"""
        from test_findings import review
        saved = (f"until [ -n \"$(find {self.root}/.runs -path '*round-1/invocation-1/outcome.json')\" ];"
                 " do sleep 0.05; done; sleep 2")
        self.flow(IN_REPLAY.format(wait=saved) + "grep -q good src/a.txt")
        self.script({"make": [{"write": {"src/a.txt": "good\n"}, "answer": done()}],
                     self.rid: [{"answer": review()}]})
        env = dict(os.environ, CODE_SMITH_CRASH_AT="panel:answer-saved")
        res = subprocess.run([sys.executable, RUNNER, "start", self.wf_path], env=env,
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(res.returncode, 70, res.stderr)
        state = self.the_run().state
        [replay] = [i for i in state["intents"] if i["kind"] == "replay"]
        self.assertTrue([i for i in state["intents"] if i["kind"] == "command" and i.get("replay")])
        [job] = [j for j in state["tasks"]["make"]["panel"]["jobs"] if j["kind"] == "review"]
        self.assertEqual(job["raw_outcome"]["status"], "ok")     # the answer was saved
        self.assertEqual(self.resume("--stop-orphans"), 0, self.output)
        self.assertFalse(os.path.exists(replay["dir"]))
        self.assertEqual(self.reviewer_calls(), 1)                 # kept, not asked again
        replays = [e["result"] for e in self.events()
                   if e["event"] == "outcome" and e["kind"] == "replay"]
        self.assertEqual(replays, ["interrupted", "pass"])        # run again, once
        verification = self.read_json("make", "attempt-1", "verification.json")
        self.assertEqual(verification["replay"]["result"], "pass")
        self.assertEqual(self.status("make"), "accepted")
        self.assertEqual(self.git_out("rev-list", "--count", "main..HEAD"), "1")
        self.check_invariants()

    def test_a_crash_while_the_replay_gate_runs_leaves_it_for_resume(self):
        """proc: a crash at replay:running beside the panel comes after the gate is released, so
        resume finds it alive and refuses naming its leader; --stop-orphans stops the group and
        the replay runs once more (PROC-20)"""
        from codesmith import gitops, record
        from test_findings import review
        once = os.path.join(self.side, "replayed-once")
        self.flow(IN_REPLAY.format(wait=f"test -e {once} || {{ touch {once}; sleep 60; }}")
                  + "grep -q good src/a.txt")
        self.script({"make": [{"write": {"src/a.txt": "good\n"}, "answer": done()}],
                     self.rid: [{"answer": review()}]})
        env = dict(os.environ, CODE_SMITH_CRASH_AT="replay:running")
        res = subprocess.run([sys.executable, RUNNER, "start", self.wf_path], env=env,
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(res.returncode, 70, res.stderr)
        [gate] = [i for i in self.the_run().state["intents"] if i["kind"] == "command" and i.get("replay")]
        self.addCleanup(record.stop_process_group, gate["process"], .1)
        leader = gate["process"]["group"]["leader"]
        self.assertTrue(record.is_alive(leader))
        # The reviewer beside it ends on its own: wait for that, so the one orphan is the gate.
        deadline = time.monotonic() + 60
        for it in self.the_run().state["intents"]:
            while it["op"] != gate["op"] and record.is_alive(it.get("process")) and time.monotonic() < deadline:
                time.sleep(.05)
        with self.assertRaises(record.OrphanAlive) as ctx:
            record.reconcile(self.the_run(), gitops.Git(self.root))
        self.assertIn(str(leader["pid"]), str(ctx.exception))
        self.assertEqual(self.resume("--stop-orphans"), 0, self.output)
        self.assertFalse(record.is_alive(leader))
        replays = [e["result"] for e in self.events()
                   if e["event"] == "outcome" and e["kind"] == "replay"]
        self.assertEqual(replays, ["interrupted", "pass"])
        self.assertEqual(self.read_json("make", "attempt-1", "verification.json")["replay"]["result"], "pass")
        self.assertEqual(self.status("make"), "accepted")
        self.check_invariants()

    def test_a_reused_replay_is_recorded_with_its_source(self):
        """acc: when a ruled retry --apply-patch reverifies a candidate whose replay already passed
        on the same starting commit, the panel does not replay it and the new attempt's
        verification.json says pass and names the attempt holding the evidence (ACC-53)"""
        from test_findings import finding, review
        self.flow("grep -q good src/a.txt", extra="max_attempts = 1\n")
        self.script({"make": [{"write": {"src/a.txt": "good\n"}, "answer": done()}],
                     self.rid: [{"answer": review([finding("src/a.txt:1")])}, {"answer": review()}]})
        self.assertEqual(self.start(), 255, self.output)
        self.assertEqual(self.status("make"), "blocked")
        first = self.the_run().state["tasks"]["make"]["attempt_dir"]
        self.assertEqual(self.runner("resolve", "latest", "make/PE-1", "--as", "advisory",
                                     "--note", "Owner decision", "-C", self.root), 0, self.output)
        self.assertEqual(self.runner("retry", "latest", "make", "--apply-patch", "-C", self.root),
                         0, self.output)
        self.assertEqual(self.resume(), 0, self.output)
        st = self.the_run().state["tasks"]["make"]
        self.assertEqual(st["status"], "accepted")
        self.assertNotEqual(st["attempt_dir"], first)
        replays = [e for e in self.events() if e["event"] == "outcome" and e["kind"] == "replay"]
        self.assertEqual(len(replays), 1)                          # replayed once, on attempt 1
        path = os.path.join(self.the_run().path, st["attempt_dir"], "verification.json")
        with open(path, encoding="utf-8") as fh:
            replay = json.load(fh)["replay"]
        self.assertEqual(replay, {"beside_panel": True, "result": "pass", "reused_from": first})
        with open(os.path.join(self.the_run().path, first, "verification.json"), encoding="utf-8") as fh:
            evidence = json.load(fh)["replay"]
        self.assertEqual((evidence["result"], evidence["tree"]), ("pass", st["candidate"]))
        self.check_invariants()

    def test_the_serial_order_is_kept_on_request(self):
        """acc: replay_beside_panel = false replays before the first review intent (ACC-52)"""
        from test_findings import review
        self.flow("grep -q good src/a.txt", defaults="replay_beside_panel = false")
        self.script({"make": [{"write": {"src/a.txt": "good\n"}, "answer": done()}],
                     self.rid: [{"answer": review()}]})
        self.assertEqual(self.start(), 0, self.output)
        events = self.events()
        replay_done = next(i for i, e in enumerate(events)
                           if e["event"] == "outcome" and e["kind"] == "replay")
        review_intent = [i for i, e in enumerate(events)
                         if e["event"] == "intent" and e["kind"] == "agent"][-1]
        self.assertLess(replay_done, review_intent)
        replay = self.read_json("make", "attempt-1", "verification.json")["replay"]
        self.assertEqual(replay["result"], "pass")
        self.assertNotIn("beside_panel", replay)
        self.check_invariants()


if __name__ == "__main__":
    unittest.main()
