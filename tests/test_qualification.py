"""Out-of-band qualification and gate preflight (PRE-01 to PRE-11). No model calls."""
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from helpers import EngineCase, RepoCase, done, run_cli
from codesmith import activity, agents, budgets, gitops, preflight, qualification, record

PROBE_AGENT = str(Path(__file__).with_name('probe_agent.py'))
TASK = '[[task]]\nid="make"\ntype="implement"\nprompt="p"\noutputs=["out.txt"]\ngate=["true"]\n'

class Qualification(RepoCase):
    def workflow(self, agent=PROBE_AGENT, tasks=TASK, **profile):
        extra = ''.join(f'{key} = {json.dumps(value)}\n' for key, value in profile.items())
        return self.load('name="probe"\n[defaults]\nagent="probe"\n[agents.probe]\n'
                         + f'argv = [{json.dumps(sys.executable)}, {json.dumps(agent)}]\n' + extra + tasks)

    def test_observed_effects_and_cache(self):
        wf = self.workflow()
        self.assertLoads(wf)
        report = qualification.check_workflow(wf)
        self.assertEqual(report['problems'], [])
        entry = next(iter(report['profiles'].values()))
        self.assertEqual(set(entry['capabilities']), {'answer', 'read', 'execute', 'write'})
        self.assertFalse(entry['cached'])
        self.assertEqual(entry['spend']['unpriced']['calls'], 4)
        with patch.object(agents.CommandAgent, 'run', side_effect=AssertionError('cache called agent')):
            cached = qualification.check_workflow(wf)
        self.assertTrue(next(iter(cached['profiles'].values()))['cached'])
        self.assertEqual(cached['spend']['unpriced']['calls'], 0)
        refreshed = qualification.check_workflow(wf, force=True)
        self.assertFalse(next(iter(refreshed['profiles'].values()))['cached'])
        self.assertTrue(Path(self.root, '.runs', 'qualification.json').is_file())

    def test_a_greeting_and_fabricated_execution_are_not_capabilities(self):
        script = self.write('liar.py', '''import json, pathlib, sys
p=json.load(sys.stdin)
if p['code_smith_probe']=='execute': pathlib.Path('execute-result.txt').write_text('wrong digest')
print(json.dumps({'value':p.get('value','I did it')}))
''')
        wf = self.workflow(script)
        report = qualification.check_workflow(wf)
        caps = next(iter(report['profiles'].values()))['capabilities']
        self.assertEqual(caps, ['answer'])
        self.assertTrue(report['problems'])
        self.commit()
        res = run_cli('start', wf.workflow_file)
        self.assertEqual(res.returncode, 2, res.stderr)
        self.assertIn('needs', res.stderr)
        self.assertFalse(Path(self.root, '.runs', 'probe').exists())

    def test_profile_binary_model_and_host_change_the_cache_key(self):
        wf = self.workflow()
        p = wf.agents['probe']
        first, meta = qualification.fingerprint(p, 'm', False, self.root)
        self.assertNotEqual(first, qualification.fingerprint(p, 'm2', False, self.root)[0])
        self.assertNotEqual(first, qualification.fingerprint(p, 'm', True, self.root)[0])
        self.assertNotEqual(first, qualification.fingerprint(dict(p, extra_args=['--flag']), 'm', False, self.root)[0])
        with patch.object(qualification, 'host_identity', return_value='other host'):
            self.assertNotEqual(first, qualification.fingerprint(p, 'm', False, self.root)[0])
        agent = self.write('agent.py', 'print(1)')
        p = dict(p, argv=[sys.executable, agent])
        old = qualification.fingerprint(p, '', False, self.root)[0]
        self.write('agent.py', 'print(2)')
        self.assertNotEqual(old, qualification.fingerprint(p, '', False, self.root)[0])

    def test_boundary_is_observed_in_the_read_only_profile(self):
        wf = self.workflow(read_only_args=['--read-only'])
        key, meta = qualification.fingerprint(wf.agents['probe'], '', True, self.root)
        directory = os.path.join(self.root, 'qualification')
        good = qualification.qualify('probe', meta, directory)
        self.assertIn('boundary', good['capabilities'])
        meta['profile']['read_only_args'] = []
        bad = qualification.qualify('probe', meta, directory + '-bad')
        self.assertNotIn('boundary', bad['capabilities'])

    def test_provided_context_requires_only_answer(self):
        task = {'kind':'review','requires':['read','execute']}
        self.assertEqual(qualification.required(task, {'review_mode':'provided_context'}), {'answer'})
        self.assertEqual(qualification.required(task, {}), {'answer','read','execute','boundary'})

    def test_resume_uses_the_explicit_session_and_old_value(self):
        class Resumable(agents.CommandAgent):
            def capabilities(self):
                return super().capabilities() | {'resume'}
            def interpret(self, result):
                answer = super().interpret(result)
                answer.session_id = 'explicit-session'
                return answer
        wf = self.workflow()
        with patch.dict(agents.REGISTRY, command=Resumable):
            report = qualification.check_workflow(wf)
        self.assertIn('resume', next(iter(report['profiles'].values()))['capabilities'])

    def test_probes_stop_after_an_environment_failure(self):
        wf = self.workflow()
        original = agents.CommandAgent.run
        calls = []
        def sandbox_failure(agent, prompt, **kwargs):
            probe = json.loads(prompt)['code_smith_probe']
            calls.append(probe)
            if probe == 'answer':
                return original(agent, prompt, **kwargs)
            return agents.AgentResult(agents.ENVIRONMENT, error='sandbox failed to start')
        with patch.object(agents.CommandAgent, 'run', sandbox_failure):
            report = qualification.check_workflow(wf)
        self.assertEqual(calls, ['answer', 'read'])
        self.assertEqual(next(iter(report['profiles'].values()))['capabilities'], ['answer'])

    def test_run_bound_probes_settle_by_the_one_evidence_rule(self):
        """proc: a probe for a run settles by the run's evidence rule (an environment failure
        after work with unknown usage holds its reservation; only positive before_work is free),
        and one whose group outlived its stop is a problem naming it, never cached, for doctor on
        its own too, whose spend is unchanged (PROC-31, V-05); a probe holds the probe-sized bound,
        not the run's call reserve (BUD-24)"""
        wf = self.workflow()
        original = agents.CommandAgent.run
        def answering(result):
            def run(agent, prompt, **kwargs):
                if json.loads(prompt)['code_smith_probe'] == 'answer':
                    return result()
                return original(agent, prompt, **kwargs)
            return run
        after_work = lambda: agents.AgentResult(agents.ENVIRONMENT, error='provider failed mid-call')
        refused = lambda: agents.AgentResult(agents.ENVIRONMENT, error='capacity', before_work=True)
        probe = budgets.PROBE_RESERVE_TOKENS
        for result, guarded, counted in ((after_work, True, probe), (refused, True, 0),
                                         (after_work, False, 0)):
            with self.subTest(before_work=result().before_work, guarded=guarded):
                guard = qualification.RunBudget(5, 1000000, call_reserve=400000) if guarded else None
                with patch.object(agents.CommandAgent, 'run', answering(result)):
                    report = qualification.check_workflow(wf, force=True, guard=guard)
                unpriced = report['spend']['unpriced']
                self.assertEqual(unpriced.get('counted_tokens', 0), counted)
                self.assertEqual(unpriced['unknown_calls'], 1 if counted else 0)
                if guarded:
                    self.assertEqual(guard.state['spend']['unpriced'].get('counted_tokens', 0), counted)
        state = {'spend': {'known_usd': 0.0, 'reserved_usd': 0.0, 'unpriced': {
            'calls': 0, 'unknown_calls': 0, 'tokens_in': 0, 'tokens_out': 0}}}
        qualification.merge_spend(state['spend'], report['spend'])
        qualification.merge_spend(state['spend'], {'known_usd': 0.0, 'unpriced': {'counted_tokens': 7},
                                                   'unsettled': {'usd': 1.0, 'calls': 1}})
        self.assertEqual((state['spend']['unpriced']['counted_tokens'], state['spend']['unsettled']['usd']),
                         (7, 1.0))
        group = {'pid': 4242, 'pgid': 4242}
        def open_cleanup():
            answer = original_answer[0]
            answer.cleanup = {'status': 'open', 'error': 'injected stop failure', 'group': group}
            return answer
        original_answer = []
        def run(agent, prompt, **kwargs):
            answer = original(agent, prompt, **kwargs)
            if json.loads(prompt)['code_smith_probe'] == 'answer':
                original_answer[:] = [answer]
                return open_cleanup()
            return answer
        for guarded in (True, False):
            with self.subTest(cleanup=True, guarded=guarded), patch.object(agents.CommandAgent, 'run', run):
                guard = qualification.RunBudget(5) if guarded else None
                report = qualification.check_workflow(wf, force=True, guard=guard)
                named = [p for p in report['problems'] if 'process group 4242' in p]
                self.assertEqual(len(named), 1, report['problems'])
                if guarded:
                    self.assertIn("qualification probe 'answer' of profile 'probe' left process "
                                  "group 4242 running: injected stop failure", named[0])
        cache = record.read_json(os.path.join(self.root, '.runs', 'qualification-cache.json'))
        self.assertFalse([e for e in cache['entries'].values() if e.get('cleanup_open')])

    def test_doctor_on_its_own_reports_an_open_probe_cleanup(self):
        """proc: doctor on its own reports a probe whose group stop failed as a problem naming
        the group, exits 2 and caches nothing for the profile (V-05)"""
        wf = self.workflow()
        self.commit()
        original = agents.CommandAgent.run
        def run(agent, prompt, **kwargs):
            answer = original(agent, prompt, **kwargs)
            if json.loads(prompt)['code_smith_probe'] == 'answer':
                answer.cleanup = {'status': 'open', 'error': 'injected stop failure',
                                  'group': {'pid': 4343, 'pgid': 4343}}
            return answer
        import contextlib
        import io
        from codesmith import cli
        out, err = io.StringIO(), io.StringIO()
        with patch.object(agents.CommandAgent, 'run', run), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(['doctor', wf.workflow_file]), 2)
        self.assertIn("qualification probe 'answer' of profile 'probe' left process group 4343 "
                      "running: injected stop failure", err.getvalue())
        cache = os.path.join(self.root, '.runs', 'qualification-cache.json')
        entries = record.read_json(cache)['entries'] if os.path.exists(cache) else {}
        self.assertEqual(entries, {})

    def test_doctor_cli_is_model_free_for_command_profiles(self):
        wf = self.workflow()
        self.commit()
        first = run_cli('doctor', wf.workflow_file)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn('answer, read, execute, write', first.stdout)
        second = run_cli('doctor', wf.workflow_file)
        self.assertIn('(cached)', second.stdout)

    def test_doctor_names_the_detected_cause_of_claude_silence(self):
        wf = self.workflow(kind='claude')
        self.commit()
        with patch.dict(agents.REGISTRY, claude=agents.CommandAgent):
            report = qualification.check_workflow(wf)
        entry = next(iter(report['profiles'].values()))
        self.assertEqual(entry['observed_activity'], [])
        self.assertEqual(entry['activity_cause'], {'cause': activity.NO_HOOKS,
            'message': activity.diagnose_claude_silence(self.root)[1]})
        import contextlib
        import io
        from codesmith import cli
        out = io.StringIO()
        with patch.dict(agents.REGISTRY, claude=agents.CommandAgent), contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(['doctor', wf.workflow_file]), 0)
        self.assertIn('observed activity: none; the work tree has no .claude/settings.json', out.getvalue())

    def test_a_failed_codex_preflight_skips_the_model_probes_and_names_the_cause(self):
        """pre: a Codex profile whose sandbox cannot start is not probed with a model (PRE-09)"""
        wf = self.workflow(kind='codex')
        self.commit()
        cause = "the Codex sandbox cannot start on this host: bwrap: No permissions to create a new namespace"
        with patch.object(agents.CodexAgent, 'preflight', return_value=([], cause)), \
             patch.object(agents.CodexAgent, 'run', side_effect=AssertionError('a model was called')):
            report = qualification.check_workflow(wf)
        entry = next(iter(report['profiles'].values()))
        self.assertEqual(entry['capabilities'], [])
        self.assertEqual(entry['probes']['sandbox']['error'], cause)
        self.assertEqual(entry['probes']['answer']['status'], agents.ENVIRONMENT)
        self.assertEqual(entry['spend']['unpriced']['calls'], 0)
        self.assertTrue(any(cause in p for p in report['problems']), report['problems'])

    def test_doctor_prints_the_preflight_notes(self):
        wf = self.workflow(kind='codex')
        self.commit()
        note = "feature 'use_legacy_landlock' is deprecated in this Codex CLI"
        import contextlib
        import io
        from codesmith import cli

        class Scripted(agents.CommandAgent):
            def preflight(self, cwd, env, read_only, timeout_s=60):
                return [note], ""
        out = io.StringIO()
        with patch.dict(agents.REGISTRY, codex=Scripted), contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(['doctor', wf.workflow_file]), 0)
        self.assertIn('  note: ' + note, out.getvalue())
        with patch.dict(agents.REGISTRY, codex=Scripted), contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(['doctor', wf.workflow_file]), 0)   # cached: the note is kept
        self.assertEqual(out.getvalue().count('  note: ' + note), 2)

    def test_check_workflow_survives_malformed_project_hook_settings(self):
        wf = self.workflow(kind='claude')
        os.makedirs(os.path.join(self.root, '.claude'), exist_ok=True)
        with open(os.path.join(self.root, '.claude', 'settings.json'), 'w') as fh:
            fh.write('{"hooks": ' + '[' * 10000 + ']' * 10000 + '}')
        self.commit()
        with patch.dict(agents.REGISTRY, claude=agents.CommandAgent):
            report = qualification.check_workflow(wf)
        entry = next(iter(report['profiles'].values()))
        self.assertEqual(entry['activity_cause']['cause'], activity.INSPECTION_FAILED)
        self.assertEqual(report['problems'], [])

class Invalidation(EngineCase):
    def test_environment_failure_discards_cached_qualification(self):
        self.workflow(TASK)
        self.script([{'write':{'out.txt':'done'},'answer':done()}])
        original = agents.CommandAgent.run
        def broken(agent, prompt, **kwargs):
            if 'code_smith_probe' in prompt:
                return original(agent, prompt, **kwargs)
            return agents.AgentResult(agents.ENVIRONMENT, error='sandbox failed to start')
        with patch.object(agents.CommandAgent, 'run', broken):
            self.assertEqual(self.start(), 2, self.output)
        run = self.the_run()
        key = run.state['tasks']['make']['qualification_key']
        cache = record.read_json(os.path.join(self.root, '.runs', 'qualification-cache.json'))
        self.assertNotIn(key, cache['entries'])
        self.assertEqual(run.state['tasks']['make']['attempts_used'], 0)
        self.assertTrue(run.state['needs_qualification'])
        self.assertEqual(self.resume(), 0, self.output)
        self.check_invariants()

    def test_a_network_failure_keeps_the_cached_qualification(self):
        """prov: the network is down (PROV-16): the run stops, nothing about the profile is
        forgotten, and `resume` continues without probing again"""
        self.workflow(TASK)
        self.script([{'write':{'out.txt':'done'},'answer':done()}])
        original = agents.CommandAgent.run
        calls = []
        def offline(agent, prompt, **kwargs):
            if 'code_smith_probe' in prompt:
                calls.append('probe')
                return original(agent, prompt, **kwargs)
            calls.append('call')
            if calls.count('call') == 1:
                return agents.AgentResult(agents.ENVIRONMENT, error="API Error: Can't reach the API "
                                          "server \u2014 check your internet or DNS (ENOTFOUND)")
            return original(agent, prompt, **kwargs)
        with patch.object(agents.CommandAgent, 'run', offline):
            self.assertEqual(self.start(), 2, self.output)
            self.assertIn('the network is down', self.output)
            self.assertIn('No attempt was used', self.output)
            run = self.the_run()
            key = run.state['tasks']['make']['qualification_key']
            cache = record.read_json(os.path.join(self.root, '.runs', 'qualification-cache.json'))
            self.assertIn(key, cache['entries'])
            self.assertNotIn('needs_qualification', run.state)
            self.assertEqual(run.state['tasks']['make']['attempts_used'], 0)
            probes = calls.count('probe')
            self.assertEqual(self.resume(), 0, self.output)
            self.assertEqual(calls.count('probe'), probes)           # nothing was probed again
        self.assertEqual(self.the_run().state['tasks']['make']['status'], 'accepted')
        self.check_invariants()

class GatePreflight(RepoCase):
    def workflow(self, gates, checks=''):
        wf = self.load('name="gates"\n[[task]]\nid="make"\ntype="implement"\n'
                       'prompt="p"\noutputs=["out.txt"]\ngate=[' + ','.join(gates) + ']\n' + checks)
        self.assertLoads(wf)
        self.commit()
        return wf

    def test_new_and_invariant_gates(self):
        wf = self.workflow(['"true"', '{run="echo intended; exit 1",new=true,fail_pattern="intended"}',
                            '{run="true",new=true,fail_pattern="intended"}'])
        report = preflight.check_gates(wf)
        self.assertEqual([r['result'] for r in report['results']], ['pass','fail','objection'])
        self.assertTrue(report['results'][1]['fails_as_intended'])
        self.assertFalse(report['ok'])

    def test_the_same_command_is_checked_once(self):
        wf = self.workflow(['"true"', '"true"', '{run="true",new=true,fail_pattern="x"}'],
                           '[[task]]\nid="also"\ntype="implement"\nprompt="p"\noutputs=["b.txt"]\ngate=["true"]\n')
        report = preflight.check_gates(wf)
        self.assertEqual([(r['id'], r['result'], r.get('same_as')) for r in report['results']],
                         [('make:gate:1', 'pass', None), ('make:gate:2', 'pass', 'make:gate:1'),
                          ('make:gate:3', 'objection', None), ('also:gate:1', 'pass', 'make:gate:1')])
        logs = [f for f in os.listdir(report['directory']) if f.endswith('.log')]
        self.assertEqual(len(logs), 2)                 # `new` gates are always run on their own

    def test_new_gate_without_fail_pattern_is_unconfirmed(self):
        """pre: a `new` gate that fails with no fail_pattern is reported apart from a confirmed one"""
        wf = self.workflow(['{run="exit 1",new=true}'])
        report = preflight.check_gates(wf)
        self.assertEqual(report['results'][0]['result'], 'unconfirmed')
        self.assertTrue(report['ok'])
        result = run_cli('check-gates', wf.workflow_file)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('make:gate:1: fails (no fail_pattern to confirm the reason)', result.stdout)

    def test_commands_verifying_unproduced_output_may_fail(self):
        """pre: a gate or check that names an output not yet produced fails as intended"""
        wf = self.workflow(['"test -s out.txt"', '"test -s other.txt"'],
                           '[[task]]\nid="check"\ntype="check"\nverifies="make"\nrun=["test -s out.txt"]\n')
        report = preflight.check_gates(wf)
        self.assertEqual([(r['result'], r['fails_as_intended']) for r in report['results']],
                         [('fail', True), ('fail', False), ('fail', True)])
        self.assertFalse(report['ok'])               # the unrelated failing gate still counts
        wf = self.workflow(['"test -s out.txt"'], '[[task]]\nid="check"\ntype="check"\nverifies="make"\nrun=["test -s out.txt"]\n')
        self.assertTrue(preflight.check_gates(wf)['ok'])

    def test_an_unproduced_output_is_judged_in_the_clone_and_per_producer(self):
        """pre: whether a failure is intended is judged in the clean clone the command ran in,
        not the owner's tree, and only for the producer whose output the command names: `also`
        does not need `make`, so its gate on make's output is an error (PRE-11)"""
        wf = self.workflow(['"test -s out.txt"'],
                           '[[task]]\nid="also"\ntype="implement"\nprompt="p"\noutputs=["b.txt"]\n'
                           'gate=["test -s out.txt"]\n')
        with open(os.path.join(self.root, '.git', 'info', 'exclude'), 'a') as fh:
            fh.write('out.txt\n')                    # left over in the owner's tree, ignored
        self.write('out.txt', 'left over\n')
        report = preflight.check_gates(wf)
        self.assertEqual([(r['id'], r['result'], r['fails_as_intended']) for r in report['results']],
                         [('make:gate:1', 'fail', True), ('also:gate:1', 'error', False)])

    def test_gates_may_wait_for_upstream_outputs(self):
        """pre: gates may wait for upstream outputs (PRE-11): a gate or check naming a literal
        output of a task its task needs, directly or transitively, fails as intended and says
        which task it waits for; one naming an output of a task that is not upstream is an error"""
        wf = self.workflow(['"test -s out.txt"'], '''
[[task]]
id = "middle"
type = "implement"
prompt = "p"
needs = ["make"]
outputs = ["mid.txt"]
gate = ["true"]
[[task]]
id = "fix"
type = "implement"
prompt = "p"
needs = ["middle"]
outputs = ["fix.txt"]
gate = ["test -s out.txt", {run="test -s mid.txt || { echo absent; exit 1; }",new=true,fail_pattern="SYMPTOM"}]
[[task]]
id = "fixcheck"
type = "check"
verifies = "fix"
run = ["test -s mid.txt"]
[[task]]
id = "stranger"
type = "implement"
prompt = "p"
outputs = ["s.txt"]
gate = ["test -s fix.txt"]
''')
        report = preflight.check_gates(wf)
        got = {r['id']: (r['result'], r['fails_as_intended'], r['waits_for']) for r in report['results']}
        self.assertEqual(got['make:gate:1'], ('fail', True, []))              # its own output
        self.assertEqual(got['fix:gate:1'], ('fail', True, ['make']))         # transitively upstream
        self.assertEqual(got['fix:gate:2'], ('fail', True, ['middle']))       # a `new` gate too
        self.assertEqual(got['fixcheck:check:1'], ('fail', True, ['middle']))
        self.assertEqual(got['stranger:gate:1'][0], 'error')
        self.assertFalse(report['ok'])
        texts = {r['id']: preflight.describe(r) for r in report['results']}
        self.assertEqual(texts['fix:gate:1'], 'fails as intended (waits for make)')
        self.assertIn("names an output of fix, which 'stranger' does not need", texts['stranger:gate:1'])

    def test_expected_failure_gates_in_check_gates(self):
        """pre: expected-failure gates in check-gates (PRE-10): one that names its task's missing
        output fails as intended; one that already fails with its pattern on the untouched tree
        passes with a note; a wrong failure is an error"""
        self.write('tests/old_test.sh', 'echo "ZeroDivisionError: boom"; exit 1\n')
        wf = self.workflow(['{run="test -s out.txt && sh out.txt",expect="fail",fail_pattern="ZeroDivisionError"}',
                            '{run="sh tests/old_test.sh",expect="fail",fail_pattern="ZeroDivisionError"}',
                            '{run="sh tests/old_test.sh",expect="fail",fail_pattern="KeyError"}',
                            '{run="nonexistent-command-xyz",expect="fail",fail_pattern="."}'])
        report = preflight.check_gates(wf)
        self.assertEqual([(r['result'], r['fails_as_intended']) for r in report['results']],
                         [('fail', True), ('pass', False), ('error', False), ('error', False)])
        self.assertEqual(report['results'][0]['expect'], 'fail')
        self.assertEqual(preflight.describe(report['results'][1]),
                         'pass: the defect already reproduces through existing files')
        self.assertEqual(len([f for f in os.listdir(report['directory']) if f.endswith('.log')]), 4)

    def test_a_missing_script_waits_for_its_task(self):
        """pre: a command that cannot run because the script it runs is a missing output (PRE-11):
        `sh tests/regression/t.sh` exits 127 before reproduce writes the script; the expect-fail
        gate on reproduce and the `new` gate on fix fail as intended, fix's waiting for
        reproduce; the same command after a timeout, or naming nothing missing, is an error"""
        wf = self.load('''name="gates"
[[task]]
id = "reproduce"
type = "implement"
prompt = "p"
outputs = ["tests/regression/t.sh"]
gate = [{run="sh tests/regression/t.sh",expect="fail",fail_pattern="SYMPTOM"}]
[[task]]
id = "fix"
type = "implement"
prompt = "p"
needs = ["reproduce"]
outputs = ["src.txt"]
gate = [{run="sh tests/regression/t.sh",new=true,fail_pattern="SYMPTOM"}, "sh tests/other.sh"]
[[task]]
id = "slow"
type = "implement"
prompt = "p"
needs = ["reproduce"]
outputs = ["slow.txt"]
gate = ["sleep 60; sh tests/regression/t.sh"]
''')
        self.assertLoads(wf)
        self.commit()
        wf.tasks[2]['gate_timeout_min'] = .001
        report = preflight.check_gates(wf)
        got = {r['id']: (r['result'], r['fails_as_intended'], r['waits_for']) for r in report['results']}
        self.assertEqual(got['reproduce:gate:1'], ('fail', True, []))
        self.assertEqual(got['fix:gate:1'], ('fail', True, ['reproduce']))
        self.assertEqual(got['fix:gate:2'], ('error', False, []))           # names nothing missing
        self.assertEqual(got['slow:gate:1'], ('error', False, []))          # a timeout never waits
        texts = {r['id']: preflight.describe(r) for r in report['results']}
        self.assertEqual(texts['fix:gate:1'], 'fails as intended (waits for reproduce)')

    def test_glob_outputs_count_as_waited_for(self):
        """pre: glob outputs count as waited for (PRE-11): a command naming a glob output's literal
        prefix, or a path the glob matches, waits for it while the clone holds no match, as the refactoring template's steps run the
        characterisation tests; once a file matches, a failure is a plain failure"""
        source = '''name="gates"
[[task]]
id = "characterise"
type = "implement"
prompt = "p"
outputs = ["tests/char/x/**"]
gate = ["test -s tests/char/x/run.sh"]
[[task]]
id = "step-1"
type = "implement"
prompt = "p"
needs = ["characterise"]
outputs = ["src/a.txt"]
gate = ["test -d tests/char/x", "sh tests/char/x/run.sh"]
'''
        wf = self.load(source)
        self.assertLoads(wf)
        self.commit()
        got = {r['id']: (r['result'], r['fails_as_intended'], r['waits_for'])
               for r in preflight.check_gates(wf)['results']}
        self.assertEqual(got, {'characterise:gate:1': ('fail', True, []),
                               'step-1:gate:1': ('fail', True, ['characterise']),
                               'step-1:gate:2': ('fail', True, ['characterise'])})
        self.write('tests/char/x/old.txt', 'already here\n')
        self.commit()
        report = preflight.check_gates(wf)
        self.assertEqual([(r['result'], r['fails_as_intended']) for r in report['results']],
                         [('fail', False), ('pass', False), ('error', False)])

    def test_a_failed_clone_is_a_record_error(self):
        """pre: a clone that fails ends as a RecordError, not a CalledProcessError traceback"""
        from unittest import mock
        wf = self.workflow(['"true"'])
        real = preflight.subprocess.run
        def run(argv, *args, **kw):
            if 'clone' in argv:
                return preflight.subprocess.CompletedProcess(argv, 128, b'', b'fatal: nope')
            return real(argv, *args, **kw)
        with mock.patch.object(preflight.subprocess, 'run', side_effect=run):
            with self.assertRaisesRegex(record.RecordError, 'could not clone.*nope'):
                preflight.check_gates(wf)

    def test_wrong_failures_are_errors(self):
        wf = self.workflow(['{run="nonexistent-command-xyz",new=true,fail_pattern="missing"}',
                            '{run="exit 126",new=true,fail_pattern="."}',
                            '{run="echo ModuleNotFoundError; exit 1",new=true,fail_pattern="expected assertion"}'])
        report = preflight.check_gates(wf)
        self.assertEqual([r['result'] for r in report['results']], ['error'] * 3)

    def test_litter_is_reported_and_original_tree_untouched(self):
        wf = self.workflow(['"echo bad > README.md; echo stray > junk.txt"', '"grep -qx scratch README.md"'])
        original = Path(self.root, 'README.md').read_bytes()
        report = preflight.check_gates(wf)
        self.assertEqual(report['results'][0]['result'], 'error')
        self.assertIn('junk.txt', ' '.join(report['results'][0]['changed_paths']))
        self.assertEqual(report['results'][1]['result'], 'pass')
        self.assertEqual(Path(self.root, 'README.md').read_bytes(), original)
        self.assertFalse(Path(self.root, 'junk.txt').exists())
        self.assertTrue(gitops.Git(self.root).is_clean())

    def test_timeout_is_an_error(self):
        wf = self.workflow(['{run="sleep 60",new=true,fail_pattern="."}'])
        wf.tasks[0]['gate_timeout_min'] = .001
        self.assertEqual(preflight.check_gates(wf)['results'][0]['result'], 'error')

    def test_check_gates_runs_standalone_checks_too(self):
        wf = self.workflow(['"true"'], '[[task]]\nid="check"\ntype="check"\nrun=["true"]\n')
        result = run_cli('check-gates', wf.workflow_file)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('check:check:1: pass', result.stdout)

if __name__ == '__main__':
    unittest.main()
