"""A checkpoint must survive the runner's actual record-index regeneration."""
import importlib.util
from pathlib import Path
from test_record import RunCase
from codesmith import activity

SPEC = importlib.util.spec_from_file_location('checkpoint_skill',
    Path(__file__).resolve().parents[1] / '.claude/skills/agent-checkpoints/scripts/checkpoint.py')
checkpoint = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checkpoint)


class CheckpointRecords(RunCase):
    def test_index_named_artifact_survives_record_regeneration(self):
        self.write('index.json', '{"important":"user artifact"}')
        inv = Path(self.run_.path) / 'observer'; inv.mkdir()
        activity.prepare('claude', self.root, inv, {})
        store = inv / 'hooks/checkpoints'
        report = dict(task_id='design', objective='Preserve draft', stage='draft', status='working',
            requirements=[dict(id='R1',status='implemented',evidence='file exists')],
            accomplished=['draft'], remaining=['review'], next_action='review',
            verification=[], blockers=[], recovery='compare against current base')
        checkpoint.save(store, self.root, report, ['index.json'])
        self.run_.regenerate()
        data, warnings = checkpoint.latest(store)
        self.assertEqual(warnings, [])
        artifact = store / data['checkpoint_id'] / data['artifacts'][0]['snapshot']
        self.assertEqual(artifact.read_text(), '{"important":"user artifact"}')
        self.assertEqual(self.run_.integrity_check(), [])
