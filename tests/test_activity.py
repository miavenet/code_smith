"""Native hook routing, concurrent invocations, and passive telemetry contracts."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
import helpers
from codesmith import activity, qualification

ROOT = Path(__file__).resolve().parents[1]


class Activity(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def fire(self, kind, directory, task, payload):
        directory.mkdir(exist_ok=True)
        env = activity.prepare(kind, str(ROOT), directory,
            dict(os.environ, CODE_SMITH_RUN='test-run', CODE_SMITH_TASK=task))
        script = (ROOT / '.claude/hooks/log-hook.py' if kind == 'claude' else ROOT / '.codex/statusline.py')
        argv = [sys.executable, '-S', '-E', str(script)] + (['--hook'] if kind == 'codex' else [])
        result = subprocess.run(argv, input=json.dumps(payload), text=True, capture_output=True, env=env)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, '', ''))
        return activity.read_events(directory / 'hooks')

    def test_native_loggers_correlate_and_redact_independent_invocations(self):
        payload = {'hook_event_name':'PreToolUse','session_id':'session','tool_name':'Bash',
                   'tool_input':{'api_key':'private-value', 'command':'echo ok'}}
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda kind:self.fire(kind,self.root/kind,kind,payload), ('claude','codex')))
        for kind, rows in zip(('claude','codex'), results):
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['code_smith']['task'], kind)
            self.assertEqual(rows[0]['code_smith']['run'], 'test-run')
            self.assertNotIn('private-value', json.dumps(rows))
            self.assertEqual((self.root/kind/'hooks/hooks.jsonl').stat().st_mode & 0o777,0o600)
        self.assertIn('claude PreToolUse', activity.render(self.root, 'claude'))
        self.assertNotIn('codex PreToolUse', activity.render(self.root, 'claude'))

    def test_empty_and_partial_logs_do_not_claim_inactivity_or_success(self):
        inv=self.root/'inv';inv.mkdir()
        activity.prepare('codex',str(ROOT),inv,{})
        self.assertIn('No hook or exec-stream events observed',activity.render(self.root))
        (inv/'hooks/hooks.jsonl').write_text('{"event":"Stop","timestamp":"1"}\n{"event":')
        self.assertEqual(len(activity.read_events(inv/'hooks')),1)
        self.assertIn('observational only',activity.render(self.root))

    def test_copilot_repository_hooks_and_user_settings_invalidate_qualification(self):
        hooks=self.root/'.github/hooks';hooks.mkdir(parents=True)
        (hooks/'log.json').write_text('{"version":1}');(hooks/'notes.txt').write_text('not a hook file')
        self.assertEqual(activity.assets(self.root,'copilot'),[hooks/'log.json'])
        home=self.root/'copilot-home';home.mkdir();(home/'settings.json').write_text('{"model":"a"}')
        profile={'kind':'copilot','argv':[sys.executable]}
        with patch.dict(os.environ,COPILOT_HOME=str(home)):
            first=qualification.fingerprint(profile,'m',True,str(self.root))
            self.assertIn(str(home/'settings.json'),first[1]['config_hashes'])
            (hooks/'log.json').write_text('{"version":2}')
            second=qualification.fingerprint(profile,'m',True,str(self.root))[0]
            (home/'settings.json').write_text('{"model":"b"}')
            third=qualification.fingerprint(profile,'m',True,str(self.root))[0]
        self.assertEqual(len({first[0],second,third}),3)

    def test_hook_changes_invalidate_qualification(self):
        folder=self.root/'.claude/hooks';folder.mkdir(parents=True)
        script=folder/'log-hook.py';script.write_text('# first')
        profile={'kind':'claude','argv':[sys.executable]}
        first=qualification.fingerprint(profile,'m',False,str(self.root))[0]
        script.write_text('# revised')
        self.assertNotEqual(first,qualification.fingerprint(profile,'m',False,str(self.root))[0])

    def test_codex_logger_failure_is_passive(self):
        target=self.root/'not-a-directory';target.write_text('x')
        result=subprocess.run([sys.executable,str(ROOT/'.codex/statusline.py'),'--hook'],input='{}',
            text=True,capture_output=True,env=dict(os.environ,CODEX_HOOK_LOG=str(target/'log')))
        self.assertEqual((result.returncode,result.stdout,result.stderr),(0,'',''))

    def test_exec_stream_records_tool_command_without_claiming_native_hook(self):
        inv=self.root/'exec';inv.mkdir()
        env=activity.prepare('codex',str(ROOT),inv,dict(os.environ,CODE_SMITH_TASK='tool-task'))
        telemetry=activity.CodexTelemetry(str(ROOT),env)
        telemetry.feed(b'{"type":"thread.started","thread_id":"abc"}\n')
        telemetry.feed(b'{"type":"item.completed","item":{"type":"command_execution","id":"1","command":"python3 test.py","exit_code":0}}\n')
        rows=activity.read_events(inv/'hooks')
        self.assertEqual(rows[-1]['payload']['source'],'codex-exec-stream')
        self.assertEqual(rows[-1]['session_id'],'abc')
        self.assertEqual(rows[-1]['hook_event_name'],'ExecStream.item.completed')
        self.assertIn('python3 test.py',activity.render(self.root))

    def test_diagnoses_no_hooks_defined_in_the_work_tree(self):
        cause, message = activity.diagnose_claude_silence(self.root)
        self.assertEqual(cause, activity.NO_HOOKS)
        self.assertIn('no .claude/settings.json', message)
        self.assertIn('settings.local.json', message)
        # An empty or hook-less settings file is the same cause as no file at all.
        (self.root / '.claude').mkdir()
        (self.root / '.claude/settings.json').write_text(json.dumps({'other': True}))
        self.assertEqual(activity.diagnose_claude_silence(self.root)[0], activity.NO_HOOKS)

    def test_diagnoses_hooks_defined_but_settings_local_also_counts(self):
        (self.root / '.claude').mkdir()
        (self.root / '.claude/settings.local.json').write_text(json.dumps(
            {'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'true'}]}]}}))
        cause, message = activity.diagnose_claude_silence(self.root)
        self.assertEqual(cause, activity.NO_LOGGER)
        self.assertIn('none invokes a logger', message)
        self.assertIn('HOOK_LOG_DIR', message)

    def test_diagnoses_hooks_defined_but_no_logger_writes_to_hook_log_dir(self):
        (self.root / '.claude/hooks').mkdir(parents=True)
        (self.root / '.claude/hooks/log-hook.py').write_text('print("no env var referenced here")\n')
        (self.root / '.claude/settings.json').write_text(json.dumps({'hooks': {'Stop': [{'hooks': [
            {'type': 'command', 'command': 'python3',
             'args': ['${CLAUDE_PROJECT_DIR}/.claude/hooks/log-hook.py']}]}]}}))
        cause, message = activity.diagnose_claude_silence(self.root)
        self.assertEqual(cause, activity.NO_LOGGER)

    def test_diagnoses_definitions_look_right_but_nothing_was_recorded(self):
        (self.root / '.claude/hooks').mkdir(parents=True)
        (self.root / '.claude/hooks/log-hook.py').write_text(
            'import os\nos.environ.get("HOOK_LOG_DIR")\n')
        (self.root / '.claude/settings.json').write_text(json.dumps({'hooks': {'Stop': [{'hooks': [
            {'type': 'command', 'command': 'python3',
             'args': ['${CLAUDE_PROJECT_DIR}/.claude/hooks/log-hook.py']}]}]}}))
        cause, message = activity.diagnose_claude_silence(self.root)
        self.assertEqual(cause, activity.UNRECORDED)
        self.assertIn('trust prompt', message)

    def test_diagnosis_never_raises_on_malformed_settings(self):
        (self.root / '.claude').mkdir()
        (self.root / '.claude/settings.json').write_text('{not json')
        cause, _ = activity.diagnose_claude_silence(self.root)
        self.assertEqual(cause, activity.NO_HOOKS)

    def test_non_string_args_do_not_abort_the_diagnosis(self):
        (self.root / '.claude').mkdir()
        (self.root / '.claude/settings.json').write_text(json.dumps({'hooks': {'Stop': [{'hooks': [
            {'type': 'command', 'command': 'true', 'args': 42}]}]}}))
        cause, _ = activity.diagnose_claude_silence(self.root)
        self.assertEqual(cause, activity.NO_LOGGER)

    def test_deeply_nested_settings_json_reports_inspection_failed(self):
        (self.root / '.claude').mkdir()
        nested = '{"hooks": ' + '[' * 10000 + ']' * 10000 + '}'
        (self.root / '.claude/settings.json').write_text(nested)
        cause, message = activity.diagnose_claude_silence(self.root)
        self.assertEqual(cause, activity.INSPECTION_FAILED)
        self.assertIsInstance(message, str)

    def test_shell_form_command_string_is_recognized_as_invoking_the_logger(self):
        (self.root / '.claude/hooks').mkdir(parents=True)
        (self.root / '.claude/hooks/log-hook.py').write_text(
            'import os\nos.environ.get("HOOK_LOG_DIR")\n')
        (self.root / '.claude/settings.json').write_text(json.dumps({'hooks': {'Stop': [{'hooks': [
            {'type': 'command',
             'command': 'python3 "${CLAUDE_PROJECT_DIR}/.claude/hooks/log-hook.py"'}]}]}}))
        cause, _ = activity.diagnose_claude_silence(self.root)
        self.assertEqual(cause, activity.UNRECORDED)

    def test_documented_unbraced_project_dir_variable_is_recognized(self):
        (self.root / '.claude/hooks').mkdir(parents=True)
        (self.root / '.claude/hooks/log-hook.py').write_text(
            'import os\nos.environ.get("HOOK_LOG_DIR")\n')
        (self.root / '.claude/settings.json').write_text(json.dumps({'hooks': {'Stop': [{'hooks': [
            {'type': 'command',
             'command': 'python3 "$CLAUDE_PROJECT_DIR/.claude/hooks/log-hook.py"'}]}]}}))
        cause, _ = activity.diagnose_claude_silence(self.root)
        self.assertEqual(cause, activity.UNRECORDED)

    def test_unbraced_project_dir_variable_respects_word_boundary(self):
        # A longer variable name that merely starts with $CLAUDE_PROJECT_DIR must be left
        # untouched - not treated as $CLAUDE_PROJECT_DIR followed by literal extra characters.
        # This directly catches removing the trailing \b from the substitution regex, which
        # a test that only checked the final cause would not: without a file sitting at the
        # wrongly-truncated path, both the correct and the broken regex end up at NO_LOGGER.
        token = '$CLAUDE_PROJECT_DIRECTORY_NAME/log-hook.py'
        substituted = activity._PROJECT_DIR_VAR.sub(lambda _m: str(self.root), token)
        self.assertEqual(substituted, token)
        (self.root / '.claude').mkdir()
        (self.root / '.claude/settings.json').write_text(json.dumps({'hooks': {'Stop': [{'hooks': [
            {'type': 'command', 'command': 'echo $CLAUDE_PROJECT_DIRECTORY_NAME'}]}]}}))
        cause, _ = activity.diagnose_claude_silence(self.root)
        self.assertEqual(cause, activity.NO_LOGGER)

    def test_non_command_hooks_are_defined_but_not_a_logger(self):
        (self.root / '.claude').mkdir()
        (self.root / '.claude/settings.json').write_text(json.dumps({'hooks': {'Stop': [{'hooks': [
            {'type': 'prompt', 'prompt': 'Are you sure?'}]}]}}))
        cause, message = activity.diagnose_claude_silence(self.root)
        self.assertEqual(cause, activity.NO_LOGGER)
        self.assertIn('none invokes a logger', message)

    def test_malformed_event_rows_are_skipped_not_fatal(self):
        """a null or string payload and a non-UTF-8 byte neither raise nor hide the good rows"""
        inv = self.root / 'bad'; inv.mkdir()
        activity.prepare('claude', str(ROOT), inv, {'CODE_SMITH_TASK': 'design'})
        hooks = inv / 'hooks'
        good = json.dumps({'ts': '2026-09-20T12:00:03Z', 'event': 'PostToolUse', 'result': 'fine'})
        (hooks / 'hooks.jsonl').write_bytes(
            b'{"event":"A","payload":null}\n{"event":"B","payload":"text"}\n\xff\xfe\n' + good.encode() + b'\n')
        self.assertEqual(len(activity.read_events(hooks)), 3)
        self.assertIn('fine', activity.render(self.root))

    def test_checkpoint_events_merge_with_native_activity_by_time(self):
        inv = self.root / 'milestones'; inv.mkdir()
        activity.prepare('claude', str(ROOT), inv, {'CODE_SMITH_TASK': 'design'})
        hooks = inv / 'hooks'
        (hooks / 'hooks.jsonl').write_text(json.dumps({'ts':'2026-09-20T12:00:03Z','event':'PostToolUse','result':'native newest'})+'\n')
        (hooks / 'milestones.jsonl').write_text('\n'.join(json.dumps({'ts':f'2026-09-20T12:00:0{n}Z',
            'event':'AgentCheckpoint','result':f'milestone {n}'}) for n in (1,2))+'\n{"event":')
        rows = activity.read_events(hooks, limit=2)
        self.assertEqual([r['event'] for r in rows], ['AgentCheckpoint','PostToolUse'])
        display = activity.render(self.root, limit=2)
        self.assertIn('AgentCheckpoint agent-reported milestone 2', display)
        self.assertIn('native newest', display)
        self.assertNotIn('milestone 1', display)
