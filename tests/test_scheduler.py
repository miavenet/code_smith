"""Scheduling: one producer owns the tree until acceptance or set-aside."""
import unittest
from helpers import EngineCase, done

ONE = '''
[[task]]
id = "make"
type = "implement"
prompt = "Make it."
outputs = ["src/a.txt"]
gate = ["true"]
'''
OTHER = '''
[[task]]
id = "other"
type = "implement"
prompt = "Independent."
outputs = ["other.txt"]
gate = ["test -f src/a.txt"]
'''
GOOD = {"write": {"src/a.txt": "good\n"}, "answer": done()}

class Scheduling(EngineCase):
    def test_human_verification_holds_the_tree(self):
        """SCH-09, SCH-10: no other producer starts while approval holds a candidate."""
        self.workflow(ONE + '[[task]]\nid = "look"\ntype = "human"\nverifies = "make"\n' + OTHER)
        self.script([GOOD, {"write": {"other.txt": "other\n"}, "answer": done()}])
        self.assertEqual(self.start(), 255, self.output)
        self.assertEqual(self.calls(), 1)
        self.assertEqual(self.status("other"), "pending")
        self.check_invariants()
        self.assertEqual(self.runner("approve", "latest", "look", "-C", self.root), 0)
        self.assertEqual(self.resume(), 0, self.output)
        self.assertEqual(self.calls(), 2)
        self.check_invariants()

    def test_writing_checks_are_rolled_back(self):
        """SCH-11: a standalone writer cannot leak changes into the next producer."""
        self.workflow('[[task]]\nid = "check"\ntype = "check"\nrun = ["echo bad > README.md"]\n' + ONE)
        self.script([GOOD])
        self.assertEqual(self.start(), 2, self.output)
        self.assertEqual(self.status("check"), "failed")
        self.assertEqual(self.status("make"), "accepted")
        self.assertEqual(self.git_out("show", "HEAD:README.md"), "scratch")
        self.check_invariants()

    def test_retry_respects_the_open_transaction(self):
        """SCH-13: retry cannot disturb another producer waiting for a person."""
        self.workflow('[[task]]\nid = "failed"\ntype = "check"\nrun = ["false"]\n' + ONE +
                      '[[task]]\nid = "look"\ntype = "human"\nverifies = "make"\n')
        self.script([GOOD])
        self.assertEqual(self.start(), 255)
        self.assertEqual(self.runner("retry", "latest", "failed", "-C", self.root), 2)
        self.assertIn("holds the work tree", self.output)
        self.assertEqual(self.status("failed"), "failed")
        self.check_invariants()

    def test_verifier_prerequisites_run_before_the_transaction(self):
        self.workflow(ONE + '''
[[task]]
id = "check"
type = "check"
verifies = "make"
needs = ["prepare"]
run = ["test -f prepared.txt"]
[[task]]
id = "prepare"
type = "implement"
prompt = "Prepare first."
outputs = ["prepared.txt"]
gate = ["true"]
''')
        self.script([{"match": "Prepare first", "write": {"prepared.txt": "ready\n"}, "answer": done()}, GOOD])
        self.assertEqual(self.start(), 0, self.output)
        self.assertEqual(self.calls(), 2)
        self.check_invariants()

    def test_failed_verifier_prerequisite_skips_the_producer(self):
        self.workflow(ONE + '[[task]]\nid = "check"\ntype = "check"\nverifies = "make"\n'
                      'needs = ["prepare"]\nrun = ["true"]\n'
                      '[[task]]\nid = "prepare"\ntype = "check"\nrun = ["false"]\n')
        self.script([GOOD])
        self.assertEqual(self.start(), 2, self.output)
        self.assertEqual(self.status("make"), "skipped")
        self.assertEqual(self.calls(), 0)
        self.check_invariants()

    def test_a_task_after_an_objection_to_a_failed_producer_is_skipped(self):
        """B7: a check that objected to a producer which then failed will never be accepted, so
        a task that needs it is skipped with a reason, not left pending."""
        self.workflow(ONE.replace("gate =", "max_attempts = 1\ngate =") +
                      '[[task]]\nid = "words"\ntype = "check"\nverifies = "make"\n'
                      'read_only = true\nrun = ["false"]\n'
                      '[[task]]\nid = "after"\ntype = "check"\nneeds = ["words"]\nrun = ["true"]\n')
        self.script([GOOD])
        self.assertEqual(self.start(), 2, self.output)
        self.assertEqual((self.status("make"), self.status("words"), self.status("after")),
                         ("failed", "objected", "skipped"))
        self.assertEqual(self.the_run().state["tasks"]["after"]["reason"],
                         "'words' is objected and 'make' is failed")
        self.check_invariants()

if __name__ == "__main__":
    unittest.main()
