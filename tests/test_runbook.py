"""RUN-19: the commands in the runbook's own recovery rows, read out of `docs/runbook.md` and run
against a scratch repository, so the table can never quietly drift from what the commands do (R5,
G1). This is the only test file that reads the runbook's text; every other scenario drives the
engine and the CLI directly."""

import os
import re

from helpers import EngineCase, done

HERE = os.path.dirname(os.path.abspath(__file__))
RUNBOOK = os.path.normpath(os.path.join(HERE, "..", "docs", "runbook.md"))


def _row(text, first_cell_prefix):
    """The `Do` cell of the table row in `text` whose first cell starts with `first_cell_prefix`."""
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|") or line.startswith("|---"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) >= 2 and cells[0].startswith(first_cell_prefix):
            return cells[1]
    raise AssertionError(f"no runbook row starts with {first_cell_prefix!r}")


def _sequences(cell, task):
    """The apply-patch and the clean command sequence a row's cell spells out, in the order they
    appear, with RUN and TASK filled in. A cell may name both a `retry --apply-patch` and a plain
    `retry`; each sequence keeps whichever applies and every other command, in order."""
    commands = [span.split() for span in re.findall(r"`runner ([^`]+)`", cell)]

    def fill(argv):
        return ["latest" if a == "RUN" else task if a == "TASK" else a for a in argv]

    def is_retry(argv):
        return argv and argv[0] == "retry"

    apply_seq = [fill(a) for a in commands if not (is_retry(a) and "--apply-patch" not in a)]
    clean_seq = [fill(a) for a in commands if not (is_retry(a) and "--apply-patch" in a)]
    return apply_seq, clean_seq


with open(RUNBOOK, encoding="utf-8") as _fh:
    _TEXT = _fh.read()

BLOCKED_ROW = _row(_TEXT, "TASK is blocked: the agent said")
FAILED_ROW = _row(_TEXT, "A task failed")
DELETED_ROW = _row(_TEXT, "The run record was deleted")

WORKFLOW = '''
[[task]]
id = "make"
type = "implement"
prompt = "Make src/a.txt say good."
outputs = ["src/a.txt"]
writes = ["src/**"]
max_attempts = 1
gate = ["grep -q good src/a.txt"]
'''

SET_ASIDE = {"write": {"src/a.txt": "bad\n", "src/notes.txt": "kept work\n"}}


def _finish_step(capture_dir):
    """The second attempt's step: before touching anything, copy whatever `src/` holds at the
    moment this author is invoked into `capture_dir`, so the test can check what was in the tree
    *before* this step's own fix — recovered work or not — rather than only the final result,
    which a missing or a wrongly-applied recovery could also produce (a `write` step would
    overwrite the evidence before the test ever saw it)."""
    return {"run": [f'mkdir -p "{capture_dir}"',
                    f'cp -a src "{capture_dir}/src" 2>/dev/null || true',
                    "mkdir -p src", "printf 'good\\n' > src/a.txt"],
            "answer": done()}


class RunbookRecoveryRows(EngineCase):
    """run: the runbook's recovery rows are executable (RUN-19)"""

    def setUp(self):
        super().setUp()
        self.capture_dir = os.path.join(self.side, "before-attempt-2")

    def _run(self, seq):
        for argv in seq:
            code = self.runner(*argv, "-C", self.root)
            self.assertEqual(code, 0, self.output)

    def _captured(self, rel):
        """A file's content as it was in the tree the moment attempt 2 was invoked, or None if it
        did not exist there. `mkdir -p "{capture_dir}"` in `_finish_step` guarantees the directory
        itself exists by then, whether or not `src/` did."""
        path = os.path.join(self.capture_dir, "src", rel)
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def _assert_recovered_before_the_next_attempt(self):
        """The tree at the second author's entry was the accepted tree (empty: nothing of this
        task had ever been accepted) plus every path the set-aside work touched, restored
        unchanged — proving the recovery happened *before* the attempt, not that the attempt
        merely produced an acceptable result some other way."""
        self.assertEqual(self._captured("a.txt"), "bad\n")
        self.assertEqual(self._captured("notes.txt"), "kept work\n")

    def _assert_clean_before_the_next_attempt(self):
        """The tree at the second author's entry was the accepted tree alone: no set-aside path
        was put back."""
        self.assertIsNone(self._captured("a.txt"))
        self.assertIsNone(self._captured("notes.txt"))

    def _notes(self):
        path = os.path.join(self.root, "src", "notes.txt")
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def _blocked(self):
        self.workflow(WORKFLOW)
        self.script([dict(SET_ASIDE, answer={"outcome": "blocked", "summary": "",
                                             "responses": [], "blocked_reason": "The brief "
                                             "contradicts the gate."}),
                     _finish_step(self.capture_dir)])
        self.assertEqual(self.start(), 255)
        self.assertEqual(self.status("make"), "blocked")

    def _edit_brief(self, replacement):
        with open(self.wf_path, encoding="utf-8") as fh:
            text = fh.read()
        self.write("wf.toml", text.replace("Make src/a.txt say good.", replacement))
        self.commit()

    def test_the_blocked_task_row_with_apply_patch(self):
        """run: the runbook's recovery rows are executable (RUN-19, blocked task, --apply-patch)"""
        self._blocked()
        self._edit_brief("Make src/a.txt say good, and keep the notes.")
        apply_seq, _ = _sequences(BLOCKED_ROW, "make")
        self.assertGreaterEqual(len(apply_seq), 3)
        self._run(apply_seq)
        self._assert_recovered_before_the_next_attempt()
        self.assertEqual(self.status("make"), "accepted")
        self.assertEqual(self._notes(), "kept work\n")
        self.check_invariants()

    def test_the_blocked_task_row_clean(self):
        """run: the runbook's recovery rows are executable (RUN-19, blocked task, clean)"""
        self._blocked()
        self._edit_brief("Make src/a.txt say good, cleanly.")
        _, clean_seq = _sequences(BLOCKED_ROW, "make")
        self.assertGreaterEqual(len(clean_seq), 3)
        self._run(clean_seq)
        self._assert_clean_before_the_next_attempt()
        self.assertEqual(self.status("make"), "accepted")
        self.assertIsNone(self._notes())
        self.check_invariants()

    def _failed(self):
        self.workflow(WORKFLOW)
        self.script([dict(SET_ASIDE, answer=done()), _finish_step(self.capture_dir)])
        self.assertEqual(self.start(), 2)
        self.assertEqual(self.status("make"), "failed")

    def test_the_failed_task_row_with_apply_patch(self):
        """run: the runbook's recovery rows are executable (RUN-19, failed task, --apply-patch)"""
        self._failed()
        apply_seq, _ = _sequences(FAILED_ROW, "make")
        self.assertGreaterEqual(len(apply_seq), 2)
        self._run(apply_seq)
        self._assert_recovered_before_the_next_attempt()
        self.assertEqual(self.status("make"), "accepted")
        self.assertEqual(self._notes(), "kept work\n")
        self.check_invariants()

    def test_the_failed_task_row_clean(self):
        """run: the runbook's recovery rows are executable (RUN-19, failed task, clean)"""
        self._failed()
        _, clean_seq = _sequences(FAILED_ROW, "make")
        self.assertGreaterEqual(len(clean_seq), 2)
        self._run(clean_seq)
        self._assert_clean_before_the_next_attempt()
        self.assertEqual(self.status("make"), "accepted")
        self.assertIsNone(self._notes())
        self.check_invariants()

    def test_the_deleted_record_row(self):
        """run: the runbook's recovery rows are executable (RUN-19, the record deleted by
        `git clean -fdx`, GIT-25)"""
        self.workflow(WORKFLOW)
        self.script([{"run": ["git clean -fdxq"], "answer": done()},
                     {"write": {"src/a.txt": "good\n"}, "answer": done()}])
        self.assertEqual(self.start(), 2)
        self.assertIn("the run record was removed while the runner worked", self.output)
        _, seq = _sequences(DELETED_ROW, "make")
        self.assertEqual([a[0] for a in seq], ["repair-record", "resume"])
        self._run(seq)
        self.assertEqual(self.status("make"), "accepted")
        self.check_invariants()
