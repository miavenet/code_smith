"""Route native hook telemetry to each invocation; observations never decide acceptance."""
from collections import deque
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

from . import proc, record


def assets(root, kind):
    root = Path(root)
    if kind == "copilot":
        # Repository hooks; Copilot loads them in prompt mode only for a trusted folder.
        return sorted(p for p in (root / '.github' / 'hooks').glob('*.json') if p.is_file())
    if kind not in ("claude", "codex"):
        return []
    relative = (['.claude/settings.json', '.claude/settings.local.json', '.claude/hooks/log-hook.py']
                if kind == 'claude' else ['.codex/config.toml', '.codex/hooks.json', '.codex/statusline.py',
                                         '.claude/hooks/log-hook.py'])
    return [root / p for p in relative if (root / p).is_file()]


NO_HOOKS = 'no_hooks'
NO_LOGGER = 'no_logger'
UNRECORDED = 'unrecorded'
INSPECTION_FAILED = 'inspection_failed'

_CAUSE_MESSAGES = {
    NO_HOOKS: 'the work tree has no .claude/settings.json (or settings.local.json) defining hooks',
    NO_LOGGER: 'hooks are defined but none invokes a logger that writes to HOOK_LOG_DIR',
    UNRECORDED: 'definitions look right but nothing was recorded (trust prompt, or the profile ignores project settings)',
    INSPECTION_FAILED: 'hook definitions could not be safely inspected; check .claude/settings.json by hand',
}


def _leaf_hooks(hooks_value):
    """Yield each leaf hook-definition object under settings.json's "hooks" key.

    Expected shape: {event: [{"matcher": ..., "hooks": [{"type": ..., "command": ..., ...}]}]}.
    Every level is type-checked before iterating, so an unexpected shape (wrong type,
    extra nesting, scalars where a container is expected) is skipped rather than raising -
    this walk is intentionally shallow and never recurses into the leaf objects themselves.
    """
    if not isinstance(hooks_value, dict):
        return
    for groups in hooks_value.values():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            leaves = group.get('hooks')
            if not isinstance(leaves, list):
                continue
            for leaf in leaves:
                if isinstance(leaf, dict):
                    yield leaf


def _hook_definitions(root):
    """Read-only: every leaf hook-definition object from the work tree's own settings files."""
    leaves = []
    for name in ('.claude/settings.json', '.claude/settings.local.json'):
        path = Path(root) / name
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError, UnicodeError):
            # Missing file or invalid JSON: treat this file as contributing no hooks. A
            # RecursionError is different - the file exists and may define hooks, but this
            # code cannot tell; it must propagate so the caller reports INSPECTION_FAILED
            # instead of silently agreeing with NO_HOOKS.
            continue
        if not isinstance(data, dict):
            continue
        leaves.extend(_leaf_hooks(data.get('hooks')))
    return leaves


def _command_tokens(hook):
    """Every whitespace-separated token from a hook's command and args fields.

    `command` may be exec-form (just the interpreter, e.g. "python3") or shell-form
    (interpreter and script together, e.g. 'python3 "$X/log-hook.py"'); either way the
    referenced script is one of the resulting tokens, not the field's raw value.
    """
    command = hook.get('command')
    tokens = []
    if isinstance(command, str):
        try:
            tokens.extend(shlex.split(command))
        except ValueError:
            tokens.append(command)
    args = hook.get('args')
    if isinstance(args, list):
        tokens.extend(a for a in args if isinstance(a, str))
    return tokens


# Shell substitution of ${CLAUDE_PROJECT_DIR} or the unbraced $CLAUDE_PROJECT_DIR, the latter
# only up to a word boundary so it doesn't also consume e.g. $CLAUDE_PROJECT_DIRECTORY.
_PROJECT_DIR_VAR = re.compile(r'\$\{CLAUDE_PROJECT_DIR\}|\$CLAUDE_PROJECT_DIR\b')


def _invokes_hook_log_dir_logger(root, hook):
    if not isinstance(hook, dict) or hook.get('type') not in (None, 'command'):
        return False
    for token in _command_tokens(hook):
        candidate = _PROJECT_DIR_VAR.sub(lambda _m: str(root), token)
        path = Path(candidate)
        if not path.is_absolute():
            path = Path(root) / candidate
        try:
            if path.is_file() and 'HOOK_LOG_DIR' in path.read_text(errors='replace'):
                return True
        except OSError:
            continue
    return False


def diagnose_claude_silence(root):
    """Read-only cause for a Claude profile with no observed native activity.

    Inspects only the work tree's own hook definitions and referenced scripts;
    never runs anything, and never raises - malformed or adversarial settings.json
    content degrades to INSPECTION_FAILED rather than aborting the caller.
    """
    try:
        hooks = _hook_definitions(root)
        if not hooks:
            cause = NO_HOOKS
        elif not any(_invokes_hook_log_dir_logger(root, hook) for hook in hooks):
            cause = NO_LOGGER
        else:
            cause = UNRECORDED
    except (RecursionError, OSError, ValueError, TypeError, AttributeError):
        cause = INSPECTION_FAILED
    return cause, _CAUSE_MESSAGES[cause]


def prepare(kind, cwd, invocation_dir, env):
    directory = Path(invocation_dir).resolve()
    logs = directory / 'hooks'
    logs.mkdir(mode=0o700)
    updated = dict(env)
    updated.update(HOOK_LOG_DIR=str(logs), CODEX_HOOK_LOG=str(logs / 'hooks.jsonl'),
                   CODE_SMITH_INVOCATION=str(directory), CODE_SMITH_AGENT_KIND=kind)
    sources = {str(p): record.sha256_file(p) for p in assets(cwd, kind)}
    record.write_durable(directory / 'activity.json', record.dump_json({
        'agent_kind': kind, 'run': env.get('CODE_SMITH_RUN'), 'task': env.get('CODE_SMITH_TASK'),
        'invocation': str(directory), 'hook_log': 'hooks/hooks.jsonl', 'sources': sources,
        'note': 'Native hooks are observational. Empty logs mean no events observed, not inactivity. '
                'Inspect stderr for hook trust/configuration warnings; Codex and Copilot stdout.log also have tool events.'}))
    return updated


def read_events(directory, limit=20):
    """Bound retained memory; tolerate async partial lines and malformed external telemetry."""
    rows = []
    paths = [Path(directory) / name for name in ('hooks.jsonl.1', 'hooks.jsonl', 'milestones.jsonl')]
    for path in paths:
        recent = deque(maxlen=limit)
        try:
            with path.open(errors='replace') as source:
                for line in source:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(row, dict):
                        recent.append(row)
        except OSError:
            pass
        rows.extend(recent)
    return sorted(rows, key=lambda row: str(row.get('ts') or row.get('timestamp') or ''))[-limit:]


def render(run_path, task=None, limit=20):
    observations = []
    invocations = []
    for metadata in Path(run_path).rglob('activity.json'):
        try:
            info = record.read_json(metadata)
        except (OSError, ValueError):
            continue
        if not isinstance(info, dict):
            continue
        if task and info.get('task') != task:
            continue
        invocations.append(info)
        for row in read_events(metadata.parent / 'hooks', limit):
            stamp = str(row.get('ts') or row.get('timestamp') or '')
            event = row.get('event') or row.get('hook_event_name') or 'unknown'
            payload = row.get('payload') if isinstance(row.get('payload'), dict) else {}
            summary = row.get('result') or payload.get('summary') or row.get('tool_name') or ''
            summary = ' '.join(str(summary).split())[:300]
            if event == 'AgentCheckpoint' and not summary.startswith('agent-reported '):
                summary = 'agent-reported ' + summary
            observations.append((stamp, f"{stamp} {info.get('task', '?')} {event} {summary}"))
    lines = [f"Agent activity: {len(invocations)} invocation(s); observational only."]
    lines.extend(text for _, text in sorted(observations)[-limit:])
    if not observations:
        lines.append('No hook or exec-stream events observed. Check invocation activity.json, stderr.log, and stdout.log.')
    return proc.redact(('\n'.join(lines)+'\n').encode()).decode()


class CodexTelemetry:
    """Use the existing passive logger for exec events when native hooks are absent.

    These rows retain an explicit exec-stream origin: they never pretend to be native hooks.
    """
    def __init__(self, cwd, env):
        self.env = env
        # Execute only the logger shipped beside this runner, never an arbitrary target-repo script.
        self.logger = Path(__file__).resolve().parents[2] / '.codex/statusline.py'
        self.session_id = None

    def feed(self, data):
        for line in data.splitlines():
            try:
                event = json.loads(line)
                if not isinstance(event, dict):
                    continue
                if event.get('type') not in {'thread.started', 'turn.started', 'turn.completed',
                                              'turn.failed', 'error', 'item.started', 'item.completed'}:
                    continue
                if event.get('type') == 'thread.started':
                    self.session_id = event.get('thread_id')
                item = event.get('item') if isinstance(event.get('item'), dict) else {}
                payload = {'hook_event_name': 'ExecStream.' + str(event.get('type', 'unknown')),
                           'source': 'codex-exec-stream', 'session_id': self.session_id,
                           'tool_name': item.get('type'), 'tool_use_id': item.get('id'),
                           'summary': item.get('command') or item.get('text') or event.get('type'),
                           'event': event}
                subprocess.run([sys.executable, '-S', '-E', str(self.logger), '--hook'],
                               input=json.dumps(payload).encode(), env=self.env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=3, check=False)
            except (ValueError, OSError, subprocess.TimeoutExpired):
                pass
