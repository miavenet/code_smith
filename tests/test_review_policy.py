"""Review policy, owner provenance and historical advisories; no model calls."""
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib
import unittest
from unittest import mock

from helpers import EngineCase, done
from codesmith import findings, prompts, validate, workflow
from test_findings import R, finding, resolution, review
from test_prompts import PERSONA, review_prompt, shipped

ROOT = Path(__file__).resolve().parents[1]
ONE = '''
[[task]]
id="make"
type="implement"
prompt="Make the work"
outputs=["src/a"]
gate=["test -f src/a"]
reviewers=["principled-priya"]
'''
PASS = {'answer': review()}


class Terminal(io.StringIO):
    """Standard input that says it is a terminal."""
    def isatty(self):
        return True


class Policy(unittest.TestCase):
    def test_absent_owner_rule(self):
        """prm: one absent-owner rule separates judgement from explicit contracts (PRM-11)"""
        rule = ("A directly checkable violation of a requirement the brief or the project rules state, "
                "quoted in the finding's detail, is blocking when its owner is absent; "
                'specialist judgement without such a quote is advisory. Say which it is.')
        for advisory in (False, True):
            text = review_prompt(shipped('code-review'), persona=PERSONA, advisory=advisory)
            self.assertEqual(' '.join(text.split()).count(rule), 2)
            self.assertIn('do not duplicate their blocker when they are present', text)
            if advisory:
                self.assertIn('every finding is advisory', text)
        self.assertNotIn('unless they are not on the panel', shipped('code-review'))

    def test_disputed_remedy(self):
        """prm: a kept dispute needs a shown remedy or a person ruling (PRM-12)"""
        text = ' '.join(review_prompt(shipped('code-review')).split())
        self.assertIn('a remedy you propose must be one you can show (cite where it compiles or is specified)', text)
        self.assertIn('otherwise say the reading belongs to a person and keep the finding open only for the ruling', text)
        for reason in ('the finding is false', 'the requirements conflict', 'I ask for a scope exception'):
            self.assertIn(reason, prompts.DISPUTE_RULE)

    def test_review_execution_policy(self):
        """prm: review inspection is available but execution is unauthorised (PRM-13)"""
        text = ' '.join(review_prompt(shipped('code-review')).split())
        self.assertIn('Repository inspection is available.', text)
        self.assertIn('Building, testing or running the product is not authorised during a review', text)
        self.assertIn('the supplied gate results are the execution evidence', text)
        self.assertNotIn('You cannot run or write a test', text)

    def test_reach_and_oracle(self):
        """prm: Mira and the fixed check require reach and oracle evidence (PRM-14)"""
        with (ROOT / 'library/personas/meticulous-mira.toml').open('rb') as fh:
            mira = tomllib.load(fh)
        for text in (prompts.persona_text(mira, False), shipped('code-review')):
            text = ' '.join(text.split())
            self.assertIn('what establishes that the state occurred, and what happens if that establishment fails', text)
            self.assertIn('Require separate reach and oracle evidence', text)

    def test_ruling_caps_and_legacy(self):
        """prm: bounded owner notes keep task candidate attribution and record pointers (PRM-15)"""
        item = dict(id='up', type='implement', title='Upstream', files=['src/up'], summary='author claim',
                    accepted_candidate='ACCEPTED', rulings_record='tasks/010-up/findings.json',
                    owner_rulings=[dict(finding='up/PE-1', candidate='RULED', decision='advisory',
                                       note='界' * 10000)])
        for template in ('{target} {inputs}', '{target}'):
            text = review_prompt(template, inputs=[item])
            self.assertIn('ACCEPTED', text)
            self.assertIn('RULED', text)
            self.assertIn('up/PE-1', text)
            self.assertIn('tasks/010-up/findings.json', text)
            self.assertIn('truncated; read the full record', text)
            self.assertLess(len(text.encode()), 4000)
        self.assertEqual(len(item['owner_rulings'][0]['note']), 10000)
        legacy = {k: item[k] for k in ('id', 'type', 'title', 'files', 'summary')}
        self.assertNotIn('Owner rulings', prompts.inputs_text([legacy], 20000))

    def test_advisory_reports_are_only_history(self):
        """fnd: advisory reports cannot settle blockers and replay adds no duplicate (FND-43)"""
        ledger, _, _ = findings.apply_review(findings.empty('make'), R,
                                            review([finding(), finding(severity='advisory')]), 'C1', {})
        before = copy.deepcopy(ledger)
        answer = dict(done(), advisories_addressed=[{'finding': fid, 'note': 'addressed'}
                      for fid in ('make/PE-1', 'make/PE-2', 'unknown')])
        reported = findings.note_advisories(ledger, answer, 2, 'C2')
        self.assertEqual(ledger, before)
        self.assertEqual(findings.blockers(reported), findings.blockers(ledger))
        self.assertEqual(reported['findings'][1]['status'], 'noted')
        self.assertEqual(findings.note_advisories(reported, answer, 2, 'C2'), reported)
        self.assertEqual(findings.note_advisories(ledger, done(), 2, 'C2'), ledger)
        text = prompts.feedback_text(findings.feedback(ledger), 60000)
        self.assertIn('attempt unknown, candidate C1', text)


class Delivery(EngineCase):
    HEADER = EngineCase.HEADER + 'read_only_args = ["--read-only"]\n'

    def count(self, tid):
        path = Path(self.script_path + '.' + tid + '.counter')
        return int(path.read_text()) if path.exists() else 0

    def test_owner_rulings_travel(self):
        """fnd: accepted owner rulings reach the dependent author and reviewer (FND-41)"""
        self.workflow(ONE + '''
[[task]]
id="down"
type="implement"
needs=["make"]
prompt="Use the upstream work"
outputs=["src/down"]
gate=["test -f src/down"]
reviewers=["principled-priya"]
''')
        rid, downstream = 'make.review.principled-priya', 'down.review.principled-priya'
        dispute = dict(finding='make/PE-1', action='disputed', note='I ask for a scope exception')
        summary = 'Author claims no implementation can serve it'
        self.script({'make': [{'write': {'src/a': 'one\n'}, 'answer': done()},
                              {'answer': done(summary, responses=[dispute])}],
                     rid: [{'answer': review([finding()])},
                           {'answer': review(resolutions=[resolution(status='unresolved')])}],
                     'down': [{'write': {'src/down': 'down\n'}, 'answer': done()}], downstream: [PASS]})
        self.assertEqual(self.start(), 255, self.output)
        candidate = self.the_run().state['tasks']['make']['candidate']
        note = 'Owner accepts the narrower scope, not the impossibility claim'
        self.assertEqual(self.runner('resolve', 'latest', 'make/PE-1', '--as', 'advisory',
                                     '--note', note, '-C', self.root), 0, self.output)
        self.assertEqual(self.resume(), 0, self.output)
        for path in (Path(self.task_file('down', 'attempt-1', 'prompt.md')),
                     Path(self.task_file(downstream, 'round-1', 'prompt.md'))):
            text = path.read_text()
            for fragment in (summary, note, candidate, 'make/PE-1', 'Owner rulings for task make',
                             'not the author', '/findings.json'):
                self.assertIn(fragment, text)
        run = self.the_run()
        for path in (Path(run.path) / 'STATUS.md', Path(self.task_file('make', 'STATUS.md'))):
            text = path.read_text()
            for fragment in ('accepted with owner rulings', 'make/PE-1', note, candidate):
                self.assertIn(fragment, text)
            self.assertIn('outputs.json)', text)
            self.assertIn('findings.json)', text)
        manifest = run.state['tasks']['make']['attempt_dir'] + '/outputs.json'
        self.assertIn(f'<a href="{manifest}">{candidate}</a>',
                      (Path(run.path) / 'STATUS.html').read_text())
        self.assertEqual([self.count(t) for t in ('make', rid, 'down', downstream)], [2, 2, 1, 1])
        self.check_invariants()

    def test_advisory_age_and_author_report(self):
        """fnd: advisory age and optional author marks survive later feedback (FND-42)"""
        self.workflow(ONE)
        rid = 'make.review.principled-priya'
        fixed = dict(finding='make/PE-1', action='fixed', note='fixed blocker')
        mark = dict(finding='make/PE-2', note='Removed the stale diagnostic')
        self.script({'make': [{'write': {'src/a': 'one\n'}, 'answer': done()},
                             {'write': {'src/a': 'two\n'},
                              'answer': dict(done(responses=[fixed]), advisories_addressed=[mark])},
                             {'write': {'src/a': 'three\n'}, 'answer': done(responses=[fixed])}],
                     rid: [{'answer': review([finding(), finding(severity='advisory')])},
                           {'answer': review(resolutions=[resolution(status='unresolved')])},
                           {'answer': review(resolutions=[resolution()])}]})
        self.assertEqual(self.start(), 0, self.output)
        ledger = self.read_json('make', 'findings.json')
        advisory = ledger['findings'][1]
        raised, addressed = advisory['history']
        self.assertEqual((raised['attempt'], addressed['attempt']), (1, 2))
        self.assertNotEqual(raised['candidate'], addressed['candidate'])
        self.assertEqual(advisory['status'], 'noted')
        first_feedback = Path(self.task_file('make', 'attempt-2', 'feedback.md')).read_text()
        # Attempt 2 reworks the very candidate the advisory was raised on (V-03 of the v4 runs).
        self.assertIn('Raised on your previous attempt (1).', first_feedback)
        self.assertNotIn('Historically noted', first_feedback)
        self.assertIn('## Earlier advisories', Path(self.task_file('make', 'STATUS.md')).read_text())
        for name in ('attempt-3/feedback.md', 'STATUS.md'):
            text = Path(self.task_file('make', name)).read_text()
            self.assertIn('Historically noted on attempt 1, candidate ' + raised['candidate'], text)
            self.assertIn('Author reports addressed on attempt 2, candidate ' + addressed['candidate'], text)
            self.assertIn('not reviewer-confirmed', text)
            self.assertIn(mark['note'], text)
        self.assertEqual([self.count(t) for t in ('make', rid)], [3, 3])
        self.check_invariants()

    def test_contract_gate_precedes_review(self):
        """acc: a literal output gate rejects bad output before review and supplies evidence (ACC-47)"""
        self.write('contract_gate.py', (ROOT / 'examples/contract_gate.py').read_text())
        self.write('tests.cpp', '#include "testing/doctest.hpp"\n')
        gate = f'{sys.executable} contract_gate.py --source tests.cpp --program {sys.executable} src/a'
        self.workflow(ONE.replace('test -f src/a', gate))
        program = ("import sys\nif sys.argv[1] in ('0', 'invalid', '10oops', '-1', '18446744073709551616'): sys.exit(2)\n"
                   "print('x' * 9000)\nprint('latency: 12.3 ns/op{suffix}')\n")
        rid = 'make.review.principled-priya'
        self.script({'make': [{'write': {'src/a': program.format(suffix=' (median)')}, 'answer': done()},
                              {'write': {'src/a': program.format(suffix='')}, 'answer': done(),
                               'match': 'expected one latency line ending in a number and ns/op'}],
                     rid: [PASS]})
        self.assertEqual(self.start(), 0, self.output)
        self.assertEqual([self.count(t) for t in ('make', rid)], [2, 1])
        text = Path(self.task_file(rid, 'round-1', 'prompt.md')).read_text()
        target = json.loads(text.split('<<<DATA target\n')[1].split('\nDATA>>>')[0])
        evidence = target['gate_evidence']
        self.assertEqual(evidence['candidate'], target['candidate'])
        self.assertEqual(evidence['attempt'], 2)
        self.assertTrue(evidence['truncated'])
        self.assertLessEqual(len(evidence['output'].encode()), 8192)
        self.assertIn('latency: 12.3 ns/op', evidence['output'])
        self.assertIn('attempt-2/gate.log', evidence['path'])
        verification = self.read_json('make', 'attempt-2', 'verification.json')
        self.assertEqual(verification['replay']['result'], 'pass')
        self.check_invariants()

    def test_example_gate_contracts(self):
        """acc: the example contract gate checks includes units and invalid arguments (ACC-48)"""
        source = self.write('tests.cpp', '#include "testing/doctest.hpp"\n')
        program = self.write('bench.py', '')
        command = [sys.executable, str(ROOT / 'examples/contract_gate.py'), '--source', source,
                   '--program', sys.executable, program]
        good = "import sys\nif sys.argv[1] in ('0', 'invalid', '10oops', '-1', '18446744073709551616'): sys.exit(2)\nprint('latency: 12.3 ns/op')\n"
        for name, include, product in (
                ('include', '// transitive include\n', good),
                ('units', '#include "testing/doctest.hpp"\n', good.replace('ns/op', 'ns')),
                ('zero', '#include "testing/doctest.hpp"\n', good.replace("('0', 'invalid', '10oops', '-1', '18446744073709551616')", "('invalid', '10oops', '-1', '18446744073709551616')")),
                ('invalid', '#include "testing/doctest.hpp"\n', good.replace("('0', 'invalid', '10oops', '-1', '18446744073709551616')", "('0', '10oops', '-1', '18446744073709551616')")),
                ('pass', '#include "testing/doctest.hpp"\n', good)):
            with self.subTest(name=name):
                Path(source).write_text(include)
                Path(program).write_text(product)
                result = subprocess.run(command, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0 if name == 'pass' else 1, result.stdout + result.stderr)


DOWN = '''
[[task]]
id="down"
type="implement"
needs=["make"]
prompt="Use the upstream work"
outputs=["src/down"]
gate=["test -f src/down"]
reviewers=["principled-priya"]
'''


class Provenance(EngineCase):
    """What the record says about an advisory's candidate and a ruling's author (REC-48, REC-49)."""
    HEADER = Delivery.HEADER
    count = Delivery.count

    def events(self, name):
        path = Path(self.the_run().path) / 'events.jsonl'
        return [e for e in map(json.loads, path.read_text().splitlines()) if e['event'] == name]

    def ruling_env(self, **env):
        """The environment of a scripted ruling: no agent session but the markers given."""
        from codesmith import cli
        for name in cli.AGENT_SESSION_MARKERS:
            os.environ.pop(name, None)
        os.environ.update(env)

    def rule(self, *extra, terminal=False):
        with mock.patch.object(sys, 'stdin', Terminal() if terminal else io.StringIO()):
            return self.runner('resolve', 'latest', 'make/PE-1', '--as', 'advisory', '--note',
                               'Owner accepts the narrower scope', '-C', self.root, *extra)

    def held(self, tasks, defaults=''):
        rid = 'make.review.principled-priya'
        dispute = dict(finding='make/PE-1', action='disputed', note='I ask for a scope exception')
        self.workflow(tasks, defaults=defaults)
        self.script({'make': [{'write': {'src/a': 'one\n'}, 'answer': done()},
                              {'answer': done('Asked for a ruling', responses=[dispute])}],
                     rid: [{'answer': review([finding()])},
                           {'answer': review(resolutions=[resolution(status='unresolved')])}],
                     'down': [{'write': {'src/down': 'down\n'}, 'answer': done()}],
                     'down.review.principled-priya': [PASS]})
        self.assertEqual(self.start(), 255, self.output)
        return self.the_run().state['tasks']['make']['candidate']

    def test_advisories_are_labelled_by_the_candidate_they_were_raised_on(self):
        """rec: advisories are labelled by the candidate they were raised on, split on the task
        page, and listed per accepted task on the run page (REC-48)"""
        self.workflow(ONE)
        rid = 'make.review.principled-priya'
        fixed = dict(finding='make/PE-1', action='fixed', note='fixed blocker')
        fresh = dict(finding(severity='advisory'), title='A comment is wrong')
        self.script({'make': [{'write': {'src/a': 'one\n'}, 'answer': done()},
                              {'write': {'src/a': 'two\n'}, 'answer': done(responses=[fixed])}],
                     rid: [{'answer': review([finding(), finding(severity='advisory')])},
                           {'answer': review([fresh], resolutions=[resolution()])}]})
        self.assertEqual(self.start(), 0, self.output)
        feedback = Path(self.task_file('make', 'attempt-2', 'feedback.md')).read_text()
        self.assertIn('make/PE-2 [advisory] Fix this\nRaised on your previous attempt (1).', feedback)
        self.assertIn('Advisories are not requests.', feedback)
        page = Path(self.task_file('make', 'STATUS.md')).read_text()
        accepted, earlier = page.index('## Advisories on the accepted candidate'), page.index('## Earlier advisories')
        self.assertLess(accepted, page.index('make/PE-3: A comment is wrong\nRaised on this candidate (attempt 2).'))
        self.assertLess(page.index('make/PE-3'), earlier)
        self.assertLess(earlier, page.index('make/PE-2: Fix this\nHistorically noted on attempt 1, candidate '))
        self.assertNotIn('Advisory history', page)
        commit = self.the_run().state['tasks']['make']['commit']
        run_page = (Path(self.the_run().path) / 'STATUS.md').read_text()
        self.assertIn(f'## Advisories on accepted work\n- make: 1 advisory on {commit[:7]} — '
                      'PE-3: A comment is wrong. See tasks/010-make/STATUS.md.', run_page)
        self.assertEqual([self.count(t) for t in ('make', rid)], [2, 2])
        self.check_invariants()

    def test_a_ruling_records_how_its_author_was_named_and_where_it_ran(self):
        """rec: a ruling records how its author was named, whether a terminal gave it and the agent
        session markers present; a demoted blocker says so; the candidate is a tree with its
        commit (REC-49)"""
        candidate = self.held(ONE + DOWN)
        self.ruling_env(USER='owner', CLAUDECODE='1')
        self.assertEqual(self.rule(), 0, self.output)
        human = self.read_json('make', 'findings.json')['findings'][0]['history'][-1]
        self.assertEqual({k: human[k] for k in ('by', 'by_source', 'interactive', 'agent_markers')},
                         {'by': 'owner', 'by_source': 'env', 'interactive': False,
                          'agent_markers': ['CLAUDECODE']})
        [event] = self.events('finding-resolved')
        self.assertEqual((event['by'], event['by_source'], event['interactive'], event['agent_markers']),
                         ('owner', 'env', False, ['CLAUDECODE']))
        self.assertEqual(self.resume(), 0, self.output)
        st = self.the_run().state['tasks']['make']
        self.assertEqual(st['candidate'], candidate)
        commit = st['commit'][:7]
        run_page = (Path(self.the_run().path) / 'STATUS.md').read_text()
        self.assertIn('make/PE-1: advisory, ruled by owner (non-interactive, name from the environment; '
                      f'agent session markers: CLAUDECODE) on candidate tree {candidate} (commit {commit}), ',
                      run_page)
        self.assertIn(f'candidate tree [{candidate}](tasks/010-make/', run_page)
        self.assertIn(f') (commit {commit}). [Findings', run_page)
        page = Path(self.task_file('make', 'STATUS.md')).read_text()
        self.assertLess(page.index('## Advisories on the accepted candidate'), page.index(
            f'make/PE-1: Fix this\nRaised blocking on attempt 1; ruled advisory by owner on candidate {candidate}.'))
        prompt = Path(self.task_file('down', 'attempt-1', 'prompt.md')).read_text()
        self.assertIn(f'accepted candidate tree {candidate} (commit {commit}) (not the author', prompt)
        self.check_invariants()

    def test_a_workflow_can_take_rulings_only_from_a_terminal_or_a_name(self):
        """rec: with rulings_require_interactive a ruling from a non-terminal without --by is
        refused and nothing is recorded; --by names who rules (REC-49)"""
        self.held(ONE, defaults='rulings_require_interactive = true')
        self.ruling_env(USER='owner')
        before = self.read_json('make', 'findings.json')
        self.assertEqual(self.rule(), 2, self.output)
        self.assertIn('rulings_require_interactive', self.output)
        self.assertIn('Nothing was recorded', self.output)
        self.assertEqual(self.events('finding-resolved'), [])
        self.assertEqual(self.read_json('make', 'findings.json'), before)
        self.assertEqual(self.rule('--by', 'Sri', terminal=True), 0, self.output)
        [event] = self.events('finding-resolved')
        self.assertEqual((event['by'], event['by_source'], event['interactive'], event['agent_markers']),
                         ('Sri', 'flag', True, []))
        self.assertEqual(self.resume(), 0, self.output)
        self.assertIn('make/PE-1: advisory, ruled by Sri (interactive, name given with --by) on candidate',
                      (Path(self.the_run().path) / 'STATUS.md').read_text())
        self.check_invariants()

    def test_every_ruling_command_passes_the_one_interactive_check(self):
        """rec: with rulings_require_interactive, resolve, approve and reject from a non-terminal
        or inside an agent session are refused with or without --by, and nothing is recorded
        (REC-52)"""
        # `make` blocked with an open blocker, `other` waiting for a person (as FND-39)
        self.workflow(ONE.replace('gate=', 'max_attempts=1\ngate=') + '[[task]]\nid="other"\n'
                      'type="produce"\nprompt="Write b"\noutputs=["src/b"]\ngate=["test -f src/b"]\n'
                      '[[task]]\nid="approval"\ntype="human"\nverifies="other"\n',
                      defaults='rulings_require_interactive = true')
        self.script({'make': [{'write': {'src/a': 'one\n'}, 'answer': done()}],
                     'make.review.principled-priya': [{'answer': review([finding()])}],
                     'other': [{'write': {'src/b': 'b\n'}, 'answer': done()}]})
        self.assertEqual(self.start(), 255, self.output)
        self.assertEqual([self.the_run().state['tasks'][t]['status'] for t in ('make', 'approval')],
                         ['blocked', 'waiting_human'])
        state = Path(self.the_run().path) / 'state.json'
        commands = {'resolve': ('resolve', 'latest', 'make/PE-1', '--as', 'advisory', '-m', 'ok'),
                    'resolve --standing': ('resolve', 'latest', 'make/PE-1', '--as', 'advisory',
                                           '-m', 'ok', '--standing'),
                    'approve': ('approve', 'latest', 'approval'),
                    'reject': ('reject', 'latest', 'approval', '-m', 'redo')}
        for name, argv in commands.items():
            for terminal, markers in ((False, {}), (False, {'CLAUDECODE': '1'}), (True, {'CLAUDECODE': '1'})):
                for by in ((), ('--by', 'Sri')):
                    with self.subTest(command=name, terminal=terminal, markers=markers, by=by):
                        self.ruling_env(USER='owner', **markers)
                        before = state.read_bytes(), self.read_json('make', 'findings.json')
                        decision = Path(self.task_file('approval', 'decision.json'))
                        with mock.patch.object(sys, 'stdin', Terminal() if terminal else io.StringIO()):
                            code = self.runner(*argv, '-C', self.root, *by)
                        self.assertEqual(code, 2, self.output)
                        self.assertIn('rulings_require_interactive', self.output)
                        self.assertIn('--by names who rules but does not replace the terminal',
                                      self.output)
                        if markers:
                            self.assertIn('agent session variables are set (CLAUDECODE)', self.output)
                        self.assertEqual((state.read_bytes(), self.read_json('make', 'findings.json')),
                                         before)
                        self.assertFalse(decision.exists())
        self.assertEqual(self.events('finding-resolved'), [])
        self.ruling_env(USER='owner')
        with mock.patch.object(sys, 'stdin', Terminal()):
            self.assertEqual(self.runner('approve', 'latest', 'approval', '-C', self.root), 0, self.output)
        self.assertEqual(self.read_json('approval', 'decision.json')['decision'], 'approve')
        self.check_invariants()

class Learning(unittest.TestCase):
    def test_reach_question(self):
        """prm: Mira names entry persistence expiry and cleanup (PRM-17)"""
        with (ROOT / 'library/personas/meticulous-mira.toml').open('rb') as fh:
            mira = tomllib.load(fh)
        with (ROOT / 'library/types/code-review.toml').open('rb') as fh:
            fixed = tomllib.load(fh)['prompt']
        for text in (prompts.persona_text(mira, False), fixed):
            self.assertIn('entry, persistence until observation, expiry and cleanup', ' '.join(text.split()))

    def test_optional_hints_prompt(self):
        """prm: review answers invite optional gate and reach hints (PRM-18)"""
        text = prompts.result_schema_text('review')
        self.assertIn('if a shell command could check this requirement, say which', text)
        self.assertIn('optional gateable.command_hint', text)
        self.assertIn('Optionally supply reach_audit', text)
        self.assertNotIn('reach_audit', validate.REVIEW['required'])
        self.assertNotIn('gateable', validate.FINDING['required'])

    def test_optional_shapes_and_cap(self):
        """fnd: optional hints validate atomically and reach audits obey the byte cap (FND-45)
        by truncation; a hint out of place is dropped, not a rejection (FND-48)"""
        audit = dict(test='held', state_claimed='full', reach_evidence='界' * 8, oracle='reject')
        answer = dict(review([dict(finding(), gateable={'command_hint': 'check include'})]), reach_audit=[audit])
        initial = findings.empty('make')
        size = len(json.dumps([audit], ensure_ascii=False).encode())
        ledger, verdict, _ = findings.apply_review(initial, R, answer, 'C', {}, size)
        self.assertEqual(verdict, 'block')
        self.assertEqual(ledger['reviewers'][R['id']]['reach_audit'], [audit])
        self.assertEqual(ledger['findings'][0]['gateable'], {'command_hint': 'check include'})
        bad = []
        for key, value in (('test', 1), ('oracle', None), ('extra', 'not allowed')):
            item = copy.deepcopy(answer)
            item['reach_audit'][0][key] = value
            bad.append(item)
        item = copy.deepcopy(answer)
        del item['reach_audit'][0]['oracle']
        bad.append(item)
        for value in ({}, {'command_hint': 4}, {'command_hint': 'x', 'extra': 'x'}):
            bad.append(review([dict(finding(), gateable=value)]))
        for item in bad:
            with self.subTest(item=item), self.assertRaises(findings.ProtocolError):
                findings.apply_review(initial, R, item, 'C', {})
        cut, verdict, repair = findings.apply_review(initial, R, answer, 'C', {}, size - 1)
        self.assertEqual((verdict, cut['reviewers'][R['id']]['reach_audit']), ('block', []))
        self.assertEqual(repair['hints'], [{'field': 'reach_audit', 'kept': 0, 'dropped': 1,
                                            'bytes': size, 'cap': size - 1}])
        self.assertEqual(initial, findings.empty('make'))
        old, _, _ = findings.apply_review(initial, R, review(), 'C', {})
        self.assertNotIn('reach_audit', old['reviewers'][R['id']])
        self.assertEqual(findings.lab_followups(old), [])

    def test_audit_cut_serializes_each_entry_once(self):
        """fnd: a reach audit over the cap keeps its longest fitting prefix, serializing each
        entry once: the work is linear in the audit, not quadratic (FND-50)"""
        def longest(audit, cap):                 # the definition: the cut the earlier loop made
            kept = list(audit)
            while kept and len(json.dumps(kept, ensure_ascii=False).encode()) > cap:
                kept.pop()
            return kept
        small = [dict(test=f't{n}', state_claimed='s' * n, reach_evidence='界' * (n % 3), oracle='o')
                 for n in range(12)]
        for cap in range(0, len(json.dumps(small, ensure_ascii=False).encode()) + 3):
            with self.subTest(cap=cap):
                cut, _hints = findings._drop_hints(dict(review(), reach_audit=small), cap)
                self.assertEqual(cut['reach_audit'], longest(small, cap))
        audit = [dict(test=f't{n}', state_claimed='s' * 20, reach_evidence='界' * 10, oracle='o')
                 for n in range(3000)]
        full = len(json.dumps(audit, ensure_ascii=False).encode())
        cap, serialized, real = full // 2, [], json.dumps
        def counting(obj, *args, **kwargs):
            text = real(obj, *args, **kwargs)
            serialized.append(len(text))
            return text
        with mock.patch.object(json, 'dumps', side_effect=counting):
            cut, hints = findings._drop_hints(dict(review(), reach_audit=audit), cap)
        self.assertLessEqual(sum(serialized), 2 * full)          # the whole once, each entry once
        kept = cut['reach_audit']
        self.assertEqual(kept, audit[:len(kept)])
        self.assertLessEqual(len(real(kept, ensure_ascii=False).encode()), cap)
        self.assertGreater(len(real(audit[:len(kept) + 1], ensure_ascii=False).encode()), cap)
        self.assertEqual(hints, [{'field': 'reach_audit', 'kept': len(kept),
                                  'dropped': len(audit) - len(kept), 'bytes': full, 'cap': cap}])

    def test_overlap_boundaries(self):
        """fnd: disagreements require overlapping paths and the same review attempt (FND-46)"""
        first, _, _ = findings.apply_review(findings.empty('make'), R,
                                            review([finding('a.cpp:10-12')]), 'C', {})
        other = dict(R, id='other', persona_code='OT')
        both, _, _ = findings.apply_review(first, other,
                    review([finding('a.cpp:11', 'advisory')]), 'C', {})
        self.assertEqual(len(findings.disagreements(both['findings'])), 1)
        exact = copy.deepcopy(both)
        exact['findings'][1]['location'] = 'a.cpp:10-12'
        text = prompts.feedback_text(findings.feedback(exact), 60000)
        self.assertIn('also raised by other as advisory OT-1', text)
        self.assertNotIn('answer each id', text)
        answered = copy.deepcopy(exact['findings'][0])
        answered.update(id='make/PE-2', response_version=answered['version'])
        exact['findings'].append(answered)
        self.assertNotIn('answer each id', prompts.feedback_text(findings.feedback(exact), 60000))
        for change in ('candidate', 'round', 'reviewer', 'path', 'severity'):
            changed = copy.deepcopy(both['findings'])
            if change in ('candidate', 'round'):
                changed[1]['history'][0][change] = 'different'
            elif change == 'path':
                changed[1]['location'] = 'else.cpp:11'
            elif change == 'severity':
                changed[1]['history'][0]['reported']['severity'] = 'blocking'
            else:
                changed[1]['reviewer'] = R['id']
            self.assertEqual(findings.disagreements(changed), [], change)
        for a, b, expected in [('a:10-12', 'a:12', True), ('a:L10-L12', 'a:11', True),
                               ('a', 'a:900', True), ('a:10-12', 'a:13', False),
                               ('a:12-10', 'a:11', False), ('', '', False)]:
            self.assertEqual(findings.overlapping(a, b), expected)


class LearningDelivery(EngineCase):
    HEADER = EngineCase.HEADER + 'read_only_args = ["--read-only"]\n'

    def entry(self, **extra):
        return dict(task='make', finding_title='Fix this', location_glob='src/a:*', decision='advisory',
                    note='Owner accepts the narrow contract', ruled_at='2026-10-08T12:00:00Z',
                    by='owner', **extra)

    def rulings_file(self, entries):
        return self.write('rulings.toml', '\n'.join(map(findings.ruling_toml, entries)))

    events = Provenance.events
    ruling_env = Provenance.ruling_env
    held = Provenance.held

    def target(self, rid, run=None):
        path = Path(run.task_dir(rid) if run else self.task_file(rid), 'round-1', 'prompt.md')
        return json.loads(path.read_text().split('<<<DATA target\n')[1].split('\nDATA>>>')[0])

    def brief_of(self, task='make', prompt='Make the work', rules=''):
        """The effective brief of `task` in the committed workflow, as the runner hashes it."""
        tasks = {t['id']: t for t in workflow.load(self.wf_path).tasks}
        return findings.effective_brief(tasks[task], prompt, rules, tasks)

    def standing(self, note, *extra):
        with mock.patch.object(sys, 'stdin', io.StringIO()):            # not a terminal
            return self.runner('resolve', 'latest', 'make/PE-1', '--as', 'advisory', '--standing',
                               '--note', note, '-C', self.root, *extra)

    def started_run(self):
        """The run the last `start` created, by the record path it printed."""
        from codesmith import record
        line = next(l for l in self.output.splitlines() if l.startswith('record  '))
        return record.Run.load(line.split(None, 1)[1])

    def test_grouped_disagreement(self):
        """fnd: overlapping panel findings group feedback and STATUS without changing severity (FND-44)"""
        self.workflow(ONE.replace('["principled-priya"]', '["neckbeard-nate", "concerned-carlos"]'))
        nate, carlos = 'make.review.neckbeard-nate', 'make.review.concerned-carlos'
        fixed = dict(finding='make/NN-1', action='fixed', note='fixed')
        self.script({'make': [{'write': {'src/a': 'one\n'}, 'answer': done()},
                              {'write': {'src/a': 'two\n'}, 'answer': done(responses=[fixed]),
                               'match': 'also raised by concerned-carlos as advisory CC-1'}],
                     nate: [{'answer': review([finding('a.cpp:10-12')])},
                            {'answer': review(resolutions=[resolution('make/NN-1')])}],
                     carlos: [{'answer': review([finding('a.cpp:11', 'advisory')])}, PASS]})
        self.assertEqual(self.start(), 0, self.output)
        for name in ('attempt-2/feedback.md', 'STATUS.md'):
            text = Path(self.task_file('make', name)).read_text()
            self.assertIn('also raised by concerned-carlos as advisory CC-1', text)
        ledger = self.read_json('make', 'findings.json')
        self.assertEqual([f['severity'] for f in ledger['findings']], ['blocking', 'advisory'])
        self.check_invariants()

    def test_standing_delivery(self):
        """run: standing rulings reach reviewers and downstream author inputs (RUN-61)"""
        self.workflow(ONE + '''
[[task]]
id="down"
type="implement"
needs=["make"]
prompt="Use upstream"
outputs=["src/down"]
gate=["test -f src/down"]
''', defaults='rulings_file="rulings.toml"')
        self.rulings_file([self.entry(brief_sha256=findings.brief_hash(self.brief_of()))])
        self.commit()
        rid = 'make.review.principled-priya'
        self.script({'make': [{'write': {'src/a': 'one\n'}, 'answer': done()}],
                     rid: [dict(PASS, match=r'(?s)"settled_by_person".*Owner accepts the narrow contract.*"standing": true')],
                     'down': [{'write': {'src/down': 'down\n'}, 'answer': done(),
                               'match': 'Owner accepts the narrow contract'}]})
        self.assertEqual(self.start(), 0, self.output)
        settled = self.target(rid)['settled_by_person'][0]
        self.assertTrue(settled['standing'])
        self.assertEqual(settled['provenance'], 'provenance unknown')      # written by hand
        page = Path(self.task_file('make', 'STATUS.md')).read_text()
        self.assertIn('1 standing rulings supplied to reviewers', page)
        self.assertIn('- Fix this at src/a:*: advisory, ruled by owner (provenance unknown), ', page)
        self.check_invariants()

    def test_standing_source_is_frozen(self):
        """run: the standing rulings are the committed copy, pinned by blob; an author's edit of the
        file fails the protected check (RUN-67)"""
        original = self.entry()
        text = '[[ruling]]\n' + ''.join(f'{k} = {json.dumps(v)}\n' for k, v in original.items())
        self.write('rulings.toml', text)
        # `writes` covers the whole tree: only the protection keeps the author out of the file.
        self.workflow(ONE.replace('outputs=["src/a"]', 'outputs=["src/a"]\nwrites=["**"]'),
                      defaults='rulings_file="rulings.toml"')
        rid = 'make.review.principled-priya'
        forged = text.replace(original['note'], 'Author tries to change the settled reading')
        self.script({'make': [{'write': {'src/a': 'one\n', 'rulings.toml': forged}, 'answer': done()},
                              {'write': {'src/a': 'one\n', 'rulings.toml': text}, 'answer': done()}],
                     rid: [dict(PASS, match='Owner accepts the narrow contract')]})
        self.assertEqual(self.start(), 0, self.output)
        self.assertEqual(self.the_run().state['tasks']['make']['attempts'], 2)
        self.assertRegex(Path(self.task_file('make', 'attempt-2', 'prompt.md')).read_text(),
                         r'rulings\.toml[^\n]*it is protected')
        self.assertEqual(self.target(rid)['settled_by_person'][0]['note'], original['note'])
        expanded = json.loads((Path(self.the_run().path) / 'workflow.expanded.json').read_text())
        self.assertEqual(expanded['defaults']['_standing_rulings'], [original])
        self.assertEqual(expanded['defaults']['_standing_rulings_blob'],
                         self.git_out('rev-parse', 'main:rulings.toml'))
        self.assertIn('rulings.toml', expanded['tasks'][0]['protected'])
        # A replan reads the copy of the run's starting commit, not a later one.
        start = self.git_out('rev-parse', 'main')
        self.write('rulings.toml', forged)
        self.commit()
        self.assertEqual(workflow.load(self.wf_path, rulings_at=start).defaults['_standing_rulings'],
                         [original])
        self.git_out('reset', '-q', '--hard', 'HEAD~1')
        self.check_invariants()

    def test_stale_standing_dropped(self):
        """run: a changed brief drops its standing ruling and STATUS says why (RUN-62)"""
        self.workflow(ONE, defaults='rulings_file="rulings.toml"')
        self.rulings_file([self.entry(brief_sha256=findings.brief_hash(self.brief_of(prompt='Earlier brief')))])
        self.commit()
        rid = 'make.review.principled-priya'
        self.script({'make': [{'write': {'src/a': 'one\n'}, 'answer': done()}],
                     rid: [dict(PASS, match=r'"settled_by_person": \[\]')]})
        self.assertEqual(self.start(), 0, self.output)
        self.assertEqual(self.target(rid)['settled_by_person'], [])
        self.assertIn('0 standing rulings supplied to reviewers (1 dropped: the brief changed)',
                      Path(self.task_file('make', 'STATUS.md')).read_text())
        self.check_invariants()

    def test_resolve_standing(self):
        """run: resolve standing queues the ruling and the finished run creates the TOML with a brief hash (RUN-63)"""
        self.workflow(ONE, defaults='rulings_file="rulings.toml"')
        rid = 'make.review.principled-priya'
        disputes = [dict(finding=f'make/PE-{n}', action='disputed', note='scope exception')
                    for n in (1, 2)]
        self.script({'make': [{'write': {'src/a': 'one\n'}, 'answer': done()},
                             {'answer': done(responses=disputes)}],
                     rid: [{'answer': review([finding(), finding('src/a:3')])},
                           {'answer': review(resolutions=[resolution(f'make/PE-{n}', status='unresolved')
                                                         for n in (1, 2)])}]})
        self.assertEqual(self.start(), 255, self.output)
        before = self.the_run().state['tasks']['make']
        self.assertEqual(self.runner('resolve', 'latest', 'make/PE-1', '--as', 'upheld',
                                     '--standing', '--note', 'why', '-C', self.root), 2, self.output)
        self.assertEqual(self.the_run().state['tasks']['make']['ledger'], before['ledger'])
        note = 'Narrow scope; the brief will say so. Quote: "accepted".\nNext line.'
        self.assertEqual(self.runner('resolve', 'latest', 'make/PE-1', '--as', 'advisory',
                                     '--standing', '--note', note, '-C', self.root), 0, self.output)
        path = Path(self.root) / 'rulings.toml'
        self.assertFalse(path.exists())                     # the paused run's tree is not touched
        self.assertIn('written to rulings.toml when the run is done', self.output)
        after = self.the_run().state['tasks']['make']
        self.assertEqual(set(before), set(after))
        self.assertEqual(self.runner('resolve', 'latest', 'make/PE-2', '--as', 'resolved',
                                     '--standing', '--note', 'Settled too', '-C', self.root), 0, self.output)
        self.assertEqual(len(self.the_run().state['standing_pending']), 2)
        self.assertIn('queued; written to the rulings file when the run is done',
                      (Path(self.the_run().path) / 'STATUS.md').read_text())
        self.assertEqual(self.resume(), 0, self.output)
        entries = findings.read_rulings(str(path))
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]['brief_sha256'], findings.brief_hash(self.brief_of()))
        self.assertEqual(entries[0]['note'], note)
        self.assertEqual(entries[0]['location_glob'], 'src/a:2')
        self.assertEqual(entries[1]['decision'], 'resolved')
        self.assertIn('commit it, so the next run reads them', self.output)
        exported = json.loads((Path(self.the_run().path) / 'follow-ups.json').read_text())
        self.assertEqual(exported['tasks']['make'][0]['ruling']['note'], note)
        self.commit()                                       # the operator commits the export
        self.check_invariants()

    def test_gateable_followups(self):
        """run: gateable blockers retain command hints and paid rounds in follow-ups (RUN-64)"""
        self.workflow(ONE)
        rid = 'make.review.principled-priya'
        hint = 'grep -q required src/a'
        fixed = dict(finding='make/PE-1', action='fixed', note='fixed')
        self.script({'make': [{'write': {'src/a': 'one\n'}, 'answer': done()},
                             {'write': {'src/a': 'two\n'}, 'answer': done(responses=[fixed])}],
                     rid: [{'answer': review([dict(finding(), gateable={'command_hint': hint})])},
                           {'answer': review(resolutions=[resolution()])}]})
        self.assertEqual(self.start(), 0, self.output)
        text = Path(self.task_file('make', 'STATUS.md')).read_text()
        self.assertIn('## Lab follow-ups', text)
        self.assertIn(hint, text)
        self.assertIn('review rounds: 1, 2 (2 rounds)', text)
        path = Path(self.the_run().path) / 'follow-ups.json'
        report = json.loads(path.read_text())['tasks']['make'][0]
        self.assertEqual((report['command_hint'], report['rounds']), (hint, [1, 2]))
        path.unlink()
        self.the_run().regenerate(full=True)
        self.assertEqual(json.loads(path.read_text())['tasks']['make'][0], report)
        self.check_invariants()

    def test_a_hint_out_of_place_is_a_repair(self):
        """fnd: a gateable hint on an advisory finding is dropped as a repair: the answer is
        accepted on its one call and the review-repair event names the hint (FND-48)"""
        self.workflow(ONE)
        rid = 'make.review.principled-priya'
        hinted = dict(finding(severity='advisory'), gateable={'command_hint': 'grep -q x src/a'})
        self.script({'make': [{'write': {'src/a': 'one\n'}, 'answer': done()}],
                     rid: [{'answer': review([hinted])}]})
        self.assertEqual(self.start(), 0, self.output)
        self.assertEqual(self.status('make'), 'accepted')
        self.assertEqual(Path(self.script_path + '.' + rid + '.counter').read_text(), '1')
        events = [json.loads(line) for line in
                  (Path(self.the_run().path) / 'events.jsonl').read_text().splitlines()]
        self.assertEqual([e['status'] for e in events if e['event'] == 'review-call'], ['ok'])
        self.assertEqual([e for e in events if e['event'] == 'review-answer-rejected'], [])
        hint = {'field': 'gateable', 'finding': 'Fix this', 'command_hint': 'grep -q x src/a'}
        [repair] = [e for e in events if e['event'] == 'review-repair']
        self.assertEqual((repair['kind'], repair['dropped'], repair['hints']), ('dropped_hints', 0, [hint]))
        verdict = self.read_json(rid, 'round-1', 'verdict.json')['result']
        self.assertEqual(verdict['repair']['hints'], [hint])
        [noted] = self.the_run().state['tasks']['make']['ledger']['findings']
        self.assertNotIn('gateable', noted)
        self.assertEqual(findings.lab_followups(self.the_run().state['tasks']['make']['ledger']), [])
        self.check_invariants()

    def test_mira_optional_audit(self):
        """run: Mira reach audits render a table and old answers need no repair (RUN-65)"""
        self.workflow(ONE.replace('principled-priya', 'meticulous-mira') + '''
[[task]]
id="old"
type="implement"
needs=["make"]
prompt="Old answer"
outputs=["src/old"]
gate=["test -f src/old"]
reviewers=["meticulous-mira"]
''')
        audit = dict(test='held | state', state_claimed='full', reach_evidence='entered; held until observation',
                     oracle='reject next write')
        rid = 'make.review.meticulous-mira'
        self.script({'make': [{'write': {'src/a': 'one\n'}, 'answer': done()}],
                     rid: [{'answer': dict(review(), reach_audit=[audit])}],
                     'old': [{'write': {'src/old': 'old\n'}, 'answer': done()}],
                     'old.review.meticulous-mira': [PASS]})
        self.assertEqual(self.start(), 0, self.output)
        text = Path(self.task_file('make', 'STATUS.md')).read_text()
        self.assertIn('| Reviewer / round | Test | State claimed | Reach evidence | Oracle |', text)
        self.assertIn(r'held \| state', text)
        self.assertIn('reject next write', text)
        old = Path(self.task_file('old', 'STATUS.md')).read_text()
        self.assertNotIn('## Reach audit', old)
        self.assertNotIn('## Lab follow-ups', old)
        self.assertEqual(Path(self.script_path + '.old.review.meticulous-mira.counter').read_text(), '1')
        self.check_invariants()

    def test_rulings_validation_and_location(self):
        """run: standing rulings validate repository paths shapes and known locations (RUN-66)"""
        self.rulings_file([self.entry()])
        self.workflow(ONE, defaults='rulings_file="rulings.toml"')
        self.assertTrue(workflow.load(self.wf_path).ok)
        for item in (dict(self.entry(), decision='upheld'), dict(self.entry(), note=2),
                     dict(self.entry(), brief_sha256='bad'), dict(self.entry(), extra='bad'),
                     dict(self.entry(), by_source='agent'), dict(self.entry(), interactive='no')):
            self.rulings_file([item])
            self.commit()
            self.assertFalse(workflow.load(self.wf_path).ok)
        entry, brief = self.entry(), self.brief_of()
        self.assertEqual(len(findings.standing_rulings([entry], 'make', brief)[0]), 1)
        self.assertEqual(findings.standing_rulings([entry], 'another', brief), ([], []))
        self.assertEqual(findings.standing_rulings([entry], 'make', brief, [finding('else:1')]), ([], []))
        self.assertEqual(len(findings.standing_rulings([entry], 'make', brief, [finding('src/a:2')])[0]), 1)
        for name in ('', '../outside.toml', '/tmp/outside.toml', 'rulings-*.toml'):
            with self.assertRaises(ValueError):
                findings.rulings_path(self.root, name)
        Path(self.root, 'link').symlink_to(Path(self.root).parent, target_is_directory=True)
        with self.assertRaises(ValueError):
            findings.rulings_path(self.root, 'link/outside.toml')

    def test_tracked_standing_round_trip(self):
        """run: a tracked rulings file: start, resolve --standing and resume reach done (RUN-68)"""
        self.rulings_file([dict(self.entry(), task='other')])
        self.held(ONE, defaults='rulings_file="rulings.toml"')
        self.ruling_env(USER='owner')
        self.assertEqual(self.standing('Owner accepts the narrower scope'), 0, self.output)
        self.assertEqual(self.git_out('status', '--porcelain', '--', 'rulings.toml'), '')
        self.assertEqual(self.resume(), 0, self.output)
        self.assertEqual(self.the_run().state['status'], 'done')
        entries = findings.read_rulings(os.path.join(self.root, 'rulings.toml'))
        self.assertEqual([e['task'] for e in entries], ['other', 'make'])
        self.assertEqual((entries[1]['run'], entries[1]['finding']), (self.the_run().name, 'make/PE-1'))
        self.assertEqual(self.git_out('status', '--porcelain'), 'M rulings.toml')
        self.commit()
        self.check_invariants()

    def test_rulings_file_is_owner_input(self):
        """run: no task may claim the rulings file, and start refuses an uncommitted change to it
        (RUN-69; an author's write during its call is RUN-67's run)"""
        self.rulings_file([self.entry()])
        self.workflow(ONE.replace('outputs=["src/a"]', 'outputs=["src/a"]\nwrites=["rulings.toml"]'),
                      defaults='rulings_file="rulings.toml"')
        self.assertIn("task 'make' names the standing rulings file rulings.toml in its writes",
                      ' '.join(workflow.load(self.wf_path).errors))
        self.workflow(ONE, defaults='rulings_file="rulings.toml"')
        self.rulings_file([dict(self.entry(), note='Forged after the commit')])
        self.assertEqual(self.start(), 2, self.output)
        self.assertIn("rulings_file 'rulings.toml' differs from its committed copy. A run reads the "
                      "committed copy only: commit rulings.toml, or remove rulings_file", self.output)
        self.assertFalse(os.path.exists(os.path.join(self.root, '.runs', 'demo')))

    def test_ignored_or_untracked_rulings_refused(self):
        """run: an ignored or untracked rulings file is refused at start with the remedy (RUN-70)"""
        self.write('.gitignore', 'private/\n')
        for name in ('private/rulings.toml', '.runs/rulings.toml'):
            with self.subTest(name=name):
                self.workflow(ONE, defaults=f'rulings_file="{name}"')
                self.assertEqual(self.start(), 2, self.output)
                self.assertIn(f"rulings_file '{name}' is ignored by git", self.output)
                self.assertIn('move them to a tracked path and commit it, or remove rulings_file', self.output)
        self.workflow(ONE, defaults='rulings_file="rulings.toml"')
        self.rulings_file([self.entry()])
        self.assertEqual(self.start(), 2, self.output)
        self.assertIn("rulings_file 'rulings.toml' is not committed. A run reads the committed copy "
                      "only: commit rulings.toml, or remove rulings_file. Nothing was changed", self.output)
        self.assertFalse(os.path.exists(os.path.join(self.root, '.runs', 'demo')))

    def test_standing_provenance_travels(self):
        """run: an exported standing ruling carries its provenance to the next run's reviewer and
        STATUS (RUN-71)"""
        self.held(ONE, defaults='rulings_file="rulings.toml"')
        self.ruling_env(USER='owner', CLAUDECODE='1')
        self.assertEqual(self.standing('Owner accepts the narrower scope'), 0, self.output)
        self.assertEqual(self.resume(), 0, self.output)
        first = self.the_run()
        [entry] = findings.read_rulings(os.path.join(self.root, 'rulings.toml'))
        self.assertEqual({k: entry[k] for k in ('by', 'by_source', 'interactive', 'agent_markers', 'run', 'finding')},
                         {'by': 'owner', 'by_source': 'env', 'interactive': False,
                          'agent_markers': ['CLAUDECODE'], 'run': first.name, 'finding': 'make/PE-1'})
        how = 'non-interactive, name from the environment; agent session markers: CLAUDECODE'
        self.assertIn(f'make/PE-1: advisory, ruled by owner ({how}): written to the rulings file',
                      (Path(first.path) / 'STATUS.md').read_text())
        self.commit()
        rid = 'make.review.principled-priya'
        for counter in Path(self.side).glob('script.json.*.counter'):
            counter.unlink()                                # the second run's calls count from 1
        self.script({'make': [{'write': {'src/a': 'two\n'}, 'answer': done()}],
                     rid: [dict(PASS, match=r'agent session markers: CLAUDECODE')]})
        self.assertEqual(self.start(), 0, self.output)
        second = self.started_run()
        settled = self.target(rid, second)['settled_by_person'][0]
        self.assertEqual((settled['note'], settled['provenance']), ('Owner accepts the narrower scope', how))
        self.assertIn(f'ruled by owner ({how}) in run {first.name}, ',
                      (Path(second.task_dir('make')) / 'STATUS.md').read_text())

    def test_changed_rules_drop_standing(self):
        """run: a changed project rules file drops a standing ruling whose prompt is unchanged, and
        STATUS names the part that changed (RUN-72)"""
        self.write('rules.md', 'New rules\n')
        self.workflow(ONE, defaults='rulings_file="rulings.toml"\nrules_file="rules.md"')
        old, new = self.brief_of(rules='Old rules\n'), self.brief_of(rules='New rules\n')
        self.rulings_file([
            self.entry(brief_sha256=findings.brief_hash(old), brief_parts=findings.brief_parts(old)),
            dict(self.entry(brief_sha256=findings.brief_hash(new), brief_parts=findings.brief_parts(new)),
                 finding_title='Other', note='Kept under the new rules')])
        self.commit()
        rid = 'make.review.principled-priya'
        self.script({'make': [{'write': {'src/a': 'one\n'}, 'answer': done()}],
                     rid: [dict(PASS, match='Kept under the new rules')]})
        self.assertEqual(self.start(), 0, self.output)
        self.assertEqual([e['note'] for e in self.target(rid)['settled_by_person']], ['Kept under the new rules'])
        self.assertIn('1 standing rulings supplied to reviewers (1 dropped: the brief changed (project rules)).',
                      Path(self.task_file('make', 'STATUS.md')).read_text())
        self.check_invariants()

    def test_standing_rulings_need_a_terminal_when_required(self):
        """run: with rulings_require_interactive a committed standing ruling not recorded as ruled
        at a terminal is dropped, never supplied (RUN-74)"""
        self.workflow(ONE, defaults='rulings_file="rulings.toml"\nrulings_require_interactive=true')
        self.rulings_file([self.entry()])                       # written by hand, no provenance
        self.commit()
        rid = 'make.review.principled-priya'
        self.script({'make': [{'write': {'src/a': 'one\n'}, 'answer': done()}], rid: [PASS]})
        self.assertEqual(self.start(), 0, self.output)
        self.assertEqual(self.target(rid).get('settled_by_person', []), [])
        self.assertIn('0 standing rulings supplied to reviewers (1 dropped: not ruled at a terminal).',
                      Path(self.task_file('make', 'STATUS.md')).read_text())
        brief = self.brief_of()
        terminal = dict(by_source='flag', interactive=True, agent_markers=[])
        for entry, supplied in ((self.entry(), 0), (self.entry(**dict(terminal, interactive=False)), 0),
                                (self.entry(**dict(terminal, agent_markers=['CLAUDECODE'])), 0),
                                (self.entry(**terminal), 1)):
            found, dropped = findings.standing_rulings([entry], 'make', brief, require_interactive=True)
            self.assertEqual((len(found), dropped), (supplied, [] if supplied else ['not ruled at a terminal']))
        self.assertEqual(len(findings.standing_rulings([self.entry()], 'make', brief)[0]), 1)
        self.check_invariants()

    def test_older_export_is_dropped_with_its_own_reason(self):
        """run: a standing ruling exported before the effective brief (prompt hash, no brief_parts)
        is dropped as exported by an older runner, not as a changed brief (RUN-75)"""
        import hashlib
        self.workflow(ONE, defaults='rulings_file="rulings.toml"')
        brief = self.brief_of()
        legacy = self.entry(brief_sha256=hashlib.sha256(b'Make the work').hexdigest())
        self.assertEqual(findings.standing_rulings([legacy], 'make', brief),
                         ([], ['exported by an older runner; rule it again']))
        other = self.entry(brief_sha256=hashlib.sha256(b'Another prompt').hexdigest())
        self.assertEqual(findings.standing_rulings([other], 'make', brief), ([], ['the brief changed']))

    def test_standing_export_survives_a_crash(self):
        """run: the ruling and its queued export are one save; a crash after the export's write and
        a failed write each leave a record that resume finishes, writing the entry once (RUN-73);
        a failure before the write leaves no remembered tree, so the owner's fix is resumed"""
        class Killed(Exception):
            pass

        self.held(ONE, defaults='rulings_file="rulings.toml"')
        self.ruling_env(USER='owner')
        self.assertEqual(self.standing('Owner accepts the narrower scope'), 0, self.output)
        state = self.the_run().state
        self.assertEqual(state['tasks']['make']['ledger']['findings'][0]['history'][-1]['event'], 'human')
        self.assertEqual(len(state['standing_pending']), 1)
        # Refused after its entry was queued in memory: neither the ruling nor the entry is saved.
        self.assertEqual(self.standing('Again'), 2, self.output)
        self.assertEqual(self.the_run().state, state)

        def hook(where, error):
            if where == 'standing:written':
                raise error
        path = os.path.join(self.root, 'rulings.toml')
        # Before the write: the run ends failed holding no tree, so the owner's fix of the cause
        # (here a rulings file broken in the tree) is no change resume refuses.
        def before(where):
            if where == 'standing:before-write':
                raise OSError('permission denied')
        self.cli.CRASH = before
        self.assertEqual(self.resume(), 2, self.output)
        self.assertEqual(self.the_run().state['status'], 'failed')
        self.assertIsNone(self.the_run().state['expect'])
        self.assertIn('could not be written to rulings.toml: permission denied', self.output)
        self.assertFalse(os.path.exists(path))
        # The page names resume as the writer, not "this failed run will not write it" (V-06).
        page = (Path(self.the_run().path) / 'STATUS.md').read_text()
        self.assertIn(f'the run failed writing it: fix the cause, then `runner resume '
                      f'{self.the_run().name}` writes it to rulings.toml', page)
        self.assertNotIn('will not write it', page)
        self.cli.CRASH = None
        self.write('rulings.toml', '[[ruling]\nbroken = ')
        self.assertEqual(self.resume(), 2, self.output)
        self.assertIn('the standing rulings could not be written to rulings.toml', self.output)
        os.unlink(path)                                       # the owner fixes the cause
        self.assertEqual(self.events('standing-rulings-written'), [])
        # After the write: a kill, then a failure; resume writes the entry once.
        self.cli.CRASH = lambda where: hook(where, Killed(where))
        with self.assertRaises(Killed):
            self.resume()
        self.cli.CRASH = lambda where: hook(where, OSError('disk full'))
        self.assertEqual(self.resume(), 2, self.output)
        self.cli.CRASH = None
        self.assertEqual(self.the_run().state['status'], 'failed')
        self.assertIn('the standing rulings could not be written to rulings.toml: disk full',
                      self.the_run().state['stop_reason'])
        self.assertEqual(len(findings.read_rulings(path)), 1)
        self.assertEqual(self.resume(), 0, self.output)
        self.assertEqual(len(findings.read_rulings(path)), 1)
        self.assertEqual(self.the_run().state['standing_pending'][0]['written'], True)
        self.assertEqual(len(self.events('standing-rulings-written')), 1)
        self.assertEqual(self.the_run().integrity_check(), [])
        self.commit()
        self.check_invariants()

    def test_a_queued_ruling_keeps_its_destination(self):
        """run: a queued standing ruling is bound to its rulings file: a replan that drops or
        moves rulings_file is refused naming it, and an entry with no file left fails as a stop,
        never a traceback (RUN-76)"""
        from codesmith import engine
        self.held(ONE + '\n[[task]]\nid="look"\ntype="human"\nneeds=["make"]\n',
                  defaults='rulings_file="rulings.toml"')
        self.ruling_env(USER='owner')
        self.assertEqual(self.standing('Owner accepts the narrower scope'), 0, self.output)
        # The producer is accepted and the run waits for a person: nothing holds the tree.
        self.assertEqual(self.resume(), 255, self.output)
        state = self.the_run().state
        self.assertEqual(state['standing_pending'][0]['file'], 'rulings.toml')
        self.assertFalse(state['standing_pending'][0].get('written'))
        for defaults, said in (('', 'drops rulings_file'),
                               ('rulings_file="elsewhere.toml"', 'names elsewhere.toml')):
            with self.subTest(defaults=defaults):
                revised = os.path.join(self.side, 'revised.toml')       # outside the repository
                text = Path(self.wf_path).read_text().replace(
                    'rulings_file="rulings.toml"', defaults).replace(
                    'name = "demo"\n', f'name = "demo"\nroot = "{self.root}"\n', 1)
                Path(revised).write_text(text)
                self.assertEqual(self.runner('replan', 'latest', '--workflow', revised, '-C',
                                             self.root), 2, self.output)
                self.assertIn('standing rulings are queued for rulings.toml (make/PE-1)', self.output)
                self.assertIn(said, self.output)
                self.assertIn('Nothing was changed', self.output)
                self.assertEqual(self.the_run().state, state)
        self.assertEqual(self.runner('approve', 'latest', 'look', '-C', self.root), 0, self.output)
        self.assertEqual(self.resume(), 0, self.output)
        self.assertEqual(len(findings.read_rulings(os.path.join(self.root, 'rulings.toml'))), 1)
        # An entry queued before it was bound, with no rulings_file now: the failure branch.
        run = self.the_run()
        legacy = dict(run.state['standing_pending'][0], written=False, run='other')
        del legacy['file']
        run.state['standing_pending'] = [legacy]
        with self.assertRaises(engine.StandingExportError) as ctx:
            engine.export_queued(run, self.root, None, lambda where: None)
        self.assertIn('make/PE-1 has no rulings file', str(ctx.exception))
        self.assertFalse(legacy['written'])
        self.assertEqual(engine.export_queued(run, self.root, 'rulings.toml', lambda where: None),
                         [('rulings.toml', 1)])

    def test_a_failed_run_exports_its_queued_rulings(self):
        """run: a run that ends failed lists its queued standing ruling in full on STATUS with
        `runner export-rulings`, which writes it once, refuses while the branch or the tree is
        not as the stop left it, and leaves a tree `resume` does not refuse (RUN-77)"""
        tasks = ONE + DOWN.replace('gate=', 'max_attempts=1\ngate=')
        self.held(tasks, defaults='rulings_file="rulings.toml"')
        rid = 'make.review.principled-priya'
        dispute = dict(finding='make/PE-1', action='disputed', note='I ask for a scope exception')
        steps = {'make': [{'write': {'src/a': 'one\n'}, 'answer': done()},
                          {'answer': done('Asked for a ruling', responses=[dispute])}],
                 rid: [{'answer': review([finding()])},
                       {'answer': review(resolutions=[resolution(status='unresolved')])}],
                 'down': [{'answer': done()},                        # no src/down: its gate fails
                          {'write': {'src/down': 'down\n'}, 'answer': done()}],   # its retry
                 'down.review.principled-priya': [PASS]}
        self.script(steps)
        self.ruling_env(USER='owner')
        self.assertEqual(self.standing('Owner accepts the narrower scope'), 0, self.output)
        self.assertEqual(self.resume(), 2, self.output)
        run = self.the_run()
        self.assertEqual(run.state['status'], 'failed')
        page = (Path(run.path) / 'STATUS.md').read_text()
        self.assertIn(f'this run writes it only if it gets to done (by `runner retry {run.name} '
                      f'down`): `runner export-rulings {run.name}` writes it to rulings.toml now',
                      page)
        for shown in ('task "make"', 'finding_title "Fix this"', 'location_glob "src/a:2"',
                      'note "Owner accepts the narrower scope"', 'by "owner"'):
            self.assertIn(shown, page)
        path = os.path.join(self.root, 'rulings.toml')
        self.git_out('checkout', '-q', 'main')
        self.assertEqual(self.runner('export-rulings', run.name, '-C', self.root), 2, self.output)
        self.assertIn('check it out first', self.output)
        self.git_out('checkout', '-q', run.info['branch'])
        self.write('stray.txt', 'x\n')
        self.assertEqual(self.runner('export-rulings', run.name, '-C', self.root), 2, self.output)
        self.assertIn('the branch or the work tree changed since the run stopped', self.output)
        os.unlink(os.path.join(self.root, 'stray.txt'))
        self.assertFalse(os.path.exists(path))
        for _ in range(2):
            self.assertEqual(self.runner('export-rulings', run.name, '-C', self.root), 0, self.output)
            self.assertEqual(len(findings.read_rulings(path)), 1)
            if _ == 0:
                # Nothing in the run touches the tree any more: commit it; a retry adopts the
                # commit (RUN-81).
                self.assertIn('1 standing ruling(s) written to rulings.toml; commit it, so the next '
                              f'run reads it; then `runner retry {run.name} down` starts again '
                              'from your commit', self.output)
        self.assertIn('nothing to do', self.output)
        self.assertEqual(len(self.events('standing-rulings-written')), 1)
        self.assertIn('written to the rulings file rulings.toml',
                      (Path(run.path) / 'STATUS.md').read_text())
        # The tree the stop remembers takes the written file: resume does not refuse it, and
        # writes nothing again.
        self.assertEqual(self.resume(), 2, self.output)
        self.assertNotIn('work tree changed', self.output)
        self.assertEqual(self.the_run().state['status'], 'failed')
        self.assertEqual(len(findings.read_rulings(path)), 1)
        self.assertEqual(len(self.events('standing-rulings-written')), 1)
        # A retry before the commit names the true remedy: commit the file as the export wrote
        # it, never remove it, which would drop a ruling marked written (RUN-81, V-05).
        self.assertEqual(self.runner('retry', run.name, 'down', '-C', self.root), 2, self.output)
        self.assertIn('the work tree has uncommitted changes (?? rulings.toml). Nothing was changed; '
                      'commit rulings.toml as `runner export-rulings` wrote it, then run this retry '
                      'again.', self.output)
        self.assertNotIn('remove', self.output)
        self.commit()                                       # the owner commits the export
        self.assertEqual(self.runner('retry', run.name, 'down', '-C', self.root), 0, self.output)
        self.assertEqual(self.resume(), 0, self.output)
        self.assertEqual(self.the_run().state['status'], 'done')
        self.assertEqual(len(findings.read_rulings(path)), 1)
        self.assertEqual(len(self.events('standing-rulings-written')), 1)
        self.check_invariants()

    LOOK = '\n[[task]]\nid="look"\ntype="human"\nneeds=["make"]\n'

    def refused_export(self, live):
        """`export-rulings` refuses a run where anything can still change the tree, naming it and
        the ways on, and writes nothing: no file, the state as it was (RUN-80, RUN-81)."""
        run = self.the_run()
        before = run.state
        self.assertEqual(self.runner('export-rulings', run.name, '-C', self.root), 2, self.output)
        self.assertIn(f'run {run.name} can still change the work tree ({live}), and a file '
                      'written now would be undone or stop it. Finish it: `runner resume '
                      f'{run.name}` (after the approvals or rulings its STATUS names) goes on, '
                      'and the run writes the rulings when it is done', self.output)
        self.assertIn(f'`runner replan {run.name} --workflow FILE` with those tasks removed. '
                      'Nothing was written', self.output)
        # STATUS points the same way, never at the export (RUN-81).
        page = (Path(run.path) / 'STATUS.md').read_text()
        self.assertIn(f'Work is left ({live}): finish it: `runner resume {run.name}`', page)
        self.assertNotIn(f'`runner export-rulings {run.name}`', page)
        self.assertFalse(os.path.exists(os.path.join(self.root, 'rulings.toml')))
        self.assertEqual(self.the_run().state, before)
        self.assertEqual(self.events('standing-rulings-written'), [])

    def test_export_rulings_refuses_a_run_waiting_with_a_producer_left(self):
        """run: export-rulings refuses a run with a producer still to run (needs_human waiting on
        an approval with a producer after it), naming it and the ways on, writing nothing; a
        replan that removes the producer lets the run finish and write the ruling (RUN-80)"""
        tasks = ONE + self.LOOK + DOWN.replace('needs=["make"]', 'needs=["look"]')
        self.held(tasks, defaults='rulings_file="rulings.toml"')
        self.ruling_env(USER='owner')
        self.assertEqual(self.standing('Owner accepts the narrower scope'), 0, self.output)
        self.assertEqual(self.resume(), 255, self.output)
        self.assertEqual(self.the_run().state['status'], 'needs_human')
        self.refused_export("'look' waiting_human, 'down' pending, "
                            "'down.review.principled-priya' pending")
        # Ended without the producer: a replan removes it, and the run finishes writing it.
        revised = os.path.join(self.side, 'revised.toml')                # outside the repository
        text = Path(self.wf_path).read_text().replace(
            DOWN.replace('needs=["make"]', 'needs=["look"]'), '').replace(
            'name = "demo"\n', f'name = "demo"\nroot = "{self.root}"\n', 1)
        Path(revised).write_text(text)
        self.assertEqual(self.runner('replan', 'latest', '--workflow', revised, '-C', self.root),
                         0, self.output)
        self.assertEqual(self.runner('approve', 'latest', 'look', '-C', self.root), 0, self.output)
        self.assertEqual(self.resume(), 0, self.output)
        self.assertEqual(self.the_run().state['status'], 'done')
        self.assertEqual(len(findings.read_rulings(os.path.join(self.root, 'rulings.toml'))), 1)
        self.assertEqual(len(self.events('standing-rulings-written')), 1)
        self.commit()
        self.check_invariants()

    def test_export_rulings_refuses_a_stopped_run_with_a_producer_left(self):
        """run: export-rulings refuses a run paused with a producer still to run, writing nothing,
        and resume finishes it, writing the ruling once (RUN-80)"""
        self.held(ONE + DOWN, defaults='rulings_file="rulings.toml"')
        self.ruling_env(USER='owner')
        self.assertEqual(self.standing('Owner accepts the narrower scope'), 0, self.output)
        self.the_run().request_pause()
        self.assertEqual(self.resume(), 2, self.output)
        run = self.the_run()
        state = run.state
        self.assertEqual(state['status'], 'stopped')
        # The pause comes at a call of the producer's transaction: that refusal comes first.
        self.assertEqual(state['active_producer'], 'down')
        self.assertEqual(self.runner('export-rulings', run.name, '-C', self.root), 2, self.output)
        self.assertIn("work tree ('down' in its transaction", self.output)
        self.assertFalse(os.path.exists(os.path.join(self.root, 'rulings.toml')))
        self.assertEqual(self.the_run().state, state)
        self.assertEqual(self.resume(), 0, self.output)
        self.assertEqual(self.the_run().state['status'], 'done')
        self.assertEqual(len(findings.read_rulings(os.path.join(self.root, 'rulings.toml'))), 1)
        self.assertEqual(len(self.events('standing-rulings-written')), 1)
        self.commit()
        self.check_invariants()

    def test_export_rulings_refuses_a_run_waiting_for_an_approval(self):
        """run: export-rulings refuses a run waiting for an approval with no producer left, since
        a waiting task is not terminal; approve and resume finish it done, writing the ruling once
        (RUN-80, RUN-81)"""
        self.held(ONE + self.LOOK, defaults='rulings_file="rulings.toml"')
        self.ruling_env(USER='owner')
        self.assertEqual(self.standing('Owner accepts the narrower scope'), 0, self.output)
        self.assertEqual(self.resume(), 255, self.output)
        self.refused_export("'look' waiting_human")
        self.assertEqual(self.runner('approve', 'latest', 'look', '-C', self.root), 0, self.output)
        self.assertEqual(self.resume(), 0, self.output)
        self.assertEqual(self.the_run().state['status'], 'done')
        path = os.path.join(self.root, 'rulings.toml')
        self.assertEqual(len(findings.read_rulings(path)), 1)
        self.assertEqual(len(self.events('standing-rulings-written')), 1)
        self.commit()
        self.check_invariants()

    LINT = '\n[[task]]\nid="lint"\ntype="check"\nneeds=["make"]\nrun=["false"]\n'

    def test_export_rulings_waits_for_a_running_check(self):
        """run: export-rulings refuses a run paused inside a standalone check, whose resume would
        restore the check's base over the file; once the check has failed it writes the ruling,
        resume takes the file and its commit, and a replan fixing the check ends done with the
        entry written once (RUN-81)"""
        self.held(ONE + self.LINT, defaults='rulings_file="rulings.toml"')
        self.ruling_env(USER='owner')
        self.assertEqual(self.standing('Owner accepts the narrower scope'), 0, self.output)
        self.the_run().request_pause()
        self.assertEqual(self.resume(), 2, self.output)
        run = self.the_run()
        self.assertEqual(run.state['status'], 'stopped')
        self.assertEqual(run.state['tasks']['lint']['status'], 'running')
        self.assertIsNone(run.state.get('active_producer'))
        self.assertEqual(run.state['intents'], [])
        self.refused_export("'lint' running")
        path = os.path.join(self.root, 'rulings.toml')
        # Finished: the check runs and fails; nothing can touch the tree now.
        self.assertEqual(self.resume(), 2, self.output)
        run = self.the_run()
        self.assertEqual((run.state['status'], run.state['tasks']['lint']['status']),
                         ('failed', 'failed'))
        self.assertEqual(self.events('standing-rulings-written'), [])
        self.assertEqual(self.runner('export-rulings', run.name, '-C', self.root), 0, self.output)
        self.assertIn('1 standing ruling(s) written to rulings.toml; commit it, so the next run '
                      f'reads it; then `runner retry {run.name} lint` starts again from your '
                      'commit', self.output)
        page = (Path(run.path) / 'STATUS.md').read_text()
        self.assertIn('written to the rulings file rulings.toml; commit it, so the next run reads '
                      f'it; then `runner retry {run.name} lint`', page)
        # resume only re-renders: it does not refuse the file, nor its commit, nor undo it.
        self.assertEqual(self.resume(), 2, self.output)
        self.assertNotIn('changed', self.output)
        self.commit()                                       # the owner commits the export
        self.assertEqual(self.resume(), 2, self.output)
        self.assertNotIn('changed', self.output)
        self.assertEqual(self.the_run().state['status'], 'failed')
        self.assertEqual(len(findings.read_rulings(path)), 1)
        self.assertEqual(len(self.events('standing-rulings-written')), 1)
        # A commit of anything else besides is still a moved tip resume refuses.
        self.write('stray.txt', 'x\n')
        self.commit()
        self.assertEqual(self.resume(), 2, self.output)
        self.assertIn('while the run was paused the branch tip changed', self.output)
        self.git_out('reset', '-q', '--hard', 'HEAD~1')
        # replan takes the committed export as no change beyond the definitions (V-04).
        revised = os.path.join(self.side, 'revised.toml')                # outside the repository
        text = Path(self.wf_path).read_text().replace('run=["false"]', 'run=["true"]').replace(
            'name = "demo"\n', f'name = "demo"\nroot = "{self.root}"\n', 1)
        Path(revised).write_text(text)
        self.assertEqual(self.runner('replan', 'latest', '--workflow', revised, '-C', self.root),
                         0, self.output)
        if self.the_run().state['tasks']['lint']['status'] == 'failed':
            self.assertEqual(self.runner('retry', 'latest', 'lint', '-C', self.root), 0,
                             self.output)
        self.assertEqual(self.resume(), 0, self.output)
        self.assertEqual(self.the_run().state['status'], 'done')
        self.assertEqual(len(findings.read_rulings(path)), 1)
        self.assertEqual(len(self.events('standing-rulings-written')), 1)
        self.check_invariants()

    def test_export_steps_follow_the_stop(self):
        """run: the export's next steps and STATUS's ways on come from one helper: a token-capped
        stop names --add-tokens, a failed task its retry, and a done run only the commit (RUN-81)"""
        from codesmith import status
        state = {'status': 'stopped', 'order': ['make', 'down'],
                 'stop_reason': 'the token cap of 100 tokens is reached',
                 'tasks': {'make': {'status': 'accepted'}, 'down': {'status': 'failed'}},
                 'intents': [], 'cleanup_obligations': []}
        self.assertEqual(status.export_live(state), [])
        self.assertEqual(status.export_next_steps('r1', state),
                         'commit it, so the next run reads it; then `runner retry r1 down` starts '
                         'again from your commit (a blocked task after the rulings its STATUS '
                         'names); `runner resume r1 --add-tokens N` ends the run as it stands')
        state['tasks']['down']['status'] = 'pending'
        self.assertEqual(status.export_live(state), ["'down' pending"])
        self.assertIn('finish it: `runner resume r1 --add-tokens N` (after',
                      status.export_ways_on('r1', state))
        state.update(status='done', tasks={'make': {'status': 'accepted'},
                                           'down': {'status': 'accepted'}})
        self.assertEqual(status.export_next_steps('r1', state),
                         'commit it, so the next run reads it')

    def test_a_standing_entry_its_reader_refuses_is_never_queued(self):
        """run: resolve --standing refuses an entry the rulings file's reader would refuse (an
        empty title from a reviewer, a blank --by), naming the field and queueing nothing, and
        the export checks each entry again (RUN-78)"""
        from codesmith import engine
        rid = 'make.review.principled-priya'
        dispute = dict(finding='make/PE-1', action='disputed', note='I ask for a scope exception')
        self.workflow(ONE, defaults='rulings_file="rulings.toml"')
        self.script({'make': [{'write': {'src/a': 'one\n'}, 'answer': done()},
                              {'answer': done('Asked for a ruling', responses=[dispute])}],
                     rid: [{'answer': review([dict(finding(), title='')])},
                           {'answer': review(resolutions=[resolution(status='unresolved')])}]})
        self.assertEqual(self.start(), 255, self.output)
        self.ruling_env(USER='owner')
        before = self.the_run().state
        self.assertEqual(self.standing('Owner accepts the narrower scope'), 2, self.output)
        self.assertIn('standing ruling fields must not be empty: finding_title', self.output)
        self.assertIn('nothing was recorded', self.output)
        self.assertEqual(self.the_run().state, before)
        self.assertNotIn('standing_pending', before)
        self.assertEqual(self.standing('Owner accepts the narrower scope', '--by', ' '), 2, self.output)
        self.assertIn('must not be empty: finding_title, by', self.output)
        self.assertEqual(self.the_run().state, before)
        run = self.the_run()
        run.state['standing_pending'] = [dict(self.entry(), finding_title=' ', run=run.name,
                                              finding='make/PE-1', file='rulings.toml')]
        with self.assertRaises(engine.StandingExportError) as ctx:
            engine.export_queued(run, self.root, 'rulings.toml', lambda where: None)
        self.assertIn('make/PE-1: standing ruling fields must not be empty: finding_title',
                      str(ctx.exception))
        self.assertFalse(os.path.exists(os.path.join(self.root, 'rulings.toml')))

    def test_a_rulings_path_through_a_link_is_refused(self):
        """run: a rulings_file with a symbolic link on its path (the file or a directory, in the
        work tree or on the commit read) is refused at start with the remedy, and the export
        refuses one made later (RUN-79)"""
        from codesmith import engine
        self.write('policy.toml', '')
        os.symlink('policy.toml', os.path.join(self.root, 'rulings.toml'))
        os.makedirs(os.path.join(self.root, 'real'))
        self.write('real/rulings.toml', '')
        os.symlink('real', os.path.join(self.root, 'pol'))
        for name, link in (('rulings.toml', 'rulings.toml'), ('pol/rulings.toml', 'pol')):
            with self.subTest(name=name):
                self.workflow(ONE, defaults=f'rulings_file="{name}"')
                self.assertEqual(self.start(), 2, self.output)
                self.assertIn(f"rulings_file '{name}' passes through a symbolic link ({link}, in "
                              "the work tree)", self.output)
                self.assertIn('name the real file in rulings_file (no link on the way) and commit '
                              'it, or remove rulings_file', self.output)
                self.assertFalse(os.path.exists(os.path.join(self.root, '.runs', 'demo')))
        linked = self.git_out('rev-parse', 'HEAD')
        os.unlink(os.path.join(self.root, 'rulings.toml'))
        self.write('rulings.toml', '')
        self.workflow(ONE, defaults='rulings_file="rulings.toml"')
        wf = workflow.load(self.wf_path, rulings_at=linked)
        self.assertIn("passes through a symbolic link (rulings.toml, committed)", ' '.join(wf.errors))
        self.assertTrue(workflow.load(self.wf_path).ok)
        run = type('R', (), {'state': {'standing_pending': [dict(self.entry(), run='r', finding='make/PE-1',
                                                                   file='pol/rulings.toml')]}})()
        with self.assertRaises(engine.StandingExportError) as ctx:
            engine.export_queued(run, self.root, None, lambda where: None)
        self.assertIn("'pol' is a symbolic link", str(ctx.exception))
        self.assertEqual(Path(self.root, 'real', 'rulings.toml').read_text(), '')

    def test_contract_numeric_evidence(self):
        """fnd: contract gates reject malformed and overflow arguments and bound fast-path evidence (FND-47)"""
        source = self.write('test.cpp', '#include "testing/doctest.hpp"\n')
        good = ("import sys\nif len(sys.argv) == 1: print('DEFAULT PATH'); sys.exit(0)\n"
                "if sys.argv[1] != '10': sys.exit(2)\nprint('latency: 12 ns/op')\n")
        program = self.write('bench.py', good)
        command = [sys.executable, str(ROOT / 'examples/contract_gate.py'), '--source', source,
                   '--program', sys.executable, program]
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('fast path (argument 10) checked; default path not exercised', result.stdout)
        self.assertNotIn('DEFAULT PATH', result.stdout)
        for bad in ('0', 'invalid', '10oops', '-1', '18446744073709551616'):
            Path(program).write_text(good.replace("!= '10'", f"not in ('10', {bad!r})"))
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 1, (bad, result.stdout))
            self.assertIn(f'{bad!r} must be rejected', result.stdout)
        Path(program).write_text('import time\ntime.sleep(20)\n')
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('completion and output not verified', result.stdout)
