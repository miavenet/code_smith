"""BUD-01..05, BUD-12 and FAIL-05: reservations, unpriced calls and durable budget pauses."""
import copy
import unittest
import glob
import json
import os
from unittest.mock import patch
from helpers import EngineCase, done
from test_panels import ONE, GOOD, PASS
from codesmith import agents, budgets, engine, qualification, record, schema, validate, workflow


class Priced(agents.CommandAgent):
    reports_cost = True

    def interpret(self, res):
        result=super().interpret(res)
        result.cost_usd=0.0 if result.structured and 'value' in result.structured else 1.0
        return result


class Budgets(EngineCase):
    HEADER = EngineCase.HEADER + 'read_only_args = ["--read-only"]\n'

    def setup_budget(self, amount, tasks=ONE):
        self.workflow(tasks,defaults=f'run_budget_usd={amount}\nbudget_usd=5')
        reviewers=[t['id'] for t in workflow.load(self.wf_path).tasks if t['kind']=='review']
        self.script({'make':[GOOD],**{rid:[PASS] for rid in reviewers}})
        self.addCleanup(agents.REGISTRY.__setitem__,'command',agents.CommandAgent)
        agents.REGISTRY['command']=Priced
        return reviewers

    def count(self,tid):
        try:
            with open(self.script_path+'.'+tid+'.counter') as fh:return int(fh.read())
        except FileNotFoundError:return 0

    def test_reservations_prevent_dispatch_and_resume_preserves_completed_review(self):
        a,b=self.setup_budget(6)
        self.assertEqual(self.start(),2,self.output)
        run=self.the_run()
        self.assertEqual(run.state['status'],'stopped')
        self.assertEqual(run.state['active_producer'],'make')
        self.assertEqual((self.count('make'),self.count(a),self.count(b)),(1,1,0))
        self.assertEqual(run.state['spend']['known_usd'],2)
        self.assertEqual(run.state['spend']['reserved_usd'],0)
        self.check_invariants()
        self.assertEqual(self.resume('--add-budget','20'),0,self.output)
        self.assertEqual((self.count('make'),self.count(a),self.count(b)),(1,1,1))
        self.assertEqual(self.the_run().state['run_budget_usd'],26)
        with open(os.path.join(run.path,'events.jsonl')) as fh:
            self.assertIn('budget-added',fh.read())
        self.check_invariants()

    def test_one_dollar_remaining_starts_no_five_dollar_review(self):
        a,b=self.setup_budget(5)
        original = Priced.interpret
        def four_dollar_author(agent, res):
            result = original(agent, res)
            if result.structured and 'outcome' in result.structured:
                result.cost_usd = 4
            return result
        with patch.object(Priced, 'interpret', four_dollar_author):
            self.assertEqual(self.start(),2,self.output)
        self.assertEqual((self.count(a),self.count(b)),(0,0))
        self.assertEqual(self.the_run().state['spend']['known_usd'],4)
        self.check_invariants()

    def test_budget_before_first_producer_call_uses_no_attempt(self):
        self.setup_budget(1)
        self.assertEqual(self.start(),2,self.output)
        st=self.the_run().state['tasks']['make']
        self.assertEqual(st['attempts_used'],0)
        self.assertEqual(self.count('make'),0)
        self.assertEqual(self.resume('--add-budget','20'),0,self.output)
        self.assertEqual(self.the_run().state['tasks']['make']['attempts'],1)
        self.check_invariants()

    def test_budget_increase_requires_unchanged_paused_tree(self):
        self.setup_budget(5)
        self.assertEqual(self.start(),2,self.output)
        self.write('src/a','changed by owner\n')
        self.assertEqual(self.resume('--add-budget','20'),2,self.output)
        self.assertIn('work tree changed',self.output)
        self.assertEqual(self.the_run().state['run_budget_usd'],5)
        for amount in ('-1','nan','inf'):
            self.assertEqual(self.resume('--add-budget',amount),2)

    def test_interrupted_priced_call_keeps_its_reservation_as_unsettled_spend(self):
        """bud: a priced call that ends without a cost is never settled at $0 (BUD-05)"""
        self.setup_budget(20)
        class Killed(Exception):pass
        def crash(point):
            if point=='agent:running':raise Killed()
        self.cli.CRASH=crash
        with self.assertRaises(Killed):self.start()
        run=self.the_run()
        self.assertEqual(run.state['spend']['reserved_usd'],5)
        from codesmith import gitops
        record.reconcile(run,gitops.Git(self.root),stop_orphans=True,grace_s=.1)
        self.assertEqual(run.state['spend']['reserved_usd'],0)
        self.assertEqual(run.state['spend']['unsettled'],{'usd':5,'calls':1,'tokens_in':0,'tokens_out':0})
        self.assertEqual(run.state['spend']['unpriced']['unknown_calls'],0)
        self.assertEqual(budgets.unsettled_usd(run.state),5)
        self.assertFalse(budgets.fits(run.state,15.01))               # the $5 still counts
        record.reconcile(run,gitops.Git(self.root))
        self.assertEqual(run.state['spend']['unsettled']['calls'],1)

    def test_unsettled_spend_is_shown_in_status_and_the_runs_listing(self):
        """bud: dollars held for a priced call that ended without a cost count against the limit,
        so STATUS.md and `runner runs` show them; a run without any shows nothing new"""
        self.setup_budget(20)
        class Killed(Exception):pass
        def crash(point):
            if point=='agent:running':raise Killed()
        self.cli.CRASH=crash
        with self.assertRaises(Killed):self.start()
        self.cli.CRASH=None
        run=self.the_run()
        with open(os.path.join(run.path,'STATUS.md')) as fh:
            self.assertNotIn('unsettled',fh.read())
        from codesmith import gitops
        record.reconcile(run,gitops.Git(self.root),stop_orphans=True,grace_s=.1)
        with open(os.path.join(run.path,'STATUS.md')) as fh:
            self.assertIn('$0.00 reserved, $5.00 unsettled (1 call ended without a price).',fh.read())
        self.assertEqual(self.runner('runs',self.wf_path),0,self.output)
        self.assertIn('(+$5.00 unsettled)',self.output)

    def test_interrupted_call_with_a_provider_record_is_not_unknown_usage(self):
        """bud: the usage of an interrupted call is read from the provider's record (BUD-07)"""
        self.setup_budget(20)
        class Killed(Exception):pass
        def crash(point):
            if point=='agent:running':raise Killed()
        self.cli.CRASH=crash
        with self.assertRaises(Killed):self.start()
        run=self.the_run()
        it=next(i for i in run.state['intents'] if i['kind']=='agent')
        self.assertEqual(it['agent_kind'],'command')
        from codesmith import gitops
        with patch.object(agents,'partial_usage',return_value={'tokens_in':1234,'tokens_out':56}) as reader:
            lines=record.reconcile(run,gitops.Git(self.root),stop_orphans=True,grace_s=.1)
        self.assertEqual(reader.call_args.args[0],'command')
        self.assertAlmostEqual(reader.call_args.args[2],agents._epoch(it['at']))
        # A priced agent: its tokens stay with its unsettled dollars, off the unpriced stop line.
        unsettled=run.state['spend']['unsettled']
        self.assertEqual((unsettled['usd'],unsettled['tokens_in'],unsettled['tokens_out']),(5,1234,56))
        self.assertEqual(budgets.tokens_used(run.state),0)
        self.assertEqual(run.state['spend']['reserved_usd'],0)
        self.assertTrue(any('it had used 1234 tokens in, 56 out' in l for l in lines),lines)
        outcome=record.read_json(os.path.join(run.path,it['invocation_dir'],'outcome.json'))
        self.assertEqual((outcome['status'],outcome['usage_source']),('interrupted','provider-record'))
        self.cli.CRASH=None
        self.assertEqual(self.resume(),0,self.output)
        self.check_invariants()

    def test_unpriced_usage_is_not_invented_dollars(self):
        self.workflow(ONE.replace('reviewers=["principled-priya", "clause-by-clause-chen"]',''))
        self.script([GOOD])
        self.assertEqual(self.start(),0,self.output)
        state=self.the_run().state
        self.assertEqual(state['spend']['known_usd'],0)
        self.assertGreaterEqual(state['spend']['unpriced']['calls'],1)  # includes doctor probes
        state=copy.deepcopy(state)
        budgets.settle(state,'make',0,agents.AgentResult(agents.OK,usage={'tokens_in':17,'tokens_out':3}))
        self.assertEqual(state['spend']['known_usd'],0)
        self.assertEqual(state['spend']['unpriced']['tokens_in'],17)

    def test_priced_calls_without_a_cost_still_count_against_the_dollar_limit(self):
        """bud: a priced call that timed out is held at its cap, never at $0 (BUD-05)"""
        state={'run_budget_usd':10.0,'seconds':0,'tasks':{'r':{'cost_usd':0.0}},
               'spend':{'known_usd':0.0,'reserved_usd':0.0,          # a run saved before 'unsettled'
                        'unpriced':{'calls':0,'unknown_calls':0,'tokens_in':0,'tokens_out':0}}}
        started=0
        while budgets.fits(state,5.0) and started<6:
            state['spend']['reserved_usd']+=5.0
            budgets.settle(state,'r',5.0,agents.AgentResult(agents.TIMED_OUT,seconds=600,
                           usage={'tokens_in':900,'tokens_out':20},usage_source='provider-record'))
            started+=1
        self.assertEqual(started,2)
        self.assertEqual(state['spend']['unsettled'],{'usd':10.0,'calls':2,'tokens_in':1800,'tokens_out':40})
        self.assertEqual((state['spend']['known_usd'],budgets.tokens_used(state)),(0.0,0))
        budgets.settle(state,'r',0,agents.AgentResult(agents.QUOTA,before_work=True))     # refused: nothing spent
        budgets.settle(state,'r',5.0,agents.AgentResult(agents.QUOTA,before_work=True))
        self.assertEqual(state['spend']['unsettled']['calls'],2)

    def test_a_failed_call_keeps_the_usage_it_was_seen_to_use(self):
        """bud: an outage or a quota refusal after work began keeps its usage (BUD-08): a
        priced call keeps its reservation as unsettled with its tokens, an unpriced one adds its
        tokens to the stop line; only a proven pre-work refusal without usage counts as nothing spent"""
        def fresh():
            return {'run_budget_usd':10.0,'run_budget_tokens':20000,'seconds':0,'tasks':{'r':{'cost_usd':0.0}},
                    'spend':{'known_usd':0.0,'reserved_usd':5.0,
                             'unpriced':{'calls':0,'unknown_calls':0,'tokens_in':0,'tokens_out':0}}}
        used={'tokens_in':10000,'tokens_out':500}
        for status in (agents.ENVIRONMENT,agents.QUOTA):
            with self.subTest(status=status):
                state=fresh()
                budgets.settle(state,'r',5.0,agents.AgentResult(status,usage=dict(used),
                                                                usage_source='provider-record'))
                self.assertEqual(state['spend']['unsettled'],{'usd':5.0,'calls':1,'tokens_in':10000,'tokens_out':500})
                self.assertEqual(state['spend']['reserved_usd'],0)
                self.assertFalse(budgets.fits(state,5.01))
                state=fresh()
                budgets.settle(state,'r',0,agents.AgentResult(status,usage=dict(used),
                                                              usage_source='provider-record'))
                self.assertEqual(budgets.tokens_used(state),10500)
                self.assertEqual(state['spend']['unpriced']['calls'],1)
                self.assertFalse(budgets.fits_tokens(dict(state,run_budget_tokens=10500),Metered('m',{})))
                state=fresh()                                  # refused before any work
                budgets.settle(state,'r',5.0,agents.AgentResult(status,before_work=True))
                self.assertNotIn('unsettled',state['spend'])
                self.assertEqual(state['spend']['known_usd'],0)
        state=fresh()
        budgets.settle(state,'r',0,agents.AgentResult(agents.ENVIRONMENT,before_work=True))
        self.assertEqual(state['spend']['unpriced'],{'calls':0,'unknown_calls':0,'tokens_in':0,'tokens_out':0})
        spend=qualification.empty_spend()
        qualification.add_spend(spend,agents.AgentResult(agents.ENVIRONMENT,usage=dict(used)))
        self.assertEqual(spend['unpriced']['tokens_in'],10000)

    def test_validate_warns_that_dollar_limits_do_not_bind_on_command_agents(self):
        self.workflow(ONE)
        wf=workflow.load(self.wf_path)
        self.assertTrue(any('dollar limits do not bind' in w for w in wf.warnings))
        for amount in ('nan','inf','-1','0'):
            self.workflow(ONE,defaults=f'run_budget_usd={amount}')
            self.assertTrue(any('finite and greater than zero' in e for e in workflow.load(self.wf_path).errors))


class SessionCost(unittest.TestCase):
    """A continued Claude session is charged for this call only (BUD-13)."""

    class Fake:
        def __init__(self):
            self.state = {"make": {}}

        def st(self, tid):
            return self.state[tid]

    def book(self, fake, kind, session, result_session, cost):
        agent = type("A", (), {"kind": kind})()
        result = agents.AgentResult(agents.OK, session_id=result_session, cost_usd=cost)
        engine.Engine.book_session_cost(fake, agent, {"id": "make"}, session, result)
        return result

    def test_a_resumed_session_is_charged_its_increment(self):
        """bud: Claude reports the session's total cost, so a resumed attempt is charged the
        difference from the last total seen for that session (BUD-13); a fresh session, a
        different session id, a total that went down or a non-Claude agent is charged as reported"""
        fake = self.Fake()
        r = self.book(fake, "claude", None, "s1", 5.0)
        self.assertEqual((r.cost_usd, r.reported_cost_usd), (5.0, None))
        r = self.book(fake, "claude", "s1", "s1", 8.3)
        self.assertEqual((r.cost_usd, r.reported_cost_usd), (3.3, 8.3))
        self.assertEqual(r.outcome()["reported_cost_usd"], 8.3)
        r = self.book(fake, "claude", "s1", "s1", 9.79)
        self.assertEqual(r.cost_usd, 1.49)
        r = self.book(fake, "claude", "s1", "s2", 2.0)          # the provider opened another session
        self.assertEqual((r.cost_usd, r.reported_cost_usd), (2.0, None))
        r = self.book(fake, "claude", "s2", "s2", 1.0)          # a total that went down: as reported
        self.assertEqual((r.cost_usd, fake.state["make"]["session_cost"]), (1.0, {"s2": 1.0}))
        r = self.book(fake, "command", "s2", "s2", 7.0)
        self.assertEqual((r.cost_usd, r.reported_cost_usd), (7.0, None))
        self.assertNotIn("reported_cost_usd", r.outcome())


class Metered(agents.CommandAgent):
    """Reports no dollar cost, like Codex, but reports usage: 1000 tokens in and 100 out per call."""

    def interpret(self, res):
        result=super().interpret(res)
        result.usage={'tokens_in':1000,'tokens_out':100}
        return result


class TokenCap(EngineCase):
    HEADER = EngineCase.HEADER + 'read_only_args = ["--read-only"]\n'


    def setup_cap(self, cap, tasks=ONE, doctor=True, defaults=''):
        """`doctor` qualifies first, outside any run, so the run's own calls meet the cap; probes
        made for a run are held to it as well (BUD-09)."""
        self.workflow(tasks,defaults=f'run_budget_tokens={cap}\n'+defaults)
        reviewers=[t['id'] for t in workflow.load(self.wf_path).tasks if t['kind']=='review']
        self.script({'make':[GOOD],**{rid:[PASS] for rid in reviewers}})
        self.addCleanup(agents.REGISTRY.__setitem__,'command',agents.CommandAgent)
        agents.REGISTRY['command']=Metered
        if doctor:
            self.assertEqual(self.runner('doctor',self.wf_path),0,self.output)
        return reviewers

    def probes(self):
        return sorted(glob.glob(os.path.join(self.root,'.runs','doctor','*','*','invocation-*')))

    def used(self):
        unpriced=self.the_run().state['spend']['unpriced']
        return unpriced['tokens_in']+unpriced['tokens_out']

    def test_token_cap_stops_before_the_next_call_and_resume_adds_tokens(self):
        """bud: a token cap for agents that report no cost (BUD-06)"""
        a,b=self.setup_cap(1000)
        self.assertEqual(self.start(),2,self.output)                 # the producer's call crossed the line
        run=self.the_run()
        self.assertEqual(run.state['status'],'stopped')
        self.assertTrue(run.state['stop_reason'].startswith('the token cap'),run.state['stop_reason'])
        self.assertIn('1100 of 1000 tokens',self.output)
        self.assertIn('runner resume --add-tokens N',self.output)
        self.assertEqual(self.status(a),'pending')
        self.assertEqual(self.status(b),'pending')
        self.assertEqual(run.state['spend']['known_usd'],0)          # no invented dollars
        with open(os.path.join(run.path,'STATUS.md')) as fh:
            status=fh.read()
        self.assertIn('cap 1000 tokens',status)
        self.assertIn('--add-tokens N',status)
        self.assertNotIn('--add-budget',status)
        self.check_invariants()
        self.assertEqual(self.resume(),2,self.output)                # nothing added: stops again at once
        self.assertEqual(self.resume('--add-tokens','5000'),0,self.output)
        self.assertEqual(self.the_run().state['run_budget_tokens'],6000)
        self.assertEqual(self.status('make'),'accepted')
        with open(os.path.join(run.path,'events.jsonl')) as fh:
            events=[json.loads(l) for l in fh if l.strip()]
        added=[e for e in events if e['event']=='budget-added']
        self.assertEqual(added[0]['amount_tokens'],5000)
        self.assertEqual(added[0]['budget_tokens'],6000)
        self.assertEqual(self.the_run().state['spend']['known_usd'],0)
        self.check_invariants()

    def test_under_the_cap_unpriced_reviews_start_one_at_a_time(self):
        """bud: under a token cap at most one unpriced call crosses the stop line (BUD-06)"""
        a,b=self.setup_cap(1500)
        self.assertEqual(self.start(),2,self.output)                 # 1100 < 1500: one review starts
        self.assertEqual(self.used(),2200)             # it crossed; the other never started
        jobs={j['task']:j for j in self.the_run().state['tasks']['make']['panel']['jobs']}
        self.assertEqual((jobs[a]['tries'],jobs[b]['tries']),(1,0))
        self.assertEqual(self.resume('--add-tokens','5000'),0,self.output)
        self.assertEqual(self.used(),3300)
        self.assertEqual(self.status('make'),'accepted')

    def test_no_cap_means_no_stop_and_nothing_to_add_to(self):
        self.setup_cap(0, ONE + '[[task]]\nid="look"\ntype="human"\nverifies="make"\n')
        self.assertEqual(self.start(),255,self.output)               # run_budget_tokens = 0: waits on the person
        self.assertEqual(self.used(),3300)
        self.assertEqual(self.resume('--add-tokens','5'),2)
        self.assertIn('no token cap',self.output)
        self.assertEqual(self.resume('--add-tokens','-1'),2)

    def test_qualification_for_a_new_run_stops_at_the_first_probe_over_the_cap(self):
        """bud: qualification probes are held to the run's limits (BUD-09): with a 1000-token
        cap the first probe (1100 tokens) completes and no other starts; no run is created, the
        spend is in qualification.json, and nothing incomplete is cached"""
        self.setup_cap(1000,doctor=False)
        self.assertEqual(self.start(),2,self.output)
        self.assertIn('qualification stopped: the token cap',self.output)
        self.assertIn('before the next qualification probe',self.output)
        self.assertEqual(len(self.probes()),1)
        self.assertFalse([d for d in os.listdir(os.path.join(self.root,'.runs')) if d=='demo'])
        report=record.read_json(os.path.join(self.root,'.runs','qualification.json'))
        self.assertEqual(report['spend']['unpriced']['tokens_in']+report['spend']['unpriced']['tokens_out'],1100)
        self.assertEqual(report['budget_stop']['hint'],'--add-tokens N')
        cache=record.read_json(os.path.join(self.root,'.runs','qualification-cache.json'))
        self.assertEqual(cache['entries'],{})

    def test_requalification_on_resume_is_charged_and_held_to_the_cap(self):
        """bud: requalification on resume is held to the run's cap and charged even when it stops
        (BUD-09): over the line, no probe starts; with room for one more, the probe that
        crosses completes, the next does not, and both probes' tokens are in the run's spend"""
        self.setup_cap(1000)
        self.assertEqual(self.start(),2,self.output)                 # the producer crossed: 1100
        self.assertEqual(self.used(),1100)
        cache=os.path.join(self.root,'.runs','qualification-cache.json')
        before=len(self.probes())
        os.unlink(cache)                                             # as a changed CLI would
        self.assertEqual(self.resume(),2,self.output)
        self.assertEqual(len(self.probes()),before)                  # over the line: none started
        self.assertIn('before the next qualification probe',self.the_run().state['stop_reason'])
        self.assertEqual(self.the_run().state['status'],'stopped')
        self.assertEqual(self.resume('--add-tokens','1500'),2,self.output)   # cap 2500
        self.assertEqual(len(self.probes()),before+2)                # 2200 < 2500, then 3300
        self.assertEqual(self.used(),3300)                           # charged though it stopped
        run=self.the_run()
        self.assertEqual(run.state['status'],'stopped')
        self.assertIn('3300 of 2500 tokens',run.state['stop_reason'])
        self.assertEqual(self.resume('--add-tokens','20000'),0,self.output)
        self.assertEqual(self.status('make'),'accepted')
        self.check_invariants()

    def test_validate_checks_the_cap_and_names_it_in_the_warning(self):
        self.workflow(ONE,defaults='run_budget_tokens=-1')
        self.assertTrue(any("'run_budget_tokens' must be 0 (no cap) or more" in e
                            for e in workflow.load(self.wf_path).errors))
        self.workflow(ONE,defaults='run_budget_tokens=2.5')
        self.assertTrue(any("'run_budget_tokens' must be" in e for e in workflow.load(self.wf_path).errors))
        self.workflow(ONE,defaults='run_budget_tokens=200000')
        warnings=workflow.load(self.wf_path).warnings
        self.assertTrue(any('stops the run once 200000 tokens' in w for w in warnings),warnings)
        self.workflow(ONE)
        self.assertTrue(any('set run_budget_tokens to cap' in w for w in workflow.load(self.wf_path).warnings))
        self.workflow(ONE,defaults='run_budget_tokens=200000')
        warnings=workflow.load(self.wf_path).warnings                  # the fake agent is a command agent
        self.assertTrue(any('each of its calls counts at its reservation' in w for w in warnings),warnings)

    def test_unknown_usage_under_the_cap_counts_its_reservation(self):
        """bud: under a token cap a call whose usage stays unknown counts as its reservation (BUD-12):
        the tokens left under the line when it started; usage that is known, a refusal with no
        usage, no cap, or an intent recorded before BUD-12 counts nothing extra"""
        def fresh(cap=20000, used=0):
            return {'run_budget_usd':10.0,'run_budget_tokens':cap,'unpriced_call_reserve':0,'seconds':0,'tasks':{'r':{'cost_usd':0.0}},
                    'spend':{'known_usd':0.0,'reserved_usd':0.0,
                             'unpriced':{'calls':1,'unknown_calls':0,'tokens_in':used,'tokens_out':0}}}
        metered=Metered('m',{})
        state=fresh(used=6000)
        self.assertEqual(budgets.token_reservation(state,metered),14000)
        self.assertEqual(budgets.token_reservation(fresh(cap=0),metered),0)
        self.assertEqual(budgets.token_reservation(state,Priced('p',{})),0)
        budgets.settle(state,'r',0,agents.AgentResult(agents.INTERRUPTED,usage_source='unknown'),14000)
        unpriced=state['spend']['unpriced']
        self.assertEqual((unpriced['unknown_calls'],unpriced['counted_calls'],unpriced['counted_tokens']),(1,1,14000))
        self.assertEqual(budgets.tokens_used(state),20000)
        self.assertFalse(budgets.fits_tokens(state,metered))
        stop=str(budgets.token_stop(state,'the next call'))
        self.assertIn('6000 measured + 14000 held for 1 call with unknown usage = 20000 of 20000 tokens',stop)
        for result,tokens in ((agents.AgentResult(agents.OK,usage={'tokens_in':10,'tokens_out':1}),14000),
                              (agents.AgentResult(agents.QUOTA,before_work=True),14000),       # refused before any work
                              (agents.AgentResult(agents.INTERRUPTED),0)):   # no cap, or an older intent
            with self.subTest(status=result.status,tokens=tokens):
                state=fresh(used=6000)
                budgets.settle(state,'r',0,result,tokens)
                self.assertNotIn('counted_calls',state['spend']['unpriced'])
                self.assertEqual(budgets.tokens_used(state),6000+sum(result.usage.values()))

    def test_a_command_agent_under_the_cap_stops_after_each_call(self):
        """bud: a command agent reports no usage, so under a token cap each of its calls counts the
        rest of the cap and the run stops; STATUS.md names the counted calls and --add-tokens continues
        one call at a time (BUD-12)"""
        a,b=self.setup_cap(5000)
        agents.REGISTRY['command']=agents.CommandAgent                  # reports no usage at all
        self.assertEqual(self.start(),2,self.output)
        run=self.the_run()
        unpriced=run.state['spend']['unpriced']
        self.assertEqual((unpriced['tokens_in'],unpriced['counted_calls'],unpriced['counted_tokens']),(0,1,5000))
        self.assertIn('0 measured + 5000 held for 1 call with unknown usage = 5000 of 5000 tokens; '
                      'held: tasks/010-make/attempt-1/invocation-1 (', run.state['stop_reason'])
        self.assertEqual((self.status(a),self.status(b)),('pending','pending'))
        with open(os.path.join(run.path,'STATUS.md')) as fh:
            spend_line=next(l for l in fh if l.startswith('Spend:'))
        self.assertIn('5000 tokens held, not measured, for 1 call with unknown usage: '
                      'tasks/010-make/attempt-1/invocation-1 (',spend_line)
        self.assertIn('; cap 5000 tokens)',spend_line)
        self.assertEqual(self.resume('--add-tokens','100'),2,self.output)  # one review, counted at 100
        unpriced=self.the_run().state['spend']['unpriced']
        self.assertEqual((unpriced['counted_calls'],unpriced['counted_tokens']),(2,5100))
        self.assertEqual(self.resume('--add-tokens','100'),0,self.output)
        self.assertEqual(self.status('make'),'accepted')
        self.check_invariants()

    def test_an_interrupted_call_with_unknown_usage_counts_its_reservation_on_resume(self):
        """bud: a call killed under a token cap with no provider record is reconciled by resume
        at the reservation its intent recorded, so the run stops there and `--add-tokens`
        continues; an intent recorded before BUD-12 has none and counts nothing (BUD-12)"""
        for legacy in (False,True):
            with self.subTest(legacy=legacy):
                self.setUp()
                self.setup_cap(5000)
                class Killed(Exception):pass
                def crash(point):
                    if point=='agent:running':raise Killed()
                self.cli.CRASH=crash
                with self.assertRaises(Killed):self.start()
                self.cli.CRASH=None
                run=self.the_run()
                it=next(i for i in run.state['intents'] if i['kind']=='agent')
                self.assertEqual(it['token_reservation'],5000)
                if legacy:
                    del it['token_reservation']
                    del run.state['schema_version']          # as an older runner wrote it
                    run.save()
                code=self.resume()
                unpriced=self.the_run().state['spend']['unpriced']
                if legacy:
                    self.assertEqual(code,0,self.output)                   # unknown usage left out
                    self.assertNotIn('counted_calls',unpriced)
                else:
                    self.assertEqual(code,2,self.output)
                    self.assertEqual((unpriced['counted_calls'],unpriced['counted_tokens']),(1,5000))
                    self.assertIn('0 measured + 5000 held for 1 call with unknown usage',self.the_run().state['stop_reason'])
                    self.assertEqual(self.resume('--add-tokens','10000'),0,self.output)
                self.assertEqual(self.status('make'),'accepted')
                self.check_invariants()
                self.doCleanups()

    def events(self):
        with open(os.path.join(self.the_run().path,'events.jsonl')) as fh:
            return [json.loads(l) for l in fh if l.strip()]

    def refuse_first_review(self, before_work):
        """The first call of the first reviewer fails at the provider with no usage, as a Codex
        "model at capacity" refusal does; `before_work` is what the adapter saw (BUD-15)."""
        original=agents.CommandAgent.run
        refused=[]
        def run(agent, prompt, **kwargs):
            answer=original(agent, prompt, **kwargs)
            if kwargs['read_only'] and kwargs['env'].get('CODE_SMITH_RUN')!='doctor' and not refused:
                refused.append(kwargs['env']['CODE_SMITH_TASK'])
                answer=agents.AgentResult(agents.TRANSIENT,error='Selected model is at capacity',
                                          seconds=3.1)
                answer.before_work=before_work
            return answer
        patcher=patch.object(agents.CommandAgent,'run',run)
        patcher.start()
        self.addCleanup(patcher.stop)
        return refused

    def test_a_refusal_before_any_work_spends_nothing(self):
        """bud: a provider refusal before any work spends nothing under a token cap (BUD-15): the
        panel's transient retry calls the reviewer again and the run ends done; a transient failure
        after work began, with no usage, is still held at its reservation and stops the run"""
        a,b=self.setup_cap(5000)
        self.script({'make':[GOOD],a:[PASS,PASS],b:[PASS]})
        refused=self.refuse_first_review(before_work=True)
        self.assertEqual(self.start(),0,self.output)
        unpriced=self.the_run().state['spend']['unpriced']
        self.assertNotIn('counted_calls',unpriced)
        self.assertEqual(self.used(),3300)                           # three answered calls
        self.assertEqual(self.count_calls(refused[0]),2)
        self.assertEqual(self.status('make'),'accepted')
        self.check_invariants()

    def count_calls(self,tid):
        with open(self.script_path+'.'+tid+'.counter') as fh:return int(fh.read())

    def test_a_held_call_is_named_and_kept_apart_from_measured_usage(self):
        """bud: unknown usage is held apart from measured tokens and the stop names the call
        (BUD-16): a transient failure after work began, with no usage, is held at its reservation;
        the stop reason and STATUS.md say measured and held separately and name the invocation,
        its seconds and error; the stop writes `run-stopped`; `--add-tokens` continues"""
        a,b=self.setup_cap(5000)
        self.script({'make':[GOOD],a:[PASS,PASS],b:[PASS]})
        refused=self.refuse_first_review(before_work=False)
        self.assertEqual(self.start(),2,self.output)
        run=self.the_run()
        unpriced=run.state['spend']['unpriced']
        self.assertEqual((unpriced['tokens_in']+unpriced['tokens_out'],unpriced['counted_tokens']),(1100,3900))
        where=f"tasks/{run.state['tasks'][refused[0]]['dir']}/round-1/invocation-1"
        self.assertEqual(unpriced['held'],[{'task':refused[0],'invocation':where,'seconds':3.1,
                                            'status':'transient','error':'Selected model is at capacity',
                                            'tokens':3900}])
        self.assertIn(f"(1100 measured + 3900 held for 1 call with unknown usage = 5000 of 5000 tokens; "
                      f"held: {where} (3.1 s, 3900 tokens; transient: Selected model is at capacity))",
                      run.state['stop_reason'])
        with open(os.path.join(run.path,'STATUS.md')) as fh:
            status=fh.read()
        self.assertIn('3900 tokens held, not measured, for 1 call with unknown usage: '+where,status)
        self.assertIn('- Budget used: $0.00 known of $50.00; 1100 measured + 3900 held for unknown '
                      'usage = 5000 of 5000 tokens.',status)
        names=[e['event'] for e in self.events()]
        self.assertEqual(names[names.index('budget-stop')+1],'run-stopped')
        self.assertEqual(self.events()[names.index('budget-stop')+1]['status'],'stopped')
        self.assertEqual(self.resume('--add-tokens','5000'),0,self.output)
        self.assertEqual(self.status('make'),'accepted')
        self.check_invariants()

    def test_unpriced_reviewers_run_side_by_side_within_bounded_reservations(self):
        """bud: under a token cap each unpriced call reserves a bounded amount and reviewers run
        side by side while their reservations fit under the line (BUD-17): two reservations of
        3000 fit under 20000 and both reviewers start in one batch; under 5000 only one fits and
        they start one at a time; a run recorded before BUD-17 keeps one at a time"""
        def batches():
            kinds=[(e['event'],e.get('kind')) for e in self.events() if e.get('kind')=='agent']
            return kinds[2:]                                 # after the producer's call
        for cap,legacy,together in ((20000,False,True),(5000,False,False),(20000,True,False)):
            with self.subTest(cap=cap,legacy=legacy):
                self.setUp()
                a,b=self.setup_cap(cap,defaults='unpriced_call_reserve=3000\n')
                if legacy:
                    class Killed(Exception):pass
                    def crash(point):
                        if point=='provider:outcome-recorded':raise Killed()
                    self.cli.CRASH=crash
                    with self.assertRaises(Killed):self.start()
                    self.cli.CRASH=None
                    run=self.the_run()
                    del run.state['unpriced_call_reserve']
                    del run.state['schema_version']          # as an older runner wrote it
                    run.save()
                    self.resume()
                else:
                    self.start()
                self.assertEqual(self.status('make'),'accepted',self.output)
                intents=[e for e in batches()]
                self.assertEqual(intents[:2]==[('intent','agent'),('intent','agent')],together,intents)
                self.check_invariants()
                self.doCleanups()

    def test_the_bound_follows_the_run_and_a_command_agent_counts_it(self):
        """bud: the bound of an unpriced call's reservation is unpriced_call_reserve, else twice
        the largest measured call and at least 500k (BUD-17); a command agent, which reports no
        usage, counts that bounded amount per call, not the rest of the cap"""
        state={'run_budget_tokens':10_000_000,'unpriced_call_reserve':0,
               'spend':{'unpriced':{'tokens_in':0,'tokens_out':0}}}
        self.assertEqual(budgets.call_reserve(state),500_000)
        state['spend']['unpriced']['largest_call']=400_000
        self.assertEqual(budgets.call_reserve(state),800_000)
        self.assertEqual(budgets.token_reservation(state,Metered('m',{})),800_000)
        self.assertEqual(budgets.token_reservation(state,Metered('m',{}),held=9_500_000),500_000)
        del state['unpriced_call_reserve']                          # recorded before BUD-17
        schema.migrate(state)                                       # as Run.load reads it
        self.assertEqual(budgets.token_reservation(state,Metered('m',{})),10_000_000)
        self.assertFalse(budgets.admits_unpriced(state,1))
        a,b=self.setup_cap(5000,defaults='unpriced_call_reserve=3000\n')
        agents.REGISTRY['command']=agents.CommandAgent                  # reports no usage at all
        self.assertEqual(self.start(),2,self.output)
        unpriced=self.the_run().state['spend']['unpriced']
        self.assertEqual([h['tokens'] for h in unpriced['held']],[3000,2000])   # producer, one reviewer
        self.assertEqual(unpriced['counted_tokens'],5000)
        self.assertEqual(self.resume('--add-tokens','10000'),0,self.output)
        self.check_invariants()

    def test_probes_hold_a_probe_sized_bound_in_every_run(self):
        """bud: a qualification probe under a token cap holds the probe-sized bound
        (probe_reserve_tokens), not the call reserve and never the whole rest of the line: the
        documented 3M cap starts with no doctor first, and a run recorded before BUD-17 qualifies
        on resume and ends its --add-tokens loop (BUD-24)"""
        probe=budgets.PROBE_RESERVE_TOKENS
        self.assertEqual(probe,20_000)
        legacy={'run_budget_tokens':3_000_000,'spend':{'unpriced':{'tokens_in':0,'tokens_out':0}}}
        schema.migrate(legacy)                                      # no unpriced_call_reserve
        self.assertEqual(budgets.token_reservation(legacy,Metered('m',{})),3_000_000)
        self.assertEqual(budgets.probe_reservation(legacy,Metered('m',{})),probe)
        self.assertEqual(budgets.probe_reservation(legacy,Metered('m',{}),5),5)
        legacy['run_budget_tokens']=7
        self.assertEqual(budgets.probe_reservation(legacy,Metered('m',{})),7)
        # The tutorial's cap, no doctor first, an agent that reports no usage at all: it starts.
        self.setup_cap(3_000_000,doctor=False)
        agents.REGISTRY['command']=agents.CommandAgent
        self.assertEqual(self.start(),0,self.output)
        run=self.the_run()
        probes=len(self.probes())
        self.assertGreater(probes,0)
        held=[h['tokens'] for h in run.state['spend']['unpriced'].get('held',[])]
        self.assertEqual(run.state['spend']['unpriced']['counted_tokens'],
                         probes*probe+sum(held),(probes,held))
        self.check_invariants()
        self.doCleanups()
        # A legacy run under a tight line: the probes of its resume stop at the line, and
        # --add-tokens ends the loop instead of each probe eating all that was added.
        self.setUp()
        self.setup_cap(1_000_000,tasks=ONE+'[[task]]\nid="look"\ntype="human"\nneeds=["make"]\n')
        self.assertEqual(self.start(),255,self.output)
        run=self.the_run()
        del run.state['unpriced_call_reserve']
        del run.state['schema_version']                             # as an older runner wrote it
        run.state['run_budget_tokens']=budgets.tokens_used(run.state)+2*probe+1
        run.save()
        self.assertEqual(self.the_run().state['unpriced_call_reserve'],budgets.SERIAL_RESERVE)
        os.unlink(os.path.join(self.root,'.runs','qualification-cache.json'))
        agents.REGISTRY['command']=agents.CommandAgent              # probes with unknown usage
        before=self.the_run().state['spend']['unpriced'].get('counted_tokens',0)
        self.assertEqual(self.resume(),2,self.output)
        self.assertIn('before the next qualification probe',self.the_run().state['stop_reason'])
        stopped=self.the_run().state['spend']['unpriced']['counted_tokens']
        # Two probes of 20000, a third held to the 1 token left, then the line: never the rest.
        self.assertEqual(stopped-before,2*probe+1)
        self.assertEqual(self.resume('--add-tokens','400000'),255,self.output)
        counted=self.the_run().state['spend']['unpriced']['counted_tokens']
        self.assertLessEqual(counted-stopped,8*probe)
        self.assertEqual(self.runner('approve','latest','look','-C',self.root),0,self.output)
        self.assertEqual(self.resume(),0,self.output)
        self.check_invariants()

    def fail_first_call(self, author, answer, times=1, only=None):
        """The first call of the author (`author`) or the first `times` reviewer calls return
        `answer`, a provider failure with no usage, in place of the scripted one. Reviewers of
        one batch run in threads, so "first" is the order in which their calls return; a test
        that needs one particular reviewer to fail names it in `only`, or a loaded machine picks
        the other one."""
        original=agents.CommandAgent.run
        failed=[]
        def run(agent, prompt, **kwargs):
            result=original(agent, prompt, **kwargs)
            task=kwargs['env'].get('CODE_SMITH_TASK')
            if (kwargs['read_only']!=author and kwargs['env'].get('CODE_SMITH_RUN')!='doctor'
                    and len(failed)<times and only in (None,task)):
                failed.append(kwargs['env']['CODE_SMITH_TASK'])
                return agents.AgentResult(**answer.outcome())
            return result
        patcher=patch.object(agents.CommandAgent,'run',run)
        patcher.start()
        self.addCleanup(patcher.stop)
        return failed

    def test_a_saved_refusal_before_work_is_read_back_after_a_kill(self):
        """bud: a refusal before work saved with its answer is read back after a kill (BUD-18):
        killed at `provider:outcome-recorded` (author) and `panel:outcomes-recorded` (reviewer),
        resume reads the saved `before_work` result, calls again, and the refusal spends nothing"""
        refusal=agents.AgentResult(agents.TRANSIENT,error='Selected model is at capacity',
                                   seconds=3.1,before_work=True)
        self.assertTrue(agents.AgentResult(**refusal.outcome()).before_work)
        for author,point in ((True,'provider:outcome-recorded'),(False,'panel:outcomes-recorded')):
            with self.subTest(point=point):
                self.setUp()
                a,b=self.setup_cap(5000)
                self.script({'make':[GOOD,GOOD] if author else [GOOD],
                             a:[PASS] if author else [PASS,PASS],b:[PASS]})
                failed=self.fail_first_call(author,refusal,only=None if author else a)
                class Killed(Exception):pass
                def crash(at):
                    if at==point:raise Killed()
                self.cli.CRASH=crash
                with self.assertRaises(Killed):self.start()
                self.cli.CRASH=None
                self.assertEqual(self.resume(),0,self.output)
                unpriced=self.the_run().state['spend']['unpriced']
                self.assertNotIn('counted_calls',unpriced)
                self.assertEqual(self.used(),3300)                   # three answered calls
                self.assertEqual(self.count_calls(failed[0]),2)
                self.assertEqual(self.status('make'),'accepted')
                self.check_invariants()
                self.doCleanups()

    def test_unknown_usage_after_work_counts_the_rest_of_the_line(self):
        """bud: a call that ends without its final event and with unknown usage counts what was
        left under the line when it started (BUD-20): under a 3M cap a reviewer that times out
        after work, with no usage, stops the run before the next call; largest_call does not
        grow; a refusal before work still spends nothing.

        The time-out is injected into reviewer `a` by name and `b` answers a second later, so `a`
        has returned while `b` is still running. Injected into "the first call to return", a
        loaded machine let `b` return first, time out instead, and meet its one-step script
        again on resume; the runner was right, the test was order-sensitive. The runner never
        stops a batch half-collected: `reader_batch` waits for every call of the batch and
        settles each before the stop line is looked at again, so the slow sibling's paid answer
        is kept and resume calls only `a`."""
        a,b=self.setup_cap(3_000_000)
        self.script({'make':[GOOD],a:[PASS,PASS],b:[dict(PASS,sleep_s=1)]})
        timed_out=agents.AgentResult(agents.TIMED_OUT,error='the call ran past its time limit',
                                     seconds=1800.0)
        failed=self.fail_first_call(False,timed_out,only=a)
        self.assertEqual(self.start(),2,self.output)
        self.assertEqual(failed,[a])
        run=self.the_run()
        unpriced=run.state['spend']['unpriced']
        # Both reviewers start in one batch (two bounded reservations of 500k fit). The held call
        # brings the run to the line, not 500k past the measured; the sibling's measured 1100
        # lands beside it, before or after (settlement follows the batch's order, not the clock).
        self.assertEqual(unpriced['counted_calls'],1)
        self.assertIn(budgets.tokens_used(run.state),(3_000_000,3_001_100))
        self.assertGreater(unpriced['counted_tokens'],2_990_000)
        self.assertEqual(unpriced['largest_call'],1100)
        self.assertIn('held for 1 call with unknown usage = 300',run.state['stop_reason'])
        self.assertEqual(self.count_calls(failed[0]),1)              # no call after the stop
        jobs={j['task']:j for j in run.state['tasks']['make']['panel']['jobs']}
        self.assertEqual(jobs[b]['result']['status'],'ok')          # the slow sibling's answer kept
        self.assertEqual(self.resume('--add-tokens','3000000'),0,self.output)
        self.assertEqual((self.count_calls(a),self.count_calls(b)),(2,1))
        self.assertEqual(self.status('make'),'accepted')
        self.check_invariants()
        # The remainder is counted only for a call that did not complete; a refusal before work,
        # and a completed call that reports no usage (a command agent), count as before.
        def fresh():
            return {'run_budget_tokens':3_000_000,'unpriced_call_reserve':0,'seconds':0,
                    'tasks':{'r':{'cost_usd':0.0}},'spend':{'known_usd':0.0,'reserved_usd':0.0,
                    'unpriced':{'calls':0,'unknown_calls':0,'tokens_in':1100,'tokens_out':0}}}
        for result,counted in ((agents.AgentResult(agents.TRANSIENT,before_work=True),0),
                               (agents.AgentResult(agents.OK,completed=True),500_000),
                               (agents.AgentResult(agents.INTERRUPTED),2_998_900)):
            with self.subTest(status=result.status,completed=result.completed):
                state=fresh()
                budgets.settle(state,'r',0,result,500_000,token_remainder=2_998_900)
                self.assertEqual(state['spend']['unpriced'].get('counted_tokens',0),counted)
        state=fresh()                                               # an intent recorded before
        budgets.settle(state,'r',0,agents.AgentResult(agents.INTERRUPTED),500_000)
        self.assertEqual(state['spend']['unpriced']['counted_tokens'],500_000)

    def test_unknown_calls_started_together_share_one_remainder(self):
        """bud: two reviewers started together that both end with unknown usage hold one
        remainder between them, not two (BUD-20): one stop, the held total is the remainder the
        batch was admitted with, both calls are named, and resume charges neither again"""
        a,b=self.setup_cap(3_000_000)
        self.script({'make':[GOOD],a:[PASS,PASS],b:[PASS,PASS]})
        timed_out=agents.AgentResult(agents.TIMED_OUT,error='the call ran past its time limit',
                                     seconds=1800.0)
        failed=self.fail_first_call(False,timed_out,times=2)
        self.assertEqual(self.start(),2,self.output)
        run=self.the_run()
        unpriced=run.state['spend']['unpriced']
        self.assertEqual(sorted(failed),sorted([a,b]))                # one batch, both unknown
        self.assertEqual((unpriced['counted_calls'],unpriced['counted_tokens']),(2,3_000_000-1100))
        self.assertEqual(budgets.tokens_used(run.state),3_000_000)
        self.assertEqual(len(unpriced['held']),2)
        self.assertIn('1100 measured + 2998900 held for 2 calls with unknown usage = 3000000 of '
                      '3000000 tokens',run.state['stop_reason'])
        with open(os.path.join(run.path,'STATUS.md')) as fh:
            status=fh.read()
        self.assertIn('2998900 tokens held, not measured, for 2 calls with unknown usage: ',status)
        self.assertIn(unpriced['held'][-1]['invocation'],status)
        self.assertEqual(self.resume('--add-tokens','3000000'),0,self.output)
        unpriced=self.the_run().state['spend']['unpriced']
        self.assertEqual((unpriced['counted_calls'],unpriced['counted_tokens']),(2,3_000_000-1100))
        self.assertEqual(self.status('make'),'accepted')
        self.check_invariants()


class CapacityAdapters(unittest.TestCase):
    """What the Codex and Claude adapters call a refusal before any work (BUD-15)."""

    def setUp(self):
        import tempfile
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=os.path.realpath(self.tmp.name)

    def codex(self, mode):
        import sys
        home=os.path.join(self.root,f'home-{mode}'); os.makedirs(home)
        inv=os.path.join(self.root,f'inv-{mode}'); os.makedirs(inv)
        fake=os.path.join(os.path.dirname(os.path.abspath(__file__)),'fake_codex.py')
        a=agents.make('codex-fake',{'kind':'codex','argv':[sys.executable,fake]})
        env=dict(os.environ,CODEX_HOME=home,FAKE_CODEX_MODE=mode)
        return a.run('p',cwd=self.root,invocation_dir=inv,schema=validate.PRODUCE,session_id=None,
                     model='',timeout_s=20,budget_usd=1,read_only=True,env=env)

    def test_a_codex_capacity_refusal_before_any_item_is_marked(self):
        """bud: a Codex "model at capacity" refusal before any item event is a refusal before
        work (BUD-15); after an item event it is not, and settles as unknown usage"""
        r=self.codex('capacity')
        self.assertEqual((r.status,r.usage,r.before_work),(agents.TRANSIENT,{},True))
        self.assertTrue(r.outcome()['before_work'])
        state={'run_budget_tokens':5000,'unpriced_call_reserve':0,'seconds':0,'tasks':{'r':{'cost_usd':0.0}},
               'spend':{'known_usd':0.0,'reserved_usd':0.0,
                        'unpriced':{'calls':0,'unknown_calls':0,'tokens_in':0,'tokens_out':0}}}
        before=copy.deepcopy(state)
        budgets.settle(state,'r',0,r,5000)
        self.assertEqual(state['spend'],before['spend'])
        worked=self.codex('capacity-after-work')
        self.assertEqual((worked.status,worked.usage,worked.before_work),(agents.TRANSIENT,{},False))
        self.assertNotIn('before_work',worked.outcome())
        budgets.settle(state,'r',0,worked,5000)
        self.assertEqual(state['spend']['unpriced']['counted_tokens'],5000)


    def test_a_claude_provider_failure_is_free_only_without_an_assistant_message(self):
        """bud: a Claude provider failure with no cost, no usage and no assistant message in its
        transcript since the call began is a refusal before work (BUD-15): its envelope says
        `num_turns` 0; one with an assistant message is not"""
        from codesmith import proc
        import datetime
        home=os.path.join(self.root,'claude'); inv=os.path.join(self.root,'inv'); os.makedirs(inv)
        envelope={'type':'result','subtype':'success','is_error':True,'api_error_status':529,
                  'result':'API Error: 529 overloaded','session_id':'s','num_turns':0,'duration_api_ms':0}
        def failing(argv,**kw):
            return proc.ProcResult('exited',1,json.dumps(envelope).encode(),b'',2.0,None)
        a=agents.make('claude',{'kind':'claude'})
        call=dict(cwd=self.root,invocation_dir=inv,schema=validate.PRODUCE,session_id=None,model='',
                  timeout_s=5,budget_usd=1,read_only=True,env={'CLAUDE_CONFIG_DIR':home})
        with patch.object(proc,'run_process',failing):
            r=a.run('p',**call)
        self.assertEqual((r.status,r.cost_usd,r.usage,r.before_work),(agents.TRANSIENT,None,{},True))
        session=json.load(open(os.path.join(inv,'argv.json')))['argv']
        session=session[session.index('--session-id')+1]
        slug=''.join(c if c.isalnum() else '-' for c in os.path.realpath(self.root))
        os.makedirs(os.path.join(home,'projects',slug))
        stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')
        def written(argv,**kw):
            with open(os.path.join(home,'projects',slug,argv[argv.index('--session-id')+1]+'.jsonl'),'w') as fh:
                fh.write(json.dumps({'type':'assistant','timestamp':stamp,'message':{'id':'m'}})+'\n')
            return failing(argv,**kw)
        inv=os.path.join(self.root,'inv-2'); os.makedirs(inv)
        with patch.object(proc,'run_process',written):
            r=a.run('p',**dict(call,invocation_dir=inv))
        self.assertEqual((r.status,r.before_work),(agents.TRANSIENT,False))

    def test_a_claude_failure_without_positive_evidence_is_held(self):
        """bud: a Claude 5xx with no cost whose envelope does not say that no turn ran is held,
        even when the runner cannot find its transcript (BUD-19): the call wrote files, under a
        CLAUDE_CONFIG_DIR the runner does not read; its dollar reservation stays unsettled"""
        from codesmith import proc
        envelope={'type':'result','subtype':'success','is_error':True,'api_error_status':503,
                  'result':'API Error: 503 unavailable','session_id':'s','num_turns':4,
                  'duration_api_ms':41000}
        def worked(argv,**kw):
            with open(os.path.join(self.root,'claude-worked.txt'),'w') as fh:
                fh.write('worked\n')
            return proc.ProcResult('exited',1,json.dumps(envelope).encode(),b'',40.0,None)
        a=agents.make('claude',{'kind':'claude'})
        def call(name):
            inv=os.path.join(self.root,name); os.makedirs(inv)
            with patch.object(proc,'run_process',worked):
                return a.run('p',cwd=self.root,invocation_dir=inv,schema=validate.PRODUCE,
                             session_id=None,model='',timeout_s=5,budget_usd=1,read_only=False,
                             env={'CLAUDE_CONFIG_DIR':os.path.join(self.root,'not-read')})
        r=call('inv')
        self.assertTrue(os.path.exists(os.path.join(self.root,'claude-worked.txt')))
        self.assertEqual((r.status,r.cost_usd,r.usage,r.before_work),(agents.TRANSIENT,None,{},False))
        state={'run_budget_usd':10.0,'seconds':0,'tasks':{'r':{'cost_usd':0.0}},
               'spend':{'known_usd':0.0,'reserved_usd':1.0,
                        'unpriced':{'calls':0,'unknown_calls':0,'tokens_in':0,'tokens_out':0}}}
        budgets.settle(state,'r',1.0,r)
        self.assertEqual(state['spend']['unsettled']['usd'],1.0)
        del envelope['num_turns'], envelope['duration_api_ms']       # an envelope that says nothing
        self.assertFalse(call('inv-2').before_work)

    def test_claude_before_work_needs_every_field_to_agree(self):
        """bud: Claude's refusal before work needs every turn and API-time field its envelope holds
        to be 0, and without a transcript the runner can read, `duration_api_ms` 0 (BUD-22)"""
        from codesmith import proc
        import datetime
        a=agents.make('claude',{'kind':'claude'})
        home=os.path.join(self.root,'claude')
        slug=''.join(c if c.isalnum() else '-' for c in os.path.realpath(self.root))
        os.makedirs(os.path.join(home,'projects',slug))
        ABSENT=object()
        # (num_turns, duration_api_ms, transcript): None no transcript, '' one with no
        # assistant message since the call began, 'assistant' one with such a message
        table=[((0,0,None),True),((0,ABSENT,None),False),((ABSENT,0,None),True),
               ((0,8400,None),False),((3,0,None),False),((ABSENT,ABSENT,None),False),
               ((0,8400,''),False),((0,ABSENT,''),True),((ABSENT,0,''),True),((0,0,''),True),
               ((0,0,'assistant'),False),((False,0,None),False)]
        for n,((turns,api_ms,transcript),expected) in enumerate(table):
            envelope={'type':'result','subtype':'success','is_error':True,'api_error_status':529,
                      'result':'API Error: 529 overloaded','session_id':'s'}
            envelope.update({k:v for k,v in (('num_turns',turns),('duration_api_ms',api_ms)) if v is not ABSENT})
            def failing(argv,**kw):
                if transcript is not None:
                    # Stamped as the call runs, not when the test began: under load a stamp taken
                    # earlier falls before the call's start and the message would not count.
                    stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')
                    path=os.path.join(home,'projects',slug,argv[argv.index('--session-id')+1]+'.jsonl')
                    with open(path,'w') as fh:
                        if transcript:
                            fh.write(json.dumps({'type':transcript,'timestamp':stamp,'message':{'id':'m'}})+'\n')
                return proc.ProcResult('exited',1,json.dumps(envelope).encode(),b'',2.0,None)
            inv=os.path.join(self.root,f'inv-{n}'); os.makedirs(inv)
            with self.subTest(fields=(turns,api_ms,transcript)), patch.object(proc,'run_process',failing):
                r=a.run('p',cwd=self.root,invocation_dir=inv,schema=validate.PRODUCE,session_id=None,
                        model='',timeout_s=5,budget_usd=1,read_only=True,env={'CLAUDE_CONFIG_DIR':home})
                self.assertEqual((r.status,r.before_work),(agents.TRANSIENT,expected))

    def test_codex_work_after_a_retryable_error_is_not_before_work(self):
        """bud: a Codex stream with a retryable error, then item events and a file written, that
        exits non-zero with no usage is not a refusal before work: it is held (BUD-19)"""
        r=self.codex('capacity-then-work')
        self.assertTrue(os.path.exists(os.path.join(self.root,'codex-worked.txt')))
        self.assertEqual((r.status,r.usage,r.before_work),(agents.TRANSIENT,{},False))
        state={'run_budget_tokens':5000,'unpriced_call_reserve':0,'seconds':0,'tasks':{'r':{'cost_usd':0.0}},
               'spend':{'known_usd':0.0,'reserved_usd':0.0,
                        'unpriced':{'calls':0,'unknown_calls':0,'tokens_in':0,'tokens_out':0}}}
        budgets.settle(state,'r',0,r,5000,token_remainder=5000)
        self.assertEqual(state['spend']['unpriced']['counted_tokens'],5000)


class RefusalBoundary(EngineCase):
    def test_all_refusals_require_positive_evidence(self):
        """bud: every refusal class needs positive no-work evidence (BUD-23)"""
        import sys
        quota_after_work = None
        for status, message in ((agents.QUOTA, 'usage_limit_reached'),
                                (agents.ENVIRONMENT, 'not logged in'),
                                (agents.TRANSIENT, 'model at capacity')):
            for worked in (False, True):
                with self.subTest(status=status, worked=worked):
                    inv = os.path.join(self.side, f'{status}-{worked}')
                    os.makedirs(inv)
                    rows = [{'type': 'thread.started', 'thread_id': 'scripted'}, {'type': 'turn.started'}]
                    if worked: rows.append({'type': 'item.started', 'item': {'type': 'command_execution'}})
                    rows.append({'type': 'turn.failed', 'error': {'message': message}})
                    script = 'import sys; print(' + repr('\n'.join(json.dumps(r) for r in rows)) + '); sys.exit(1)'
                    agent = agents.CodexAgent('scripted', {'argv': [sys.executable, '-c', script]})
                    result = agent.run('p', cwd=self.root, invocation_dir=inv, schema=validate.PRODUCE,
                                       session_id=None, model='', timeout_s=20, budget_usd=1, read_only=True,
                                       env=dict(os.environ, CODEX_HOME=self.side))
                    self.assertEqual(result.status, status)
                    self.assertEqual(result.before_work, not worked)
                    result = agents.AgentResult(**result.outcome())
                    if status == agents.QUOTA and worked:
                        quota_after_work = result
                    state = {'run_budget_tokens':3_000_000,'unpriced_call_reserve':0,'seconds':0,
                             'tasks':{'r':{'cost_usd':0.0}},'spend':{'known_usd':0.0,'reserved_usd':0.0,
                             'unpriced':{'calls':0,'unknown_calls':0,'tokens_in':0,'tokens_out':0}}}
                    budgets.settle(state, 'r', 0, result, 500_000, token_remainder=3_000_000)
                    self.assertEqual(budgets.tokens_held(state), 3_000_000 if worked else 0)
                    self.assertEqual(budgets.fits_tokens(state, agent), not worked)
                    self.assertEqual(state['spend']['unpriced']['calls'], int(worked))
                    if worked:
                        self.assertEqual(state['spend']['unpriced']['held'][0]['status'], status)

        # Drive the same parsed post-work quota answer through the author/fallback path.
        self.workflow('[agents.spare]\nargv = ["' + sys.executable + '", "' +
                      os.path.join(os.path.dirname(__file__), 'fake_agent.py') + '"]\n' +
                      ONE.replace('reviewers=["principled-priya", "clause-by-clause-chen"]',
                                  'fallback_agents=["spare"]'),
                      defaults='run_budget_tokens=3000000')
        self.script({'make': [GOOD]})
        parsed = quota_after_work
        original = agents.CommandAgent.run
        launched = []
        def quota(agent, prompt, **kwargs):
            answer = original(agent, prompt, **kwargs)
            if kwargs['env'].get('CODE_SMITH_TASK') == 'make':
                launched.append(agent.name)
                return parsed
            return answer
        with patch.object(agents.CommandAgent, 'run', quota):
            self.assertEqual(self.start(), 2, self.output)
        run = self.the_run()
        self.assertEqual(run.state['status'], 'stopped')
        self.assertEqual(budgets.tokens_held(run.state), 3_000_000)
        self.assertEqual(launched, ['fake'])
        self.assertEqual(self.resume(), 2, self.output)
        self.assertEqual(budgets.tokens_held(self.the_run().state), 3_000_000)
        from codesmith import invariants
        self.assertEqual(invariants.check(self.the_run().state), [])


class Windowed(agents.CommandAgent):
    """A command agent that reports what a Codex call reports: a million tokens in, a hundred
    thousand out, and the weekly window moving by 1.5 points per call. `cached` of the input
    tokens are reported read from the provider's cache."""
    calls = 0
    cached = 0

    def interpret(self, res):
        result = super().interpret(res)
        if result.structured and 'outcome' in result.structured:   # not a qualification probe
            Windowed.calls += 1
            before = float(Windowed.calls)
            result.usage = {'tokens_in': 1_000_000, 'cached_tokens_in': Windowed.cached, 'tokens_out': 100_000}
            result.limits = {'kind': 'codex', 'plan_type': 'plus', 'limit_id': 'codex',
                             'window_minutes': 10080, 'used_percent_before': before,
                             'used_percent_after': before + 1.5, 'delta_percent': 1.5,
                             'resets_at': 1791936000, 'credits': None}
        return result


TWO_TRIES = '''
[[task]]
id = "make"
type = "implement"
prompt = "Make src/a.txt say good."
outputs = ["src/a.txt"]
writes = ["src/**"]
max_attempts = 2
gate = ["grep -q good src/a.txt"]
'''


class WindowsAndEstimates(EngineCase):
    def setUp(self):
        super().setUp()
        Windowed.calls = 0
        Windowed.cached = 0
        self.addCleanup(agents.REGISTRY.__setitem__, 'command', agents.CommandAgent)
        agents.REGISTRY['command'] = Windowed

    def go(self, profile, budget):
        self.workflow(profile + TWO_TRIES, defaults=f'run_budget_usd = {budget}')
        self.script([{'write': {'src/a.txt': 'bad\n'}, 'answer': done()},
                     {'write': {'src/a.txt': 'good\n'}, 'answer': done()}])
        return self.start()

    def test_counted_author_estimate_stops_the_panel(self):
        """bud: panel admission counts the author's estimated spend (BUD-14)"""
        self.workflow('read_only_args = ["--read-only"]\n'
                      'price_per_mtok = { input = 2.0, output = 8.0 }\n'
                      'estimated_counts = true\n' + ONE, defaults='run_budget_usd = 2')
        reviewers = [t['id'] for t in workflow.load(self.wf_path).tasks if t['kind'] == 'review']
        self.script({'make': [GOOD], **{rid: [PASS] for rid in reviewers}})
        self.assertEqual(self.start(), 2, self.output)
        self.assertEqual(self.the_run().state['status'], 'stopped')
        for rid in reviewers:
            self.assertFalse(os.path.exists(self.script_path + '.' + rid + '.counter'))
        self.assertIn('budget cannot cover the next panel call', self.output)
        self.assertEqual(self.resume('--add-budget', '10'), 0, self.output)
        for tid in ['make'] + reviewers:
            with open(self.script_path + '.' + tid + '.counter') as fh:
                self.assertEqual(fh.read(), '1')
        self.check_invariants()

    def test_the_rate_limit_window_is_summed_per_run_and_shown(self):
        """bud: a run sums its calls' rate-limit window movement and shows it (BUD-10)"""
        self.assertEqual(self.go('', 50), 0, self.output)
        run = self.the_run()
        window = run.state['spend']['windows']['codex:codex']
        self.assertEqual({k: window[k] for k in ('window_minutes', 'used_percent_first',
                                                 'used_percent_last', 'delta_percent', 'plan_type')},
                         {'window_minutes': 10080, 'used_percent_first': 1.0, 'used_percent_last': 3.5,
                          'delta_percent': 3.0, 'plan_type': 'plus'})
        observed = ('. Observed Codex weekly account window: 1.0% → 3.5% (shared-account '
                    'observations, not attributable run usage).')
        with open(os.path.join(run.path, 'STATUS.md')) as fh:
            spend_line = next(l for l in fh if l.startswith('Spend:'))
        self.assertIn(observed, spend_line)
        self.assertNotIn('estimated', spend_line)
        self.assertNotIn('cached', spend_line)                       # no cache read was reported
        self.assertEqual(self.runner('runs', '-C', self.root), 0, self.output)   # no workflow: every run
        self.assertIn(observed[2:-1], self.output)
        legacy = copy.deepcopy(run.state)                              # a run recorded before windows
        del legacy['spend']['windows']
        self.assertNotIn('window', record.render_run_status(run.info, legacy))

    def test_window_observations_are_shown_as_observed_and_the_cached_share_with_them(self):
        """bud: the account's rate-limit window is shown as observed, never as the run's own use, and
        the Spend line gives the cached share of the input tokens (BUD-21)"""
        def limits(before, after, reset):
            return {'kind': 'codex', 'plan_type': 'plus', 'limit_id': 'codex', 'window_minutes': 10080,
                    'used_percent_before': before, 'used_percent_after': after,
                    'delta_percent': round(after - before if after >= before else after, 3),
                    'resets_at': reset}
        # Three reviewers side by side each saw the same one-point move, in whatever order they
        # ended; then the window reset. Summed, the deltas would claim 3 points of this run.
        seen = [limits(2.0, 3.0, 1791936000), limits(2.0, 3.0, 1791936000),
                limits(0.0, 1.0, 1792540800), limits(2.0, 3.0, 1791936000)]
        spend = {}
        for at, observation in enumerate(seen):
            budgets.record_windows(spend, observation, at=1791000000 + at)
        window = spend['windows']['codex:codex']
        self.assertEqual(len(window['observations']), 4)
        self.assertEqual(window['observations'][2], {'at': '2026-10-03T04:00:02.000000Z', 'before': 0.0,
                                                     'after': 1.0, 'resets_at': '1792540800'})
        self.assertEqual(record.window_phrases(spend), [
            'Observed Codex weekly account window: 2.0% → 3.0%; reset, then 0.0% → 1.0% '
            '(shared-account observations, not attributable run usage)'])
        self.assertNotIn('this run', record.window_phrases(spend)[0])
        self.assertNotIn('+', record.window_phrases(spend)[0])
        # A window recorded before the observations were kept shows its first and last readings.
        legacy = {'windows': {'codex:codex': {'window_minutes': 10080, 'used_percent_first': 1.0,
                                              'used_percent_last': 3.0, 'delta_percent': 3.0}}}
        self.assertEqual(record.window_phrases(legacy), [
            'Observed Codex weekly account window: 1.0% → 3.0% (shared-account observations, not '
            'attributable run usage)'])
        # Through a run: the token accounting is the measured one, and two thirds were cached.
        Windowed.cached = 660_000
        self.assertEqual(self.go('', 50), 0, self.output)
        run = self.the_run()
        unpriced = run.state['spend']['unpriced']
        self.assertEqual((unpriced['tokens_in'], unpriced['tokens_out'], unpriced['cached_tokens_in']),
                         (2_000_000, 200_000, 1_320_000))
        with open(os.path.join(run.path, 'STATUS.md')) as fh:
            spend_line = next(l for l in fh if l.startswith('Spend:'))
        self.assertIn('(2000000 tokens in, 66% cached, 200000 out;', spend_line)
        self.assertNotIn('this run', spend_line)

    def test_profile_rates_give_an_estimate_that_is_never_known_spend(self):
        """bud: profile rates give an estimate, never known spend unless the owner counts it (BUD-11)"""
        rates = 'price_per_mtok = { input = 2.0, cached_input = 0.5, output = 8.0 }\n'
        self.assertEqual(self.go(rates, 2), 0, self.output)          # $5.60 estimated, $2 budget: no stop
        run = self.the_run()
        spend = run.state['spend']
        self.assertEqual((spend['known_usd'], spend['estimated_usd']), (0.0, 5.6))
        self.assertNotIn('estimated_counted_usd', spend)
        outcomes = sorted(glob.glob(os.path.join(run.task_dir('make'), 'attempt-*', 'invocation-*', 'outcome.json')))
        with open(outcomes[0]) as fh:
            self.assertEqual((json.load(fh)['estimated_usd']), 2.8)
        with open(os.path.join(run.path, 'STATUS.md')) as fh:
            self.assertIn('≈$5.60 estimated from profile rates.', fh.read())
        # The owner's choice: the estimate counts against run_budget_usd like known spend, so the
        # second call does not start once the first one's $2.80 passed the $2 limit.
        self.assertEqual(self.go(rates + 'estimated_counts = true\n', 2), 2, self.output)
        run = self.the_run()
        self.assertEqual(run.state['status'], 'stopped')
        self.assertEqual(run.state['spend']['estimated_counted_usd'], 2.8)
        self.assertIn('budget cannot cover the next call', run.state['stop_reason'])
        with open(os.path.join(run.path, 'STATUS.md')) as fh:
            self.assertIn('≈$2.80 estimated from profile rates ($2.80 of it counted against the budget)', fh.read())

    def test_rates_are_checked(self):
        """wf: price_per_mtok is validated and only for agents that report no cost (WF-26)"""
        def errors(profile, kind='command'):
            self.workflow(f'kind = "{kind}"\n' + profile + TWO_TRIES)
            return workflow.load(self.wf_path).errors
        self.assertEqual(errors('price_per_mtok = { input = 0, output = 1.5 }\nestimated_counts = true\n'), [])
        self.assertTrue(any("price_per_mtok.input must be a number of at least 0" in e
                            for e in errors('price_per_mtok = { input = -1, output = 1 }\n')))
        self.assertTrue(any("price_per_mtok needs 'output'" in e for e in errors('price_per_mtok = { input = 1 }\n')))
        self.assertTrue(any("unknown key 'cache'" in e
                            for e in errors('price_per_mtok = { input = 1, output = 1, cache = 1 }\n')))
        self.assertTrue(any("'estimated_counts' needs 'price_per_mtok'" in e for e in errors('estimated_counts = true\n')))
        self.assertTrue(any("only for kinds that report no cost" in e
                            for e in errors('price_per_mtok = { input = 1, output = 1 }\n', kind='claude')))
        self.workflow(TWO_TRIES, defaults='status_refresh_s = -1')
        self.assertTrue(any("'status_refresh_s' must be 0" in e for e in workflow.load(self.wf_path).errors))
