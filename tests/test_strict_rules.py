"""The strict-only rules (C15, INV-01 to INV-03). Each rule has a table of states: the ones it must
condemn, built directly or reached by restoring the behaviour it guards against (named by the
commit restored), and valid ones it must leave alone. Every rule in `invariants.RULES` fires on
at least one row. No model call; the agents are the scripted ones."""
import contextlib
import inspect
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from helpers import EngineCase
from codesmith import findings, invariants, record, replan
import test_review_policy as policy


@contextlib.contextmanager
def replan_drops_the_rulings_file():
    """Restores 35cff76 (before package S): `replan.prepare` did not look at queued standing
    rulings, so a revised workflow could drop `rulings_file` while an entry that names no file
    of its own was queued for it, and the finish then had nowhere to write it (ledger: replan
    drops rulings_file with entries queued; RUN-76). The rest of `prepare` is today's."""
    source = inspect.getsource(replan.prepare)
    start = source.index('    # A queued standing ruling is written to the file it was bound to')
    end = source.index('    problems = run.record_problems()')
    scope = dict(vars(replan))
    exec(compile(source[:start] + source[end:], replan.__file__, 'exec'), scope)
    with mock.patch.object(replan, 'prepare', scope['prepare']):
        yield


def entry(**extra):
    return {'task': 'make', 'finding_title': 'Fix this', 'location_glob': 'src/a:*',
            'decision': 'advisory', 'note': 'Owner accepts the narrower scope',
            'ruled_at': '2026-10-08T12:00:00Z', 'by': 'owner', 'run': 'r1',
            'finding': 'make/PE-1', **extra}


def git(cwd, *args):
    subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True)


def rulings(*entries):
    return '\n'.join(findings.ruling_toml(findings.exported_entry(e)) for e in entries)


def a_run(directory, entries, defaults, tree=None, tip=None, missing_at_open=()):
    """A record holding what the rules read: the state, the frozen workflow, `run.json`, and a
    repository whose rulings.toml is `tip` in the last commit and `tree` in the work tree (None:
    no file). `missing_at_open` names the findings the command found already missing."""
    top, path = os.path.join(directory, 'repo'), os.path.join(directory, 'run')
    os.makedirs(top)
    os.makedirs(path)
    git(top, 'init', '-q', '-b', 'main')
    git(top, 'config', 'user.email', 'test@example.invalid')
    git(top, 'config', 'user.name', 'Test')
    target = Path(top, 'rulings.toml')
    Path(top, 'README.md').write_text('scratch\n')
    if tip is not None:
        target.write_text(tip)
    git(top, 'add', '-A')
    git(top, 'commit', '-q', '-m', 'start')
    if tree is None:
        target.unlink(missing_ok=True)
    else:
        target.write_text(tree)
    with open(os.path.join(path, 'workflow.expanded.json'), 'w', encoding='utf-8') as fh:
        json.dump({'defaults': defaults}, fh)
    with open(os.path.join(path, 'run.json'), 'w', encoding='utf-8') as fh:
        json.dump({'git_toplevel': top}, fh)
    run = record.Run(path, {'standing_pending': entries, 'status': 'stopped'})
    invariants.LOST_AT_OPEN[path] = {invariants._key(e) for e in entries
                                     if e.get('finding') in missing_at_open}
    return run


KEPT = 'written-ruling-kept'
W = entry(written=True, file='rulings.toml')
# (rule, row, entries, the frozen [defaults], fires, the repository: tree / tip / missing_at_open)
STATES = [
    ('queued-entry-has-file', 'an entry with no file, a workflow with no rulings_file',
     [entry()], {}, True, {}),
    ('queued-entry-has-file', 'a written entry with no file, a workflow with no rulings_file',
     [entry(written=True)], {}, True, {}),
    ('queued-entry-has-file', 'one bound entry beside one with no file, no rulings_file',
     [entry(file='rulings.toml'), entry(finding='make/PE-2')], {}, True, {}),
    ('queued-entry-has-file', 'an entry bound to its file, a workflow with no rulings_file',
     [entry(file='rulings.toml')], {}, False, {}),
    ('queued-entry-has-file', 'an entry with no file, the workflow names rulings_file',
     [entry()], {'rulings_file': 'rulings.toml'}, False, {}),
    ('queued-entry-has-file', 'no entry queued', [], {}, False, {}),
    (KEPT, 'written and held at the open, the file gone at the end (an export reverted)',
     [W], {}, True, {}),
    (KEPT, 'written and held at the open, the file without the entry and not in the commit',
     [W], {}, True, {'tree': "# the owner's rulings\n"}),
    (KEPT, 'written with no file: the frozen rulings_file lacks it in tree and commit',
     [entry(written=True)], {'rulings_file': 'rulings.toml'}, True, {}),
    (KEPT, 'written and in its file in the work tree', [W], {}, False, {'tree': rulings(W)}),
    (KEPT, 'written and in the last commit only', [W], {}, False, {'tip': rulings(W)}),
    (KEPT, 'written, already missing when the command opened the run (a person removed it)',
     [W], {}, False, {'missing_at_open': ('make/PE-1',)}),
    (KEPT, 'queued, not written, no file in the tree', [entry(file='rulings.toml')], {}, False,
     {}),
]


def fired(name, run, before=None):
    """What rule `name` alone finds in `run`."""
    fn = next(fn for rule, _at, fn in invariants.RULES if rule == name)
    return fn(run, before)


class Rules(unittest.TestCase):
    def tearDown(self):
        invariants.LOST_AT_OPEN.clear()

    def test_states(self):
        """inv: each strict-only rule condemns the states built for it, naming itself in the
        Violation, and leaves the valid ones alone (INV-01, INV-03)"""
        for name, row, entries, defaults, fires, repo in STATES:
            with self.subTest(rule=name, row=row), tempfile.TemporaryDirectory() as d:
                run = a_run(d, entries, defaults, **repo)
                at = next(at for rule, at, _fn in invariants.RULES if rule == name)
                if fires:
                    with self.assertRaisesRegex(invariants.Violation, '^' + name + ': '):
                        invariants.strict_check(run, at)
                else:
                    self.assertEqual(fired(name, run), [])

    def test_every_rule_fires(self):
        """inv: every strict-only rule has a trip table, and returns problems on at least one of
        its rows: a rule that never fires is decoration (INV-02)"""
        names = [rule for rule, _at, _fn in invariants.RULES]
        self.assertEqual(sorted(names), sorted({row[0] for row in STATES}))
        for name in names:
            with self.subTest(rule=name):
                hits = 0
                for rule, _row, entries, defaults, fires, repo in STATES:
                    if rule == name and fires:
                        with tempfile.TemporaryDirectory() as d:
                            hits += bool(fired(name, a_run(d, entries, defaults, **repo)))
                self.assertGreater(hits, 0, f'{name} returned nothing on every row')


class Restored(EngineCase):
    """The rules against the old behaviour itself, on the journeys that reach it. The restored
    export gate of 50d41ca for `written-ruling-kept` is RUN-82's mutation test
    (`test_recovery_sequences`), whose `resume` after the journey raises it."""
    HEADER = policy.LearningDelivery.HEADER
    events = policy.Provenance.events
    ruling_env = policy.Provenance.ruling_env
    held = policy.Provenance.held
    standing = policy.LearningDelivery.standing
    journey_destination = policy.LearningDelivery.test_a_queued_ruling_keeps_its_destination

    def test_queued_entry_has_file_catches_the_unrefused_replan(self):
        """inv: RUN-76's journey under 35cff76's replan (no look at queued rulings) raises at the
        replan's own save, naming queued-entry-has-file, before any finish (INV-01)"""
        with replan_drops_the_rulings_file(), self.assertRaisesRegex(
                invariants.Violation, "^queued-entry-has-file: make/PE-1 names no rulings file "
                "and the run's workflow sets no rulings_file$"):
            self.journey_destination()
        # The replan's revised workflow was installed: the finish would have had nowhere to go.
        self.assertNotIn('rulings_file', record.read_json(
            os.path.join(self.the_run().path, 'workflow.expanded.json'))['defaults'])
        self.assertFalse(self.the_run().state['standing_pending'][0].get('written'))


if __name__ == '__main__':
    unittest.main()
