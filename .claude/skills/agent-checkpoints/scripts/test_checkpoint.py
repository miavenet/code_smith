"""Exercise persistence and failure boundaries, not wording or report layout."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import checkpoint

SCRIPT = Path(checkpoint.__file__).resolve()


def report(stage='draft'):
    return dict(task_id='task-a', objective='Deliver a checked artifact', stage=stage,
                status='working', requirements=[dict(id='R1', status='implemented', evidence='draft exists')],
                accomplished=['draft'], remaining=['verify'], next_action='run the test',
                verification=[], blockers=[], recovery='inspect saved draft against current base')


class Checkpoints(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.work = self.root / 'work'; self.work.mkdir()
        self.store = self.root / 'durable'
        (self.work / 'draft.txt').write_text('useful partial work')

    def save(self, stage='draft'):
        return checkpoint.save(self.store, self.work, report(stage), ['draft.txt'])

    def test_artifacts_survive_worktree_rollback_and_record_deletions(self):
        checkpoint.save(self.store, self.work, report(), ['draft.txt'], ['removed.txt'])
        (self.work / 'draft.txt').unlink()
        data, warnings = checkpoint.latest(self.store)
        self.assertEqual(warnings, [])
        self.assertEqual(data['correlation']['task'], 'task-a')
        generation = self.store / data['checkpoint_id']
        self.assertEqual((generation / data['artifacts'][0]['snapshot']).read_text(), 'useful partial work')
        self.assertEqual(data['artifacts'][1], {'path': 'removed.txt', 'deleted': True})
        self.assertEqual((generation / 'checkpoint.json').stat().st_mode & 0o777, 0o600)

    def test_killed_writer_before_publication_keeps_previous_checkpoint(self):
        previous = self.save()['checkpoint_id']
        (self.work / 'draft.txt').write_text('later unfinished work')
        payload = self.root / 'input.json'; payload.write_text(json.dumps(report('later')))
        code = '''import checkpoint, os, signal, json, sys
from pathlib import Path
def die(*args): os.kill(os.getpid(), signal.SIGKILL)
checkpoint.os.rename = die
checkpoint.save(Path(sys.argv[1]), Path(sys.argv[2]), json.loads(Path(sys.argv[3]).read_text()), ['draft.txt'])
'''
        proc = subprocess.run([sys.executable, '-c', code, str(self.store), str(self.work), str(payload)],
                              env=dict(os.environ, PYTHONPATH=str(SCRIPT.parent)), capture_output=True)
        self.assertEqual(proc.returncode, -signal.SIGKILL)
        data, warnings = checkpoint.latest(self.store)
        self.assertEqual(data['checkpoint_id'], previous)
        self.assertEqual(warnings, [])
        self.assertTrue(list(self.store.glob('.pending-*')))

    def test_corrupt_newest_falls_back_visibly_and_verify_fails(self):
        first = self.save()['checkpoint_id']
        second = self.save('second')['checkpoint_id']
        manifest = json.loads((self.store / second / 'checkpoint.json').read_text())
        (self.store / second / manifest['artifacts'][0]['snapshot']).write_text('damaged')
        data, warnings = checkpoint.latest(self.store)
        self.assertEqual(data['checkpoint_id'], first)
        self.assertEqual(len(warnings), 1)
        proc = subprocess.run([sys.executable, str(SCRIPT), 'verify', '--store', str(self.store)], capture_output=True)
        self.assertEqual(proc.returncode, 2)
        self.assertIn(b'corrupt newer checkpoint', proc.stderr)

    def test_outside_symlink_metadata_and_conflicting_deletion_rejected(self):
        (self.work / 'link').symlink_to(self.work / 'draft.txt')
        for name in ('../other', '/etc/passwd', 'link', '.runs/state.json'):
            with self.subTest(name=name), self.assertRaises((checkpoint.Invalid, OSError)):
                checkpoint.save(self.store, self.work, report(), [name])
        with self.assertRaises(checkpoint.Invalid):
            checkpoint.save(self.store, self.work, report(), [], ['draft.txt'])
        self.assertFalse(list(self.store.glob('[0-9]*')))

    def test_manifest_corruption_and_acceptance_claim_are_not_trusted(self):
        identity = self.save()['checkpoint_id']
        (self.store / identity / 'checkpoint.json').write_text('{}')
        with self.assertRaises(checkpoint.Invalid): checkpoint.latest(self.store)
        invalid = report(); invalid['status'] = 'accepted'
        with self.assertRaises(checkpoint.Invalid): checkpoint.save(self.store, self.work, invalid)

    def test_hook_failure_does_not_lose_durable_checkpoint(self):
        self.store = self.root / 'hooks/checkpoints'
        self.store.parent.mkdir()
        (self.store.parent / 'milestones.jsonl').mkdir()  # cannot append to a directory
        with patch.dict(os.environ, HOOK_LOG_DIR=str(self.store.parent)):
            result = self.save()
        data, _ = checkpoint.latest(self.store)
        self.assertEqual(data['checkpoint_id'], result['checkpoint_id'])

    def test_milestone_event_correlates_to_the_saved_generation(self):
        self.store = self.root / 'hooks/checkpoints'
        with patch.dict(os.environ, HOOK_LOG_DIR=str(self.store.parent), CODE_SMITH_TASK='task-a'):
            event = self.save()
        rows = (self.store.parent / 'milestones.jsonl').read_text().splitlines()
        self.assertEqual(json.loads(rows[0]), event)
        self.assertEqual(event['code_smith']['task'], 'task-a')
        self.assertTrue((Path(event['checkpoint_path']) / 'COMMITTED').exists())


if __name__ == '__main__':
    unittest.main()
