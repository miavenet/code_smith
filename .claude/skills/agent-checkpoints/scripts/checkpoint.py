#!/usr/bin/env python3
"""Durable agent-owned milestones. Never restores files or grants task acceptance."""
import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import uuid


class Invalid(ValueError):
    pass


def encode(value):
    return (json.dumps(value, indent=2, ensure_ascii=False) + '\n').encode()


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(65536), b''):
            h.update(block)
    return h.hexdigest()


def flush_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write(path, data):
    with path.open('xb') as output:
        os.chmod(path, 0o600)
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def relative(name):
    p = Path(name)
    if not name or p.is_absolute() or '..' in p.parts or p == Path('.'):
        raise Invalid('expected a workspace-relative file: ' + name)
    if any(part in ('.git', '.runs') for part in p.parts):
        raise Invalid('runner/Git metadata cannot be an artifact: ' + name)
    return p


def artifact_path(root, name):
    p = root / relative(name)
    for part in (p, *p.parents):
        if part == root:
            break
        if part.is_symlink():
            raise Invalid('symlink artifacts are not supported: ' + name)
    if not p.resolve().is_relative_to(root.resolve()):
        raise Invalid('artifact escapes workspace: ' + name)
    return p


def validate_report(report):
    if not isinstance(report, dict):
        raise Invalid('report must be a JSON object')
    for key in ('task_id', 'objective', 'stage', 'status', 'next_action', 'recovery'):
        if not isinstance(report.get(key), str) or not report[key].strip():
            raise Invalid('report needs nonempty text: ' + key)
    if report['status'] not in ('working', 'blocked', 'ready_for_review'):
        raise Invalid('checkpoint status is not runner acceptance')
    for key in ('accomplished', 'remaining', 'blockers'):
        if not isinstance(report.get(key), list) or any(not isinstance(v, str) for v in report[key]):
            raise Invalid('report needs a list of strings: ' + key)
    requirements = report.get('requirements')
    if not isinstance(requirements, list) or not requirements:
        raise Invalid('report needs requirement-level progress')
    ids = set()
    for item in requirements:
        if not isinstance(item, dict) or not all(isinstance(item.get(k), str) and item[k] for k in ('id', 'status', 'evidence')):
            raise Invalid('each requirement needs id, status, evidence')
        if item['id'] in ids:
            raise Invalid('duplicate requirement id: ' + item['id'])
        ids.add(item['id'])
        if item['status'] not in ('pending', 'in_progress', 'implemented', 'verified', 'blocked', 'deferred'):
            raise Invalid('invalid requirement status')
    checks = report.get('verification')
    if not isinstance(checks, list):
        raise Invalid('report needs verification (possibly an empty list)')
    for check in checks:
        if not isinstance(check, dict) or not all(isinstance(check.get(k), str) and check[k] for k in ('command', 'result', 'evidence')):
            raise Invalid('verification needs command, result, evidence')
        if check['result'] not in ('passed', 'failed', 'not_run'):
            raise Invalid('invalid verification result')


def git_value(root, *args):
    p = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True, timeout=5)
    return p.stdout.strip() if p.returncode == 0 else None


def save(store, workspace, report, artifacts=(), deleted=()):
    validate_report(report)
    root = Path(workspace).resolve(strict=True)
    if not root.is_dir():
        raise Invalid('workspace must be a directory')
    names = [str(relative(n)) for n in (*artifacts, *deleted)]
    if len(set(names)) != len(names):
        raise Invalid('duplicate or conflicting artifact/deletion paths')
    store = Path(store).resolve()
    store.mkdir(parents=True, exist_ok=True, mode=0o700)
    flush_dir(store.parent)
    now = dt.datetime.now(dt.timezone.utc)
    identity = now.strftime('%Y%m%dT%H%M%S.%fZ-') + uuid.uuid4().hex[:12]
    pending = store / ('.pending-' + identity)
    pending.mkdir(mode=0o700)
    entries = []
    try:
        for name in artifacts:
            source = artifact_path(root, name)
            if source.resolve().is_relative_to(store):
                raise Invalid('cannot snapshot checkpoint storage')
            before = source.stat()
            if not stat.S_ISREG(before.st_mode):
                raise Invalid('artifact is not a regular file: ' + name)
            target = pending / 'artifacts' / f'{len(entries):06d}.blob'
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with source.open('rb') as src, target.open('xb') as dst:
                os.chmod(target, 0o600)
                shutil.copyfileobj(src, dst, 65536)
                dst.flush()
                os.fsync(dst.fileno())
            after = source.stat()
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                raise Invalid('artifact changed during checkpoint; retry at a stable milestone: ' + name)
            entries.append({'path': str(relative(name)), 'snapshot': str(target.relative_to(pending)),
                            'sha256': digest(target), 'size': target.stat().st_size,
                            'mode': stat.S_IMODE(before.st_mode)})
        for name in deleted:
            source = artifact_path(root, name)
            if source.exists():
                raise Invalid('declared deletion still exists: ' + name)
            entries.append({'path': str(relative(name)), 'deleted': True})
        manifest = {'schema_version': 1, 'checkpoint_id': identity, 'created_at': now.isoformat(),
                    'workspace': str(root), 'base_commit': git_value(root, 'rev-parse', 'HEAD'),
                    'branch': git_value(root, 'branch', '--show-current'), 'report': report,
                    'artifacts': entries,
                    'correlation': {key: os.environ.get(value) for key, value in
                        [('run', 'CODE_SMITH_RUN'), ('task', 'CODE_SMITH_TASK'),
                         ('invocation', 'CODE_SMITH_INVOCATION'), ('agent_kind', 'CODE_SMITH_AGENT_KIND')]}}
        manifest['correlation']['task'] = manifest['correlation']['task'] or report['task_id']
        write(pending / 'checkpoint.json', encode(manifest))
        write(pending / 'COMMITTED', (digest(pending / 'checkpoint.json') + '\n').encode())
        for directory, _, _ in os.walk(pending, topdown=False):
            flush_dir(directory)
        published = store / identity
        os.rename(pending, published)
        flush_dir(store)
    except BaseException:
        shutil.rmtree(pending, ignore_errors=True)
        raise
    event = {'event': 'AgentCheckpoint', 'source': 'agent-checkpoints',
             'ts': manifest['created_at'], 'checkpoint_id': identity, 'checkpoint_path': str(published),
             'code_smith': manifest['correlation'],
             'result': (f"agent-reported {report['stage'][:120]}: {report['status']}; "
                        f"{len(report['accomplished'])} accomplishments, {len(report['remaining'])} remaining; "
                        f"checkpoint {identity}")}
    hook_dir = os.environ.get('HOOK_LOG_DIR')
    if hook_dir and store == Path(hook_dir).resolve() / 'checkpoints':
        try:
            # Separate from native hook files; never rewrite another logger's stream.
            fd = os.open(store.parent / 'milestones.jsonl', os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'ab') as log:
                fcntl.flock(log, fcntl.LOCK_EX)
                log.write(json.dumps(event).encode() + b'\n')
                log.flush()
                os.fsync(log.fileno())
        except OSError as exc:
            print(f'Checkpoint saved; milestone hook event unavailable: {exc}', file=sys.stderr)
    return event


def inspect_checkpoint(directory):
    manifest_file = directory / 'checkpoint.json'
    if manifest_file.is_symlink() or (directory / 'COMMITTED').is_symlink():
        raise Invalid('checkpoint metadata is a symlink')
    if (directory / 'COMMITTED').read_text().strip() != digest(manifest_file):
        raise Invalid('checkpoint manifest checksum mismatch')
    data = json.loads(manifest_file.read_text())
    if data.get('schema_version') != 1 or data.get('checkpoint_id') != directory.name:
        raise Invalid('unknown checkpoint schema or identity mismatch')
    validate_report(data['report'])
    for index, entry in enumerate(data['artifacts']):
        relative(entry['path'])
        if entry.get('deleted'):
            continue
        if entry['snapshot'] not in (f'artifacts/{index:06d}.blob', 'artifacts/' + entry['path']):
            raise Invalid('unexpected artifact snapshot path')
        path = artifact_path(directory, entry['snapshot'])
        if not path.is_file() or path.stat().st_size != entry['size'] or digest(path) != entry['sha256']:
            raise Invalid('artifact checksum/size mismatch: ' + entry['path'])
    return data


def latest(store):
    warnings = []
    for directory in sorted(Path(store).glob('[0-9]*'), reverse=True):
        if directory.is_symlink() or not directory.is_dir():
            continue
        try:
            return inspect_checkpoint(directory), warnings
        except (OSError, ValueError, KeyError, TypeError) as exc:
            warnings.append(f'{directory.name}: {exc}')
    raise Invalid('no valid completed checkpoint' + (': ' + '; '.join(warnings) if warnings else ''))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('save', 'status', 'verify'))
    parser.add_argument('--store', help='persistent, authorized per-invocation directory; defaults to HOOK_LOG_DIR/checkpoints')
    parser.add_argument('--workspace', default='.')
    parser.add_argument('--report', help='JSON milestone report (save only)')
    parser.add_argument('--artifact', action='append', default=[], help='workspace-relative file to copy')
    parser.add_argument('--deleted', action='append', default=[], help='workspace-relative intentionally absent file')
    args = parser.parse_args()
    store = args.store
    if not store and os.environ.get('HOOK_LOG_DIR'):
        store = str(Path(os.environ['HOOK_LOG_DIR']) / 'checkpoints')
    if not store:
        parser.error('provide --store or a coordinator-designated HOOK_LOG_DIR')
    try:
        if args.command == 'save':
            if not args.report:
                parser.error('save requires --report')
            event = save(store, args.workspace, json.loads(Path(args.report).read_text()), args.artifact, args.deleted)
            print(json.dumps(event))
            return 0
        data, warnings = latest(store)
        for warning in warnings:
            print('WARNING: ignored corrupt newer checkpoint: ' + warning, file=sys.stderr)
        if args.command == 'verify':
            print(f"Stored bytes verified: {data['checkpoint_id']} (not task acceptance)")
            return 2 if warnings else 0
        print(json.dumps(data, indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print('checkpoint: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
