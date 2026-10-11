"""PROC: real process groups and deterministic launch/crash barriers; no provider calls."""

import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from helpers import EngineCase, RUNNER, done
from test_record import RunCase
from codesmith import gitops, proc, record

SOURCE = str(Path(RUNNER).parent / 'src')


class InvocationProcesses(EngineCase):
    def wait_for(self, condition):
        deadline = time.monotonic() + 45             # allow a loaded host to publish the barrier
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(.02)
        self.fail('process barrier was not reached')

    def start_child(self, code=None):
        argv = ([sys.executable, '-c', code] if code else [sys.executable, RUNNER])
        child = subprocess.Popen([*argv, 'start', self.wf_path], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE)
        root = self.root
        def cleanup():
            if child.poll() is None:
                child.kill()
            try:
                run = record.Run.load(record.resolve_run(os.path.join(root, '.runs')))
            except (OSError, ValueError, record.RecordError):
                run = None
            if run:
                for intent in run.state['intents']:
                    if intent.get('process'):
                        record.stop_process_group(intent['process'], .1)
            child.communicate(timeout=10)
        self.addCleanup(cleanup)
        return child

    def test_launch_requires_a_durable_identity(self):
        """proc: no task executes before identity publication (PROC-01)"""
        for kind in ('agent', 'command'):
            for phase in ('before', 'during', 'after'):
                with self.subTest(kind=kind, phase=phase):
                    if kind != 'agent' or phase != 'before':
                        self.setUp()
                    marker = Path(self.side, 'barrier.json')
                    effect = Path(self.side, 'executed')
                    command = 'touch ' + shlex.quote(str(effect))
                    if kind == 'agent':
                        self.workflow('[[task]]\nid="make"\ntype="implement"\nprompt="p"\n'
                                      'outputs=["src/a.txt"]\ngate=["true"]\n')
                        self.script([{'run': [command], 'write': {'src/a.txt': 'good\n'},
                                      'answer': done()}])
                    else:
                        self.workflow('[[task]]\nid="check"\ntype="check"\nrun=['
                                      + json.dumps(command) + ']\n')
                    code = f'''
import sys, time
sys.path.insert(0, {SOURCE!r})
from codesmith import cli, record
original = record.Run.amend
def amend(run, op, **more):
    identity = more.get('process')
    if identity:
        def barrier(*_):
            record.write_durable({str(marker)!r}, record.dump_json(identity))
            while True: time.sleep(1)
        if {phase!r} == 'before': barrier()
        if {phase!r} == 'during': run.crash = barrier
        original(run, op, **more)
        barrier()
    return original(run, op, **more)
record.Run.amend = amend
sys.exit(cli.main(sys.argv[1:]))
'''
                    runner = self.start_child(code)
                    self.wait_for(marker.exists)
                    identity = record.read_json(marker)
                    self.addCleanup(record.stop_process_group, identity, .1)
                    self.assertFalse(effect.exists())
                    runner.kill()
                    runner.communicate(timeout=10)
                    self.wait_for(lambda: not record.is_alive(identity))
                    self.assertEqual(record.group_pids(identity['pgid']), [])
                    self.assertFalse(effect.exists())
                    run = self.the_run()
                    intent = next(i for i in run.state['intents'] if i['kind'] == kind)
                    self.assertEqual(intent.get('process'), identity if phase == 'after' else None)
                    self.assertEqual(self.resume(), 0, self.output)
                    self.assertTrue(effect.exists())
                    self.check_invariants()

    def orphan(self, kind, leader_waits=False):
        side = Path(self.side)
        descendant = self.write('descendant.py', f'''
import os, signal, time
from pathlib import Path
signal.signal(signal.SIGINT, signal.SIG_IGN)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path({str(side / 'child')!r}).write_text(str(os.getpid()))
while True:
    Path('writer.txt').write_text('writing')
    time.sleep(.02)
''')
        shell = self.write('leader.sh', f'''
trap 'exit 0' INT TERM
echo $$ > {shlex.quote(str(side / 'leader'))}
{shlex.quote(sys.executable)} {shlex.quote(descendant)} &
while test ! -f {shlex.quote(str(side / 'release'))}; do sleep .02; done
'''+ ('while :; do sleep 1; done\n' if leader_waits else 'exit 0\n'))
        command = 'sh ' + shlex.quote(shell)
        if kind == 'agent':
            self.workflow('[[task]]\nid="make"\ntype="implement"\nprompt="p"\n'
                          'outputs=["writer.txt"]\nwrites=["writer.txt"]\ngate=["true"]\n')
            self.script([{'run': [command], 'answer': done()},
                         {'write': {'writer.txt': 'finished\n'}, 'answer': done()}])
        else:
            self.workflow('[[task]]\nid="check"\ntype="check"\nrestores=true\nrun=['
                          + json.dumps('if test ! -f ' + shlex.quote(str(side / 'release'))
                                       + '; then ' + command + '; fi') + ']\n')
        runner = self.start_child()
        self.wait_for(lambda: (side / 'child').exists() and (side / 'leader').exists())
        run = self.the_run()
        intent = next(i for i in run.state['intents'] if i['kind'] == kind)
        identity = intent['process']
        self.addCleanup(record.stop_process_group, identity, .1)
        child = int((side / 'child').read_text())
        leader = int((side / 'leader').read_text())
        leader_identity = record.process_identity(leader)
        runner.kill()
        runner.communicate(timeout=10)
        (side / 'release').touch()
        if not leader_waits:
            self.wait_for(lambda: not record.is_alive(leader_identity))
        return identity, child, leader, intent

    def test_dead_cli_leader_does_not_hide_a_writer(self):
        """proc: resume refuses descendants after the CLI leader exits (PROC-02)"""
        for kind in ('command', 'agent'):
            with self.subTest(kind=kind):
                if kind == 'agent':
                    self.setUp()
                identity, child, leader, intent = self.orphan(kind)
                pids = record.invocation_pids(identity)
                self.assertIn(child, pids)
                self.assertNotIn(leader, pids)
                self.assertTrue(record.is_alive(identity))
                with mock.patch.object(proc, 'run_process', side_effect=AssertionError('dispatch')), \
                     mock.patch.object(gitops.Git, 'snapshot', side_effect=AssertionError('snapshot')):
                    self.assertEqual(self.resume(), 2, self.output)
                self.assertIn('still running', self.output)
                self.assertIn(str(child), self.output)
                self.assertIn(str(identity['pid']), self.output)
                self.assertIn(intent.get('invocation_dir', intent['op']), self.output)
                self.assertTrue(record.stop_process_group(identity, .1))
                self.assertEqual(self.resume(), 0, self.output)
                self.check_invariants()

    def test_stop_orphans_waits_for_resistant_descendants(self):
        """proc: orphan shutdown escalates past the CLI leader (PROC-03)"""
        for kind in ('command', 'agent'):
            with self.subTest(kind=kind):
                if kind == 'agent':
                    self.setUp()
                identity, child, leader, _intent = self.orphan(kind, leader_waits=True)
                launch, stop, seen = proc.run_process, record.stop_process_group, []
                def dispatch(*args, **kwargs):
                    self.assertFalse(record.is_alive(identity))
                    self.assertEqual(record.group_pids(identity['pgid']), [])
                    self.assertIsNone(record.process_identity(child))
                    self.assertIsNone(record.process_identity(leader))
                    seen.append(True)
                    return launch(*args, **kwargs)
                signal_group = os.killpg
                def send(pgid, sig):
                    if pgid == identity['pgid'] and sig == signal.SIGTERM:
                        self.assertIsNone(record.process_identity(leader))
                        self.assertIsNotNone(record.process_identity(child))
                    signal_group(pgid, sig)
                with mock.patch.object(proc, 'run_process', dispatch), \
                     mock.patch.object(record, 'stop_process_group', lambda ident, grace_s=5, **kw: stop(ident, .1, **kw)), \
                     mock.patch.object(os, 'killpg', wraps=send) as signals:
                    self.assertEqual(self.resume('--stop-orphans'), 0, self.output)
                sent = [call.args[1] for call in signals.call_args_list if call.args[0] == identity['pgid']]
                self.assertEqual(sent, [signal.SIGINT, signal.SIGTERM, signal.SIGKILL])
                self.assertTrue(seen)
                self.check_invariants()

    def test_checkout_edits_do_not_break_the_next_supervisor(self):
        """proc: supervisors survive edits to the runner checkout (PROC-06)"""
        copied = Path(self.side, 'runner-copy')
        shutil.copytree(Path(RUNNER).parent, copied, ignore=shutil.ignore_patterns('__pycache__'))
        self.workflow('[[task]]\nid="make"\ntype="implement"\nprompt="p"\n'
                      'outputs=["src/a.txt"]\ngate=["true"]\n'
                      '[[task]]\nid="next"\ntype="implement"\nprompt="p"\n'
                      'needs=["make"]\noutputs=["src/b.txt"]\ngate=["true"]\n')
        damage = 'printf "syntax error!\\n" > ' + shlex.quote(str(copied / 'src/codesmith/record.py'))
        self.script({'make': [{'run': [damage], 'write': {'src/a.txt': 'a'}, 'answer': done()}],
                     'next': [{'write': {'src/b.txt': 'b'}, 'answer': done()}]})
        code = f"import sys; sys.path.insert(0, {str(copied / 'src')!r}); from codesmith import cli; sys.exit(cli.main(sys.argv[1:]))"
        runner = self.start_child(code)
        _out, err = runner.communicate(timeout=90)
        self.assertEqual(runner.returncode, 0, err.decode())
        self.assertEqual(self.status('next'), 'accepted')
        self.assertNotIn('call-interrupted', Path(self.the_run().path, 'events.jsonl').read_text())
        self.check_invariants()

    def test_dead_supervisor_can_be_stopped_on_resume(self):
        """proc: recovery stops a killed supervisor's surviving agent (PROC-08)"""
        self.workflow('[[task]]\nid="make"\ntype="implement"\nprompt="p"\n'
                      'outputs=["src/a.txt"]\ngate=["true"]\n')
        self.script([{'hang': True}, {'write': {'src/a.txt': 'done'}, 'answer': done()}])
        runner = self.start_child()
        def launched():
            try:
                return any(i.get('process', {}).get('group', {}).get('leader')
                           for i in self.the_run().state['intents']) and self.calls() == 1
            except (record.RecordError, FileNotFoundError):
                return False
        self.wait_for(launched)
        intent = next(i for i in self.the_run().state['intents'] if i['kind'] == 'agent')
        identity = intent['process']
        self.addCleanup(record.stop_process_group, identity, .1)
        runner.kill()
        runner.communicate(timeout=10)
        os.kill(identity['pid'], signal.SIGKILL)
        self.wait_for(lambda: not record.is_alive(identity['group']['supervisor']))
        self.assertTrue(record.is_alive(identity['group']['leader']))
        self.assertEqual(self.resume(), 2, self.output)
        stop = record.stop_process_group
        with mock.patch.object(record, 'stop_process_group', lambda ident, grace_s=5, **kw: stop(ident, .1, **kw)):
            self.assertEqual(self.resume('--stop-orphans'), 0, self.output)
        self.assertFalse(record.is_alive(identity))
        outcome = record.read_json(Path(self.the_run().path, intent['invocation_dir'], 'outcome.json'))
        self.assertEqual(outcome['status'], 'interrupted')
        self.check_invariants()

    def test_abrupt_exit_after_identity_acknowledgment_leaves_an_orphan(self):
        """proc: abrupt exit after identity acknowledgment leaves a recoverable orphan (PROC-18)"""
        effect = Path(self.side, 'started')
        command = (f'if test ! -f {shlex.quote(str(effect))}; then '
                   f'touch {shlex.quote(str(effect))}; sleep 600; fi')
        self.workflow('[[task]]\nid="check"\ntype="check"\nrun=[' + json.dumps(command) + ']\n')
        with mock.patch.dict(os.environ, CODE_SMITH_CRASH_AT='command:running'):
            runner = self.start_child()
        _out, err = runner.communicate(timeout=15)
        self.assertEqual(runner.returncode, 70, err.decode())
        intent = next(i for i in self.the_run().state['intents'] if i['kind'] == 'command')
        identity = intent['process']
        self.addCleanup(record.stop_process_group, identity, .1)
        self.assertIn('leader', identity['group'])
        self.wait_for(effect.exists)
        self.assertEqual(self.resume(), 2, self.output)
        self.assertIn('still running', self.output)
        stop = record.stop_process_group
        with mock.patch.object(record, 'stop_process_group', lambda ident, grace_s=5, **kw: stop(ident, .1, **kw)):
            self.assertEqual(self.resume('--stop-orphans'), 0, self.output)
        self.assertFalse(record.is_alive(identity))

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux child adoption is unavailable on macOS; detached daemons escape tracking')
    def test_detached_descendants_remain_live_until_stopped(self):
        """proc: Linux detached descendants remain part of the invocation (PROC-11)"""
        ready, release = Path(self.side, 'detached'), Path(self.side, 'release')
        script = self.write('detach.sh', f"""
setsid sh -c 'echo $$ > {shlex.quote(str(ready))}; sleep 20; echo late >> out.txt' &
while test ! -f {shlex.quote(str(release))}; do sleep .02; done
""")
        self.workflow('[[task]]\nid="make"\ntype="implement"\nprompt="p"\n'
                      'outputs=["out.txt"]\ngate=["true"]\n')
        self.script([{'run': ['sh ' + shlex.quote(script)], 'answer': done()},
                     {'write': {'out.txt': 'finished'}, 'answer': done()}])
        runner = self.start_child()
        self.wait_for(ready.exists)
        intent = next(i for i in self.the_run().state['intents'] if i['kind'] == 'agent')
        identity = intent['process']
        self.addCleanup(record.stop_process_group, identity, .1)
        detached = record.process_identity(int(ready.read_text()))
        self.addCleanup(lambda: record.is_alive(detached) and os.killpg(detached['pgid'], signal.SIGKILL))
        runner.kill()
        runner.communicate(timeout=10)
        release.touch()
        self.wait_for(lambda: not record.is_alive(identity['group']['leader']))
        self.assertIn(detached['pid'], record.invocation_pids(identity))
        self.assertEqual(self.resume(), 2, self.output)
        self.assertIn('still running', self.output)
        stop = record.stop_process_group
        with mock.patch.object(record, 'stop_process_group', lambda ident, grace_s=5, **kw: stop(ident, .1, **kw)):
            self.assertEqual(self.resume('--stop-orphans'), 0, self.output)
        self.assertFalse(record.is_alive(detached))
        self.assertEqual(Path(self.root, 'out.txt').read_text(), 'finished')
        self.check_invariants()


class InvocationCompatibility(RunCase):
    def child(self, code='import time; time.sleep(600)'):
        child = subprocess.Popen([sys.executable, '-c', code],
                                 start_new_session=True, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        def cleanup():
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)
        self.addCleanup(cleanup)
        return child

    def test_a_reused_group_is_not_signalled(self):
        """proc: a stale group identity never signals an unrelated group (PROC-04)"""
        child = self.child()
        actual = record.process_identity(child.pid)
        stale = dict(actual, start_ticks=actual['start_ticks'] - 1)
        stale['group'] = {'pgid': child.pid, 'supervisor': dict(stale)}
        self.run_.begin('command', task='design', command='true', process=stale)
        with mock.patch.object(os, 'killpg', wraps=os.killpg) as kill:
            self.assertFalse(record.is_alive(stale))
            self.assertTrue(record.stop_process_group(stale, .1))
            record.reconcile(self.reload(), self.g, stop_orphans=True, grace_s=.1)
        kill.assert_not_called()
        self.assertTrue(record.is_alive(actual))
        self.assertEqual(self.reload().state['intents'], [])

    def test_dead_supervisor_requires_a_surviving_start_identity(self):
        """proc: dead supervisors require a matching surviving member (PROC-13)"""
        child = self.child()
        actual = record.process_identity(child.pid)
        stale = dict(actual, start_ticks=actual['start_ticks'] - 1)
        anchor = dict(actual, pid=99999999, pgid=99999999)
        for witness in (stale, actual):
            identity = dict(anchor, group={'pgid': anchor['pid'], 'supervisor': anchor,
                                           'leader': witness, 'members': [witness]})
            with self.subTest(witness=witness), \
                 mock.patch.object(record, 'group_pids', return_value=[child.pid]), \
                 mock.patch.object(os, 'killpg') as kill_group, mock.patch.object(os, 'kill') as kill_pid:
                self.assertFalse(record.stop_process_group(identity, .1))
            kill_group.assert_not_called()
            kill_pid.assert_not_called()
        self.assertTrue(record.is_alive(actual))

    def test_old_records_keep_leader_only_recovery(self):
        """proc: old records retain leader-only recovery (PROC-05)"""
        ready = Path(self.root, 'legacy-child')
        code = ("import os, signal, time; from pathlib import Path; "
                "signal.signal(signal.SIGINT, signal.SIG_IGN); "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                f"Path({str(ready)!r}).write_text(str(os.getpid())); time.sleep(600)")
        child = self.child(f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {code!r}]); time.sleep(600)")
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(ready.exists())
        descendant = record.process_identity(int(ready.read_text()))
        self.addCleanup(lambda: record.is_alive(descendant) and os.kill(descendant['pid'], signal.SIGKILL))
        identity = record.process_identity(child.pid)
        self.assertNotIn('group', identity)
        self.run_.begin('command', task='design', command='true', process=identity)
        with self.assertRaises(record.OrphanAlive):
            record.reconcile(self.reload(), self.g)
        with mock.patch.object(record, 'group_pids', side_effect=AssertionError('new group lookup')):
            record.reconcile(self.reload(), self.g, stop_orphans=True, grace_s=.1)
        child.wait(timeout=10)
        self.assertFalse(record.is_alive(identity))
        self.assertTrue(record.is_alive(descendant))
        self.run_ = self.reload()
        self.run_.begin('command', task='design', command='true', process=identity)
        with mock.patch.object(os, 'killpg', wraps=os.killpg) as kill:
            record.reconcile(self.reload(), self.g, stop_orphans=True, grace_s=.1)
        kill.assert_not_called()
        self.assertEqual(self.reload().state['intents'], [])


class SupervisorFailures(EngineCase):
    def launch(self, **kwargs):
        return proc.run_process([sys.executable, '-c', 'print("finished")'],
                                cwd=self.root, env=dict(os.environ), timeout_s=5,
                                stdout_path=Path(self.side, 'stdout.log'),
                                stderr_path=Path(self.side, 'stderr.log'), **kwargs)

    def test_startup_failure_is_a_result(self):
        """proc: supervisor startup errors are not-started results (PROC-07)"""
        for source in ('raise ImportError("broken supervisor")', 'syntax error!'):
            with self.subTest(source=source), mock.patch.object(proc, '_SUPERVISOR_SOURCE', source):
                result = self.launch()
                self.assertEqual(result.status, 'not-started')
                self.assertIn('Error', result.error)
                self.assertTrue(result.stderr_tail)
        with mock.patch.object(proc.sys, 'executable', '/no/such/python'):
            self.assertEqual(self.launch().status, 'not-started')

    def test_fast_cli_still_has_a_start_identity(self):
        """proc: even an instant CLI publishes its start identity (PROC-14)"""
        seen = []
        result = proc.run_process(['/bin/sh', '-c', 'exit 0'], cwd=self.root,
                                  env=dict(os.environ), stdout_path=Path(self.side, 'fast.log'),
                                  on_start=lambda identity: seen.append(json.loads(json.dumps(identity))))
        self.assertEqual((result.status, result.returncode), ('exited', 0))
        self.assertEqual(len(seen), 2)
        leader = seen[-1]['group']['leader']
        self.assertTrue(leader.get('start_ticks') or leader.get('lstart'))
        self.assertNotEqual(leader['pid'], seen[-1]['pid'])

    def test_cli_waits_for_the_second_identity_save(self):
        """proc: task code waits for the leader identity save (PROC-15)"""
        effect = Path(self.side, 'executed')
        seen = []
        def started(identity):
            if 'leader' in identity['group']:
                time.sleep(.25)
                self.assertFalse(effect.exists())
                seen.append(True)
        result = proc.run_process(['/bin/sh', '-c', 'touch ' + shlex.quote(str(effect))],
                                  cwd=self.root, env=dict(os.environ),
                                  stdout_path=Path(self.side, 'barrier.log'), on_start=started)
        self.assertEqual((result.status, result.returncode), ('exited', 0))
        self.assertEqual(seen, [True])
        self.assertTrue(effect.exists())

    def test_published_identities_do_not_change_in_place(self):
        """proc: published identities remain stable during runtime updates (PROC-16)"""
        seen = []
        def started(identity):
            seen.append((identity, json.dumps(identity, sort_keys=True)))
        result = self.launch(on_start=started)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(seen), 2)
        for identity, saved in seen:
            self.assertEqual(json.dumps(identity, sort_keys=True), saved)

    def test_running_hook_keeps_its_original_release_boundary(self):
        """proc: the running hook precedes task execution (PROC-17)"""
        effect = Path(self.side, 'executed')
        def after_release():
            time.sleep(.25)
            self.assertFalse(effect.exists())
            raise RuntimeError('scripted interruption')
        with self.assertRaisesRegex(RuntimeError, 'scripted interruption'):
            proc.run_process(['/bin/sh', '-c', 'touch ' + shlex.quote(str(effect))],
                             cwd=self.root, env=dict(os.environ),
                             stdout_path=Path(self.side, 'hook.log'),
                             on_start=lambda _identity: after_release)
        self.assertFalse(effect.exists())

    def test_sinks_open_before_task_release(self):
        """proc: a log open failure never releases task code (PROC-09)"""
        effect = Path(self.side, 'executed')
        popen, children = subprocess.Popen, []
        def spawn(*args, **kwargs):
            child = popen(*args, **kwargs)
            children.append(child)
            return child
        sink = proc._Sink
        def opening(path, *args):
            if str(path).endswith('stderr.log'):
                raise OSError('unwritable stderr')
            return sink(path, *args)
        with mock.patch.object(proc, '_Sink', side_effect=opening), \
             mock.patch.object(proc.subprocess, 'Popen', side_effect=spawn):
            with self.assertRaisesRegex(OSError, 'unwritable stderr'):
                proc.run_process(['/bin/sh', '-c', 'touch ' + shlex.quote(str(effect))],
                                 cwd=self.root, env=dict(os.environ),
                                 stdout_path=Path(self.side, 'stdout.log'),
                                 stderr_path=Path(self.side, 'stderr.log'))
        self.assertFalse(effect.exists())
        self.assertIsNotNone(children[0].poll())

    def test_cleanup_failure_preserves_a_finished_result(self):
        """proc: cleanup errors accompany finished results (PROC-10)"""
        stop = proc.stop_group
        def stopping(*args, **kwargs):
            stop(*args, **kwargs)
            raise record.RecordError('could not stop invocation group')
        with mock.patch.object(proc, 'stop_group', side_effect=stopping):
            result = self.launch()
        self.assertEqual((result.status, result.returncode), ('exited', 0))
        self.assertEqual(result.stdout_tail, b'finished\n')
        self.assertIn('could not stop', result.error)
        self.assertIn('could not stop', Path(self.side, 'stderr.log').read_text())

    def test_lingering_poll_backs_off_after_broken_pipe(self):
        """proc: an orphan supervisor polls once per second (PROC-12)"""
        namespace = {}
        exec(proc._SUPERVISOR_SOURCE.rsplit('\n_supervise(', 1)[0], namespace)
        for broken in (False, True):
            sleeps = []
            def write(fd, _data):
                if broken and fd == 2:
                    raise BrokenPipeError
            fake_os = SimpleNamespace(read=lambda *_: b'G', close=lambda *_: None,
                                      getpid=lambda: 10, getpgrp=lambda: 10,
                                      pipe=lambda: (3, 4), fork=lambda: 20,
                                      waitpid=lambda *_: (20, 0), waitstatus_to_exitcode=lambda _: 0,
                                      write=write)
            releases = iter([b"G", b"A", b"G"])
            fake_os.read = lambda fd, _size: b"" if fd == 3 else next(releases)
            rows = iter(['leader start', '20 10 10 R', ''])
            fake_subprocess = SimpleNamespace(
                run=lambda *_args, **_kw: SimpleNamespace(returncode=0, stdout=next(rows)))
            namespace.update(os=fake_os, sys=SimpleNamespace(platform='test'),
                             subprocess=fake_subprocess, time=SimpleNamespace(sleep=sleeps.append),
                             signal=SimpleNamespace(SIGINT=2, SIGTERM=15, signal=lambda *_: None))
            namespace['_supervise'](1, 2, ['scripted'])
            self.assertEqual(sleeps, [1.0 if broken else .05])
