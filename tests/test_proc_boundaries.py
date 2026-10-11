"""Completed invocations still own any process group whose cleanup failed."""
import io
import json
import os
import shlex
import sys
from unittest.mock import patch

from helpers import EngineCase, done
from codesmith import agents, proc, record


class CleanupBoundary(EngineCase):
    def test_saved_answer_waits_for_cleanup(self):
        """proc: a saved answer waits for its live child's cleanup (PROC-21)"""
        marker = os.path.join(self.side, 'gate-started')
        self.workflow('[[task]]\nid="make"\ntype="implement"\nprompt="p"\n'
                      'outputs=["src/a"]\ngate=[' + repr('touch ' + shlex.quote(marker)) + ']\n')
        self.script([{'run': ['sleep 300 </dev/null >/dev/null 2>&1 &'],
                      'write': {'src/a': 'good\n'}, 'answer': done()}])
        original = proc.stop_group
        failed = []
        def stop(process, identity, *args, **kwargs):
            # This test has one author; qualification probes are not the named task.
            if os.path.exists(self.script_path + '.counter') and not failed:
                failed.append(identity)
                self.addCleanup(record.stop_process_group, identity, .1)
                raise record.RecordError('injected make cleanup failure')
            return original(process, identity, *args, **kwargs)
        with patch.object(proc, 'stop_group', side_effect=stop):
            self.assertEqual(self.start(), 2, self.output)
        run = self.the_run()
        self.assertTrue(record.is_alive(failed[0]))
        self.assertEqual(run.state['status'], 'stopped')
        entry = run.state['cleanup_obligations'][0]
        self.assertEqual(entry['cleanup']['status'], 'open')
        saved = run.state['tasks']['make']['pending_author_result']['result']
        self.assertEqual(agents.AgentResult(**saved).cleanup, entry['cleanup'])
        run.save()
        self.assertEqual(self.the_run().state['cleanup_obligations'], [entry])
        self.assertFalse(os.path.exists(marker))
        self.assertNotIn('src/a', self.git_out('ls-tree', '-r', '--name-only', 'HEAD'))
        with open(os.path.join(run.path, 'STATUS.md')) as fh:
            self.assertIn('cleanup open', fh.read())
        self.assertEqual(self.runner('approve', 'latest', 'make', '-C', self.root), 2)
        for failure in (False, record.RecordError('injected cleanup inspection failure')):
            with self.subTest(retry=failure):
                options = ({'side_effect': failure} if isinstance(failure, Exception)
                           else {'return_value': failure})
                with patch.object(record, 'stop_process_group', **options):
                    self.assertEqual(self.resume('--stop-orphans'), 2, self.output)
                self.assertEqual(self.the_run().state['cleanup_obligations'][0]['cleanup']['status'], 'open')
                self.assertFalse(os.path.exists(marker))
                self.assertEqual(self.calls(), 1)
        self.assertEqual(self.resume('--stop-orphans'), 0, self.output)
        self.assertFalse(record.is_alive(failed[0]))
        self.assertTrue(os.path.exists(marker))
        self.assertEqual(self.calls(), 1)
        self.assertEqual(self.status('make'), 'accepted')
        self.assertEqual(self.the_run().state['cleanup_obligations'][0]['cleanup']['status'], 'closed')
        self.check_invariants()

    def test_cleanup_stop_is_not_the_owners_pause(self):
        """proc: a cleanup stop has its own event, leaves a waiting pause request in place and
        ends with the cleanup's remedy, not a plain resume (PROC-30)"""
        self.workflow('[[task]]\nid="make"\ntype="implement"\nprompt="p"\n'
                      'outputs=["src/a"]\ngate=["test -f src/a"]\n')
        self.script([{'run': ['sleep 300 </dev/null >/dev/null 2>&1 &'],
                      'write': {'src/a': 'good\n'}, 'answer': done()}])
        original = proc.stop_group
        failed = []
        def stop(process, identity, *args, **kwargs):
            # The named caller: the author's call (qualification probes run before its counter).
            if os.path.exists(self.script_path + '.counter') and not failed:
                failed.append(identity)
                self.addCleanup(record.stop_process_group, identity, .1)
                # The owner asked for a pause while the call ran; it waits for a safe point.
                self.the_run().request_pause()
                raise record.RecordError('injected make cleanup failure')
            return original(process, identity, *args, **kwargs)
        with patch.object(proc, 'stop_group', side_effect=stop):
            self.assertEqual(self.start(), 2, self.output)
        run = self.the_run()
        self.assertEqual(run.state['status'], 'stopped')
        with open(os.path.join(run.path, 'events.jsonl'), encoding='utf-8') as fh:
            events = [json.loads(line)['event'] for line in fh]
        self.assertIn('cleanup-stop', events)
        self.assertNotIn('pause-stop', events)
        self.assertTrue(run.pause_requested())
        line = [l for l in self.last[2].splitlines() if l.startswith('runner: ')][-1]
        self.assertIn('cleanup open for', line)
        self.assertTrue(line.endswith('records your ruling that they are gone'), line)
        self.assertNotIn('Continue with runner resume', self.output)
        self.assertIn('--stop-orphans', run.state['stop_reason'])
        # STATUS.md's Next block names the cleanup's remedy too, never a budget one (V-04).
        with open(os.path.join(run.path, 'STATUS.md'), encoding='utf-8') as fh:
            page = fh.read()
        following = page.split('## Next', 1)[1]
        self.assertIn(f'runner resume {run.name} --stop-orphans', following)
        self.assertIn(f'runner resume {run.name} --abandon-cleanup', following)
        self.assertNotIn('--add-budget', following)
        # The waiting request is honoured at the next safe point, after the cleanup closed.
        self.assertEqual(self.resume('--stop-orphans'), 2, self.output)
        self.assertEqual(self.the_run().state['cleanup_obligations'][0]['cleanup']['status'], 'closed')
        self.assertIn('paused at the owner\'s request', self.the_run().state['stop_reason'])
        self.assertFalse(self.the_run().pause_requested())
        self.assertEqual(self.resume(), 0, self.output)
        self.assertEqual(self.status('make'), 'accepted')
        self.check_invariants()

    def test_a_probe_whose_cleanup_fails_fails_resume(self):
        """proc: a qualification probe of a resume whose group stop fails fails the resume naming
        its group, records the probes' spend, and the next resume qualifies again (PROC-31)"""
        self.workflow('[[task]]\nid="make"\ntype="implement"\nprompt="p"\noutputs=["src/a"]\n'
                      'gate=["test -f src/a"]\n[[task]]\nid="look"\ntype="human"\nneeds=["make"]\n')
        self.script([{'write': {'src/a': 'good\n'}, 'answer': done()}])
        self.assertEqual(self.start(), 255, self.output)
        cache = os.path.join(self.root, '.runs', 'qualification-cache.json')
        os.unlink(cache)                                  # resume qualifies again
        calls = self.calls()
        original, failed = proc.stop_group, []
        def stop(process, identity, *args, **kwargs):
            # Every call of this resume is a probe: the run waits for a person, no task runs.
            if not failed:
                failed.append(identity)
                self.addCleanup(record.stop_process_group, identity, .1)
                raise record.RecordError('injected probe cleanup failure')
            return original(process, identity, *args, **kwargs)
        before = self.the_run().state['spend']['unpriced']['calls']
        with patch.object(proc, 'stop_group', side_effect=stop):
            self.assertEqual(self.resume(), 2, self.output)
        self.assertIn(f"left process group {failed[0]['pgid']} running: injected probe cleanup "
                      "failure", self.output)
        self.assertIn('then run this command again: it qualifies again', self.output)
        self.assertGreater(self.the_run().state['spend']['unpriced']['calls'], before)
        self.assertEqual(self.calls(), calls)             # no task call was made
        self.assertEqual(self.the_run().state['status'], 'needs_human')
        self.assertEqual(self.resume(), 255, self.output)
        self.assertEqual(self.runner('approve', 'latest', 'look', '-C', self.root), 0, self.output)
        self.assertEqual(self.resume(), 0, self.output)
        self.check_invariants()

    def test_serial_replay_keeps_its_checkout_while_cleanup_is_open(self):
        """proc: a serial replay gate whose cleanup fails keeps its checkout and its replay intent
        until resume --stop-orphans closes the cleanup (PROC-22)"""
        from codesmith import checks
        self.workflow('[[task]]\nid="make"\ntype="implement"\nprompt="p"\noutputs=["src/a"]\n'
                      'gate=["test -f src/a && { sleep 300 </dev/null >/dev/null 2>&1 & }"]\n')
        self.script([{'write': {'src/a': 'good\n'}, 'answer': done()}])
        in_replay, failed = [], []
        run_commands, stop_group = checks.run_commands, proc.stop_group
        def commands(commands, **kwargs):
            in_replay.append('code-smith-replay-' in kwargs['cwd'])
            try:
                return run_commands(commands, **kwargs)
            finally:
                in_replay.pop()
        def stop(process, identity, *args, **kwargs):
            # The named caller: the replay's gate, never the live gate or the author.
            if in_replay and in_replay[-1] and not failed:
                failed.append(identity)
                self.addCleanup(record.stop_process_group, identity, .1)
                raise record.RecordError('injected replay cleanup failure')
            return stop_group(process, identity, *args, **kwargs)
        with patch.object(checks, 'run_commands', side_effect=commands), \
                patch.object(proc, 'stop_group', side_effect=stop):
            self.assertEqual(self.start(), 2, self.output)
        run = self.the_run()
        [replay] = [i for i in run.state['intents'] if i['kind'] == 'replay']
        self.assertTrue(os.path.isdir(replay['dir']))
        self.assertTrue(record.is_alive(failed[0]))
        self.assertEqual(run.state['cleanup_obligations'][0]['cleanup']['status'], 'open')
        # A plain resume signals nothing and removes nothing (PROC-19/20).
        self.assertEqual(self.resume(), 2, self.output)
        self.assertIn('--stop-orphans', self.output)
        self.assertNotIn('repair-record', self.output)
        self.assertTrue(os.path.isdir(replay['dir']))
        self.assertTrue(record.is_alive(failed[0]))
        self.assertEqual(self.resume('--stop-orphans'), 0, self.output)
        self.assertFalse(record.is_alive(failed[0]))
        self.assertFalse(os.path.exists(replay['dir']))
        self.assertEqual(self.calls(), 1)
        self.assertEqual(self.status('make'), 'accepted')
        self.assertEqual(self.the_run().state['cleanup_obligations'][0]['cleanup']['status'], 'closed')
        self.check_invariants()

    def test_cleanup_that_never_succeeds_has_its_remedy_and_a_way_out(self):
        """proc: an open cleanup is refused with its own remedy, never repair-record; plain resume
        signals nothing; when the stop keeps failing, resume --abandon-cleanup records a person's
        ruling and the run reaches done (PROC-23)"""
        from codesmith import cli
        self.workflow('[[task]]\nid="make"\ntype="implement"\nprompt="p"\noutputs=["src/a"]\n'
                      'gate=["test -f src/a"]\n', defaults='rulings_require_interactive=true')
        self.script([{'run': ['sleep 300 </dev/null >/dev/null 2>&1 &'],
                      'write': {'src/a': 'good\n'}, 'answer': done()}])
        original, failed = proc.stop_group, []
        def stop(process, identity, *args, **kwargs):
            if os.path.exists(self.script_path + '.counter') and not failed:
                failed.append(identity)
                self.addCleanup(record.stop_process_group, identity, .1)
                raise record.RecordError('injected make cleanup failure')
            return original(process, identity, *args, **kwargs)
        with patch.object(proc, 'stop_group', side_effect=stop):
            self.assertEqual(self.start(), 2, self.output)
        state = self.the_run().state
        for command in (('approve', 'latest', 'make'), ('reject', 'latest', 'make', '-m', 'no'),
                        ('retry', 'latest', 'make'),
                        ('resolve', 'latest', 'make/PE-1', '--as', 'advisory', '-m', 'n'),
                        ('replan', 'latest')):
            with self.subTest(command=command[0]):
                self.assertEqual(self.runner(*command, '-C', self.root), 2, self.output)
                self.assertIn("an invocation's cleanup is open", self.output)
                self.assertIn('--stop-orphans', self.output)
                self.assertIn('--abandon-cleanup', self.output)
                self.assertNotIn('repair-record', self.output)
                self.assertNotIn('the run record was changed', self.output)
        self.assertEqual(self.resume(), 2, self.output)
        self.assertIn('--stop-orphans', self.output)
        self.assertTrue(record.is_alive(failed[0]))              # not signalled
        with patch.object(record, 'stop_process_group', return_value=False):
            self.assertEqual(self.resume('--stop-orphans'), 2, self.output)
        self.assertIn('could not close it', self.output)
        self.assertIn('--abandon-cleanup', self.output)
        self.assertEqual(self.the_run().state, state)
        for name in cli.AGENT_SESSION_MARKERS:
            os.environ.pop(name, None)
        with patch.object(sys, 'stdin', io.StringIO()):            # not a terminal: refused
            self.assertEqual(self.resume('--abandon-cleanup', '--by', 'owner'), 2, self.output)
        self.assertIn('rulings_require_interactive', self.output)
        self.assertEqual(self.the_run().state, state)

        class Terminal(io.StringIO):
            def isatty(self):
                return True
        with patch.object(sys, 'stdin', Terminal()):
            self.assertEqual(self.resume('--abandon-cleanup', '--by', 'owner'), 0, self.output)
        self.assertTrue(record.is_alive(failed[0]))              # abandoned, never signalled
        run = self.the_run()
        self.assertEqual(run.state['status'], 'done')
        self.assertEqual(self.status('make'), 'accepted')
        self.assertEqual(self.calls(), 1)
        entry = run.state['cleanup_obligations'][0]
        self.assertEqual(entry['cleanup']['status'], 'abandoned')
        self.assertEqual({k: entry['ruling'][k] for k in ('by', 'by_source', 'interactive', 'agent_markers')},
                         {'by': 'owner', 'by_source': 'flag', 'interactive': True, 'agent_markers': []})
        with open(os.path.join(run.path, 'events.jsonl'), encoding='utf-8') as fh:
            events = [e for e in map(json.loads, fh) if e['event'] == 'cleanup-abandoned']
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['by'], 'owner')
        self.check_invariants()

    def open_cleanup(self):
        """A stopped run whose author's cleanup failed, its group alive; returns that group."""
        self.workflow('[[task]]\nid="make"\ntype="implement"\nprompt="p"\noutputs=["src/a"]\n'
                      'gate=["test -f src/a"]\n')
        self.script([{'run': ['sleep 300 </dev/null >/dev/null 2>&1 &'],
                      'write': {'src/a': 'good\n'}, 'answer': done()}])
        original, failed = proc.stop_group, []
        def stop(process, identity, *args, **kwargs):
            if os.path.exists(self.script_path + '.counter') and not failed:
                failed.append(identity)
                self.addCleanup(record.stop_process_group, identity, .1)
                raise record.RecordError('injected make cleanup failure')
            return original(process, identity, *args, **kwargs)
        with patch.object(proc, 'stop_group', side_effect=stop):
            self.assertEqual(self.start(), 2, self.output)
        self.assertEqual(self.the_run().state['cleanup_obligations'][0]['cleanup']['status'], 'open')
        return failed[0]

    def test_resume_cleanup_flags_are_checked_together(self):
        """proc: resume refuses --by without --abandon-cleanup, recording nothing, and
        --abandon-cleanup on a record missing its frozen workflow is the repair-record refusal,
        not a traceback (PROC-27)"""
        group = self.open_cleanup()
        run = self.the_run()
        state = run.state
        with open(os.path.join(run.path, 'events.jsonl'), 'rb') as fh:
            events = fh.read()
        for flags, says in ((('--by', 'owner'), '--by needs --abandon-cleanup'),
                            (('--by', 'owner', '--stop-orphans'), '--by needs --abandon-cleanup')):
            with self.subTest(flags=flags):
                self.assertEqual(self.resume(*flags), 2, self.output)
                self.assertIn(says, self.output)
                self.assertIn('nothing was recorded', self.output)
                self.assertEqual(self.the_run().state, state)
                with open(os.path.join(run.path, 'events.jsonl'), 'rb') as fh:
                    self.assertEqual(fh.read(), events)
                self.assertTrue(record.is_alive(group))          # nothing signalled
        frozen = os.path.join(run.path, 'workflow.expanded.json')
        os.rename(frozen, frozen + '.aside')
        self.assertEqual(self.resume('--abandon-cleanup', '--by', 'owner'), 2, self.output)
        self.assertIn('workflow.expanded.json was removed', self.output)
        self.assertIn('repair-record', self.output)
        self.assertNotIn('Traceback', self.output)
        self.assertEqual(self.the_run().state, state)
        os.rename(frozen + '.aside', frozen)
        self.assertEqual(self.resume('--abandon-cleanup', '--by', 'owner'), 0, self.output)
        self.assertEqual(self.the_run().state['cleanup_obligations'][0]['cleanup']['status'], 'abandoned')
        self.assertEqual(self.status('make'), 'accepted')
        self.check_invariants()

    def test_cleanup_abandoned_event_keeps_its_own_stamp(self):
        """rec: the cleanup-abandoned event's at is the record's stamp and the ruling's time is
        ruled_at; STATUS renders the event and its time; a payload never shadows the event's at
        (REC-60)"""
        from codesmith import gitops, status
        self.open_cleanup()
        run = self.the_run()
        ruling = {'by': 'owner', 'by_source': 'flag', 'interactive': True, 'agent_markers': [],
                  'at': '2026-10-10T00:00:00+00:00'}
        record.reconcile(run, gitops.Git(self.root), abandon=lambda: dict(ruling))
        self.assertEqual(run.state['cleanup_obligations'][0]['ruling'], ruling)
        with open(os.path.join(run.path, 'events.jsonl'), encoding='utf-8') as fh:
            events = [json.loads(line) for line in fh]
        [event] = [e for e in events if e['event'] == 'cleanup-abandoned']
        self.assertEqual(event['ruled_at'], ruling['at'])
        self.assertIsNotNone(status._parse_at(event['at']))
        self.assertNotEqual(event['at'], ruling['at'])
        self.assertEqual(events[-1], event)                    # the last event: STATUS's "Updated"
        with open(os.path.join(run.path, 'STATUS.md'), encoding='utf-8') as fh:
            page = fh.read()
        stamp = status._parse_at(event['at']).strftime('%Y-%m-%d %H:%M:%S UTC')
        self.assertIn(f'Updated {stamp}', page)
        self.assertIn('cleanup-abandoned', page)
        self.assertIn('ruled_at=2026-10-10T00:00:00+00:00', page)
        with self.assertRaises(ValueError):
            run.event('cleanup-abandoned', at='2026-10-10T00:00:00+00:00')

    def live_group(self):
        """A process group that outlives its call (its stop injected to fail), as a dead runner
        leaves one; returns its identity."""
        started = []
        def stop(process, identity, *args, **kwargs):
            raise record.RecordError('injected stop failure of the orphan')
        with patch.object(proc, 'stop_group', side_effect=stop):
            res = proc.run_process(['/bin/sh', '-c', 'sleep 300 </dev/null >/dev/null 2>&1 &'],
                                   cwd=self.side, env=dict(os.environ), timeout_s=60,
                                   stdout_path=os.path.join(self.side, 'orphan.log'),
                                   on_start=lambda identity, *rest: started.append(identity))
        self.addCleanup(record.stop_process_group, res.cleanup['group'], .1)
        self.assertTrue(record.is_alive(res.cleanup['group']))
        return res.cleanup['group']

    def test_abandon_and_stop_orphans_take_one_resume(self):
        """proc: an open cleanup whose stop keeps failing beside a live orphan: --abandon-cleanup
        alone refuses before recording the ruling, --stop-orphans alone cannot close it, and
        --abandon-cleanup --stop-orphans abandons it and stops the orphan in one resume (PROC-28)"""
        group = self.open_cleanup()
        orphan = self.live_group()
        run = self.the_run()
        op = run.begin('command', task='make', command='left by a dead runner')
        run.amend(op, process=orphan)
        state = self.the_run().state
        with open(os.path.join(run.path, 'events.jsonl'), 'rb') as fh:
            events = fh.read()

        def unchanged():
            self.assertEqual(self.the_run().state, state)
            with open(os.path.join(run.path, 'events.jsonl'), 'rb') as fh:
                self.assertEqual(fh.read(), events)
            self.assertTrue(record.is_alive(group) and record.is_alive(orphan))
        self.assertEqual(self.resume('--abandon-cleanup', '--by', 'owner'), 2, self.output)
        self.assertIn('is still running', self.output)
        self.assertIn('Nothing was recorded', self.output)
        self.assertIn('--abandon-cleanup --stop-orphans', self.output)
        self.assertNotIn('cleanup abandoned', self.output)
        unchanged()
        with patch.object(record, 'stop_process_group', return_value=False):
            self.assertEqual(self.resume('--stop-orphans'), 2, self.output)
        self.assertIn('could not close it', self.output)
        unchanged()
        self.assertEqual(self.resume('--abandon-cleanup', '--stop-orphans', '--by', 'owner'), 0,
                         self.output)
        self.assertIn('cleanup abandoned by owner; its group was not signalled; --stop-orphans '
                      'stops only the groups of interrupted calls', self.output)
        self.assertTrue(record.is_alive(group))                   # abandoned: never signalled
        self.assertFalse(record.is_alive(orphan))                 # the orphan: stopped
        run = self.the_run()
        self.assertEqual(run.state['cleanup_obligations'][0]['cleanup']['status'], 'abandoned')
        self.assertEqual(run.state['cleanup_obligations'][0]['ruling']['by'], 'owner')
        self.assertEqual(self.status('make'), 'accepted')
        self.assertEqual(self.calls(), 1)
        self.check_invariants()

    def test_an_error_after_a_closed_cleanup_names_what_was_recorded(self):
        """proc: when resume closes or abandons a cleanup and then fails on an orphan it cannot
        stop, the error names the cleanup recorded before it, never "Nothing was changed"
        (PROC-28)"""
        self.open_cleanup()
        orphan = self.live_group()
        run = self.the_run()
        run.amend(run.begin('command', task='make', command='left by a dead runner'), process=orphan)
        stop = record.stop_process_group
        with patch.object(record, 'stop_process_group',
                          side_effect=lambda identity, *a, **k: False if identity == orphan
                          else stop(identity, *a, **k)):
            self.assertEqual(self.resume('--abandon-cleanup', '--stop-orphans', '--by', 'owner'),
                             2, self.output)
        self.assertIn('could not stop invocation', self.output)
        self.assertIn('Recorded before it: tasks/010-make/attempt-1/invocation-1: cleanup abandoned',
                      self.output)
        self.assertNotIn('Nothing was changed', self.output)
        self.assertEqual(self.the_run().state['cleanup_obligations'][0]['cleanup']['status'], 'abandoned')
        self.assertEqual(self.resume('--stop-orphans'), 0, self.output)
        self.assertFalse(record.is_alive(orphan))
        self.assertEqual(self.status('make'), 'accepted')
        self.check_invariants()
