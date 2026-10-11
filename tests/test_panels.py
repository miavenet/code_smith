"""Panels through the CLI with real subprocesses in scratch repositories."""
import json
import os
from helpers import EngineCase, done
from test_findings import finding, review, resolution
from codesmith import engine, gitops

ONE='''
[[task]]
id="make"
type="implement"
prompt="Make the work"
outputs=["src/a"]
gate=["test -f src/a"]
reviewers=["principled-priya", "clause-by-clause-chen"]
'''
GOOD={'write':{'src/a':'good\n'},'answer':done()}
PASS={'answer':review()}


class Panels(EngineCase):
    HEADER = EngineCase.HEADER + 'read_only_args = ["--read-only"]\n'
    def setup_panel(self, text=ONE, defaults=''):
        self.workflow(text, defaults=defaults)
        from codesmith import workflow
        wf=workflow.load(self.wf_path)
        self.assertEqual(wf.errors,[])
        self.reviewers=[t['id'] for t in wf.tasks if t['kind']=='review']
        return self.reviewers

    def ledger(self):
        return self.read_json('make','findings.json')

    def count(self, tid):
        try:
            with open(self.script_path+'.'+tid+'.counter') as fh:
                return int(fh.read())
        except FileNotFoundError:
            return 0

    def review_prompt(self,rid,round_no=1):
        with open(self.task_file(rid,f'round-{round_no}','prompt.md')) as fh:
            return fh.read()

    def rule(self, fid='make/PE-1', decision='advisory', note='Owner decision'):
        return self.runner('resolve', 'latest', fid, '--as', decision, '--note', note, '-C', self.root)

    def blocked_for_ruling(self, two=False, gate='test -f src/a'):
        a, b = self.setup_panel(ONE.replace('gate=', 'max_attempts=1\ngate=').replace('test -f src/a', gate))
        self.script({'make': [GOOD], a: [{'answer': review([finding()])}],
                     b: [{'answer': review([finding()])}] if two else [PASS, PASS]})
        self.assertEqual(self.start(), 255, self.output)
        return a, b

    def test_rule_on_exhausted_blocker_and_restore_without_author(self):
        """fnd: a person settles an ordinary exhausted blocker (FND-33)"""
        a, b = self.blocked_for_ruling()
        self.runner('status', '-C', self.root)
        self.assertLess(self.output.index('runner resolve'), self.output.index('runner retry'))
        candidate = self.the_run().state['tasks']['make']['candidate']
        self.assertEqual(self.rule(), 0, self.output)
        ruling = self.ledger()['findings'][0]['history'][-1]
        self.assertTrue(ruling['by'])                         # never recorded as nobody
        self.assertEqual(ruling['candidate'], candidate)
        self.assertEqual(ruling['note'], 'Owner decision')
        self.assertTrue(ruling['at']); self.assertIn('by', ruling)
        self.assertEqual(self.runner('retry', 'latest', 'make', '--apply-patch', '-C', self.root), 0, self.output)
        self.assertEqual(self.resume(), 0, self.output)
        self.assertEqual((self.count('make'), self.count(a), self.count(b)), (1, 1, 1))   # no model call at all
        self.assertEqual(self.status('make'), 'accepted')
        self.assertEqual(self.read_json('make', 'attempt-2', 'result.json')['agent_calls'], 0)
        self.assertTrue(self.read_json('make', 'attempt-2', 'verification.json')['results'])
        self.check_invariants()

    def test_one_ruling_leaves_the_other_blocker(self):
        """fnd: ruling one blocker leaves unrelated blockers open (FND-34)"""
        a, b = self.blocked_for_ruling(two=True)
        self.assertEqual(self.rule(), 0, self.output)
        self.assertEqual(self.resume(), 255, self.output)
        self.script({'make': [GOOD, {'answer': done(responses=[
            dict(finding='make/SC-1', action='fixed', note='Checked')])}],
            a: [{'answer': review([finding()])}],
            b: [{'answer': review([finding()])}, {'answer': review(resolutions=[
                dict(finding='make/SC-1', status='unresolved', note='Still broken')])}]})
        self.assertEqual(self.runner('retry', 'latest', 'make', '--apply-patch', '-C', self.root), 0, self.output)
        self.assertEqual(self.resume(), 255, self.output)
        self.assertEqual(self.status('make'), 'blocked')
        self.assertEqual(self.ledger()['findings'][1]['status'], 'open')
        self.assertEqual(self.count(a), 1)

    def test_ruling_refuses_unknown_closed_running_and_wrong_lineage(self):
        """fnd: rulings refuse unknown closed running or incompatible work (FND-35)"""
        a, b = self.blocked_for_ruling()
        self.assertEqual(self.rule('make/PE-99'), 2)
        self.assertIn('not an open blocking finding', self.output)
        run = self.the_run(); st = run.state['tasks']['make']
        original = st['ledger']['reviewers'][a]['last_seen_candidate']
        st['ledger']['reviewers'][a]['last_seen_candidate'] = 'other'
        run.save()
        self.assertEqual(self.rule(), 2)
        self.assertIn('candidate lineage', self.output)
        st['ledger']['reviewers'][a]['last_seen_candidate'] = original
        st['status'] = 'running'; run.save()
        self.assertEqual(self.rule(), 2)
        self.assertIn('running', self.output)
        st['status'] = 'blocked'; run.save()
        self.assertEqual(self.rule(decision='resolved'), 0, self.output)
        self.assertEqual(self.rule(), 2)
        self.assertIn('not an open blocking finding', self.output)

    def test_upheld_ordinary_blocker_note_reaches_author(self):
        """fnd: an upheld ordinary blocker reaches the next author (FND-36)"""
        a, b = self.blocked_for_ruling()
        self.assertEqual(self.rule(decision='upheld', note='Keep the bound'), 0, self.output)
        self.script({'make': [GOOD, {'write': {'src/a': 'fixed\n'}, 'answer': done(responses=[
            dict(finding='make/PE-1', action='fixed', note='Fixed')])}],
            a: [{'answer': review([finding()])}, {'answer': review(resolutions=[resolution()])}], b: [PASS, PASS]})
        self.assertEqual(self.runner('retry', 'latest', 'make', '--apply-patch', '-C', self.root), 0, self.output)
        self.assertEqual(self.resume(), 0, self.output)
        with open(self.task_file('make', 'attempt-2', 'prompt.md')) as fh:
            self.assertIn('Person ruled upheld: Keep the bound', fh.read())

    def test_ruling_does_not_bypass_gates(self):
        """fnd: restored ruled work still has to pass gates (FND-37)"""
        marker = os.path.join(self.side, 'gate-ok')
        with open(marker, 'w') as fh: fh.write('yes')
        self.blocked_for_ruling(gate='test -f ' + marker)
        self.assertEqual(self.rule(), 0, self.output)
        os.unlink(marker)
        self.assertEqual(self.runner('retry', 'latest', 'make', '--apply-patch', '-C', self.root), 0, self.output)
        self.assertEqual(self.resume(), 2, self.output)
        self.assertNotEqual(self.status('make'), 'accepted')
        self.assertEqual(self.read_json('make', 'attempt-2', 'verification.json')['result'], 'fail')

    def test_partial_ruling_survives_rework_in_review_prompt(self):
        """fnd: later rework shows settled findings to reviewers (FND-38)"""
        a, b = self.blocked_for_ruling(two=True)
        self.assertEqual(self.rule(), 0, self.output)
        self.script({'make': [GOOD, {'write': {'src/a': 'changed\n'}, 'answer': done(responses=[
            dict(finding='make/SC-1', action='fixed', note='Fixed')])}],
            a: [{'answer': review([finding()])}, PASS],
            b: [{'answer': review([finding()])}, {'answer': review(resolutions=[resolution('make/SC-1')])}]})
        self.assertEqual(self.runner('retry', 'latest', 'make', '--apply-patch', '-C', self.root), 0, self.output)
        self.assertEqual(self.resume(), 0, self.output)
        prompt = self.review_prompt(a, 2)
        self.assertIn('settled_by_person', prompt)
        self.assertIn('Owner decision', prompt)
        self.assertIn('Do not re-raise a settled finding', prompt)
        self.assertEqual(len(self.ledger()['findings']), 2)

    def test_ruled_recovery_survives_interruption(self):
        """rec: ruled candidate recovery survives interruption (REC-23)"""
        a, b = self.blocked_for_ruling()
        self.assertEqual(self.rule(), 0, self.output)
        self.assertEqual(self.runner('retry', 'latest', 'make', '--apply-patch', '-C', self.root), 0, self.output)
        class Killed(Exception): pass
        def crash(point):
            if point == 'recover:after-restore': raise Killed()
        self.cli.CRASH = crash
        with self.assertRaises(Killed): self.resume()
        self.cli.CRASH = None
        self.assertEqual(self.resume(), 0, self.output)
        self.assertEqual((self.count('make'), self.count(a)), (1, 1))
        self.check_invariants()

    def test_blocked_finding_can_be_ruled_while_another_task_waits(self):
        """fnd: a blocked task can be ruled while another holds the tree (FND-39)"""
        a, b = self.setup_panel(ONE.replace('gate=', 'max_attempts=1\ngate=') + '\n'
            '[[task]]\nid="other"\ntype="produce"\nprompt="Write b"\n'
            'outputs=["src/b"]\ngate=["test -f src/b"]\n'
            '[[task]]\nid="approval"\ntype="human"\nverifies="other"\n')
        self.script({'make': [GOOD], a: [{'answer': review([finding()])}], b: [PASS],
                     'other': [{'write': {'src/b': 'held\n'}, 'answer': done()}]})
        self.assertEqual(self.start(), 255, self.output)
        self.assertEqual(self.status('make'), 'blocked')
        self.assertEqual(self.the_run().state['active_producer'], 'other')
        self.assertEqual(self.rule(), 0, self.output)
        self.assertEqual(self.ledger()['findings'][0]['status'], 'noted')
        self.assertEqual(self.the_run().state['active_producer'], 'other')
        self.assertEqual(self.count('make'), 1)
        self.check_invariants()

    def test_parallel_panel_passes_and_commits_candidate(self):
        a,b=self.setup_panel()
        self.script({'make':[GOOD], a:[dict(PASS,sleep_s=.05)], b:[PASS]})
        self.assertEqual(self.start(),0,self.output)
        self.assertEqual([self.status(t) for t in ('make',a,b)],['accepted']*3)
        self.assertEqual(self.ledger()['reviewers'][a]['round'],1)
        self.assertIn('Full review',self.review_prompt(a))
        self.check_invariants()

    def test_each_reviewer_is_told_who_else_sits_on_the_panel(self):
        """fnd: the review target lists the other reviewers of the panel with their effective mode
        (FND-31): a persona hands a matter to its owner only when the owner is there, and the
        roster never names the reviewer itself"""
        a,b=self.setup_panel(ONE.replace('"clause-by-clause-chen"','{ perspective = "clause-by-clause-chen", advisory = true }'))
        self.script({'make':[GOOD], a:[PASS], b:[PASS]})
        self.assertEqual(self.start(),0,self.output)
        self.assertIn('"panel": [\n    {\n      "perspective": "clause-by-clause-chen",\n      "mode": "advisory"\n    }\n  ]',
                      self.review_prompt(a))
        self.assertIn('"perspective": "principled-priya",\n      "mode": "blocking"', self.review_prompt(b))
        self.assertNotIn('"perspective": "clause-by-clause-chen"', self.review_prompt(b))
        self.assertIn("the target's `panel` lists who else sits on this panel", self.review_prompt(a))

    def test_consolidated_rework_and_ledger_verdict(self):
        a,b=self.setup_panel()
        responses=[dict(finding='make/'+code+'-1',action='fixed',note='Fixed') for code in ('PE','SC')]
        self.script({'make':[GOOD,{'write':{'src/a':'better\n'},'answer':done(responses=responses)}],
                     a:[{'answer':review([finding()])},{'answer':review(resolutions=[resolution()])}],
                     b:[{'answer':review([finding()])},{'answer':review(resolutions=[resolution('make/SC-1')])}]})
        self.assertEqual(self.start(),0,self.output)
        self.assertEqual(self.count('make'),2)
        self.assertEqual([f['status'] for f in self.ledger()['findings']],['resolved','resolved'])
        with open(self.task_file('make','attempt-2','feedback.md')) as fh:
            feedback=fh.read()
        self.assertIn('make/PE-1',feedback);self.assertIn('make/SC-1',feedback)
        self.assertIn('Judge only the fix',self.review_prompt(a,2))
        self.assertIn('Fixed',self.review_prompt(a,2))
        self.check_invariants()

    def test_protocol_retries_and_blocked_panel(self):
        a,b=self.setup_panel()
        self.script({'make':[GOOD],a:[{'raw_answer':'prose'}]*3,b:[PASS]})
        self.assertEqual(self.start(),255,self.output)
        self.assertEqual(self.count(a),3)
        self.assertEqual(self.count('make'),1)
        self.assertEqual(self.ledger()['findings'],[])
        self.assertEqual(self.status(a),'objected');self.assertEqual(self.status(b),'accepted')
        for n in (1,2,3):
            self.assertTrue(os.path.isdir(self.task_file(a,'round-1',f'invocation-{n}')))
        self.check_invariants()

    def test_protocol_retry_receives_diagnostic_and_keeps_real_blocker(self):
        a, b = self.setup_panel()
        invalid = review([finding()], verdict='pass')
        self.script({'make': [GOOD, {'answer': done(responses=[dict(
            finding='make/PE-1', action='fixed', note='Fixed')])}],
            a: [{'answer': invalid},
                {'answer': review([finding()]), 'match': 'verdict disagrees with ledger: expected block'},
                {'answer': review(resolutions=[resolution()])}], b: [PASS, PASS]})
        self.assertEqual(self.start(), 0, self.output)
        self.assertEqual(self.count(a), 3)
        self.assertEqual(self.count('make'), 2)
        with open(self.task_file(a, 'round-1', 'invocation-2', 'prompt.md')) as fh:
            self.assertIn('verdict disagrees with ledger: expected block', fh.read())
        self.assertEqual(self.ledger()['findings'][0]['status'], 'resolved')
        self.check_invariants()

    def test_escalation_holds_tree_and_advisory_resolution_does_not_rerun(self):
        a,b=self.setup_panel()
        disputed=dict(finding='make/PE-1',action='disputed',note='This is intentional')
        self.script({'make':[GOOD,{'answer':done(responses=[disputed])}],
                     a:[{'answer':review([finding()])},{'answer':review(resolutions=[resolution(status='unresolved')])}],
                     b:[PASS,PASS]})
        self.assertEqual(self.start(),255,self.output)
        self.assertEqual(self.status('make'),'waiting_human');self.check_invariants()
        self.assertEqual(self.runner('resolve','latest','make/PE-1','--as','advisory','-C',self.root),0,self.output)
        self.assertEqual(self.resume(),0,self.output)
        self.assertEqual(self.count('make'),2);self.assertEqual(self.count(a),2)
        self.assertEqual(self.ledger()['findings'][0]['status'],'noted');self.check_invariants()

    def page(self):
        self.runner('status','-C',self.root)
        with open(os.path.join(self.the_run().path,'STATUS.md')) as fh:
            text=fh.read()
        return text, text[text.index('## Next'):]

    def held_with_two_blockers(self, extra=''):
        """A disputed blocker escalates in round 2 while the other reviewer raises an ordinary
        blocker on the rework: the producer is held for a ruling on both."""
        a,b=self.setup_panel(ONE+extra)
        disputed=dict(finding='make/PE-1',action='disputed',note='This is intentional')
        self.script({'make':[GOOD,{'write':{'src/a':'better\n'},'answer':done(responses=[disputed])}],
                     a:[{'answer':review([finding()])},{'answer':review(resolutions=[resolution(status='unresolved')])}],
                     b:[PASS,{'answer':review([finding('src/a:1')])}]})
        self.assertEqual(self.start(),255,self.output)
        self.assertEqual(self.status('make'),'waiting_human')
        return a,b

    def test_a_held_task_says_what_follows_each_ruling(self):
        """run: a task held for rulings says what follows after each one (RUN-48): with an escalated
        and an ordinary blocker, STATUS names both and Next resolves both; after one ruling only
        the other; after the last, "your rulings are complete", `runner resume` continues and no
        approval follows, with neither sign-off text nor `approve`; resume accepts with no call"""
        a,b=self.held_with_two_blockers()
        text,nxt=self.page()
        self.assertIn('**make is held for your ruling** on make/PE-1, make/SC-1: `runner resolve` each one',text)
        self.assertIn('make/PE-1 --as',nxt); self.assertIn('make/SC-1 --as',nxt)
        self.assertEqual(nxt.count('runner resolve'),2)
        self.assertEqual(self.rule('make/PE-1'),0,self.output)
        text,nxt=self.page()
        self.assertIn('**make is held for your ruling** on make/SC-1:',text)
        self.assertEqual(nxt.count('runner resolve'),1); self.assertIn('make/SC-1 --as',nxt)
        self.assertEqual(self.rule('make/SC-1'),0,self.output)
        text,nxt=self.page()
        self.assertIn('**make: your rulings are complete**; `runner resume` continues acceptance; '
                      'no approval follows.',text)
        for absent in ('sign-off','held for your ruling'):
            self.assertNotIn(absent,text)
        self.assertNotIn('runner resolve',nxt); self.assertNotIn('runner approve',nxt)
        self.assertIn('runner resume',nxt)
        self.assertEqual(self.the_run().state['tasks']['make']['reason'],'rulings complete: `runner resume` continues')
        self.assertEqual(self.resume(),0,self.output)
        self.assertEqual((self.count('make'),self.count(a),self.count(b)),(2,2,2))
        self.assertEqual(self.status('make'),'accepted')
        self.check_invariants()

    def test_after_the_rulings_a_human_verifier_still_decides(self):
        """run: after the last ruling of a task a person verifies, STATUS says resume continues up
        to that person (RUN-48); resume then stops there with the sign-off text and approve/reject"""
        a,b=self.held_with_two_blockers('[[task]]\nid="look"\ntype="human"\nverifies="make"\n')
        self.assertEqual(self.rule('make/PE-1'),0,self.output)
        self.assertEqual(self.rule('make/SC-1'),0,self.output)
        text,nxt=self.page()
        self.assertIn('**make: your rulings are complete**; `runner resume` continues acceptance, up to '
                      'the person at look, who approves or rejects it then.',text)
        self.assertNotIn('runner approve',nxt)
        self.assertEqual(self.resume(),255,self.output)
        text,nxt=self.page()
        self.assertIn('**Waiting for your sign-off on make**',text)
        self.assertIn('runner approve',nxt)
        self.assertEqual((self.count('make'),self.count(a),self.count(b)),(2,2,2))

    def test_reviewer_write_fails_and_restores(self):
        a,b=self.setup_panel()
        self.script({'make':[GOOD],a:[dict(PASS,write={'src/a':'tampered\n'})],b:[PASS]})
        self.assertEqual(self.start(),2,self.output)
        self.assertIn('reviewer changed',self.the_run().state['tasks']['make']['reason'])
        self.check_invariants()

    def test_first_review_after_failed_gate_is_full(self):
        a,b=self.setup_panel()
        self.script({'make':[{'answer':done()},GOOD],a:[PASS],b:[PASS]})
        self.assertEqual(self.start(),0,self.output)
        self.assertEqual(self.ledger()['reviewers'][a]['round'],1)
        self.assertIn('Full review',self.review_prompt(a));self.check_invariants()

    def test_attempt_limit_lists_findings_and_retry_supersedes(self):
        a,b=self.setup_panel(ONE.replace('gate=', 'max_attempts=1\ngate='))
        self.script({'make':[GOOD,GOOD],a:[{'answer':review([finding()])},PASS],b:[PASS,PASS]})
        self.assertEqual(self.start(),255,self.output)
        with open(os.path.join(self.the_run().path,'STATUS.md')) as fh:
            self.assertIn('make/PE-1',fh.read())
        self.assertEqual(self.runner('retry','latest','make','-C',self.root),0,self.output)
        self.assertEqual(self.resume(),0,self.output)
        self.assertEqual(self.ledger()['findings'][0]['status'],'superseded')
        self.assertIn('Full review',self.review_prompt(a,2));self.check_invariants()

    def test_retry_with_the_patch_continues_the_line_of_work(self):
        """fnd: retry --apply-patch keeps the panel's open findings (FND-32): the author is told
        what the panel held open, with the reviewer's latest reason for keeping it open, and
        answers those ids; the reviewer's next round judges the rework diff from the candidate
        it last saw instead of a false first sight; a plain retry still starts a new line"""
        a,b=self.setup_panel(ONE.replace('gate=', 'max_attempts=2\ngate='))
        fixed=[dict(finding='make/PE-1',action='fixed',note='Done')]
        kept=dict(finding='make/PE-1',status='unresolved',note='The bound is still there: remove it')
        self.script({'make':[GOOD,{'write':{'src/a':'better\n'},'answer':done(responses=fixed)},
                             {'write':{'src/a':'best\n'},'answer':done(responses=fixed)}],
                     a:[{'answer':review([finding()])},{'answer':review(resolutions=[kept])},
                        {'answer':review(resolutions=[resolution()])}],
                     b:[PASS,PASS,PASS]})
        self.assertEqual(self.start(),255,self.output)
        self.assertEqual(self.status('make'),'blocked')
        self.assertEqual(self.runner('retry','latest','make','--apply-patch','-C',self.root),0,self.output)
        self.assertIn("the panel's open findings, which the author answers first",self.output)
        self.assertEqual(self.resume(),0,self.output)
        with open(self.task_file('make','attempt-3','prompt.md')) as fh:
            prompt=fh.read()
        self.assertIn('Earlier work of this task is already in the work tree',prompt)
        self.assertIn("the review panel's blocking findings are still open",prompt)
        self.assertIn('make/PE-1 [blocking] Fix this',prompt)
        self.assertIn('The reviewer kept it open (round 2): \u201cThe bound is still there: remove it\u201d',prompt)
        self.assertIn('Answer `disputed`, with your reason',prompt)
        self.assertIn('once each: ["make/PE-1"]',prompt)
        self.assertIn('Judge only the fix',self.review_prompt(a,3))
        self.assertEqual([(f['id'],f['status']) for f in self.ledger()['findings']],[('make/PE-1','resolved')])
        self.assertEqual(self.status('make'),'accepted')
        self.assertTrue(any(e['event']=='retry' and e.get('continued') for e in self.events()))
        self.check_invariants()

    def test_read_only_check_that_writes_is_demoted_without_new_attempt(self):
        a,b=self.setup_panel(ONE+'''
[[task]]
id="check"
type="check"
verifies="make"
read_only=true
restores=false
run=["echo litter > src/litter"]
''')
        self.script({'make':[GOOD],a:[PASS,PASS],b:[PASS,PASS]})
        # The demoted writer still litters, so verification sends work back and eventually fails.
        self.assertEqual(self.start(),2,self.output)
        self.assertTrue(self.the_run().state['tasks']['check']['demoted'])
        self.assertTrue(self.read_json(a,'round-1','verdict.json')['void'])
        self.check_invariants()

    def test_upheld_dispute_requires_rework_and_cannot_be_disputed_again(self):
        a,b=self.setup_panel()
        dispute=dict(finding='make/PE-1',action='disputed',note='intentional')
        fixed=dict(finding='make/PE-1',action='fixed',note='changed as directed')
        self.script({'make':[GOOD,{'answer':done(responses=[dispute])},
                             {'write':{'src/a':'fixed\n'},'answer':done(responses=[dispute])},
                             {'answer':done(responses=[fixed])}],
                     a:[{'answer':review([finding()])},
                        {'answer':review(resolutions=[resolution(status='unresolved')])},
                        {'answer':review(resolutions=[resolution()])}], b:[PASS,PASS,PASS]})
        self.assertEqual(self.start(),255,self.output)
        self.assertEqual(self.runner('resolve','latest','make/PE-1','--as','upheld','-C',self.root),0,self.output)
        self.assertEqual(self.resume(),0,self.output)
        self.assertEqual(self.count('make'),4)  # the rejected dispute was only a protocol retry
        self.assertEqual(self.the_run().state['tasks']['make']['attempts_used'],3)
        self.assertEqual(self.ledger()['findings'][0]['status'],'resolved');self.check_invariants()

    def test_diff_spans_intervening_gate_failure_and_answer_is_not_required_twice(self):
        a,b=self.setup_panel(ONE.replace('gate=["test -f src/a"]',
                                        'max_attempts=4\ngate=["grep -q good src/a"]'))
        fixed=dict(finding='make/PE-1',action='fixed',note='fixed before gate failed')
        self.script({'make':[GOOD,{'write':{'src/a':'bad\n'},'answer':done(responses=[fixed])},
                             {'write':{'src/a':'good changed\n'},'answer':done()}],
                     a:[{'answer':review([finding()])},{'answer':review(resolutions=[resolution()])}],
                     b:[PASS,PASS]})
        self.assertEqual(self.start(),0,self.output)
        first=self.read_json(a,'round-1','verdict.json')['candidate']
        second=self.read_json(a,'round-2','verdict.json')
        self.assertEqual(second['base'],first)
        self.assertIn('fixed before gate failed',self.review_prompt(a,2))
        self.assertEqual(self.count(a),2);self.check_invariants()

    def test_passed_reviewer_never_mode_skips_later_rounds(self):
        text=ONE.replace('"clause-by-clause-chen"]','{ perspective="clause-by-clause-chen", recheck_passed="never" }]')
        a,b=self.setup_panel(text)
        fixed=dict(finding='make/PE-1',action='fixed',note='fixed')
        self.script({'make':[GOOD,{'answer':done(responses=[fixed])}],
                     a:[{'answer':review([finding()])},{'answer':review(resolutions=[resolution()])}],b:[PASS]})
        self.assertEqual(self.start(),0,self.output)
        self.assertEqual(self.count(b),1);self.assertEqual(self.count(a),2);self.check_invariants()

    def test_crash_after_panel_application_resumes_without_duplicate_findings_or_calls(self):
        a,b=self.setup_panel()
        self.script({'make':[GOOD],a:[PASS],b:[PASS]})
        class Killed(Exception):pass
        def crash(point):
            if point=='panel:applied':raise Killed()
        self.cli.CRASH=crash
        with self.assertRaises(Killed):self.start()
        self.cli.CRASH=None
        before=self.ledger()
        self.assertEqual(self.resume(),0,self.output)
        self.assertEqual(self.ledger(),before)
        self.assertEqual((self.count('make'),self.count(a),self.count(b)),(1,1,1));self.check_invariants()

    def test_panel_findings_cap_blocks_without_truncation(self):
        a,b=self.setup_panel(defaults='findings_cap_bytes=30')
        self.script({'make':[GOOD],a:[{'answer':review([finding()])}],b:[PASS]})
        self.assertEqual(self.start(),255,self.output)
        self.assertEqual(self.count('make'),1)
        self.assertIn('None is dropped',self.the_run().state['tasks']['make']['reason'])
        self.check_invariants()

    def test_parallel_limit_and_completion_order_do_not_change_ledger_or_feedback(self):
        import threading
        import time
        from codesmith import agents, validate
        text=ONE.replace('"clause-by-clause-chen"]','"clause-by-clause-chen", "dependable-diego"]')
        ids=self.setup_panel(text,defaults='max_parallel=2')
        lock=threading.Lock(); active=0; peak=0; finished=[]; reverse=False
        class Observed(agents.CommandAgent):
            def run(agent,prompt,**kwargs):
                nonlocal active,peak
                is_review=kwargs['schema']==validate.REVIEW
                rid=kwargs['env'].get('CODE_SMITH_TASK')
                if is_review:
                    with lock:active+=1;peak=max(peak,active)
                    position=ids.index(rid)
                    time.sleep(.15 if (position==0) != reverse else .01)
                try:return super().run(prompt,**kwargs)
                finally:
                    if is_review:
                        with lock:active-=1;finished.append(rid)
        self.addCleanup(agents.REGISTRY.__setitem__,'command',agents.CommandAgent)
        agents.REGISTRY['command']=Observed
        outputs=[]
        for run_no in range(2):
            reverse=bool(run_no)
            if run_no:self.git_out('checkout','main')
            steps={'make':[GOOD,{'answer':done(responses=[
                dict(finding='make/'+code+'-1',action='fixed',note='fixed') for code in ('PE','SC','DO')])}]}
            for rid,code in zip(ids,('PE','SC','DO')):
                steps[rid]=[{'answer':review([finding()])},
                            {'answer':review(resolutions=[resolution('make/'+code+'-1')])}]
                counter=self.script_path+'.'+rid+'.counter'
                if os.path.exists(counter):os.unlink(counter)
            counter=self.script_path+'.make.counter'
            if os.path.exists(counter):os.unlink(counter)
            self.script(steps)
            self.assertEqual(self.start(),0,self.output)
            with open(self.task_file('make','attempt-2','feedback.md')) as fh:feedback=fh.read()
            outputs.append((self.ledger(),feedback))
            self.check_invariants()
        self.assertEqual(peak,2)
        self.assertNotEqual(finished[0],finished[6])
        self.assertEqual(outputs[0],outputs[1])

    def test_unrelated_producer_cannot_observe_rejected_helper(self):
        text=ONE.replace('gate=', 'max_attempts=1\ngate=')+'''
[[task]]
id="other"
type="produce"
prompt="Independent work"
outputs=["other"]
gate=["test ! -f src/a"]
'''
        a,b=self.setup_panel(text)
        self.script({'make':[GOOD],a:[{'answer':review([finding()])}],b:[PASS],
                     'other':[{'write':{'other':'independent\n'},'answer':done()}]})
        self.assertEqual(self.start(),255,self.output)
        self.assertEqual(self.status('other'),'accepted')
        self.assertNotIn('src/a',self.git_out('ls-tree','-r','--name-only','HEAD'))
        self.check_invariants()

    def test_crash_after_outcome_accounting_does_not_repeat_review(self):
        a,b=self.setup_panel()
        self.script({'make':[GOOD],a:[PASS],b:[PASS]})
        class Killed(Exception):pass
        def crash(point):
            if point=='panel:outcomes-recorded':raise Killed()
        self.cli.CRASH=crash
        with self.assertRaises(Killed):self.start()
        self.cli.CRASH=None
        spend=self.the_run().state['spend']
        self.assertEqual(self.resume(),0,self.output)
        self.assertEqual((self.count(a),self.count(b)),(1,1))
        self.assertEqual(self.the_run().state['spend'],spend);self.check_invariants()

    def coordinator(self):
        return engine.Engine(self.the_run(), gitops.Git(self.root))

    def test_rejection_after_record_repair_uses_saved_answer(self):
        """rec: rejection after record repair uses the saved answer (REC-54)"""
        import shutil
        from codesmith import record
        a, b = self.setup_panel(defaults='max_parallel=1\nreplay_beside_panel=false')
        bad = {'answer': review(resolutions=[resolution('unknown')])}
        self.script({'make': [GOOD], a: [bad, bad, bad], b: [PASS]})
        class Killed(Exception): pass
        def crash(point):
            if point == 'panel:answer-saved':
                jobs = self.the_run().state['tasks']['make']['panel']['jobs']
                if any(j['task'] == a and 'raw_outcome' in j for j in jobs):
                    raise Killed()
        self.cli.CRASH = crash
        with self.assertRaises(Killed): self.start()
        self.cli.CRASH = None
        run = self.the_run()
        path = run.path
        settled = run.state['spend']['unpriced']['calls']
        shutil.rmtree(path)
        self.assertEqual(self.runner('repair-record', run.name, '-C', self.root), 0, self.output)
        self.assertEqual(self.resume(), 255, self.output)
        self.assertEqual(self.count(a), 3)
        self.assertEqual(self.status('make'), 'blocked')
        rows = [e for e in self.events() if e['event'] == 'outcome' and e.get('task') == a]
        self.assertEqual(len({e['invocation'] for e in rows}), len(rows))
        self.assertEqual(self.the_run().state['spend']['unpriced']['calls'], settled + 3)
        self.assertTrue(any(e['event'] == 'review-diagnostic-missing' for e in self.events()))
        self.assertFalse(record.Run.load(path).state['intents'])
        self.check_invariants()

    def test_reader_guard_violation_survives_answer_checkpoint(self):
        """rec: a reader guard violation survives its answer checkpoint (REC-56)"""
        from unittest.mock import patch
        from codesmith import agents, record
        first = True
        for target in ('state.json', 'verification.json'):
            for killed in (False, True):
                with self.subTest(target=target, killed=killed):
                    if not first: self.setUp()
                    first = False
                    a, b = self.setup_panel(defaults='max_parallel=1\nreplay_beside_panel=false')
                    self.script({'make': [GOOD], a: [PASS], b: [PASS]})
                    original = agents.CommandAgent.run
                    def tamper(agent, prompt, **kwargs):
                        answer = original(agent, prompt, **kwargs)
                        if kwargs['env'].get('CODE_SMITH_TASK') == a:
                            run = self.the_run()
                            path = (os.path.join(run.path, target) if target == 'state.json'
                                    else os.path.join(run.task_dir('make'), 'attempt-1', 'verification.json'))
                            self.assertTrue(os.path.exists(path))
                            with open(path, 'ab') as fh: fh.write(b' ')
                        return answer
                    class Killed(Exception): pass
                    def crash(point):
                        if point == 'panel:answer-saved': raise Killed()
                    if killed: self.cli.CRASH = crash
                    with patch.object(agents.CommandAgent, 'run', tamper):
                        if killed:
                            with self.assertRaises(Killed): self.start()
                        else:
                            self.assertEqual(self.start(), 2, self.output)
                    self.cli.CRASH = None
                    name = ('state.json' if target == 'state.json' else os.path.relpath(os.path.join(
                        self.the_run().task_dir('make'), 'attempt-1', 'verification.json'), self.the_run().path))
                    if killed:
                        run = self.the_run()
                        job = next(j for j in run.state['tasks']['make']['panel']['jobs'] if j['task'] == a)
                        self.assertIn(f'{name} was changed', job['guard_problems'])
                        if target == 'state.json':
                            run.save()
                        else:
                            # The record itself refuses first; once repaired, the saved answer
                            # still carries what its reader did.
                            self.assertEqual(self.resume(), 2, self.output)
                            self.assertIn(f'{name} was changed', self.output)
                            self.assertEqual(self.runner('repair-record', run.name, '-C', self.root), 0,
                                             self.output)
                        self.assertEqual(record.Run.load(run.path).state['tasks']['make']['panel']['jobs'][0]
                                         ['guard_problems'], job['guard_problems'])
                        self.assertEqual(self.resume(), 2, self.output)
                    self.assertEqual(self.status('make'), 'failed')
                    self.assertIn(f'{name} was changed', self.the_run().state['tasks']['make']['reason'])
                    self.assertEqual(self.count(a), 1)
                    self.assertEqual(self.count(b), 0)
                    self.assertNotIn('src/a', self.git_out('ls-tree', '-r', '--name-only', 'HEAD'))
                    if target == 'state.json' or killed:
                        self.check_invariants()

    def test_reader_cancelled_before_release_is_no_interruption(self):
        """proc: a reviewer cancelled before release because its sibling's settlement opened a
        cleanup is settled not-started: no interruption, no try, nothing unsettled (PROC-24)"""
        import threading
        from unittest.mock import patch
        from codesmith import agents
        a, b = self.setup_panel(defaults='max_parallel=2\nreplay_beside_panel=false')
        self.script({'make': [GOOD], a: [PASS], b: [PASS]})
        settled, first = threading.Event(), {}
        call, settle = agents.CommandAgent.run, engine.Engine.settle_reader
        def run(agent, prompt, **kwargs):
            task, on_start = (kwargs.get('env') or {}).get('CODE_SMITH_TASK'), kwargs.get('on_start')
            if task not in (a, b):                        # the author and qualification probes
                return call(agent, prompt, **kwargs)
            def hooked(identity, *rest):
                first.setdefault(task, identity)
                if task == b and 'leader' in identity.get('group', {}):
                    self.assertTrue(settled.wait(60))     # b's leader waits for a's settlement
                return on_start(identity, *rest)
            answer = call(agent, prompt, **dict(kwargs, on_start=hooked))
            if task == a:                                 # the named caller: a's settlement
                answer.cleanup = {'status': 'open', 'error': 'injected cleanup failure of a',
                                  'group': first[a]}
            return answer
        def settled_reader(eng, item, outcome, candidate):
            settle(eng, item, outcome, candidate)
            if item['job']['task'] == a:
                settled.set()
        with patch.object(agents.CommandAgent, 'run', run), \
                patch.object(engine.Engine, 'settle_reader', settled_reader):
            self.assertEqual(self.start(), 2, self.output)
        run_ = self.the_run()
        self.assertEqual(run_.state['status'], 'stopped')
        self.assertEqual(run_.state['intents'], [])
        job = next(j for j in run_.state['tasks']['make']['panel']['jobs'] if j['task'] == b)
        self.assertEqual((job['tries'], job['result']), (0, None))
        for key in ('in_flight', 'raw_outcome', 'interruptions'):
            self.assertNotIn(key, job)
        spend = run_.state['spend']
        self.assertEqual(spend['reserved_usd'], 0)
        self.assertEqual((spend.get('unsettled') or {}).get('calls', 0), 0)
        unpriced = spend['unpriced']['calls']
        self.assertEqual(self.count(b), 0)
        outcomes = [e for e in self.events() if e['event'] == 'outcome' and e.get('task') == b]
        self.assertEqual([(e['status'], e['before_work']) for e in outcomes], [('not-started', True)])
        self.assertEqual(self.resume(), 0, self.output)         # a's group is gone: closed unsignalled
        self.assertEqual((self.count(a), self.count(b)), (1, 1))
        self.assertFalse([e for e in self.events() if e['event'] == 'call-interrupted'])
        self.assertEqual(self.the_run().state['spend']['unpriced']['calls'], unpriced + 1)
        self.assertEqual(self.the_run().state['cleanup_obligations'][0]['cleanup']['status'], 'closed')
        self.assertEqual(self.status('make'), 'accepted')
        self.check_invariants()

    def test_a_violation_found_at_a_cancellation_or_a_fault_is_kept(self):
        """rec: a record change found when a reader is cancelled before release, or when a
        worker faults, is kept in its job before the save that follows, and the resumed panel
        refuses it (REC-62)"""
        import threading
        from unittest.mock import patch
        from codesmith import agents
        first = True
        for branch in ('cancelled', 'fault'):
            with self.subTest(branch=branch):
                if not first: self.setUp()
                first = False
                a, b = self.setup_panel(defaults='max_parallel=2\nreplay_beside_panel=false')
                self.script({'make': [GOOD], a: [PASS], b: [PASS]})
                settled, ids = threading.Event(), {}
                call, settle = agents.CommandAgent.run, engine.Engine.settle_reader
                def tamper():
                    with open(os.path.join(self.the_run().path, 'state.json'), 'ab') as fh:
                        fh.write(b' ')
                def run(agent, prompt, **kwargs):
                    task, on_start = (kwargs.get('env') or {}).get('CODE_SMITH_TASK'), kwargs.get('on_start')
                    if task not in (a, b):
                        return call(agent, prompt, **kwargs)
                    if task == b and branch == 'fault':
                        self.assertTrue(settled.wait(60))
                        tamper()                          # b's worker changed the record, then faults
                        raise RuntimeError('injected fault in b')
                    def hooked(identity, *rest):
                        ids.setdefault(task, identity)
                        if task == b and 'leader' in identity.get('group', {}):
                            self.assertTrue(settled.wait(60))
                            tamper()                      # b changed the record before its release
                        return on_start(identity, *rest)
                    answer = call(agent, prompt, **dict(kwargs, on_start=hooked))
                    if task == a and branch == 'cancelled':
                        answer.cleanup = {'status': 'open', 'error': 'injected cleanup failure of a',
                                          'group': ids[a]}
                    return answer
                def settled_reader(eng, item, outcome, candidate):
                    settle(eng, item, outcome, candidate)
                    if item['job']['task'] == a:
                        settled.set()
                with patch.object(agents.CommandAgent, 'run', run), \
                        patch.object(engine.Engine, 'settle_reader', settled_reader):
                    if branch == 'fault':
                        with self.assertRaises(RuntimeError):
                            self.start()
                    else:
                        self.assertEqual(self.start(), 2, self.output)
                saved = self.the_run()
                kept = [p for j in saved.state['tasks']['make']['panel']['jobs']
                        for p in j.get('guard_problems', [])]
                self.assertIn('state.json was changed', kept)
                self.assertEqual(saved.state_changed(), [])        # the save after it is the runner's
                self.resume()
                self.assertEqual(self.status('make'), 'failed')
                self.assertIn('state.json was changed', self.the_run().state['tasks']['make']['reason'])
                self.assertNotIn('src/a', self.git_out('ls-tree', '-r', '--name-only', 'HEAD'))

    def cancel_with_a_failed_stop(self):
        """PROC-25's stop: reviewer b is cancelled before release because a's settlement opened a
        cleanup, and b's own group stop fails (injected for b's group) while the group lives on.
        Asserts what the stop records and that a plain resume refuses it as an open cleanup;
        returns b, b's group and the unpriced call count."""
        import signal
        import threading
        from unittest.mock import patch
        from codesmith import agents, proc, record
        a, b = self.setup_panel(defaults='max_parallel=2\nreplay_beside_panel=false')
        self.script({'make': [GOOD], a: [PASS], b: [PASS]})
        settled, first = threading.Event(), {}
        call, settle, stop_group = agents.CommandAgent.run, engine.Engine.settle_reader, proc.stop_group
        def run(agent, prompt, **kwargs):
            task, on_start = (kwargs.get('env') or {}).get('CODE_SMITH_TASK'), kwargs.get('on_start')
            if task not in (a, b):
                return call(agent, prompt, **kwargs)
            def hooked(identity, *rest):
                first.setdefault(task, identity)
                if task == b and 'leader' in identity.get('group', {}):
                    self.assertTrue(settled.wait(60))
                    # Held stopped, b's supervisor cannot end by itself once cancelled.
                    os.kill(identity['group']['supervisor']['pid'], signal.SIGSTOP)
                    self.addCleanup(record.stop_process_group, identity, .1, polite=False)
                return on_start(identity, *rest)
            answer = call(agent, prompt, **dict(kwargs, on_start=hooked))
            if task == a:
                answer.cleanup = {'status': 'open', 'error': 'injected cleanup failure of a',
                                  'group': first[a]}
            return answer
        def settled_reader(eng, item, outcome, candidate):
            settle(eng, item, outcome, candidate)
            if item['job']['task'] == a:
                settled.set()
        def stop(process, identity, *args, **kwargs):
            # The named caller: b's own group, never a's or the author's.
            if b in first and identity['group']['pgid'] == first[b]['group']['pgid']:
                raise record.RecordError('injected stop failure of b')
            return stop_group(process, identity, *args, **kwargs)
        with patch.object(agents.CommandAgent, 'run', run), \
                patch.object(engine.Engine, 'settle_reader', settled_reader), \
                patch.object(proc, 'stop_group', side_effect=stop):
            self.assertEqual(self.start(), 2, self.output)
        run_ = self.the_run()
        self.assertEqual((run_.state['status'], run_.state['intents']), ('stopped', []))
        entry = next(e for e in run_.state['cleanup_obligations'] if e['task'] == b)
        group = entry['cleanup']['group']
        self.assertEqual((entry['cleanup']['status'], group['group']['pgid']),
                         ('open', first[b]['group']['pgid']))
        self.assertIn('injected stop failure of b', entry['cleanup']['error'])
        self.assertTrue(record.is_alive(group))
        job = next(j for j in run_.state['tasks']['make']['panel']['jobs'] if j['task'] == b)
        self.assertEqual((job['tries'], job['result']), (0, None))
        self.assertNotIn('in_flight', job)
        self.assertEqual(run_.state['spend']['reserved_usd'], 0)
        [outcome] = [e for e in self.events() if e['event'] == 'outcome' and e.get('task') == b]
        self.assertEqual(entry['op'], outcome['op'])
        self.assertEqual((outcome['status'], outcome['before_work'], outcome['cancelled'],
                          outcome['cleanup']['status']), ('not-started', True, True, 'open'))
        self.assertNotIn('wall_s', outcome)
        self.assertIsNotNone(record._parse_at(outcome['ended_at']))     # the cancel time (V-05)
        self.assertEqual(self.count(b), 0)
        # STATUS names the open cleanup with its remedy, not an interruption (V-03).
        with open(os.path.join(run_.path, 'STATUS.md'), encoding='utf-8') as fh:
            page = fh.read()
        self.assertIn(f"cleanup open for '{b}'", page)
        self.assertIn('--abandon-cleanup', page)
        self.assertNotIn('were interrupted', page)
        # A plain resume refuses it as the open cleanup it is, with PROC-23's remedies.
        self.assertEqual(self.resume(), 2, self.output)
        self.assertIn("an invocation's cleanup is open", self.output)
        self.assertIn('--stop-orphans', self.output)
        self.assertIn('--abandon-cleanup', self.output)
        self.assertNotIn('repair-record', self.output)
        self.assertTrue(record.is_alive(group))
        return b, group, run_.state['spend']['unpriced']['calls']

    def assert_called_once_more(self, b, unpriced):
        outcomes = [e for e in self.events() if e['event'] == 'outcome' and e.get('task') == b]
        self.assertEqual([e.get('cancelled', False) for e in outcomes], [True, False])
        self.assertFalse([e for e in self.events() if e['event'] == 'call-interrupted'])
        self.assertEqual(self.count(b), 1)
        self.assertEqual(self.the_run().state['spend']['unpriced']['calls'], unpriced + 1)
        self.assertEqual(self.the_run().state['spend']['reserved_usd'], 0)
        self.assertEqual(self.status('make'), 'accepted')
        self.check_invariants()

    def test_a_cancelled_reader_whose_stop_fails_is_an_open_cleanup(self):
        """proc: a reviewer cancelled before release whose own group stop fails, the group still
        alive, is an open cleanup obligation: resume refuses it with the cleanup's remedy,
        --stop-orphans that cannot stop it says so, and --abandon-cleanup closes it as a ruling
        (PROC-25)"""
        from unittest.mock import patch
        from codesmith import record
        b, group, unpriced = self.cancel_with_a_failed_stop()
        with patch.object(record, 'stop_process_group', return_value=False):
            self.assertEqual(self.resume('--stop-orphans'), 2, self.output)
        self.assertIn('could not close it', self.output)
        self.assertTrue(record.is_alive(group))
        self.assertEqual(self.resume('--abandon-cleanup', '--by', 'owner'), 0, self.output)
        self.assertTrue(record.is_alive(group))                     # abandoned: never signalled
        entry = next(e for e in self.the_run().state['cleanup_obligations'] if e['task'] == b)
        self.assertEqual((entry['cleanup']['status'], entry['ruling']['by']), ('abandoned', 'owner'))
        self.assertEqual([e['invocation'] for e in self.events() if e['event'] == 'cleanup-abandoned'],
                         [entry['invocation']])
        self.assert_called_once_more(b, unpriced)

    def test_a_cancelled_reader_whose_stop_fails_is_stopped_by_stop_orphans(self):
        """proc: a reviewer cancelled before release whose own group stop fails is ended by
        resume --stop-orphans, which stops its group and closes the cleanup, settling nothing
        again (PROC-25)"""
        from codesmith import record
        b, group, unpriced = self.cancel_with_a_failed_stop()
        self.assertEqual(self.resume('--stop-orphans'), 0, self.output)
        self.assertFalse(record.is_alive(group))
        entry = next(e for e in self.the_run().state['cleanup_obligations'] if e['task'] == b)
        self.assertEqual(entry['cleanup']['status'], 'closed')
        self.assert_called_once_more(b, unpriced)

    def test_a_kept_cancelled_intent_of_an_earlier_runner_is_an_open_cleanup(self):
        """proc: an intent kept cancelled by the first version-3 runner is reconciled as the open
        cleanup this runner records, closed by --abandon-cleanup, settling nothing again
        (PROC-25)"""
        b, group, unpriced = self.cancel_with_a_failed_stop()
        run_ = self.the_run()
        entry = next(e for e in run_.state['cleanup_obligations'] if e['task'] == b)
        [outcome] = [e for e in self.events() if e['event'] == 'outcome' and e.get('task') == b]
        # The earlier runner's shape: the intent kept with its group and what it settles to.
        run_.state['cleanup_obligations'].remove(entry)
        run_.state['intents'].append({
            'op': entry['op'], 'kind': 'agent', 'task': b, 'at': outcome['at'],
            'invocation_dir': entry['invocation'], 'process': group,
            'cancelled': {k: outcome[k] for k in ('status', 'before_work', 'error', 'seconds', 'cancelled')}})
        run_.save()
        self.assertEqual(self.resume('--abandon-cleanup', '--by', 'owner'), 0, self.output)
        self.assertIn('cancelled before release', self.output)
        state = self.the_run().state
        self.assertEqual(state['intents'], [])
        [entry] = [e for e in state['cleanup_obligations'] if e['task'] == b]
        self.assertEqual((entry['op'], entry['cleanup']['status']), (outcome['op'], 'abandoned'))
        carried = [e for e in self.events() if e['event'] == 'outcome' and e.get('task') == b][1]
        self.assertEqual((carried['op'], carried['cancelled'], carried['cleanup']['status']),
                         (outcome['op'], True, 'open'))
        self.assertNotIn('wall_s', carried)
        self.assertFalse([e for e in self.events() if e['event'] == 'call-interrupted'])
        self.assertEqual(self.count(b), 1)
        self.assertEqual(state['spend']['unpriced']['calls'], unpriced + 1)
        self.assertEqual(self.status('make'), 'accepted')
        self.check_invariants()

    def test_a_replay_cancelled_between_gates_keeps_what_ran_and_its_checkout(self):
        """proc: a replay beside the panel cancelled between its gates, its own stop failing and
        its group alive, records the gate that ran with its seconds, keeps its checkout while
        the cleanup is open, and resume --stop-orphans stops the group, then removes the
        checkout (PROC-29)"""
        import signal
        import threading
        from unittest.mock import patch
        from codesmith import agents, checks, proc, record
        a, = self.setup_panel(ONE.replace('reviewers=["principled-priya", "clause-by-clause-chen"]',
                                          'reviewers=["principled-priya"]')
                              .replace('gate=["test -f src/a"]', 'gate=["test -f src/a", "true"]'),
                              defaults='max_parallel=2')
        self.script({'make': [GOOD], a: [PASS]})
        settled, replay_waits, first, gates = threading.Event(), threading.Event(), {}, []
        call, settle, stop_group = agents.CommandAgent.run, engine.Engine.settle_reader, proc.stop_group
        run_commands = checks.run_commands
        def run(agent, prompt, **kwargs):
            task, on_start = (kwargs.get('env') or {}).get('CODE_SMITH_TASK'), kwargs.get('on_start')
            if task != a:
                return call(agent, prompt, **kwargs)
            def hooked(identity, *rest):
                first.setdefault(a, identity)
                return on_start(identity, *rest)
            answer = call(agent, prompt, **dict(kwargs, on_start=hooked))
            self.assertTrue(replay_waits.wait(60))       # a settles once the second gate waits
            answer.cleanup = {'status': 'open', 'error': 'injected cleanup failure of a',
                              'group': first[a]}
            return answer
        def commands(commands, **kwargs):
            on_start = kwargs.get('on_start')
            if 'code-smith-replay-' not in kwargs['cwd'] or on_start is None:
                return run_commands(commands, **kwargs)
            gates.append(kwargs['cwd'])
            def hooked(identity, *rest):
                if len(gates) == 2 and 'leader' in identity.get('group', {}):
                    first.setdefault('replay', identity)
                    replay_waits.set()
                    self.assertTrue(settled.wait(60))
                    # Held stopped, the gate's supervisor cannot end by itself once cancelled.
                    os.kill(identity['group']['supervisor']['pid'], signal.SIGSTOP)
                    self.addCleanup(record.stop_process_group, identity, .1, polite=False)
                return on_start(identity, *rest)
            return run_commands(commands, **dict(kwargs, on_start=hooked))
        def settled_reader(eng, item, outcome, candidate):
            settle(eng, item, outcome, candidate)
            if item['job']['task'] == a:
                settled.set()
        def stop(process, identity, *args, **kwargs):
            # The named caller: the replay's second gate, never a's, the author's or a live gate.
            if 'replay' in first and identity['group']['pgid'] == first['replay']['group']['pgid']:
                raise record.RecordError('injected stop failure of the replay gate')
            return stop_group(process, identity, *args, **kwargs)
        with patch.object(agents.CommandAgent, 'run', run), \
                patch.object(checks, 'run_commands', side_effect=commands), \
                patch.object(engine.Engine, 'settle_reader', settled_reader), \
                patch.object(proc, 'stop_group', side_effect=stop):
            self.assertEqual(self.start(), 2, self.output)
        run_ = self.the_run()
        self.assertEqual(run_.state['status'], 'stopped')
        [replay] = run_.state['intents']
        self.assertEqual((replay['kind'], replay['task']), ('replay', 'make'))
        self.assertTrue(os.path.isdir(replay['dir']))
        entry = next(e for e in run_.state['cleanup_obligations'] if e['task'] == 'make')
        group = entry['cleanup']['group']
        self.assertEqual(entry['cleanup']['status'], 'open')
        self.assertTrue(record.is_alive(group))
        [outcome] = [e for e in self.events() if e['event'] == 'outcome' and e.get('cancelled')]
        self.assertEqual((outcome['op'], outcome['kind'], outcome['result']),
                         (entry['op'], 'command', 'cancelled'))
        self.assertEqual([(r['command'], r['result']) for r in outcome['completed']],
                         [('test -f src/a', 'pass')])
        self.assertEqual(outcome['seconds'], outcome['completed'][0]['seconds'])
        self.assertEqual(run_.state['replay_seconds'], outcome['seconds'])      # added once
        self.assertNotIn('wall_s', outcome)
        self.assertIn('ended_at', outcome)
        with open(os.path.join(run_.path, 'STATUS.md'), encoding='utf-8') as fh:
            page = fh.read()
        self.assertIn("The acceptance replay of 'make' keeps its checkout", page)
        self.assertNotIn('were interrupted', page)
        # A plain resume signals nothing and removes nothing.
        self.assertEqual(self.resume(), 2, self.output)
        self.assertIn("an invocation's cleanup is open", self.output)
        self.assertTrue(os.path.isdir(replay['dir']))
        self.assertTrue(record.is_alive(group))
        self.assertEqual(self.the_run().state['replay_seconds'], outcome['seconds'])
        self.assertEqual(self.resume('--stop-orphans'), 0, self.output)
        self.assertFalse(record.is_alive(group))
        self.assertFalse(os.path.exists(replay['dir']))
        state = self.the_run().state
        self.assertEqual(state['intents'], [])
        self.assertEqual(self.count(a), 1)
        self.assertEqual(self.status('make'), 'accepted')
        self.check_invariants()

    def test_a_check_cancelled_between_commands_keeps_what_ran(self):
        """proc: a check cancelled between its commands because a sibling's settlement opened a
        cleanup records the command that ran with its seconds and ends cancelled, not
        not-started; a cancelled review adds nothing to agent_spans or clock_gap (PROC-26)"""
        import threading
        from unittest.mock import patch
        from codesmith import agents, checks, record
        a, b = self.setup_panel(ONE + '''
[[task]]
id="check"
type="check"
verifies="make"
read_only=true
run=["true", "true"]
''', defaults='max_parallel=3\nreplay_beside_panel=false')
        self.script({'make': [GOOD], a: [PASS], b: [PASS]})
        settled, b_waits, check_waits, first = (threading.Event(), threading.Event(),
                                                threading.Event(), {})
        call, settle = agents.CommandAgent.run, engine.Engine.settle_reader
        cancel, run_commands = engine.Engine.settle_cancelled, checks.run_commands
        def run(agent, prompt, **kwargs):
            task, on_start = (kwargs.get('env') or {}).get('CODE_SMITH_TASK'), kwargs.get('on_start')
            if task not in (a, b):
                return call(agent, prompt, **kwargs)
            def hooked(identity, *rest):
                first.setdefault(task, identity)
                if task == b and 'leader' in identity.get('group', {}):
                    b_waits.set()
                    self.assertTrue(settled.wait(60))
                return on_start(identity, *rest)
            answer = call(agent, prompt, **dict(kwargs, on_start=hooked))
            if task == a:          # a settles once b and the check's second command both wait
                self.assertTrue(b_waits.wait(60) and check_waits.wait(60))
                answer.cleanup = {'status': 'open', 'error': 'injected cleanup failure of a',
                                  'group': first[a]}
            return answer
        def commands(commands, **kwargs):
            on_start, supervisors = kwargs.get('on_start'), []
            if kwargs['env'].get('CODE_SMITH_TASK') != 'check' or on_start is None:
                return run_commands(commands, **kwargs)
            def hooked(identity, *rest):
                if 'leader' not in identity.get('group', {}):
                    supervisors.append(identity)
                    if len(supervisors) == 2:          # the second command, before release
                        check_waits.set()
                        self.assertTrue(settled.wait(60))
                return on_start(identity, *rest)
            return run_commands(commands, **dict(kwargs, on_start=hooked))
        def settled_reader(eng, item, outcome, candidate):
            settle(eng, item, outcome, candidate)
            if item['job']['task'] == a:
                settled.set()
        clocks = []
        def cancelled(eng, item, exc):
            before = json.dumps([eng.run.state.get('agent_spans'), eng.run.state.get('clock_gap')])
            cancel(eng, item, exc)
            clocks.append((item['job']['task'], before,
                           json.dumps([eng.run.state.get('agent_spans'), eng.run.state.get('clock_gap')])))
        with patch.object(agents.CommandAgent, 'run', run), \
                patch.object(checks, 'run_commands', side_effect=commands), \
                patch.object(engine.Engine, 'settle_reader', settled_reader), \
                patch.object(engine.Engine, 'settle_cancelled', cancelled):
            self.assertEqual(self.start(), 2, self.output)
        self.assertEqual(sorted(task for task, _b, _a in clocks), sorted([b, 'check']))
        for task, before, after in clocks:
            self.assertEqual(before, after, task)
        run_ = self.the_run()
        self.assertEqual((run_.state['status'], run_.state['intents']), ('stopped', []))
        job = next(j for j in run_.state['tasks']['make']['panel']['jobs'] if j['task'] == 'check')
        self.assertIsNone(job['result'])
        outcomes = {e['task']: e for e in self.events() if e['event'] == 'outcome' and e.get('cancelled')}
        self.assertEqual(set(outcomes), {b, 'check'})
        for event in outcomes.values():
            self.assertNotIn('wall_s', event)
            self.assertIsNotNone(record._parse_at(event['ended_at']))      # the cancel time (REC-50)
        check = outcomes['check']
        self.assertEqual(check['result'], 'cancelled')
        self.assertEqual([(r['command'], r['result']) for r in check['completed']], [('true', 'pass')])
        self.assertGreater(check['seconds'], 0)
        self.assertEqual(check['seconds'], check['completed'][0]['seconds'])
        with open(self.task_file('check', 'attempt-1', 'gate.log'), encoding='utf-8') as fh:
            log = fh.read()
        self.assertEqual(log.count('$ true'), 2)
        self.assertIn('[cancelled before release: not run]', log)
        self.assertGreaterEqual(run_.state['gate_seconds'], check['seconds'])
        self.assertEqual(self.resume(), 0, self.output)
        self.assertEqual(self.status('make'), 'accepted')
        self.assertEqual((self.count(a), self.count(b)), (1, 1))
        self.check_invariants()

    def test_invocation_records_the_outcome_that_was_used(self):
        """run: the invocation records the outcome that was used (RUN-21)"""
        a, b = self.setup_panel()
        self.script({'make': [GOOD], a: [PASS], b: [PASS]})
        self.assertEqual(self.start(), 0, self.output)
        before = self.read_json(a, 'round-1', 'invocation-1', 'outcome.json')
        self.assertEqual(before['status'], 'ok')
        eng = self.coordinator()
        inv = os.path.relpath(self.task_file(a, 'round-1', 'invocation-1'), eng.run.path)
        diagnostic = ("resolutions must cover exactly this reviewer's open blocking findings, "
                     "each once: required none, so resolutions must be []; supplied 'x'")
        job = {'task': a, 'round': 1, 'tries': 1, 'invocation': inv}
        eng.note_rejected_answer(job, 'make', status='protocol-error', error=diagnostic,
                                 structured=review(resolutions=[resolution('x')]))
        after = self.read_json(a, 'round-1', 'invocation-1', 'outcome.json')
        self.assertEqual(after['status'], 'protocol-error')
        self.assertEqual(after['error'], diagnostic)
        self.assertEqual(after['seconds'], before['seconds'])
        self.check_invariants()

    def test_malformed_sibling_field_does_not_hide_a_readable_finding(self):
        """run: a malformed sibling field does not hide a readable finding (RUN-22)"""
        a, b = self.setup_panel()
        self.script({'make': [GOOD], a: [PASS], b: [PASS]})
        self.assertEqual(self.start(), 0, self.output)
        eng = self.coordinator()
        structured = {'verdict': 'block', 'summary': 'ok', 'resolutions': None,
                     'findings': [finding(), dict(finding(), title='second issue', severity='advisory')]}
        job = {'task': a, 'round': 1, 'tries': 1, 'invocation': None}
        eng.note_rejected_answer(job, 'make', status='protocol-error',
                                 error="answer.resolutions must be a JSON array, not null",
                                 structured=structured)
        entry = eng.st('make')['rejected_reviews'][0]
        self.assertEqual(entry['verdict'], 'block')
        self.assertEqual([f['title'] for f in entry['findings']], ['Fix this', 'second issue'])
        self.assertEqual(entry['unreadable_findings'], 0)
        self.check_invariants()

    def test_no_rejected_answer_or_title_is_omitted(self):
        """run: no rejected answer or title is omitted (RUN-23)"""
        a, b = self.setup_panel()
        self.script({'make': [GOOD], a: [PASS], b: [PASS]})
        self.assertEqual(self.start(), 0, self.output)
        eng = self.coordinator()
        many_findings = [dict(finding(), title=f'issue {n}') for n in range(11)]
        job = {'task': a, 'round': 1, 'tries': 1, 'invocation': None}
        eng.note_rejected_answer(job, 'make', status='protocol-error', error='e',
                                 structured={'verdict': 'block', 'summary': 's', 'resolutions': [],
                                             'findings': many_findings})
        entry = eng.st('make')['rejected_reviews'][0]
        self.assertEqual(len(entry['findings']), 11)
        self.assertEqual([f['title'] for f in entry['findings']], [f'issue {n}' for n in range(11)])
        for n in range(1, 21):
            job = {'task': f'reviewer-{n % 7}', 'round': n, 'tries': 1, 'invocation': None}
            eng.note_rejected_answer(job, 'make', status='protocol-error', error=f'e{n}',
                                     structured={'verdict': 'pass', 'summary': 's',
                                                 'resolutions': [], 'findings': []})
        self.assertEqual(len(eng.st('make')['rejected_reviews']), 21)
        self.check_invariants()

    def test_unreadable_finding_entries_are_counted_not_guessed(self):
        """run: unreadable finding entries are counted, not guessed (RUN-25)"""
        a, b = self.setup_panel()
        self.script({'make': [GOOD], a: [PASS], b: [PASS]})
        self.assertEqual(self.start(), 0, self.output)
        eng = self.coordinator()
        mixed = [finding(), 'a bare string', {'detail': 'no title here'},
                dict(finding(), title='severity is unreadable', severity=7)]
        job = {'task': a, 'round': 1, 'tries': 1, 'invocation': None}
        eng.note_rejected_answer(job, 'make', status='protocol-error', error='e',
                                 structured={'verdict': 'block', 'summary': 's', 'resolutions': [],
                                             'findings': mixed})
        entry = eng.st('make')['rejected_reviews'][0]
        self.assertEqual([f['title'] for f in entry['findings']],
                         ['Fix this', 'severity is unreadable'])
        self.assertEqual([f['severity'] for f in entry['findings']], ['blocking', 'unknown'])
        self.assertEqual(entry['unreadable_findings'], 2)
        self.check_invariants()

    def test_rejected_review_answers_are_summarised_for_the_owner(self):
        """run: rejected review answers are summarised for the owner (RUN-18)"""
        a, b = self.setup_panel()
        self.script({'make': [GOOD], a: [{'answer': review([finding()])}],
                     b: [{'answer': self.COLLIDING}] * 3})
        self.assertEqual(self.start(), 255, self.output)
        self.assertEqual((self.count(a), self.count(b)), (1, 3))
        run = self.the_run()
        self.assertEqual(run.state['tasks']['make']['status'], 'blocked')
        self.assertEqual(self.ledger()['findings'], [])      # nothing below reached the ledger
        with open(self.task_file('make', 'STATUS.md')) as fh:
            status = fh.read()
        self.assertIn('## Rejected review answers', status)
        self.assertIn('Not applied. Nothing below is in the ledger and none of it changed '
                      'acceptance. Read it before you retry: the concerns in it may be real.',
                      status)
        self.assertEqual(status.count('  - blocking: Fix this'), 3)
        self.assertEqual(status.count("  Rejected because: resolutions must cover exactly this "
                                      "reviewer's open blocking findings, each once: required none,"
                                      " so resolutions must be []; supplied 'make/PE-1'. A new "
                                      "finding belongs in findings only, never in resolutions"), 3)
        for n in (1, 2, 3):
            self.assertIn(f'- **{b}**, round 1, try {n} — claimed verdict `block`, 1 finding:',
                          status)
            inv = os.path.relpath(self.task_file(b, 'round-1', f'invocation-{n}'), run.path)
            self.assertIn(f'  Answer: `{inv}/last-message.txt`', status)
            with open(os.path.join(run.path, inv, 'last-message.txt')) as fh:
                self.assertTrue(fh.read())
        self.check_invariants()

    def test_rejected_answers_are_redacted_and_bounded(self):
        """run: rejected answers are redacted and bounded (RUN-20)"""
        from codesmith import proc
        a, b = self.setup_panel()
        self.script({'make': [GOOD], a: [PASS], b: [PASS]})
        self.assertEqual(self.start(), 0, self.output)
        eng = self.coordinator()
        token = 'sk-ant-' + 'a' * 40
        structured = {'verdict': 'block', 'summary': 's', 'resolutions': None,
                      'findings': [dict(finding(), title=token + '\n\t' + 'x' * 500)]}
        job = {'task': a, 'round': 1, 'tries': 2, 'invocation': None}
        eng.note_rejected_answer(job, 'make', status='protocol-error',
                                 error='answer.resolutions must be a JSON array, not null',
                                 structured=structured)
        eng.save()
        title = eng.st('make')['rejected_reviews'][0]['findings'][0]['title']
        self.assertIn(proc.REDACTED.decode(), title)
        self.assertNotIn(token, title)
        self.assertEqual(len(title), 121)
        self.assertTrue(title.endswith('…'))
        for control in ('\n', '\t'):
            self.assertNotIn(control, title)
        with open(self.task_file('make', 'STATUS.md')) as fh:
            before = fh.read()
        self.assertIn(f'  - blocking: {title}', before)
        self.assertNotIn(token, before)
        self.assertEqual(self.runner('status', 'latest', '--rebuild', '-C', self.root), 0,
                         self.output)
        with open(self.task_file('make', 'STATUS.md')) as fh:
            self.assertEqual(fh.read(), before)
        self.check_invariants()

    def fail_on(self, rid, status, error, calls):
        """The listed calls of reviewer `rid` end with `status` and no answer at all, as the
        adapter reports a call that ran past its time limit or died."""
        from unittest import mock
        from codesmith import agents
        original, seen = agents.CommandAgent.run, []
        def run(agent, prompt, **kwargs):
            answer = original(agent, prompt, **kwargs)
            if kwargs['env'].get('CODE_SMITH_TASK') == rid:
                seen.append(rid)
                if len(seen) in calls:
                    answer.status, answer.error, answer.structured = status, error, None
            return answer
        patcher = mock.patch.object(agents.CommandAgent, 'run', run)
        patcher.start(); self.addCleanup(patcher.stop)

    def both_status_files(self):
        run = self.the_run()
        with open(os.path.join(run.path, 'STATUS.md')) as fh:
            run_status = fh.read()
        with open(self.task_file('make', 'STATUS.md')) as fh:
            return run, run_status, fh.read()

    def test_a_protocol_block_is_not_a_substantive_block(self):
        """run: a protocol block is not a substantive block (RUN-19)"""
        a, b = self.setup_panel()
        self.script({'make': [GOOD], a: [{'raw_answer': 'prose'}] * 3, b: [PASS]})
        self.assertEqual(self.start(), 255, self.output)
        self.assertIn('make: blocked (the reviewers could not answer in the required form). '
                      'Its work is in failed.patch', self.output)
        run, status, task_status = self.both_status_files()
        t = run.state['tasks']['make']
        self.assertEqual((t['status'], t['block_kind']), ('blocked', 'protocol'))
        self.assertEqual([(r['reviewer'], r['status'], r['tries']) for r in t['block_reviewers']],
                         [(a, 'protocol-error', 3)])
        self.assertIn('| make | implement | fake | blocked (protocol) |', status)
        self.assertIn('- **make** is blocked: the reviewers could not answer in the required '
                      'form. 3 review answers were rejected and none was applied; they are '
                      f"summarised in tasks/{t['dir']}/STATUS.md.", status)
        self.assertIn('    # make: the reviewers could not answer in the required form.\n'
                      f"    # Read the rejected answers in tasks/{t['dir']}/STATUS.md first.\n"
                      f'    runner retry {run.name} make --apply-patch\n', status)
        self.assertIn('# make — blocked: the reviewers could not answer in the required form\n',
                      task_status)
        self.assertIn('This is a protocol failure of the panel, not a judgement of the work. No '
                      'finding from these rounds reached the ledger.', task_status)
        self.assertIn(f'Why each reviewer did not finish:\n- {a}: could not answer in the '
                      'required form (3 tries).\n', task_status)
        self.check_invariants()

    def test_a_protocol_block_with_no_set_aside_work_offers_a_plain_retry(self):
        """run: a protocol block is not a substantive block (RUN-19, nothing to put back)

        A producer whose declared outputs already exist can finish an attempt without changing a
        file. Its set-aside record holds `paths: []`, and `retry --apply-patch` refuses such a
        task, so "Next" must name the command that works."""
        self.write('src/a', 'good\n')
        a, b = self.setup_panel()
        self.script({'make': [{'answer': done()}], a: [{'raw_answer': 'prose'}] * 3, b: [PASS]})
        self.assertEqual(self.start(), 255, self.output)
        run, status, _task_status = self.both_status_files()
        t = run.state['tasks']['make']
        self.assertEqual((t['status'], t['block_kind']), ('blocked', 'protocol'))
        self.assertEqual(self.read_json('make', 'set-aside.json')['paths'], [])
        self.assertIn('    # make: the reviewers could not answer in the required form.\n'
                      f"    # Read the rejected answers in tasks/{t['dir']}/STATUS.md first.\n"
                      f'    runner retry {run.name} make\n', status)
        self.assertNotIn('--apply-patch', status)
        # The advertised command is the one the runner accepts.
        self.assertEqual(self.runner('retry', 'latest', 'make', '-C', self.root), 0, self.output)
        self.check_invariants()

    def test_a_timeout_is_not_a_malformed_answer(self):
        """run: a timeout is not a malformed answer (RUN-24, the mixed panel)"""
        from codesmith import agents
        a, b = self.setup_panel()
        # A time-out is retried like any provider failure; only a third one is the panel's answer.
        self.script({'make': [GOOD], a: [{'raw_answer': 'prose'}] * 3, b: [PASS] * 3})
        self.fail_on(b, agents.TIMED_OUT, 'no terminal result in time', {1, 2, 3})
        self.assertEqual(self.start(), 255, self.output)
        # The console keeps the whole reason: no reviewer is given another's explanation.
        self.assertIn('make: blocked (review panel could not produce valid answers:', self.output)
        run, status, task_status = self.both_status_files()
        t = run.state['tasks']['make']
        self.assertEqual((t['status'], t['block_kind']), ('blocked', 'mixed'))
        self.assertEqual([(r['reviewer'], r['status']) for r in t['block_reviewers']],
                         [(a, 'protocol-error'), (b, 'timed-out')])
        self.assertIn('| make | implement | fake | blocked (protocol, in part) |', status)
        self.assertIn('- **make** is blocked: one reviewer could not answer in the required form '
                      f'and another did not finish. Per reviewer: {a}: could not answer in the '
                      f'required form; {b}: ran past its time limit. The rejected answers are '
                      f"summarised in tasks/{t['dir']}/STATUS.md.", status)
        self.assertIn('    # make: one reviewer could not answer in the required form; another '
                      'did not finish.\n'
                      f"    # Read the rejected answers in tasks/{t['dir']}/STATUS.md first.\n"
                      f'    runner retry {run.name} make --apply-patch\n', status)
        self.assertIn('# make — blocked: the panel did not finish (one reviewer could not answer '
                      'in the required form)\n', task_status)
        self.assertNotIn('This is a protocol failure of the panel', task_status)
        self.assertIn(f'Why each reviewer did not finish:\n- {a}: could not answer in the '
                      f'required form (3 tries).\n- {b}: ran past its time limit.\n', task_status)
        self.check_invariants()

    def test_a_panel_broken_only_by_a_timeout_is_not_classified(self):
        """run: a timeout is not a malformed answer (RUN-24, the timed-out panel)"""
        from codesmith import agents
        a, b = self.setup_panel()
        self.script({'make': [GOOD], a: [PASS], b: [PASS] * 3})
        self.fail_on(b, agents.TIMED_OUT, 'no terminal result in time', {1, 2, 3})
        self.assertEqual(self.start(), 255, self.output)
        run, status, task_status = self.both_status_files()
        t = run.state['tasks']['make']
        self.assertEqual(t['status'], 'blocked')
        self.assertNotIn('block_kind', t)
        self.assertNotIn('block_reviewers', t)
        self.assertIn('| make | implement | fake | blocked |', status)
        self.assertIn('- **make** is blocked: review panel could not produce valid answers: '
                      f"{b}: no terminal result in time. See tasks/{t['dir']}/STATUS.md.", status)
        self.assertIn(f'    runner retry {run.name} make [--apply-patch]\n', status)
        self.assertIn('# make — blocked\n', task_status)
        for text in (status, task_status):
            self.assertNotIn('required form', text)
            self.assertNotIn('Why each reviewer did not finish', text)
        self.check_invariants()

    def review_job(self, run, rid):
        return next(j for j in run.state['tasks']['make']['panel']['jobs'] if j['task'] == rid)

    def quota_on(self, rid, calls):
        """The listed calls of reviewer `rid` report the provider's quota, which refunds their try."""
        from unittest import mock
        from codesmith import agents
        original, seen = agents.CommandAgent.run, []
        def run(agent, prompt, **kwargs):
            answer = original(agent, prompt, **kwargs)
            if kwargs['env'].get('CODE_SMITH_TASK') == rid:
                seen.append(rid)
                if len(seen) in calls:
                    answer.status, answer.error = agents.QUOTA, 'usage_limit_reached'
            return answer
        patcher = mock.patch.object(agents.CommandAgent, 'run', run)
        patcher.start(); self.addCleanup(patcher.stop)

    def upgrade(self):
        """Make the saved state look like one written before job['invocation'] existed."""
        run = self.the_run()
        for job in run.state['tasks']['make']['panel']['jobs']:
            job.pop('invocation', None)
        run.save()

    def test_answer_refused_at_final_application_is_still_a_rejected_answer(self):
        """fnd: an answer refused at final application is still a rejected answer (FND-28)"""
        from codesmith import agents, record
        a, b = self.setup_panel()
        self.script({'make': [GOOD], a: [{'answer': review([finding()])}]*3, b: [PASS]})
        diagnostic = ("resolutions must cover exactly this reviewer's open blocking findings, each "
                      "once: required none, so resolutions must be []; supplied 'make/SC-1'")
        class Killed(Exception): pass
        def crash(point):
            if point == 'panel:batch-recorded': raise Killed()
        self.cli.CRASH = crash
        before = None
        for n in (1, 2, 3):
            with self.assertRaises(Killed):
                self.start() if n == 1 else self.resume()
            eng = self.coordinator()
            if before is None:
                before = record.dump_json(eng.ledger('make'))
            job = self.review_job(eng.run, a)
            # Collection accepted the answer; the coordinator's final application refuses it.
            self.assertEqual((job['result']['status'], job['tries']), ('ok', n))
            eng.reject_answer(job, 'make', status=agents.PROTOCOL_ERROR, error=diagnostic,
                              structured=job['result']['answer'])
        self.cli.CRASH = None
        self.assertEqual(self.resume(), 255, self.output)
        self.assertEqual((self.count(a), self.count(b), self.count('make')), (3, 1, 1))
        state = self.the_run().state['tasks']['make']
        self.assertEqual(state['status'], 'blocked')
        self.assertIn(diagnostic, state['reason'])
        self.assertNotIn('interrupted calls exhausted protocol retries', state['reason'])
        entries = state['rejected_reviews']
        self.assertEqual([e['try'] for e in entries], [1, 2, 3])
        for n, entry in enumerate(entries, 1):
            inv = self.task_file(a, 'round-1', f'invocation-{n}')
            self.assertEqual(entry['invocation'], os.path.relpath(inv, self.the_run().path))
            self.assertEqual(entry['verdict'], 'block')
            self.assertEqual([f['title'] for f in entry['findings']], ['Fix this'])
            outcome = self.read_json(a, 'round-1', f'invocation-{n}', 'outcome.json')
            self.assertEqual((outcome['status'], outcome['error']), ('protocol-error', diagnostic))
        with open(self.task_file(a, 'round-1', 'invocation-2', 'prompt.md')) as fh:
            self.assertIn(diagnostic, fh.read())
        self.assertEqual(record.dump_json(state['ledger']), before)
        self.check_invariants()

    def test_panel_checkpointed_before_this_change_is_resumed_with_its_answers(self):
        """run: a panel checkpointed before this change is resumed with its answers (RUN-26)"""
        a, b = self.setup_panel()
        malformed = {'answer': {'verdict': 'block', 'summary': 's', 'resolutions': None,
                                'findings': [finding()]}}
        # Call 1 hits the quota (try refunded), call 2 is malformed, call 3 is the next try.
        self.script({'make': [GOOD], a: [PASS, malformed, PASS], b: [PASS]})
        self.quota_on(a, {1, 3})
        self.assertEqual(self.start(), 2, self.output)
        run = self.the_run(); run.state.pop('provider_quota', None); run.save()
        class Killed(Exception): pass
        def crash(point):
            if point == 'panel:outcomes-recorded': raise Killed()
        self.cli.CRASH = crash
        with self.assertRaises(Killed): self.resume()
        self.cli.CRASH = None
        run = self.the_run()
        job = self.review_job(run, a)
        self.assertEqual(job['tries'], 1)                  # charged with the saved answer
        self.assertIn('raw_outcome', job)
        self.upgrade()
        first = self.task_file(a, 'round-1', 'invocation-1', 'outcome.json')
        with open(first, 'rb') as fh: quota_bytes = fh.read()
        ledger = self.read_json('make', 'findings.json') if os.path.exists(
            self.task_file('make', 'findings.json')) else None
        self.assertEqual(self.resume(), 2, self.output)  # the next try hits the quota again
        run = self.the_run(); state = run.state['tasks']['make']
        second = os.path.relpath(self.task_file(a, 'round-1', 'invocation-2'), run.path)
        entries = state['rejected_reviews']
        self.assertEqual([(e['invocation'], e['try']) for e in entries], [(second, 1)])
        self.assertEqual([f['title'] for f in entries[0]['findings']], ['Fix this'])
        self.assertEqual(self.read_json(a, 'round-1', 'invocation-2', 'outcome.json')['status'],
                         'protocol-error')
        with open(first, 'rb') as fh: self.assertEqual(fh.read(), quota_bytes)
        self.assertNotIn('invocation-1', json.dumps(state['rejected_reviews']))
        job = self.review_job(run, a)
        self.assertEqual(job['tries'], 1)
        self.assertEqual(job['invocation'],
                         os.path.relpath(self.task_file(a, 'round-1', 'invocation-3'), run.path))
        self.assertEqual(self.count(a), 3)
        self.assertEqual(state.get('ledger', {}).get('findings', []), [])
        if ledger is not None:
            self.assertEqual(self.read_json('make', 'findings.json'), ledger)
        self.check_invariants()

    def test_recovered_invocation_survives_a_crash_before_the_rejection_is_saved(self):
        """run: a panel checkpointed before this change is resumed with its answers (RUN-26, adopted then killed)"""
        from unittest import mock
        from codesmith import panels
        a, b = self.setup_panel()
        # Adapter-valid, so the ledger check refuses it and its outcome.json is rewritten from ok.
        self.script({'make': [GOOD], a: [{'answer': review([finding()], verdict='pass')}, PASS],
                     b: [PASS]})
        self.quota_on(a, {2})
        class Killed(Exception): pass
        def crash(point):
            if point == 'panel:outcomes-recorded': raise Killed()
        self.cli.CRASH = crash
        with self.assertRaises(Killed): self.start()
        self.cli.CRASH = None
        self.upgrade()
        original, calls = panels.Panels.reject_answer, []
        def killed_before_save(eng, job, producer_id, **kwargs):
            calls.append(dict(job))
            if len(calls) == 1:
                with mock.patch.object(eng, 'save', side_effect=Killed):
                    return original(eng, job, producer_id, **kwargs)
            return original(eng, job, producer_id, **kwargs)
        with mock.patch.object(panels.Panels, 'reject_answer', killed_before_save):
            with self.assertRaises(Killed): self.resume()
            self.assertEqual(self.resume(), 2, self.output)  # the next try hits the quota
        run = self.the_run()
        first = os.path.relpath(self.task_file(a, 'round-1', 'invocation-1'), run.path)
        self.assertEqual([c['invocation'] for c in calls], [first, first])
        entries = run.state['tasks']['make']['rejected_reviews']
        self.assertEqual([(e['invocation'], e['try']) for e in entries], [(first, 1)])
        outcome = self.read_json(a, 'round-1', 'invocation-1', 'outcome.json')
        self.assertEqual(outcome['status'], 'protocol-error')
        self.assertIn('verdict disagrees with ledger', outcome['error'])

    def refused_recovery(self, variant):
        """RUN-27's sequence for one kind of refusal: the highest-numbered invocation's
        outcome.json is `absent`, or present and `mismatched` with the persisted raw_outcome."""
        from unittest import mock
        from codesmith import panels
        malformed = {'answer': {'verdict': 'block', 'summary': 's', 'resolutions': None,
                                'findings': [finding()]}}
        a, b = self.setup_panel()
        self.quota_on(a, {2})
        self.script({'make': [GOOD], a: [malformed, PASS], b: [PASS]})
        class Killed(Exception): pass
        def crash(point):
            if point == 'panel:outcomes-recorded': raise Killed()
        self.cli.CRASH = crash
        with self.assertRaises(Killed): self.start()
        self.cli.CRASH = None
        self.upgrade()
        outcome = self.task_file(a, 'round-1', 'invocation-1', 'outcome.json')
        if variant == 'absent':
            os.unlink(outcome)
        else:
            written = self.read_json(a, 'round-1', 'invocation-1', 'outcome.json')
            with open(outcome, 'wb') as fh:
                fh.write(json.dumps(dict(written, seconds=written['seconds'] + 1)).encode())
            with open(outcome, 'rb') as fh: kept = fh.read()
        # Killed once after the summary was appended and before it was saved: replay.
        original, calls = panels.Panels.reject_answer, []
        def killed_before_save(eng, job, producer_id, **kwargs):
            calls.append(dict(job))
            if len(calls) == 1:
                with mock.patch.object(eng, 'save', side_effect=Killed):
                    return original(eng, job, producer_id, **kwargs)
            return original(eng, job, producer_id, **kwargs)
        with mock.patch.object(panels.Panels, 'reject_answer', killed_before_save):
            with self.assertRaises(Killed): self.resume()
            self.assertEqual(self.resume(), 2, self.output)  # the next try hits the quota
        self.assertEqual(len(calls), 2)
        self.assertEqual([c['invocation'] for c in calls], [None, None])
        self.assertTrue(all('raw_outcome' not in c for c in calls))
        run = self.the_run(); state = run.state['tasks']['make']
        entries = state['rejected_reviews']
        self.assertEqual(len(entries), 1)
        self.assertIsNone(entries[0]['invocation'])
        self.assertEqual(entries[0]['verdict'], 'block')
        self.assertEqual([f['title'] for f in entries[0]['findings']], ['Fix this'])
        self.assertIn('resolutions', entries[0]['error'])
        if variant == 'absent':
            self.assertFalse(os.path.exists(outcome))
        else:
            with open(outcome, 'rb') as fh: self.assertEqual(fh.read(), kept)
        job = self.review_job(run, a)
        self.assertEqual(job['invocation'],
            os.path.relpath(self.task_file(a, 'round-1', 'invocation-2'), run.path))
        self.assertEqual(self.read_json(a, 'round-1', 'invocation-2', 'outcome.json')['status'],
                         'quota')

    def test_refused_recovery_stays_refused_through_the_rejection(self):
        """run: a refused recovery stays refused through the rejection (RUN-27)"""
        self.refused_recovery('mismatched')

    def test_refused_recovery_stays_refused_through_the_rejection_when_outcome_is_absent(self):
        """run: a refused recovery stays refused through the rejection (RUN-27, outcome absent)"""
        self.refused_recovery('absent')

    def events(self):
        with open(os.path.join(self.the_run().path, 'events.jsonl'), encoding='utf-8') as fh:
            return [json.loads(line) for line in fh]

    def test_meaningless_resolution_is_dropped_and_the_finding_is_kept(self):
        """fnd: a meaningless resolution is dropped, the finding is kept (FND-21)"""
        a, b = self.setup_panel()
        by_title = review([finding()], resolutions=[dict(finding='Fix this', status='unresolved', note='n')])
        fixed = dict(finding='make/PE-1', action='fixed', note='Fixed')
        self.script({'make': [GOOD, {'write': {'src/a': 'better\n'}, 'answer': done(responses=[fixed])}],
                     a: [{'answer': by_title}, {'answer': review(resolutions=[resolution()])}],
                     b: [PASS, PASS]})
        self.assertEqual(self.start(), 0, self.output)
        self.assertEqual(self.count(a), 2)
        self.assertFalse(os.path.exists(self.task_file(a, 'round-1', 'invocation-2')))
        with open(self.task_file('make', 'attempt-2', 'feedback.md')) as fh:
            self.assertIn('make/PE-1', fh.read())
        ledger = self.ledger()
        self.assertEqual([(f['id'], f['status']) for f in ledger['findings']], [('make/PE-1', 'resolved')])
        self.assertEqual(self.read_json(a, 'round-1', 'verdict.json')['result']['verdict'], 'block')
        self.assertNotIn('rejected_reviews', self.the_run().state['tasks']['make'])
        self.check_invariants()

    def test_a_repair_is_recorded_where_it_can_be_audited(self):
        """fnd: a repair is recorded where it can be audited (FND-25, event and verdict.json)"""
        a, b = self.setup_panel()
        by_title = review([finding()], resolutions=[dict(finding='Fix this', status='unresolved', note='n')])
        fixed = dict(finding='make/PE-1', action='fixed', note='Fixed')
        self.script({'make': [GOOD, {'write': {'src/a': 'better\n'}, 'answer': done(responses=[fixed])}],
                     a: [{'answer': by_title}, {'answer': review(resolutions=[resolution()])}],
                     b: [PASS, PASS]})
        self.assertEqual(self.start(), 0, self.output)
        ledger = self.ledger()
        self.assertEqual([h['event'] for h in ledger['findings'][0]['history']],
                         ['raised', 'repair', 'response', 'resolution'])
        self.assertEqual(ledger['findings'][0]['history'][1],
                         {'event': 'repair', 'round': 1, 'answer': 1,
                          'kind': 'dropped_resolutions', 'dropped': ['Fix this']})
        repairs = [e for e in self.events() if e['event'] == 'review-repair']
        self.assertEqual(len(repairs), 1)
        self.assertEqual({k: repairs[0][k] for k in ('task', 'producer', 'round', 'kind', 'dropped')},
                         {'task': a, 'producer': 'make', 'round': 1, 'kind': 'dropped_resolutions', 'dropped': 1})
        result = self.read_json(a, 'round-1', 'verdict.json')['result']
        self.assertEqual(result['repair'], {'kind': 'dropped_resolutions',
                                            'dropped': [{'finding': 'Fix this', 'status': 'unresolved'}],
                                            'why': 'the round required no resolutions and no entry named '
                                                   'a finding in the ledger'})
        self.assertEqual(result['answer']['resolutions'], by_title['resolutions'])
        self.check_invariants()

    REPAIRED = ('1 meaningless `resolutions` entry was dropped and the answer was applied. The '
                'entries named no finding in the ledger, and the round required none. '
                'See findings.json.')

    def test_a_repair_is_recorded_in_the_producers_status(self):
        """fnd: a repair is recorded where it can be audited (FND-25, the STATUS line)"""
        a, b = self.setup_panel()
        by_title = review([finding()], resolutions=[dict(finding='Fix this', status='unresolved', note='n')])
        fixed = dict(finding='make/PE-1', action='fixed', note='Fixed')
        self.script({'make': [GOOD, {'write': {'src/a': 'better\n'}, 'answer': done(responses=[fixed])}],
                     a: [{'answer': by_title}, {'answer': review(resolutions=[resolution()])}],
                     b: [PASS, PASS]})
        self.assertEqual(self.start(), 0, self.output)
        with open(self.task_file('make', 'STATUS.md')) as fh:
            status = fh.read()
        self.assertIn('## Repaired review answers', status)
        self.assertEqual(status.count(f'- **{a}**, round 1: {self.REPAIRED}'), 1)
        self.assertNotIn('## Rejected review answers', status)
        self.check_invariants()

    def test_a_second_repair_after_a_retry_keeps_its_own_line(self):
        """fnd: a repair is recorded where it can be audited (FND-25, a repeated repair)

        `retry` supersedes the open findings and clears the reviewers' rounds, so an identical
        answer repaired again on an identical candidate is round 1 for the second time. The two
        answers stay two lines because the `repair` event carries its own discriminator."""
        a, b = self.setup_panel(ONE.replace('gate=', 'max_attempts=1\ngate='))
        by_title = review([finding()], resolutions=[dict(finding='Fix this', status='unresolved', note='n')])
        self.script({'make': [GOOD, GOOD], a: [{'answer': by_title}, {'answer': by_title}],
                     b: [PASS, PASS]})
        self.assertEqual(self.start(), 255, self.output)
        self.assertEqual(self.runner('retry', 'latest', 'make', '-C', self.root), 0, self.output)
        self.assertEqual(self.resume(), 255, self.output)
        ledger = self.ledger()
        self.assertEqual([(f['id'], f['status']) for f in ledger['findings']],
                         [('make/PE-1', 'superseded'), ('make/PE-2', 'open')])
        repairs = [h for f in ledger['findings'] for h in f['history'] if h['event'] == 'repair']
        self.assertEqual([(h['round'], h['answer'], h['dropped']) for h in repairs],
                         [(1, 1, ['Fix this']), (1, 2, ['Fix this'])])
        self.assertEqual(len([e for e in self.events() if e['event'] == 'review-repair']), 2)
        with open(self.task_file('make', 'STATUS.md')) as fh:
            status = fh.read()
        self.assertEqual(status.count(f'- **{a}**, round 1: {self.REPAIRED}'), 2)
        self.check_invariants()

    def repair_lines(self, ledger):
        """The rendered "Repaired review answers" entries of a producer holding this ledger."""
        from codesmith import record
        t = {'status': 'blocked', 'kind': 'produce', 'type': 'implement', 'reason': '',
             'commit': '', 'ledger': ledger}
        status = record.render_task_status('make', t, self.side, 'run')
        return [line for line in status.splitlines() if line.startswith('- **')]

    def test_the_findings_of_one_repaired_answer_are_one_line(self):
        """fnd: a repair is recorded where it can be audited (FND-25, one answer, one line)"""
        from codesmith import findings as ledgers
        from test_findings import R
        junk = [dict(finding='Fix this', status='unresolved', note='n')]
        two = review([finding(), dict(finding(), title='Fix that')], resolutions=junk)
        ledger = ledgers.apply_review(ledgers.empty('make'), R, two, 'C1', {})[0]
        one_line = [f'- **{R["id"]}**, round 1: {self.REPAIRED}']
        self.assertEqual(self.repair_lines(ledger), one_line)
        # The line belongs to the answer, not to its findings: a response, a resolution and a
        # retry are all ordinary transitions of the findings and leave it exactly as it was.
        responses = [dict(finding=f'make/PE-{n}', action='fixed', note='Fixed') for n in (1, 2)]
        ledger = ledgers.respond(ledger, done(responses=responses), 2)
        ledger = ledgers.apply_review(ledger, R, review(resolutions=[resolution('make/PE-1'),
                                                                     resolution('make/PE-2')]), 'C2', {})[0]
        self.assertEqual([f['status'] for f in ledger['findings']], ['resolved', 'resolved'])
        self.assertEqual(self.repair_lines(ledger), one_line)
        self.assertEqual(self.repair_lines(ledgers.restart(ledger)), one_line)

    def test_a_repair_survives_a_colliding_sibling_that_retries_once(self):
        """fnd: a repair is recorded once, not once per pass (FND-25, retry-success)"""
        a, b = self.setup_panel()
        by_title = review([finding()], resolutions=[dict(finding='Fix this', status='unresolved', note='n')])
        responses = [dict(finding='make/'+code+'-1', action='fixed', note='Fixed') for code in ('PE', 'SC')]
        self.script({'make': [GOOD, {'write': {'src/a': 'better\n'}, 'answer': done(responses=responses)}],
                     a: [{'answer': by_title}, {'answer': review(resolutions=[resolution()])}],
                     b: [{'answer': self.COLLIDING},
                         {'answer': review([finding()]), 'match': self.COLLISION},
                         {'answer': review(resolutions=[resolution('make/SC-1')])}]})
        self.assertEqual(self.start(), 0, self.output)  # PE's repair restarts nothing; SC's own retry does
        self.assertEqual((self.count(a), self.count(b)), (2, 3))
        ledger = self.ledger()
        self.assertEqual([f['id'] for f in ledger['findings']], ['make/PE-1', 'make/SC-1'])
        self.assertEqual([h['event'] for h in ledger['findings'][0]['history']],
                         ['raised', 'repair', 'response', 'resolution'])
        repairs = [e for e in self.events() if e['event'] == 'review-repair']
        self.assertEqual(len(repairs), 1)
        self.assertEqual(self.read_json(a, 'round-1', 'verdict.json')['result']['repair']['kind'],
                         'dropped_resolutions')
        self.check_invariants()

    def test_a_repair_is_not_published_when_a_colliding_sibling_exhausts_its_tries(self):
        """fnd: a repair is not published when a colliding sibling exhausts its tries (FND-25, retry-exhaustion)"""
        a, b = self.setup_panel()
        by_title = review([finding()], resolutions=[dict(finding='Fix this', status='unresolved', note='n')])
        self.script({'make': [GOOD], a: [{'answer': by_title}], b: [{'answer': self.COLLIDING}] * 3})
        self.assertEqual(self.start(), 255, self.output)
        self.assertEqual((self.count(a), self.count(b)), (1, 3))
        state = self.the_run().state['tasks']['make']
        self.assertEqual(state['status'], 'blocked')
        self.assertEqual(state.get('ledger', {}).get('findings', []), [])
        self.assertEqual(len(state['rejected_reviews']), 3)
        self.assertEqual([e for e in self.events() if e['event'] == 'review-repair'], [])
        self.check_invariants()

    def test_a_repair_never_produces_a_pass_and_the_rejections_are_summarised(self):
        """fnd: a repair never produces a pass (FND-24, the owner's summary)"""
        a, b = self.setup_panel()
        junk = review(resolutions=[dict(finding='looks fine to me', status='resolved', note='n')],
                      verdict='pass')
        self.script({'make': [GOOD], a: [{'answer': junk}]*3, b: [PASS]})
        self.assertEqual(self.start(), 255, self.output)
        self.assertEqual(self.count(a), 3)
        state = self.the_run().state['tasks']['make']
        self.assertEqual(state['status'], 'blocked')
        self.assertIn('required none, so resolutions must be []', state['reason'])
        entries = state['rejected_reviews']
        self.assertEqual([(e['try'], e['verdict'], e['findings']) for e in entries],
                         [(1, 'pass', []), (2, 'pass', []), (3, 'pass', [])])
        self.assertEqual(state.get('ledger', {}).get('findings', []), [])
        self.check_invariants()

    def test_valid_answers_are_untouched_by_the_repair(self):
        """fnd: valid answers are untouched (FND-26)"""
        a, b = self.setup_panel()
        responses = [dict(finding='make/'+code+'-1', action='fixed', note='Fixed') for code in ('PE', 'SC')]
        self.script({'make': [GOOD, {'write': {'src/a': 'better\n'}, 'answer': done(responses=responses)}],
                     a: [{'answer': review([finding()])}, {'answer': review(resolutions=[resolution()])}],
                     b: [{'answer': review([finding()])}, {'answer': review(resolutions=[resolution('make/SC-1')])}]})
        self.assertEqual(self.start(), 0, self.output)
        self.assertEqual((self.count('make'), self.count(a), self.count(b)), (2, 2, 2))
        state = self.the_run().state['tasks']['make']
        self.assertEqual(state['attempts_used'], 2)
        self.assertNotIn('rejected_reviews', state)
        for f in self.ledger()['findings']:
            self.assertEqual([h['event'] for h in f['history']], ['raised', 'response', 'resolution'])
        self.assertEqual([e for e in self.events() if e['event'] in ('review-repair', 'review-answer-rejected')], [])
        for rid in (a, b):
            for n in (1, 2):
                self.assertNotIn('repair', self.read_json(rid, f'round-{n}', 'verdict.json')['result'])
        self.check_invariants()

    COLLIDING = review([finding()], resolutions=[dict(finding='make/PE-1', status='unresolved', note='n')])
    COLLISION = "required none, so resolutions must be \\[\\]; supplied 'make/PE-1'"

    def collision(self, a, b, run_no=0):
        """PE raises make/PE-1; SC, beside it, blocks and names that id as a resolution. At collection
        the id does not exist yet, so SC is repaired; at final application it does, so SC is refused."""
        if run_no:
            self.git_out('checkout', 'main')
            for tid in ('make', a, b):
                counter = self.script_path+'.'+tid+'.counter'
                if os.path.exists(counter): os.unlink(counter)
        responses = [dict(finding='make/'+code+'-1', action='fixed', note='Fixed') for code in ('PE', 'SC')]
        self.script({'make': [GOOD, {'write': {'src/a': 'better\n'}, 'answer': done(responses=responses)}],
                     a: [{'answer': review([finding()])}, {'answer': review(resolutions=[resolution()])}],
                     b: [{'answer': self.COLLIDING},
                         {'answer': review([finding()]), 'match': self.COLLISION},
                         {'answer': review(resolutions=[resolution('make/SC-1')])}]})

    def collided(self, a, b):
        run = self.the_run(); state = run.state['tasks']['make']
        return self.ledger(), state['status'], state['rejected_reviews'], (
            self.count('make'), self.count(a), self.count(b))

    def test_repair_eligibility_is_decided_by_the_ledger_that_applies_it(self):
        """fnd: repair eligibility is decided by the ledger that applies it (FND-27)"""
        a, b = self.setup_panel()
        self.collision(a, b)
        self.assertEqual(self.start(), 0, self.output)  # no exception escapes the engine
        ledger, status, entries, counts = self.collided(a, b)
        self.assertEqual(status, 'accepted')
        self.assertEqual(counts, (2, 2, 3))  # SC re-called once, inside its existing tries
        self.assertEqual([f['id'] for f in ledger['findings']], ['make/PE-1', 'make/SC-1'])
        self.assertEqual(ledger['next_ids'], {'PE': 1, 'SC': 1})
        with open(self.task_file(b, 'round-1', 'invocation-2', 'prompt.md')) as fh:
            self.assertIn("supplied 'make/PE-1'", fh.read())
        first = os.path.relpath(self.task_file(b, 'round-1', 'invocation-1'), self.the_run().path)
        self.assertEqual([(e['reviewer'], e['try'], e['invocation']) for e in entries], [(b, 1, first)])
        self.assertIn("supplied 'make/PE-1'", entries[0]['error'])
        self.assertEqual(self.read_json(b, 'round-1', 'invocation-1', 'outcome.json')['status'],
                         'protocol-error')
        self.check_invariants()
        # The same run, killed at panel:applied and resumed, reaches the same ledger.
        class Killed(Exception): pass
        kills = []
        def crash(point):
            if point == 'panel:applied' and not kills:
                kills.append(point); raise Killed()
        self.collision(a, b, run_no=1)
        self.cli.CRASH = crash
        with self.assertRaises(Killed): self.start()
        self.cli.CRASH = None
        self.assertEqual([f['id'] for f in self.ledger()['findings']], ['make/PE-1', 'make/SC-1'])
        self.assertEqual(self.resume(), 0, self.output)
        again = self.collided(a, b)
        self.assertEqual(again[:2], (ledger, status))
        self.assertEqual([(e['reviewer'], e['try']) for e in again[2]], [(b, 1)])
        self.assertEqual(again[3], counts)
        self.check_invariants()

    def test_a_coordinator_rejection_survives_replay_exactly_once(self):
        """fnd: a coordinator rejection survives replay exactly once (FND-29)"""
        from unittest import mock
        from codesmith import panels
        a, b = self.setup_panel()
        self.collision(a, b)
        self.assertEqual(self.start(), 0, self.output)
        uninterrupted = self.collided(a, b)
        self.collision(a, b, run_no=1)
        class Killed(Exception): pass
        original, calls = panels.Panels.reject_answer, []
        def killed_before_save(eng, job, producer_id, **kwargs):
            calls.append(dict(job))
            if len(calls) == 1:  # the coordinator's refusal: collection accepted SC's answer
                with mock.patch.object(eng, 'save', side_effect=Killed):
                    return original(eng, job, producer_id, **kwargs)
            return original(eng, job, producer_id, **kwargs)
        with mock.patch.object(panels.Panels, 'reject_answer', killed_before_save):
            with self.assertRaises(Killed): self.start()
            self.assertEqual(self.resume(), 0, self.output)
        self.assertEqual(len(calls), 2)
        self.assertEqual([(c['task'], c['tries'], c['result']['status']) for c in calls],
                         [(b, 1, 'ok'), (b, 1, 'ok')])
        ledger, status, entries, counts = self.collided(a, b)
        run = self.the_run()
        first = os.path.relpath(self.task_file(b, 'round-1', 'invocation-1'), run.path)
        self.assertEqual([(e['reviewer'], e['try'], e['invocation']) for e in entries], [(b, 1, first)])
        self.assertEqual([f['id'] for f in ledger['findings']], ['make/PE-1', 'make/SC-1'])
        self.assertEqual(ledger['next_ids'], {'PE': 1, 'SC': 1})
        self.assertEqual((ledger, status, counts), (uninterrupted[0], uninterrupted[1], uninterrupted[3]))
        self.check_invariants()

    def test_reader_detects_ledger_tampering(self):
        from codesmith import agents,validate
        a,b=self.setup_panel()
        self.script({'make':[GOOD],a:[PASS],b:[PASS]})
        outer=self
        class Tamper(agents.CommandAgent):
            def run(agent,prompt,**kwargs):
                if kwargs['schema']==validate.REVIEW:
                    with open(outer.task_file('make','findings.json'),'w') as fh:fh.write('{}')
                return super().run(prompt,**kwargs)
        self.addCleanup(agents.REGISTRY.__setitem__,'command',agents.CommandAgent)
        agents.REGISTRY['command']=Tamper
        self.assertEqual(self.start(),2,self.output)
        self.assertIn('record was changed',self.the_run().state['tasks']['make']['reason'])
        self.check_invariants()

    def test_a_voided_panel_is_never_replayed_after_a_pause(self):
        """run: a panel a reader wrote during is void durably, before anything can stop the run"""
        a,b=self.setup_panel(ONE+'''
[[task]]
id="check"
type="check"
verifies="make"
read_only=true
restores=false
run=["true"]
''')
        # Reviewer a writes to the tree and, standing in for `runner pause` during the panel,
        # leaves a pause request that stops the recheck of the suspect read-only check.
        tamper={'answer':review(),'write':{'src/a':'tampered\n'},
                'run':['for d in .runs/*/*/; do touch "$d/pause.requested"; done']}
        self.script({'make':[GOOD],a:[tamper],b:[PASS]})
        self.assertEqual(self.start(),2,self.output)
        panel=self.the_run().state['tasks']['make']['panel']
        self.assertEqual([j['task'] for j in panel['jobs'] if 'raw_outcome' in j],[])
        self.assertEqual(panel['void']['suspects'],['check'])
        self.assertTrue(self.read_json(a,'round-1','verdict.json')['void'])
        self.assertEqual(self.resume(),2,self.output)
        self.assertEqual(self.status('make'),'failed')
        self.assertIn('reviewer changed',self.the_run().state['tasks']['make']['reason'])
        self.assertEqual(self.count(a),1)
        self.check_invariants()

    def test_a_rework_blocker_named_as_in_the_diff_counts_when_root_is_a_subdirectory(self):
        """fnd: changed lines match the repository-relative path the review diff shows"""
        import sys
        from helpers import FAKE_AGENT
        from codesmith import workflow
        header=self.HEADER.format(defaults='',python=sys.executable,agent=FAKE_AGENT)
        self.wf_path=self.write('sub/wf.toml',header.replace("'",'"')+ONE.replace(
            'reviewers=["principled-priya", "clause-by-clause-chen"]','reviewers=["principled-priya"]'))
        self.write('sub/.keep','')
        self.commit()
        a=[t['id'] for t in workflow.load(self.wf_path).tasks if t['kind']=='review'][0]
        fixed=dict(finding='make/PE-1',action='fixed',note='Fixed')
        regression=review([finding(location='sub/src/a:1')],[resolution('make/PE-1')])
        self.script({'make':[{'write':{'src/a':'v1\n'},'answer':done()},
                             {'write':{'src/a':'v2 broken\n'},'answer':done(responses=[fixed])},
                             {'write':{'src/a':'v3\n'},'answer':done(responses=[
                                 dict(fixed,finding='make/PE-2')])}],
                     a:[{'answer':review([finding(location='sub/src/a:1')])},{'answer':regression},
                        {'answer':review(resolutions=[resolution('make/PE-2')])}]})
        self.assertEqual(self.start(),0,self.output)
        self.assertIn('+++ b/sub/src/a',self.review_prompt(a,2))
        self.assertEqual([(f['id'],f['severity'],f['status']) for f in self.ledger()['findings']],
                         [('make/PE-1','blocking','resolved'),('make/PE-2','blocking','resolved')])

    def test_changed_locations_mark_deletion_points_and_whole_files(self):
        """fnd: a deletion-only hunk names the lines either side; a bare path only a whole file"""
        from codesmith import findings, panels
        self.write('a.py',''.join(f'line{i}\n' for i in range(1,11)))
        self.write('bin','\0\1')
        self.write('gone','x\n')
        self.commit()
        git=gitops.Git(self.root)
        base=git.tree_of('HEAD')
        self.write('a.py',''.join(f'line{i}\n' for i in range(1,11) if i not in (5,6)))
        self.write('bin','\2\0')
        self.write('new','y\n')
        os.remove(os.path.join(self.root,'gone'))
        self.commit()
        class Coordinator(panels.Panels):
            def __init__(self):
                self.git=git
            def to_root(self,path):
                return path
        changes=Coordinator().changed_locations(base,git.tree_of('HEAD'))
        self.assertEqual([n for n in range(1,12) if findings.inside(f'a.py:{n}',changes)],[4,5])
        self.assertFalse(findings.inside('a.py',changes))
        for path in ('bin','new','gone'):
            self.assertTrue(findings.inside(path,changes),path)

    def test_changed_locations_ignore_the_owners_diff_colour_and_textconv(self):
        """fnd: the owner's color.diff/color.ui and a textconv driver never hide a changed line"""
        import subprocess
        from codesmith import findings, panels
        self.write('a.py',''.join(f'line{i}\n' for i in range(1,11)))
        self.write('.gitattributes','*.py diff=upper\n')
        self.commit()
        for key,value in (('color.ui','always'),('color.diff','always'),
                          ('diff.upper.textconv','tr a-z A-Z <')):
            subprocess.run(['git','-C',self.root,'config',key,value],check=True)
        git=gitops.Git(self.root)
        base=git.tree_of('HEAD')
        self.write('a.py',''.join(f'line{i}\n' if i!=7 else 'seven\n' for i in range(1,11)))
        self.commit()
        class Coordinator(panels.Panels):
            def __init__(self):
                self.git=git
            def to_root(self,path):
                return path
        changes=Coordinator().changed_locations(base,git.tree_of('HEAD'))
        self.assertTrue(findings.inside('a.py:7',changes))
        self.assertFalse(findings.inside('a.py',changes))

    def interrupt_reviewer(self, times):
        """One reviewer that sleeps in its call; the runner is killed during it `times` times."""
        a,=self.setup_panel(ONE.replace('reviewers=["principled-priya", "clause-by-clause-chen"]',
                                        'reviewers=["principled-priya"]'))
        self.script({'make':[GOOD],a:[dict(PASS,sleep_s=.3)]*(times+1)})
        class Killed(BaseException):pass
        def crash(point):
            if point=='reader:running':raise Killed()
        for n in range(times):
            self.cli.CRASH=crash
            with self.assertRaises(Killed):
                self.start() if n==0 else self.resume()
            self.cli.CRASH=None
        return a

    def test_interrupted_reviewer_calls_use_no_try(self):
        """run: a reviewer's calls interrupted three times use no try; resume calls it again and
        the panel decides (RUN-59)"""
        a=self.interrupt_reviewer(3)
        job=[j for j in self.the_run().state['tasks']['make']['panel']['jobs'] if j['kind']=='review'][0]
        self.assertEqual((job['tries'],job['result'],job.get('in_flight')),(0,None,True))
        self.assertEqual(self.resume(),0,self.output)
        with open(os.path.join(self.the_run().path,'events.jsonl')) as fh:
            counted=[e['count'] for e in map(json.loads,fh)
                     if e['event']=='call-interrupted' and e['task']==a]
        self.assertEqual(counted,[1,2,3])
        self.assertEqual(self.status('make'),'accepted')
        self.assertTrue(os.path.isdir(self.task_file(a,'round-1','invocation-4')))
        self.assertFalse(os.path.exists(self.task_file(a,'round-1','invocation-5')))
        self.check_invariants()

    def test_a_crash_while_a_reviewer_runs_leaves_it_for_resume(self):
        """proc: a crash at reader:running comes after the reviewer is released, so resume finds
        it alive and refuses naming its leader; --stop-orphans stops the group and the job counts
        one interruption (PROC-19)"""
        import subprocess, sys
        from helpers import RUNNER
        from codesmith import record
        a,=self.setup_panel(ONE.replace('reviewers=["principled-priya", "clause-by-clause-chen"]',
                                        'reviewers=["principled-priya"]'))
        self.script({'make':[GOOD],a:[dict(PASS,sleep_s=60),PASS]})
        env=dict(os.environ,CODE_SMITH_CRASH_AT='reader:running')
        res=subprocess.run([sys.executable,RUNNER,'start',self.wf_path],env=env,
                           capture_output=True,text=True,timeout=120)
        self.assertEqual(res.returncode,70,res.stderr)
        [intent]=[i for i in self.the_run().state['intents'] if i['kind']=='agent' and i['task']==a]
        self.addCleanup(record.stop_process_group,intent['process'],.1)
        leader=intent['process']['group']['leader']
        self.assertTrue(record.is_alive(leader))
        # The replay gate beside it ends on its own: wait for that, so the one orphan is the reviewer.
        import time
        deadline=time.monotonic()+60
        for it in self.the_run().state['intents']:
            while it['op']!=intent['op'] and record.is_alive(it.get('process')) and time.monotonic()<deadline:
                time.sleep(.05)
        with self.assertRaises(record.OrphanAlive) as ctx:
            record.reconcile(self.the_run(),gitops.Git(self.root))
        self.assertIn(str(leader['pid']),str(ctx.exception))
        self.assertEqual(self.resume(),2,self.output)
        self.assertIn('--stop-orphans',self.output)
        self.assertEqual(self.resume('--stop-orphans'),0,self.output)
        self.assertFalse(record.is_alive(leader))
        with open(os.path.join(self.the_run().path,'events.jsonl')) as fh:
            counted=[e['count'] for e in map(json.loads,fh)
                     if e['event']=='call-interrupted' and e['task']==a]
        self.assertEqual(counted,[1])
        self.assertEqual((self.count(a),self.status('make')),(2,'accepted'))
        self.check_invariants()

    def test_too_many_reviewer_interruptions_block_naming_the_count(self):
        """run: a reviewer whose calls are interrupted five times blocks the producer with the
        count in its reason, never as a protocol failure (RUN-59)"""
        from codesmith import transaction
        a=self.interrupt_reviewer(transaction.MAX_INTERRUPTIONS)
        self.assertEqual(self.resume(),255,self.output)
        st=self.the_run().state['tasks']['make']
        self.assertEqual(st['status'],'blocked')
        self.assertNotIn('block_kind',st['final'])
        self.assertIn(f'{a}: 5 calls were interrupted before they returned (the limit is 5)',
                      st['reason'])
        self.assertTrue(os.path.isdir(self.task_file(a,'round-1','invocation-5')))
        self.assertFalse(os.path.exists(self.task_file(a,'round-1','invocation-6')))   # no sixth call
        self.check_invariants()

    def test_an_unchanged_candidate_failing_the_same_panel_check_is_no_progress(self):
        """run: a failing read-only check in a panel is fingerprinted as verify does"""
        a,b=self.setup_panel(ONE+'''
[[task]]
id="check"
type="check"
verifies="make"
read_only=true
run=["echo checked; exit 3"]
''')
        self.script({'make':[GOOD]*3,a:[PASS]*3,b:[PASS]*3})
        self.assertEqual(self.start(),2,self.output)
        st=self.the_run().state['tasks']['make']
        self.assertEqual((st['status'],st['attempts_used']),('failed',2))
        self.assertIn('no progress',st['reason'])
        self.assertIn('check:fail:3',st['reason'])
        self.assertIn('$ echo checked; exit 3\n[fail, exit 3]\nchecked',st['last_cause'])

    def test_a_runner_fault_in_one_reader_stops_the_run_and_keeps_its_siblings_answers(self):
        """run: a runner fault in one reader's worker is no verdict: the run stops, nothing is
        blocked or spent, and the sibling's paid answer is kept and not asked for again"""
        from unittest import mock
        from codesmith import agents
        a,b=self.setup_panel()
        self.script({'make':[GOOD],a:[PASS],b:[PASS]})
        real=agents.review_call
        def review_call(agent,prompt,**kwargs):
            if kwargs['env']['CODE_SMITH_TASK']==a:
                raise OSError(28,'No space left on device')
            return real(agent,prompt,**kwargs)
        with mock.patch.object(agents,'review_call',review_call):
            self.assertNotIn(self.start(),(0,255),self.output)
        self.assertIn('No space left on device',self.output)
        st=self.the_run().state['tasks']
        self.assertNotIn(st['make']['status'],('blocked','failed'))
        self.assertEqual(self.read_json(b,'round-1','invocation-1','outcome.json')['status'],'ok')
        self.assertEqual(self.count(b),1)
        self.assertEqual(self.resume(),0,self.output)
        self.assertEqual((self.count('make'),self.count(a),self.count(b)),(1,1,1))
        self.check_invariants()

    def test_a_failing_check_worker_does_not_spend_an_author_attempt(self):
        """run: a runner fault while a panel check runs stops the run; it is no `check` failure"""
        from unittest import mock
        from codesmith import checks
        a,b=self.setup_panel(ONE+'''
[[task]]
id="check"
type="check"
verifies="make"
read_only=true
run=["true"]
''')
        self.script({'make':[GOOD,GOOD],a:[PASS,PASS],b:[PASS,PASS]})
        real=checks.run_commands
        def run_commands(commands,**kwargs):
            if kwargs['env']['CODE_SMITH_TASK']=='check':
                raise OSError(28,'No space left on device')
            return real(commands,**kwargs)
        with mock.patch.object(checks,'run_commands',run_commands):
            self.assertNotIn(self.start(),(0,255),self.output)
        self.assertIn('No space left on device',self.output)
        self.assertNotEqual(self.status('check'),'objected')
        self.assertEqual(self.resume(),0,self.output)
        self.assertEqual(self.count('make'),1)
        self.check_invariants()


SPANNED='''
[[task]]
id="first"
type="implement"
prompt="Make b"
outputs=["src/b"]
gate=["test -f src/b"]

[[task]]
id="mid"
type="implement"
prompt="Make c"
needs=["first"]
outputs=["src/c"]
gate=["test -f src/c"]

[[task]]
id="make"
type="implement"
prompt="Make the work"
needs=["mid"]
review_diff_from="first"
outputs=["src/a"]
gate=["test -f src/a"]
reviewers=["principled-priya", "clause-by-clause-chen"]
'''
FIRST={'write':{'src/b':'b\n'},'answer':done()}
MID={'write':{'src/c':'c one\nc two\n'},'answer':done()}


class ReviewDiffFrom(EngineCase):
    HEADER = Panels.HEADER
    setup_panel, ledger, count, review_prompt = (Panels.setup_panel, Panels.ledger, Panels.count,
                                                 Panels.review_prompt)

    def first_tree(self):
        from codesmith import gitops
        return gitops.Git(self.root).tree_of(self.the_run().state['tasks']['first']['commit'])

    def diff_patch(self,rid,round_no):
        with open(self.task_file(rid,f'round-{round_no}','diff.patch')) as fh:
            return fh.read()

    def test_the_first_round_sees_the_cumulative_diff_and_rework_the_rework_diff(self):
        """fnd: review_diff_from gives the panel the cumulative diff (FND-30): round 1 sees the diff
        from the named task's accepted commit, its work and this candidate's, and is told it spans
        from that task; the verdict records that base and the task; the rework round sees the
        rework diff only, and the rework-location rule judges new blockers against it"""
        a,b=self.setup_panel(SPANNED)
        fixed=dict(finding='make/PE-1',action='fixed',note='Fixed')
        late=finding('src/c:1')                  # in the cumulative diff, not in the rework
        self.script({'first':[FIRST],'mid':[MID],
                     'make':[GOOD,{'write':{'src/a':'better\n'},'answer':done(responses=[fixed])}],
                     a:[{'answer':review([finding('src/a:1')])},
                        {'answer':review([late],resolutions=[resolution()],verdict='pass')}],
                     b:[PASS,PASS]})
        self.assertEqual(self.start(),0,self.output)
        commit=self.the_run().state['tasks']['first']['commit']
        for rid in (a,b):
            patch=self.diff_patch(rid,1)
            self.assertIn('+++ b/src/c',patch); self.assertIn('+++ b/src/a',patch)
            self.assertNotIn('src/b',patch)          # first's own work is in its commit
            prompt=self.review_prompt(rid)
            self.assertIn(f"This diff spans from 'first': it starts at the accepted commit {commit[:12]}",prompt)
            self.assertIn(f'"diff_from": {{\n    "task": "first"',prompt)
            verdict=self.read_json(rid,'round-1','verdict.json')
            self.assertEqual(verdict['base'],self.first_tree())
            self.assertEqual(verdict['diff_from'],{'task':'first','commit':commit})
        self.assertNotEqual(self.the_run().state['tasks']['make']['base'],self.first_tree())
        second=self.read_json(a,'round-2','verdict.json')
        self.assertEqual(second['base'],self.read_json(a,'round-1','verdict.json')['candidate'])
        self.assertNotIn('diff_from',second)
        self.assertNotIn('src/c',self.diff_patch(a,2))
        self.assertNotIn('spans from',self.review_prompt(a,2))
        noted=[x for x in self.ledger()['findings'] if x['location']=='src/c:1']
        self.assertEqual([(x['severity'],x['status']) for x in noted],[('advisory','noted')])
        self.check_invariants()

    def test_without_the_key_the_panel_sees_only_its_own_diff(self):
        """fnd: without review_diff_from a panel's first diff starts at its own base (FND-30)"""
        a,b=self.setup_panel(SPANNED.replace('review_diff_from="first"\n',''))
        self.script({'first':[FIRST],'mid':[MID],'make':[GOOD],a:[PASS],b:[PASS]})
        self.assertEqual(self.start(),0,self.output)
        self.assertNotIn('src/c',self.diff_patch(a,1))
        self.assertNotIn('spans from',self.review_prompt(a))
        self.assertNotIn('diff_from',self.read_json(a,'round-1','verdict.json'))

    def test_a_resumed_panel_keeps_the_cumulative_base(self):
        """fnd: a panel killed after its answers were recorded resumes on the same pinned range
        (FND-30): no reviewer is called again and the verdict keeps the named task's base"""
        a,b=self.setup_panel(SPANNED)
        self.script({'first':[FIRST],'mid':[MID],'make':[GOOD],a:[PASS],b:[PASS]})
        class Killed(Exception):pass
        def crash(point):
            if point=='panel:outcomes-recorded':raise Killed()
        self.cli.CRASH=crash
        with self.assertRaises(Killed):self.start()
        self.cli.CRASH=None
        self.assertEqual(self.resume(),0,self.output)
        self.assertEqual((self.count(a),self.count(b)),(1,1))
        self.assertEqual(self.read_json(a,'round-1','verdict.json')['base'],self.first_tree())
        self.check_invariants()

    def test_reopening_the_named_task_reopens_the_reviewed_one(self):
        """fnd: replan --reopen of the task review_diff_from names reopens the task that reviews
        from it (FND-30): it is a need, so the existing closure already includes it"""
        from codesmith import replan
        self.setup_panel(SPANNED)
        self.script({'first':[FIRST],'mid':[MID],'make':[GOOD],self.reviewers[0]:[PASS],self.reviewers[1]:[PASS]})
        self.assertEqual(self.start(),0,self.output)
        run=self.the_run()
        from codesmith import record
        before=record.read_json(os.path.join(run.path,'workflow.expanded.json'))
        self.assertEqual(replan._closure(run,before,{'first'}),{'first','mid','make',*self.reviewers})
