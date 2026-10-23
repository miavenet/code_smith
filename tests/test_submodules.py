"""Registered git submodules (C14, GIT-27 to GIT-32): the `submodules` contract, the checks that
look inside a submodule, and the line between a registered submodule and an embedded repository.

The submodule is built from a second local bare repository in the scratch directory, which is
removed once the submodule is added: every remedy the runner prints must then work from the
submodule's local object store alone, with no network and no fetch. Each journey is
mutation-checked: a named mutation restores the earlier behaviour (its docstring names the
commit) and a test asserts the journey fails under it. No model call; the agent is scripted."""
import contextlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from unittest import mock

from helpers import EngineCase, done
from codesmith import engine, gitops
import test_recovery_sequences as sequences

PINNED_WHY = 'it is a submodule pointer, and this task\'s `submodules` is "pinned"'
INSIDE_WHY = 'it is a submodule with changes inside it, and no task writes inside a submodule'
REMEDY = re.compile(r"`([^`]+)` puts it back|put (?:it|them) back with `([^`]+)`"
                    r"|: `([^`]+)` \(an empty directory stands for")


def moved(sha, staged=False):
    """The remedy for a pointer checked out away from `sha`: names `sha`, never the index's."""
    why = ("staged at a pointer other than HEAD's and " if staged else "")
    cmd = (("git restore --staged sub && " if staged else "")
           + f"git -C sub checkout -q --detach {sha}")
    return f"sub is a submodule {why}checked out at a commit other than {sha}: `{cmd}` puts it back"


INSIDE =('sub is a submodule with uncommitted changes inside it: see them with `git -C sub '
          'status`, then commit them there or set them aside with `git -C sub stash -u`')


def task(extra=''):
    return ('\n[[task]]\nid="make"\ntype="implement"\nprompt="Make the work"\noutputs=["src/a"]\n'
            'gate=["test -f src/a"]\nmax_attempts=2\n' + extra)


# -- mutations: each restores an earlier behaviour, named by the commit it restores -------------

@contextlib.contextmanager
def submodules_are_embedded():
    """c6d5078's `embedded_repositories`: every directory holding `.git` that git does not ignore,
    a checked-out registered submodule included (W's observation)."""
    with mock.patch.object(gitops.Git, 'submodules', lambda self: []):
        yield


@contextlib.contextmanager
def dirty_paths_ignoring_submodules():
    """c6d5078's `dirty_paths`: `git status --porcelain` with no `--ignore-submodules`, so an
    owner's `ignore = all` hides a submodule's changes."""
    def old(self):
        raw = self.run("status", "--porcelain", "-z", "--untracked-files=all").stdout
        return [gitops._text(e) for e in raw.split(b"\0") if e]
    with mock.patch.object(gitops.Git, 'dirty_paths', old):
        yield


@contextlib.contextmanager
def changed_paths_ignoring_submodules():
    """c6d5078's `changed_paths`: `git diff-tree` with no `--ignore-submodules`, so an owner's
    `ignore = all` hides a moved pointer from every comparison of two trees."""
    real = gitops.Git.run

    def run(self, *args, **kw):
        if args and args[0] == 'diff-tree':
            args = tuple(a for a in args if a != '--ignore-submodules=none')
        return real(self, *args, **kw)
    with mock.patch.object(gitops.Git, 'run', run):
        yield


@contextlib.contextmanager
def pinned_check_dropped():
    """The contract's pinned check dropped: a pointer move is no violation in any task."""
    real = engine.Engine.violation

    def lax(self, task, top_path, new_mode, frozen, old_mode=None):
        if gitops.GITLINK in (new_mode, old_mode):
            return ''
        return real(self, task, top_path, new_mode, frozen, old_mode)
    with mock.patch.object(engine.Engine, 'violation', lax):
        yield


@contextlib.contextmanager
def remedy_at_the_base():
    """2d6d1ce's inspect: every violating submodule's remedy targets the task's base, so a write
    inside a listed submodule that also moved names the base's pointer (Y1b ripple V-03)."""
    def old(self, subs, base):
        return (self.git.submodule_advice(only=[r["path"] for r in subs], target=base)
                or "put " + ", ".join(r["path"] for r in subs) + " back as the task's base "
                "has it (`git status` shows it)")
    with mock.patch.object(engine.Engine, 'submodule_remedy', old):
        yield


class SubmoduleCase(EngineCase):
    def setUp(self):
        super().setUp()
        self.add_submodule()

    def sgit(self, cwd, *args):
        return subprocess.run(['git', '-c', 'user.name=x', '-c', 'user.email=x@x',
                               '-c', 'protocol.file.allow=always', *args], cwd=cwd, check=True,
                              capture_output=True, text=True).stdout.strip()

    def add_submodule(self):
        """`sub`, registered on the starting commit and checked out at its second commit; the
        bare repository it came from is removed, so nothing can be fetched afterwards."""
        work, bare = os.path.join(self.side, 'sub-work'), os.path.join(self.side, 'sub.git')
        os.makedirs(work)
        self.sgit(work, 'init', '-q', '-b', 'main')
        for text in ('one\n', 'two\n'):
            Path(work, 'f').write_text(text)
            self.sgit(work, 'add', '-A')
            self.sgit(work, 'commit', '-qm', text.strip())
        self.c1, self.c2 = self.sgit(work, 'rev-parse', 'HEAD~1'), self.sgit(work, 'rev-parse', 'HEAD')
        self.sgit(self.side, 'clone', '-q', '--bare', work, bare)
        self.sgit(self.root, 'submodule', 'add', '-q', bare, 'sub')
        self.sgit(self.root, 'commit', '-qm', 'the sub submodule')
        shutil.rmtree(bare)
        shutil.rmtree(work)

    def pointer(self, rev='HEAD'):
        return self.git_out('ls-tree', rev, 'sub').split()[2]

    def checked_out(self):
        return self.sgit(os.path.join(self.root, 'sub'), 'rev-parse', 'HEAD')

    def update(self):
        """`git submodule update --init sub`: the owner's own command, which puts back the
        *index's* pointer; it must work offline."""
        self.sgit(self.root, 'submodule', 'update', '--init', 'sub')

    def follow(self, text=None):
        """Runs the remedy the runner printed, literally, as an operator would; offline. Fails
        when the stop names no command."""
        text = self.output if text is None else text
        self.assertNotIn('rm -rf', text, 'a printed remedy contains rm -rf: ' + text)
        cmds = [next(g for g in m.groups() if g) for m in REMEDY.finditer(text)]
        self.assertTrue(cmds, 'the remedy is empty: ' + text)
        for cmd in cmds:
            # "<a directory outside the work tree>" is the operator's choice: a fresh one here.
            away = os.path.join(self.side, f'moved-{len(os.listdir(self.side))}')
            subprocess.run(cmd.replace(gitops.OUTSIDE, away), shell=True, cwd=self.root,
                           check=True, capture_output=True)
        return cmds


class Contract(SubmoduleCase):
    # GIT-27 ---------------------------------------------------------------------------------------

    def journey_pinned_pointer_moved(self):
        """An author moves the pointer of a pinned submodule and leaves a file outside `writes`.
        The run stops before anything is put back, naming the path, "pinned" and the update; the
        operator updates; resume sends the attempt back with both named and ends done."""
        self.workflow(task())
        self.script([{'write': {'src/a': 'one\n', 'stray.txt': 'x\n'},
                      'run': ['git -C sub checkout -q HEAD~1'], 'answer': done()},
                     {'write': {'src/a': 'two\n'}, 'answer': done()}])
        code = self.start()
        self.assertEqual(code, 2, 'the pinned pointer move was not stopped: ' + self.output)
        self.assertIn(f"'make' changed a submodule it may not change: sub: {PINNED_WHY}. Restoring "
                      "a submodule is not supported, so nothing was put back: "
                      f"{moved(self.c2)}, then `runner resume`", self.output)
        state = self.the_run().state
        self.assertEqual((state['status'], state['expect'], state['intents']), ('failed', None, []))
        # Nothing half-done: the pointer and the file outside `writes` are both as the author left
        # them; no restore intent is open for `resume` to repeat.
        self.assertEqual(self.checked_out(), self.c1)
        self.assertTrue(os.path.exists(os.path.join(self.root, 'stray.txt')))
        self.follow()
        self.assertEqual(self.checked_out(), self.c2)
        self.assertEqual(self.resume(), 0, self.output)
        self.assertIn(f'sub: {PINNED_WHY}; it was put back', self.prompt(2))
        self.assertIn('stray.txt: it is outside this task\'s `writes`; the runner put it back',
                      self.prompt(2))
        self.assertEqual(self.pointer(), self.c2)
        self.assertEqual(self.git_out('show', 'HEAD:src/a'), 'two')
        self.check_invariants()

    def test_pinned_pointer_moved(self):
        """git: an author's move of a pinned submodule pointer stops the run naming the path,
        "pinned" and the command that checks out the base's pointer, nothing put back and no
        intent left; after it resume sends the attempt back with it named and ends done (GIT-27)"""
        self.journey_pinned_pointer_moved()

    def test_pinned_pointer_moved_catches_the_dropped_pinned_check(self):
        """git: GIT-27 fails when the contract's pinned check is dropped: the move passes
        inspection and only the replay's backstop stops it"""
        with pinned_check_dropped(), self.assertRaises(AssertionError) as ctx:
            self.journey_pinned_pointer_moved()
        self.assertIn("changed a submodule it may not change: sub", str(ctx.exception))
        self.assertNotIn("changed a submodule it may not change", self.output)
        # Only the replay's backstop is left between the move and its commit: the move passed
        # inspection, and the acceptance replay refused it as an environment failure.
        self.assertIn("replaying a submodule's acceptance is not supported", self.output)
        self.assertNotIn('next package', self.output, 'a promise of a later package')
        self.assertEqual(self.checked_out(), self.c1)

    # GIT-28 ---------------------------------------------------------------------------------------

    def test_declared_pointer_moved(self):
        """git: a task that lists the submodule moves its pointer: the candidate tree carries the
        160000 entry, the review diff shows `Subproject commit a..b`, the commit holds it and the
        tree is clean (GIT-28)"""
        self.workflow(task('submodules=["sub"]\nacceptance_replay=false\n'))
        self.script([{'write': {'src/a': 'one\n'}, 'run': ['git -C sub checkout -q HEAD~1'],
                      'answer': done()}])
        self.assertEqual(self.start(), 0, self.output)
        st = self.the_run().state['tasks']['make']
        g = gitops.Git(self.root)
        self.assertEqual(g.ls_tree(st['candidate'])['sub'], (gitops.GITLINK, self.c1))
        diff = Path(self.the_run().path, st['attempt_dir'], 'changes.diff').read_text()
        self.assertIn(f'-Subproject commit {self.c2}\n+Subproject commit {self.c1}\n', diff)
        self.assertEqual(self.pointer(), self.c1)
        self.assertEqual(self.git_out('status', '--porcelain'), '')
        self.check_invariants()

    def test_set_aside_of_a_declared_move(self):
        """git: a listed move whose attempt fails is set aside: the restore to the base refuses
        before its intent naming the base's pointer; after it `resume` completes the set-aside (the
        pointer already back is skipped); `retry --apply-patch` of the move is refused (GIT-28)"""
        self.workflow(task('submodules=["sub"]\nacceptance_replay=false\n').replace(
            'gate=["test -f src/a"]\nmax_attempts=2', 'gate=["test -f src/never"]\nmax_attempts=1'))
        self.script([{'write': {'src/a': 'one\n'}, 'run': ['git -C sub checkout -q HEAD~1'],
                      'answer': done()}])
        self.assertEqual(self.start(), 2, self.output)
        self.assertIn('environment failure: putting a submodule back is not supported: the runner '
                      "refuses to restore a submodule's checkout itself. Nothing was changed; put "
                      f'it back with `git -C sub checkout -q --detach {self.c2}`, then '
                      '`runner resume`', self.output)
        self.assertEqual(self.the_run().state['intents'], [])
        self.assertIsNone(self.the_run().state['expect'])
        self.assertEqual(Path(self.root, 'src', 'a').read_text(), 'one\n')   # nothing put back
        self.follow()
        self.assertEqual(self.resume(), 2, self.output)
        self.assertEqual(self.status('make'), 'failed')
        self.assertFalse(os.path.exists(os.path.join(self.root, 'src', 'a')))
        self.assertEqual(self.checked_out(), self.c2)
        self.assertEqual(self.git_out('status', '--porcelain'), '')
        self.assertTrue(os.path.exists(self.task_file('make', 'failed.patch')))
        name = self.the_run().name
        self.assertEqual(self.runner('retry', name, 'make', '--apply-patch', '-C', self.root), 2)
        self.assertIn("the set-aside work of 'make' (attempt 1) cannot be put back: it moves the "
                      "submodule pointer at sub, and restoring a submodule is not supported. "
                      "Nothing was changed. Retry without --apply-patch to start clean.", self.output)
        self.check_invariants()

    def test_contract_validation(self):
        """wf: `submodules` is "pinned" or a list of registered submodules; a path inside a
        submodule, a nested or unregistered one, and a declared move with the replay on are
        workflow errors (GIT-28)"""
        head = 'name="w"\n'
        self.assertError(self.load(head + task('submodules=["sub"]\n')),
                         "replaying a submodule's acceptance is not supported; set "
                         'acceptance_replay = false')
        self.assertError(self.load(head + task('submodules=["lib"]\nacceptance_replay=false\n')),
                         "submodules path 'lib' is not a submodule registered in .gitmodules")
        self.assertError(self.load(head + task('submodules=["sub/inner"]\nacceptance_replay=false\n')),
                         'nested submodules are not supported')
        self.assertError(self.load(head + task('submodules="all"\n')),
                         "'submodules' must be \"pinned\" or a list")
        self.assertError(self.load(head + task().replace('outputs=["src/a"]',
                                                         'outputs=["src/a"]\nwrites=["src/a", "sub/**"]')),
                         "writes path 'sub/**' names the submodule 'sub'")
        self.assertLoads(self.load(head + task('submodules="pinned"\n')))
        self.assertLoads(self.load(head + task('submodules=["sub"]\nacceptance_replay=false\n')))

    def test_declared_contract_enters_the_brief_hash(self):
        """findings: a declared submodule is an output of the effective brief; "pinned" adds
        nothing, so a ruling bound before the contract existed still matches (GIT-28)"""
        from codesmith import findings
        base = {'outputs': [{'path': 'src/a'}], 'gates': [], 'needs': []}
        plain = findings.effective_brief(base, 'p', '', {})
        self.assertEqual(findings.effective_brief(dict(base, submodules='pinned'), 'p', '', {}), plain)
        moved = findings.effective_brief(dict(base, submodules=['sub']), 'p', '', {})
        self.assertEqual(moved['outputs'], ['src/a', 'submodule sub'])
        self.assertNotEqual(findings.brief_hash(moved), findings.brief_hash(plain))

    def test_replay_of_a_moved_pointer_is_refused_before_a_checkout(self):
        """git: a replay checkout of a candidate that moves a pointer refuses naming the path and
        that replaying a submodule's acceptance is not supported, and makes nothing (GIT-28)"""
        g = gitops.Git(self.root)
        index = os.path.join(self.side, 'idx')
        self.sgit(os.path.join(self.root, 'sub'), 'checkout', '-q', 'HEAD~1')
        moved = g.snapshot(index)
        dest = os.path.join(self.side, 'replay')
        with self.assertRaises(gitops.SubmoduleUnsupported) as ctx:
            g.replay_checkout(moved, g.head(), dest)
        self.assertIn("replaying a submodule's acceptance is not supported", str(ctx.exception))
        self.assertNotIn('next package', str(ctx.exception), 'a promise of a later package')
        self.assertEqual(ctx.exception.paths, ['sub'])
        self.assertFalse(os.path.exists(dest))

    def test_restore_of_a_gitlink_is_refused_before_any_change(self):
        """git: a restore that would move a pointer refuses naming the target's pointer, before
        any path is changed; once the pointer is back it completes, and a submodule is never
        removed (GIT-27)"""
        g = gitops.Git(self.root)
        index = os.path.join(self.side, 'idx')
        base = g.snapshot(index)
        self.sgit(os.path.join(self.root, 'sub'), 'checkout', '-q', 'HEAD~1')
        self.write('README.md', 'edited\n')
        with self.assertRaises(gitops.SubmoduleUnsupported) as ctx:
            g.restore(base, ['README.md', 'sub'], expected_tree=base, index_file=index)
        self.assertIn("restore a submodule's checkout itself. Nothing was changed; put it back "
                      f'with `git -C sub checkout -q --detach {self.c2}`', str(ctx.exception))
        self.assertNotIn('next package', str(ctx.exception), 'a promise of a later package')
        self.assertEqual(Path(self.root, 'README.md').read_text(), 'edited\n')
        self.follow(str(ctx.exception))
        g.restore(base, ['README.md', 'sub'], expected_tree=base, index_file=index)
        self.assertEqual(Path(self.root, 'README.md').read_text(), 'scratch\n')
        with self.assertRaises(gitops.RestoreError):
            g.remove_embedded('sub')
        self.assertTrue(os.path.exists(os.path.join(self.root, 'sub', '.git')))

    # GIT-29 ---------------------------------------------------------------------------------------

    def journey_write_inside(self, extra):
        self.workflow(task(extra))
        self.script([{'write': {'src/a': 'one\n', 'sub/f': 'changed inside\n', 'sub/new': 'x\n'},
                      'answer': done()},
                     {'write': {'src/a': 'two\n'}, 'answer': done()}])
        self.assertEqual(self.start(), 2, self.output)
        self.assertIn(f"'make' changed a submodule it may not change: sub: {INSIDE_WHY}. Restoring "
                      f"a submodule is not supported, so nothing was put back: {INSIDE}, then "
                      "`runner resume`", self.output)
        self.assertEqual(Path(self.root, 'sub', 'f').read_text(), 'changed inside\n')
        self.sgit(os.path.join(self.root, 'sub'), 'stash', '-u')
        self.assertEqual(self.resume(), 0, self.output)
        self.assertIn(f'sub: {INSIDE_WHY}; it was put back', self.prompt(2))
        self.assertEqual(self.pointer(), self.c2)
        self.check_invariants()

    def test_write_inside_a_pinned_submodule(self):
        """git: an author's write inside a pinned submodule stops naming `git -C sub` and ends
        done after the stash (GIT-29)"""
        self.journey_write_inside('')

    def test_write_inside_a_declared_submodule(self):
        """git: a write inside a submodule the task may move is a violation too (GIT-29)"""
        self.journey_write_inside('submodules=["sub"]\nacceptance_replay=false\n')

    def journey_move_and_write_inside(self):
        """A task that lists `sub` moves it (allowed) and writes inside it. The stop names only
        the stash; every command it prints, run literally, keeps the allowed move. Then any
        further printed stop is followed until the run ends done."""
        self.workflow(task('submodules=["sub"]\nacceptance_replay=false\n'))
        self.script([{'write': {'src/a': 'one\n'},
                      'run': ['git -C sub checkout -q HEAD~1', 'echo inside > sub/f'],
                      'answer': done()},
                     {'write': {'src/a': 'two\n'}, 'answer': done()}])
        self.assertEqual(self.start(), 2, self.output)
        first = self.output
        self.assertIn(f"'make' changed a submodule it may not change: sub: {INSIDE_WHY}. ", first)
        self.sgit(os.path.join(self.root, 'sub'), 'stash', '-u')     # the INSIDE advice
        cmds = [next(g for g in m.groups() if g) for m in REMEDY.finditer(first)]
        for cmd in cmds:
            subprocess.run(cmd, shell=True, cwd=self.root, check=True, capture_output=True)
        self.assertEqual(self.checked_out(), self.c1, 'the remedy undoes the allowed move')
        code, stops = self.resume(), []
        while code == 2 and len(stops) < 3:
            stops.append(self.output)
            self.follow()
            code = self.resume()
        self.assertEqual(code, 0, self.output)
        self.assertEqual(self.pointer(), self.c1)         # the rework keeps the allowed move
        self.assertIn(f'sub: {INSIDE_WHY}; it was put back', self.prompt(2))
        self.check_invariants()
        return first, stops

    def test_move_and_write_inside_keeps_the_move(self):
        """git: a listed move plus a write inside the submodule stops naming only the stash, not
        the base's pointer; run literally, the pointer stays at the candidate's, the attempt is
        sent back once with no other stop, and the run ends done with the move committed
        (GIT-29, Y1b ripple V-03)"""
        first, stops = self.journey_move_and_write_inside()
        self.assertIn(f"so nothing was put back: {INSIDE}, then `runner resume`", first)
        self.assertEqual(stops, [])

    def test_move_and_write_inside_catches_the_base_target(self):
        """git: GIT-29's listed move fails at "the remedy undoes the allowed move" when the stop
        names the base's pointer for a write-only violation (2d6d1ce)"""
        with remedy_at_the_base(), self.assertRaises(AssertionError) as ctx:
            self.journey_move_and_write_inside()
        self.assertIn('the remedy undoes the allowed move', str(ctx.exception))


class CleanTree(SubmoduleCase):
    # GIT-30 ---------------------------------------------------------------------------------------

    def journey_dirty_submodule(self):
        """The owner set `ignore = all` for the submodule. Changes inside it refuse `start` with the
        `git -C` remedy; after the stash the run starts; changes made while the run waits for a
        person refuse the next producer with the same remedy and no tree remembered; after the
        stash, resume ends done."""
        self.sgit(self.root, 'config', '-f', '.gitmodules', 'submodule.sub.ignore', 'all')
        self.git_out('commit', '-qam', 'ignore the submodule')
        self.workflow(task() + '\n[[task]]\nid="look"\ntype="human"\nneeds=["make"]\n'
                      '\n[[task]]\nid="down"\ntype="implement"\nneeds=["look"]\nprompt="More"\n'
                      'outputs=["src/down"]\ngate=["test -f src/down"]\n')
        self.script({'make': [{'write': {'src/a': 'one\n'}, 'answer': done()}],
                     'down': [{'write': {'src/down': 'down\n'}, 'answer': done()}]})
        Path(self.root, 'sub', 'f').write_text('owner edit\n')
        code = self.start()
        self.assertTrue(code == 2 and 'a run starts only from a clean tree' in self.output,
                        'start took a dirty submodule: ' + self.output)
        self.assertIn(f'nothing was changed:\n   M sub\n{INSIDE}\n', self.output)
        self.assertNotIn('Remove it', self.output)
        sub = os.path.join(self.root, 'sub')
        self.sgit(sub, 'stash', '-u')
        self.assertEqual(self.start(), 255, self.output)
        # Changes inside the submodule leave the remembered tree as it was (the snapshot holds
        # only its pointer), so resume's reconcile passes and the next producer refuses them.
        Path(sub, 'f').write_text('owner edit again\n')
        self.assertEqual(self.runner('approve', 'latest', 'look', '-C', self.root), 0, self.output)
        self.assertEqual(self.resume(), 2, 'resume took a dirty submodule: ' + self.output)
        self.assertIn("the work tree is not at a clean accepted state, so 'down' cannot start: "
                      f" M sub; {INSIDE}, then `runner resume`", self.output)
        self.assertIsNone(self.the_run().state['expect'], 'the stop remembered the dirty tree')
        self.sgit(sub, 'stash', '-u')
        self.assertEqual(self.resume(), 0, self.output)
        self.assertEqual(self.pointer(), self.c2)
        self.check_invariants()

    def test_dirty_submodule_at_start_and_resume(self):
        """git: a submodule with changes inside refuses start, and the next producer at resume, with
        its git remedy and no tree remembered, even under the owner's `ignore = all`; each remedy
        followed, the run ends done (GIT-30)"""
        self.journey_dirty_submodule()

    def test_dirty_submodule_catches_dirty_paths_ignoring_submodules(self):
        """git: GIT-30 fails when `dirty_paths` lets the owner's settings hide a submodule
        (c6d5078)"""
        with dirty_paths_ignoring_submodules(), self.assertRaises(AssertionError) as ctx:
            self.journey_dirty_submodule()
        self.assertIn('start took a dirty submodule', str(ctx.exception))

    def test_moved_pointer_while_paused_names_the_update(self):
        """git: a pointer moved while the run is paused holding the tree is refused by resume's
        reconcile naming the update, also under `ignore = all`; after it resume ends done (GIT-30)"""
        self.sgit(self.root, 'config', '-f', '.gitmodules', 'submodule.sub.ignore', 'all')
        self.git_out('commit', '-qam', 'ignore the submodule')
        self.workflow(task() + '\n[[task]]\nid="look"\ntype="human"\nneeds=["make"]\n')
        self.script([{'write': {'src/a': 'one\n'}, 'answer': done()}])
        self.assertEqual(self.start(), 255, self.output)
        self.assertIsNotNone(self.the_run().state['expect'])
        self.sgit(os.path.join(self.root, 'sub'), 'checkout', '-q', 'HEAD~1')
        self.assertEqual(self.resume(), 2, self.output)
        self.assertIn(f'while the run was paused the work tree changed: sub ({moved(self.c2)})',
                      self.output)
        self.follow()
        self.assertEqual(self.runner('approve', 'latest', 'look', '-C', self.root), 0, self.output)
        self.assertEqual(self.resume(), 0, self.output)
        self.check_invariants()

    def test_moved_pointer_while_paused_catches_changed_paths_ignoring_submodules(self):
        """git: GIT-30's paused case fails when `changed_paths` lets `ignore = all` hide the
        pointer (c6d5078's diff-tree): the refusal names no path and no update"""
        with changed_paths_ignoring_submodules(), self.assertRaises(AssertionError) as ctx:
            self.test_moved_pointer_while_paused_names_the_update()
        self.assertIn('the work tree changed: sub (', str(ctx.exception))
        self.assertIn('the work tree changed: . Nothing', self.output)


class Embedded(SubmoduleCase):
    # GIT-31 ---------------------------------------------------------------------------------------

    def journey_checked_out_submodule(self):
        """A checked-out registered submodule is no embedded repository: the run stops at a person
        with a tree remembered (`remember_tree` sets `expect`), and ends done."""
        self.workflow(task() + '\n[[task]]\nid="look"\ntype="human"\nneeds=["make"]\n')
        self.script([{'write': {'src/a': 'one\n'}, 'answer': done()}])
        self.assertEqual(self.start(), 255, 'the checked-out submodule stopped the run: ' + self.output)
        self.assertEqual(gitops.Git(self.root).embedded_repositories(), [])
        expect = self.the_run().state['expect']
        self.assertIsNotNone(expect, 'no tree was remembered')
        self.assertEqual(gitops.Git(self.root).ls_tree(expect['tree'])['sub'],
                         (gitops.GITLINK, self.c2))
        self.assertEqual(self.runner('approve', 'latest', 'look', '-C', self.root), 0, self.output)
        self.assertEqual(self.resume(), 0, self.output)
        self.check_invariants()

    def test_checked_out_submodule_is_not_embedded(self):
        """git: a checked-out registered submodule: snapshots succeed, a stop remembers the tree
        with its pointer, the run ends done (GIT-31)"""
        self.journey_checked_out_submodule()

    def test_checked_out_submodule_catches_counting_it_embedded(self):
        """git: GIT-31 fails when `embedded_repositories` counts a registered submodule (c6d5078,
        W's observation)"""
        with submodules_are_embedded(), self.assertRaises(AssertionError) as ctx:
            self.journey_checked_out_submodule()
        self.assertIn('the checked-out submodule stopped the run', str(ctx.exception))
        self.assertIn("an embedded git repository is in the work tree at 'sub'", self.output)

    # GIT-32 ---------------------------------------------------------------------------------------

    def test_unregistered_embedded_repository_is_refused_as_before(self):
        """git: a clone that is not in .gitmodules is still an embedded repository: the snapshot
        refuses it with today's text, and an author's is removed and named (GIT-32)"""
        self.workflow(task() + '\n[[task]]\nid="look"\ntype="human"\nneeds=["make"]\n')
        self.script([{'write': {'src/a': 'one\n'},
                      'run': ['git clone -q sub vendored'], 'answer': done()},
                     {'write': {'src/a': 'two\n'}, 'answer': done()}])
        self.assertEqual(self.start(), 255, self.output)
        self.assertIn("you left an embedded git repository at 'vendored'; it was removed",
                      self.prompt(2))
        self.assertFalse(os.path.exists(os.path.join(self.root, 'vendored')))
        self.sgit(self.root, 'clone', '-q', 'sub', 'owner-clone')
        self.assertEqual(gitops.Git(self.root).embedded_repositories(), ['owner-clone'])
        self.assertEqual(self.runner('approve', 'latest', 'look', '-C', self.root), 0, self.output)
        self.assertEqual(self.resume(), 2, self.output)
        self.assertIn("owner-clone", self.output)


class XGuardsWithASubmodule(SubmoduleCase, sequences.Journey):
    """X's guards with a checked-out registered submodule present: their snapshots now succeed,
    so each takes the remembered-tree branch; each journey still ends done with the ruling once."""

    def test_retry_guard(self):
        """run: RUN-87's journey (retry refused for an exported ruling put back) with a checked-out
        submodule (GIT-31)"""
        self.journey_exported_ruling_put_back()

    def test_replan_guard(self):
        """run: RUN-88's replan journey with a checked-out submodule (GIT-31)"""
        self.journey_exported_ruling_removed_then_replan()

    def test_missing_written_entry_at_finish(self):
        """run: RUN-88's resume journey (the finish refuses a missing written entry) with a
        checked-out submodule (GIT-31)"""
        self.journey_exported_ruling_removed_then_resume()

    def test_export_guard(self):
        """run: RUN-82's journey (export-rulings refused beside a running check) with a
        checked-out submodule (GIT-31)"""
        self.journey_export_and_a_paused_check()


# Only the four journeys above run here; RUN-82..90 themselves run in test_recovery_sequences.
for _name in dir(sequences.Journey):
    if _name.startswith('test') and _name not in vars(XGuardsWithASubmodule):
        setattr(XGuardsWithASubmodule, _name, None)


# -- Y1b: the four ripple blockers (GIT-33 to GIT-36) -------------------------------------------

@contextlib.contextmanager
def remedy_from_the_index():
    """b37577c's remedy: `git submodule update --init P` wherever a pointer is to be put back. It
    checks out the pointer of the superproject's index, not the one the refusal compares against
    (Y1 ripple V-01)."""
    real = gitops.Git.pointer_remedy

    def old(self, rel, want):
        why, cmds = real(self, rel, want)
        return ((["checked out at a commit other than the registered one"],
                 [f"git submodule update --init {rel}"]) if cmds else (why, cmds))
    with mock.patch.object(gitops.Git, 'pointer_remedy', old):
        yield


@contextlib.contextmanager
def dirty_tree_remembered():
    """b37577c's reconcile: a tree remembered dirty refuses every other tree, the clean tip the
    stop asked for included (Y1 ripple V-02)."""
    from codesmith import record
    with mock.patch.object(record, '_back_at_tip', lambda *a: False):
        yield


@contextlib.contextmanager
def submodule_evidence_raises():
    """b37577c's `provided_context`: a gitlink in base or candidate raises (Y1 ripple V-03)."""
    from codesmith import prompts

    def raises(_commit):
        raise ValueError('text-only evidence cannot include a submodule')
    with mock.patch.object(prompts, 'gitlink_text', raises):
        yield


@contextlib.contextmanager
def unregistered_gitlink_unnamed():
    """b37577c's `submodule_advice`, which names registered submodules only ('' for a gitlink
    with no `.gitmodules` entry), and its `remove_embedded`, which takes the directory with the
    clone (Y1 ripple V-04)."""
    real_advice, real_remove = gitops.Git.submodule_advice, gitops.Git.remove_embedded

    def advice(self, only=None, target=None):
        if not [rel for rel in self.submodules() if only is None or rel in only]:
            return ""
        return real_advice(self, only, target)

    def remove(self, rel):
        real_remove(self, rel)
        full = os.path.join(self.top, rel)
        if os.path.isdir(full) and not os.listdir(full):
            os.rmdir(full)
    with mock.patch.object(gitops.Git, 'submodule_advice', advice), \
            mock.patch.object(gitops.Git, 'remove_embedded', remove):
        yield


@contextlib.contextmanager
def put_head_back_without_restage():
    """32da730's `put_head_back`: the index reset to HEAD's tree and nothing staged again, so a
    verifier that moves HEAD leaves the index at HEAD's pointer (Y1c ripple V-01)."""
    def old(self, branch, commit, keep=None):
        if branch:
            self.run("update-ref", "-m", "code-smith: put the run branch back", branch, commit)
            self.run("symbolic-ref", "HEAD", branch)
        else:
            self.run("update-ref", "--no-deref", "HEAD", commit)
        self.sync_index()
    with mock.patch.object(gitops.Git, 'put_head_back', old):
        yield


@contextlib.contextmanager
def no_staging_before_verifiers():
    """32da730's verify loop: no verifier stages the candidate's pointers first; the other
    callers of `stage_gitlinks` (`put_head_back`, the set-aside) keep theirs."""
    real = gitops.Git.stage_gitlinks

    def staging(self, links):
        if sys._getframe(1).f_code.co_name != 'verify':
            real(self, links)
    with mock.patch.object(gitops.Git, 'stage_gitlinks', staging):
        yield


@contextlib.contextmanager
def set_aside_keeps_the_staging():
    """32da730's set-aside: the candidate's staged pointer stays in the index while the restore
    to the base is refused, so its remedy names `git restore --staged` (Y1c ripple V-04)."""
    real = gitops.Git.stage_gitlinks

    def staging(self, links):
        if sys._getframe(1).f_code.co_name != 'set_aside':
            real(self, links)
    with mock.patch.object(gitops.Git, 'stage_gitlinks', staging):
        yield


def _stop_line(text):
    """The line of a stop that names its remedy."""
    lines = [line for line in text.splitlines() if '`runner resume`' in line]
    return lines[-1] if lines else text


STASH = re.compile(r"set them aside with `([^`]+)`")
EMBEDDED = re.compile(r"Move it out of the work tree(?: and leave the empty directory)? \(`([^`]+)`\)")


class Following(SubmoduleCase):
    """The operator of every journey from Y1b on: each printed command, run literally."""

    def follow(self, text=None, stash=False):
        """As `SubmoduleCase.follow`; with `stash`, also the stash an inside-dirt stop names. A
        printed command that fails is the finding, not an error of the test."""
        text = self.output if text is None else text
        self.stops = getattr(self, 'stops', []) + [text]
        try:
            if stash:
                for cmd in STASH.findall(text):
                    subprocess.run(cmd, shell=True, cwd=self.root, check=True, capture_output=True)
                if not REMEDY.search(text) and not EMBEDDED.search(text):
                    return []
            moves = EMBEDDED.findall(text)
            for cmd in moves:
                away = os.path.join(self.side, f'moved-{len(os.listdir(self.side))}')
                subprocess.run(cmd.replace(gitops.OUTSIDE, away), shell=True, cwd=self.root,
                               check=True, capture_output=True)
            if moves and not REMEDY.search(text):
                return moves
            return super().follow(text)
        except subprocess.CalledProcessError as exc:
            self.fail(f'a printed remedy failed: {exc.cmd}: {exc.stderr!r}')

    def follow_until_done(self, code, limit=4, stash=False):
        """Each printed stop, run literally, then `resume`, until the run ends; a stop printed
        again right after its own remedy is the livelock (Y1b ripple V-01)."""
        while code == 2 and '`runner resume`' in self.output and len(self.stops) < limit:
            if self.stops and _stop_line(self.output) == _stop_line(self.stops[-1]):
                self.fail('the stop repeated after its printed remedy: ' + self.output)
            self.follow(stash=stash)
            code = self.resume()
        return code

    def _start_or_kill(self, kill_after, point='verify:after-command'):
        if kill_after:
            class Killed(Exception):
                pass
            seen = {'n': 0}

            def hook(where):
                if where == point:
                    seen['n'] += 1
                    if seen['n'] == kill_after:
                        raise Killed(where)
            self.cli.CRASH = hook
            with self.assertRaises(Killed):
                self.start()
            self.cli.CRASH = None
            return self.resume()
        return self.start()


class Remedies(Following):
    """Every printed pointer remedy, run literally, ends the stop (V-01)."""

    # GIT-33 ---------------------------------------------------------------------------------------

    def journey_staged_move(self):
        """An author of a pinned task moves the pointer and stages it (`git add -A`): the stop's
        printed remedy, run literally, puts back both the checkout and the index, and resume
        sends the attempt back once and ends done."""
        self.workflow(task())
        self.script([{'write': {'src/a': 'one\n'},
                      'run': ['git -C sub checkout -q HEAD~1', 'git add -A'], 'answer': done()},
                     {'write': {'src/a': 'two\n'}, 'answer': done()}])
        self.assertEqual(self.start(), 2, self.output)
        self.assertIn(f"'make' changed a submodule it may not change: sub: {PINNED_WHY}",
                      self.output)
        self.follow()
        code = self.resume()
        self.assertNotIn('changed a submodule it may not change', self.output,
                         'the stop repeated after its printed remedy')
        self.assertEqual(code, 0, self.output)
        self.assertIn(f'sub: {PINNED_WHY}; it was put back', self.prompt(2))
        self.assertEqual(self.pointer(), self.c2)
        self.assertEqual(self.git_out('status', '--porcelain'), '')
        self.check_invariants()

    def test_staged_move_names_the_base_pointer(self):
        """git: an author's staged move of a pinned pointer stops naming `git restore --staged`
        and a checkout of the base's commit; run literally, resume ends done (GIT-33)"""
        self.journey_staged_move()
        self.assertIn(f"nothing was put back: {moved(self.c2, staged=True)}, then `runner resume`",
                      self.stops[0])

    def test_staged_move_catches_the_index_remedy(self):
        """git: GIT-33's staged move repeats its stop under b37577c's remedy, `git submodule
        update --init sub`, which checks out the staged pointer again"""
        with remedy_from_the_index(), self.assertRaises(AssertionError) as ctx:
            self.journey_staged_move()
        self.assertIn('the stop repeated after its printed remedy', str(ctx.exception))

    def journey_gate_updates(self):
        """A declared move whose gate resets the index and runs `git submodule update --init sub`
        once (a build script's first run), so the runner's staging does not reach the update:
        the gate's result is void and attempt 1 used (C14 as revised), then the restore to the
        candidate is refused naming the candidate's pointer; run literally, resume puts nothing
        back twice, the next attempt is verified on the candidate and the run ends done."""
        marker = os.path.join(self.side, 'updated-once')
        gate = (f"test -e {marker} || {{ touch {marker} && git reset -q && "
                "git submodule update --init sub; }; test -f src/a")
        self.workflow(task('submodules=["sub"]\nacceptance_replay=false\n').replace(
            'gate=["test -f src/a"]', f"gate=['{gate}']"))
        self.script([{'write': {'src/a': 'one\n'}, 'run': ['git -C sub checkout -q HEAD~1'],
                      'answer': done()},
                     {'write': {'src/a': 'two\n'}, 'answer': done()}])
        self.assertEqual(self.start(), 2, self.output)
        self.assertIn("'make': a verifier changed a submodule, so its result is void and attempt "
                      "1 is used (C14). The candidate is put back before the next attempt: "
                      "putting a submodule back is not supported", self.output)
        self.assertEqual(self.checked_out(), self.c2)            # the gate's update moved it
        self.follow()
        code = self.resume()
        self.assertNotIn('putting a submodule back is not supported', self.output,
                         'the stop repeated after its printed remedy')
        self.assertEqual(code, 0, self.output)
        self.assertEqual(self.pointer(), self.c1)
        self.assertIn(engine.SUBMODULE_VOID, self.prompt(2))
        self.check_invariants()

    def test_gate_update_names_the_candidate_pointer(self):
        """git: a gate's `git submodule update` under a declared move stops naming a checkout of
        the candidate's commit, not the update, and no `git add`; run literally, resume ends
        done (GIT-33)"""
        self.journey_gate_updates()
        self.assertIn('Nothing was changed; put it back with `git -C sub checkout -q --detach '
                      f'{self.c1}`, then `runner resume`', self.stops[0])

    def test_gate_update_catches_the_index_remedy(self):
        """git: GIT-33's gate journey repeats its stop under b37577c's remedy"""
        with remedy_from_the_index(), self.assertRaises(AssertionError) as ctx:
            self.journey_gate_updates()
        self.assertIn('the stop repeated after its printed remedy', str(ctx.exception))

    # GIT-37, GIT-38 ------------------------------------------------------------------------------

    UPDATE = 'git submodule update --init sub && '
    COMMIT = 'git -c user.name=x -c user.email=x@x commit -q --allow-empty -m moved'

    def journey_listed_gates(self, gates, attempts=2, kill_after=0):
        """A listed move (`git -C sub checkout HEAD~1`, the candidate's pointer c1) verified by
        `gates`. The operator follows each printed remedy literally. `self.index_after` records
        the index's pointer after each verifier and each reset of the verify step's lease; with
        `kill_after`, the runner is killed after that many verifier commands and resumed."""
        self.stops, self.index_after = [], []
        listed = ', '.join(f"'{g}'" for g in gates)
        self.workflow(task('submodules=["sub"]\nacceptance_replay=false\n').replace(
            'gate=["test -f src/a"]\nmax_attempts=2', f'gate=[{listed}]\nmax_attempts={attempts}'))
        self.script([{'write': {'src/a': 'one\n'}, 'run': ['git -C sub checkout -q HEAD~1'],
                      'answer': done()}])
        real = engine.Engine.keep_lease

        def spy(eng, task_, where):
            repairs = len(eng.st(task_['id']).get('git_repairs', []))
            real(eng, task_, where)
            reset = len(eng.st(task_['id']).get('git_repairs', [])) > repairs
            if where.startswith('`') or (reset and eng.st(task_['id'])['step'] == 'verify'):
                self.index_after.append((where, eng.git.index_gitlink('sub')))
        with mock.patch.object(engine.Engine, 'keep_lease', spy):
            return self.follow_until_done(self._start_or_kill(kill_after))

    def seen_by_the_update(self):
        """The index's pointer each run of the updating gate found (`git rev-parse :sub`)."""
        with open(os.path.join(self.side, 'seen.log')) as fh:
            return fh.read().split()

    def updating_gate(self, last='test -f src/a'):
        return f"git rev-parse :sub >> {os.path.join(self.side, 'seen.log')} && {self.UPDATE}{last}"

    def assert_done_holding_the_move(self, code):
        self.assertEqual(self.stops, [], 'a stop was printed: ' + '\n'.join(self.stops))
        self.assertEqual(code, 0, self.output)
        self.assertEqual(self.pointer(), self.c1)
        self.assertEqual(self.pointer('HEAD~1'), self.c2)    # no verifier's commit was kept
        self.assertEqual(self.checked_out(), self.c1)
        self.assertEqual(self.git_out('rev-parse', ':sub'), self.c1)
        self.assertEqual(self.git_out('status', '--porcelain'), '')
        self.check_invariants()

    def test_gate_updating_every_run_ends_done(self):
        """git: a listed move whose gate runs `git submodule update` on every run: the runner
        stages the candidate's pointer before the gate, so the update checks it out, no stop is
        printed and the run ends done, the commit holding it and the index at HEAD (GIT-37,
        Y1c ripple V-01)"""
        code = self.journey_listed_gates([self.updating_gate()])
        self.assert_done_holding_the_move(code)
        self.assertEqual(self.seen_by_the_update(), [self.c1])

    def test_gate_updating_every_run_catches_no_staging(self):
        """git: GIT-37 fails at "a stop was printed" when no verifier stages the candidate's
        pointer first (32da730's verify loop): the update checks out HEAD's pointer, which voids
        the gate's result (C14 as revised), a bounded wrong fail where Y1d had a livelock"""
        with no_staging_before_verifiers(), self.assertRaises(AssertionError) as ctx:
            self.assert_done_holding_the_move(self.journey_listed_gates([self.updating_gate()]))
        self.assertIn('a stop was printed', str(ctx.exception))
        self.assertIn('a verifier changed a submodule, so its result is void', str(ctx.exception))

    def journey_failing_gate(self, gates):
        code = self.journey_listed_gates(gates, attempts=1)
        self.assertEqual(code, 2, self.output)
        self.assertEqual(self.status('make'), 'failed')
        self.assertEqual(len(self.stops), 1, self.stops)
        self.assertIn('put it back with `git -C sub checkout -q --detach '
                      f'{self.c2}`, then `runner resume`', self.stops[0],
                      'the set-aside names an index command')
        self.assertEqual(self.checked_out(), self.c2)
        self.assertEqual(self.git_out('rev-parse', ':sub'), self.c2)
        self.assertEqual(self.git_out('status', '--porcelain'), '')
        self.check_invariants()

    def test_gate_updating_every_run_that_fails_is_bounded(self):
        """git: GIT-37's gate also fails: the failure is judged on the candidate's pointer, the
        set-aside puts the index back at HEAD's and names only the base's checkout; run
        literally, the run ends failed with the pointer and the index at HEAD"""
        self.journey_failing_gate([self.updating_gate('test -f src/never')])
        self.assertEqual(self.seen_by_the_update(), [self.c1])

    def test_failing_gate_catches_the_kept_staging(self):
        """git: GIT-37's failing gate names `git restore --staged sub` when the set-aside keeps
        the runner's staging (32da730)"""
        with set_aside_keeps_the_staging(), self.assertRaises(AssertionError) as ctx:
            self.journey_failing_gate([self.updating_gate('test -f src/never')])
        self.assertIn('the set-aside names an index command', str(ctx.exception))

    def test_head_moving_gate_before_updating_gate_ends_done(self):
        """git: a gate that moves HEAD (an empty commit) before a gate that runs `git submodule
        update`, on start and across a kill and `resume` mid-verify: after every verifier and
        every lease check the index holds the candidate's pointer, the update checks it out,
        no stop is printed and the run ends done (GIT-38, Y1c ripple V-01)"""
        for kill_after in (0, 1):
            with self.subTest(kill_after=kill_after):
                if kill_after:
                    self.setUp()
                code = self.journey_listed_gates([self.COMMIT, self.updating_gate()],
                                                 kill_after=kill_after)
                self.assertIn('put back what `git -c user.name=x', self.output)
                self.assert_done_holding_the_move(code)
                self.assertTrue(self.index_after, 'no verifier ran')
                if kill_after:                   # resume's lease check put the moved HEAD back
                    self.assertIn('before step verify', [w for w, _h in self.index_after])
                for where, held in self.index_after:
                    self.assertEqual(held, self.c1, "the index lost the candidate's pointer "
                                     f"after a verifier: {where}")
                self.assertEqual(set(self.seen_by_the_update()), {self.c1})
                os.unlink(os.path.join(self.side, 'seen.log'))

    def test_head_moving_gate_catches_no_restage(self):
        """git: GIT-38 fails at "the index lost the candidate's pointer after a verifier" under
        32da730's `put_head_back`, which resets the index and stages nothing again"""
        with put_head_back_without_restage(), self.assertRaises(AssertionError) as ctx:
            self.journey_listed_gates([self.COMMIT, self.updating_gate()])
            for where, held in self.index_after:
                self.assertEqual(held, self.c1, "the index lost the candidate's pointer "
                                 f"after a verifier: {where}")
        self.assertIn("the index lost the candidate's pointer after a verifier",
                      str(ctx.exception))

    def test_index_resetting_gate_before_updating_gate_ends_done(self):
        """git: a gate that resets the index itself (`git reset -q`) before a gate that runs
        `git submodule update`: the next verifier stages the candidate's pointer again and the
        run ends done with no stop (GIT-38)"""
        code = self.journey_listed_gates(['git reset -q', self.updating_gate()])
        self.assert_done_holding_the_move(code)
        self.assertEqual(self.seen_by_the_update(), [self.c1])

    def test_head_moving_gate_then_failing_update_is_consistent(self):
        """git: GIT-38's updating gate also fails: the set-aside leaves the index and the
        checkout at HEAD's pointer once its one printed checkout is run (GIT-38)"""
        self.journey_failing_gate([self.COMMIT, self.updating_gate('test -f src/never')])

    def test_pinned_gate_updating_every_run_sends_back(self):
        """git: a pinned task whose gate runs `git submodule update` on every run: the author's
        move stops naming the base's checkout (no `git add`); run literally, the attempt is sent
        back once and the run ends done (GIT-37)"""
        self.stops = []
        # No replay: its fresh clone would fetch the submodule, and the test has no network.
        self.workflow(task('acceptance_replay=false\n').replace(
            'gate=["test -f src/a"]', f'gate=["{self.UPDATE}test -f src/a"]'))
        self.script([{'write': {'src/a': 'one\n'}, 'run': ['git -C sub checkout -q HEAD~1'],
                      'answer': done()},
                     {'write': {'src/a': 'two\n'}, 'answer': done()}])
        code = self.follow_until_done(self.start())
        self.assertEqual(code, 0, self.output)
        self.assertEqual(len(self.stops), 1, self.stops)
        self.assertIn(f"nothing was put back: {moved(self.c2)}, then `runner resume`",
                      self.stops[0])
        self.assertIn(f'sub: {PINNED_WHY}; it was put back', self.prompt(2))
        self.assertEqual(self.pointer(), self.c2)
        self.assertEqual(self.git_out('status', '--porcelain'), '')
        self.check_invariants()


THREE = ('\n[[task]]\nid="look"\ntype="human"\nneeds=["make"]\n'
         '\n[[task]]\nid="down"\ntype="implement"\nneeds=["look"]\nprompt="More"\n'
         'outputs=["src/down"]\ngate=["test -f src/down"]\n')


class Reconcile(SubmoduleCase):
    """A stop's own remedy is never refused by the next reconcile (V-02)."""

    def window(self):
        """GIT-30's window: changes inside sub while the run waits for a person; the next producer
        refuses them with no tree remembered."""
        self.workflow(task() + THREE)
        self.script({'make': [{'write': {'src/a': 'one\n'}, 'answer': done()}],
                     'down': [{'write': {'src/down': 'down\n'}, 'answer': done()}]})
        self.assertEqual(self.start(), 255, self.output)
        Path(self.root, 'sub', 'f').write_text('owner edit\n')
        self.assertEqual(self.runner('approve', 'latest', 'look', '-C', self.root), 0, self.output)
        self.assertEqual(self.resume(), 2, self.output)
        self.assertIn(f"so 'down' cannot start:  M sub; {INSIDE}, then `runner resume`",
                      self.output)
        self.assertIsNone(self.the_run().state['expect'])

    # GIT-34 ---------------------------------------------------------------------------------------

    def journey_owner_edit_in_the_window(self):
        """GIT-30's window, then any owner edit: the next stop remembers the dirty tree; the owner
        cleans it, as that stop asks; resume must take the clean tip, not ask for the edit back."""
        self.window()
        self.write('README.md', 'owner edit\n')
        self.sgit(os.path.join(self.root, 'sub'), 'stash', '-u')          # the printed remedy
        self.assertEqual(self.resume(), 2, self.output)
        self.assertIn("the work tree is not at a clean accepted state, so 'down' cannot start: "
                      " M README.md", self.output)
        self.assertIsNotNone(self.the_run().state['expect'])     # the dirty tree is remembered
        self.git_out('checkout', '--', 'README.md')               # what the stop asks: clean
        code = self.resume()
        self.assertNotIn('while the run was paused the work tree changed', self.output,
                         'the stops alternate: cleaning the tree is refused as a change')
        self.assertEqual(code, 0, self.output)
        self.check_invariants()

    def test_cleaned_tree_is_taken(self):
        """run: a stop that remembered a dirty tree takes the clean tip it asked for; the two
        stops never alternate, and the run ends done (GIT-34)"""
        self.journey_owner_edit_in_the_window()

    def test_cleaned_tree_catches_the_remembered_dirty_tree(self):
        """run: GIT-34 alternates when reconcile holds the owner to the dirty tree (b37577c)"""
        with dirty_tree_remembered(), self.assertRaises(AssertionError) as ctx:
            self.journey_owner_edit_in_the_window()
        self.assertIn('the stops alternate', str(ctx.exception))
        self.assertIn('put it back as it was', self.output)

    def test_half_followed_commit_names_the_index(self):
        """run: in GIT-30's window the owner moves the pointer and stages it without the commit:
        the next producer's stop names `git restore --staged` and the checkout, remembers no
        tree, and the printed remedy, run literally, ends done (GIT-34)"""
        self.window()
        sub = os.path.join(self.root, 'sub')
        self.sgit(sub, 'stash', '-u')
        self.sgit(sub, 'checkout', '-q', 'HEAD~1')
        self.git_out('add', 'sub')                       # half of `git add sub && git commit`
        self.assertEqual(self.resume(), 2, self.output)
        self.assertIn(f"{moved(self.c2, staged=True)}, or commit the new pointer with "
                      "`git add sub && git commit`, then `runner resume`", self.output)
        self.assertIsNone(self.the_run().state['expect'])
        self.follow()
        self.assertEqual(self.resume(), 0, self.output)
        self.assertEqual(self.git_out('status', '--porcelain'), '')
        self.check_invariants()


class TextEvidence(SubmoduleCase):
    """A `provided_context` reviewer sees a gitlink as its pointer line (V-03)."""
    HEADER = EngineCase.HEADER + 'review_mode = "provided_context"\n'

    def untraced(self, *args):
        try:
            return self.runner(*args)
        except Exception as exc:                          # the finding itself: nothing caught it
            self.fail(f'{args[0]} tracebacked: {exc!r}')

    # GIT-35 ---------------------------------------------------------------------------------------

    def journey_text_only_review(self):
        from test_findings import review
        self.workflow(task('reviewers=["principled-priya"]\n'))
        self.script({'make': [{'write': {'src/a': 'one\n'}, 'answer': done()}],
                     'make.review.principled-priya': [{'answer': review()}]})
        self.assertEqual(self.untraced('start', self.wf_path), 0, self.output)
        self.check_invariants()

    def test_text_only_review_shows_the_pointer(self):
        """git: a `provided_context` reviewer on a tree with a registered submodule runs; its
        evidence carries the gitlink as `Subproject commit <sha>` and nothing inside (GIT-35)"""
        self.journey_text_only_review()
        sent = Path(self.script_path + '.make.review.principled-priya.prompts', '1.md').read_text()
        self.assertIn(f'Subproject commit {self.c2}', sent)
        self.assertNotIn('sub/f', sent)
        found = list(Path(self.the_run().path).rglob('evidence.json'))
        self.assertEqual(len(found), 1, found)
        files = {f['path']: f for f in json.loads(found[0].read_text())['files']}
        self.assertEqual(files['sub']['versions']['base']['mode'], gitops.GITLINK)
        self.assertEqual(files['sub']['versions']['base']['object'], self.c2)

    def test_text_only_review_catches_the_raise(self):
        """git: GIT-35 fails when a gitlink raises in the evidence (b37577c): `start` tracebacks
        and `resume` repeats it"""
        with submodule_evidence_raises():
            with self.assertRaises(AssertionError) as ctx:
                self.journey_text_only_review()
            self.assertIn('start tracebacked', str(ctx.exception))
            with self.assertRaises(AssertionError) as ctx:
                self.untraced('resume', '-C', self.root)
            self.assertIn('resume tracebacked', str(ctx.exception))


class UnregisteredGitlink(SubmoduleCase):
    """A committed gitlink with no `.gitmodules` entry (RUN-85's kind) beside `sub` (V-04)."""

    def setUp(self):
        super().setUp()
        self.lib = os.path.join(self.side, 'lib')
        os.makedirs(self.lib)
        Path(self.lib, 'f').write_text('lib\n')
        for args in (('init', '-q', '-b', 'main'), ('add', '-A'), ('commit', '-qm', 'lib')):
            self.sgit(self.lib, *args)
        self.sgit(self.root, 'clone', '-q', self.lib, 'vend')
        self.sgit(self.root, 'add', 'vend')
        self.sgit(self.root, 'commit', '-qm', 'vend, a gitlink with no .gitmodules entry')
        shutil.rmtree(os.path.join(self.root, 'vend'))
        os.makedirs(os.path.join(self.root, 'vend'))
        self.vend = self.git_out('ls-tree', 'HEAD', 'vend').split()[2]
        self.stops = []

    # GIT-36 ---------------------------------------------------------------------------------------

    def journey(self, run):
        """The author's `run` empties or replaces the committed gitlink's directory; the operator
        follows every printed remedy literally until the run ends."""
        self.workflow(task())
        self.script([{'write': {'src/a': 'one\n'}, 'run': run, 'answer': done()},
                     {'write': {'src/a': 'two\n'}, 'answer': done()}])
        code = self.start()
        for _ in range(3):
            if code != 2:
                break
            self.stops.append(self.output)
            self.follow()
            code = self.resume()
        self.assertEqual(code, 0, self.output)
        self.assertEqual(self.git_out('ls-tree', 'HEAD', 'vend').split()[2], self.vend)
        self.assertTrue(os.path.isdir(os.path.join(self.root, 'vend')))
        self.assertEqual(self.git_out('status', '--porcelain'), '')
        self.check_invariants()

    def test_removed_directory_names_mkdir(self):
        """git: an author removes a committed gitlink's directory (no .gitmodules entry): the stop
        names `mkdir vend`, never an empty remedy; followed, resume ends done (GIT-36)"""
        self.journey(['rmdir vend'])
        self.assertEqual(len(self.stops), 1, self.stops)
        self.assertIn("vend is a gitlink with no .gitmodules entry whose directory is gone: "
                      f"`mkdir vend` (an empty directory stands for the pointer {self.vend}), then "
                      "`runner resume`", self.stops[0])

    def test_clone_into_the_directory_keeps_it(self):
        """git: an author clones into a committed gitlink's empty directory: the clone is removed,
        the empty directory made again, the attempt sent back naming it, and no stop (GIT-36)"""
        self.journey([f'git clone -q {self.lib} vend'])
        self.assertEqual(self.stops, [])
        self.assertIn("you left an embedded git repository at 'vend'; it was removed",
                      self.prompt(2))

    def test_removed_directory_catches_the_empty_remedy(self):
        """git: GIT-36 fails at "the remedy is empty" under b37577c's advice and removal"""
        with unregistered_gitlink_unnamed(), self.assertRaises(AssertionError) as ctx:
            self.journey(['rmdir vend'])
        self.assertIn('the remedy is empty', str(ctx.exception))

    def files_in_the_directory(self):
        """A restore to HEAD that names the committed gitlink while files sit in its directory:
        refused before any change, naming the move; run literally, the files survive where they
        went and the restore completes."""
        Path(self.root, 'vend', 'notes').write_text('work\n')
        g = gitops.Git(self.root)
        index = os.path.join(self.side, 'idx')
        with self.assertRaises(gitops.SubmoduleUnsupported) as ctx:
            g.restore(g.tree_of('HEAD'), ['vend'], index_file=index)
        text = str(ctx.exception)
        self.follow(text)
        kept = list(Path(self.side).glob('moved-*/notes'))
        self.assertEqual([p.read_text() for p in kept], ['work\n'], 'the files were not kept')
        g.restore(g.tree_of('HEAD'), ['vend'], index_file=index)
        self.assertEqual(os.listdir(os.path.join(self.root, 'vend')), [])
        return text

    def test_files_in_the_directory_are_moved_not_deleted(self):
        """git: a restore meets files in a committed gitlink's directory (no .gitmodules entry):
        it names `mv vend <a directory outside the work tree> && mkdir vend`, never `rm -rf`; run
        literally, the files survive where they went (GIT-36, Y1b ripple V-02)"""
        text = self.files_in_the_directory()
        self.assertIn("put it back with `mv vend <a directory outside the work tree> && mkdir "
                      f"vend` (an empty directory stands for the pointer {self.vend}), then "
                      "`runner resume`", text)

    def test_files_in_the_directory_catches_rm(self):
        """git: GIT-36's directory with files fails at "a printed remedy contains rm -rf" under
        2d6d1ce's `rm -rf vend && mkdir vend`"""
        with sequences.embedded_remedy_deletes(), self.assertRaises(AssertionError) as ctx:
            self.files_in_the_directory()
        self.assertIn('a printed remedy contains rm -rf', str(ctx.exception))

    def test_clone_catches_the_empty_remedy(self):
        """git: GIT-36's clone way fails at "the remedy is empty" under b37577c"""
        with unregistered_gitlink_unnamed(), self.assertRaises(AssertionError) as ctx:
            self.journey([f'git clone -q {self.lib} vend'])
        self.assertIn('the remedy is empty', str(ctx.exception))


# -- Y1e: the submodule lineage closed (GIT-39 to GIT-47) ---------------------------------------
#
# Every journey ends at a settled terminal: the task's status, the tree released
# (`active_producer` None), no intent open, `git status` clean and the work preserved; exit code 2
# or a different stop text alone proves nothing (Y1e brief).

@contextlib.contextmanager
def restore_before_disposition():
    """e24828e's (Y1d's) order at all three sites: the verify loop puts the candidate back before
    the void is recorded, the panel's suspect recheck restores before `demoted` and `rechecked`
    are saved (panels.py:649), and a standalone check restores before its verdict is written,
    so `resume` reruns the offending command (audit L1-L3 and the standalone repeat)."""
    from codesmith import panels
    real_void = engine.Engine.void_result

    def void(self, task_, v, entry, results, budget, hits, after, ran):
        cand = self.st(task_['id'])['candidate']
        now = self.snapshot()
        self.restore(cand, [p for _s, p, _o, _n in self.git.changed_paths(cand, now)], cand)
        return real_void(self, task_, v, entry, results, budget, hits, after, ran)

    def recheck(self, task_, cid, void_, candidate):
        check = self.tasks[cid]
        _, directory = self.run.new_attempt(cid)
        _ran, problems = self.run_commands(check['run'], cid, directory, check['gate_timeout_min'])
        after = self.snapshot()
        if after != candidate:
            paths = [p for _s, p, _o, _n in self.git.changed_paths(candidate, after)]
            self.restore(candidate, paths, candidate)
            engine.set_status(self.st(cid), cid, 'objected', demoted=True,
                              reason='declared read_only but wrote: ' + ', '.join(paths))
            void_['demoted'].append(cid)
        if problems:
            return problems
        void_['rechecked'].append(cid)
        self.save()
        return []

    def check(self, task_):
        tid, st = task_['id'], self.st(task_['id'])
        base = self.snapshot()
        self.pin(f"{tid}/check-base", base)
        engine.set_status(st, tid, "running", reason="", check_base=base)
        self.run.save()
        _n, adir = self.run.new_attempt(tid)
        results, _problems = self.run_commands(task_["run"], tid, adir, task_["gate_timeout_min"])
        after = self.snapshot()
        verdict = "accepted" if engine.checks.passed(results, task_["run"]) else "failed"
        if after != base:
            self.restore(base, [p for _s, p, _o, _n in self.git.changed_paths(base, after)], base)
            verdict = "failed" if not task_["restores"] else verdict
        self.run.write_decision(os.path.join(adir, "verification.json"),
                                {"task": tid, "tree": base, "results": results,
                                 "result": "pass" if verdict == "accepted" else "fail"})
        engine.set_status(st, tid, verdict, reason="")
        self.save()
    with mock.patch.object(engine.Engine, 'void_result', void), \
            mock.patch.object(panels.Panels, 'recheck_suspect', recheck), \
            mock.patch.object(engine.Engine, 'standalone_check', check):
        yield


@contextlib.contextmanager
def dirt_unseen():
    """e24828e's verifier check: snapshot-tree equality alone, so a write inside a submodule by a
    gate, a panel check or a reviewer is never seen (audit D2)."""
    with mock.patch.object(engine.Engine, 'submodule_dirt', lambda self: []):
        yield


@contextlib.contextmanager
def missing_entry_not_restored():
    """e24828e's `pointer_remedy`: no index command for a registered path whose target is not
    HEAD's, even when the verifier removed the entry (`update-index --force-remove`), so
    `git submodule update --init P` cannot select the path (audit L4)."""
    real = gitops.Git.pointer_remedy

    def old(self, rel, want):
        why, cmds = real(self, rel, want)
        if (self.index_gitlink(rel) is None and rel in self.submodules()
                and want != self.gitlink_at('HEAD', rel)):
            keep = [c for c in cmds if not c.startswith('git restore --staged')]
            why = [w for w in why if not w.startswith('staged at')]
            return why, keep
        return why, cmds
    with mock.patch.object(gitops.Git, 'pointer_remedy', old):
        yield


@contextlib.contextmanager
def pointer_reads_head():
    """e24828e's `pointer`: a path without `.git` stands for HEAD's pointer whatever is there, a
    regular file included, so a restore skips it as already put back (audit L5)."""
    def old(self, rel):
        if os.path.lexists(os.path.join(self.top, *rel.split("/"), ".git")):
            res = self.run("-C", rel, "rev-parse", "--verify", "-q", "HEAD", check=False)
            return gitops._text(res.stdout).strip() or None
        return self.gitlink_at("HEAD", rel)
    with mock.patch.object(gitops.Git, 'pointer', old):
        yield


@contextlib.contextmanager
def restore_intent_deadlock():
    """e24828e's reconcile: a change to the gitlink of an open restore intent is refused against
    the remembered tree before the intent is replayed (audit L5)."""
    from codesmith import record
    with mock.patch.object(record, '_restoring_gitlinks', lambda *a: False):
        yield


@contextlib.contextmanager
def unquoted_paths():
    """e24828e's remedies: the raw `{rel}` in every printed command (audit L6)."""
    import types
    with mock.patch.object(gitops, 'shlex', types.SimpleNamespace(quote=lambda s: s)):
        yield


@contextlib.contextmanager
def file_removed():
    """e24828e's obstruction remedy: `rm P && mkdir P`, which deletes the file (audit D1)."""
    real = gitops.Git.pointer_remedy

    def old(self, rel, want):
        why, cmds = real(self, rel, want)
        return why, [f"rm {rel}" if c.startswith('mv ') and c.endswith(gitops.OUTSIDE) else c
                     for c in cmds]
    with mock.patch.object(gitops.Git, 'pointer_remedy', old):
        yield


@contextlib.contextmanager
def enclosing_stash_only():
    """e24828e's inside-dirt advice: `git -C P stash -u` alone, which never reaches a nested
    checkout's changes (audit L7)."""
    with mock.patch.object(gitops.Git, 'nested_dirty', lambda self, rel, depth=0: []):
        yield


@contextlib.contextmanager
def check_head_kept():
    """e24828e's standalone check: no HEAD put back, so its tree is classified, and restored,
    against the registration its own commit made (audit L8)."""
    real = gitops.Git.put_head_back

    def kept(self, branch, commit, keep=None):
        if sys._getframe(1).f_code.co_name != 'standalone_check':
            real(self, branch, commit, keep)
    with mock.patch.object(gitops.Git, 'put_head_back', kept):
        yield


@contextlib.contextmanager
def revert_refuses_checkouts():
    """e24828e's revert precheck: any tracked difference refuses, a gitlink's checkout between
    two reverts of its pointer included (audit L9)."""
    def old(self):
        raw = self.run("status", "--porcelain", "-z", "--no-renames",
                       "--untracked-files=no").stdout
        return [gitops._text(e[3:]) for e in raw.split(b"\0") if e]
    with mock.patch.object(gitops.Git, 'revert_blockers', old):
        yield


ANSWER_BLOCKED = {'outcome': 'blocked', 'summary': 'stuck', 'blocked_reason': 'cannot go on',
                  'responses': []}


class Settled(Following):
    """Oracles shared by GIT-39 to GIT-47."""

    def assert_settled(self, status, tid='make'):
        state = self.the_run().state
        self.assertEqual(self.status(tid), status, self.output)
        self.assertIsNone(state.get('active_producer'), 'the tree is still held')
        self.assertEqual(state['intents'], [], 'an intent is still open')
        self.assertEqual(self.git_out('status', '--porcelain'), '', 'the tree is not clean')
        self.check_invariants()

    def listed(self, gates, extra='', attempts=2):
        listed = ', '.join(json.dumps(g) for g in gates)
        return task('submodules=["sub"]\nacceptance_replay=false\n' + extra).replace(
            'gate=["test -f src/a"]\nmax_attempts=2', f'gate=[{listed}]\nmax_attempts={attempts}')

    def mover(self, n=2, target='HEAD~1'):
        """An author who moves sub to `target` once and then reworks only its file."""
        return ([{'write': {'src/a': 'one\n'}, 'run': [f'git -C sub checkout -q {target}'],
                  'answer': done()}]
                + [{'write': {'src/a': f'{k}\n'}, 'answer': done()} for k in range(2, n + 1)])


class Invalidation(Settled):
    # A reviewer needs the `boundary` capability; the scripted agent ignores the flag, so a
    # scripted reviewer can still write, which is the case GIT-47 needs.
    HEADER = EngineCase.HEADER + 'read_only_args = ["--read-only"]\n'

    # GIT-39 ---------------------------------------------------------------------------------------

    def journey_void(self, wf, script, kill=0, point='verify:void-recorded', limit=6):
        self.stops = []
        self.workflow(wf)
        self.script(script)
        return self.follow_until_done(self._start_or_kill(kill, point), limit=limit)

    def entry_points(self):
        """name -> (workflow, script, the terminal status of `make`)."""
        c1, c2 = self.c1, self.c2
        check = ('\n[[task]]\nid="c"\ntype="check"\nverifies="make"\nrestores=false\n'
                 'run=["git reset -q", "git submodule update --init sub"]\n')
        return {
            'reset-then-update gate': (
                self.listed(['git reset -q && git submodule update --init sub && test -f src/a']),
                self.mover(), 'failed'),
            'pinned explicit checkout': (
                task('acceptance_replay=false\n').replace(
                    'gate=["test -f src/a"]',
                    f'gate=["git -C sub checkout -q --detach {c1} && test -f src/a"]'),
                [{'write': {'src/a': 'one\n'}, 'answer': done()},
                 {'write': {'src/a': 'two\n'}, 'answer': done()}], 'failed'),
            'listed explicit checkout': (
                self.listed([f'git -C sub checkout -q --detach {c2} && test -f src/a']),
                self.mover(), 'failed'),
            'check list entry': (self.listed(['test -f src/a']) + check, self.mover(), 'failed'),
        }

    def test_verifier_that_changes_a_submodule_is_void_and_bounded(self):
        """git: a reset-then-update gate, a pinned and a listed explicit checkout and a check's
        two-command list: each run voids its result and uses the attempt before any recovery is
        printed; every printed remedy run literally, `resume` never reruns the verifier on that
        result and the task ends `failed` within its attempts, released, no intent, the tree
        clean; also killed between the disposition and the restore (GIT-39)"""
        for name in self.entry_points():
            for kill in (0, 1):
                with self.subTest(entry=name, kill=kill):
                    self.setUp()
                    wf, script, status = self.entry_points()[name]
                    code = self.journey_void(wf, script, kill)
                    self.assertEqual(code, 2, self.output)
                    self.assert_settled(status)
                    st = self.the_run().state['tasks']['make']
                    self.assertLessEqual(st['attempts_used'], 2)
                    self.assertIn("a verifier changed a submodule, so its result is void and "
                                  "attempt 1 is used (C14). The candidate is put back before the "
                                  "next attempt: ", self.stops[0])
                    self.assertEqual(self.checked_out(), self.c2)

    def test_void_catches_restore_before_disposition(self):
        """git: GIT-39's reset-then-update gate repeats its stop under e24828e's order (restore,
        then record the void): `resume` reruns the gate"""
        wf, script, _status = self.entry_points()['reset-then-update gate']
        with restore_before_disposition(), self.assertRaises(AssertionError) as ctx:
            self.journey_void(wf, script)
        self.assertIn('the stop repeated after its printed remedy', str(ctx.exception))

    def journey_panel_recheck(self):
        checks = ''.join(f'\n[[task]]\nid="{cid}"\ntype="check"\nverifies="make"\nread_only=true\n'
                         f'restores=false\nrun=["{run}"]\n' for cid, run in (
                             ('c1', 'true'), ('c2', f'git -C sub checkout -q --detach {self.c2}')))
        return self.journey_void(self.listed(['test -f src/a']) + checks, self.mover(), limit=8)

    def test_panel_suspect_recheck_is_recorded_first(self):
        """git: a read-only panel check that checks out HEAD's pointer: the panel is void, the
        suspect is rechecked once and demoted before anything is put back; run literally, the
        demoted check voids verify as a writer and the task ends `failed` within its attempts
        (GIT-39, audit L3)"""
        code = self.journey_panel_recheck()
        self.assertEqual(code, 2, self.output)
        self.assert_settled('failed')
        self.assertTrue(self.the_run().state['tasks']['c2']['demoted'])
        self.assertTrue(any("read-only check 'c2' changed the candidate on its recheck; it is "
                            "demoted and its result recorded" in s for s in self.stops), self.stops)

    def test_panel_recheck_catches_restore_before_disposition(self):
        """git: GIT-39's panel recheck repeats its stop under e24828e's panels.py:649 order"""
        with restore_before_disposition(), self.assertRaises(AssertionError) as ctx:
            self.journey_panel_recheck()
        self.assertIn('the stop repeated after its printed remedy', str(ctx.exception))

    def journey_standalone(self, kill=0):
        wf = (f'\n[[task]]\nid="c"\ntype="check"\nrun=["git -C sub checkout -q --detach '
              f'{self.c1}"]\n')
        return self.journey_void(wf, [], kill, point='check:verdict-recorded')

    def test_standalone_check_is_decided_before_the_restore(self):
        """git: a standalone check that checks out another pointer: its `failed` is recorded
        before the restore; run literally, `resume` completes the restore and applies it without
        running the check again, also when killed in between (GIT-39)"""
        for kill in (0, 1):
            with self.subTest(kill=kill):
                self.setUp()
                code = self.journey_standalone(kill)
                self.assertEqual(code, 2, self.output)
                self.assert_settled('failed', 'c')
                self.assertEqual(len(list(Path(self.the_run().path).rglob('attempt-*'))), 1)
                if not kill:
                    self.assertIn("check 'c' failed (its result is recorded); the tree is put "
                                  "back before anything else runs: putting a submodule back",
                                  self.stops[0])

    def test_standalone_catches_restore_before_disposition(self):
        """git: GIT-39's standalone check repeats its stop under e24828e's order"""
        with restore_before_disposition(), self.assertRaises(AssertionError) as ctx:
            self.journey_standalone()
        self.assertIn('the stop repeated after its printed remedy', str(ctx.exception))

    def test_ordinary_update_still_ends_done(self):
        """git: GIT-37's every-run `git submodule update` leaves the candidate's pointer, so it
        voids nothing and the run ends `done` with no stop (GIT-39)"""
        self.stops = []
        self.workflow(self.listed(['git submodule update --init sub && test -f src/a']))
        self.script(self.mover(1))
        self.assertEqual(self.start(), 0, self.output)
        self.assertEqual(self.pointer(), self.c1)

    # GIT-40 ---------------------------------------------------------------------------------------

    def journey_deinit(self):
        gate = 'git submodule deinit -q -f sub && git update-index --force-remove sub && test -f src/a'
        return self.journey_void(self.listed([gate]), self.mover())

    def test_deinit_and_removed_entry_recover(self):
        """git: a gate that deinitialises sub and removes its index entry: the remedy restores the
        entry before `git submodule update --init sub`; every printed command run literally
        offline, the task ends `failed` within its attempts (GIT-40, audit L4)"""
        code = self.journey_deinit()
        self.assertEqual(code, 2, self.output)
        self.assert_settled('failed')
        self.assertIn(f"`git restore --staged sub && git submodule update --init sub && git -C sub "
                      f"checkout -q --detach {self.c1}`", self.stops[0])

    def test_deinit_catches_the_suppressed_entry(self):
        """git: GIT-40 fails at "a printed remedy failed" under e24828e's `pointer_remedy`, which
        names no index command for a candidate's target"""
        with missing_entry_not_restored(), self.assertRaises(AssertionError) as ctx:
            self.journey_deinit()
        self.assertIn('a printed remedy failed: git submodule update --init sub', str(ctx.exception))

    # GIT-47 ---------------------------------------------------------------------------------------

    def journey_inside_write(self, wf, script, limit=8):
        self.stops = []
        self.workflow(wf)
        self.script(script)
        return self.follow_until_done(self.start(), limit=limit, stash=True)

    def inside_writers(self):
        from test_findings import review
        write = 'echo dirty >> sub/f'
        check = ('\n[[task]]\nid="c"\ntype="check"\nverifies="make"\nread_only=true\n'
                 f'restores=false\nrun=["{write}"]\n')
        return {
            'gate': (self.listed([f'{write} && test -f src/a']), self.mover()),
            'panel check': (self.listed(['test -f src/a']) + check, self.mover()),
            'reviewer': (self.listed(['test -f src/a'], 'reviewers=["principled-priya"]\n'),
                         {'make': self.mover(),
                          'make.review.principled-priya': [
                              {'write': {'sub/f': 'reviewed\n'}, 'answer': review()}] * 2}),
        }

    def test_write_inside_a_submodule_never_reaches_done(self):
        """git: a gate, a panel read-only check or a reviewer that writes inside sub: the result
        is void (the panel void) before acceptance, the printed stash run literally, and the task
        ends `failed`, never `done` with a result earned on the dirty dependency (GIT-47)"""
        for name, (wf, script) in self.inside_writers().items():
            with self.subTest(writer=name):
                self.setUp()
                code = self.journey_inside_write(wf, script)
                self.assertNotEqual(self.status('make'), 'accepted',
                                    'accepted a result earned on a dirty submodule')
                self.assertEqual(code, 2, self.output)
                self.assert_settled('failed')
                self.assertTrue(any('git -C sub stash -u' in s for s in self.stops), self.stops)

    def test_write_inside_catches_snapshot_equality(self):
        """git: GIT-47's gate reaches `done` under e24828e's snapshot equality alone"""
        wf, script = self.inside_writers()['gate']
        with dirt_unseen(), self.assertRaises(AssertionError) as ctx:
            self.journey_inside_write(wf, script)
            self.assertNotEqual(self.status('make'), 'accepted',
                                'accepted a result earned on a dirty submodule')
        self.assertIn('accepted a result earned on a dirty submodule', str(ctx.exception))


class Shapes(Settled):
    # GIT-41 ---------------------------------------------------------------------------------------

    def blocked_author(self):
        self.stops = []
        away = os.path.join(self.side, 'authors-sub')
        self.workflow(task())
        self.script([{'run': [f'mv sub {away}', 'printf "owner notes\\n" > sub'],
                      'answer': ANSWER_BLOCKED}])
        return away

    def journey_file_at_the_path(self, kill=0):
        self.blocked_author()
        self.assertEqual(self.start(), 2, self.output)
        self.assertIn("put it back with `mv sub <a directory outside the work tree> && "
                      "mkdir sub`, then `runner resume`", self.output)
        self.assertEqual(self.the_run().state['intents'], [])
        self.follow()
        if kill:
            class Killed(Exception):
                pass

            def hook(where):
                if where == 'restore:after-removals':
                    raise Killed(where)
            self.cli.CRASH = hook
            with self.assertRaises(Killed):
                self.resume()
            self.cli.CRASH = None
            self.assertEqual([it['kind'] for it in self.the_run().state['intents']], ['restore'])
        self.assertEqual(self.resume(), 255, self.output)
        self.assert_settled('blocked')
        kept = [p.read_text() for p in Path(self.side).glob('moved-*')]
        self.assertEqual(kept, ['owner notes\n'], "the file's bytes were not kept")

    def test_file_at_a_submodule_path_is_kept(self):
        """git: a blocked author replaces sub's directory with a file: the set-aside's restore
        refuses before its intent naming `mv sub … && mkdir sub`; run literally the file's bytes
        survive where they went, and `resume` settles `blocked`, also when killed inside the
        restore (GIT-41, audit L5)"""
        for kill in (0, 1):
            with self.subTest(kill=kill):
                self.setUp()
                self.journey_file_at_the_path(kill)

    def test_file_catches_the_pointer_read_from_head(self):
        """git: GIT-41 fails under e24828e's `pointer` (HEAD's pointer for a file): the restore
        opens its intent, then fails its verification naming no command"""
        with pointer_reads_head(), self.assertRaises(AssertionError) as ctx:
            self.journey_file_at_the_path()
        self.assertIn('put it back with `mv sub', str(ctx.exception))

    def journey_open_intent(self):
        """A run an earlier runner stopped with the restore intent open and the file's tree
        remembered (e24828e's `pointer`); then this runner: the printed remedy, `resume`."""
        self.blocked_author()
        with pointer_reads_head():
            self.assertEqual(self.start(), 2, self.output)
        state = self.the_run().state
        self.assertEqual([it['kind'] for it in state['intents']], ['restore'])
        self.assertIsNotNone(state['expect'])
        subprocess.run(f'mv sub {os.path.join(self.side, "moved-0")} && mkdir sub', shell=True,
                       cwd=self.root, check=True)
        code = self.resume()
        self.assertNotIn('while the run was paused the work tree changed', self.output,
                         'the restore intent deadlocked')
        self.assertEqual(code, 255, self.output)
        self.assert_settled('blocked')

    def test_open_restore_intent_takes_the_remedy(self):
        """git: a run stopped with the restore intent open and the file's tree remembered: the
        remedy's change touches only the intent's gitlink, so `resume` takes it, replays the
        intent and settles `blocked` (GIT-41)"""
        self.journey_open_intent()

    def test_open_intent_catches_the_deadlock(self):
        """git: GIT-41's open intent is refused as a change under e24828e's reconcile"""
        with restore_intent_deadlock(), self.assertRaises(AssertionError) as ctx:
            self.journey_open_intent()
        self.assertIn('the restore intent deadlocked', str(ctx.exception))

    # GIT-43 ---------------------------------------------------------------------------------------

    def journey_owner_notes(self):
        self.stops = []
        self.workflow(task())
        self.script([{'write': {'src/a': 'one\n'}, 'answer': done()}])
        shutil.move(os.path.join(self.root, 'sub'), os.path.join(self.side, 'owners-sub'))
        Path(self.root, 'sub').write_text('unique owner notes\n')
        self.assertEqual(self.start(), 2, self.output)
        self.follow()
        kept = [p.read_text() for p in Path(self.side).glob('moved-*')]
        self.assertEqual(kept, ['unique owner notes\n'], 'the owner notes were not kept')
        self.assertEqual(self.start(), 0, self.output)
        self.check_invariants()

    def test_obstructing_file_is_moved_not_removed(self):
        """git: the owner's file where sub's directory belongs: `start` names `mv sub … && mkdir
        sub`, never `rm sub`; run literally, the bytes survive and the run ends `done` (GIT-43,
        audit D1)"""
        self.journey_owner_notes()
        self.assertIn("sub is a submodule whose directory is a file: `mv sub <a directory outside "
                      "the work tree> && mkdir sub` puts it back", self.stops[0])

    def test_obstructing_file_catches_rm(self):
        """git: GIT-43 fails at "the owner notes were not kept" under e24828e's `rm sub`"""
        with file_removed(), self.assertRaises(AssertionError) as ctx:
            self.journey_owner_notes()
        self.assertIn('the owner notes were not kept', str(ctx.exception))


class SpacedPath(Settled):
    """GIT-42: a submodule registered at `vendor dep`."""

    def add_submodule(self):
        super().add_submodule()
        self.sgit(self.root, 'mv', 'sub', 'vendor dep')
        self.sgit(self.root, 'commit', '-qm', 'move the submodule to a path with a space')

    def checked_out(self):
        return self.sgit(os.path.join(self.root, 'vendor dep'), 'rev-parse', 'HEAD')

    def journeys(self):
        self.stops = []
        q = "'vendor dep'"
        # inspect: a pinned author's move
        self.workflow(task('acceptance_replay=false\n'))
        self.script([{'write': {'src/a': 'one\n'}, 'run': [f'git -C {q} checkout -q HEAD~1'],
                      'answer': done()},
                     {'write': {'src/a': 'two\n'}, 'answer': done()}])
        self.assertEqual(self.follow_until_done(self.start()), 0, self.output)
        self.assertIn(f"`git -C {q} checkout -q --detach {self.c2}` puts it back", self.stops[0])
        self.check_invariants()
        # restore: a listed move set aside
        self.setUp()
        self.stops = []
        self.workflow(task('submodules=["vendor dep"]\nacceptance_replay=false\n').replace(
            'gate=["test -f src/a"]\nmax_attempts=2', 'gate=["test -f src/never"]\nmax_attempts=1'))
        self.script([{'write': {'src/a': 'one\n'}, 'run': [f'git -C {q} checkout -q HEAD~1'],
                      'answer': done()}])
        self.assertEqual(self.follow_until_done(self.start()), 2, self.output)
        self.assertIn(f"`git -C {q} checkout -q --detach {self.c2}`", self.stops[0])
        self.assert_settled('failed')
        # reconcile: moved while the run waits for a person
        self.setUp()
        self.stops = []
        self.workflow(task('acceptance_replay=false\n') + '\n[[task]]\nid="look"\ntype="human"\n'
                      'needs=["make"]\n')
        self.script([{'write': {'src/a': 'one\n'}, 'answer': done()}])
        self.assertEqual(self.start(), 255, self.output)
        self.sgit(os.path.join(self.root, 'vendor dep'), 'checkout', '-q', 'HEAD~1')
        self.assertEqual(self.resume(), 2, self.output)
        self.follow()
        self.assertEqual(self.runner('approve', 'latest', 'look', '-C', self.root), 0, self.output)
        self.assertEqual(self.resume(), 0, self.output)
        self.check_invariants()

    def test_spaced_path_remedies_run(self):
        """git: a submodule at `vendor dep` through inspect, a set-aside restore and resume's
        reconcile: every printed command quotes the path, runs literally and each journey ends
        settled (GIT-42, audit L6)"""
        self.journeys()

    def test_spaced_path_catches_raw_paths(self):
        """git: GIT-42 fails at "a printed remedy failed" under e24828e's raw `{rel}`"""
        with unquoted_paths(), self.assertRaises(AssertionError) as ctx:
            self.journeys()
        self.assertIn('a printed remedy failed', str(ctx.exception))


class Nested(Settled):
    """GIT-44: `sub` holds an initialised nested submodule `inner`."""

    def add_submodule(self):
        inner, inner_bare = os.path.join(self.side, 'inner-work'), os.path.join(self.side, 'in.git')
        os.makedirs(inner)
        self.sgit(inner, 'init', '-q', '-b', 'main')
        Path(inner, 'g').write_text('inner\n')
        self.sgit(inner, 'add', '-A')
        self.sgit(inner, 'commit', '-qm', 'inner')
        self.sgit(self.side, 'clone', '-q', '--bare', inner, inner_bare)
        work, bare = os.path.join(self.side, 'sub-work'), os.path.join(self.side, 'sub.git')
        os.makedirs(work)
        self.sgit(work, 'init', '-q', '-b', 'main')
        Path(work, 'f').write_text('one\n')
        self.sgit(work, 'add', '-A')
        self.sgit(work, 'commit', '-qm', 'one')
        self.sgit(work, 'submodule', 'add', '-q', inner_bare, 'inner')
        self.sgit(work, 'commit', '-qm', 'two')
        self.c1, self.c2 = self.sgit(work, 'rev-parse', 'HEAD~1'), self.sgit(work, 'rev-parse', 'HEAD')
        self.sgit(self.side, 'clone', '-q', '--bare', work, bare)
        self.sgit(self.root, 'submodule', 'add', '-q', bare, 'sub')
        self.sgit(self.root, 'submodule', 'update', '-q', '--init', '--recursive')
        self.sgit(self.root, 'commit', '-qm', 'the sub submodule, with inner inside')
        for path in (bare, work, inner_bare, inner):
            shutil.rmtree(path)

    def journey_nested_dirt(self):
        self.stops = []
        self.workflow(task('acceptance_replay=false\n'))
        self.script([{'write': {'src/a': 'one\n', 'sub/inner/g': 'dirty\n'}, 'answer': done()},
                     {'write': {'src/a': 'two\n'}, 'answer': done()}])
        code = self.follow_until_done(self.start(), stash=True)
        self.assertEqual(code, 0, self.output)
        self.assertIn('set them aside with `git -C sub/inner stash -u && git -C sub stash -u`',
                      self.stops[0])
        self.assert_settled('accepted')

    def test_nested_dirt_names_the_nested_stash(self):
        """git: an author's write inside a nested checkout `sub/inner`: the stop names the
        nested stash before the enclosing one; run literally, the attempt is sent back once and
        the run ends `done` (GIT-44, audit L7)"""
        self.journey_nested_dirt()

    def test_nested_dirt_catches_the_enclosing_stash(self):
        """git: GIT-44 repeats its stop under e24828e's enclosing `git -C sub stash -u` alone"""
        with enclosing_stash_only(), self.assertRaises(AssertionError) as ctx:
            self.journey_nested_dirt()
        self.assertIn('the stop repeated after its printed remedy', str(ctx.exception))


class Registration(Settled):
    """GIT-45: a check's commit registers a submodule over an ordinary file."""

    def setUp(self):
        super().setUp()
        self.lib = os.path.join(self.side, 'libsrc')
        os.makedirs(self.lib)
        Path(self.lib, 'x').write_text('lib source\n')
        for args in (('init', '-q', '-b', 'main'), ('add', '-A'), ('commit', '-qm', 'lib')):
            self.sgit(self.lib, *args)
        Path(self.root, 'lib').write_text('the owner file\n')
        self.sgit(self.root, 'add', 'lib')
        self.sgit(self.root, 'commit', '-qm', 'an ordinary file lib')
        self.register = (f"git rm -q lib && git -c protocol.file.allow=always submodule add -q "
                         f"{self.lib} lib && git -c user.name=x -c user.email=x@x commit -qm reg")

    def journey_check_registers(self):
        self.stops = []
        self.workflow(f'\n[[task]]\nid="c"\ntype="check"\nrun=["{self.register}"]\n')
        self.script([])
        head = self.git_out('rev-parse', 'HEAD')
        code = self.follow_until_done(self.start())
        self.assertEqual(code, 2, self.output)
        self.assert_settled('failed', 'c')
        self.assertEqual(self.git_out('rev-parse', 'HEAD'), head, "the check's commit was kept")
        self.assertEqual(Path(self.root, 'lib').read_text(), 'the owner file\n')
        self.assertTrue(os.path.isdir(os.path.join(self.root, '.git', 'modules', 'lib')),
                        "the check's repository was lost")
        self.assertIn("an embedded git repository is in the work tree at 'lib'", self.stops[0])

    def test_check_that_registers_a_submodule(self):
        """git: a standalone check replaces an ordinary file with a committed, registered
        submodule: HEAD is put back before its tree is classified, the result recorded `failed`,
        the printed move run literally; `resume` settles the check `failed` with the owner's file
        back and the check's repository kept (GIT-45, audit L8)"""
        self.journey_check_registers()

    def test_registration_catches_the_kept_head(self):
        """git: GIT-45 fails under e24828e's standalone check, which classifies against the
        registration its own commit made"""
        with check_head_kept(), self.assertRaises(AssertionError):
            self.journey_check_registers()

    def test_gate_that_registers_a_submodule(self):
        """git: a gate commits a new registered submodule: the lease puts HEAD back, the
        embedded repository voids the result before the snapshot's stop; run literally, the task
        ends `failed` within its attempts (GIT-45)"""
        self.stops = []
        gate = self.register.replace('git rm -q lib && ', '').replace(' lib && ', ' lib2 && ')
        self.workflow(self.listed([gate + ' && test -f src/a']))
        self.script(self.mover())
        code = self.follow_until_done(self.start(), limit=6)
        self.assertEqual(code, 2, self.output)
        self.assert_settled('failed')
        self.assertIn("an embedded git repository is in the work tree at 'lib2'", self.stops[0])


class Reopen(Settled):
    """GIT-46: two accepted dependent pointer moves, the upstream one reopened."""

    def setUp(self):
        super().setUp()
        sub = os.path.join(self.root, 'sub')
        self.sgit(sub, 'commit', '-q', '--allow-empty', '-m', 'three')
        self.c3 = self.sgit(sub, 'rev-parse', 'HEAD')
        self.sgit(sub, 'checkout', '-q', '--detach', self.c2)

    def journey_reopen(self):
        self.stops = []
        a = task('submodules=["sub"]\nacceptance_replay=false\n')
        b = a.replace('id="make"', 'id="down"').replace('src/a', 'src/b') + 'needs=["make"]\n'
        self.workflow(a + b)
        self.script({'make': [{'write': {'src/a': 'one\n'},
                               'run': [f'git -C sub checkout -q {self.c1}'], 'answer': done()}] * 2,
                     'down': [{'write': {'src/b': 'one\n'},
                               'run': [f'git -C sub checkout -q {self.c3}'], 'answer': done()}] * 2})
        self.assertEqual(self.start(), 0, self.output)
        name = self.the_run().name
        code = self.runner('replan', name, '--reopen', 'make', '-C', self.root)
        self.assertEqual(code, 0, 'the reverts refused the intermediate checkout: ' + self.output)
        self.assertIn(f"before resume: sub is a submodule checked out at a commit other than "
                      f"{self.c2}: `git -C sub checkout -q --detach {self.c2}` puts it back",
                      self.output)
        self.assertNotIn('git commit', self.output)
        self.follow()
        self.assertEqual(self.follow_until_done(self.resume()), 0, self.output)
        self.assertEqual(self.pointer(), self.c3)
        self.assert_settled('accepted', 'down')

    def test_reopen_of_two_pointer_moves(self):
        """git: A moves sub a→b, B b→c, both accepted; `replan --reopen` A reverts both commits
        through the checkout left between them, prints the checkout of the reverted pointer and
        no commit; run literally, `resume` reruns both and ends `done` (GIT-46, audit L9)"""
        self.journey_reopen()

    def test_reopen_catches_the_generic_refusal(self):
        """git: GIT-46 fails under e24828e's revert precheck ("commit or stash them")"""
        with revert_refuses_checkouts(), self.assertRaises(AssertionError) as ctx:
            self.journey_reopen()
        self.assertIn('the reverts refused the intermediate checkout', str(ctx.exception))

    def test_revert_refusal_never_advises_a_commit(self):
        """git: a revert refused for a change of the owner's names the stash and `runner resume`,
        never a commit, which the replan intent would refuse (GIT-46)"""
        g = gitops.Git(self.root)
        self.write('README.md', 'owner edit\n')
        head = g.head()
        with self.assertRaises(gitops.RevertConflict) as ctx:
            g.revert_commits([head], 'op-x', 'run', head)
        text = str(ctx.exception)
        self.assertIn('README.md; set them aside with `git stash -u`, then `runner resume`', text)
        self.assertNotIn('commit', text.split(': ', 1)[1].split(': ', 1)[1])


# -- Y1f: the narrowing where Y1e missed it (GIT-48 to GIT-51, GIT-42 extended) -----------------

@contextlib.contextmanager
def panel_batch_snapshot_first():
    """a95d556's (Y1e's) batch check, panels.py:415: `self.snapshot()` before anything is saved,
    so an embedded repository a reader left stops the run before the panel's void (V-01)."""
    from codesmith import panels
    real = panels.Panels.reader_changes

    def old(self, candidate):
        if sys._getframe(1).f_code.co_name == 'dispatch_panel':
            return ['?'] if self.snapshot() != candidate or self.submodule_dirt() else []
        return real(self, candidate)
    with mock.patch.object(panels.Panels, 'reader_changes', old):
        yield


@contextlib.contextmanager
def panel_entry_restores_first():
    """a95d556's (Y1e's) panel entry, panels.py:255-258: an interrupted reader's change is put
    back before any void is saved, then the task ends; a refused restore stops first, and after
    the remedy `resume` collects the batch's saved outcomes (V-01)."""
    from codesmith import panels
    real = panels.Panels.panel

    def old(self, task_):
        st = self.st(task_['id'])
        if st.get('panel') and not st['panel'].get('void'):
            candidate = st['candidate']
            if self.snapshot() != candidate or self.submodule_dirt():
                self.restore_panel(task_, 'interrupted reader changed the candidate')
                return self.end(task_, 'failed', 'a reviewer or read-only check changed the '
                                'candidate during interruption')
        return real(self, task_)
    with mock.patch.object(panels.Panels, 'panel', old):
        yield


@contextlib.contextmanager
def settle_keeps_the_check_head():
    """a95d556's `settle`: no HEAD put back for a check found `running`, so it classifies and
    restores against the commit the check made (V-02). A partial restoration: only the HEAD
    put-back is removed; Y1f's departure 1 (`fail` on a submodule hit) is kept (Y1f ripple V-04)."""
    real = gitops.Git.put_head_back

    def kept(self, branch, commit, keep=None):
        if sys._getframe(1).f_code.co_name != 'settle_check':
            real(self, branch, commit, keep)
    with mock.patch.object(gitops.Git, 'put_head_back', kept):
        yield


@contextlib.contextmanager
def check_unguarded():
    """a95d556's standalone check: no refusal of dirt inside a submodule before it runs, so the
    owner's dirt is ruled the check's own change (V-03)."""
    real = engine.Engine.submodule_dirt

    def dirt(self):
        frame = sys._getframe(1)
        if frame.f_code.co_name == 'standalone_check' and 'results' not in frame.f_locals:
            return []
        return real(self)
    with mock.patch.object(engine.Engine, 'submodule_dirt', dirt):
        yield


@contextlib.contextmanager
def snapshot_unquoted():
    """a95d556's `Engine.snapshot`: the raw `{rel}` in its `mv` (V-04)."""
    import types
    with mock.patch.object(engine, 'shlex', types.SimpleNamespace(quote=lambda s: s)):
        yield


@contextlib.contextmanager
def stale_verdict_applied():
    """a95d556's `settle`: the latest attempt's verification.json is applied whatever tree and
    commands it records (V-05)."""
    with mock.patch.object(engine, '_verdict_of', lambda *a: True):
        yield


class Killed(Exception):
    pass


def local_repo(case, name):
    """A one-commit repository in the scratch directory, to clone from offline."""
    path = os.path.join(case.side, name)
    os.makedirs(path)
    Path(path, 'x').write_text('lib source\n')
    for args in (('init', '-q', '-b', 'main'), ('add', '-A'), ('commit', '-qm', 'lib')):
        case.sgit(path, *args)
    return path


class PanelVoid(Settled):
    """GIT-48: the panel's void is saved before anything is put back, at the batch and at entry."""
    HEADER = Invalidation.HEADER

    def run_with_kills(self, points, limit=10):
        """`start`, killed at each of `points` in turn (each its first occurrence after the last
        resume), then every printed stop followed until the run ends."""
        self.stops = []
        points = list(points)
        started = False
        while points:
            point = points.pop(0)

            def hook(where, point=point):
                if where == point:
                    raise Killed(where)
            self.cli.CRASH = hook
            with self.assertRaises(Killed):
                self.resume() if started else self.start()
            started = True
            self.cli.CRASH = None
        code = self.resume() if started else self.start()
        return self.follow_until_done(code, limit=limit, stash=True)

    # Two read-only checks: one alone runs in verify, not in the panel (`parallel_checks_of`).
    QUIET = '\n[[task]]\nid="c0"\ntype="check"\nverifies="make"\nread_only=true\nrun=["true"]\n'

    def clone_check(self):
        lib = local_repo(self, 'libsrc')
        return (self.listed(['test -f src/a']) + self.QUIET
                + '\n[[task]]\nid="c"\ntype="check"\nverifies="make"\nread_only=true\n'
                  f'restores=false\nrun=["git clone -q {lib} vend && true"]\n')

    def dirt_check(self):
        return (self.listed(['test -f src/a']) + self.QUIET
                + '\n[[task]]\nid="c"\ntype="check"\nverifies="make"\nread_only=true\n'
                  'restores=false\nrun=["echo dirty >> sub/f"]\n')

    def journey(self, variant, kills=()):
        self.workflow(self.clone_check() if variant == 'batch' else self.dirt_check())
        self.script(self.mover())
        return self.run_with_kills(kills)

    def assert_voided(self, code):
        self.assertNotEqual(self.status('make'), 'accepted',
                            "accepted a result earned while a reader changed the candidate")
        self.assertEqual(code, 2, self.output)
        self.assert_settled('failed')
        st = self.the_run().state['tasks']
        self.assertTrue(st['c'].get('demoted'), 'the writer was not demoted')
        self.assertGreaterEqual(st['make']['attempts_used'], 1, 'no attempt was used')

    def test_reader_that_leaves_an_embedded_repository(self):
        """git: a read-only panel check clones a repository into the work tree: the void is
        saved before the snapshot's stop; the printed move run literally, the batch's outcome is
        never collected, the check is demoted and the task ends `failed` within its attempts;
        also killed between the void and its restore (GIT-48, Y1e ripple V-01)"""
        for kills in ((), ('panel:void-recorded',)):
            with self.subTest(kills=kills):
                self.setUp()
                code = self.journey('batch', kills)
                self.assert_voided(code)
                if not kills:
                    self.assertIn("a reader changed the candidate of 'make', so the panel is void; "
                                  "the candidate is put back first: an embedded git repository is "
                                  "in the work tree at 'vend'", self.stops[0])

    def test_batch_catches_the_snapshot_first(self):
        """git: GIT-48's clone is accepted under a95d556's batch check (snapshot before void)"""
        with panel_batch_snapshot_first(), self.assertRaises(AssertionError) as ctx:
            self.assert_voided(self.journey('batch'))
        self.assertIn('accepted a result earned while a reader changed the candidate',
                      str(ctx.exception))

    def test_interrupted_reader_with_dirt_at_entry(self):
        """git: a read-only panel check writes inside sub and the runner is killed after the
        batch's outcomes are saved: `resume` saves the void before the restore's stop; the stash
        run literally, the outcomes are never collected, the check is demoted and the task ends
        `failed`; also killed between the void and its restore (GIT-48, Y1e ripple V-01)"""
        for kills in (('panel:outcomes-recorded',),
                      ('panel:outcomes-recorded', 'panel:void-recorded')):
            with self.subTest(kills=kills):
                self.setUp()
                code = self.journey('entry', kills)
                self.assert_voided(code)
                if len(kills) == 1:
                    self.assertIn("a reader changed the candidate of 'make', so the panel is "
                                  "void; the candidate is put back first: sub is a submodule with "
                                  "uncommitted changes inside it", self.stops[0])

    def test_entry_catches_the_restore_first(self):
        """git: GIT-48's interrupted dirt is accepted under a95d556's panel entry (restore first)"""
        with panel_entry_restores_first(), self.assertRaises(AssertionError) as ctx:
            self.assert_voided(self.journey('entry', ('panel:outcomes-recorded',)))
        self.assertIn('accepted a result earned while a reader changed the candidate',
                      str(ctx.exception))


class CheckHead(Settled):
    """GIT-49: a check that commits, then is paused or killed before its HEAD is put back."""

    def setUp(self):
        super().setUp()
        self.lib = local_repo(self, 'libsrc')
        Path(self.root, 'lib').write_text('the owner file\n')
        self.sgit(self.root, 'add', 'lib')
        self.sgit(self.root, 'commit', '-qm', 'an ordinary file lib')
        # GIT-45's registration, then a second command for a pause or a kill to come before.
        self.register = (f"git rm -q lib && git -c protocol.file.allow=always submodule add -q "
                         f"{self.lib} lib && git -c user.name=x -c user.email=x@x commit -qm reg")

    def journey_interrupted(self, how, second_kill=False):
        self.stops = []
        self.workflow(f'\n[[task]]\nid="c"\ntype="check"\nrun=["{self.register}", "true"]\n')
        self.script([])
        head = self.git_out('rev-parse', 'HEAD')
        seen = {'n': 0}

        def hook(where):
            if where == 'command:running':
                seen['n'] += 1
                if how == 'pause' and seen['n'] == 1:
                    self.the_run().request_pause()
                if how == 'kill' and seen['n'] == 2:
                    raise Killed(where)
        self.cli.CRASH = hook
        if how == 'kill':
            with self.assertRaises(Killed):
                self.start()
        else:
            self.assertEqual(self.start(), 2, self.output)
            self.assertIn("paused at the owner's request before the next command of 'c'",
                          self.output)
        self.assertNotEqual(self.git_out('rev-parse', 'HEAD'), head, 'the check did not commit')
        if second_kill:
            def again(where):
                if where == 'check:verdict-recorded':
                    raise Killed(where)
            self.cli.CRASH = again
            with self.assertRaises(Killed):
                self.resume()
        self.cli.CRASH = None
        code = self.follow_until_done(self.resume())
        self.assertEqual(code, 2, self.output)
        self.assert_settled('failed', 'c')
        self.assertEqual(self.git_out('rev-parse', 'HEAD'), head, "the check's commit was kept")
        self.assertNotIn('reg', self.git_out('log', '--format=%s').splitlines(),
                         "the check's commit is on the run branch")
        self.assertEqual(Path(self.root, 'lib').read_text(), 'the owner file\n')
        self.assertTrue(os.path.isdir(os.path.join(self.root, '.git', 'modules', 'lib')),
                        "the check's repository was lost")
        run = self.the_run()
        self.assertEqual(len(list(Path(run.path).rglob('attempt-*'))), 1, 'the check ran again')
        with open(os.path.join(run.task_dir('c'), 'attempt-1', 'verification.json')) as fh:
            self.assertEqual(json.load(fh)['result'], 'fail')
        self.assertFalse([r for r in gitops.Git(self.root).pins(run.name)
                          if r.endswith('/c/check-head')], 'the start commit is still pinned')
        self.assertIn("check 'c' failed (its result is recorded); the tree is put back before "
                      "anything else runs: an embedded git repository is in the work tree at "
                      "'lib'", self.stops[0])

    def test_check_paused_after_its_commit(self):
        """git: a standalone check commits a registration, then `runner pause` stops it before its
        next command: `resume` puts HEAD back to the pinned start commit, records `fail` before
        the restore; the printed move run literally, the check is `failed`, its commit on no
        branch, the owner's file back, the start pin deleted (GIT-49, Y1e ripple V-02)"""
        self.journey_interrupted('pause')

    def test_check_killed_after_its_commit(self):
        """git: as GIT-49's pause, with a kill during the check's second command; and again with
        a second kill between the recorded `fail` and the restore (GIT-49)"""
        for second in (False, True):
            with self.subTest(second_kill=second):
                self.setUp()
                self.journey_interrupted('kill', second)

    def test_paused_check_catches_the_kept_head(self):
        """git: GIT-49 repeats its stop under a95d556's settle, which keeps the check's commit as
        HEAD"""
        with settle_keeps_the_check_head(), self.assertRaises(AssertionError) as ctx:
            self.journey_interrupted('pause')
        # The registered `lib` cannot be put back by `mv`: the L8 strand.
        self.assertIn('the stop repeated after its printed remedy', str(ctx.exception))

    def test_killed_check_catches_the_kept_head(self):
        """git: GIT-49's kill variant repeats its stop under a95d556's settle, which keeps the
        check's commit as HEAD (Y1f ripple V-04: run, not inferred)"""
        with settle_keeps_the_check_head(), self.assertRaises(AssertionError) as ctx:
            self.journey_interrupted('kill')
        self.assertIn('the stop repeated after its printed remedy', str(ctx.exception))

    def test_registration_catches_the_rerun(self):
        """git: GIT-49 runs the check again under a95d556's settle with Y1f's departure 1 removed
        (an interrupted check with no verdict restored and rerun, whatever it registered): the
        rerun registers `lib` again and its index entry is left behind"""
        with settle_records_no_fail(), self.assertRaises(AssertionError) as ctx:
            self.journey_interrupted('pause')
        self.assertIn('the tree is not clean', str(ctx.exception))


class OwnerDirt(Settled):
    """GIT-50: the owner's dirt inside a submodule before a standalone check."""

    def journey_owner_dirt(self):
        self.stops = []
        self.workflow(task('acceptance_replay=false\n')
                      + '\n[[task]]\nid="look"\ntype="human"\nneeds=["make"]\n'
                      + '\n[[task]]\nid="c"\ntype="check"\nneeds=["look"]\nrun=["true"]\n')
        self.script([{'write': {'src/a': 'one\n'}, 'answer': done()}])
        self.assertEqual(self.start(), 255, self.output)
        Path(self.root, 'sub', 'f').write_text('the owner edit\n')
        self.assertEqual(self.runner('approve', 'latest', 'look', '-C', self.root), 0, self.output)
        code = self.follow_until_done(self.resume(), stash=True)
        self.assertNotEqual(self.status('c'), 'failed', "the owner's dirt was ruled the check's")
        self.assertEqual(code, 0, self.output)
        self.assertEqual(self.status('c'), 'accepted')
        self.assertEqual(len(list(Path(self.the_run().task_dir('c')).glob('attempt-*'))), 1)
        self.assertIn("check 'c' cannot run while a submodule has changes inside it: " + INSIDE
                      + ", then `runner resume`", self.stops[0])
        self.check_invariants()

    def test_owner_dirt_before_a_check_is_refused(self):
        """git: the owner edits inside sub while the run waits for a person: the standalone check
        refuses before it runs, naming the stash; run literally, `resume` runs the check, which
        is `accepted` with one attempt and no `fail` against it (GIT-50, Y1e ripple V-03)"""
        self.journey_owner_dirt()

    def test_owner_dirt_catches_the_unguarded_check(self):
        """git: GIT-50 rules the check `failed` under a95d556's unguarded entry"""
        with check_unguarded(), self.assertRaises(AssertionError) as ctx:
            self.journey_owner_dirt()
        self.assertIn("the owner's dirt was ruled the check's", str(ctx.exception))


class SpacedEmbedded(Settled):
    """GIT-42 extended: an embedded repository at a spaced path through the void recovery."""

    def journey_embedded(self):
        self.stops = []
        lib = local_repo(self, 'libsrc')
        self.workflow(task('acceptance_replay=false\n').replace(
            'gate=["test -f src/a"]', f'gate=["git clone -q {lib} \'third party\' && test -f src/a"]'))
        self.script([{'write': {'src/a': 'one\n'}, 'answer': done()},
                     {'write': {'src/a': 'two\n'}, 'answer': done()}])
        code = self.follow_until_done(self.start(), limit=6)
        self.assertEqual(code, 2, self.output)
        self.assert_settled('failed')
        self.assertIn("(`mv 'third party' <a directory outside the work tree>`)", self.stops[0])

    def test_embedded_repository_at_a_spaced_path(self):
        """git: a gate clones into `third party`: the void's recovery quotes the `mv`; run
        literally, the task ends `failed` within its attempts, settled (GIT-42, Y1e ripple V-04)"""
        self.journey_embedded()

    def test_embedded_spaced_path_catches_the_raw_mv(self):
        """git: GIT-42's embedded journey fails at "a printed remedy failed" under a95d556's raw
        `{rel}` in `Engine.snapshot`"""
        with snapshot_unquoted(), self.assertRaises(AssertionError) as ctx:
            self.journey_embedded()
        self.assertIn('a printed remedy failed', str(ctx.exception))


class StaleVerdict(SubmoduleCase):
    """GIT-51: a verification.json of another run of the same check."""

    def journey_stale(self):
        self.workflow('\n[[task]]\nid="c"\ntype="check"\nrun=["true"]\n'
                      + task('acceptance_replay=false\nneeds=["c"]\n'))
        self.script([{'write': {'src/a': 'one\n'}, 'answer': done()}])
        self.assertEqual(self.start(), 0, self.output)
        run = self.the_run()
        self.assertFalse([r for r in gitops.Git(self.root).pins(run.name)
                          if r.endswith('/c/check-head')], 'the start commit is still pinned')
        # An older runner reopened `c` on the newer tree, saved it `running` and was killed
        # before its attempt directory: attempt-1's pass (on the older tree) is the latest.
        run.state['tasks']['c'].update(status='running', check_base=self.git_out(
            'rev-parse', 'HEAD^{tree}'))
        run.state.update(status='stopped', expect=None)
        run.save()
        self.assertEqual(self.resume(), 0, self.output)
        self.assertEqual(self.status('c'), 'accepted')
        self.assertEqual(len(list(Path(self.the_run().task_dir('c')).glob('attempt-*'))), 2,
                         'a verdict of another tree was applied')

    def test_stale_verdict_is_not_applied(self):
        """git: a check `running` whose latest verification.json records another tree: it is no
        verdict of this run of the check; `resume` runs the check (GIT-51, Y1e ripple V-05)"""
        self.journey_stale()

    def test_stale_verdict_catches_the_unconditional_apply(self):
        """git: GIT-51 applies the older tree's pass under a95d556's settle"""
        with stale_verdict_applied(), self.assertRaises(AssertionError) as ctx:
            self.journey_stale()
        self.assertIn('a verdict of another tree was applied', str(ctx.exception))


# -- Y1g: two guards on the check-head pin (GIT-52, GIT-53; GIT-49 extended) -------------------

@contextlib.contextmanager
def replan_ignores_running_checks():
    """e11c555's `replan`: no refusal of a running check whose stop left HEAD away from its pin,
    and its `running` state copied with the pin kept (Y1f ripple V-01)."""
    from codesmith import replan
    with mock.patch.object(replan, 'running_checks', lambda run, tasks: []):
        yield


@contextlib.contextmanager
def retry_ignores_running_checks():
    """8ab75ba's `retry`: no refusal of a running check whose stop left HEAD away from its pin,
    and the check left `running` with its pin kept while the owner's tip is adopted (Y1g)."""
    with mock.patch.object(engine, 'running_checks', lambda run, tasks: []):
        yield


@contextlib.contextmanager
def settle_rules_dirt():
    """e11c555's `settle_check`: dirt inside a submodule seen at `resume` counted as the check's
    own change, so it records `fail` (Y1f ripple V-02)."""
    real = engine.Engine.submodule_hits

    def hits(self, target, after, embedded=(), dirt=()):
        if sys._getframe(1).f_code.co_name == 'settle_check':
            dirt = self.submodule_dirt()
        return real(self, target, after, embedded, dirt)
    with mock.patch.object(engine.Engine, 'submodule_hits', hits):
        yield


@contextlib.contextmanager
def settle_records_no_fail():
    """a95d556's `settle`: an interrupted check with no verdict is restored and runs again,
    whatever it changed of a submodule (Y1f's departure 1 removed)."""
    real = engine.Engine.submodule_hits

    def hits(self, *args, **kw):
        if sys._getframe(1).f_code.co_name == 'settle_check':
            return []
        return real(self, *args, **kw)
    with mock.patch.object(engine.Engine, 'submodule_hits', hits):
        yield


@contextlib.contextmanager
def settle_put_back_silent(printed=False):
    """e11c555's `settle_check` put-back: nothing printed, `their_head=None` in its
    `git-repaired` event (Y1f ripple V-03). `printed` keeps the line, to reach the event."""
    from codesmith import record
    real_say, real_event = engine.Engine.say, record.Run.event

    def say(self, text):
        if printed or not re.match(r"check '[^']+': put HEAD back to ", text):
            real_say(self, text)

    def event(self, name, **fields):
        if name == 'git-repaired' and fields.get('after', '').endswith('was interrupted'):
            fields['their_head'] = None
        return real_event(self, name, **fields)
    with mock.patch.object(engine.Engine, 'say', say), mock.patch.object(record.Run, 'event', event):
        yield


def moved_head_refusal(theirs, start, name, then):
    """`replan`'s or `retry`'s refusal while a running check stopped with HEAD at `theirs`: the
    resume, the pause before `then` (Y1g V-03) and the cherry-pick if the commit is the owner's
    (Y1g V-01)."""
    return (f"check 'c' was stopped with HEAD at {theirs[:7]}, not at its start commit "
            f"{start[:7]}: `runner pause {name}`, then `runner resume {name}` (it settles the "
            f"check, puts HEAD back to {start[:7]} and stops at its first safe point, before any "
            f"call), then {then}. If {theirs[:7]} is yours, not the check's, cherry-pick it after "
            f"that resume (`git cherry-pick {theirs[:7]}`). Nothing was changed")


class Interrupted(Settled):
    """A standalone check `c` stopped between its commands, by a pause or a kill."""

    def stop_check(self, how, tid='c'):
        """`start` until `c` is stopped before its second command: `runner pause` while its first
        runs, or a kill at its second `command:running`."""
        seen = {'n': 0}

        def hook(where):
            if where != 'command:running' or self.the_run().state['tasks'][tid]['status'] != 'running':
                return
            seen['n'] += 1
            if how == 'pause' and seen['n'] == 1:
                self.the_run().request_pause()
            if how == 'kill' and seen['n'] == 2:
                raise Killed(where)
        self.cli.CRASH = hook
        if how == 'kill':
            with self.assertRaises(Killed):
                self.start()
        else:
            self.assertEqual(self.start(), 2, self.output)
            self.assertIn(f"paused at the owner's request before the next command of '{tid}'",
                          self.output)
        self.cli.CRASH = None
        self.assertEqual(self.status(tid), 'running')

    def pin_of(self, tid='c'):
        run = self.the_run()
        return gitops.Git(self.root).pins(run.name).get(f'{gitops.REF_PREFIX}{run.name}/{tid}/check-head')

    def attempts(self, tid='c'):
        return sorted(Path(self.the_run().task_dir(tid)).glob('attempt-*'))

    def verdicts(self, tid='c'):
        return [json.loads((a / 'verification.json').read_text())['result']
                for a in self.attempts(tid) if (a / 'verification.json').exists()]

    def is_ancestor(self, sha):
        return subprocess.run(['git', 'merge-base', '--is-ancestor', sha, 'HEAD'],
                              cwd=self.root).returncode == 0

    def events(self):
        with open(os.path.join(self.the_run().path, 'events.jsonl'), encoding='utf-8') as fh:
            return [json.loads(line) for line in fh if line.strip()]


class ReplanCheck(Interrupted):
    """GIT-52: `replan` while a standalone check is paused between its commands."""

    CHECK = '\n[[task]]\nid="c"\ntype="check"\nrun=["true", "true"]\n'

    def replan(self, *extra):
        return self.runner('replan', self.the_run().name, *extra, '-C', self.root)

    def journey_owner_commit(self, kill_at_unpin=False):
        self.workflow(self.CHECK)
        self.script([])
        self.stop_check('pause')
        self.assertEqual(self.pin_of(), self.git_out('rev-parse', 'HEAD'))
        Path(self.wf_path).write_text(Path(self.wf_path).read_text() + '# the owner edit\n')
        self.sgit(self.root, 'commit', '-qam', 'the owner edits the workflow')
        owner = self.git_out('rev-parse', 'HEAD')
        if kill_at_unpin:
            def hook(where):
                if where == 'replan:check-unpinned':
                    raise Killed(where)
            self.cli.CRASH = hook
            with self.assertRaises(Killed):
                self.replan()
            self.cli.CRASH = None
            self.assertIsNone(self.pin_of(), 'the kill came before the unpin')
            self.assertEqual(self.status('c'), 'running')
        else:
            self.assertEqual(self.replan(), 0, self.output)
        code = self.resume()
        self.assertTrue(self.is_ancestor(owner), "the owner's commit left the run branch")
        self.assertIn('# the owner edit', Path(self.wf_path).read_text())
        self.assertEqual(code, 0, self.output)
        self.assert_settled('accepted', 'c')
        self.assertEqual(len(self.attempts()), 2, 'the check did not run again from pending')
        self.assertIsNone(self.pin_of(), 'the start commit is still pinned')

    def test_owner_commit_kept_across_replan(self):
        """git: a standalone check paused between its commands; the owner commits a workflow
        edit and replans: `replan` installs the check `pending` and unpins it, and `resume` runs
        it again with the owner's commit on the branch; also killed between the unpin and the
        installed state, `resume` replays the replan to the same end (GIT-52, Y1f ripple V-01)"""
        for kill in (False, True):
            with self.subTest(kill_at_unpin=kill):
                self.setUp()
                self.journey_owner_commit(kill)

    def test_owner_commit_catches_the_unguarded_replan(self):
        """git: GIT-52 drops the owner's commit under e11c555's replan (the check stays
        `running`, pinned, and settle puts HEAD back to the pin)"""
        with replan_ignores_running_checks(), self.assertRaises(AssertionError) as ctx:
            self.journey_owner_commit()
        self.assertIn("the owner's commit left the run branch", str(ctx.exception))

    def journey_reopen(self):
        self.workflow(task('acceptance_replay=false\n') + self.CHECK)
        self.script({'make': [{'write': {'src/a': 'one\n'}, 'answer': done()}] * 2})
        self.stop_check('pause')
        self.assertEqual(self.replan('--reopen', 'make'), 0, self.output)
        revert = self.git_out('rev-parse', 'HEAD')
        self.assertFalse(os.path.exists(os.path.join(self.root, 'src', 'a')))
        code = self.resume()
        self.assertTrue(self.is_ancestor(revert), 'the revert of the reopened work left the branch')
        self.assertEqual(code, 0, self.output)
        self.assert_settled('accepted', 'make')
        self.assertEqual(self.status('c'), 'accepted')
        self.assertIsNone(self.pin_of(), 'the start commit is still pinned')

    def test_reopen_kept_across_replan(self):
        """git: as GIT-52 with `replan --reopen make` (the check does not need it): the revert
        stays on the branch, `make` and the check run again, `done` (GIT-52)"""
        self.journey_reopen()

    def test_reopen_catches_the_unguarded_replan(self):
        """git: GIT-52's revert leaves the branch under e11c555's replan"""
        with replan_ignores_running_checks(), self.assertRaises(AssertionError) as ctx:
            self.journey_reopen()
        self.assertIn('the revert of the reopened work left the branch', str(ctx.exception))

    def journey_check_commit(self):
        once = os.path.join(self.side, 'once')
        commit = (f"test -e {once} || (touch {once} && echo by-check > f && git add f && "
                  "git -c user.name=x -c user.email=x@x commit -qm by-check)")
        self.workflow(f'\n[[task]]\nid="c"\ntype="check"\nrun=["{commit}", "true"]\n')
        self.script([])
        start = self.git_out('rev-parse', 'HEAD')
        self.stop_check('pause')
        theirs = self.git_out('rev-parse', 'HEAD')
        self.assertNotEqual(theirs, start, 'the check did not commit')
        name = self.the_run().name
        code = self.replan()
        self.assertNotEqual(code, 0, "replan accepted the check's own commit as the run's "
                            "history: " + self.output)
        self.assertIn(moved_head_refusal(theirs, start, name, 'replan'), self.output)
        self.assertEqual(self.resume(), 0, self.output)
        self.assertIn(f"check 'c': put HEAD back to {start[:7]}; it was at {theirs[:7]}",
                      self.output)
        self.assertEqual(self.git_out('rev-parse', 'HEAD'), start)
        self.assert_settled('accepted', 'c')
        repaired = [e for e in self.events() if e['event'] == 'git-repaired']
        self.assertEqual([e['their_head'] for e in repaired], [theirs])
        self.assertEqual(self.replan(), 0, self.output)
        self.assertFalse(self.is_ancestor(theirs), "the check's commit is on the run branch")

    def test_check_commit_refuses_replan(self):
        """git: the check's first command commits before the pause: `replan` refuses, naming the
        start commit and `runner resume`; `resume` puts HEAD back, prints the commit it was at
        and records it as `their_head`; the check reruns, then `replan` is accepted (GIT-52)"""
        self.journey_check_commit()

    def test_check_commit_catches_the_unguarded_replan(self):
        """git: GIT-52's check commit is taken as the run's history under e11c555's replan"""
        with replan_ignores_running_checks(), self.assertRaises(AssertionError) as ctx:
            self.journey_check_commit()
        self.assertIn("replan accepted the check's own commit as the run's history",
                      str(ctx.exception))

    def journey_retry_other(self):
        self.workflow(task() + self.CHECK)
        self.script({'make': [{'answer': done()}] * 2 + [{'write': {'src/a': 'one\n'},
                                                           'answer': done()}]})
        self.stop_check('pause')
        self.assertEqual(self.status('make'), 'failed', self.output)
        self.write('notes', 'the owner note\n')
        self.sgit(self.root, 'add', 'notes')
        self.sgit(self.root, 'commit', '-qm', 'the owner commits a note')
        owner = self.git_out('rev-parse', 'HEAD')
        self.assertEqual(self.runner('retry', self.the_run().name, 'make', '-C', self.root), 0,
                         self.output)
        installed, pin = self.status('c'), self.pin_of()
        code = self.resume()
        self.assertTrue(self.is_ancestor(owner), "the owner's commit left the run branch")
        self.assertEqual(installed, 'pending')
        self.assertIsNone(pin, 'the start commit was still pinned after the retry')
        self.assertEqual(code, 0, self.output)
        self.assert_settled('accepted', 'make')
        self.assertEqual(self.status('c'), 'accepted')
        self.assertEqual(len(self.attempts()), 2, 'the check did not run again from pending')

    def test_owner_commit_kept_across_retry(self):
        """git: a standalone check paused between its commands while `make` is failed; the owner
        commits and runs `retry make`: the retry adopts the tip, installs the check `pending`
        and unpins it, and `resume` keeps the owner's commit on the branch (GIT-52)"""
        self.journey_retry_other()

    def test_owner_commit_catches_the_unguarded_retry(self):
        """git: GIT-52's retry drops the owner's commit under 8ab75ba's retry (the check stays
        `running`, pinned, and settle puts HEAD back to the pin)"""
        with retry_ignores_running_checks(), self.assertRaises(AssertionError) as ctx:
            self.journey_retry_other()
        self.assertIn("the owner's commit left the run branch", str(ctx.exception))

    def test_check_commit_refuses_retry(self):
        """git: GIT-52's check commit before the pause, with `make` failed: `retry make` refuses,
        naming the start commit and `runner resume` (GIT-52)"""
        once = os.path.join(self.side, 'once')
        commit = (f"test -e {once} || (touch {once} && echo by-check > f && git add f && "
                  "git -c user.name=x -c user.email=x@x commit -qm by-check)")
        self.workflow(task() + f'\n[[task]]\nid="c"\ntype="check"\nrun=["{commit}", "true"]\n')
        self.script({'make': [{'answer': done()}] * 2})
        start = self.git_out('rev-parse', 'HEAD')
        self.stop_check('pause')
        theirs = self.git_out('rev-parse', 'HEAD')
        name = self.the_run().name
        self.assertEqual(self.runner('retry', name, 'make', '-C', self.root), 2, self.output)
        self.assertIn(moved_head_refusal(theirs, start, name, 'retry'), self.output)
        self.assertEqual(self.status('make'), 'failed')
        self.assertEqual(self.status('c'), 'running')

    def test_check_commit_catches_the_silent_put_back(self):
        """git: GIT-52's put-back names no commit under e11c555's settle (Y1f ripple V-03)"""
        with settle_put_back_silent(), self.assertRaises(AssertionError) as ctx:
            self.journey_check_commit()
        self.assertIn("check 'c': put HEAD back to", str(ctx.exception))
        self.setUp()
        with settle_put_back_silent(printed=True), self.assertRaises(AssertionError) as ctx:
            self.journey_check_commit()
        self.assertIn('[None]', str(ctx.exception))


class SettleDirt(Interrupted):
    """GIT-53: dirt inside a submodule at `resume` of a check stopped between its commands."""

    def journey_dirt(self, how):
        self.stops = []
        self.workflow('\n[[task]]\nid="c"\ntype="check"\nrun=["true", "true"]\n')
        self.script([])
        self.stop_check(how)
        Path(self.root, 'sub', 'f').write_text('the owner edit\n')
        code = self.follow_until_done(self.resume(), stash=True)
        self.assertNotIn('fail', self.verdicts(), "the owner's dirt was ruled the check's")
        self.assertEqual(code, 0, self.output)
        self.assert_settled('accepted', 'c')
        self.assertEqual(len(self.attempts()), 2)
        self.assertIn("check 'c' cannot run while a submodule has changes inside it: " + INSIDE
                      + ", then `runner resume`", self.stops[0])

    def test_owner_dirt_at_settle_is_refused(self):
        """git: the owner writes inside sub while a check is paused between its commands (or
        after a kill there): `resume` records no `fail`, restores the check to `pending` and its
        guard names the stash; run literally, the check reruns and is `accepted` (GIT-53, Y1f
        ripple V-02)"""
        for how in ('pause', 'kill'):
            with self.subTest(how=how):
                self.setUp()
                self.journey_dirt(how)

    def test_owner_dirt_catches_dirt_as_a_criterion(self):
        """git: GIT-53 records the owner's dirt as the check's `fail` under e11c555's settle"""
        for how in ('pause', 'kill'):
            with self.subTest(how=how):
                self.setUp()
                with settle_rules_dirt(), self.assertRaises(AssertionError) as ctx:
                    self.journey_dirt(how)
                self.assertIn("the owner's dirt was ruled the check's", str(ctx.exception))

    def journey_pointer(self):
        self.stops = []
        self.workflow('\n[[task]]\nid="c"\ntype="check"\n'
                      f'run=["git -C sub checkout -q --detach {self.c1}", "true"]\n')
        self.script([])
        self.stop_check('pause')
        code = self.follow_until_done(self.resume())
        self.assertEqual(len(self.attempts()), 1, 'the check ran again')
        self.assertEqual(code, 2, self.output)
        self.assert_settled('failed', 'c')
        self.assertEqual(self.verdicts(), ['fail'])
        self.assertEqual(self.checked_out(), self.c2)
        self.assertIn("check 'c' failed (its result is recorded); the tree is put back before "
                      "anything else runs: ", self.stops[0])
        self.assertIn(f'git -C sub checkout -q --detach {self.c2}', self.stops[0])

    def test_pointer_moved_at_settle_records_fail(self):
        """git: a check moves sub's pointer, then is paused before its next command: `resume`
        records `fail` before the restore (Y1f's departure 1, kept); run literally, the check is
        `failed` with one attempt (GIT-53)"""
        self.journey_pointer()

    def test_pointer_catches_the_rerun(self):
        """git: GIT-53's pointer check runs again under a95d556's settle (departure 1 removed)"""
        with settle_records_no_fail(), self.assertRaises(AssertionError) as ctx:
            self.journey_pointer()
        self.assertIn('the check ran again', str(ctx.exception))


# -- the panel's put-back and a check's moved HEAD (Y1d V-02, Y1g V-04) --------------------------

@contextlib.contextmanager
def keep_only_at_verify():
    """f41027a's `keep_lease`: the candidate's pointers are staged again only at step `verify`,
    so a reset of HEAD at step `panel` leaves the index at HEAD's pointer (Y1d V-02)."""
    real = engine.Engine.listed_pointers

    def listed(self, task_, tree):
        if sys._getframe(1).f_code.co_name == '<lambda>' and \
                self.st(task_['id']).get('step') != 'verify':
            return {}
        return real(self, task_, tree)
    with mock.patch.object(engine.Engine, 'listed_pointers', listed):
        yield


@contextlib.contextmanager
def check_put_back_unnamed():
    """f41027a's `standalone_check`: its HEAD put back prints nothing and records
    `their_head=None` (Y1g V-04)."""
    from codesmith import record
    real_say, real_event = engine.Engine.say, record.Run.event

    def say(self, text):
        if not re.match(r"check '[^']+': put HEAD back to ", text):
            real_say(self, text)

    def event(self, name, **fields):
        if name == 'git-repaired' and re.fullmatch(r"check '[^']+'", fields.get('after', '')):
            fields['their_head'] = None
        return real_event(self, name, **fields)
    with mock.patch.object(engine.Engine, 'say', say), \
            mock.patch.object(record.Run, 'event', event):
        yield


class PanelRestage(Settled):
    """GIT-54: HEAD moved at the panel step and the runner killed; `resume` resets the lease."""

    COMMIT = Remedies.COMMIT

    def journey(self):
        """A listed move; two read-only panel checks run one at a time: `c0` commits (HEAD moves)
        and the runner is killed once its answer is saved; `c` runs `git submodule update` and
        records the pointer it found staged."""
        self.stops = []
        seen = os.path.join(self.side, 'seen.log')
        update = f'git rev-parse :sub >> {seen} && git submodule update --init sub && test -f src/a'
        checks = ''.join(f'\n[[task]]\nid="{cid}"\ntype="check"\nverifies="make"\nread_only=true\n'
                         f'run=["{run}"]\n' for cid, run in (('c0', self.COMMIT), ('c', update)))
        self.workflow(self.listed(['test -f src/a']) + checks, defaults='max_parallel = 1')
        self.script(self.mover())

        def hook(where):
            if where == 'panel:batch-recorded':
                raise Killed(where)
        self.cli.CRASH = hook
        with self.assertRaises(Killed):
            self.start()
        self.cli.CRASH = None
        lease = self.the_run().state['tasks']['make']['lease']
        self.assertNotEqual(self.git_out('rev-parse', 'HEAD'), lease['commit'], 'HEAD never moved')
        code = self.resume()
        self.assertIn('make: put back what before step panel changed in git', self.output)
        code = self.follow_until_done(code, limit=6, stash=True)
        self.assertEqual(self.stops, [], "the panel check's verdict was not a pass on the staged "
                         'pointer: ' + '\n'.join(self.stops))
        with open(seen) as fh:
            self.assertEqual(fh.read().split(), [self.c1])
        self.assertEqual(self.status('c'), 'accepted')
        self.assertEqual(code, 0, self.output)
        self.assert_settled('accepted')
        self.assertEqual(self.pointer(), self.c1)
        self.assertEqual(self.checked_out(), self.c1)

    def test_kill_at_the_panel_restages_the_candidate_pointer(self):
        """git: a read-only panel check moves HEAD and the runner is killed at the panel step:
        `resume` puts HEAD back with the candidate's pointer staged again, so the next panel
        check's `git submodule update` checks it out and passes; the run ends done (GIT-54,
        Y1d V-02)"""
        self.journey()

    def test_kill_at_the_panel_catches_the_verify_only_restage(self):
        """git: GIT-54 fails at "the panel check's verdict was not a pass on the staged pointer"
        under f41027a's `keep_lease`, which stages the pointers again only at step verify"""
        with keep_only_at_verify(), self.assertRaises(AssertionError) as ctx:
            self.journey()
        self.assertIn("the panel check's verdict was not a pass on the staged pointer",
                      str(ctx.exception))


class CheckHeadNamed(Interrupted):
    """GIT-55: a standalone check that commits and completes."""

    def journey(self):
        self.workflow(f'\n[[task]]\nid="c"\ntype="check"\nrun=["{Remedies.COMMIT}"]\n')
        self.script([])
        start = self.git_out('rev-parse', 'HEAD')
        self.assertEqual(self.start(), 0, self.output)
        self.assert_settled('accepted', 'c')
        self.assertEqual(self.git_out('rev-parse', 'HEAD'), start)
        repaired = [e for e in self.events() if e['event'] == 'git-repaired']
        self.assertEqual([e['after'] for e in repaired], ["check 'c'"])
        theirs = repaired[0]['their_head']
        self.assertTrue(theirs, "the check's commit is recorded nowhere")
        self.assertIn(f"check 'c': put HEAD back to {start[:7]}; it was at {theirs[:7]}",
                      self.output)
        self.assertEqual(self.git_out('rev-parse', theirs + '^'), start)    # reachable by its sha
        self.assertFalse(self.is_ancestor(theirs), "the check's commit is on the run branch")

    def test_completed_check_commit_is_named(self):
        """git: a standalone check commits and passes: HEAD is put back to its start commit, the
        `git-repaired` event records the check's commit as `their_head` and the runner prints
        it; the commit is reachable by that sha and on no branch (GIT-55, Y1g V-04)"""
        self.journey()

    def test_completed_check_catches_the_unnamed_put_back(self):
        """git: GIT-55 fails at "the check's commit is recorded nowhere" under f41027a's
        standalone check (`their_head=None`, nothing printed)"""
        with check_put_back_unnamed(), self.assertRaises(AssertionError) as ctx:
            self.journey()
        self.assertIn("the check's commit is recorded nowhere", str(ctx.exception))


# -- printed remedies that are followed (Y1-hb a, Y1g V-01/V-03, Y1e V-08a, C14's restore) ------

class RetryAdvice(Settled):
    """GIT-56: `retry` refused for dirt inside a submodule names what puts it back."""

    def test_retry_refused_for_dirt_inside_names_the_stash(self):
        """git: a failed task; the owner writes inside sub; `retry` is refused naming the
        submodule's stash, not only "commit them"; run literally, the same retry is taken
        (GIT-56, Y1-hb a)"""
        self.stops = []
        self.workflow(task().replace('gate=["test -f src/a"]\nmax_attempts=2',
                                     'gate=["test -f src/never"]\nmax_attempts=1'))
        self.script([{'write': {'src/a': 'one\n'}, 'answer': done()}])
        self.assertEqual(self.start(), 2, self.output)
        self.assertEqual(self.status('make'), 'failed')
        Path(self.root, 'sub', 'f').write_text('the owner edit\n')
        name = self.the_run().name
        self.assertEqual(self.runner('retry', name, 'make', '-C', self.root), 2, self.output)
        self.assertIn("'make' cannot be retried as the run stands: the work tree has uncommitted "
                      "changes ( M sub)", self.output)
        self.assertIn(INSIDE + ', then run this retry again', self.output,
                      'the retry refusal names no submodule advice')
        self.follow(stash=True)
        self.assertEqual(self.runner('retry', name, 'make', '-C', self.root), 0, self.output)


class BuildOutputInside(Settled):
    """GIT-58: a gate that writes build output inside a submodule, and the advice it is sent."""

    EXCLUDE = re.compile(r"\(`git -C <path> (rev-parse --git-path info/exclude)` prints where")

    def test_exclude_advice_needs_no_commit_and_ends_done(self):
        """git: a gate writes `build.out` inside sub, so its result is void; the author is told
        to list it in the submodule's own .git/info/exclude (no commit), not its .gitignore;
        followed, with the printed stash, the next attempt passes and the run ends done with
        sub's pointer unmoved (GIT-58, Y1e V-08a)"""
        self.stops = []
        self.workflow(self.listed(['echo built > sub/build.out && test -f src/a']))
        self.script(self.mover(target='HEAD'))
        code = self.start()
        feedback = json.dumps(self.the_run().state['tasks']['make'].get('feedback'))
        self.assertNotIn('belong in paths its .gitignore ignores', feedback)
        found = self.EXCLUDE.search(' '.join(feedback.split()))
        self.assertTrue(found, 'the advice names no exclude file: ' + feedback)
        exclude = self.sgit(os.path.join(self.root, 'sub'), *found.group(1).split())
        with open(os.path.join(self.root, 'sub', exclude), 'a') as fh:
            fh.write('build.out\n')
        code = self.follow_until_done(code, limit=6, stash=True)
        self.assertEqual(code, 0, self.output)
        self.assert_settled('accepted')
        self.assertEqual(self.pointer(), self.c2, "sub's pointer moved")


class MovedHeadRemedy(Interrupted):
    """GIT-57: the moved-HEAD refusal's remedy, followed as printed."""

    def test_moved_head_remedy_pauses_before_any_call_and_cherry_picks(self):
        """git: a check commits, then is paused before its next command; a producer needs it:
        `replan` refuses naming the commit as possibly the owner's and `runner pause`, then
        `runner resume`, then replan; followed through the CLI (the pause kept on the stopped
        run, the printed cherry-pick after the resume), the resume settles the check and stops
        before any agent call, replan is accepted with no call paid, and the run ends done
        with the picked commit on the branch (GIT-57, Y1g V-01, V-03)"""
        once = os.path.join(self.side, 'once')
        # A workflow edit, as an owner's commit before a replan would be.
        commit = (f"test -e {once} || (touch {once} && echo '# picked' >> wf.toml && git add "
                  "wf.toml && git -c user.name=x -c user.email=x@x commit -qm picked)")
        self.workflow(f'\n[[task]]\nid="c"\ntype="check"\nrun=["{commit}", "true"]\n'
                      + task('needs=["c"]\n'))
        self.script([{'write': {'src/a': 'one\n'}, 'answer': done()}])
        start = self.git_out('rev-parse', 'HEAD')
        self.stop_check('pause')
        theirs = self.git_out('rev-parse', 'HEAD')
        name = self.the_run().name
        self.assertEqual(self.runner('replan', name, '-C', self.root), 2, self.output)
        self.assertIn(moved_head_refusal(theirs, start, name, 'replan'), self.output)
        # The printed sequence, each step a command: pause (no runner), resume, replan.
        self.assertEqual(self.runner('pause', name, '-C', self.root), 0, self.output)
        paused = self.output
        code = self.resume()
        self.assertEqual(self.calls(), 0, 'the resume paid for the rest of the workflow')
        self.assertIn(f'The pause is kept: the next `runner resume {name}` settles', paused)
        self.assertEqual(code, 2, self.output)
        self.assertIn(f"check 'c': put HEAD back to {start[:7]}; it was at {theirs[:7]}",
                      self.output)
        self.assertIn("paused at the owner's request", self.output)
        self.assertIsNone(self.the_run().state.get('active_producer'))
        picked = re.search(r'\(`(git cherry-pick [0-9a-f]{7})`\)',
                           moved_head_refusal(theirs, start, name, 'replan')).group(1)
        subprocess.run(picked, shell=True, cwd=self.root, check=True, capture_output=True)
        self.assertEqual(self.runner('replan', name, '-C', self.root), 0, self.output)
        self.assertEqual(self.calls(), 0, 'a call was paid between the resume and the replan')
        self.assertEqual(self.resume(), 0, self.output)
        self.assertIn('# picked', self.git_out('show', 'HEAD:wf.toml'))
        self.assert_settled('accepted')

    def journey_approve_variant(self):
        """[h human, make needs h, c check]: c commits and is paused; the owner approves h, so
        `make` is ready before c when the resume settles it."""
        once = os.path.join(self.side, 'once')
        commit = (f"test -e {once} || (touch {once} && echo '# picked' >> wf.toml && git add "
                  "wf.toml && git -c user.name=x -c user.email=x@x commit -qm picked)")
        self.workflow('\n[[task]]\nid="h"\ntype="human"\n' + task('needs=["h"]\n')
                      + f'\n[[task]]\nid="c"\ntype="check"\nrun=["{commit}", "true"]\n')
        self.script([{'write': {'src/a': 'one\n'}, 'answer': done()}])
        start = self.git_out('rev-parse', 'HEAD')
        self.stop_check('pause')
        theirs = self.git_out('rev-parse', 'HEAD')
        name = self.the_run().name
        self.assertEqual(self.runner('approve', name, 'h', '-C', self.root), 0, self.output)
        self.assertEqual(self.runner('replan', name, '-C', self.root), 2, self.output)
        self.assertIn(moved_head_refusal(theirs, start, name, 'replan'), self.output)
        self.assertEqual(self.runner('pause', name, '-C', self.root), 0, self.output)
        self.assertEqual(self.resume(), 2, self.output)
        self.assertIn(f"check 'c': put HEAD back to {start[:7]}; it was at {theirs[:7]}",
                      self.output)
        self.assertIsNone(self.the_run().state.get('active_producer'),
                          "the pause stopped inside a producer's transaction: " + self.output)
        self.assertEqual(self.calls(), 0, 'the resume paid for the rest of the workflow')
        self.assertIn("paused at the owner's request before 'make' starts", self.output)
        subprocess.run(f'git cherry-pick {theirs[:7]}', shell=True, cwd=self.root, check=True,
                       capture_output=True)
        self.assertEqual(self.runner('replan', name, '-C', self.root), 0, self.output)
        self.assertEqual(self.calls(), 0, 'a call was paid between the resume and the replan')
        self.assertEqual(self.resume(), 0, self.output)
        self.assertIn('# picked', self.git_out('show', 'HEAD:wf.toml'))
        self.assert_settled('accepted')

    def test_moved_head_remedy_after_an_approval_stops_before_the_producer(self):
        """git: GIT-57 with [h human, make needs h, c check]: the owner approves h while c is
        paused, so `make` is ready before c; `runner pause` then `runner resume` puts HEAD back
        and stops before 'make' starts: no transaction holds the tree, no call is paid, replan
        is accepted and the run ends done (GIT-57, Y1g V-03)"""
        self.journey_approve_variant()

    def test_approve_variant_catches_the_pause_inside_the_transaction(self):
        """git: GIT-57's approve variant fails at "the pause stopped inside a producer's
        transaction" under 9497b59's loop (no pause point before a producer starts)"""
        real = engine.Engine.pause_point

        def pause_point(eng, what):
            if what.endswith(' starts'):
                return None
            return real(eng, what)
        with mock.patch.object(engine.Engine, 'pause_point', pause_point), \
                self.assertRaises(AssertionError) as ctx:
            self.journey_approve_variant()
        self.assertIn("the pause stopped inside a producer's transaction", str(ctx.exception))

    def test_moved_head_remedy_catches_the_cleared_pause(self):
        """git: GIT-57 fails at "the resume paid for the rest of the workflow" under f41027a's
        `cmd_pause`, which clears the request on a run no runner works on"""
        def pause_cleared(args):
            run = self.the_run()
            run.clear_pause()
            print(f'run {run.name} is {run.state["status"]}; no runner is working on it, '
                  'nothing to pause', file=sys.stderr)
            return 0
        with mock.patch.object(self.cli, 'cmd_pause', pause_cleared), \
                self.assertRaises(AssertionError) as ctx:
            self.test_moved_head_remedy_pauses_before_any_call_and_cherry_picks()
        self.assertIn('the resume paid for the rest of the workflow', str(ctx.exception))
