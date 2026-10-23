"""Recovery sequences: stop, the operator's remedy, the next command, the finish (RUN-82..RUN-86,
PROC-32). Each journey types the commands an operator would, in order, and checks that what the
runner printed between two steps names the next one. It asserts what the run leaves outside its
record: the bytes of the files in the tree and in the branch's last commit, the rulings file's
entries, the events, the processes. Each journey is mutation-checked: a named mutation restores an
earlier behaviour (its docstring names the commit), and a test asserts the journey fails under it.
No model call; the agents are the scripted ones."""
import contextlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tomllib
import unittest
from unittest import mock

from helpers import EngineCase, done
from codesmith import cli, engine, findings, gitops, invariants, record
import test_proc_boundaries as boundaries
import test_review_policy as policy

NOTE = 'Owner accepts the narrower scope'


# -- mutations: each restores an earlier behaviour, named by the commit it restores -------------

@contextlib.contextmanager
def producer_only_export_gate():
    """Restores 50d41ca (package T): `runner export-rulings` refused only a producer's
    transaction, an operation to reconcile and a producer left to start. A standalone check
    paused `running` was no bar, so the export wrote the file and `resume`'s `settle()` then
    restored the check's base over it (T ripple V-01)."""
    from codesmith import status

    def gate(state):
        if state.get('active_producer'):
            return [f"'{state['active_producer']}' in its transaction"]
        if state.get('intents'):
            return ['operations to reconcile']
        return [f"'{t}' {state['tasks'][t]['status']}" for t in state['order']
                if state['tasks'][t]['kind'] == 'produce'
                and state['tasks'][t]['status'] not in ('accepted', 'failed', 'skipped')]
    with mock.patch.object(cli, 'export_live', gate), mock.patch.object(status, 'export_live', gate):
        yield


@contextlib.contextmanager
def retry_offers_removing_the_export():
    """Restores 50d41ca (package T): a retry refused for an uncommitted file named the remedy
    "commit them (or remove them)", also for a rulings file `runner export-rulings` wrote and
    marked written, so the offered removal drops the ruling (T ripple V-05)."""
    real = engine._adopt_tip

    def adopt(run, git, task_id, rulings_file=None):
        try:
            return real(run, git, task_id, rulings_file)
        except engine.Refused as exc:
            text = str(exc)
            head, _sep, _tail = text.partition('. Nothing was changed; ')
            if head == text:
                raise
            raise engine.Refused(head + '. Nothing was changed; commit them (or remove them), '
                                 'then run this retry again.') from None
    with mock.patch.object(engine, '_adopt_tip', adopt):
        yield


@contextlib.contextmanager
def abandon_contradicts_stop_orphans():
    """Restores be6429a (package Q): `resume --abandon-cleanup --stop-orphans` was refused as a
    contradiction, so the one resume the orphan's refusal names cannot be run (Q ripple V-01,
    fixed in 3fb4731)."""
    real = cli.cmd_resume

    def resume(args):
        if args.abandon_cleanup and args.stop_orphans:
            return cli.fail("--abandon-cleanup and --stop-orphans contradict each other: one "
                            "leaves the group without a signal, the other stops it. Give one; "
                            "nothing was recorded")
        return real(args)
    with mock.patch.object(cli, 'cmd_resume', resume):
        yield


@contextlib.contextmanager
def abandon_recorded_before_the_orphan_check():
    """Restores be6429a (package Q): `resume --abandon-cleanup` recorded the ruling (the
    obligation abandoned, a `cleanup-abandoned` event) before it met the live orphan and refused,
    so a refusal changed the record (fixed in 3fb4731, PROC-28)."""
    real = record._reconcile_orphan

    def orphan(it, stop_orphans, grace_s):
        caller = sys._getframe(1)
        run = caller.f_locals.get('run')
        if (caller.f_code.co_name == 'reconcile' and run is not None and any(
                e['cleanup']['status'] == 'open' for e in run.state.get('cleanup_obligations', []))):
            return None                                   # the check before the ruling: gone
        return real(it, stop_orphans, grace_s)
    with mock.patch.object(record, '_reconcile_orphan', orphan):
        yield


@contextlib.contextmanager
def export_leaves_no_tree_when_none_was_remembered():
    """Restores ec51a3a (package U) and every runner before it: `runner export-rulings` on a run
    whose stop remembered no tree leaves none, so `retry` adopts nothing and refuses nothing
    (U ripple V-01)."""
    real = engine.Engine.remember_tree

    def remember(self):
        if self.run.state.get('expect') is None and _in_export():
            return None
        return real(self)
    with mock.patch.object(engine.Engine, 'remember_tree', remember):
        yield


@contextlib.contextmanager
def retry_adopts_nothing_without_a_remembered_tree():
    """Restores 7f58c69 (package V) and every runner before it: with no remembered tree `retry`
    checks nothing, so a retry before the commit of a rulings file `export-rulings` wrote is
    taken when the export could not remember the tree either (V ripple V-01)."""
    real = engine._adopt_tip

    def adopt(run, git, task_id, rulings_file=None):
        if not run.state.get('expect'):
            return None
        return real(run, git, task_id, rulings_file)
    with mock.patch.object(engine, '_adopt_tip', adopt):
        yield


@contextlib.contextmanager
def blind_to_missing_exports(*sites):
    """Restores f6bdf21 (package W) and every runner before it at `sites`, the functions that ask
    `findings.missing_exports`: there, a written ruling missing from its file is not seen. At
    `_adopt_tip` the retry checks dirty paths only; at `prepare` the replan exempts the exported
    files by path; at `export_standing` the finish checks nothing; at `cmd_export_rulings` the
    export returns "nothing to do" for written entries (W ripple W-02)."""
    from codesmith import findings
    real = findings.missing_exports

    def missing(*args, **kwargs):
        if sys._getframe(1).f_code.co_name in sites:
            return []
        return real(*args, **kwargs)
    with mock.patch.object(findings, 'missing_exports', missing):
        yield


@contextlib.contextmanager
def replan_checks_a_done_run():
    """Restores ed6e908 (package X): `replan.prepare` asked `findings.missing_exports` of a done
    run too, so a replan after the owner's withdrawal by commit was refused naming
    `runner export-rulings RUN`, which says "nothing to do" on a done run (X ripple V-01). The
    earlier checks of `prepare` (branch, clean tree) pass in RUN-89's state, so running this one
    first is the same refusal."""
    from codesmith import findings, replan
    real = replan.prepare

    def prepare(run, git, source, reopen=()):
        if run.state['status'] == 'done':
            before = record.read_json(os.path.join(run.path, 'workflow.expanded.json'))
            lost = findings.missing_exports(run.state, git.top, before['defaults'].get('rulings_file'))
            if lost:
                raise replan.Refused(findings.missing_export_text(run.name, lost)
                                     + '. Nothing was changed')
        return real(run, git, source, reopen)
    with mock.patch.object(replan, 'prepare', prepare):
        yield


@contextlib.contextmanager
def export_lets_the_snapshot_stop_escape():
    """Restores f6bdf21 (package W) and every runner before it: `runner export-rulings` called
    `eng.snapshot()` with no handler, so an embedded repository's `EngineStop` escaped as a
    traceback (W ripple W-05). Inside the command only, the handler's class matches nothing."""
    class Unraised(Exception):
        pass

    class WithoutTheHandler:
        EngineStop = Unraised

        def __getattr__(self, name):
            return getattr(engine, name)
    real = cli.cmd_export_rulings

    def export(args):
        with mock.patch.object(cli, 'engine', WithoutTheHandler()):
            return real(args)
    with mock.patch.object(cli, 'cmd_export_rulings', export):
        yield


@contextlib.contextmanager
def embedded_remedy_deletes():
    """Restores 2d6d1ce (package Y1b): the embedded-repository stop and `_restore_remedy` print
    `rm -rf P && mkdir P`, which, run literally, destroys the owner's clone (Y1b ripple V-02)."""
    def snapshot(self):
        for rel in self.git.embedded_repositories():
            keep = (f" and leave the empty directory (`rm -rf {rel} && mkdir {rel}`)"
                    if self.git.gitlink_at("HEAD", rel) else "")
            raise engine.EngineStop(f"an embedded git repository is in the work tree at '{rel}'; a "
                                    f"snapshot cannot be taken. Remove it{keep}, then `runner resume`")
        return self.git.snapshot(self.run.index_file)
    real = gitops.Git._restore_remedy

    def remedy(self, rel, target):
        text = real(self, rel, target)
        return text.replace(f"`mv {rel} {gitops.OUTSIDE} && mkdir {rel}`",
                            f"`rm -rf {rel} && mkdir {rel}`")
    with mock.patch.object(engine.Engine, 'snapshot', snapshot), \
            mock.patch.object(gitops.Git, '_restore_remedy', remedy):
        yield


@contextlib.contextmanager
def restore_admits_any_change():
    """Not an earlier behaviour: the remembered-tree check of `export-rulings` admitting every
    change once a restore is due, as a path-name or "anything goes" predicate would. Shows that
    RUN-87's refusal of an unrelated edit has teeth."""
    with mock.patch.object(cli, '_only_restored', lambda *args: True):
        yield


def _in_export():
    frame = sys._getframe(2)
    while frame is not None:
        if frame.f_code.co_name == 'cmd_export_rulings':
            return True
        frame = frame.f_back
    return False


# -- the journeys --------------------------------------------------------------------------------

class Journey(EngineCase):
    HEADER = EngineCase.HEADER + 'read_only_args = ["--read-only"]\n'
    events = policy.Provenance.events
    ruling_env = policy.Provenance.ruling_env
    held = policy.Provenance.held
    standing = policy.LearningDelivery.standing

    def page(self):
        return (Path(self.the_run().path) / 'STATUS.md').read_text()

    def committed(self, rel):
        """The file's bytes in the branch's last commit; None when it is not there."""
        r = subprocess.run(['git', 'show', f'HEAD:{rel}'], cwd=self.root, capture_output=True)
        return r.stdout.decode() if r.returncode == 0 else None

    def assert_ruling_kept(self, rel):
        """The finish every export journey must reach: the run `done`, the branch's last commit
        carrying the rulings file with the one ruling (not dropped, not twice), the tree clean,
        one `standing-rulings-written` event."""
        self.assertIn('Status: done', self.page())
        text = self.committed(rel)
        self.assertIsNotNone(text, f'{rel} is not in the last commit: the ruling was dropped')
        rulings = tomllib.loads(text)['ruling']
        self.assertEqual([(r['task'], r['finding_title'], r['decision'], r['note'], r['by'])
                          for r in rulings], [('make', 'Fix this', 'advisory', NOTE, 'owner')])
        self.assertEqual(Path(self.root, rel).read_text(), text)
        self.assertEqual(self.git_out('status', '--porcelain'), '')
        self.assertEqual(len(self.events('standing-rulings-written')), 1)

    def commit_file(self, rel):
        self.git_out('add', '--', rel)
        self.git_out('commit', '-q', '-m', f'{rel} as runner export-rulings wrote it')

    def flaky_check(self):
        """A standalone check that fails until the operator fixes its cause (a file outside the
        repository): the check's own failure is the stop, the fix is no change in the tree."""
        self.fixed = os.path.join(self.side, 'lint-fixed')
        command = 'test -f ' + shlex.quote(self.fixed)
        return '\n[[task]]\nid="lint"\ntype="check"\nneeds=["make"]\nrun=[' + json.dumps(command) + ']\n'

    # RUN-82 ---------------------------------------------------------------------------------------

    def journey_export_and_a_paused_check(self):
        """A standing ruling queued; the run paused inside a standalone check. The export is
        refused, naming `resume`; the check fails; the export, its commit, the retry the export
        named; a second pause inside the check; resume; done with the ruling in the commit."""
        rulings = os.path.join(self.root, 'rulings.toml')
        self.held(policy.ONE + self.flaky_check(), defaults='rulings_file="rulings.toml"')
        self.ruling_env(USER='owner')
        self.assertEqual(self.standing(NOTE), 0, self.output)
        self.the_run().request_pause()
        self.assertEqual(self.resume(), 2, self.output)
        name = self.the_run().name
        self.assertIn('| 020 | lint | check |  | running |', self.page())
        # The operator runs the export the queued ruling waits for: refused, writing nothing.
        code = self.runner('export-rulings', name, '-C', self.root)
        self.assertFalse(os.path.exists(rulings), 'the export wrote beside a running check')
        self.assertEqual(code, 2, self.output)
        self.assertIn("'lint' running", self.output)
        self.assertIn(f'Finish it: `runner resume {name}`', self.output)
        self.assertIn(f'finish it: `runner resume {name}`', self.page())
        # Next: resume. The check runs and fails; the run ends failed.
        self.assertEqual(self.resume(), 2, self.output)
        self.assertIn('Status: failed', self.page())
        self.assertIn(f'`runner export-rulings {name}` writes it to rulings.toml now', self.page())
        self.assertEqual(self.runner('export-rulings', name, '-C', self.root), 0, self.output)
        written = Path(rulings).read_text()
        self.assertIn(f'commit it, so the next run reads it; then `runner retry {name} lint` '
                      'starts again from your commit', self.output)
        self.commit_file('rulings.toml')
        Path(self.fixed).touch()                          # the operator fixes the check's cause
        self.assertEqual(self.runner('retry', name, 'lint', '-C', self.root), 0, self.output)
        self.assertIn(f'Continue with: runner resume {name}', self.output)
        # The check is paused again, the export in the tree: resume's settle keeps it.
        self.the_run().request_pause()
        self.assertEqual(self.resume(), 2, self.output)
        self.assertIn('| 020 | lint | check |  | running |', self.page())
        self.assertEqual(Path(rulings).read_text(), written)
        self.assertIn(f'## Next\n    runner resume {name}\n', self.page())
        self.assertEqual(self.resume(), 0, self.output)
        self.assertEqual(Path(rulings).read_text(), written)
        self.assert_ruling_kept('rulings.toml')
        self.check_invariants()

    def test_export_and_a_paused_check(self):
        """run: a standing ruling with a standalone check paused running: the export is refused
        naming resume; after the check fails the export, its commit, the named retry and a second
        pause inside the check end done with the ruling in the last commit once (RUN-82)"""
        self.journey_export_and_a_paused_check()

    def test_export_and_a_paused_check_catches_the_producer_only_gate(self):
        """run: RUN-82's journey fails when the export gate counts only producers (50d41ca)"""
        with producer_only_export_gate(), self.assertRaises(AssertionError) as ctx:
            self.journey_export_and_a_paused_check()
        self.assertIn('the export wrote beside a running check', str(ctx.exception))
        # The harm the oracle stands for: the resume the export then names restores the check's
        # base over the file, and the ruling, marked written, is in neither tree nor commit.
        self.assertIn(f'`runner resume {self.the_run().name}` ends the run as it stands',
                      self.output)
        # The strict-only rule judges that resume at its boundary (C15, INV-03).
        with self.assertRaisesRegex(invariants.Violation, "^written-ruling-kept: make/PE-1 is "
                                    "marked written to rulings.toml, which holds it neither in "
                                    "the work tree nor in the branch's last commit$"):
            self.resume()
        self.assertFalse(os.path.exists(os.path.join(self.root, 'rulings.toml')))
        self.assertIsNone(self.committed('rulings.toml'))

    # RUN-83 ---------------------------------------------------------------------------------------

    def journey_export_failures_and_their_remedies(self):
        """A run failed with a queued ruling. The export is refused because the tree moved, then
        fails because the rulings directory cannot be written, then succeeds; the operator slips
        and retries before committing. Each refusal's remedy is followed as printed; where a
        message offers removing the file, the operator removes it (every remedy offered must be
        safe). The run ends done with the ruling in the last commit once."""
        rel = 'rules/rulings.toml'
        self.write('rules/README.md', 'the owner keeps rulings here\n')
        tasks = policy.ONE + policy.DOWN.replace('gate=', 'max_attempts=1\ngate=')
        self.workflow(tasks, defaults=f'rulings_file="{rel}"')
        rid = 'make.review.principled-priya'
        dispute = dict(finding='make/PE-1', action='disputed', note='I ask for a scope exception')
        self.script({'make': [{'write': {'src/a': 'one\n'}, 'answer': done()},
                              {'answer': done('Asked for a ruling', responses=[dispute])}],
                     rid: [{'answer': policy.review([policy.finding()])},
                           {'answer': policy.review(resolutions=[policy.resolution(status='unresolved')])}],
                     'down': [{'answer': done()},                       # no src/down: its gate fails
                              {'write': {'src/down': 'down\n'}, 'answer': done()}],
                     'down.review.principled-priya': [policy.PASS]})
        self.assertEqual(self.start(), 255, self.output)
        self.ruling_env(USER='owner')
        self.assertEqual(self.standing(NOTE), 0, self.output)
        self.assertEqual(self.resume(), 2, self.output)
        name = self.the_run().name
        self.assertIn('Status: failed', self.page())
        self.assertIn(f'`runner export-rulings {name}` writes it to {rel} now', self.page())
        path = os.path.join(self.root, rel)
        # 1. The owner edited a file meanwhile: refused, naming the remedy.
        self.write('README.md', 'edited while the run was stopped\n')
        self.assertEqual(self.runner('export-rulings', name, '-C', self.root), 2, self.output)
        self.assertIn('the branch or the work tree changed since the run stopped; put it back as '
                      'it was', self.output)
        self.assertFalse(os.path.exists(path))
        self.git_out('checkout', '--', 'README.md')            # put back as it was
        # 2. The rulings directory cannot be written: the failure names its remedy.
        os.chmod(os.path.join(self.root, 'rules'), 0o555)
        self.addCleanup(os.chmod, os.path.join(self.root, 'rules'), 0o755)
        self.assertEqual(self.runner('export-rulings', name, '-C', self.root), 2, self.output)
        self.assertIn(f'Fix it, then `runner export-rulings {name}`', self.output)
        self.assertFalse(os.path.exists(path))
        self.assertEqual(self.events('standing-rulings-written'), [])
        self.assertIn(f'`runner export-rulings {name}` writes it to {rel} now', self.page())
        os.chmod(os.path.join(self.root, 'rules'), 0o755)        # fixed
        self.assertEqual(self.runner('export-rulings', name, '-C', self.root), 0, self.output)
        self.assertIn(f'commit it, so the next run reads it; then `runner retry {name} down` '
                      'starts again from your commit', self.output)
        # 3. The operator retries before committing: the refusal's remedy, followed.
        self.assertEqual(self.runner('retry', name, 'down', '-C', self.root), 2, self.output)
        remedy = self.output.split('Nothing was changed; ', 1)[1]
        if 'remove' in remedy:
            os.unlink(path)
        else:
            self.assertIn(f'commit {rel} as `runner export-rulings` wrote it, then run this retry '
                          'again', remedy)
            self.commit_file(rel)
        self.assertEqual(self.runner('retry', name, 'down', '-C', self.root), 0, self.output)
        self.assertIn(f'Continue with: runner resume {name}', self.output)
        self.assertEqual(self.resume(), 0, self.output)
        self.assert_ruling_kept(rel)
        self.check_invariants()

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'root writes a 0555 directory')
    def test_export_failures_and_their_remedies(self):
        """run: export-rulings refused for a moved tree, then failing on a rulings directory it
        cannot write, then a retry before the commit: each printed remedy, followed, ends done
        with the ruling in the last commit once, never dropped or written twice (RUN-83)"""
        self.journey_export_failures_and_their_remedies()

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'root writes a 0555 directory')
    def test_export_failures_catch_a_retry_offering_removal(self):
        """run: RUN-83's journey fails when a retry's remedy offers removing the export (50d41ca)
        and the retry and finish do not see a removed export (f6bdf21): the ruling is dropped"""
        with retry_offers_removing_the_export(), \
                blind_to_missing_exports('_adopt_tip', 'export_standing'), \
                self.assertRaises(AssertionError) as ctx:
            self.journey_export_failures_and_their_remedies()
        self.assertIn('the ruling was dropped', str(ctx.exception))

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'root writes a 0555 directory')
    def test_export_failures_catch_a_retry_offering_removal_since_run87(self):
        """run: RUN-83's journey with only the remedy offering removal (50d41ca) fails at the
        retry after the removal, which RUN-87's refusal stops naming the restore"""
        with retry_offers_removing_the_export(), self.assertRaises(AssertionError) as ctx:
            self.journey_export_failures_and_their_remedies()
        self.assertIn("'down' cannot be retried as the run stands: the exported ruling make/PE-1 "
                      "is missing from rules/rulings.toml; restore it", str(ctx.exception))

    # RUN-84 ---------------------------------------------------------------------------------------

    def journey_export_without_a_remembered_tree(self):
        """U ripple V-01 as an oracle. A run ends failed by a stop that remembered no tree (here:
        the snapshot at the stop failed, as an embedded repository makes it). The export writes;
        the operator retries before committing. The correct outcome is the one RUN-77 gives a
        run with a remembered tree: the retry refuses, naming the commit of the file as written,
        and changes nothing; the commit, the retry and resume then end done with the ruling."""
        rel = 'rulings.toml'
        tasks = policy.ONE + policy.DOWN.replace('gate=', 'max_attempts=1\ngate=')
        self.workflow(tasks, defaults=f'rulings_file="{rel}"')
        rid = 'make.review.principled-priya'
        dispute = dict(finding='make/PE-1', action='disputed', note='I ask for a scope exception')
        self.script({'make': [{'write': {'src/a': 'one\n'}, 'answer': done()},
                              {'answer': done('Asked for a ruling', responses=[dispute])}],
                     rid: [{'answer': policy.review([policy.finding()])},
                           {'answer': policy.review(resolutions=[policy.resolution(status='unresolved')])}],
                     'down': [{'answer': done()},
                              {'write': {'src/down': 'down\n'}, 'answer': done()}],
                     'down.review.principled-priya': [policy.PASS]})
        self.assertEqual(self.start(), 255, self.output)
        self.ruling_env(USER='owner')
        self.assertEqual(self.standing(NOTE), 0, self.output)

        def no_snapshot(eng):
            eng.run.state['expect'] = None                # remember_tree when the snapshot fails
        with mock.patch.object(engine.Engine, 'remember_tree', no_snapshot):
            self.assertEqual(self.resume(), 2, self.output)
        name = self.the_run().name
        self.assertIn('Status: failed', self.page())
        self.assertIn(f'`runner export-rulings {name}` writes it to {rel} now', self.page())
        self.assertEqual(self.runner('export-rulings', name, '-C', self.root), 0, self.output)
        self.assertIn(f'then `runner retry {name} down` starts again from your commit', self.output)
        written = Path(self.root, rel).read_text()
        events = Path(self.the_run().path, 'events.jsonl').read_bytes()
        self.assertEqual(self.runner('retry', name, 'down', '-C', self.root), 2,
                         'a retry before the commit was taken: ' + self.output)
        self.assertIn(f'commit {rel} as `runner export-rulings` wrote it, then run this retry '
                      'again', self.output)
        self.assertEqual(Path(self.the_run().path, 'events.jsonl').read_bytes(), events)
        self.assertEqual(Path(self.root, rel).read_text(), written)
        self.commit_file(rel)
        self.assertEqual(self.runner('retry', name, 'down', '-C', self.root), 0, self.output)
        self.assertEqual(self.resume(), 0, self.output)
        self.assert_ruling_kept(rel)
        self.check_invariants()

    def test_export_without_a_remembered_tree(self):
        """run: export-rulings after a stop that remembered no tree guards the retry as RUN-77's
        export does: a retry before the commit is refused naming the commit, and the commit,
        the retry and resume end done with the ruling once (RUN-84)"""
        self.journey_export_without_a_remembered_tree()

    def test_export_without_a_remembered_tree_catches_the_unguarded_export(self):
        """run: RUN-84's journey fails when the export leaves no tree where none was remembered
        (ec51a3a and every runner before it, whose retry also checked nothing without a
        remembered tree: since RUN-85 either one alone is caught)"""
        with export_leaves_no_tree_when_none_was_remembered(), \
                retry_adopts_nothing_without_a_remembered_tree(), \
                self.assertRaises(AssertionError) as ctx:
            self.journey_export_without_a_remembered_tree()
        self.assertIn('a retry before the commit was taken', str(ctx.exception))

    # RUN-85 ---------------------------------------------------------------------------------------

    def next_lines(self):
        """STATUS's Next section, as printed."""
        return self.page().split('## Next\n', 1)[1].split('\n## ', 1)[0].strip()

    def submodule(self):
        """An **unregistered** embedded repository: a clone committed as a gitlink with no
        `.gitmodules` entry (git's "embedded repository" warning), not checked out (an empty
        directory, no change to the tree). Checked out (`check_out_sub`) it is an embedded
        repository to the runner's snapshot, and yet no change to the tree: its commit is the
        committed one. Until Y1 a registered submodule did the same; since C14 a registered one
        is a normal tree entry, so RUN-85's reachability rests on this fixture (GIT-31)."""
        self.lib = os.path.join(self.side, 'lib')
        os.makedirs(self.lib)
        for args in (('init', '-q', '-b', 'main'), ('add', '-A'), ('commit', '-qm', 'lib')):
            Path(self.lib, 'f').write_text('lib\n')
            subprocess.run(['git', '-c', 'user.name=x', '-c', 'user.email=x@x', *args],
                           cwd=self.lib, check=True, capture_output=True)
        self.git_out('clone', '-q', self.lib, 'sub')
        self.git_out('add', 'sub')
        self.git_out('commit', '-qm', 'sub, committed as a gitlink with no .gitmodules entry')
        self.remove_sub()

    def check_out_sub(self):
        self.git_out('clone', '-q', self.lib, 'sub')

    def remove_sub(self):
        """The fixture's own setup: the clone that made the gitlink goes, its empty directory
        stays (no change to the tree)."""
        shutil.rmtree(os.path.join(self.root, 'sub'))
        os.makedirs(os.path.join(self.root, 'sub'))

    def move_sub(self, out):
        """The embedded-repository stop's printed command, run literally with its placeholder
        filled by a fresh directory beside the repository: "Move it out of the work tree and leave
        the empty directory (`mv sub <a directory outside the work tree> && mkdir sub`)". The
        clone is moved, never deleted: the commit it was at is still reachable where it went
        (Y1b ripple V-02)."""
        self.assertNotIn('rm -rf', out, 'a printed remedy contains rm -rf: ' + out)
        found = re.search(r"\(`(mv sub [^`]+ && mkdir sub)`\)", out)
        self.assertIsNotNone(found, 'the embedded stop names no command: ' + out)
        at = self.git_out('-C', 'sub', 'rev-parse', 'HEAD')
        self.moved_to = os.path.join(self.side, f'moved-sub-{len(os.listdir(self.side))}')
        command = found.group(1).replace(gitops.OUTSIDE, shlex.quote(self.moved_to))
        subprocess.run(command, shell=True, cwd=self.root, check=True, capture_output=True)
        self.assertEqual(subprocess.run(['git', '-C', self.moved_to, 'rev-parse', 'HEAD'],
                                        check=True, capture_output=True, text=True).stdout.strip(),
                         at, "the clone's work did not survive its printed remedy")
        self.assertEqual(os.listdir(os.path.join(self.root, 'sub')), [])

    def operator(self, name, rel, limit=8):
        """Follows what the runner prints, literally, from a retry of 'down', until `done` or
        `limit` commands. Each step records the command, its exit code, the remedy it printed
        and STATUS's Next; `self.transcript` keeps them for the caller's assertions."""
        self.transcript, self.outputs = [], []
        command = ('retry', name, 'down')
        for _ in range(limit):
            events = Path(self.the_run().path, 'events.jsonl').read_bytes()
            written = Path(self.root, rel).read_bytes() if os.path.exists(Path(self.root, rel)) else None
            code = self.runner(*command, '-C', self.root)
            out = self.output
            said = next((s for s in ("commit rulings.toml as `runner export-rulings` wrote it",
                                     'Continue with: runner resume',
                                     "embedded git repository is in the work tree at 'sub'",
                                     'not at a clean accepted state',
                                     'the branch tip changed', ': done.') if s in out), out)
            self.transcript.append((command[0], code, said, self.next_lines()))
            self.outputs.append(out)
            self.assertNotIn('rm -rf', out, 'a printed remedy contains rm -rf: ' + out)
            if code != 0:                                 # a refusal or a stop changes no file
                self.assertEqual(Path(self.root, rel).read_bytes() if written is not None else None,
                                 written)
            if said == ': done.':
                return
            if said.startswith('commit rulings.toml'):    # "commit …, then run this retry again"
                self.assertEqual(Path(self.the_run().path, 'events.jsonl').read_bytes(), events,
                                 'a refused retry changed the events')
                self.commit_file(rel)
                command = ('retry', name, 'down')
            elif said.startswith('embedded'):             # "Move it out … (`mv …`), then resume"
                self.move_sub(out)
                command = ('resume',)
            elif said == 'not at a clean accepted state':
                self.commit_file(rel)                     # the export said: commit it
                command = ('resume',)
            elif said == 'the branch tip changed':        # "put it back as it was"
                self.git_out('reset', '-q', '--soft', 'HEAD~1')
                self.git_out('reset', '-q')
                command = ('resume',)
            elif said.startswith('Continue with'):
                command = ('resume',)
            else:
                self.fail(f'no remedy the operator can follow: {out}')
        self.fail('livelock: the printed remedies cycle without naming the way out: '
                  + ' / '.join(f'{c} -> {s}' for c, _code, s, _n in self.transcript))

    def journey_export_while_the_snapshot_fails(self):
        """V ripple V-01, reached by the command line alone. The repository has an unregistered
        embedded repository, committed as a gitlink and not checked out (`submodule`; since C14
        a registered submodule no longer reaches this state, GIT-31); the run ends `failed`
        ('down' failed); the owner checks it out (no change to the tree, so `resume` takes it)
        and resumes: the stop's snapshot now fails, and the run
        remembers no tree. `export-rulings` writes, and its snapshot fails too. The operator
        retries before committing and then does what each printed line says. The run must end
        `done` with the ruling committed once."""
        rel = 'rulings.toml'
        self.submodule()
        tasks = policy.ONE + policy.DOWN.replace('gate=', 'max_attempts=1\ngate=')
        self.workflow(tasks, defaults=f'rulings_file="{rel}"')
        rid = 'make.review.principled-priya'
        dispute = dict(finding='make/PE-1', action='disputed', note='I ask for a scope exception')
        self.script({'make': [{'write': {'src/a': 'one\n'}, 'answer': done()},
                              {'answer': done('Asked for a ruling', responses=[dispute])}],
                     rid: [{'answer': policy.review([policy.finding()])},
                           {'answer': policy.review(resolutions=[policy.resolution(status='unresolved')])}],
                     'down': [{'answer': done()},
                              {'write': {'src/down': 'down\n'}, 'answer': done()}],
                     'down.review.principled-priya': [policy.PASS]})
        self.assertEqual(self.start(), 255, self.output)
        self.ruling_env(USER='owner')
        self.assertEqual(self.standing(NOTE), 0, self.output)
        self.assertEqual(self.resume(), 2, self.output)
        name = self.the_run().name
        self.assertIsNotNone(self.the_run().state['expect'])
        retry_or_resume = f'runner retry {name} down [--apply-patch]\n    runner resume {name}'
        self.assertEqual(self.next_lines(), retry_or_resume)
        # The owner checks the unregistered repository out: the tree is unchanged, so resume
        # takes it, and the snapshot at the stop fails. No monkeypatch: the state is the command
        # line's. (A registered submodule no longer does this since C14: GIT-31.)
        self.check_out_sub()
        self.assertEqual(self.git_out('status', '--porcelain'), '')
        self.assertEqual(self.resume(), 2, self.output)
        state = self.the_run().state
        self.assertEqual((state['status'], state['expect']), ('failed', None))
        self.assertEqual(self.next_lines(), retry_or_resume)
        self.assertIn(f'`runner export-rulings {name}` writes it to {rel} now', self.page())
        self.assertEqual(self.runner('export-rulings', name, '-C', self.root), 0, self.output)
        self.assertIn(f'commit it, so the next run reads it; then `runner retry {name} down` '
                      'starts again from your commit', self.output)
        self.assertIsNone(self.the_run().state['expect'], 'the export remembered a tree')
        self.assertEqual(self.next_lines(), retry_or_resume)
        self.operator(name, rel)
        self.assert_ruling_kept(rel)
        self.check_invariants()

    def test_export_while_the_snapshot_fails(self):
        """run: a checked-out unregistered embedded repository fails the snapshots of the stop and
        of export-rulings, so no tree is remembered; a retry before the commit is still refused
        naming the commit, and each printed step then ends done with the ruling committed once
        (RUN-85; a registered submodule no longer reaches this state since C14)"""
        self.journey_export_while_the_snapshot_fails()
        name = self.the_run().name
        self.assertEqual(self.transcript, [
            ('retry', 2, "commit rulings.toml as `runner export-rulings` wrote it",
             f'runner retry {name} down [--apply-patch]\n    runner resume {name}'),
            ('retry', 0, 'Continue with: runner resume', f'runner resume {name}'),
            ('resume', 2, "embedded git repository is in the work tree at 'sub'",
             f'runner resume {name}'),
            ('resume', 0, ': done.', 'Inspect the branch; merging it is your call.')])

    def test_export_while_the_snapshot_fails_catches_the_unguarded_retry(self):
        """run: RUN-85's journey fails as a livelock when a retry with no remembered tree checks
        nothing (7f58c69): the remedies alternate and none names the way out"""
        with retry_adopts_nothing_without_a_remembered_tree(), \
                self.assertRaises(AssertionError) as ctx:
            self.journey_export_while_the_snapshot_fails()
        self.assertIn('livelock', str(ctx.exception))
        said = [s for _c, _code, s, _n in self.transcript]
        self.assertEqual(said[:3], ['Continue with: runner resume',
                                    "embedded git repository is in the work tree at 'sub'",
                                    'not at a clean accepted state'])
        self.assertEqual(said[3:], ['the branch tip changed', 'not at a clean accepted state'] * 2
                         + ['the branch tip changed'])
        # The way out (`retry` again, or `replan`) is printed by no step and named by no STATUS.
        for out, (_c, _code, _s, nxt) in list(zip(self.outputs, self.transcript))[1:]:
            self.assertNotIn('retry', out + nxt)
            self.assertNotIn('replan', out + nxt)
        # Nothing is lost: the ruling is in the tree as written and in no commit.
        self.assertEqual(len(self.events('standing-rulings-written')), 1)
        self.assertIn('Owner accepts the narrower scope', Path(self.root, 'rulings.toml').read_text())

    # RUN-86 ---------------------------------------------------------------------------------------

    def failed_writing_at_done(self, rel='rules/rulings.toml'):
        """RUN-73's state from the command line: every task accepted, the rulings directory not
        writable when the run gets to done, so the run ends `failed` with no remembered tree."""
        self.write('rules/README.md', 'the owner keeps rulings here\n')
        self.held(policy.ONE, defaults=f'rulings_file="{rel}"')
        self.ruling_env(USER='owner')
        self.assertEqual(self.standing(NOTE), 0, self.output)
        rules = os.path.join(self.root, 'rules')
        os.chmod(rules, 0o555)
        self.addCleanup(os.chmod, rules, 0o755)
        self.assertEqual(self.resume(), 2, self.output)
        self.assertIn(f'could not be written to {rel}', self.output)
        state = self.the_run().state
        self.assertEqual((state['status'], state['expect']), ('failed', None))
        name = self.the_run().name
        self.assertEqual(self.next_lines(), f'runner resume {name}')
        os.chmod(rules, 0o755)                            # the owner fixes the cause
        return name

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'root writes a 0555 directory')
    def test_export_after_a_failed_write_at_done(self):
        """run: export-rulings on RUN-73's state (a failed write at done) writes once and remembers
        the tree: an edit after it is refused naming "put it back as it was", and resume with the
        file uncommitted ends done with one event; the owner's commit carries it (RUN-86)"""
        rel = 'rules/rulings.toml'
        name = self.failed_writing_at_done(rel)
        self.assertEqual(self.runner('export-rulings', name, '-C', self.root), 0, self.output)
        self.assertIn(f'1 standing ruling(s) written to {rel}; commit it, so the next run reads '
                      f'it; then `runner resume {name}` finishes the run\n', self.output)
        written = Path(self.root, rel).read_bytes()
        self.assertEqual(self.next_lines(), f'runner resume {name}')
        self.write('README.md', 'edited after the export\n')
        events = Path(self.the_run().path, 'events.jsonl').read_bytes()
        self.assertEqual(self.resume(), 2, 'an edit after the export was taken: ' + self.output)
        self.assertIn('while the run was paused the work tree changed: README.md', self.output)
        self.assertIn('put it back as it was', self.output)
        self.assertEqual(Path(self.the_run().path, 'events.jsonl').read_bytes(), events)
        self.assertEqual(self.next_lines(), f'runner resume {name}')
        self.git_out('checkout', '--', 'README.md')       # put back as it was
        self.assertEqual(self.resume(), 0, self.output)
        self.assertIn('Status: done', self.page())
        self.assertEqual(self.next_lines(), 'Inspect the branch; merging it is your call.')
        self.assertEqual(Path(self.root, rel).read_bytes(), written)
        self.assertEqual(len(self.events('standing-rulings-written')), 1)
        self.commit_file(rel)
        self.assert_ruling_kept(rel)
        self.check_invariants()

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'root writes a 0555 directory')
    def test_export_after_a_failed_write_at_done_catches_the_unremembered_tree(self):
        """run: RUN-86's journey fails when the export on RUN-73's state leaves no tree (ec51a3a):
        the edit made after the export is taken by resume"""
        with export_leaves_no_tree_when_none_was_remembered(), \
                self.assertRaises(AssertionError) as ctx:
            self.test_export_after_a_failed_write_at_done()
        self.assertIn('an edit after the export was taken', str(ctx.exception))
        self.assertIn('Status: done', self.page())

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'root writes a 0555 directory')
    def test_replan_after_an_export_on_a_failed_write_at_done(self):
        """run: on RUN-73's state an owner's commit made before export-rulings is the base the
        export remembers: the committed export, replan and resume take it, as resume took it
        before the export, and the run ends done with the ruling once (RUN-86, V ripple V-02)"""
        rel = 'rules/rulings.toml'
        name = self.failed_writing_at_done(rel)
        self.write('notes.txt', 'the owner notes why the write failed\n')
        self.git_out('add', 'notes.txt')
        self.git_out('commit', '-q', '-m', 'owner notes')
        owner = self.git_out('rev-parse', 'HEAD')
        self.assertEqual(self.runner('export-rulings', name, '-C', self.root), 0, self.output)
        self.commit_file(rel)
        self.assertEqual(self.runner('replan', name, '--workflow', self.wf_path, '-C', self.root),
                         0, self.output)
        self.assertIn('revert commits: none', self.output)
        self.assertEqual(self.resume(), 0, self.output)
        self.assert_ruling_kept(rel)
        # The owner's commit stays on the branch as the owner's: no Run: trailer, no task's.
        self.assertEqual(self.git_out('merge-base', '--is-ancestor', owner, 'HEAD'), '')
        self.assertNotIn('Run:', self.git_out('log', '-1', '--format=%B', owner))
        self.assertNotIn(owner, [t.get('commit') for t in self.the_run().state['tasks'].values()])
        self.check_invariants()

    # RUN-87, RUN-88, RUN-89 -----------------------------------------------------------------------

    OWNER_RULINGS = "# the owner's standing rulings\n"

    def status_next(self):
        """STATUS's Next as `runner status` renders it now, the work tree observed afresh."""
        self.assertEqual(self.runner('status', 'latest', '-C', self.root), 0, self.output)
        return self.next_lines()

    def restore_text(self, name, rel):
        return (f'the exported ruling make/PE-1 is missing from {rel}; restore it with '
                f'`runner export-rulings {name}`, then commit {rel}')

    def journey_exported_ruling_put_back(self):
        """W ripple W-02 as an oracle. A run failed with a ruling exported into a tracked rulings
        file; the owner undoes the file change (`git checkout --`) instead of committing it, so the
        tree is clean. The retry must refuse, naming the restore; an unrelated edit still stops
        the restore; the restore writes the export's bytes; commit, retry, resume end done with
        the ruling in the last commit once and one event."""
        rel = 'rules/rulings.toml'
        self.write(rel, self.OWNER_RULINGS)                       # tracked: on the starting commit
        tasks = policy.ONE + policy.DOWN.replace('gate=', 'max_attempts=1\ngate=')
        self.workflow(tasks, defaults=f'rulings_file="{rel}"')
        rid = 'make.review.principled-priya'
        dispute = dict(finding='make/PE-1', action='disputed', note='I ask for a scope exception')
        self.script({'make': [{'write': {'src/a': 'one\n'}, 'answer': done()},
                              {'answer': done('Asked for a ruling', responses=[dispute])}],
                     rid: [{'answer': policy.review([policy.finding()])},
                           {'answer': policy.review(resolutions=[policy.resolution(status='unresolved')])}],
                     'down': [{'answer': done()},
                              {'write': {'src/down': 'down\n'}, 'answer': done()}],
                     'down.review.principled-priya': [policy.PASS]})
        self.assertEqual(self.start(), 255, self.output)
        self.ruling_env(USER='owner')
        self.assertEqual(self.standing(NOTE), 0, self.output)
        self.assertEqual(self.resume(), 2, self.output)
        name = self.the_run().name
        path = Path(self.root, rel)
        retry_or_resume = f'runner retry {name} down [--apply-patch]\n    runner resume {name}'
        self.assertEqual(self.runner('export-rulings', name, '-C', self.root), 0, self.output)
        exported = path.read_bytes()
        self.assertIn(NOTE.encode(), exported)
        self.assertEqual(self.status_next(), retry_or_resume)
        # The owner undoes the change instead of committing it: the tree is clean again.
        self.git_out('checkout', '--', rel)
        self.assertEqual(self.git_out('status', '--porcelain'), '')
        restore = self.restore_text(name, rel)
        self.assertEqual(self.status_next(), f'# {restore}\n    runner export-rulings {name}\n'
                                             f'    {retry_or_resume}')
        self.assertIn(f'written to the rulings file {rel}; {restore}', self.page())
        self.assertNotIn('commit it, so the next run reads it', self.page())
        events = Path(self.the_run().path, 'events.jsonl').read_bytes()
        state = Path(self.the_run().path, 'state.json').read_bytes()
        self.assertEqual(self.runner('retry', name, 'down', '-C', self.root), 2,
                         'the retry took a tree without the exported ruling: ' + self.output)
        self.assertIn(f"'down' cannot be retried as the run stands: {restore}. Nothing was "
                      "changed", self.output)
        self.assertNotIn('commit the removal', self.output)
        self.assertEqual(Path(self.the_run().path, 'events.jsonl').read_bytes(), events)
        self.assertEqual(Path(self.the_run().path, 'state.json').read_bytes(), state)
        # An unrelated edit beside the removal: the restore is refused as before, writing nothing.
        self.write('README.md', 'edited while the run was stopped\n')
        self.assertEqual(self.runner('export-rulings', name, '-C', self.root), 2,
                         'export-rulings took an unrelated change: ' + self.output)
        self.assertIn('the branch or the work tree changed since the run stopped; put it back as '
                      'it was', self.output)
        self.assertEqual(path.read_text(), self.OWNER_RULINGS)
        self.git_out('checkout', '--', 'README.md')
        # The restore the refusal named: the export's bytes, nothing recorded.
        self.assertEqual(self.runner('export-rulings', name, '-C', self.root), 0,
                         'the restore was not made: ' + self.output)
        self.assertIn(f'1 exported standing ruling(s) restored in {rel}; commit it, so the next '
                      f'run reads it; then `runner retry {name} down` starts again from your '
                      'commit', self.output)
        self.assertEqual(path.read_bytes(), exported, 'the restore wrote other bytes')
        self.assertEqual(len(self.events('standing-rulings-written')), 1)
        self.assertEqual(self.status_next(), retry_or_resume)
        self.assertIn(f'runner retry {name} down', self.next_lines())  # the restore's next step
        self.commit_file(rel)
        self.assertEqual(self.status_next(), retry_or_resume)
        self.assertEqual(self.runner('retry', name, 'down', '-C', self.root), 0, self.output)
        self.assertIn(f'Continue with: runner resume {name}', self.output)
        self.assertEqual(self.status_next(), f'runner resume {name}')
        self.assertEqual(self.resume(), 0, self.output)
        self.assertEqual(self.next_lines(), 'Inspect the branch; merging it is your call.')
        self.assert_ruling_kept(rel)
        self.assertEqual(self.committed(rel).count('[[ruling]]'), 1)
        self.assertTrue(self.committed(rel).startswith(self.OWNER_RULINGS))
        self.check_invariants()

    def test_exported_ruling_put_back(self):
        """run: an exported ruling the owner put back with git checkout instead of committing:
        retry is refused naming the restore, export-rulings restores the export's bytes (an
        unrelated edit still refused), and commit, retry, resume end done with it once (RUN-87)"""
        self.journey_exported_ruling_put_back()

    def test_exported_ruling_put_back_catches_the_dirty_paths_retry(self):
        """run: RUN-87's journey fails when the retry checks dirty paths only (f6bdf21); the finish
        then still stops the run, and without that too the run is done with the ruling lost"""
        with blind_to_missing_exports('_adopt_tip'), self.assertRaises(AssertionError) as ctx:
            self.journey_exported_ruling_put_back()
        self.assertIn('the retry took a tree without the exported ruling', str(ctx.exception))
        name = self.the_run().name
        self.assertEqual(self.resume(), 2, self.output)        # the finish's guard holds
        self.assertIn(self.restore_text(name, 'rules/rulings.toml'), self.output)

    def test_exported_ruling_put_back_catches_the_unguarded_runner(self):
        """run: RUN-87's journey under f6bdf21's retry and finish ends done with the ruling in no
        commit, still marked written"""
        with blind_to_missing_exports('_adopt_tip', 'export_standing'), \
                self.assertRaises(AssertionError) as ctx:
            self.journey_exported_ruling_put_back()
        self.assertIn('the retry took a tree without the exported ruling', str(ctx.exception))
        with blind_to_missing_exports('_adopt_tip', 'export_standing'):
            self.assertEqual(self.resume(), 0, self.output)
        self.assertIn('Status: done', self.page())
        self.assertNotIn(NOTE, self.committed('rules/rulings.toml'))
        self.assertTrue(all(e['written'] for e in self.the_run().state['standing_pending']))

    def test_exported_ruling_put_back_catches_the_early_return(self):
        """run: RUN-87's journey fails at the restore when export-rulings returns "nothing to do"
        for written entries (f6bdf21)"""
        with blind_to_missing_exports('cmd_export_rulings'), self.assertRaises(AssertionError) as ctx:
            self.journey_exported_ruling_put_back()
        self.assertIn('export-rulings took an unrelated change', str(ctx.exception))
        self.assertIn('nothing to do', self.output)

    def test_exported_ruling_put_back_catches_the_unchanged_tree_check(self):
        """run: RUN-87's journey fails at the restore when the remembered-tree check refuses the
        removal it restores (f6bdf21's check: any difference is a moved tree)"""
        with mock.patch.object(cli, '_only_restored', lambda *args: False), \
                self.assertRaises(AssertionError) as ctx:
            self.journey_exported_ruling_put_back()
        self.assertIn('the restore was not made', str(ctx.exception))
        self.assertIn('the branch or the work tree changed since the run stopped', self.output)

    def test_exported_ruling_put_back_catches_a_restore_admitting_any_change(self):
        """run: RUN-87's journey fails when the restore admits an unrelated edit"""
        with restore_admits_any_change(), self.assertRaises(AssertionError) as ctx:
            self.journey_exported_ruling_put_back()
        self.assertIn('export-rulings took an unrelated change', str(ctx.exception))

    def exported_and_stopped(self, rel):
        """RUN-87's run: `down` failed with make/PE-1's standing ruling exported into `rel`."""
        self.write(rel, self.OWNER_RULINGS)
        self.workflow(policy.ONE + policy.DOWN.replace('gate=', 'max_attempts=1\ngate='),
                      defaults=f'rulings_file="{rel}"')
        rid = 'make.review.principled-priya'
        dispute = dict(finding='make/PE-1', action='disputed', note='I ask for a scope exception')
        unresolved = policy.review(resolutions=[policy.resolution(status='unresolved')])
        self.script({'make': [{'write': {'src/a': 'one\n'}, 'answer': done()},
                              {'answer': done('Asked for a ruling', responses=[dispute])}],
                     rid: [{'answer': policy.review([policy.finding()])}, {'answer': unresolved}],
                     'down': [{'answer': done()},
                              {'write': {'src/down': 'down\n'}, 'answer': done()}],
                     'down.review.principled-priya': [policy.PASS]})
        self.assertEqual(self.start(), 255, self.output)
        self.ruling_env(USER='owner')
        self.assertEqual(self.standing(NOTE), 0, self.output)
        self.assertEqual(self.resume(), 2, self.output)
        name = self.the_run().name
        self.assertEqual(self.runner('export-rulings', name, '-C', self.root), 0, self.output)
        return name

    # One facet of the exported ruling, as an owner edits it: its line, then the new line.
    EDITS = {'ruled_at': (r'ruled_at = "[^"]*"', 'ruled_at = "2026-01-01T00:00:00+00:00"'),
             'finding': (r'finding = "make/PE-1"', 'finding = "make/PE-1-retold"'),
             'scope': (r'location_glob = "[^"]*"', 'location_glob = "src/narrower"')}

    def journey_edited_export(self, facet):
        """The owner edits one facet of the exported ruling and commits it: the edited entry
        stands for the export, so nothing is restored beside it; retry and resume end done with
        one ruling, the edited one, in the last commit."""
        rel = 'rules/rulings.toml'
        name = self.exported_and_stopped(rel)
        path = Path(self.root, rel)
        line, edit = self.EDITS[facet]
        text, n = re.subn(line, edit, path.read_text())
        self.assertEqual(n, 1, facet)
        path.write_text(text)
        self.commit_file(rel)
        edited = tomllib.loads(text)['ruling'][0]
        self.assertEqual(self.runner('export-rulings', name, '-C', self.root), 0,
                         'the edited ruling was taken as missing: ' + self.output)
        rulings = tomllib.loads(path.read_text())['ruling']
        self.assertEqual(len(rulings), 1, 'the restore put the old ruling beside the edited one')
        self.assertEqual(rulings[0], edited)
        self.assertEqual(self.runner('retry', name, 'down', '-C', self.root), 0, self.output)
        self.assertEqual(self.resume(), 0, self.output)
        self.assertEqual(tomllib.loads(self.committed(rel))['ruling'], [edited])
        self.check_invariants()

    def test_edited_export_is_not_restored_beside(self):
        """run: the owner retimes, re-ids or narrows the scope of an exported standing ruling
        and commits it: `export-rulings` restores nothing beside it, and retry and resume end
        done with the one edited ruling in the last commit (RUN-93, X V-04)"""
        for facet in self.EDITS:
            with self.subTest(facet=facet):
                self.setUp()
                self.journey_edited_export(facet)

    def journey_retried_check_on_a_dirty_tree(self, forget_tree=False):
        """W d: a standalone check fails; the owner edits a tracked file and leaves it
        uncommitted; `retry` of the check, then `resume`. The check's command records the file
        as it reads it. Returns (retry's exit code, what each run of the check read)."""
        seen = os.path.join(self.side, 'seen')
        fixed = os.path.join(self.side, 'fixed')
        command = f'cat README.md >> {shlex.quote(seen)} && test -f {shlex.quote(fixed)}'
        self.workflow('\n[[task]]\nid="lint"\ntype="check"\nrun=[' + json.dumps(command) + ']\n')
        self.script([])
        self.assertEqual(self.start(), 2, self.output)
        self.assertEqual(self.status('lint'), 'failed')
        name = self.the_run().name
        if forget_tree:
            # A stop whose snapshot failed remembers no tree (RUN-85's kind of stop).
            run = self.the_run()
            run.state['expect'] = None
            run.save()
        self.write('README.md', 'the owner edit, not committed\n')
        Path(fixed).touch()
        code = self.runner('retry', name, 'lint', '-C', self.root)
        if code == 0:
            self.resume()
        return code, Path(seen).read_text()

    def test_retried_check_never_runs_on_an_uncommitted_edit(self):
        """run: a standalone check fails, the owner edits a tracked file without committing it
        and retries the check: `retry` refuses naming the change, whether the stop remembered
        its tree or not, so the check never runs on the owner's uncommitted edit (RUN-94, W d)"""
        for forget_tree in (False, True):
            with self.subTest(forget_tree=forget_tree):
                self.setUp()
                code, seen = self.journey_retried_check_on_a_dirty_tree(forget_tree)
                self.assertNotIn('the owner edit', seen,
                                 "the retried check ran on the owner's uncommitted edit")
                self.assertEqual(code, 2, self.output)
                self.assertIn("'lint' cannot be retried as the run stands: the work tree has "
                              "uncommitted changes ( M README.md)", self.output)
                # The remedy it names, followed: the commit, then the same retry and resume.
                self.git_out('commit', '-qam', 'the owner commits the edit')
                name = self.the_run().name
                self.assertEqual(self.runner('retry', name, 'lint', '-C', self.root), 0,
                                 self.output)
                self.assertEqual(self.resume(), 0, self.output)
                self.assertEqual(self.status('lint'), 'accepted')

    def test_retried_check_catches_the_unguarded_retry(self):
        """run: RUN-94 fails at "the retried check ran on the owner's uncommitted edit" under
        f41027a's `_adopt_tip`, which with no remembered tree refuses only an exported file"""
        real = engine._adopt_tip

        def old(run, git, task_id, rulings_file=None):
            if run.state.get('expect'):
                return real(run, git, task_id, rulings_file)
            refusal = engine._uncommitted_export(run, git, task_id, rulings_file,
                                                 only_exported=True)
            if refusal:
                raise refusal
            return None
        with mock.patch.object(engine, '_adopt_tip', old), self.assertRaises(AssertionError) as ctx:
            _code, seen = self.journey_retried_check_on_a_dirty_tree(forget_tree=True)
            self.assertNotIn('the owner edit', seen,
                             "the retried check ran on the owner's uncommitted edit")
        self.assertIn("the retried check ran on the owner's uncommitted edit", str(ctx.exception))

    def test_ruling_identity_keeps_two_of_three_facets(self):
        """findings: an entry of the same run is the exported ruling while it keeps two of its
        finding, its moment and its scope; one more edit, or another run, makes it another
        ruling, so a second ruling of the same title elsewhere is never taken for it (RUN-93)"""
        a = dict(run='r', finding='t/PE-1', ruled_at='1', task='t', finding_title='F',
                 location_glob='src/a')
        for edit in ({'finding': 'x'}, {'ruled_at': '2'}, {'location_glob': 'src/b'}):
            self.assertTrue(findings.same_ruling(a, dict(a, **edit)), edit)
        for edit in ({'finding': 'x', 'ruled_at': '2'}, {'ruled_at': '2', 'task': 'u'},
                     {'run': 's'}, {'finding': 't/PE-2', 'ruled_at': '2'}):
            self.assertFalse(findings.same_ruling(a, dict(a, **edit)), edit)

    def test_edited_export_catches_the_old_identity(self):
        """run: RUN-93 fails at "the edited ruling was taken as missing" under f41027a's
        `same_ruling` (run, finding and ruled_at only), for a retimed ruling: `export-rulings`
        and `retry` then refuse each other; a restore taken would append the old scope beside
        the edited one"""
        def old(a, b):
            return all(a.get(k) == b.get(k) for k in ('run', 'finding', 'ruled_at'))
        with mock.patch.object(findings, 'same_ruling', old), \
                self.assertRaises(AssertionError) as ctx:
            self.journey_edited_export('ruled_at')
        self.assertIn('the edited ruling was taken as missing', str(ctx.exception))
        # The append itself: the export's writer, asked to restore it, writes it beside.
        rel = 'rules/rulings.toml'
        entry = self.the_run().state['standing_pending'][0]
        with mock.patch.object(findings, 'same_ruling', old):
            _path, data = engine.export_bytes(self.root, rel, [entry])
        self.assertEqual(data.decode().count('[[ruling]]'), 2)
        _path, data = engine.export_bytes(self.root, rel, [entry])
        self.assertIsNone(data, 'the restore put the old ruling beside the edited one')

    def journey_put_back_off_the_branch(self):
        """RUN-87's put-back, then `export-rulings` run with another branch checked out: the
        branch is named first, never "nothing to do"; back on the run branch the restore."""
        rel = 'rules/rulings.toml'
        self.write(rel, self.OWNER_RULINGS)
        self.workflow(policy.ONE + policy.DOWN.replace('gate=', 'max_attempts=1\ngate='),
                      defaults=f'rulings_file="{rel}"')
        rid = 'make.review.principled-priya'
        dispute = dict(finding='make/PE-1', action='disputed', note='I ask for a scope exception')
        unresolved = policy.review(resolutions=[policy.resolution(status='unresolved')])
        self.script({'make': [{'write': {'src/a': 'one\n'}, 'answer': done()},
                              {'answer': done('Asked for a ruling', responses=[dispute])}],
                     rid: [{'answer': policy.review([policy.finding()])}, {'answer': unresolved}],
                     'down': [{'answer': done()}]})
        self.assertEqual(self.start(), 255, self.output)
        self.ruling_env(USER='owner')
        self.assertEqual(self.standing(NOTE), 0, self.output)
        self.assertEqual(self.resume(), 2, self.output)
        name = self.the_run().name
        branch = self.the_run().info['branch']
        self.assertEqual(self.runner('export-rulings', name, '-C', self.root), 0, self.output)
        exported = Path(self.root, rel).read_bytes()
        self.git_out('checkout', '--', rel)
        self.git_out('checkout', '-q', '-b', 'elsewhere')
        code = self.runner('export-rulings', name, '-C', self.root)
        self.assertNotIn('nothing to do', self.output,
                         'off the run branch the export said nothing to do while a restore is due')
        self.assertEqual(code, 2, self.output)
        self.assertIn(f"the run works on branch '{branch}'; check it out first. Nothing was "
                      "written", self.output)
        self.git_out('checkout', '-q', branch)
        self.assertEqual(self.runner('export-rulings', name, '-C', self.root), 0, self.output)
        self.assertIn(f'1 exported standing ruling(s) restored in {rel}', self.output)
        self.assertEqual(Path(self.root, rel).read_bytes(), exported)

    def test_export_off_the_branch_with_a_restore_due(self):
        """run: `export-rulings` with another branch checked out while an exported ruling put
        back waits for its restore: refused naming the run's branch, never "nothing to do"; on the
        branch the restore writes the export's bytes (RUN-92, X V-05)"""
        self.journey_put_back_off_the_branch()

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'root writes a 0555 directory')
    def test_exported_ruling_removed_then_replan(self):
        """run: on RUN-73's state an export the owner removed (rm, untracked) instead of committing:
        replan is refused naming the restore and writes nothing; export-rulings restores the
        export's bytes; commit, replan and resume end done with the ruling once (RUN-88)"""
        self.journey_exported_ruling_removed_then_replan()

    def journey_exported_ruling_removed_then_replan(self):
        rel = 'rules/rulings.toml'
        name = self.failed_writing_at_done(rel)
        path = Path(self.root, rel)
        self.assertEqual(self.runner('export-rulings', name, '-C', self.root), 0, self.output)
        exported = path.read_bytes()
        self.assertEqual(self.git_out('status', '--porcelain'), f'?? {rel}')
        os.unlink(path)                                   # untracked: nothing left to commit
        self.assertEqual(self.git_out('status', '--porcelain'), '')
        restore = self.restore_text(name, rel)
        self.assertEqual(self.status_next(), f'# {restore}\n    runner export-rulings {name}\n'
                                             f'    runner resume {name}')
        events = Path(self.the_run().path, 'events.jsonl').read_bytes()
        self.assertEqual(self.runner('replan', name, '--workflow', self.wf_path, '-C', self.root), 2,
                         'the replan took a tree without the exported ruling: ' + self.output)
        self.assertIn(f'{restore}. Nothing was changed', self.output)
        self.assertFalse(Path(self.the_run().path, 'replans').exists())
        self.assertEqual(Path(self.the_run().path, 'events.jsonl').read_bytes(), events)
        self.assertEqual(self.runner('export-rulings', name, '-C', self.root), 0,
                         'the restore was not made: ' + self.output)
        self.assertEqual(path.read_bytes(), exported, 'the restore wrote other bytes')
        # The restore names the command STATUS's Next prints (X ripple V-02).
        self.assertIn(f'1 exported standing ruling(s) restored in {rel}; commit it, so the next '
                      f'run reads it; then `runner resume {name}` finishes the run\n', self.output)
        self.assertEqual(self.status_next(), f'runner resume {name}')
        self.commit_file(rel)
        self.assertEqual(self.runner('replan', name, '--workflow', self.wf_path, '-C', self.root),
                         0, self.output)
        self.assertEqual(self.resume(), 0, self.output)
        self.assert_ruling_kept(rel)
        self.check_invariants()

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'root writes a 0555 directory')
    def test_exported_ruling_removed_then_replan_catches_the_exempt_replan(self):
        """run: RUN-88's replan journey fails when replan exempts the exported file by path
        (f6bdf21)"""
        with blind_to_missing_exports('prepare'), self.assertRaises(AssertionError) as ctx:
            self.journey_exported_ruling_removed_then_replan()
        self.assertIn('the replan took a tree without the exported ruling', str(ctx.exception))

    def journey_exported_ruling_removed_then_resume(self):
        """RUN-73's state, the export unable to remember a tree (S6): the owner removes the
        untracked file and resumes. Every task is accepted, so the resume finishes: it must stop
        naming the restore instead of ending done without the ruling."""
        rel = 'rules/rulings.toml'
        name = self.failed_writing_at_done(rel)
        path = Path(self.root, rel)

        def no_snapshot(eng):
            eng.run.state['expect'] = None                # remember_tree when the snapshot fails
        with mock.patch.object(engine.Engine, 'remember_tree', no_snapshot):
            self.assertEqual(self.runner('export-rulings', name, '-C', self.root), 0, self.output)
        self.assertIsNone(self.the_run().state['expect'])
        exported = path.read_bytes()
        os.unlink(path)
        restore = self.restore_text(name, rel)
        self.assertEqual(self.resume(), 2, 'resume finished without the exported ruling: '
                         + self.output)
        # The stop names the restore and, after it, the command STATUS's Next prints (V-02).
        self.assertIn(f'runner: {restore}; then `runner resume {name}` finishes the run\n',
                      self.output)
        state = self.the_run().state
        self.assertEqual((state['status'], state['expect'], state['stop_reason']),
                         ('failed', None, restore))
        self.assertEqual(self.next_lines(), f'# {restore}\n    runner export-rulings {name}\n'
                                            f'    runner resume {name}')
        self.assertEqual(self.runner('export-rulings', name, '-C', self.root), 0, self.output)
        self.assertIn(f'1 exported standing ruling(s) restored in {rel}; commit it, so the next '
                      f'run reads it; then `runner resume {name}` finishes the run\n', self.output)
        self.assertEqual(path.read_bytes(), exported)
        self.assertIsNotNone(self.the_run().state['expect'])
        self.commit_file(rel)
        self.assertEqual(self.status_next(), f'runner resume {name}')
        self.assertEqual(self.resume(), 0, self.output)
        self.assert_ruling_kept(rel)
        self.check_invariants()

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'root writes a 0555 directory')
    def test_exported_ruling_removed_then_resume(self):
        """run: on RUN-73's state with no tree remembered by the export, an export the owner
        removed stops the finishing resume failed naming the restore; export-rulings, commit and
        resume end done with the ruling once (RUN-88)"""
        self.journey_exported_ruling_removed_then_resume()

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'root writes a 0555 directory')
    def test_exported_ruling_removed_then_resume_catches_the_unchecked_finish(self):
        """run: RUN-88's resume journey fails when the finish checks nothing (f6bdf21): the run is
        done with the ruling in no commit"""
        with blind_to_missing_exports('export_standing'), self.assertRaises(AssertionError) as ctx:
            self.journey_exported_ruling_removed_then_resume()
        self.assertIn('resume finished without the exported ruling', str(ctx.exception))
        self.assertIn('Status: done', self.page())
        self.assertIsNone(self.committed('rules/rulings.toml'))

    def test_export_with_a_remembered_tree_and_an_embedded_repository(self):
        """run: export-rulings on a run with a remembered tree while a checked-out unregistered
        embedded repository fails its snapshot exits 2 naming the repository and "Nothing was written", no traceback; once
        removed the export writes (RUN-90, W ripple W-05)"""
        rel = 'rulings.toml'
        self.submodule()
        tasks = policy.ONE + policy.DOWN.replace('gate=', 'max_attempts=1\ngate=')
        self.workflow(tasks, defaults=f'rulings_file="{rel}"')
        rid = 'make.review.principled-priya'
        dispute = dict(finding='make/PE-1', action='disputed', note='I ask for a scope exception')
        self.script({'make': [{'write': {'src/a': 'one\n'}, 'answer': done()},
                              {'answer': done('Asked for a ruling', responses=[dispute])}],
                     rid: [{'answer': policy.review([policy.finding()])},
                           {'answer': policy.review(resolutions=[policy.resolution(status='unresolved')])}],
                     'down': [{'answer': done()}],
                     'down.review.principled-priya': [policy.PASS]})
        self.assertEqual(self.start(), 255, self.output)
        self.ruling_env(USER='owner')
        self.assertEqual(self.standing(NOTE), 0, self.output)
        self.assertEqual(self.resume(), 2, self.output)
        name = self.the_run().name
        self.assertIsNotNone(self.the_run().state['expect'])
        self.check_out_sub()
        state = Path(self.the_run().path, 'state.json').read_bytes()
        self.assertEqual(self.runner('export-rulings', name, '-C', self.root), 2,
                         'export-rulings did not refuse the embedded repository: ' + self.output)
        # The retry is this command, not `resume` (X ripple V-06).
        self.assertNotIn('rm -rf', self.output, 'a printed remedy contains rm -rf: ' + self.output)
        self.assertIn("an embedded git repository is in the work tree at 'sub'; a snapshot cannot "
                      "be taken. Move it out of the work tree and leave the empty directory (`mv "
                      "sub <a directory outside the work tree> && mkdir sub`), then `runner "
                      f"export-rulings {name}`. Nothing was written\n", self.output)
        self.assertNotIn('runner resume', self.output)
        self.assertFalse(Path(self.root, rel).exists())
        self.assertEqual(Path(self.the_run().path, 'state.json').read_bytes(), state)
        # The owner has worked in the clone; the printed command, run literally, keeps the work.
        Path(self.root, 'sub', 'f').write_text("owner's work\n")
        for args in (('add', '-A'), ('commit', '-qm', "owner's work")):
            subprocess.run(['git', '-c', 'user.name=x', '-c', 'user.email=x@x', '-C', 'sub', *args],
                           cwd=self.root, check=True, capture_output=True)
        self.move_sub(self.output)
        self.assertEqual(Path(self.moved_to, 'f').read_text(), "owner's work\n")
        self.assertEqual(self.runner('export-rulings', name, '-C', self.root), 0, self.output)
        self.assertIn(NOTE, Path(self.root, rel).read_text())

    def test_export_with_a_remembered_tree_and_an_embedded_repository_catches_rm(self):
        """run: RUN-90 fails when the embedded stop prints 2d6d1ce's `rm -rf sub && mkdir sub`,
        which, run literally, destroys the owner's clone (Y1b ripple V-02)"""
        with embedded_remedy_deletes(), self.assertRaises(AssertionError) as ctx:
            self.test_export_with_a_remembered_tree_and_an_embedded_repository()
        self.assertIn('a printed remedy contains rm -rf', str(ctx.exception))

    def test_export_while_the_snapshot_fails_catches_rm(self):
        """run: RUN-85's journey fails when the embedded stop prints 2d6d1ce's `rm -rf sub &&
        mkdir sub` (Y1b ripple V-02)"""
        with embedded_remedy_deletes(), self.assertRaises(AssertionError) as ctx:
            self.journey_export_while_the_snapshot_fails()
        self.assertIn('a printed remedy contains rm -rf', str(ctx.exception))

    def test_export_with_a_remembered_tree_and_an_embedded_repository_catches_the_traceback(self):
        """run: RUN-90 fails when export-rulings lets the snapshot's stop escape (f6bdf21): a
        traceback instead of a refusal, at the first export"""
        with export_lets_the_snapshot_stop_escape(), self.assertRaises(engine.EngineStop) as ctx:
            self.test_export_with_a_remembered_tree_and_an_embedded_repository()
        self.assertIn("an embedded git repository is in the work tree at 'sub'", str(ctx.exception))
        self.assertFalse(Path(self.root, 'rulings.toml').exists())

    def journey_withdrawn_after_done(self):
        """After `done` the rulings file is the owner's: a commit that removes the ruling is an
        ordinary withdrawal, and nothing refuses or nags about it."""
        rel = 'rules/rulings.toml'
        name = self.failed_writing_at_done(rel)
        self.assertEqual(self.runner('export-rulings', name, '-C', self.root), 0, self.output)
        self.commit_file(rel)
        self.assertEqual(self.resume(), 0, self.output)
        self.assert_ruling_kept(rel)
        self.git_out('rm', '-q', rel)
        self.git_out('commit', '-q', '-m', 'withdraw the standing ruling')
        self.assertNotIn('export-rulings', self.status_next(), 'STATUS nags after a withdrawal')
        self.assertTrue(self.next_lines().endswith('Inspect the branch; merging it is your call.'))
        self.assertNotIn('missing from', self.page(), 'STATUS nags after a withdrawal')
        self.assertEqual(self.runner('export-rulings', name, '-C', self.root), 0, self.output)
        self.assertIn('nothing to do', self.output, 'export-rulings restored a withdrawn ruling')
        self.assertFalse(Path(self.root, rel).exists())
        self.assertEqual(self.runner('resume', name, '-C', self.root), 0, self.output)
        self.assertIn('is done; nothing to resume', self.output)
        self.assertEqual(self.git_out('status', '--porcelain'), '')
        self.assertEqual(len(self.events('standing-rulings-written')), 1)
        # The owner reopens the done run (X ripple V-01): the replan is not refused for the
        # withdrawal, since the restore it would name says "nothing to do" on a done run.
        self.script({'make': [{'write': {'src/a': 'two\n'}, 'answer': done()}],
                     'make.review.principled-priya': [policy.PASS]})
        for counter in Path(self.script_path).parent.glob(Path(self.script_path).name + '.*.counter'):
            counter.unlink()                              # the per-task steps start again
        replan = ('replan', name, '--workflow', self.wf_path, '--reopen', 'make', '-C', self.root)
        if self.runner(*replan):
            refusal = self.output.strip()
            self.runner('export-rulings', name, '-C', self.root)
            remedy = self.output.strip()
            again = self.runner(*replan)
            self.fail(f'livelock: replan refused ({refusal}); the export-rulings it names says '
                      f'({remedy}); the replan is refused again (exit {again}: '
                      f'{self.output.strip()}); STATUS names no way out ({self.status_next()})')
        self.assertIn(f'Continue with: runner resume {name}', self.output)
        self.assertEqual(self.status_next(), f'runner resume {name}')
        # The reopened run is not done: its finish checks the export again, and every printed
        # step names the next command.
        restore = self.restore_text(name, rel)
        self.assertEqual(self.resume(), 2, 'the reopened run finished without the ruling: '
                         + self.output)
        self.assertIn(f'runner: {restore}; then `runner resume {name}` finishes the run',
                      self.output)
        self.assertEqual(self.next_lines(), f'# {restore}\n    runner export-rulings {name}\n'
                                            f'    runner resume {name}')
        self.assertEqual(self.runner('export-rulings', name, '-C', self.root), 0, self.output)
        self.assertIn(f'1 exported standing ruling(s) restored in {rel}; commit it, so the next '
                      f'run reads it; then `runner resume {name}` finishes the run', self.output)
        self.assertEqual(self.status_next(), f'runner resume {name}')
        self.commit_file(rel)
        self.assertEqual(self.resume(), 0, self.output)
        self.assertEqual(self.next_lines(), 'Inspect the branch; merging it is your call.')
        self.assert_ruling_kept(rel)
        self.assertEqual(Path(self.root, 'src/a').read_text(), 'two\n')
        # Done again: the owner may withdraw the ruling again, as before.
        self.git_out('rm', '-q', rel)
        self.git_out('commit', '-q', '-m', 'withdraw the standing ruling again')
        self.assertNotIn('export-rulings', self.status_next(), 'STATUS nags after a withdrawal')
        self.assertTrue(self.next_lines().endswith('Inspect the branch; merging it is your call.'))
        self.check_invariants()

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'root writes a 0555 directory')
    def test_withdrawn_after_done(self):
        """run: an owner's commit that removes an exported ruling after the run is done is not
        refused: STATUS, export-rulings and resume leave it alone (RUN-89)"""
        self.journey_withdrawn_after_done()

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'root writes a 0555 directory')
    def test_withdrawn_after_done_catches_a_check_without_the_done_exemption(self):
        """run: RUN-89 fails when export-rulings and STATUS check a done run too (no earlier
        behaviour: the oracle for the exemption)"""
        real_exportable, real_seen = cli.EXPORTABLE, record.missing_exports_seen
        from codesmith import findings

        def seen(run, info):
            try:
                default = json.loads(Path(run.path, 'workflow.expanded.json').read_text())[
                    'defaults'].get('rulings_file')
                return findings.missing_exports(run.state, info['git_toplevel'], default)
            except OSError:
                return real_seen(run, info)
        with mock.patch.object(cli, 'EXPORTABLE', real_exportable + ('done',)), \
                mock.patch.object(record, 'missing_exports_seen', seen), \
                self.assertRaises(AssertionError) as ctx:
            self.journey_withdrawn_after_done()
        self.assertIn('STATUS nags after a withdrawal', str(ctx.exception))

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'root writes a 0555 directory')
    def test_withdrawn_after_done_catches_the_livelocked_replan(self):
        """run: RUN-89's reopen fails when replan checks a done run (ed6e908): the refusal names
        export-rulings, which says nothing to do, and the refusal repeats"""
        with replan_checks_a_done_run(), self.assertRaises(AssertionError) as ctx:
            self.journey_withdrawn_after_done()
        text = str(ctx.exception)
        self.assertIn('livelock: replan refused (runner: the exported ruling make/PE-1 is missing from '
                      'rules/rulings.toml; restore it with `runner export-rulings', text)
        self.assertIn('nothing to do', text)
        self.assertIn('the replan is refused again (exit 2', text)
        self.assertNotIn('export-rulings', text.rpartition('STATUS names no way out')[2])


class CleanupAndOrphan(EngineCase):
    open_cleanup = boundaries.CleanupBoundary.open_cleanup
    live_group = boundaries.CleanupBoundary.live_group

    def events(self):
        path = Path(self.the_run().path) / 'events.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()]

    # PROC-32 --------------------------------------------------------------------------------------

    def journey_cleanup_beside_an_orphan(self):
        """An author's cleanup is open (its group's stop keeps failing) and a call a killed runner
        left is still running. The operator follows STATUS: `--stop-orphans` cannot close the
        cleanup; the operator stops that group by hand, then `--abandon-cleanup --by owner`,
        which refuses on the orphan recording nothing and names the one resume; that resume
        abandons the cleanup, stops the orphan and finishes, the author's work committed."""
        group = self.open_cleanup()
        orphan = self.live_group()
        run = self.the_run()
        run.amend(run.begin('command', task='make', command='left by a dead runner'), process=orphan)
        name = run.name
        run.regenerate_safely()
        self.assertIn(f'## Next\n    runner resume {name} --stop-orphans\n', self.page())
        # 1. The first way STATUS names: the cleanup's group will not stop.
        real = record.stop_process_group
        with mock.patch.object(record, 'stop_process_group',
                               side_effect=lambda ident, *a, **k: False if ident == group
                               else real(ident, *a, **k)):
            self.assertEqual(self.resume('--stop-orphans'), 2, self.output)
        self.assertIn('could not close it', self.output)
        self.assertTrue(record.is_alive(orphan))
        # 2. STATUS: "if the stop keeps failing: stop the processes yourself, then" abandon it.
        self.assertIn(f'# if the stop keeps failing: stop the processes yourself, then\n'
                      f'    runner resume {name} --abandon-cleanup\n', self.page())
        os.killpg(group['pgid'], signal.SIGKILL)
        events = (Path(run.path) / 'events.jsonl').read_bytes()
        self.assertEqual(self.resume('--abandon-cleanup', '--by', 'owner'), 2, self.output)
        self.assertEqual((Path(run.path) / 'events.jsonl').read_bytes(), events,
                         'a refused resume changed the events')
        self.assertIn('Nothing was recorded', self.output)
        self.assertIn(f'`runner resume {name} --abandon-cleanup --stop-orphans`', self.output)
        # 3. The one resume the refusal names.
        self.assertEqual(self.resume('--abandon-cleanup', '--stop-orphans', '--by', 'owner'), 0,
                         self.output)
        self.assertFalse(record.is_alive(orphan))
        self.assertIn('Status: done', (Path(run.path) / 'STATUS.md').read_text())
        self.assertEqual(self.committed('src/a'), 'good\n')
        self.assertEqual(self.git_out('status', '--porcelain'), '')
        self.assertEqual(self.calls(), 1)
        said = [e for e in self.events() if e['event'] in ('cleanup-abandoned', 'cleanup-closed')]
        self.assertEqual([(e['event'], e.get('by')) for e in said], [('cleanup-abandoned', 'owner')])
        self.check_invariants()

    committed = Journey.committed
    page = Journey.page

    def test_cleanup_beside_an_orphan(self):
        """proc: an open cleanup beside a live orphan, recovered as STATUS and each refusal say:
        --stop-orphans cannot close it, the group stopped by hand, --abandon-cleanup refuses on
        the orphan recording nothing, and --abandon-cleanup --stop-orphans ends done with the
        author's work committed and one cleanup-abandoned event (PROC-32)"""
        self.journey_cleanup_beside_an_orphan()

    def test_cleanup_beside_an_orphan_catches_the_contradiction_refusal(self):
        """proc: PROC-32's journey fails when the two resume flags are refused together (be6429a)"""
        with abandon_contradicts_stop_orphans(), self.assertRaises(AssertionError) as ctx:
            self.journey_cleanup_beside_an_orphan()
        self.assertIn('contradict each other', str(ctx.exception))

    def test_cleanup_beside_an_orphan_catches_a_ruling_recorded_on_refusal(self):
        """proc: PROC-32's journey fails when a refused --abandon-cleanup records its ruling
        (be6429a)"""
        with abandon_recorded_before_the_orphan_check(), self.assertRaises(AssertionError) as ctx:
            self.journey_cleanup_beside_an_orphan()
        self.assertIn('a refused resume changed the events', str(ctx.exception))


if __name__ == '__main__':
    unittest.main()
