"""Response-only producer repair, using scripted command and Claude CLIs."""
import json
import os
from pathlib import Path
import sys
import unittest

from helpers import EngineCase, done
from codesmith import agents, proc, qualification, record, workflow
from test_findings import finding, resolution, review

ONE = '''
[[task]]
id = "make"
type = "implement"
prompt = "BRIEF-MARKER: Make src/a.txt say good."
outputs = ["src/a.txt"]
max_attempts = 1
gate = ["grep -q good src/a.txt"]
'''
BAD_ANSWER = {'write': {'src/a.txt': 'good\n'}, 'answer': done('x' * 5000)}
FIXED = {'answer': done()}


class Killed(BaseException):
    pass


class ResponseRepair(EngineCase):
    HEADER = EngineCase.HEADER + 'read_only_args = ["--read-only"]\n'

    def claude(self):
        fake = str(Path(__file__).with_name('fake_claude.py'))
        self.HEADER = ('name="demo"\n[defaults]\nagent="fake"\n{defaults}\n'
                       '[agents.fake]\nkind="claude"\n'
                       'argv=[' + json.dumps(sys.executable) + ', ' + json.dumps(fake) + ']\n')

    def qualify_readonly(self):
        wf = workflow.load(self.wf_path)
        qualification.check_workflow(wf)
        key, meta = qualification.fingerprint(wf.agents['fake'], '', True, wf.root)
        entry = qualification.qualify('fake', meta, os.path.join(self.side, 'readonly'))
        cache_path = os.path.join(self.root, '.runs', 'qualification-cache.json')
        cache = record.read_json(cache_path)
        cache['entries'][key] = entry
        record.write_durable(cache_path, record.dump_json(cache))

    def invocation(self, n, name, attempt=1):
        return self.read_json('make', f'attempt-{attempt}', f'invocation-{n}', name)

    def assert_repair(self, attempt=1, invocation=2):
        prompt = Path(self.task_file('make', f'attempt-{attempt}',
                                     f'invocation-{invocation}', 'prompt.md')).read_text()
        self.assertNotIn('BRIEF-MARKER', prompt)
        self.assertIn('change no files', prompt)
        self.assertIn('original answer', prompt)
        self.assertIn('validation diagnostic', prompt)
        self.assertLess(len(prompt.encode()), 9000)
        evidence = self.invocation(invocation, 'repair.json', attempt)
        self.assertEqual(evidence['before'], evidence['after'])
        self.assertEqual(evidence['edited_files'], [])
        self.assertEqual(self.git_out('rev-parse', 'HEAD^{tree}'), evidence['after'])

    def test_completed_claude_answer_resumes_only_to_repair(self):
        """acc: a completed author resumes only to repair its answer (ACC-39, BUD-13)"""
        self.claude()
        self.workflow(ONE)
        self.script([BAD_ANSWER, FIXED])
        self.assertEqual(self.start(), 0, self.output)
        argv = self.invocation(2, 'argv.json')
        self.assertEqual(argv['session_id'], 'scripted-session')
        self.assertIn('--resume', argv['argv'])
        self.assertFalse(argv['read_only'])
        cost = self.invocation(2, 'outcome.json')
        self.assertAlmostEqual(cost['cost_usd'], 0.1)
        self.assertAlmostEqual(cost['reported_cost_usd'], 0.3)
        self.assertEqual(self.calls(), 2)
        self.assertEqual(self.the_run().state['tasks']['make']['attempts_used'], 1)
        self.assert_repair()
        self.check_invariants()

    def test_nonresumable_uses_qualified_readonly(self):
        """acc: a nonresumable author repairs under qualified read-only mode (ACC-40)"""
        self.workflow(ONE)
        self.qualify_readonly()
        self.script([BAD_ANSWER, FIXED])
        self.assertEqual(self.start(), 0, self.output)
        argv = self.invocation(2, 'argv.json')
        self.assertTrue(argv['read_only'])
        self.assertIn('--read-only', argv['argv'])
        self.assertIsNone(argv['session_id'])
        self.assertEqual(self.the_run().state['tasks']['make']['attempts_used'], 1)
        self.assert_repair()
        self.check_invariants()

    def test_unqualified_readonly_flags_do_not_enable_repair(self):
        """acc: without either qualification a retry still gets the full prompt (ACC-41)"""
        self.workflow(ONE)
        self.script([BAD_ANSWER, FIXED])
        self.assertEqual(self.start(), 0, self.output)
        self.assertIn('BRIEF-MARKER', self.prompt(2))
        self.assertIn('validation diagnostic', self.prompt(2))
        self.assertFalse(self.invocation(2, 'argv.json')['read_only'])
        self.assertEqual(self.the_run().state['tasks']['make']['attempts_used'], 1)
        self.check_invariants()

    def test_writer_repair_edits_are_judged_as_authoring(self):
        """acc: a write-capable repair's edits are recorded and judged (ACC-42)"""
        self.claude()
        self.workflow(ONE)
        self.script([BAD_ANSWER, {'write': {'src/a.txt': 'bad\n'}, 'answer': done()}])
        self.assertEqual(self.start(), 2, self.output)
        self.assertEqual(self.status('make'), 'failed')
        evidence = self.invocation(2, 'repair.json')
        self.assertNotEqual(evidence['before'], evidence['after'])
        self.assertEqual(evidence['edited_files'], ['src/a.txt'])
        self.assertTrue(evidence['ordinary_authoring'])
        self.assertIn('repair-edited-files', Path(self.the_run().path, 'events.jsonl').read_text())
        self.assertEqual(self.read_json('make', 'attempt-1', 'verification.json')['candidate'], evidence['after'])
        self.assertEqual(self.calls(), 2)
        self.check_invariants()

    def test_readonly_repair_write_fails_and_restores(self):
        """acc: a read-only repair that writes is restored and fails (ACC-43)"""
        self.workflow(ONE)
        self.qualify_readonly()
        self.script([BAD_ANSWER, {'write': {'src/a.txt': 'bad\n'}, 'answer': done()}])
        self.assertEqual(self.start(), 2, self.output)
        state = self.the_run().state['tasks']['make']
        self.assertIn('read-only response repair changed', state['reason'])
        self.assertEqual(self.invocation(2, 'repair.json')['edited_files'], ['src/a.txt'])
        patch = Path(self.task_file('make', 'failed.patch')).read_text()
        self.assertIn('+good', patch)
        self.assertNotIn('+bad', patch)
        self.check_invariants()

    def test_repairs_are_bounded_by_protocol_allowance(self):
        """acc: repeated invalid repairs exhaust only the protocol allowance (ACC-44)"""
        self.claude()
        self.workflow(ONE)
        self.script([BAD_ANSWER, {'answer': done('y' * 5000)}, {'answer': done('z' * 5000)}])
        self.assertEqual(self.start(), 2, self.output)
        self.assertEqual(self.calls(), 3)
        self.assertEqual(self.the_run().state['tasks']['make']['attempts_used'], 1)
        self.assertNotIn('BRIEF-MARKER', self.prompt(3))
        self.assertIn('y' * 5000, self.prompt(3))
        self.assertEqual(self.invocation(3, 'argv.json')['session_id'], 'scripted-session')
        self.check_invariants()

    def test_schema_and_non_json_answers_can_be_repaired(self):
        """acc: completed prose and malformed schemas get response-only repair (ACC-45)"""
        self.claude()
        self.workflow(ONE)
        self.script([{'write': {'src/a.txt': 'good\n'}, 'raw_answer': 'Finished the file.'},
                     {'answer': {'outcome': 'done'}}, FIXED])
        self.assertEqual(self.start(), 0, self.output)
        self.assertIn('Finished the file.', self.prompt(2))
        self.assertNotIn('BRIEF-MARKER', self.prompt(3))
        self.assertTrue(self.invocation(1, 'outcome.json')['completed'])
        self.assert_repair(invocation=3)
        self.check_invariants()

    def test_missing_terminal_event_keeps_authoring_retry(self):
        """acc: an incomplete call still retries the full authoring prompt (ACC-46)"""
        self.claude()
        self.workflow(ONE)
        self.script([{'write': {'src/a.txt': 'good\n'}, 'raw_answer': 'NO_TERMINAL'}, FIXED])
        self.assertEqual(self.start(), 0, self.output)
        self.assertFalse(self.invocation(1, 'outcome.json')['completed'])
        self.assertIn('BRIEF-MARKER', self.prompt(2))
        self.assertIsNone(self.invocation(2, 'argv.json')['session_id'])
        self.assertEqual(self.the_run().state['tasks']['make']['attempts_used'], 1)
        self.check_invariants()

    def test_invented_ids_on_rework_are_repaired_against_assigned_ids(self):
        """fnd: response repair names exactly the assigned rework IDs (FND-40)"""
        self.workflow(ONE.replace('max_attempts = 1', 'max_attempts = 2\nreviewers = ["principled-priya"]'))
        rid = 'make.review.principled-priya'
        fid = 'make/PE-1'
        self.script({'make': [dict(BAD_ANSWER, answer=done()),
                     {'write': {'src/a.txt': 'very good\n'}, 'answer': done(responses=[
                         dict(finding='invented-id', action='fixed', note='Fixed')])},
                     {'answer': done(responses=[dict(finding=fid, action='fixed', note='Fixed')])}],
                     rid: [{'answer': review([finding(location='src/a.txt:1')])},
                           {'answer': review(resolutions=[resolution(fid)])}]})
        self.assertEqual(self.start(), 0, self.output)
        prompt = Path(self.task_file('make', 'attempt-2', 'invocation-2', 'prompt.md')).read_text()
        self.assertIn('exactly these assigned finding IDs, once each: ["make/PE-1"]', prompt)
        self.assertIn('invented-id', prompt)
        self.assertEqual(self.the_run().state['tasks']['make']['attempts_used'], 2)
        self.assert_repair(attempt=2)
        self.check_invariants()

    def test_kill_during_repair_resumes_repair(self):
        """rec: an interrupted repair resumes with the saved short prompt (REC-25)"""
        self.claude()
        self.workflow(ONE)
        self.script([BAD_ANSWER, FIXED])
        calls = 0
        def kill(point):
            nonlocal calls
            if point == 'agent:running':
                calls += 1
                if calls == 2:
                    raise Killed()
        self.cli.CRASH = kill
        with self.assertRaises(Killed):
            self.start()
        before = self.the_run().state['tasks']['make']['pending_repair']['candidate']
        self.cli.CRASH = None
        self.assertEqual(self.resume(), 0, self.output)
        self.assertEqual(self.invocation(2, 'outcome.json')['status'], 'interrupted')
        self.assertEqual(self.invocation(3, 'argv.json')['session_id'], 'scripted-session')
        self.assertEqual(self.invocation(3, 'repair.json')['before'], before)
        self.assertEqual(self.read_json('make', 'attempt-1', 'result.json')['agent_calls'], 3)
        self.assertEqual(self.the_run().state['tasks']['make']['attempts_used'], 1)
        self.assert_repair(invocation=3)
        self.check_invariants()

    def test_settled_answer_survives_kill_before_validation(self):
        """rec: settled answers are replayed without calling or charging again (REC-26)"""
        self.claude()
        self.workflow(ONE)
        self.script([BAD_ANSWER, FIXED])
        def kill(point):
            if point == 'provider:outcome-recorded':
                raise Killed()
        self.cli.CRASH = kill
        with self.assertRaises(Killed):
            self.start()
        # First resume repairs the rejected answer, then stops after that call is settled too.
        with self.assertRaises(Killed):
            self.resume()
        spend = self.the_run().state['spend']['known_usd']
        self.cli.CRASH = None
        self.assertEqual(self.resume(), 0, self.output)
        self.assertEqual(self.calls(), 2)
        self.assertEqual(self.the_run().state['spend']['known_usd'], spend)
        self.assert_repair()
        self.check_invariants()

    def test_readonly_write_is_detected_after_interruption(self):
        """rec: a repair's read-only violation survives interruption and restore (REC-27)"""
        self.workflow(ONE)
        self.qualify_readonly()
        self.script([BAD_ANSWER, {'write': {'src/a.txt': 'bad\n'}, 'answer': done()}])
        calls = 0
        def kill(point):
            nonlocal calls
            if point == 'provider:outcome-recorded':
                calls += 1
                if calls == 2:
                    raise Killed()
        self.cli.CRASH = kill
        with self.assertRaises(Killed):
            self.start()
        def kill_restore(point):
            if point == 'restore:after-removals':
                raise Killed()
        self.cli.CRASH = kill_restore
        with self.assertRaises(Killed):
            self.resume()
        self.cli.CRASH = None
        self.assertEqual(self.resume(), 2, self.output)
        self.assertEqual(self.calls(), 2)
        self.assertIn('read-only response repair changed', self.the_run().state['tasks']['make']['reason'])
        self.assertEqual(self.invocation(2, 'repair.json')['edited_files'], ['src/a.txt'])
        patch = Path(self.task_file('make', 'failed.patch')).read_text()
        self.assertIn('+good', patch)
        self.assertNotIn('+bad', patch)
        self.check_invariants()


class Completion(unittest.TestCase):
    def test_only_trustworthy_completion_enables_repair(self):
        """agent: answer validation is distinct from missing completion (AGENT-17)"""
        res = proc.ProcResult('exited', 0, b'', b'', 0, None)
        claude = agents.make('c', {'kind': 'claude'})
        terminal = {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'prose',
                    'usage': {}, 'total_cost_usd': 0}
        res.stdout_tail = json.dumps(terminal).encode()
        self.assertTrue(claude.interpret(res).completed)
        res.stdout_tail = b'{}'
        self.assertFalse(claude.interpret(res).completed)
        for parser_type, message, terminal in [
            (agents.CodexEvents, {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'prose'}},
             {'type': 'turn.completed', 'usage': {'input_tokens': 1, 'output_tokens': 1}}),
            (agents.CopilotEvents, {'type': 'assistant.message', 'data': {'content': 'prose'}},
             {'type': 'result', 'exitCode': 0})]:
            with self.subTest(parser=parser_type):
                parser = parser_type()
                parser.feed((json.dumps(message) + '\n').encode())
                self.assertFalse(parser.result(res).completed)
                parser.feed((json.dumps(terminal) + '\n').encode())
                self.assertTrue(parser.result(res).completed)
                res.returncode = 1
                self.assertFalse(parser.result(res).completed)
                res.returncode = 0
        self.assertFalse(agents.AgentResult(**{'status': 'protocol-error'}).completed)


if __name__ == '__main__':
    unittest.main()
