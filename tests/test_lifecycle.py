"""One producer transaction, end to end, with the scripted agent: acceptance and rework (ACC),
freezing and protection (FRZ), failure and the DAG (FAIL), the record of set-aside work (RUN-18,
RUN-21) and resuming from disk (RUN-03, RUN-12).

Every test ends with `check_invariants`: every accepted commit is exactly the verified candidate,
and nothing else is in the tree when a transaction ends."""

import hashlib
import json
import os
import subprocess
import sys
import unittest

from helpers import FAKE_AGENT, RUNNER, EngineCase, done, git

from codesmith import agents, checks, engine, gitops, prompts, record, validate

ONE = '''
[[task]]
id = "make"
type = "implement"
prompt = "Make src/a.txt say good."
outputs = ["src/a.txt"]
gate = ["grep -q good src/a.txt"]
'''
GOOD = {"write": {"src/a.txt": "good\n"}, "answer": done()}
BAD = {"write": {"src/a.txt": "bad\n"}, "answer": done()}


def bad(n):
    return {"write": {"src/a.txt": f"bad {n}\n"}, "answer": done()}


class Acceptance(EngineCase):
    def test_agent_report_is_never_acceptance(self):
        """acc: agent report is never acceptance (ACC-01)"""
        self.workflow(ONE.replace("gate =", "max_attempts = 1\ngate ="))
        self.script([{"write": {"src/a.txt": "bad\n"}, "answer": done("All done and tested.")}])
        self.assertEqual(self.start(), 2)
        self.assertEqual(self.status("make"), "failed")
        self.assertEqual(self.git_out("log", "--oneline", "main..HEAD"), "")
        self.check_invariants()

    def test_output_contract(self):
        """acc: output contract (ACC-02)"""
        self.write("old/legacy.txt", "x\n")
        self.workflow('''
[[task]]
id = "make"
type = "implement"
prompt = "p"
outputs = ["src/a.txt", "src/b.txt"]
writes = ["src/**", "old/**"]
removes = ["old/legacy.txt"]
gate = ["true"]
''')
        self.script([{"write": {"src/a.txt": ""}, "answer": done()},
                     {"write": {"src/a.txt": "a\n", "src/b.txt": "b\n"}, "remove": ["old/legacy.txt"],
                      "answer": done()}])
        self.assertEqual(self.start(), 0, self.output)
        feedback = self.prompt(2)
        self.assertIn("the declared output src/a.txt is empty", feedback)
        self.assertIn("the declared output src/b.txt does not exist", feedback)
        self.assertIn("old/legacy.txt must not exist afterwards", feedback)
        self.assertFalse(os.path.exists(os.path.join(self.root, "old/legacy.txt")))
        self.check_invariants()

    def test_gates_before_reviews(self):
        """acc: gates before reviews (ACC-03; the reviewer half is in test_panels)"""
        self.workflow(ONE.replace("grep -q good src/a.txt",
                                  "grep -q good src/a.txt || { echo MARKER-7731; exit 1; }"))
        self.script([BAD, GOOD])
        self.assertEqual(self.start(), 0, self.output)
        self.assertIn("MARKER-7731", self.prompt(2))
        self.assertIn("did not pass", self.prompt(2))
        self.assertTrue(os.path.exists(self.task_file("make", "attempt-2", "feedback.md")))
        self.check_invariants()

    def test_no_progress_needs_an_unchanged_tree(self):
        """acc: no-progress needs an unchanged tree (ACC-04)"""
        self.workflow(ONE.replace("gate =", "max_attempts = 5\ngate ="))
        self.script([BAD, BAD, GOOD])
        self.assertEqual(self.start(), 2)
        self.assertEqual(self.calls(), 2)
        self.assertIn("no progress", self.the_run().state["tasks"]["make"]["reason"])
        self.check_invariants()

        # The same failure after a real change is not "no progress": the attempt limit applies.
        self.script([bad(1), bad(2), bad(3), bad(4), bad(5), GOOD])
        self.assertEqual(self.runner("retry", "latest", "make", "-C", self.root), 0, self.output)
        self.assertEqual(self.resume(), 2)
        self.assertEqual(self.calls(), 5)
        self.assertIn("5 attempts used", self.the_run().state["tasks"]["make"]["reason"])
        self.check_invariants()

    def test_rework_continues_the_session(self):
        """acc: rework continues the session (ACC-06)"""
        class Sessions(agents.CommandAgent):
            def capabilities(self):
                return super().capabilities() | {"resume"}

            def interpret(self, res):
                result = super().interpret(res)
                result.session_id = "session-1"
                return result

        self.addCleanup(agents.REGISTRY.__setitem__, "command", agents.CommandAgent)
        agents.REGISTRY["command"] = Sessions
        self.workflow(ONE)
        self.script([BAD, GOOD])
        self.assertEqual(self.start(), 0, self.output)
        self.assertIn("Make src/a.txt say good.", self.prompt(1))
        self.assertNotIn("Make src/a.txt say good.", self.prompt(2))      # only the feedback
        self.assertIn("did not pass", self.prompt(2))
        argv = self.read_json("make", "attempt-2", "invocation-1", "argv.json")
        self.assertEqual(argv["session_id"], "session-1")
        self.check_invariants()

    def test_rework_without_resume(self):
        """acc: rework without resume (ACC-21)"""
        self.workflow(ONE)
        self.script([BAD, GOOD])
        self.assertEqual(self.start(), 0, self.output)
        second = self.prompt(2)
        self.assertIn("Make src/a.txt say good.", second)                # the full prompt again
        self.assertIn("Your previous attempt was not accepted", second)   # plus the feedback
        argv = self.read_json("make", "attempt-2", "invocation-1", "argv.json")
        self.assertIsNone(argv["session_id"])
        self.check_invariants()

    def test_broken_session_is_abandoned(self):
        """acc: broken session is abandoned (ACC-07)"""
        self.workflow(ONE.replace("gate =", "timeout_min = 0.02\ngate ="))
        self.script([{"exit": 3}, {"write": {"src/a.txt": "half\n"}, "hang": True}, GOOD])
        self.assertEqual(self.start(), 0, self.output)
        self.assertIn("ended with an error", self.prompt(2))
        self.assertNotIn("already in the work tree", self.prompt(2))      # the tree was still at base
        self.assertIn("# Work of this task is already in the work tree", self.prompt(3))
        self.assertIn("src/a.txt", self.prompt(3).split("already in the work tree")[1])
        self.assertIn("Make src/a.txt say good.", self.prompt(2))
        self.assertIn("ran past its time limit", self.prompt(3))
        outcome = self.read_json("make", "attempt-2", "invocation-1", "outcome.json")
        self.assertEqual(outcome["status"], "timed-out")
        self.assertIsNone(self.read_json("make", "attempt-3", "invocation-1", "argv.json")["session_id"])
        self.check_invariants()

    def test_attempts_bounded_by_gates(self):
        """acc: attempts bounded by gates (ACC-08)"""
        self.workflow(ONE)
        self.script([bad(1), bad(2), bad(3), GOOD])
        self.assertEqual(self.start(), 2)
        self.assertEqual((self.status("make"), self.calls()), ("failed", 3))
        self.check_invariants()

    def test_blocked_is_a_legal_answer(self):
        """acc: blocked is a legal answer (ACC-10)"""
        self.workflow(ONE)
        self.script([{"answer": {"outcome": "blocked", "summary": "", "responses": [],
                                 "blocked_reason": "The gate greps a file the brief forbids."}}])
        self.assertEqual(self.start(), 255)
        st = self.the_run().state["tasks"]["make"]
        self.assertEqual(st["status"], "blocked")
        self.assertIn("The gate greps a file the brief forbids.", st["reason"])
        self.check_invariants()

    def test_invalid_answers_are_protocol_retries(self):
        """A prose answer is retried at most twice in fresh invocation directories, and uses no
        producer attempt."""
        self.workflow(ONE)
        self.script([{"write": {"src/a.txt": "good\n"}, "raw_answer": "I think it went well."},
                     {"answer": {"outcome": "done"}},
                     {"answer": done()}])
        self.assertEqual(self.start(), 0, self.output)
        self.assertEqual(self.the_run().state["tasks"]["make"]["attempts_used"], 1)
        for n in (1, 2, 3):
            self.assertTrue(os.path.isdir(self.task_file("make", "attempt-1", f"invocation-{n}")))
        self.assertEqual(self.read_json("make", "attempt-1", "result.json")["agent_calls"], 3)
        self.check_invariants()

    def test_invented_response_id_retry_gets_error_and_keeps_candidate(self):
        self.workflow(ONE)
        self.script([{'write': {'src/a.txt': 'good\n'}, 'answer': done(responses=[
            dict(finding='I fixed a typo', action='fixed', note='Self-found')])},
            {'answer': done(), 'match': 'which was not listed as needing a response'}])
        self.assertEqual(self.start(), 0, self.output)
        self.assertEqual(self.the_run().state['tasks']['make']['attempts_used'], 1)
        with open(self.task_file('make', 'attempt-1', 'invocation-2', 'prompt.md')) as fh:
            prompt = fh.read()
        self.assertIn('Return "responses": []', prompt)
        self.assertIn('I fixed a typo', prompt)
        self.assertIn('Do not repeat completed', prompt)
        self.check_invariants()

    def test_verifying_check(self):
        """acc: verifying check (ACC-11)"""
        self.workflow(ONE + '''
[[task]]
id = "words"
type = "check"
verifies = "make"
read_only = true
run = ["grep -q 'very good' src/a.txt || { echo NEEDS-VERY; exit 1; }"]
''')
        self.script([GOOD, {"write": {"src/a.txt": "very good\n"}, "answer": done()}])
        self.assertEqual(self.start(), 0, self.output)
        self.assertIn("NEEDS-VERY", self.prompt(2))
        self.assertEqual(self.status("words"), "accepted")
        self.check_invariants()

    def test_human_rejection(self):
        """acc: human rejection (ACC-12)"""
        self.workflow(ONE + '[[task]]\nid = "look"\ntype = "human"\nverifies = "make"\n')
        self.script([GOOD, {"write": {"src/a.txt": "good, and polite\n"}, "answer": done()}])
        self.assertEqual(self.start(), 255)
        self.check_invariants()
        self.assertEqual(self.runner("reject", "latest", "look", "-m", "Say it politely.",
                                     "-C", self.root), 0, self.output)
        self.assertEqual(self.resume(), 255)
        self.assertIn("Say it politely.", self.prompt(2))
        self.assertEqual(self.runner("approve", "latest", "look", "-C", self.root), 0)
        self.assertEqual(self.resume(), 0, self.output)
        self.assertEqual(self.read_json("look", "decision.json")["decision"], "approve")
        self.assertEqual(self.calls(), 2)                 # approval repeats no work
        self.check_invariants()

    def test_undeclared_writes_are_reverted(self):
        """acc: undeclared writes are reverted (ACC-13)"""
        self.workflow(ONE)
        self.script([{"write": {"src/a.txt": "good\n", "NOTES.md": "scratch\n"}, "answer": done()},
                     {"answer": done()}])
        self.assertEqual(self.start(), 0, self.output)
        self.assertFalse(os.path.exists(os.path.join(self.root, "NOTES.md")))
        self.assertIn("NOTES.md: it is outside this task's `writes`", self.prompt(2))
        reverted = self.read_json("make", "attempt-1", "reverted.json")
        self.assertEqual([r["path"] for r in reverted], ["NOTES.md"])
        self.assertEqual(self.git_out("show", "--name-only", "--format=", "HEAD"), "src/a.txt")
        self.check_invariants()

    def test_gates_must_not_change_the_candidate(self):
        """acc: gates must not change the candidate (ACC-14)"""
        self.workflow(ONE.replace('gate = ["grep -q good src/a.txt"]',
                                  'max_attempts = 1\ngate = ["echo rewritten > src/a.txt; echo x > litter.log"]'))
        self.script([GOOD])
        self.assertEqual(self.start(), 2)
        verification = self.read_json("make", "attempt-1", "verification.json")
        self.assertEqual(verification["results"][0]["result"], "void")
        self.assertEqual(sorted(verification["results"][0]["changed_the_tree"]),
                         ["litter.log", "src/a.txt"])
        cause = self.the_run().state["tasks"]["make"]["last_cause"]
        self.assertIn("echo rewritten > src/a.txt", cause)
        self.assertIn("litter.log", cause)
        self.check_invariants()

    def test_checks_must_not_change_the_candidate(self):
        """acc: checks must not change the candidate (ACC-15)"""
        self.workflow(ONE.replace("gate =", "max_attempts = 1\ngate =") + '''
[[task]]
id = "fmt"
type = "check"
verifies = "make"
run = ["echo formatted >> src/a.txt"]
''')
        self.script([GOOD])
        self.assertEqual(self.start(), 2)
        self.assertIn("src/a.txt", self.the_run().state["tasks"]["make"]["last_cause"])
        with open(self.task_file("make", "failed.patch")) as fh:
            self.assertNotIn("formatted", fh.read())      # the patch holds the author's work only
        self.check_invariants()

    def test_commit_exactly_the_verified_candidate(self):
        """acc: commit exactly the verified candidate (ACC-16)"""
        self.workflow(ONE + '[[task]]\nid = "c"\ntype = "check"\nverifies = "make"\n'
                      'read_only = true\nrun = ["true"]\n')
        self.script([BAD, GOOD])
        self.assertEqual(self.start(), 0, self.output)
        st = self.the_run().state["tasks"]["make"]
        verification = self.read_json("make", "attempt-2", "verification.json")
        self.assertEqual([r["verifier"] for r in verification["results"]], ["gate:1", "c"])
        self.assertEqual({r["candidate"] for r in verification["results"]}, {st["candidate"]})
        self.assertTrue(all(len(r["config_sha256"]) == 64 for r in verification["results"]))
        self.assertEqual(self.git_out("rev-parse", "HEAD^{tree}"), st["candidate"])
        self.assertEqual(self.git_out("rev-list", "--count", "main..HEAD"), "1")    # GIT-02
        body = self.git_out("log", "-1", "--format=%B")
        self.assertIn(f"Run: {self.the_run().state['run_id']}", body)
        self.assertIn("Task: make", body)
        self.check_invariants()

    def test_deletions_and_empty_files(self):
        """acc: deletions and empty files (ACC-17)"""
        self.write("legacy/mod.py", "x = 1\n")
        self.workflow('''
[[task]]
id = "make"
type = "implement"
prompt = "p"
outputs = [{ path = "pkg/__init__.py", may_be_empty = true }]
writes = ["pkg/**", "legacy/**"]
removes = ["legacy/**"]
gate = ['test -z "$(ls legacy 2>/dev/null)"']
''')
        self.script([{"write": {"pkg/__init__.py": ""}, "remove": ["legacy/mod.py"],
                      "answer": done()}])
        self.assertEqual(self.start(), 0, self.output)
        self.check_invariants()

    def test_work_must_be_visible_to_git(self):
        """acc: work must be visible to git (ACC-18)"""
        self.workflow(ONE.replace("gate =", "max_attempts = 1\ngate ="))
        self.script([{"write": {".gitignore": "src/\n", "src/a.txt": "good\n"}, "answer": done()}])
        self.assertEqual(self.start(), 2)
        cause = self.the_run().state["tasks"]["make"]["last_cause"]
        self.assertIn(".gitignore: it is protected", cause)
        self.assertFalse(os.path.exists(os.path.join(self.root, ".gitignore")))
        self.check_invariants()

    def test_a_task_that_owns_gitignore_cannot_hide_its_output(self):
        """acc: work must be visible to git (ACC-18, the task owns .gitignore)"""
        self.workflow(ONE.replace('gate =', 'writes = ["src/**", ".gitignore"]\n'
                                  'max_attempts = 1\ngate ='))
        self.script([{"write": {".gitignore": "src/\n", "src/a.txt": "good\n"}, "answer": done()}])
        self.assertEqual(self.start(), 2)
        self.assertIn("src/a.txt is now ignored by git",
                      self.the_run().state["tasks"]["make"]["last_cause"])
        self.check_invariants()

    def test_standalone_verifiers(self):
        """acc: standalone verifiers (ACC-19)"""
        self.workflow(ONE + '''
[[task]]
id = "lint"
type = "check"
needs = ["make"]
read_only = true
run = ["false"]
[[task]]
id = "after"
type = "human"
needs = ["lint"]
''')
        self.script([GOOD])
        self.assertEqual(self.start(), 2)
        state = self.the_run().state["tasks"]
        self.assertEqual((state["lint"]["status"], state["after"]["status"]), ("failed", "skipped"))
        self.assertIn("'lint' is failed", state["after"]["reason"])
        self.check_invariants()

    def test_standalone_human_rejection(self):
        """acc: standalone verifiers (ACC-19, the human half)"""
        self.workflow(ONE + '[[task]]\nid = "signoff"\ntype = "human"\nneeds = ["make"]\n'
                      '[[task]]\nid = "later"\ntype = "check"\nneeds = ["signoff"]\nrun = ["true"]\n')
        self.script([GOOD])
        self.assertEqual(self.start(), 255)
        self.assertIsNone(self.the_run().state["active_producer"])       # it holds nothing
        self.assertEqual(self.runner("reject", "latest", "signoff", "-m", "Not this quarter.",
                                     "-C", self.root), 0)
        self.assertEqual(self.resume(), 255)
        state = self.the_run().state["tasks"]
        self.assertEqual((state["signoff"]["status"], state["later"]["status"]),
                         ("blocked", "skipped"))
        self.assertEqual(self.read_json("signoff", "decision.json")["note"], "Not this quarter.")
        self.check_invariants()

    def test_destructive_verifiers_are_restored(self):
        """acc: destructive verifiers are restored (ACC-20)"""
        self.workflow(ONE.replace("gate =", "max_attempts = 1\ngate =") + '''
[[task]]
id = "mutants"
type = "check"
verifies = "make"
restores = true
run = ["echo mutated > src/a.txt; sleep 30"]
''', defaults="gate_timeout_min = 0.05")     # 3 s: `true` must not time out on a loaded machine
        self.script([GOOD])
        self.assertEqual(self.start(), 2)
        results = self.read_json("make", "attempt-1", "verification.json")["results"]
        self.assertEqual((results[1]["result"], results[1]["runs"][0]["result"]), ("fail", "timeout"))
        self.assertEqual(results[1]["changed_the_tree"], ["src/a.txt"])
        self.assertNotIn("changed the candidate", self.the_run().state["tasks"]["make"]["reason"])
        with open(self.task_file("make", "failed.patch")) as fh:
            self.assertNotIn("mutated", fh.read())
        self.check_invariants()

    def test_a_destructive_verifier_that_passes(self):
        """acc: destructive verifiers are restored (ACC-20, the passing case)"""
        self.workflow(ONE + '[[task]]\nid = "mutants"\ntype = "check"\nverifies = "make"\n'
                      'restores = true\nrun = ["echo mutated > src/a.txt"]\n')
        self.script([GOOD])
        self.assertEqual(self.start(), 0, self.output)
        self.assertEqual(self.git_out("show", "HEAD:src/a.txt"), "good")
        self.check_invariants()

    def test_what_a_rework_prompt_holds(self):
        """acc: what a rework prompt holds (ACC-22: the composition
        and the validator the engine uses)"""
        answered = [{"id": "make/PE-1", "severity": "blocking", "title": "Off by one",
                     "response": "fixed: bounds corrected"},
                    {"id": "make/SC-1", "severity": "blocking", "title": "Wrong field width",
                     "response": "fixed"}]
        text = prompts.feedback_text({"cause_title": "`ctest` did not pass", "cause": "FAILED t3",
                                      "needing": [], "info": answered}, 60000)
        self.assertIn("FAILED t3", text)
        self.assertIn("For information, no response needed", text)
        self.assertIn("make/PE-1", text)
        self.assertNotIn("Findings that need a response", text)
        self.assertEqual(validate.check_produce(done(), needing_response=[]), [])
        errors = validate.check_produce(done(), needing_response=["make/PE-2"])
        self.assertIn("no entry for finding make/PE-2", errors[0])
        twice = done(responses=[{"finding": "make/PE-1", "action": "fixed", "note": "again"}])
        self.assertIn("not listed as needing a response", validate.check_produce(twice, [])[0])


def script_step(text):
    return {"write": {"t.sh": text}, "answer": done()}


class ExpectedFailures(EngineCase):
    REPRO = '''
[[task]]
id = "make"
type = "implement"
prompt = "Reproduce the bug."
outputs = ["t.sh"]
max_attempts = 5
gate = ["true", { run = "sh t.sh", expect = "fail", fail_pattern = "SYMPTOM-42" }]
'''

    def test_expected_failure_gates(self):
        """acc: expected-failure gates (ACC-24): it passes only on a non-zero exit, other than 126,
        127 and a timeout, whose output matches fail_pattern; each wrong way is named"""
        self.workflow(self.REPRO, defaults="gate_timeout_min = 0.05")     # 3 s: `true` must not time out on a loaded machine
        self.script([script_step("exit 0\n"), script_step("echo other; exit 1\n"),
                     script_step("exit 127\n"), script_step("sleep 30\n"),
                     script_step("echo SYMPTOM-42; exit 1\n")])
        self.assertEqual(self.start(), 0, self.output)
        self.assertIn("sh t.sh   (must FAIL, matching /SYMPTOM-42/)", self.prompt(1))
        for n, why in [(2, "passed (expected a failure)"), (3, "failed without matching /SYMPTOM-42/"),
                       (4, "could not run (exit 127)"), (5, "could not run (it timed out)")]:
            self.assertIn("`sh t.sh` did not fail as required: " + why, self.prompt(n))
        verification = self.read_json("make", "attempt-5", "verification.json")
        gate = verification["results"][1]
        self.assertEqual((gate["result"], gate["expect"], gate["fail_pattern"], gate["runs"][0]["result"]),
                         ("pass", "fail", "SYMPTOM-42", "fail"))
        self.assertEqual(gate["candidate"], self.the_run().state["tasks"]["make"]["candidate"])
        wrong = self.read_json("make", "attempt-1", "verification.json")["results"][1]
        self.assertEqual((wrong["result"], wrong["note"]), ("fail", "passed (expected a failure)"))
        self.check_invariants()

    def test_a_signal_death_is_not_an_expected_failure(self):
        """acc: a gate killed by a signal does not reproduce the defect (ACC-30): output
        that matches fail_pattern followed by the process's own death is not a failure"""
        self.workflow(self.REPRO)
        self.script([script_step("printf 'SYMPTOM-42 test starting\\n'; kill -TERM $$\n"),
                     script_step("sh -c 'echo SYMPTOM-42; kill -KILL $$'; exit $?\n"),
                     script_step("echo SYMPTOM-42; exit 1\n")])
        self.assertEqual(self.start(), 0, self.output)
        for n, attempt in [(2, "attempt-1"), (3, "attempt-2")]:
            gate = self.read_json("make", attempt, "verification.json")["results"][1]
            self.assertEqual(gate["result"], "fail", gate)
            self.assertRegex(gate["note"], r"could not run \(killed by signal (15|9)\)")
            self.assertIn("`sh t.sh` did not fail as required: could not run (killed by signal",
                          self.prompt(n))
        self.assertEqual(self.read_json("make", "attempt-3", "verification.json")["results"][1]["result"],
                         "pass")
        self.assertEqual((checks.killed_by(-15), checks.killed_by(143), checks.killed_by(1),
                          checks.killed_by(255), checks.killed_by(None)), (15, 15, 0, 0, 0))
        self.check_invariants()

    def test_expected_failures_are_not_regression_gates(self):
        """acc: expected failures are not regression gates (ACC-25): a later task that touches a
        file the accepted task consumed re-runs its other gates, not the expected failure"""
        self.workflow('''
[[task]]
id = "base"
type = "implement"
prompt = "p"
outputs = ["src/calc.txt"]
gate = ["true"]
[[task]]
id = "repro"
type = "implement"
prompt = "p"
needs = ["base"]
outputs = ["t.sh"]
gate = ["test -s src/calc.txt", { run = "sh t.sh", expect = "fail", fail_pattern = "BUG-7" }]
[[task]]
id = "fix"
type = "implement"
prompt = "p"
needs = ["repro"]
outputs = ["src/calc.txt"]
gate = [{ run = "sh t.sh", new = true, fail_pattern = "BUG-7" }]
''')
        self.script({"base": [{"write": {"src/calc.txt": "broken\n"}, "answer": done()}],
                     "repro": [{"write": {"t.sh": "grep -q fixed src/calc.txt || { echo BUG-7; exit 1; }\n"},
                                "answer": done()}],
                     "fix": [{"write": {"src/calc.txt": "fixed\n"}, "answer": done()}]})
        self.assertEqual(self.start(), 0, self.output)
        ran = [r["verifier"] for r in self.read_json("fix", "attempt-1", "verification.json")["results"]]
        self.assertIn("regression:repro:gate:1", ran)
        self.assertNotIn("regression:repro:gate:2", ran)
        repro = self.read_json("repro", "attempt-1", "verification.json")["results"][1]
        self.assertEqual((repro["result"], repro["expect"]), ("pass", "fail"))
        self.check_invariants()


def lines(n, tag="x"):
    return "".join(f"{tag} {i}\n" for i in range(n))


class DiffBudget(EngineCase):
    BUDGET = '''
[[task]]
id = "make"
type = "implement"
prompt = "Make src/a.txt say good."
outputs = ["src/a.txt"]
writes = ["src/**"]
max_changed_files = 2
max_changed_lines = 3
gate = ["grep -q good src/a.txt"]
'''

    def test_diff_budget(self):
        """acc: diff budget (ACC-26): over budget the attempt does not pass, no gate or reviewer
        runs, and the feedback gives both counts, the limits and the five largest files"""
        self.workflow(self.BUDGET)
        big = {"write": {"src/a.txt": "good\n", **{f"src/f{i}.txt": lines(i) for i in range(1, 7)}},
               "answer": done()}
        self.script([big, {"write": {"src/a.txt": "good\n"}, "remove": [f"src/f{i}.txt" for i in range(1, 7)],
                           "answer": done()}])
        self.assertEqual(self.start(), 0, self.output)
        feedback = self.prompt(2)
        self.assertIn("the change is larger than the task's diff budget", feedback)
        self.assertIn("changes 7 files and 22 lines (added plus deleted); the limits are 2 files "
                      "and 3 lines", feedback)
        self.assertIn("- src/f6.txt: 6 lines\n- src/f5.txt: 5 lines\n- src/f4.txt: 4 lines\n"
                      "- src/f3.txt: 3 lines\n- src/f2.txt: 2 lines", feedback)
        self.assertNotIn("src/f1.txt", feedback.split("# Work of this task is already")[0])
        self.assertIn("src/f1.txt", feedback.split("# Work of this task is already")[1])   # ACC-32
        first = self.read_json("make", "attempt-1", "verification.json")
        self.assertEqual((first["results"], first["result"]), ([], "fail"))
        self.assertEqual({k: first["diff_budget"][k] for k in ("files", "lines", "result")},
                         {"files": 7, "lines": 22, "result": "fail"})
        self.assertFalse(os.path.exists(self.task_file("make", "attempt-1", "gate.log")))
        second = self.read_json("make", "attempt-2", "verification.json")
        self.assertEqual((second["diff_budget"]["files"], second["diff_budget"]["result"]), (1, "pass"))
        with open(self.task_file("make", "STATUS.md"), encoding="utf-8") as fh:
            self.assertIn("Diff budget of the latest candidate: 1 file and 1 line changed from its "
                          "base, against limits of 2 files and 3 lines: within budget.", fh.read())
        self.check_invariants()

    def test_rework_is_measured_from_base(self):
        """acc: diff budget (ACC-26: a rework is measured from BASE, not from the previous attempt,
        so small increments cannot reach a large change)"""
        self.workflow(self.BUDGET.replace("max_changed_files = 2\n", "")
                      .replace("grep -q good src/a.txt", "grep -q good src/a.txt && test -s src/d.txt"))
        self.script([{"write": {"src/a.txt": "good\n", "src/b.txt": "1\n"}, "answer": done()},
                     {"write": {"src/c.txt": "1\n", "src/d.txt": "1\n"}, "answer": done()},
                     {"remove": ["src/b.txt", "src/c.txt"], "answer": done()}])
        self.assertEqual(self.start(), 0, self.output)
        self.assertIn("did not pass", self.prompt(2))                 # 2 lines: within budget
        # 2 lines more than attempt 1, but 4 from the base: over the limit of 3
        self.assertIn("changes 4 files and 4 lines (added plus deleted); the limits are 3 lines",
                      self.prompt(3))
        self.check_invariants()

    def test_diff_budget_and_no_progress(self):
        """acc: diff budget and no progress (ACC-27): the same overage on an identical candidate
        tree twice fails the task as no progress"""
        self.workflow(self.BUDGET.replace("gate =", "max_attempts = 5\ngate ="))
        over = {"write": {"src/a.txt": lines(10)}, "answer": done()}
        self.script([over, over, GOOD])
        self.assertEqual(self.start(), 2)
        self.assertEqual(self.calls(), 2)
        reason = self.the_run().state["tasks"]["make"]["reason"]
        self.assertIn("no progress", reason)
        self.assertIn("diff-budget:1:10", reason)
        self.check_invariants()

    def test_diff_budget_counts_every_change(self):
        """acc: diff budget counts every change (ACC-28): an added untracked file, a deletion, an
        unchanged rename and a changed binary are 4 files; the rename and the binary add no lines"""
        self.write("data/gone.txt", lines(3))
        self.write("data/keep.txt", lines(20, "keep"))
        with open(os.path.join(self.root, "data/blob.bin"), "wb") as fh:
            fh.write(b"\0\1\2binary")
        self.workflow('''
[[task]]
id = "make"
type = "implement"
prompt = "p"
outputs = ["data/new.txt"]
writes = ["data/**"]
max_attempts = 1
max_changed_files = 3
gate = ["true"]
''')
        self.script([{"write": {"data/new.txt": lines(2, "new"), "data/moved.txt": lines(20, "keep"),
                                "data/blob.bin": "\u0000\u0003other"},
                      "remove": ["data/gone.txt", "data/keep.txt"], "answer": done()}])
        self.assertEqual(self.start(), 2)
        budget = self.read_json("make", "attempt-1", "verification.json")["diff_budget"]
        self.assertEqual((budget["files"], budget["lines"], budget["max_changed_files"]), (4, 5, 3))
        self.assertNotIn("max_changed_lines", budget)
        self.assertEqual(sorted((f["path"], f["lines"]) for f in budget["largest"]),
                         [("data/blob.bin", 0), ("data/gone.txt", 3), ("data/moved.txt", 0),
                          ("data/new.txt", 2)])
        self.check_invariants()


class Freezing(EngineCase):
    TWO = '''
[[task]]
id = "a"
type = "implement"
prompt = "Make a."
outputs = ["src/a.txt"]
gate = ["grep -q good src/a.txt"]
[[task]]
id = "b"
type = "implement"
prompt = "Make b."
needs = ["a"]
outputs = ["src/b.txt"]
{b_extra}
gate = ["true"]
'''

    def test_protected_files(self):
        """frz: protected files (FRZ-01)"""
        self.write("docs/spec/x.md", "spec\n")
        self.workflow(ONE.replace('outputs = ["src/a.txt"]', 'outputs = ["src/a.txt"]\n'
                                  'writes = ["src/**", "docs/**"]'),
                      defaults='protected = ["docs/spec/**"]')
        self.script([{"write": {"src/a.txt": "good\n", "docs/spec/x.md": "edited\n",
                                "docs/spec/new.md": "new\n"}, "answer": done()},
                     {"answer": done()}])
        self.assertEqual(self.start(), 0, self.output)
        with open(os.path.join(self.root, "docs/spec/x.md")) as fh:
            self.assertEqual(fh.read(), "spec\n")
        self.assertFalse(os.path.exists(os.path.join(self.root, "docs/spec/new.md")))
        self.assertIn("docs/spec/x.md: it is protected", self.prompt(2))
        self.assertIn("docs/spec/new.md: it is protected", self.prompt(2))
        self.check_invariants()

    def test_accepted_outputs_are_frozen(self):
        """frz: accepted outputs are frozen (FRZ-02)"""
        self.workflow(self.TWO.format(b_extra=""))
        self.script([{"write": {"src/a.txt": "good\n"}, "answer": done("A is made.")},
                     {"write": {"src/b.txt": "b\n", "src/a.txt": "good, but edited by b\n"},
                      "answer": done()},
                     {"answer": done()}])
        self.assertEqual(self.start(), 0, self.output)
        self.assertIn("frozen output of accepted task 'a'", self.prompt(3))
        self.assertEqual(self.git_out("show", "HEAD:src/a.txt"), "good")
        self.assertIn("A is made.", self.prompt(2))              # inputs: the upstream summary
        self.assertIn("src/a.txt", self.prompt(2))
        self.check_invariants()

    def test_explicit_claim(self):
        """frz: explicit claim (FRZ-03)"""
        self.workflow(self.TWO.format(b_extra='writes = ["src/b.txt", "src/a.txt"]'))
        self.script([{"write": {"src/a.txt": "good\n"}, "answer": done()},
                     {"write": {"src/b.txt": "b\n", "src/a.txt": "good, extended by b\n"},
                      "answer": done()}])
        self.assertEqual(self.start(), 0, self.output)
        self.assertEqual(self.git_out("show", "HEAD:src/a.txt"), "good, extended by b")
        with open(self.task_file("b", "attempt-1", "changes.diff")) as fh:
            self.assertIn("src/a.txt", fh.read())
        self.check_invariants()

    def test_checked_after_gates(self):
        """frz: checked after gates (FRZ-04)"""
        self.write("docs/spec/x.md", "spec\n")
        self.workflow(ONE.replace('gate = ["grep -q good src/a.txt"]',
                                  'max_attempts = 1\ngate = ["echo hacked > docs/spec/x.md"]'),
                      defaults='protected = ["docs/spec/**"]')
        self.script([GOOD])
        self.assertEqual(self.start(), 2)
        with open(os.path.join(self.root, "docs/spec/x.md")) as fh:
            self.assertEqual(fh.read(), "spec\n")
        self.assertIn("docs/spec/x.md", self.the_run().state["tasks"]["make"]["last_cause"])
        self.check_invariants()

    def test_claims_re_run_the_gates_they_touch(self):
        """frz: claims re-run the gates they touch (FRZ-05)"""
        self.workflow(self.TWO.format(b_extra='writes = ["src/b.txt", "src/a.txt"]\nmax_attempts = 1'))
        self.script([{"write": {"src/a.txt": "good\n"}, "answer": done()},
                     {"write": {"src/b.txt": "b\n", "src/a.txt": "broken by b\n"}, "answer": done()}])
        self.assertEqual(self.start(), 2)
        results = self.read_json("b", "attempt-1", "verification.json")["results"]
        self.assertEqual([(r["verifier"], r["result"]) for r in results],
                         [("gate:1", "pass"), ("regression:a:gate:1", "fail")])
        self.assertEqual(self.status("b"), "failed")
        self.assertEqual(self.git_out("show", "HEAD:src/a.txt"), "good")
        self.check_invariants()

    def test_a_shared_gate_runs_once_per_candidate(self):
        """frz: a gate shared by the tasks a candidate touches runs once (FRZ-10)"""
        import tempfile
        counter = os.path.join(tempfile.mkdtemp(prefix="gate-count-"), "count")
        gate = f"echo x >> {counter}"
        self.workflow(self.TWO.replace('gate = ["grep -q good src/a.txt"]', f'gate = ["{gate}"]')
                      .format(b_extra='writes = ["src/b.txt", "src/a.txt"]')
                      .replace('gate = ["true"]', f'gate = ["{gate}"]'))
        self.script([{"write": {"src/a.txt": "good\n"}, "answer": done()},
                     {"write": {"src/b.txt": "b\n", "src/a.txt": "also b\n"}, "answer": done()}])
        self.assertEqual(self.start(), 0, self.output)
        results = self.read_json("b", "attempt-1", "verification.json")["results"]
        self.assertEqual([(r["verifier"], r["result"], r.get("same_as")) for r in results],
                         [("gate:1", "pass", None), ("regression:a:gate:1", "pass", "gate:1")])
        with open(counter) as fh:
            # a's own gate, then b's: once each, not twice; each acceptance replays its own once
            self.assertEqual(fh.read(), "x\nx\nx\nx\n")
        self.check_invariants()

    def test_stale_acceptance_is_shown(self):
        """frz: stale acceptance is shown (FRZ-06)"""
        self.workflow('''
[[task]]
id = "a"
type = "design"
prompt = "Write a."
outputs = ["docs/a.md"]
gate = ["test -s docs/a.md"]
[[task]]
id = "c"
type = "design"
prompt = "Write c from a."
needs = ["a"]
outputs = ["docs/c.md"]
[[task]]
id = "c-ok"
type = "human"
verifies = "c"
[[task]]
id = "b"
type = "design"
prompt = "Revise a."
needs = ["a", "c"]
outputs = ["docs/b.md"]
writes = ["docs/b.md", "docs/a.md"]
gate = ["true"]
''')
        self.script([{"write": {"docs/a.md": "v1\n"}, "answer": done()},
                     {"write": {"docs/c.md": "from v1\n"}, "answer": done()},
                     {"write": {"docs/b.md": "b\n", "docs/a.md": "v2\n"}, "answer": done()}])
        self.assertEqual(self.start(), 255)
        self.assertEqual(self.runner("approve", "latest", "c-ok", "-C", self.root), 0)
        self.assertEqual(self.resume(), 0, self.output)
        stale = self.the_run().state["tasks"]["c"]["stale"]
        self.assertEqual([(s["file"], s["by"]) for s in stale], [("docs/a.md", "b")])
        self.assertNotEqual(stale[0]["was"], stale[0]["now"])
        self.assertNotIn("stale", self.the_run().state["tasks"]["a"])      # a has a gate: re-run
        verifiers = [r["verifier"] for r in
                     self.read_json("b", "attempt-1", "verification.json")["results"]]
        self.assertIn("regression:a:gate:1", verifiers)
        with open(os.path.join(self.the_run().path, "STATUS.md")) as fh:
            status = fh.read()
        self.assertIn("accepted (stale)", status)
        self.assertIn("older version of docs/a.md", status)
        self.check_invariants()

    def test_expected_failures_recheck_nothing(self):
        """frz: expected failures re-check nothing (FRZ-12): an accepted task whose only gate is
        an expected failure is never re-run, so it is marked stale when its input changes"""
        self.workflow('''
[[task]]
id = "a"
type = "design"
prompt = "Write a."
outputs = ["docs/a.md"]
gate = ["test -s docs/a.md"]
[[task]]
id = "c"
type = "design"
prompt = "Reproduce from a."
needs = ["a"]
outputs = ["docs/c.md"]
gate = [{run="echo SYMPTOM; exit 1",expect="fail",fail_pattern="SYMPTOM"}]
[[task]]
id = "b"
type = "design"
prompt = "Revise a."
needs = ["a", "c"]
outputs = ["docs/b.md"]
writes = ["docs/b.md", "docs/a.md"]
gate = ["true"]
''')
        self.script([{"write": {"docs/a.md": "v1\n"}, "answer": done()},
                     {"write": {"docs/c.md": "from v1\n"}, "answer": done()},
                     {"write": {"docs/b.md": "b\n", "docs/a.md": "v2\n"}, "answer": done()}])
        self.assertEqual(self.start(), 0, self.output)
        tasks = self.the_run().state["tasks"]
        self.assertEqual([(s["file"], s["by"]) for s in tasks["c"]["stale"]], [("docs/a.md", "b")])
        self.assertNotIn("stale", tasks["a"])
        self.check_invariants()

    def test_the_record_protects_itself(self):
        """frz: the record protects itself (FRZ-07)"""
        self.workflow(ONE)
        self.script([{"write": {"src/a.txt": "good\n"},
                      "run": ["for f in .runs/demo/*/state.json; do echo tampered >> $f; done"],
                      "answer": done()}])
        self.assertEqual(self.start(), 2)
        st = self.the_run().state["tasks"]["make"]
        self.assertEqual(st["status"], "failed")
        self.assertIn("the run record was changed", st["reason"])
        self.assertIn("state.json", st["reason"])
        self.check_invariants()

    def test_a_producer_cannot_edit_the_frozen_definitions(self):
        """frz: a producer cannot rewrite what the run executes (FRZ-13): editing the run's
        copied type and expanded workflow inside .runs/ during its call fails the job, and a
        resume refuses the changed record instead of trusting it"""
        self.workflow(ONE)
        self.script([{"write": {"src/a.txt": "good\n"},
                      "run": ["for f in .runs/demo/*/library/types/implement.toml; do "
                              "echo '# reviewers approve' >> $f; done",
                              "for f in .runs/demo/*/workflow.expanded.json; do echo ' ' >> $f; done"],
                      "answer": done()}])
        self.assertEqual(self.start(), 2)
        st = self.the_run().state["tasks"]["make"]
        self.assertEqual(st["status"], "failed")
        self.assertIn("the run record was changed", st["reason"])
        self.assertIn("library/types/implement.toml was changed", st["reason"])
        self.assertIn("workflow.expanded.json was changed", st["reason"])
        self.check_invariants()
        self.assertEqual(self.resume(), 2)
        self.assertIn("the run record was changed", self.output)

    def test_a_gate_cannot_rewrite_a_finished_result(self):
        """frz: the record protects itself (FRZ-07, a decision-bearing file of a finished
        directory, changed by a gate)"""
        self.workflow(ONE + '[[task]]\nid = "second"\ntype = "implement"\nprompt = "p"\n'
                      'needs = ["make"]\noutputs = ["src/b.txt"]\nmax_attempts = 1\n'
                      'gate = ["for f in .runs/demo/*/tasks/010-make/attempt-1/verification.json; '
                      'do echo {} > $f; done"]\n')
        self.script([GOOD, {"write": {"src/b.txt": "b\n"}, "answer": done()}])
        self.assertEqual(self.start(), 2)
        st = self.the_run().state["tasks"]["second"]
        self.assertIn("the run record was changed", st["reason"])
        self.assertIn("verification.json", st["reason"])

    def test_protection_cannot_be_narrowed(self):
        """frz: protection cannot be narrowed (FRZ-08)"""
        self.write("docs/spec/x.md", "spec\n")
        self.workflow(ONE.replace('outputs = ["src/a.txt"]', 'outputs = ["src/a.txt"]\n'
                                  'writes = ["src/**", "docs/**"]\nprotected = []'),
                      defaults='protected = ["docs/spec/**"]')
        self.script([{"write": {"src/a.txt": "good\n", "docs/spec/x.md": "edited\n"},
                      "answer": done()}, {"answer": done()}])
        self.assertEqual(self.start(), 0, self.output)
        self.assertIn("docs/spec/x.md: it is protected", self.prompt(2))
        self.check_invariants()

    def test_the_record_is_exported_only_where_needed(self):
        """frz: the record is exported only where needed (FRZ-09); run: environment (RUN-09)"""
        env = os.path.join(self.side, "env")
        self.workflow(f'''
[[task]]
id = "make"
type = "implement"
prompt = "p"
outputs = ["src/a.txt"]
gate = ["env > {env}.gate"]
[[task]]
id = "report"
type = "summarize"
prompt = "Summarise."
needs = ["make"]
outputs = ["docs/report.md"]
gate = ["true"]
''')
        self.script([{"write": {"src/a.txt": "good\n"}, "run": [f"env > {env}.make"],
                      "answer": done()},
                     {"write": {"docs/report.md": "r\n"}, "run": [f"env > {env}.report"],
                      "answer": done()}])
        os.environ["ANTHROPIC_API_KEY"] = "not-for-gates"
        self.assertEqual(self.start(), 0, self.output)
        seen = {}
        for who in ("make", "report", "gate"):
            with open(f"{env}.{who}") as fh:
                seen[who] = dict(l.rstrip("\n").split("=", 1) for l in fh if "=" in l)
        run = self.the_run()
        for who, task in (("make", "make"), ("report", "report"), ("gate", "make")):
            self.assertEqual(seen[who]["CODE_SMITH_RUN"], run.state["run_id"])
            self.assertEqual(seen[who]["CODE_SMITH_TASK"], task)
        self.assertEqual(seen["report"]["CODE_SMITH_RUN_DIR"], run.path)
        self.assertNotIn("CODE_SMITH_RUN_DIR", seen["make"])
        self.assertNotIn("CODE_SMITH_RUN_DIR", seen["gate"])
        self.assertIn("ANTHROPIC_API_KEY", seen["make"])
        self.assertNotIn("ANTHROPIC_API_KEY", seen["gate"])       # gates never see agent credentials
        self.check_invariants()

    def test_files_a_gate_executes_are_protected(self):
        """frz: files a gate executes are protected (FRZ-11)"""
        self.write("tools/check.py", "import sys\nsys.exit(0 if 'good' in open('src/a.txt').read() else 1)\n")
        self.workflow(ONE.replace('gate = ["grep -q good src/a.txt"]',
                                  'writes = ["src/**", "tools/**"]\ngate = ["python3 tools/check.py"]'))
        self.script([{"write": {"src/a.txt": "bad\n", "tools/check.py": "import sys\nsys.exit(0)\n"},
                      "answer": done()}, GOOD])
        self.assertEqual(self.start(), 0, self.output)
        self.assertIn("tools/check.py: it is protected", self.prompt(2))
        self.assertEqual(self.git_out("show", "--name-only", "--format=", "HEAD"), "src/a.txt")
        self.check_invariants()

    def test_an_embedded_repository_is_removed_early(self):
        """git: unsupported entries are removed early (GIT-11, through the engine)"""
        self.workflow(ONE)
        self.script([{"write": {"src/a.txt": "good\n"},
                      "run": ["git init -q src/nested", "git init -q src/vendored && cd src/vendored "
                              "&& git -c user.email=a@b -c user.name=n commit -q --allow-empty -m x"],
                      "answer": done()}, {"answer": done()}])
        self.assertEqual(self.start(), 0, self.output)
        self.assertIn("embedded git repository at 'src/nested'", self.prompt(2))
        self.assertIn("src/vendored", self.prompt(2))
        self.assertFalse(os.path.exists(os.path.join(self.root, "src/nested")))
        self.check_invariants()

    def test_a_link_out_of_the_repository_is_not_accepted(self):
        """A symbolic link inside `writes` that points outside the repository is put back."""
        self.workflow(ONE.replace('outputs = ["src/a.txt"]',
                                  'outputs = ["src/a.txt"]\nwrites = ["src/**"]'))
        self.script([{"write": {"src/a.txt": "good\n"}, "symlink": {"src/escape": self.side},
                      "answer": done()}, {"answer": done()}])
        self.assertEqual(self.start(), 0, self.output)
        self.assertIn("src/escape: it is a symbolic link that points outside", self.prompt(2))
        self.assertFalse(os.path.lexists(os.path.join(self.root, "src/escape")))
        self.check_invariants()


def read_record(case, task):
    """The task's set-aside record, read from `set-aside.json` or derived from what is left."""
    run = case.the_run()
    return engine.set_aside_record(run, gitops.Git(case.root), task)


def make_legacy(case, task):
    """A run as the previous runner left it: `failed.patch` and `base` in the state, but no
    `set-aside.json` and no manifest entry for one. Returns the record that was published."""
    run = case.the_run()
    path = os.path.join(run.task_dir(task), "set-aside.json")
    published = record.read_json(path)
    os.unlink(path)
    manifest_path = os.path.join(run.path, "integrity.json")
    manifest = record.read_json(manifest_path)
    del manifest["files"][os.path.relpath(path, run.path)]
    record.write_durable(manifest_path, record.dump_json(manifest))
    # The previous runner had no record pin; do not leave today's manifest pinned.
    git(case.root, "update-ref", "-d", record.record_ref(run.name))
    return published


class Failure(EngineCase):
    def test_work_is_set_aside(self):
        """fail: work is set aside (FAIL-01)"""
        self.write("src/keep.txt", "keep\n")
        self.workflow(ONE.replace('outputs = ["src/a.txt"]', 'outputs = ["src/a.txt"]\n'
                                  'writes = ["src/**"]\nmax_attempts = 1'))
        self.script([{"write": {"src/a.txt": "bad\n", "src/keep.txt": "changed\n",
                                "src/deep/er/new.txt": "n\n"}, "answer": done()}])
        self.assertEqual(self.start(), 2)
        with open(self.task_file("make", "failed.patch")) as fh:
            patch = fh.read()
        for path in ("src/a.txt", "src/keep.txt", "src/deep/er/new.txt"):
            self.assertIn(path, patch)
        with open(os.path.join(self.root, "src/keep.txt")) as fh:
            self.assertEqual(fh.read(), "keep\n")
        self.assertFalse(os.path.exists(os.path.join(self.root, "src/deep")))     # GIT-16
        self.check_invariants()

    def test_restore_by_type_and_mode(self):
        """fail: restore by type and mode (FAIL-02, through the engine)"""
        self.write("src/run.sh", "#!/bin/sh\n")
        os.chmod(os.path.join(self.root, "src/run.sh"), 0o755)
        self.write("src/f.txt", "file\n")
        self.write("src/data.bin", "old\n")
        os.symlink("f.txt", os.path.join(self.root, "src/link"))
        outside = os.path.join(self.side, "victim.txt")
        with open(outside, "w") as fh:
            fh.write("untouched\n")
        self.workflow(ONE.replace('outputs = ["src/a.txt"]', 'outputs = ["src/a.txt"]\n'
                                  'writes = ["src/**"]\nmax_attempts = 1'))
        self.script([{"write": {"src/a.txt": "bad\n", "src/with space\nand newline.txt": "odd\n"},
                      "run": ["chmod 644 src/run.sh", "rm src/f.txt && mkdir src/f.txt && "
                              "echo inner > src/f.txt/inner", f"rm src/link && ln -s {outside} src/link",
                              "printf '\\000\\001\\002' > src/data.bin"],
                      "answer": done()}])
        self.assertEqual(self.start(), 2)
        self.assertTrue(os.access(os.path.join(self.root, "src/run.sh"), os.X_OK))
        self.assertTrue(os.path.isfile(os.path.join(self.root, "src/f.txt")))
        self.assertEqual(os.readlink(os.path.join(self.root, "src/link")), "f.txt")
        with open(outside) as fh:
            self.assertEqual(fh.read(), "untouched\n")
        self.check_invariants()

    def test_other_branches_continue(self):
        """fail: other branches continue (FAIL-03)"""
        self.workflow(ONE.replace("gate =", "max_attempts = 1\ngate =") + '''
[[task]]
id = "after"
type = "implement"
prompt = "p"
needs = ["make"]
outputs = ["src/after.txt"]
gate = ["true"]
[[task]]
id = "after-check"
type = "check"
verifies = "after"
run = ["true"]
[[task]]
id = "other"
type = "implement"
prompt = "Independent."
outputs = ["other/o.txt"]
gate = ["true"]
''')
        self.script([BAD, {"match": "Independent", "write": {"other/o.txt": "o\n"},
                           "answer": done()}])
        self.assertEqual(self.start(), 2)
        state = self.the_run().state["tasks"]
        self.assertEqual({k: v["status"] for k, v in state.items()},
                         {"make": "failed", "after": "skipped", "after-check": "skipped",
                          "other": "accepted"})
        self.assertIn("'make' is failed", state["after"]["reason"])
        self.assertIn("'after' is skipped", state["after-check"]["reason"])
        self.check_invariants()

    def test_retry(self):
        """fail: retry (FAIL-04); rec: attempt numbers are never reused (REC-07, through the engine)"""
        self.workflow(ONE.replace('outputs = ["src/a.txt"]', 'outputs = ["src/a.txt"]\n'
                                  'writes = ["src/**"]\nmax_attempts = 1')
                      + '[[task]]\nid = "after"\ntype = "check"\nneeds = ["make"]\nrun = ["true"]\n')
        self.script([{"write": {"src/a.txt": "bad\n", "src/notes.txt": "kept work\n"},
                      "answer": done()}])
        self.assertEqual(self.start(), 2)
        self.assertEqual(self.status("after"), "skipped")

        # With the patch: the base still matches, so the work is put back and attempts restart.
        self.script([{"match": "Earlier work of this task is already in the work tree",
                      "write": {"src/a.txt": "good\n"}, "answer": done()}])
        self.assertEqual(self.runner("retry", "latest", "make", "--apply-patch", "-C", self.root),
                         0, self.output)
        self.assertEqual(self.status("after"), "pending")
        self.assertEqual(self.resume(), 0, self.output)
        self.assertEqual(self.git_out("show", "HEAD:src/notes.txt"), "kept work")
        self.assertTrue(os.path.isdir(self.task_file("make", "attempt-2")))
        self.assertFalse(os.path.exists(self.task_file("make", "attempt-2", "..", "attempt-1",
                                                       "prompt.md.changed")))
        self.check_invariants()

    def test_retry_refuses_a_patch_whose_base_moved(self):
        """fail: retry (FAIL-04, the refusal)"""
        self.workflow(ONE.replace("gate =", "max_attempts = 1\ngate =") + '''
[[task]]
id = "other"
type = "implement"
prompt = "Independent."
outputs = ["other/o.txt"]
gate = ["true"]
''')
        self.script([BAD, {"write": {"other/o.txt": "o\n"}, "answer": done()}])
        self.assertEqual(self.start(), 2)
        self.assertEqual(self.runner("retry", "latest", "make", "--apply-patch", "-C", self.root), 2)
        self.assertIn("other work was accepted since", self.output)
        self.assertEqual(self.status("make"), "failed")
        self.assertEqual(self.runner("retry", "latest", "other", "-C", self.root), 2)   # accepted
        self.script([GOOD])
        self.assertEqual(self.runner("retry", "latest", "make", "-C", self.root), 0)
        self.assertEqual(self.resume(), 0, self.output)
        self.assertNotIn("Earlier work of this task", self.prompt(1))     # a clean start
        self.check_invariants()

    def test_recovery_leaves_the_record_alone(self):
        """fail: recovery leaves the record alone (FAIL-14)"""
        self.workflow(ONE.replace('outputs = ["src/a.txt"]', 'outputs = ["src/a.txt"]\n'
                                  'writes = ["src/**"]\nmax_attempts = 1'))
        self.script([{"write": {"src/a.txt": "bad\n", "src/notes.txt": "kept work\n"},
                      "answer": done()}])
        self.assertEqual(self.start(), 2)
        run = self.the_run()
        first = read_record(self, "make")
        self.assertEqual((first["task"], first["attempt"], first["status"]), ("make", 1, "failed"))
        self.assertEqual(first["paths"], ["src/a.txt", "src/notes.txt"])
        self.assertEqual(first["head"], self.git_out("rev-parse", "HEAD"))
        self.assertEqual(first["candidate_ref"],
                         f"refs/code-smith/{run.name}/make/set-aside")
        self.assertEqual(first["patch"],
                         os.path.relpath(self.task_file("make", "failed.patch"), run.path))
        with open(self.task_file("make", "failed.patch")) as fh:
            first_patch = fh.read()
        attempt_one = self.task_file("make", "attempt-1")
        digest = tree_digest(attempt_one)

        # The work goes back, the next attempt adds to it, and the task is set aside again.
        self.script([{"write": {"src/a.txt": "still bad\n", "src/more.txt": "more\n"},
                      "answer": done()}])
        self.assertEqual(self.runner("retry", "latest", "make", "--apply-patch", "-C", self.root),
                         0, self.output)
        self.assertTrue(os.path.exists(self.task_file("make", "failed.patch")))
        self.assertEqual(self.resume(), 2, self.output)
        second = read_record(self, "make")
        self.assertEqual((second["attempt"], second["attempt_dir"]),
                         (2, os.path.relpath(self.task_file("make", "attempt-2"), run.path)))
        self.assertEqual(second["paths"], ["src/a.txt", "src/more.txt", "src/notes.txt"])
        self.assertEqual(second["base"], first["base"])              # nothing was accepted between
        self.assertNotEqual(second["candidate"], first["candidate"])
        self.assertEqual(second["candidate"],
                         self.git_out("rev-parse", second["candidate_ref"]))
        with open(self.task_file("make", "failed.patch")) as fh:
            second_patch = fh.read()
        for path in ("src/a.txt", "src/more.txt", "src/notes.txt"):  # the recovered work as well
            self.assertIn(path, second_patch)
        self.assertNotEqual(second_patch, first_patch)
        self.assertEqual(tree_digest(attempt_one), digest)      # the recovered attempt is untouched
        self.assertEqual(self.the_run().integrity_check(), [])
        self.check_invariants()

    def test_an_interrupted_set_aside_keeps_the_record(self):
        """fail: recovery leaves the record alone (FAIL-14, across a crash in the set-aside)"""
        for point in ("set-aside:before-restore", "restore:after-removals"):
            with self.subTest(point=point):
                self.setUp()
                self.workflow(ONE.replace('outputs = ["src/a.txt"]', 'outputs = ["src/a.txt"]\n'
                                          'writes = ["src/**"]\nmax_attempts = 1'))
                self.script([{"write": {"src/a.txt": "bad\n", "src/notes.txt": "kept work\n"},
                              "answer": done()}])

                def hook(where, point=point):
                    if where == point:
                        raise Killed(where)
                self.cli.CRASH = hook
                with self.assertRaises(Killed):
                    self.start()
                self.cli.CRASH = None

                # The restore is finished by the reconciler, so the step is replayed against a
                # work tree that no longer holds the work. The record must survive that.
                self.assertEqual(self.resume(), 2, self.output)
                kept = read_record(self, "make")
                self.assertEqual(kept["paths"], ["src/a.txt", "src/notes.txt"])
                self.assertNotEqual(kept["candidate"], kept["base"])
                self.assertEqual(kept["candidate"], self.git_out("rev-parse", kept["candidate_ref"]))
                self.assertEqual(self.the_run().integrity_check(), [])
                self.check_invariants()
                # The patch is still the whole work: applying it rebuilds the pinned candidate.
                git(self.root, "apply", self.task_file("make", "failed.patch"))
                git(self.root, "add", "-A")
                self.assertEqual(self.git_out("write-tree"), kept["candidate"])
                git(self.root, "reset", "-q", "--hard")
                self.doCleanups()

    def test_migration_does_not_lose_the_acceptance_boundary(self):
        """fail: migration does not lose the acceptance boundary (FAIL-16)"""
        self.workflow(ONE.replace("gate =", "max_attempts = 1\ngate =")
                      + '[[task]]\nid = "look"\ntype = "human"\nverifies = "make"\n' + '''
[[task]]
id = "other"
type = "implement"
prompt = "Independent."
outputs = ["other/o.txt"]
gate = ["true"]
''')
        self.script([GOOD, {"write": {"other/o.txt": "o\n"}, "answer": done()}])
        self.assertEqual(self.start(), 255)
        self.assertEqual(self.runner("reject", "latest", "look", "-m", "Not like this.",
                                     "-C", self.root), 0, self.output)
        self.assertEqual(self.resume(), 255, self.output)      # blocked: a person is needed
        self.assertEqual((self.status("make"), self.status("other")), ("blocked", "accepted"))
        published = make_legacy(self, "make")

        derived = read_record(self, "make")
        self.assertEqual(derived, dict(published, at=None))          # only the timestamp is lost
        self.assertFalse(os.path.exists(self.task_file("make", "set-aside.json")))   # a read only
        self.assertEqual(self.the_run().integrity_check(), [])
        accepted = self.git_out("rev-parse", "HEAD")
        self.assertNotEqual(derived["head"], accepted)               # not the later tip
        self.assertEqual(derived["head"], self.the_run().info["base_commit"])
        self.assertEqual(self.git_out("rev-parse", derived["head"] + "^{tree}"), derived["base"])
        # The acceptance the boundary exists to catch is inside <head>..HEAD, not before it.
        self.assertIn(accepted, self.git_out("rev-list", derived["head"] + "..HEAD").splitlines())
        trailers = gitops.Git(self.root).trailers(accepted)
        self.assertEqual((trailers["Run"], trailers["Task"]),
                         (self.the_run().state["run_id"], "other"))

    def test_environment_failures_use_no_attempts(self):
        """FAIL-06: a missing binary is rejected before any producer attempt or run exists."""
        self.workflow(ONE.replace('type = "implement"', 'type = "implement"\nagent = "ghost"')
                      + '[agents.ghost]\nargv = ["/nonexistent/agent-binary"]\n')
        self.assertEqual(self.start(), 2)
        self.assertIn("/nonexistent/agent-binary", self.output)
        self.assertFalse(os.path.exists(os.path.join(self.root, ".runs", "demo")))

    def test_recovery_artifacts_are_complete_and_pinned(self):
        """fail: recovery artifacts are complete and pinned (FAIL-07)"""
        self.workflow(ONE.replace('outputs = ["src/a.txt"]', 'outputs = ["src/a.txt"]\n'
                                  'writes = ["src/**"]\nmax_attempts = 1'),
                      defaults="diff_cap_bytes = 200")
        big = "".join(f"line {n}\n" for n in range(400))
        self.script([{"write": {"src/a.txt": "bad\n", "src/big.txt": big},
                      "run": ["head -c 2048 /dev/urandom > src/blob.bin"], "answer": done()}])
        self.assertEqual(self.start(), 2)
        with open(self.task_file("make", "attempt-1", "changes.diff")) as fh:
            self.assertIn("[diff truncated", fh.read())
        run = self.the_run()
        st = run.state["tasks"]["make"]
        git(self.root, "gc", "-q", "--prune=now")
        ref = f"refs/code-smith/{run.name}/make/set-aside"
        self.assertEqual(self.git_out("cat-file", "-t", ref), "tree")
        git(self.root, "apply", "--check", self.task_file("make", "failed.patch"))
        git(self.root, "apply", self.task_file("make", "failed.patch"))
        git(self.root, "add", "-A")
        self.assertEqual(self.git_out("write-tree"), self.git_out("rev-parse", ref))
        git(self.root, "reset", "-q", "--hard")
        self.assertEqual(st["status"], "failed")

    def test_an_unrelated_commit_does_not_block_recovery(self):
        """fail: an unrelated commit does not block recovery (FAIL-09, the check)"""
        self.workflow(ONE.replace('outputs = ["src/a.txt"]', 'outputs = ["src/a.txt"]\n'
                                  'writes = ["src/**"]\nmax_attempts = 1'))
        self.script([{"write": {"src/a.txt": "bad\n", "src/notes.txt": "kept work\n"},
                      "answer": done()}])
        self.assertEqual(self.start(), 2)
        set_aside_at = read_record(self, "make")["head"]

        # The owner fixes the brief and commits it, which `replan` requires. It touches none of
        # the paths the set-aside work changed.
        with open(self.wf_path, encoding="utf-8") as fh:
            text = fh.read()
        self.write("wf.toml", text.replace("Make src/a.txt say good.",
                                           "Make src/a.txt say good, and keep the notes."))
        self.commit()
        plan = plan_for(self, "make")
        self.assertEqual(plan["paths"], ["src/a.txt", "src/notes.txt"])
        self.assertEqual(plan["record"]["head"], set_aside_at)
        self.assertEqual(plan["base"], self.git_out("rev-parse", "HEAD^{tree}"))
        self.assertNotEqual(plan["record"]["base"], plan["base"])   # the whole tree did move

        # `expected` is what applying the patch by hand produces: the brief commit kept, and the
        # work of the attempt back on top of it.
        git(self.root, "apply", self.task_file("make", "failed.patch"))
        git(self.root, "add", "-A")
        self.assertEqual(self.git_out("write-tree"), plan["expected"])
        git(self.root, "reset", "-q", "--hard")
        self.assertEqual(self.the_run().integrity_check(), [])
        self.check_invariants()

    def test_conflicting_paths_refuse_recovery(self):
        """fail: conflicting paths refuse recovery (FAIL-10)"""
        self.workflow(ONE.replace('outputs = ["src/a.txt"]', 'outputs = ["src/a.txt"]\n'
                                  'writes = ["src/**"]\nmax_attempts = 1'))
        self.script([{"write": {"src/a.txt": "bad\n", "src/notes.txt": "kept work\n"},
                      "answer": done()}])
        self.assertEqual(self.start(), 2)

        # The owner commits one of the work's own paths, and one path the work never touched.
        self.write("src/a.txt", "the owner's own\n")
        self.write("src/elsewhere.txt", "unrelated\n")
        self.commit()
        refusal = ("the set-aside work of 'make' (attempt 1) no longer applies: these paths "
                   "changed since it was set aside: src/a.txt. Nothing was changed. Settle them, "
                   "or retry without --apply-patch to start clean.")
        for how in ("the record on disk", "a derived record"):
            with self.subTest(record=how):
                if how == "a derived record":
                    make_legacy(self, "make")
                before = untouched_digest(self)
                with self.assertRaises(engine.Refused) as caught:
                    plan_for(self, "make")
                self.assertEqual(str(caught.exception), refusal)
                self.assertEqual(untouched_digest(self), before)      # not even a derived record
        self.assertFalse(os.path.exists(self.task_file("make", "set-aside.json")))
        self.assertEqual(self.the_run().integrity_check(), [])

        # The owner's own local work is in the tree as well: an edit git has not been told about,
        # a file it does not track, and a mode change. A refusal leaves every one of them alone.
        clean = untouched_digest(self)
        self.write("src/a.txt", "the owner is still editing this\n")
        self.write("src/scratch.txt", "untracked\n")
        os.chmod(os.path.join(self.root, "src/elsewhere.txt"), 0o755)
        dirty = untouched_digest(self)
        self.assertNotEqual(dirty, clean)                  # the digest does see all three

        # A second edit of the file that is dirty already, and one under the directory git is
        # told to ignore: neither shows in `status` or in the index, and both move the digest.
        self.write("src/a.txt", "and editing it again\n")
        self.assertNotEqual(untouched_digest(self), dirty)
        self.write("src/a.txt", "the owner is still editing this\n")
        probe = self.write(os.path.join(".runs", "owner-notes.txt"), "git never sees this\n")
        self.assertNotEqual(untouched_digest(self), dirty)
        os.unlink(probe)
        self.assertEqual(untouched_digest(self), dirty)    # and it is the same digest each time

        with self.assertRaises(engine.Refused):
            plan_for(self, "make")
        self.assertEqual(untouched_digest(self), dirty)
        os.unlink(os.path.join(self.root, "src/scratch.txt"))
        git(self.root, "checkout", "--", ".")
        self.check_invariants()

    def test_a_path_that_became_a_directory_refuses_recovery(self):
        """fail: conflicting paths refuse recovery (FAIL-10, a file and a directory of one name)"""
        wide = ONE.replace('outputs = ["src/a.txt"]',
                           'outputs = ["src/a.txt"]\nwrites = ["src/**"]\nmax_attempts = 1')
        for work, owner in (({"src/here": "a file\n"}, "src/here/owner.txt"),
                            ({"src/here/deep.txt": "a file in a directory\n"}, "src/here")):
            with self.subTest(owner=owner):
                self.setUp()
                self.workflow(wide)
                self.script([{"write": dict(work, **{"src/a.txt": "bad\n"}),
                              "answer": done()}])
                self.assertEqual(self.start(), 2)
                mine = sorted(work)[0]
                self.assertIn(mine, read_record(self, "make")["paths"])

                # The owner commits the other kind of thing under the same name. Neither tree
                # holds an entry that differs by name, and the work still cannot go back: it
                # would take the owner's committed file with it.
                self.write(owner, "the owner's own\n")
                self.commit()
                before = untouched_digest(self)
                with self.assertRaises(engine.Refused) as caught:
                    plan_for(self, "make")
                self.assertEqual(str(caught.exception),
                                 "the set-aside work of 'make' (attempt 1) no longer applies: "
                                 f"these paths changed since it was set aside: {mine}. Nothing "
                                 "was changed. Settle them, or retry without --apply-patch to "
                                 "start clean.")
                self.assertEqual(untouched_digest(self), before)
                with open(os.path.join(self.root, owner), encoding="utf-8") as fh:
                    self.assertEqual(fh.read(), "the owner's own\n")
                self.check_invariants()
                self.doCleanups()

    def test_accepted_work_since_refuses_recovery(self):
        """fail: accepted work since refuses recovery (FAIL-11)"""
        other = '''
[[task]]
id = "other"
type = "implement"
prompt = "Independent."
outputs = ["other/o.txt"]
gate = ["true"]
'''
        made = {"match": "Independent", "write": {"other/o.txt": "o\n"}, "answer": done()}

        # An acceptance after the set-aside: 'make' fails first, 'other' is accepted after it.
        self.workflow(ONE.replace("gate =", "max_attempts = 1\ngate =") + other)
        self.script([BAD, made])
        self.assertEqual(self.start(), 2)
        accepted = self.git_out("rev-parse", "HEAD")
        before = untouched_digest(self)
        with self.assertRaises(engine.Refused) as caught:
            plan_for(self, "make")
        self.assertEqual(str(caught.exception),
                         "the set-aside work of 'make' (attempt 1) cannot be put back: other work "
                         f"was accepted since (commit {accepted[:7]} of task 'other'). Nothing was "
                         "changed. Retry without --apply-patch to start clean.")
        self.assertEqual(untouched_digest(self), before)
        self.check_invariants()

        # A `--reopen` revert after the set-aside: 'other' is accepted first, so the only commit
        # the branch gained since the work was set aside is the revert.
        self.doCleanups()
        self.setUp()
        self.workflow(other + ONE.replace("gate =", "max_attempts = 1\ngate ="))
        self.script([made, BAD])
        self.assertEqual(self.start(), 2)
        accepted = self.git_out("rev-parse", "HEAD")
        self.assertEqual(read_record(self, "make")["head"], accepted)
        revised = revised_workflow(self, other.replace("other/o.txt", "other/renamed.txt")
                                   + ONE.replace("gate =", "max_attempts = 1\ngate ="))
        self.assertEqual(self.runner("replan", "latest", "--workflow", revised, "--reopen", "other",
                                     "-C", self.root), 0, self.output)
        revert = self.git_out("rev-parse", "HEAD")
        self.assertEqual(self.git_out("rev-list", accepted + "..HEAD").splitlines(), [revert])
        before = untouched_digest(self)
        with self.assertRaises(engine.Refused) as caught:
            plan_for(self, "make")
        self.assertEqual(str(caught.exception),
                         "the set-aside work of 'make' (attempt 1) cannot be put back: accepted "
                         f"work was reverted since (commit {revert[:7]} reverts {accepted[:7]}). "
                         "Nothing was changed. Retry without --apply-patch to start clean.")
        self.assertEqual(untouched_digest(self), before)
        self.check_invariants()

    def test_migration_does_not_lose_the_acceptance_boundary_refusal(self):
        """fail: migration does not lose the acceptance boundary (FAIL-16, the refusal)"""
        self.workflow(ONE.replace("gate =", "max_attempts = 1\ngate =") + '''
[[task]]
id = "other"
type = "implement"
prompt = "Independent."
outputs = ["other/o.txt"]
gate = ["true"]
''')
        self.script([BAD, {"match": "Independent", "write": {"other/o.txt": "o\n"},
                           "answer": done()}])
        self.assertEqual(self.start(), 2)
        accepted = self.git_out("rev-parse", "HEAD")
        make_legacy(self, "make")                      # a run the previous runner left behind
        derived = read_record(self, "make")
        self.assertEqual(derived["head"], self.the_run().info["base_commit"])

        # Had the head been taken from the run's last tip, the range the check examines would be
        # empty and precisely this acceptance would pass unseen.
        self.assertEqual(self.the_run().state["last_tip"], accepted)
        self.assertEqual(self.git_out("rev-list", accepted + "..HEAD"), "")
        before = untouched_digest(self)
        with self.assertRaises(engine.Refused) as caught:
            plan_for(self, "make")
        self.assertEqual(str(caught.exception),
                         "the set-aside work of 'make' (attempt 1) cannot be put back: other work "
                         f"was accepted since (commit {accepted[:7]} of task 'other'). Nothing was "
                         "changed. Retry without --apply-patch to start clean.")
        self.assertEqual(untouched_digest(self), before)
        self.assertFalse(os.path.exists(self.task_file("make", "set-aside.json")))
        self.check_invariants()


    def test_a_moved_branch_refuses_recovery(self):
        """fail: a branch moved away from the set-aside refuses recovery (C1, and the legacy
        fallback to today's whole-tree rule)"""
        self.workflow(ONE.replace('outputs = ["src/a.txt"]', 'outputs = ["src/a.txt"]\n'
                                  'writes = ["src/**"]\nmax_attempts = 1'))
        self.script([{"write": {"src/a.txt": "bad\n", "src/notes.txt": "kept work\n"},
                      "answer": done()}])
        self.assertEqual(self.start(), 2)
        set_aside_at = read_record(self, "make")["head"]
        base = read_record(self, "make")["base"]
        git(self.root, "reset", "-q", "--hard", set_aside_at + "~1")

        before = untouched_digest(self)
        with self.assertRaises(engine.Refused) as caught:
            plan_for(self, "make")
        self.assertEqual(str(caught.exception),
                         "the set-aside work of 'make' (attempt 1) cannot be put back: the run "
                         f"branch is no longer a descendant of {set_aside_at[:7]}, where the work "
                         "was set aside. Nothing was changed.")
        self.assertEqual(untouched_digest(self), before)

        # The same branch under an old run: no commit of it has the recorded base as its tree, so
        # there is no head to derive and today's whole-tree rule stands in place of C1 and C3.
        make_legacy(self, "make")
        self.assertIsNone(read_record(self, "make")["head"])
        before = untouched_digest(self)
        with self.assertRaises(engine.Refused) as caught:
            plan_for(self, "make")
        self.assertEqual(str(caught.exception),
                         f"the patch of 'make' was made against tree {base}, but the accepted tree "
                         f"is now {self.git_out('rev-parse', 'HEAD^{tree}')}: other work was "
                         "accepted since. Retry without --apply-patch")
        self.assertEqual(untouched_digest(self), before)

    def test_a_lost_candidate_tree_refuses_recovery(self):
        """fail: a candidate tree that is no longer in the repository refuses recovery (C4)"""
        self.workflow(ONE.replace('outputs = ["src/a.txt"]', 'outputs = ["src/a.txt"]\n'
                                  'writes = ["src/**"]\nmax_attempts = 1'))
        self.script([{"write": {"src/a.txt": "bad\n", "src/notes.txt": "kept work\n"},
                      "answer": done()}])
        self.assertEqual(self.start(), 2)
        rec = read_record(self, "make")
        run = self.the_run()
        for ref in gitops.Git(self.root).pins(run.name):   # by hand: `prune` keeps these refs
            git(self.root, "update-ref", "-d", ref)
        git(self.root, "gc", "-q", "--prune=now")

        before = untouched_digest(self)
        with self.assertRaises(engine.Refused) as caught:
            plan_for(self, "make")
        self.assertEqual(str(caught.exception),
                         f"the candidate tree of 'make' is no longer in this repository "
                         f"({rec['candidate_ref']}). Its complete patch is still at {rec['patch']} "
                         "and can be applied by hand with `git apply`. Nothing was changed.")
        self.assertEqual(untouched_digest(self), before)
        self.assertTrue(os.path.exists(self.task_file("make", "failed.patch")))
        self.check_invariants()

    def test_work_outside_the_current_writes_refuses_recovery(self):
        """fail: a replan that narrows `writes` away from the work refuses recovery (C5)"""
        wide = ONE.replace('outputs = ["src/a.txt"]',
                           'outputs = ["src/a.txt"]\nwrites = ["src/**"]\nmax_attempts = 1')
        self.workflow(wide)
        self.script([{"write": {"src/a.txt": "bad\n", "src/notes.txt": "kept work\n"},
                      "answer": done()}])
        self.assertEqual(self.start(), 2)
        self.assertEqual(read_record(self, "make")["paths"], ["src/a.txt", "src/notes.txt"])

        narrow = revised_workflow(self, wide.replace('writes = ["src/**"]',
                                                     'writes = ["src/a.txt"]'))
        self.assertEqual(self.runner("replan", "latest", "--workflow", narrow, "-C", self.root),
                         0, self.output)
        self.assertEqual(self.status("make"), "pending")
        before = untouched_digest(self)
        with self.assertRaises(engine.Refused) as caught:
            plan_for(self, "make")
        self.assertEqual(str(caught.exception),
                         "the set-aside work of 'make' (attempt 1) cannot be put back: it changed "
                         "src/notes.txt, which 'make' may no longer write. Nothing was changed. "
                         "Retry without --apply-patch to start clean.")
        self.assertEqual(untouched_digest(self), before)
        self.check_invariants()


def tree_digest(path):
    digest = hashlib.sha256()
    for dirpath, dirnames, files in os.walk(path):
        dirnames.sort()
        for name in sorted(f for f in files if f not in ("index.json", "STATUS.md", "STATUS.html")):
            with open(os.path.join(dirpath, name), "rb") as fh:
                digest.update(os.path.relpath(os.path.join(dirpath, name), path).encode() + b"\0"
                              + fh.read())
    return digest.hexdigest()


def plan_for(case, task):
    """The task's recovery plan, or the refusal that says why the work cannot be put back."""
    run = case.the_run()
    git_ = gitops.Git(case.root)
    return engine.recovery_plan(run, engine.Engine(run, git_), git_, task)


def untouched_digest(case):
    """Everything a refusal must leave alone: the index exactly as it lies on disk, and every path
    of the work tree — contents, type and mode, the run record, untracked, uncommitted and ignored
    files included. No git command is run from here, so nothing in the digest can refresh the
    index's stat cache and hide a write that did happen."""
    digest = hashlib.sha256()

    def walk(directory):
        for entry in sorted(os.scandir(directory), key=lambda e: e.name):
            if entry.path == os.path.join(case.root, ".git"):
                continue                             # only its index is the runner's business
            info = entry.stat(follow_symlinks=False)
            digest.update(f"{os.path.relpath(entry.path, case.root)}\0{info.st_mode:o}\0".encode())
            if entry.is_symlink():
                digest.update(os.readlink(entry.path).encode())
            elif entry.is_dir():
                walk(entry.path)
            else:
                with open(entry.path, "rb") as fh:
                    digest.update(fh.read())

    walk(case.root)
    with open(os.path.join(case.root, ".git", "index"), "rb") as fh:
        digest.update(fh.read())
    return digest.hexdigest()


def revised_workflow(case, tasks):
    """A revised workflow outside the repository, so the tree stays clean for replan."""
    header = case.HEADER.format(defaults="", python=sys.executable,
                                agent=os.path.join(os.path.dirname(__file__),
                                                   "fake_agent.py")).replace("'", '"')
    header = header.replace('name = "demo"', 'name = "demo"\nroot = ' + json.dumps(case.root))
    path = os.path.join(case.side, "replan.toml")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(header + tasks)
    return path


class SetAsideRecord(EngineCase):
    """What `set-aside.json` holds, what survives a replan, and what an old run derives to."""

    def revised(self, tasks):
        """A revised workflow outside the repository, so the tree stays clean for replan."""
        header = self.HEADER.format(defaults="", python=sys.executable,
                                    agent=os.path.join(os.path.dirname(__file__),
                                                       "fake_agent.py")).replace("'", '"')
        header = header.replace('name = "demo"', 'name = "demo"\nroot = ' + json.dumps(self.root))
        path = os.path.join(self.side, "revised.toml")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(header + tasks)
        return path

    def test_replan_keeps_the_way_back_to_set_aside_work(self):
        """run: replan keeps the way back to set-aside work (RUN-18, the record)"""
        self.workflow(ONE.replace('outputs = ["src/a.txt"]', 'outputs = ["src/a.txt"]\n'
                                  'writes = ["src/**"]\nmax_attempts = 1'))
        self.script([{"write": {"src/a.txt": "bad\n", "src/notes.txt": "kept work\n"},
                      "answer": done()}])
        self.assertEqual(self.start(), 2)
        before = read_record(self, "make")
        self.assertEqual(before["paths"], ["src/a.txt", "src/notes.txt"])

        revised = self.revised(ONE.replace('outputs = ["src/a.txt"]', 'outputs = ["src/a.txt"]\n'
                                           'writes = ["src/**"]\nmax_attempts = 1')
                               .replace("Make src/a.txt say good.", "Make src/a.txt say good, "
                                        "and keep the notes."))
        self.assertEqual(self.runner("replan", "latest", "--workflow", revised, "-C", self.root),
                         0, self.output)
        st = self.the_run().state["tasks"]["make"]
        self.assertEqual(st["status"], "pending")
        self.assertNotIn("base", st)              # the state's way back went with the reduction
        self.assertNotIn("candidate", st)
        after = read_record(self, "make")         # the file in the task directory is still there
        self.assertEqual(after, before)
        self.assertEqual((after["attempt"], after["base"], after["candidate"], after["paths"]),
                         (1, before["base"], before["candidate"], ["src/a.txt", "src/notes.txt"]))
        self.assertEqual(after["candidate"], self.git_out("rev-parse", after["candidate_ref"]))
        self.assertTrue(os.path.exists(self.task_file("make", "failed.patch")))
        self.assertEqual(self.the_run().integrity_check(), [])

    def test_an_old_run_set_aside_on_the_starting_commit_recovers(self):
        """run: an old run set aside on the starting commit recovers (RUN-21, the derivation)"""
        self.workflow(ONE.replace("gate =", "max_attempts = 1\ngate ="))
        self.script([BAD])
        self.assertEqual(self.start(), 2)
        start_commit = self.the_run().info["base_commit"]
        self.assertEqual(self.git_out("rev-parse", "HEAD"), start_commit)   # nothing accepted yet
        published = make_legacy(self, "make")

        # The owner edits the brief and commits it, which `replan` requires: HEAD is no longer the
        # commit the work was set aside on, so the whole-tree rule would refuse from here.
        with open(self.wf_path, encoding="utf-8") as fh:
            text = fh.read()
        self.write("wf.toml", text.replace("Make src/a.txt say good.",
                                           "Make src/a.txt say good, politely."))
        self.commit()
        self.assertNotEqual(self.git_out("rev-parse", "HEAD"), start_commit)
        self.assertNotEqual(published["base"], self.git_out("rev-parse", "HEAD^{tree}"))

        derived = read_record(self, "make")
        self.assertEqual(derived, dict(published, at=None))
        self.assertEqual(derived["head"], start_commit)          # the starting commit, not None
        self.assertEqual(self.git_out("rev-parse", derived["head"] + "^{tree}"), derived["base"])
        self.assertEqual(self.git_out("rev-list", derived["head"] + "..HEAD").splitlines(),
                         [self.git_out("rev-parse", "HEAD")])    # only the owner's brief commit

        # A branch that no longer holds the set-aside commit has no head to derive at all.
        git(self.root, "reset", "-q", "--hard", start_commit + "~1")
        self.assertIsNone(read_record(self, "make")["head"])


WIDE = ONE.replace('outputs = ["src/a.txt"]',
                   'outputs = ["src/a.txt"]\nwrites = ["src/**"]\nmax_attempts = 1')
WORK = {"write": {"src/a.txt": "bad\n", "src/notes.txt": "kept work\n"}, "answer": done()}
OTHER = '''
[[task]]
id = "other"
type = "implement"
prompt = "Independent."
outputs = ["other/o.txt"]
gate = ["true"]
'''


def events(case):
    with open(os.path.join(case.the_run().path, "events.jsonl"), encoding="utf-8") as fh:
        return [json.loads(line) for line in fh]


class RetryRecovery(EngineCase):
    """`retry` accepts a pending producer that still has set-aside work, queues its recovery with
    `--apply-patch`, cancels it without the flag, and writes nothing when it refuses."""

    def test_set_aside_work_survives_a_replan(self):
        """fail: set-aside work survives a replan (FAIL-08, the retry)"""
        self.workflow(WIDE)
        self.script([{"write": {"src/a.txt": "bad\n", "src/notes.txt": "kept work\n"},
                      "answer": {"outcome": "blocked", "summary": "", "responses": [],
                                 "blocked_reason": "The brief contradicts the gate."}}])
        self.assertEqual(self.start(), 255)
        self.assertEqual(self.status("make"), "blocked")
        rec = read_record(self, "make")
        attempt_one = self.task_file("make", "attempt-1")
        digest = tree_digest(attempt_one)
        with open(self.task_file("make", "failed.patch"), "rb") as fh:
            patch = fh.read()

        # The owner edits the brief, commits it on the run branch, and replans: the task's
        # definition changed, so it is reset to pending.
        with open(self.wf_path, encoding="utf-8") as fh:
            text = fh.read()
        self.write("wf.toml", text.replace("Make src/a.txt say good.",
                                           "Make src/a.txt say good, and keep the notes."))
        self.commit()
        self.assertEqual(self.runner("replan", "latest", "-C", self.root), 0, self.output)
        self.assertEqual(self.status("make"), "pending")
        self.assertNotIn("base", self.the_run().state["tasks"]["make"])

        self.assertEqual(self.runner("retry", "latest", "make", "--apply-patch", "-C", self.root),
                         0, self.output)
        run = self.the_run()
        self.assertEqual(self.output,
                         "make: fresh attempts, continuing from the set-aside work of attempt 1. "
                         f"Continue with: runner resume {run.name}\n")
        st = run.state["tasks"]["make"]
        self.assertEqual(st["status"], "pending")
        self.assertEqual(st["recover"], {"from": "set-aside", "attempt": 1,
                                         "candidate": rec["candidate"]})
        self.assertNotIn("apply_patch", st)
        requested = [e for e in events(self) if e["event"] == "recover-requested"]
        self.assertEqual([(e["task"], e["attempt"], e["files"]) for e in requested],
                         [("make", 1, 2)])
        self.assertEqual(read_record(self, "make"), rec)
        self.assertEqual(tree_digest(attempt_one), digest)      # no finished attempt changed
        with open(self.task_file("make", "failed.patch"), "rb") as fh:
            self.assertEqual(fh.read(), patch)
        self.assertFalse(os.path.exists(self.task_file("make", "attempt-2")))
        self.assertEqual(run.integrity_check(), [])
        self.check_invariants()

    def test_conflicting_paths_refuse_recovery(self):
        """fail: conflicting paths refuse recovery (FAIL-10, through retry)"""
        self.workflow(WIDE)
        self.script([WORK])
        self.assertEqual(self.start(), 2)
        self.write("src/a.txt", "the owner's own\n")
        self.write("src/elsewhere.txt", "unrelated\n")
        self.commit()
        for how in ("the record on disk", "a derived record"):
            with self.subTest(record=how):
                if how == "a derived record":
                    make_legacy(self, "make")
                before = untouched_digest(self)
                self.assertEqual(self.runner("retry", "latest", "make", "--apply-patch",
                                             "-C", self.root), 2)
                self.assertIn("the set-aside work of 'make' (attempt 1) no longer applies: these "
                              "paths changed since it was set aside: src/a.txt. Nothing was "
                              "changed. Settle them, or retry without --apply-patch to start "
                              "clean.", self.output)
                # The work tree, the index, state.json, integrity.json, the event log,
                # failed.patch and the task directory all lie under the digest.
                self.assertEqual(untouched_digest(self), before)
                self.assertEqual(self.status("make"), "failed")
        self.assertFalse(os.path.exists(self.task_file("make", "set-aside.json")))
        self.assertEqual(self.the_run().integrity_check(), [])
        self.check_invariants()

    def test_a_derived_record_is_published_by_an_accepted_retry(self):
        """fail: a derived record is published only once `retry --apply-patch` is accepted"""
        self.workflow(WIDE)
        self.script([WORK])
        self.assertEqual(self.start(), 2)
        published = make_legacy(self, "make")
        self.assertEqual(self.runner("retry", "latest", "make", "--apply-patch", "-C", self.root),
                         0, self.output)
        self.assertEqual(self.read_json("make", "set-aside.json"), dict(published, at=None))
        run = self.the_run()
        self.assertEqual(run.integrity_check(), [])
        self.assertEqual(run.state["intents"], [])
        self.assertEqual(run.state["tasks"]["make"]["recover"]["attempt"], 1)

    def test_accepted_work_since_refuses_recovery(self):
        """fail: accepted work since refuses recovery (FAIL-11, through retry)"""
        made = {"match": "Independent", "write": {"other/o.txt": "o\n"}, "answer": done()}
        failing = ONE.replace("gate =", "max_attempts = 1\ngate =")

        # An acceptance after the set-aside.
        self.workflow(failing + OTHER)
        self.script([BAD, made])
        self.assertEqual(self.start(), 2)
        accepted = self.git_out("rev-parse", "HEAD")
        before = untouched_digest(self)
        self.assertEqual(self.runner("retry", "latest", "make", "--apply-patch", "-C", self.root),
                         2)
        self.assertIn("the set-aside work of 'make' (attempt 1) cannot be put back: other work "
                      f"was accepted since (commit {accepted[:7]} of task 'other'). Nothing was "
                      "changed. Retry without --apply-patch to start clean.", self.output)
        self.assertEqual(untouched_digest(self), before)
        self.assertEqual(self.status("make"), "failed")
        # The way it offers is open: without the flag the task starts clean.
        self.assertEqual(self.runner("retry", "latest", "make", "-C", self.root), 0, self.output)
        self.assertNotIn("recover", self.the_run().state["tasks"]["make"])
        self.check_invariants()

        # A `--reopen` revert after the set-aside.
        self.doCleanups()
        self.setUp()
        self.workflow(OTHER + failing)
        self.script([made, BAD])
        self.assertEqual(self.start(), 2)
        accepted = self.git_out("rev-parse", "HEAD")
        revised = revised_workflow(self, OTHER.replace("other/o.txt", "other/renamed.txt")
                                   + failing)
        self.assertEqual(self.runner("replan", "latest", "--workflow", revised, "--reopen", "other",
                                     "-C", self.root), 0, self.output)
        revert = self.git_out("rev-parse", "HEAD")
        before = untouched_digest(self)
        self.assertEqual(self.runner("retry", "latest", "make", "--apply-patch", "-C", self.root),
                         2)
        self.assertIn("the set-aside work of 'make' (attempt 1) cannot be put back: accepted "
                      f"work was reverted since (commit {revert[:7]} reverts {accepted[:7]}). "
                      "Nothing was changed. Retry without --apply-patch to start clean.",
                      self.output)
        self.assertEqual(untouched_digest(self), before)
        self.check_invariants()

    def test_an_invalid_queued_recovery_can_be_cancelled(self):
        """fail: an invalid queued recovery can be cancelled (FAIL-15, the cancellation)"""
        self.workflow(WIDE)
        self.script([WORK])
        self.assertEqual(self.start(), 2)
        self.assertEqual(self.runner("retry", "latest", "make", "--apply-patch", "-C", self.root),
                         0, self.output)
        queued = self.the_run().state["tasks"]["make"]["recover"]

        # A replan narrows `writes` away from src/notes.txt, which the queued work changed.
        narrow = revised_workflow(self, WIDE.replace('writes = ["src/**"]',
                                                     'writes = ["src/a.txt"]'))
        self.assertEqual(self.runner("replan", "latest", "--workflow", narrow, "-C", self.root),
                         0, self.output)
        self.assertEqual(self.status("make"), "pending")
        # The request is carried across the replan (task 8 of the design makes `replan` do this);
        # it is put back here so that what is cancelled below is a queued, invalid request.
        run = self.the_run()
        run.state["tasks"]["make"]["recover"] = queued
        run.save()
        self.assertEqual(engine.queued_recovery(self.the_run(), gitops.Git(self.root), "make"),
                         queued)
        with self.assertRaises(engine.Refused) as caught:
            plan_for(self, "make")                              # the request is no longer valid
        self.assertIn("which 'make' may no longer write", str(caught.exception))
        self.assertEqual(self.the_run().state["tasks"]["make"]["recover"], queued)  # still queued

        # `retry` without the flag is accepted on the pending producer and clears the queue.
        with open(self.task_file("make", "failed.patch"), "rb") as fh:
            patch = fh.read()
        self.assertEqual(self.runner("retry", "latest", "make", "-C", self.root), 0, self.output)
        run = self.the_run()
        self.assertEqual(self.output,
                         "make: fresh attempts, starting clean. The set-aside work of attempt 1 "
                         "stays in failed.patch and will not be put back. Continue with: runner "
                         f"resume {run.name}\n")
        st = run.state["tasks"]["make"]
        self.assertEqual(st["status"], "pending")
        self.assertNotIn("recover", st)
        self.assertNotIn("apply_patch", st)
        self.assertEqual(events(self)[-1]["event"], "retry")     # no recovery requested
        with open(self.task_file("make", "failed.patch"), "rb") as fh:
            self.assertEqual(fh.read(), patch)

        # The next attempt starts from the accepted tree, with nothing put back.
        self.script([{"write": {"src/a.txt": "good\n"}, "answer": done()}])
        self.assertEqual(self.resume(), 0, self.output)
        self.assertNotIn("Earlier work of this task", self.prompt(1))
        self.assertFalse(os.path.exists(os.path.join(self.root, "src/notes.txt")))
        self.check_invariants()

    def test_a_queued_recovery_is_cancelled_without_a_replan(self):
        """fail: an invalid queued recovery can be cancelled (FAIL-15, the queue itself)"""
        self.workflow(WIDE)
        self.script([WORK])
        self.assertEqual(self.start(), 2)
        self.assertEqual(self.runner("retry", "latest", "make", "--apply-patch", "-C", self.root),
                         0, self.output)
        self.assertEqual(self.status("make"), "pending")
        self.assertEqual(self.runner("retry", "latest", "make", "-C", self.root), 0, self.output)
        self.assertIn("The set-aside work of attempt 1 stays in failed.patch", self.output)
        self.assertNotIn("recover", self.the_run().state["tasks"]["make"])
        self.script([{"write": {"src/a.txt": "good\n"}, "answer": done()}])
        self.assertEqual(self.resume(), 0, self.output)
        self.assertNotIn("Earlier work of this task", self.prompt(1))
        self.assertFalse(os.path.exists(os.path.join(self.root, "src/notes.txt")))
        self.check_invariants()

    def test_a_legacy_queued_request_is_read_and_cancelled(self):
        """An older runner's `apply_patch: true` is read as a queued recovery, and `retry`
        without the flag cancels it like any other"""
        self.workflow(WIDE)
        self.script([WORK])
        self.assertEqual(self.start(), 2)
        rec = read_record(self, "make")
        run = self.the_run()
        run.state["tasks"]["make"].update(status="pending", apply_patch=True)
        del run.state["schema_version"]                      # as an older runner wrote it
        run.save()
        git_ = gitops.Git(self.root)
        self.assertEqual(engine.queued_recovery(self.the_run(), git_, "make"),
                         {"from": "set-aside", "attempt": 1, "candidate": rec["candidate"]})
        self.assertEqual(self.runner("retry", "latest", "make", "-C", self.root), 0, self.output)
        self.assertNotIn("apply_patch", self.the_run().state["tasks"]["make"])
        self.assertIsNone(engine.queued_recovery(self.the_run(), git_, "make"))

    def test_a_task_without_set_aside_work_keeps_the_old_refusals(self):
        """`retry` of a pending task with no set-aside record keeps today's refusal word for word,
        and an attempt that changed nothing has no patch to put back"""
        self.workflow(ONE)
        self.script([{"answer": {"outcome": "blocked", "summary": "", "responses": [],
                                 "blocked_reason": "Nothing to do."}}])
        self.assertEqual(self.start(), 255)
        self.assertEqual(read_record(self, "make")["paths"], [])  # the attempt changed nothing
        before = untouched_digest(self)
        self.assertEqual(self.runner("retry", "latest", "make", "--apply-patch", "-C", self.root),
                         2)
        self.assertIn("'make' has no set-aside patch to apply", self.output)
        self.assertEqual(untouched_digest(self), before)
        self.assertEqual(self.runner("retry", "latest", "make", "-C", self.root), 0, self.output)
        self.assertIn("make: fresh attempts, starting clean. Continue with", self.output)

        # A pending producer that never had set-aside work: an accepted one, reopened.
        self.doCleanups()
        self.setUp()
        failing = ONE.replace("gate =", "max_attempts = 1\ngate =")
        self.workflow(OTHER + failing)
        self.script([{"match": "Independent", "write": {"other/o.txt": "o\n"}, "answer": done()},
                     BAD])
        self.assertEqual(self.start(), 2)
        revised = revised_workflow(self, OTHER.replace("other/o.txt", "other/renamed.txt")
                                   + failing)
        self.assertEqual(self.runner("replan", "latest", "--workflow", revised, "--reopen", "other",
                                     "-C", self.root), 0, self.output)
        self.assertEqual(self.status("other"), "pending")
        before = untouched_digest(self)
        for flags in ((), ("--apply-patch",)):
            with self.subTest(flags=flags):
                self.assertEqual(self.runner("retry", "latest", "other", *flags, "-C", self.root),
                                 2)
                self.assertEqual(self.output,
                                 "runner: 'other' is pending; only a failed or blocked task "
                                 "is retried\n")
        self.assertEqual(untouched_digest(self), before)


class Killed(Exception):
    pass


class Resume(EngineCase):
    WF = ONE + '''
[[task]]
id = "words"
type = "check"
verifies = "make"
read_only = true
run = ["true"]
[[task]]
id = "doomed"
type = "implement"
prompt = "This one fails."
needs = ["make"]
outputs = ["src/d.txt"]
max_attempts = 1
gate = ["false"]
'''
    POINTS = ["state:before-rename", "agent:running", "attempt:after-agent", "attempt:counted",
              "inspect:after-pin", "verify:after-command",
              "commit:intent-recorded", "commit:after-commit-object", "commit:after-update-ref",
              "commit:before-outcome", "set-aside:before-restore", "restore:after-removals"]

    def kill_at(self, point, nth=1):
        seen = {"n": 0}

        def hook(where):
            if where == point:
                seen["n"] += 1
                if seen["n"] == nth:
                    raise Killed(where)
        self.cli.CRASH = hook

    def test_resume_from_disk(self):
        """run: resume from disk (RUN-03): killed at each of these points, `resume` continues and
        no finished attempt directory is changed"""
        for point in self.POINTS:
            with self.subTest(point=point):
                self.setUp()
                self.workflow(self.WF)
                steps = [BAD, GOOD, GOOD, {"write": {"src/d.txt": "d\n"}, "answer": done()},
                         {"write": {"src/d.txt": "d\n"}, "answer": done()}]
                self.script(steps)
                self.kill_at(point, nth=3 if point == "state:before-rename" else 1)
                with self.assertRaises(Killed):
                    self.start()
                finished = {}
                for tid in ("make", "doomed"):
                    tdir = self.the_run().task_dir(tid)
                    for name in os.listdir(tdir):
                        full = os.path.join(tdir, name)
                        if name.startswith("attempt-") and os.path.exists(
                                os.path.join(full, "verification.json")):
                            finished[full] = tree_digest(full)
                self.cli.CRASH = None
                self.assertEqual(self.resume(), 2, self.output)      # `doomed` fails by design
                state = self.the_run().state["tasks"]
                self.assertEqual((state["make"]["status"], state["words"]["status"],
                                  state["doomed"]["status"]), ("accepted", "accepted", "failed"),
                                 self.output)
                self.assertEqual(self.git_out("rev-list", "--count", "main..HEAD"), "1")
                for path, digest in finished.items():
                    self.assertEqual(tree_digest(path), digest, path)
                self.check_invariants()
                self.doCleanups()

    def test_a_kill_after_the_attempt_is_counted_costs_no_attempt(self):
        """run: the attempt is counted in the same save that moves on to inspecting it, so a kill
        before its work is checked resumes the check: with max_attempts = 1 good work is
        accepted, not failed unchecked, and the author is not called again"""
        for point in ("attempt:counted", "inspect:after-pin"):
            with self.subTest(point=point):
                self.setUp()
                self.workflow(ONE.replace("gate =", "max_attempts = 1\ngate ="))
                self.script([GOOD, GOOD])
                self.kill_at(point)
                with self.assertRaises(Killed):
                    self.start()
                self.assertEqual(self.the_run().state["tasks"]["make"]["step"], "inspect")
                self.cli.CRASH = None
                self.assertEqual(self.resume(), 0, self.output)
                self.assertEqual(self.status("make"), "accepted")
                self.assertEqual(self.calls(), 1)
                self.check_invariants()
                self.doCleanups()

    def test_the_diff_budget_is_recomputed_on_resume(self):
        """rec: the diff budget is recomputed on resume (REC-18): killed after the candidate is
        pinned and the budget measured, before its result is saved, `resume` measures the pinned
        trees again with the same result, and no attempt is used"""
        self.workflow(DiffBudget.BUDGET)
        self.script([{"write": {"src/a.txt": lines(10)}, "answer": done()}, GOOD])
        self.kill_at("verify:diff-budget-measured")
        with self.assertRaises(Killed):
            self.start()
        st = self.the_run().state["tasks"]["make"]
        self.assertEqual((st["step"], st["attempts_used"]), ("verify", 1))
        self.assertNotIn("diff_budget", st)
        self.assertFalse(os.path.exists(self.task_file("make", "attempt-1", "verification.json")))
        self.cli.CRASH = None
        self.assertEqual(self.resume(), 0, self.output)
        self.assertEqual(self.calls(), 2)
        budget = self.read_json("make", "attempt-1", "verification.json")["diff_budget"]
        self.assertEqual((budget["files"], budget["lines"], budget["result"]), (1, 10, "fail"))
        self.assertIn("changes 1 file and 10 lines", self.prompt(2))
        self.assertEqual(self.the_run().state["tasks"]["make"]["attempts_used"], 2)
        self.check_invariants()

    def test_a_kill_after_an_embedded_repository_is_removed_still_tells_the_author(self):
        """run: the removal of an embedded repository is recorded before it is made, so a kill
        right after it resumes the check and the author is still told; the work is not accepted"""
        self.workflow(ONE)
        self.script([{"write": {"src/a.txt": "good\n"}, "run": ["git init -q src/nested"],
                      "answer": done()}, {"answer": done()}])
        self.kill_at("inspect:embedded-removed")
        with self.assertRaises(Killed):
            self.start()
        self.assertFalse(os.path.exists(os.path.join(self.root, "src/nested")))
        self.cli.CRASH = None
        self.assertEqual(self.resume(), 0, self.output)
        self.assertEqual(self.calls(), 2)
        self.assertIn("embedded git repository at 'src/nested'", self.prompt(2))
        self.assertEqual(self.read_json("make", "attempt-1", "embedded.json"), ["src/nested"])
        self.check_invariants()

    def test_a_run_killed_before_its_branch_exists_is_resumed(self):
        """run: interrupted after the run directory was made and before its branch was created,
        `resume` creates the branch from the intent in the run's first state"""
        from unittest import mock
        self.workflow(ONE)
        self.script([GOOD, GOOD])
        with mock.patch.object(gitops.Git, "create_and_checkout_run_branch",
                               side_effect=Killed("Ctrl-C before the branch")):
            with self.assertRaises(Killed):
                self.start()
        self.assertEqual(self.git_out("symbolic-ref", "--short", "HEAD"), "main")
        self.assertEqual(self.resume(), 0, self.output)
        self.assertIn("run branch", self.output)
        self.assertEqual(self.git_out("symbolic-ref", "--short", "HEAD"),
                         self.the_run().info["branch"])
        self.assertEqual(self.status("make"), "accepted")
        self.check_invariants()

    def test_a_real_kill(self):
        """run: resume from disk (RUN-03, with the process really gone: exit without cleanup)"""
        self.workflow(ONE)
        self.script([GOOD, GOOD])
        env = dict(os.environ, CODE_SMITH_CRASH_AT="commit:after-update-ref")
        res = subprocess.run([sys.executable, RUNNER, "start", self.wf_path], env=env,
                             capture_output=True, text=True)
        self.assertEqual(res.returncode, 70, res.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.root, ".runs", "lock")))   # a stale lock
        res = subprocess.run([sys.executable, RUNNER, "resume", "-C", self.root],
                             capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("the commit had been made", res.stdout)
        self.assertEqual(self.calls(), 1)                          # the author is not run again
        self.assertEqual(self.git_out("rev-list", "--count", "main..HEAD"), "1")
        self.check_invariants()

    def test_external_changes_while_paused(self):
        """run: external changes while paused (RUN-12)"""
        self.workflow(ONE + '[[task]]\nid = "look"\ntype = "human"\nverifies = "make"\n')
        self.script([GOOD])
        self.assertEqual(self.start(), 255)
        self.runner("approve", "latest", "look", "-C", self.root)
        self.write("src/a.txt", "good, but someone edited it while the run waited\n")
        self.assertEqual(self.resume(), 2)
        self.assertIn("reconciliation error", self.output)
        self.assertIn("the work tree changed: src/a.txt", self.output)
        self.assertEqual(self.status("make"), "waiting_human")
        self.write("src/a.txt", "good\n")

        self.write("elsewhere.txt", "x\n")
        git(self.root, "add", "elsewhere.txt")
        git(self.root, "commit", "-q", "-m", "someone commits on the run branch", "elsewhere.txt")
        self.assertEqual(self.resume(), 2)
        self.assertIn("the branch tip changed", self.output)

    def test_resume_on_the_wrong_branch(self):
        self.workflow(ONE + '[[task]]\nid = "look"\ntype = "human"\nneeds = ["make"]\n')
        self.script([GOOD])
        self.assertEqual(self.start(), 255)
        git(self.root, "checkout", "-q", "main")
        self.assertEqual(self.resume(), 2)
        self.assertIn("is checked out", self.output)

    def test_interrupted_standalone_check_restores_before_retry(self):
        self.workflow('[[task]]\nid = "check"\ntype = "check"\nrestores = true\n'
                      'run = ["echo changed > README.md"]\n' + ONE)
        self.script([GOOD])
        # Interrupt after the command has returned but before its tree is restored.
        from codesmith import checks
        original = checks.run_commands
        def interrupted(*args, **kwargs):
            original(*args, **kwargs)
            raise Killed("after command")
        from unittest.mock import patch
        with patch.object(checks, "run_commands", interrupted):
            with self.assertRaises(Killed):
                self.start()
        self.assertEqual(self.status("check"), "running")
        self.assertEqual(self.resume(), 0, self.output)
        self.assertEqual(self.git_out("show", "HEAD:README.md"), "scratch")
        self.check_invariants()

    def test_reviews_require_qualified_read_only_boundary(self):
        self.workflow(ONE.replace("gate =", 'reviewers = ["principled-priya"]\ngate ='))
        self.script([GOOD])
        self.assertEqual(self.start(), 2)
        self.assertIn("needs boundary", self.output)
        self.assertEqual(self.calls(), 0)
        self.assertEqual(self.git_out("branch", "--show-current"), "main")



class RetryAdoptsTip(EngineCase):
    """`retry` re-records the pause expectation over the owner's own commits, so that `resume`
    does not refuse the commit the runbook told the owner to make; and refuses, writing nothing,
    when the branch or the tree moved in a way those commits cannot explain."""

    def test_an_unrelated_commit_does_not_block_recovery(self):
        """fail: an unrelated commit does not block recovery (FAIL-09, through retry and resume)"""
        self.workflow(WIDE)
        self.script([WORK])
        self.assertEqual(self.start(), 2)
        paused = self.the_run().state["expect"]
        self.assertEqual(paused["tip"], self.git_out("rev-parse", "HEAD"))

        # The owner commits a file the set-aside work never touched; no replan.
        self.write("notes/owner.txt", "the owner's own\n")
        self.commit()
        tip = self.git_out("rev-parse", "HEAD")
        self.assertEqual(self.runner("retry", "latest", "make", "--apply-patch", "-C", self.root),
                         0, self.output)
        run = self.the_run()
        self.assertEqual(run.state["expect"], {"tip": tip,
                                               "tree": self.git_out("rev-parse", "HEAD^{tree}")})
        self.assertNotEqual(run.state.get("last_tip"), tip)      # no acceptance happened
        self.assertEqual(run.state["tasks"]["make"]["recover"]["attempt"], 1)

        # `resume` reconciles instead of refusing the moved branch, the work is put back, and
        # the next attempt runs on it.
        self.script([{"write": {"src/a.txt": "good\n"}, "answer": done()}])
        self.assertEqual(self.resume(), 0, self.output)
        self.assertNotIn("branch tip changed", self.output)
        self.assertEqual(self.status("make"), "accepted")
        self.assertTrue(os.path.isdir(self.task_file("make", "attempt-2")))
        self.assertEqual(self.git_out("show", "HEAD:src/notes.txt"), "kept work")
        self.assertEqual(self.git_out("show", "HEAD:src/a.txt"), "good")
        self.assertEqual(self.git_out("show", "HEAD:notes/owner.txt"), "the owner's own")
        self.assertEqual(self.git_out("rev-parse", "HEAD^"), tip)
        self.check_invariants()

    def test_a_clean_retry_adopts_the_tip_too(self):
        """`retry` without the flag adopts the owner's commit as well, so the clean row works"""
        self.workflow(WIDE)
        self.script([WORK])
        self.assertEqual(self.start(), 2)
        self.write("notes/owner.txt", "the owner's own\n")
        self.commit()
        tip = self.git_out("rev-parse", "HEAD")
        self.assertEqual(self.runner("retry", "latest", "make", "-C", self.root), 0, self.output)
        self.assertEqual(self.the_run().state["expect"]["tip"], tip)
        self.script([{"write": {"src/a.txt": "good\n"}, "answer": done()}])
        self.assertEqual(self.resume(), 0, self.output)
        self.assertEqual(self.status("make"), "accepted")
        self.assertFalse(os.path.exists(os.path.join(self.root, "src/notes.txt")))
        self.check_invariants()

    def test_no_pause_expectation_adopts_nothing(self):
        """With no pause expectation recorded, `retry` neither adopts nor refuses on its account"""
        self.workflow(WIDE)
        self.script([WORK])
        self.assertEqual(self.start(), 2)
        run = self.the_run()
        run.state["expect"] = None
        run.save()
        self.write("notes/owner.txt", "the owner's own\n")
        self.commit()
        self.assertEqual(self.runner("retry", "latest", "make", "--apply-patch", "-C", self.root),
                         0, self.output)
        self.assertIsNone(self.the_run().state["expect"])

    def refused(self, why, *flags):
        """`retry` refuses with the adoption wording and changes nothing."""
        expect = self.the_run().state["expect"]
        before = untouched_digest(self)
        self.assertEqual(self.runner("retry", "latest", "make", *flags, "-C", self.root), 2)
        if why.startswith("the work tree"):
            # The owner's own uncommitted change is adopted once committed, or put back (RUN-81).
            self.assertIn(f"'make' cannot be retried as the run stands: {why} ( M notes/owner.txt). "
                          "Nothing was changed; commit them, or put them back as they were, then "
                          "run this retry again.", self.output)
        else:
            self.assertIn(f"'make' cannot be retried as the run stands: {why}. Nothing was "
                          "changed; put the branch and the work tree back as they were, then "
                          "`runner resume`.", self.output)
        self.assertEqual(untouched_digest(self), before)
        self.assertEqual(self.the_run().state["expect"], expect)   # the expectation stands
        self.assertEqual(self.status("make"), "failed")

    def test_a_dirty_tree_is_not_adopted(self):
        """fail: an unrelated commit does not block recovery (FAIL-09: uncommitted edits refuse)"""
        self.workflow(WIDE)
        self.script([WORK])
        self.assertEqual(self.start(), 2)
        self.write("notes/owner.txt", "the owner's own\n")
        self.commit()
        self.write("notes/owner.txt", "not committed\n")
        for flags in (("--apply-patch",), ()):
            with self.subTest(flags=flags):
                self.refused("the work tree has uncommitted changes", *flags)

    def test_a_moved_branch_is_not_adopted(self):
        """fail: an unrelated commit does not block recovery (FAIL-09: a rewritten branch refuses)"""
        self.workflow(WIDE)
        self.script([WORK])
        self.assertEqual(self.start(), 2)
        paused = self.the_run().state["expect"]["tip"]
        git(self.root, "commit", "-q", "--amend", "--allow-empty", "-m", "rewritten")
        self.refused(f"the run branch is no longer a descendant of {paused[:7]}, where the run "
                     "paused")

    def test_a_runner_made_commit_is_not_adopted(self):
        """fail: an unrelated commit does not block recovery (FAIL-09: a `Run:` trailer refuses)"""
        self.workflow(WIDE)
        self.script([WORK])
        self.assertEqual(self.start(), 2)
        self.write("notes/owner.txt", "the owner's own\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "looks like the runner's\n\nRun: somewhere-else")
        made = self.git_out("rev-parse", "HEAD")
        self.write("notes/later.txt", "the owner's own\n")
        self.commit()
        self.refused(f"commit {made[:7]}, made since the run paused, carries a Run: trailer, so a "
                     "runner made it")


NOTICE = "# Earlier work of this task is already in the work tree"
BACK = {"match": NOTICE, "write": {"src/a.txt": "good\n"}, "answer": done()}


class RecoveryTransaction(EngineCase):
    """The `recover` intent: the set-aside work is put back at the start of the next transaction,
    `resume` settles a crash at every point of it from the intent, and a request that no longer
    holds stops the run with nothing restored."""
    POINTS = ["recover:before-restore", "restore:after-removals", "recover:after-restore"]
    SAVES = 5            # `open_transaction`: its own save, the pin's begin and finish, the
                         # recover intent's finish, and the last save

    def queue(self):
        """A failed producer whose set-aside work is queued to be put back."""
        self.workflow(WIDE)
        self.script([WORK])
        self.assertEqual(self.start(), 2)
        self.assertEqual(self.runner("retry", "latest", "make", "--apply-patch", "-C", self.root),
                         0, self.output)
        self.script([BACK])

    def kill_at(self, point=None, save=None):
        """Stop at `point`, or at the `save`-th state save after the work was restored."""
        seen = {"restored": False, "saves": 0}

        def hook(where):
            if where == "recover:after-restore":
                seen["restored"] = True
            if save is None and where == point:
                raise Killed(where)
            if save is not None and seen["restored"] and where == "state:before-rename":
                seen["saves"] += 1
                if seen["saves"] == save:
                    raise Killed(f"save {save}")
        self.cli.CRASH = hook

    def assert_recovered_once(self):
        self.assertEqual(self.status("make"), "accepted", self.output)
        self.assertEqual(self.calls(), 1)                           # the attempt ran once
        self.assertTrue(os.path.isdir(self.task_file("make", "attempt-2")))
        self.assertFalse(os.path.exists(self.task_file("make", "attempt-3")))
        self.assertEqual(self.prompt(1).count(NOTICE), 1)
        self.assertEqual(self.git_out("show", "HEAD:src/notes.txt"), "kept work")
        self.assertEqual(self.git_out("show", "HEAD:src/a.txt"), "good")
        st = self.the_run().state["tasks"]["make"]
        self.assertNotIn("recover", st)
        self.assertEqual((st["recovered"]["attempt"], st["recovered"]["files"]), (1, 2))
        recovered = [e for e in events(self) if e["event"] == "recovered"]
        self.assertEqual([(e["task"], e["attempt"], e["files"]) for e in recovered],
                         [("make", 1, 2)])
        self.check_invariants()

    def test_the_work_is_put_back_under_an_intent(self):
        """The recovery is intent, effect, outcome; the owner is told before the task starts"""
        self.queue()
        self.assertEqual(self.resume(), 0, self.output)
        lines = self.output.splitlines()
        self.assertIn("make: put back the set-aside work of attempt 1 (2 files)", lines)
        self.assertLess(lines.index("make: put back the set-aside work of attempt 1 (2 files)"),
                        lines.index("make: started"))
        log = events(self)
        intent = [e for e in log if e["event"] == "intent" and e["kind"] == "recover"]
        outcome = [e for e in log if e["event"] == "outcome" and e["kind"] == "recover"]
        self.assertEqual(len(intent), 1)
        self.assertEqual([(e["op"], e["restored"], e["attempt"]) for e in outcome],
                         [(intent[0]["op"], 2, 1)])
        self.assertEqual(self.the_run().state["tasks"]["make"]["recovered"]["op"], intent[0]["op"])
        self.assert_recovered_once()

    def test_set_aside_work_survives_a_replan(self):
        """fail: set-aside work survives a replan (FAIL-08)"""
        self.workflow(WIDE)
        self.script([{"write": {"src/a.txt": "bad\n", "src/notes.txt": "kept work\n"},
                      "answer": {"outcome": "blocked", "summary": "", "responses": [],
                                 "blocked_reason": "The brief contradicts the gate."}}])
        self.assertEqual(self.start(), 255)
        attempt_one = self.task_file("make", "attempt-1")
        digest = tree_digest(attempt_one)
        with open(self.task_file("make", "failed.patch"), "rb") as fh:
            patch = fh.read()
        with open(self.wf_path, encoding="utf-8") as fh:
            text = fh.read()
        self.write("wf.toml", text.replace("Make src/a.txt say good.",
                                           "Make src/a.txt say good, and keep the notes."))
        self.commit()
        self.assertEqual(self.runner("replan", "latest", "-C", self.root), 0, self.output)
        self.assertEqual(self.status("make"), "pending")
        self.assertEqual(self.runner("retry", "latest", "make", "--apply-patch", "-C", self.root),
                         0, self.output)
        self.script([BACK])
        self.assertEqual(self.resume(), 0, self.output)
        self.assertIn("make: put back the set-aside work of attempt 1 (2 files)", self.output)
        self.assertIn("keep the notes", self.prompt(1))
        self.assertEqual(tree_digest(attempt_one), digest)      # no finished attempt changed
        with open(self.task_file("make", "failed.patch"), "rb") as fh:
            self.assertEqual(fh.read(), patch)
        self.assert_recovered_once()                            # attempt 2 continues the series

    def test_the_author_is_told_about_recovered_work(self):
        """fail: the author is told about recovered work (FAIL-12)"""
        self.queue()
        self.assertEqual(self.resume(), 0, self.output)
        prompt = self.prompt(1)
        self.assertEqual(prompt.count(NOTICE), 1)
        self.assertIn("The work of attempt 1 of this task was set aside, and the runner has "
                      "now put it back into the work tree, unchanged. These files already "
                      "hold it:", prompt)
        self.assertEqual(prompt.count("<<<DATA recovered files\nsrc/a.txt\nsrc/notes.txt\n"
                                      "DATA>>>"), 1)
        self.assertIn("Continue from this work. Read these files before you change them. Do "
                      "not start over, and do not revert what is there: it is yours, from an "
                      "earlier attempt of this same task.", prompt)
        self.assertIn("<<<DATA brief\nMake src/a.txt say good.\nDATA>>>", prompt)       # unchanged
        self.assert_recovered_once()

    def test_a_template_without_findings_still_gets_the_notice(self):
        """fail: the author is told about recovered work (FAIL-12, a template without
        {findings}, and a brief that quotes the section's own heading)"""
        self.write("lib/types/notice.toml", '''
name = "notice"
kind = "produce"
requires = ["write"]
prompt = """
Task {task.id}

{task.prompt}

{outputs}
{gates}

{rules}

{result_schema}
"""
''')
        header = ('library = ["lib"]\n'
                  + self.HEADER.format(defaults="", python=sys.executable, agent=FAKE_AGENT)
                        .replace("'", '"'))
        tasks = ('[[task]]\nid = "make"\ntype = "notice"\nmax_attempts = 1\n'
                'prompt = "Make src/a.txt say good.\\n\\n' + NOTICE
                + '\\nA quote from the owner, not the runner\'s own notice."\n'
                'outputs = ["src/a.txt"]\nwrites = ["src/**"]\n'
                'gate = ["grep -q good src/a.txt"]\n')
        self.wf_path = self.write("wf.toml", header + tasks)
        self.commit()
        self.script([WORK])
        self.assertEqual(self.start(), 2)
        self.assertEqual(self.runner("retry", "latest", "make", "--apply-patch", "-C", self.root),
                         0, self.output)
        self.script([{"write": {"src/a.txt": "good\n"}, "answer": done()}])
        self.assertEqual(self.resume(), 0, self.output)
        prompt = self.prompt(1)
        self.assertNotIn("{findings}", prompt)
        # The owner's quote is data inside the brief; the runner's own section still follows it.
        self.assertEqual(prompt.count(NOTICE), 2)
        self.assertEqual(prompt.count("<<<DATA recovered files\nsrc/a.txt\nsrc/notes.txt\n"
                                      "DATA>>>"), 1)
        self.assertEqual(self.status("make"), "accepted", self.output)

    def test_recovery_is_resumable(self):
        """rec: recovery is resumable (REC-14)"""
        cases = [{"point": p} for p in self.POINTS] + [{"save": n}
                                                       for n in range(1, self.SAVES + 1)]
        for where in cases:
            with self.subTest(**where):
                self.setUp()
                self.queue()
                self.kill_at(**where)
                with self.assertRaises(Killed):
                    self.resume()
                self.cli.CRASH = None
                state = self.the_run().state
                open_intent = any(i["kind"] == "recover" for i in state["intents"])
                # Until the recover intent's own finish is on disk (the 4th save), it is open.
                self.assertEqual(open_intent, where.get("save", 0) <= 4, state["intents"])
                if where.get("save") in (2, 3, 4):      # the transaction is already on disk
                    self.assertEqual(state["active_producer"], "make")
                self.assertEqual(self.resume(), 0, self.output)
                if open_intent:
                    self.assertIn("recovery of 'make': the set-aside work of attempt 1 is back",
                                  self.output)
                self.assert_recovered_once()
                self.doCleanups()

    def test_a_half_installed_transaction_is_completed_from_the_intent(self):
        """rec: recovery is resumable (REC-14: the state already shows the transaction, while the
        request is still queued and nothing says the work is back)"""
        self.queue()
        self.kill_at("recover:after-restore")
        with self.assertRaises(Killed):
            self.resume()
        self.cli.CRASH = None
        run = self.the_run()
        run.state["active_producer"] = "make"
        run.state["tasks"]["make"].update(status="running", step="attempt", feedback=None,
                                          base=self.git_out("rev-parse", "HEAD^{tree}"))
        self.assertIn("recover", run.state["tasks"]["make"])
        self.assertNotIn("recovered", run.state["tasks"]["make"])
        run.save()
        self.assertEqual(self.resume(), 0, self.output)
        self.assert_recovered_once()

    def test_a_moved_branch_stops_an_interrupted_recovery(self):
        """rec: a moved branch stops an interrupted recovery (REC-15)"""
        self.queue()
        self.kill_at("recover:before-restore")
        with self.assertRaises(Killed):
            self.resume()
        self.cli.CRASH = None
        expected = self.git_out("rev-parse", "HEAD")
        git(self.root, "commit", "-q", "--allow-empty", "-m", "the same tree")
        found = self.git_out("rev-parse", "HEAD")
        self.assertEqual(self.git_out("rev-parse", f"{expected}^{{tree}}"),
                         self.git_out("rev-parse", "HEAD^{tree}"))
        state_before = self.the_run().state
        before = untouched_digest(self)
        self.assertEqual(self.resume(), 2)
        self.assertIn("reconciliation error: the set-aside work of 'make' was being put back on "
                      f"commit {expected}, but the branch tip is now {found}. Nothing was "
                      "restored", self.output)
        self.assertEqual(untouched_digest(self), before)
        self.assertFalse(os.path.exists(os.path.join(self.root, "src/notes.txt")))
        state = self.the_run().state
        self.assertEqual(state["intents"], state_before["intents"])     # nothing settled
        self.assertIsNone(state.get("active_producer"))                 # no transaction opened
        st = state["tasks"]["make"]
        self.assertEqual(st["status"], "pending")
        self.assertIn("recover", st)
        self.assertNotIn("recovered", st)
        self.assertEqual(self.calls(), 0)

    def test_an_invalid_queued_recovery_stops_the_run(self):
        """fail: an invalid queued recovery can be cancelled (FAIL-15, the refusal)"""
        self.queue()
        queued = self.the_run().state["tasks"]["make"]["recover"]
        narrow = revised_workflow(self, WIDE.replace('writes = ["src/**"]',
                                                     'writes = ["src/a.txt"]'))
        self.assertEqual(self.runner("replan", "latest", "--workflow", narrow, "-C", self.root),
                         0, self.output)
        # The request is carried across the replan (task 8 of the design makes `replan` do this).
        run = self.the_run()
        run.state["tasks"]["make"]["recover"] = queued
        run.save()
        with open(self.task_file("make", "failed.patch"), "rb") as fh:
            patch = fh.read()
        refusal = ("the set-aside work of 'make' (attempt 1) cannot be put back: it changed "
                   "src/notes.txt, which 'make' may no longer write. Nothing was changed. Retry "
                   "without --apply-patch to start clean.")
        for _ in range(2):                                      # a second resume refuses alike
            self.assertEqual(self.resume(), 2, self.output)
            self.assertIn(f"runner: {refusal}", self.output)
            self.assertNotIn("put back the set-aside work", self.output)
            self.assertFalse(os.path.exists(os.path.join(self.root, "src/notes.txt")))
            self.assertEqual(self.git_out("status", "--porcelain"), "")
            state = self.the_run().state
            st = state["tasks"]["make"]
            self.assertEqual((st["status"], st["recover"]), ("pending", queued))
            self.assertNotIn("recovered", st)
            self.assertIsNone(state.get("active_producer"))
            self.assertEqual([i for i in state["intents"] if i["kind"] == "recover"], [])
            self.assertEqual(self.calls(), 0)
            with open(self.task_file("make", "failed.patch"), "rb") as fh:
                self.assertEqual(fh.read(), patch)
        self.assertFalse([e for e in events(self) if e["event"] == "intent"
                          and e["kind"] == "recover"])

        # Cancelled, the next attempt starts clean.
        self.assertEqual(self.runner("retry", "latest", "make", "-C", self.root), 0, self.output)
        self.script([{"write": {"src/a.txt": "good\n"}, "answer": done()}])
        self.assertEqual(self.resume(), 0, self.output)
        self.assertNotIn(NOTICE, self.prompt(1))
        self.assertFalse(os.path.exists(os.path.join(self.root, "src/notes.txt")))
        self.check_invariants()


    def test_an_interrupted_recovery_is_not_retried(self):
        """rec: recovery is resumable (REC-14: `retry` refuses while the intent is open, with or
        without the flag, and writes nothing; `resume` then finishes the recovery)"""
        self.queue()
        self.kill_at("restore:after-removals")
        with self.assertRaises(Killed):
            self.resume()
        self.cli.CRASH = None
        op = [i["op"] for i in self.the_run().state["intents"] if i["kind"] == "recover"][0]
        before = untouched_digest(self)
        for flags in ((), ("--apply-patch",)):
            with self.subTest(flags=flags):
                self.assertEqual(self.runner("retry", "latest", "make", *flags, "-C", self.root),
                                 2)
                self.assertEqual(self.output,
                                 f"runner: putting back the set-aside work of 'make' was "
                                 f"interrupted ({op}); `runner resume` finishes it first. "
                                 "Nothing was changed\n")
                self.assertEqual(untouched_digest(self), before)
        self.assertIn("recover", self.the_run().state["tasks"]["make"])
        self.assertEqual(self.resume(), 0, self.output)
        self.assert_recovered_once()

    def test_a_failed_restore_then_an_interrupted_replay_resumes(self):
        """rec: recovery is resumable (REC-14: a restore that failed, then a replay interrupted
        after it changed the tree, is still finished by the next `resume`)"""
        self.queue()

        def broken(where):
            if where == "restore:after-removals":
                raise OSError("the disk is full")
        self.cli.CRASH = broken
        self.assertEqual(self.resume(), 2, self.output)
        self.assertIn("environment failure", self.output)
        state = self.the_run().state
        self.assertIsNotNone(state["expect"])
        self.assertEqual([i["kind"] for i in state["intents"]], ["recover"])

        self.kill_at("restore:before-verify")  # the replay has changed the tree by then
        with self.assertRaises(Killed):
            self.resume()
        self.cli.CRASH = None
        self.assertTrue(os.path.exists(os.path.join(self.root, "src/notes.txt")))
        self.assertEqual([i["kind"] for i in self.the_run().state["intents"]], ["recover"])
        self.assertEqual(self.resume(), 0, self.output)
        self.assertNotIn("work tree changed", self.output)
        self.assert_recovered_once()


class Pause(EngineCase):
    TWO = ONE + '''
[[task]]
id = "more"
type = "implement"
needs = ["make"]
prompt = "Make src/b.txt say good."
outputs = ["src/b.txt"]
gate = ["grep -q good src/b.txt"]
'''
    GOOD_B = {"write": {"src/b.txt": "good\n"}, "answer": done()}

    def test_a_requested_pause_stops_before_the_next_call(self):
        """pause: at a safe point (PAUSE-01): nothing in flight is lost, resume continues"""
        self.workflow(self.TWO)
        self.script([GOOD, self.GOOD_B])
        # The request is already there when the second task would start its call.
        import codesmith.engine as eng
        original = eng.Engine.call_agent

        def call_agent(engine, agent, task, *args, **kw):
            if task["id"] == "more":
                engine.run.request_pause()
            return original(engine, agent, task, *args, **kw)
        eng.Engine.call_agent = call_agent
        self.addCleanup(setattr, eng.Engine, "call_agent", original)
        self.assertEqual(self.start(), 2)
        self.assertIn("paused at the owner's request before the next call of 'more'", self.output)
        self.assertNotIn("--add-budget", self.output)
        run = self.the_run()
        self.assertEqual(run.state["status"], "stopped")
        self.assertEqual(self.status("make"), "accepted")
        self.assertEqual(self.status("more"), "running")           # its transaction is open, no call yet
        self.assertEqual(self.calls(), 1)
        self.assertFalse(run.pause_requested())                    # cleared, so resume does not re-pause
        self.assertEqual(run.state["intents"], [])                 # nothing to reconcile
        with open(os.path.join(run.path, "STATUS.md"), encoding="utf-8") as fh:
            status = fh.read()
        self.assertIn("The run stopped: paused at the owner's request", status)
        self.assertNotIn("--add-budget", status)
        eng.Engine.call_agent = original
        self.assertEqual(self.resume(), 0)
        self.assertEqual(self.status("more"), "accepted")
        self.assertEqual(self.calls(), 2)
        self.check_invariants()

    def test_pause_with_no_runner_is_a_no_op(self):
        self.workflow(ONE + '[[task]]\nid = "look"\ntype = "human"\nverifies = "make"\n')
        self.script([GOOD])
        self.assertEqual(self.start(), 255)
        self.assertEqual(self.runner("pause", "-C", self.root), 0)
        self.assertIn("no runner is working on it, nothing to pause", self.output)
        self.assertFalse(self.the_run().pause_requested())

    def test_pause_now_interrupts_a_live_runner_by_its_lock(self):
        """pause: --now (PAUSE-02): the runner named in the lock is stopped, the call is lost"""
        self.workflow(ONE)
        self.script([{"sleep_s": 600, **GOOD}, GOOD])
        runner = subprocess.Popen([sys.executable, RUNNER, "start", self.wf_path], cwd=self.root,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: runner.poll() is None and runner.kill())
        import time
        from codesmith import record
        lock = record.Lock(os.path.join(self.root, ".runs"))
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            holder = lock.holder()
            if holder and record.is_alive(holder.get("process")) and self.calls() == 1:
                break
            time.sleep(0.05)
        else:
            self.fail("the runner did not start its agent call")
        self.assertEqual(self.runner("pause", "--now", "-C", self.root), 0)
        self.assertIn("interrupted", self.output)
        self.assertIn("Continue with: runner resume", self.output)
        _out, err = runner.communicate(timeout=15)
        self.assertEqual(runner.returncode, 2, err)
        self.assertIn("interrupted; child processes stopped", err)
        self.assertIsNone(lock.holder())                           # released
        status_md = os.path.join(self.the_run().path, "STATUS.md")
        with open(status_md, encoding="utf-8") as fh:
            self.assertIn("1 operation(s) were interrupted", fh.read())   # the lost call, until reconciled
        self.assertEqual(self.resume(), 0)                         # reconciles the lost call, runs again
        with open(status_md, encoding="utf-8") as fh:
            self.assertNotIn("were interrupted", fh.read())        # and the page says so at once
        self.assertEqual(self.status("make"), "accepted")
        self.assertEqual(self.calls(), 2)
        self.check_invariants()


if __name__ == "__main__":
    unittest.main()
