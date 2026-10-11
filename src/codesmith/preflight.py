"""Gate qualification on disposable copies of the untouched repository."""
import os
import re
import shlex
import subprocess
import sys
import tempfile
import uuid

from . import checks, gitops, patterns, record


def _present_outputs(wf, root):
    """The outputs of every task that exist under `root`, the clean clone a command ran in (never
    the owner's tree, which may hold ignored or untracked files the clone lacks): a literal output
    by its path, a glob output when at least one file matches it."""
    present = set()
    for t in wf.tasks:
        for o in t.get('outputs', []):
            path = o['path']
            if not patterns.has_wildcard(path):
                if os.path.lexists(os.path.join(root, path)):
                    present.add(path)
            elif _glob_has_match(root, path):
                present.add(path)
    return present


def _glob_has_match(root, pattern):
    """True if a file under `root` (outside .git) matches the glob output `pattern`."""
    top = os.path.join(root, *patterns.literal_prefix(pattern))
    for folder, dirs, files in os.walk(top):
        dirs[:] = [d for d in dirs if d != '.git']
        for name in files + [d for d in dirs if os.path.islink(os.path.join(folder, d))]:
            rel = os.path.relpath(os.path.join(folder, name), root).replace(os.sep, '/')
            if patterns.matches(pattern, rel):
                return True
    return False


def _names(path, words):
    """Does the command, split into `words`, name the output `path`? A literal output by its path;
    a glob output by its literal prefix (`tests/char/x` for `tests/char/x/**`, never an empty one)
    or by a path that matches it."""
    if not patterns.has_wildcard(path):
        return os.path.normpath(path) in words
    prefix = '/'.join(patterns.literal_prefix(path))
    return bool(prefix) and os.path.normpath(prefix) in words or \
        any(patterns.matches(path, w.replace(os.sep, '/')) for w in words)


def _missing_outputs(present, task, command):
    """The outputs of `task` that the command names and the untouched tree lacks (not in
    `present`): a command that verifies output not yet produced is expected to fail until it is."""
    try:
        words = {os.path.normpath(w) for w in shlex.split(command)}
    except ValueError:
        return []
    return [o['path'] for o in task.get('outputs', [])
            if o['path'] not in present and _names(o['path'], words)]


def _upstream(by_id, task):
    """Every task `task` needs, directly or transitively."""
    seen, todo = set(), list(task.get('needs', []))
    while todo:
        tid = todo.pop()
        if tid not in seen and tid in by_id:
            seen.add(tid)
            todo.extend(by_id[tid].get('needs', []))
    return seen


def _missing_by_task(wf, present, task, producer, command):
    """Which tasks' missing outputs the command names, as (own, upstream, foreign): own is
    the producer's paths; upstream and foreign are task ids. Upstream is what the producer or the
    command's own task needs, directly or transitively: such a command waits for that task."""
    by_id = {t['id']: t for t in wf.tasks}
    own = _missing_outputs(present, producer, command) if producer else []
    before = _upstream(by_id, producer) | _upstream(by_id, task)
    upstream, foreign = [], []
    for other in wf.tasks:
        if other['kind'] != 'produce' or other['id'] == producer.get('id'):
            continue
        if _missing_outputs(present, other, command):
            (upstream if other['id'] in before else foreign).append(other['id'])
    return own, upstream, foreign


def _judge(wf, present, task, producer, gate, status, tail):
    """(result, fails_as_intended, waits_for, note) for one command on the untouched tree.
    `status` is pass, fail, error, or `missing` for exit 126 or 127: the command could not be run,
    which is intended when what it runs is a missing output (`sh tests/regression/t.sh`)."""
    own, upstream, foreign = _missing_by_task(wf, present, task, producer, gate['run'])
    missing = status == 'missing'
    if missing:
        status = 'error'
    if (status == 'fail' or missing) and foreign and not own and not upstream:
        return ('error', False, [], f"it names an output of {', '.join(foreign)}, which "
                f"'{task['id']}' does not need: that dependency is missing from the workflow")
    # it verifies output that does not exist yet (a timeout never counts)
    waits = (status == 'fail' or missing) and bool(own or upstream)
    if gate.get('expect') == 'fail':
        if waits:
            return 'fail', True, upstream if not own else [], ''
        if status == 'fail' and re.search(gate['fail_pattern'], tail):
            return 'pass', False, [], 'the defect already reproduces through existing files'
        if status == 'pass':
            return 'pass', False, [], 'it passes on the untouched tree; the task must make it fail'
        return 'error', False, [], ''
    if gate.get('new'):
        if status == 'fail':
            if re.search(gate.get('fail_pattern', ''), tail):
                return ('unconfirmed' if not gate.get('fail_pattern') else 'fail'), True, [], ''
        if waits:
            return 'fail', True, upstream if not own else [], ''
        if status == 'fail':
            return 'error', False, [], ''
        if status == 'pass':
            return 'objection', False, [], ''
        return status, False, [], ''
    if waits:
        return 'fail', True, upstream if not own else [], ''
    return status, False, [], ''


def describe(result):
    """One line of `check-gates` output for a result."""
    if result['result'] == 'fail' and result['fails_as_intended']:
        text = 'fails as intended'
        if result.get('waits_for'):
            text += f" (waits for {', '.join(result['waits_for'])})"
    elif result['result'] == 'unconfirmed':
        text = 'fails (no fail_pattern to confirm the reason)'
    else:
        text = result['result']
    if result.get('note'):
        text += f": {result['note']}"
    return text


def check_gates(wf):
    source = gitops.Git(wf.root)
    if not source.is_clean():
        raise record.RecordError('check-gates requires a clean work tree; commit or remove changes first')
    runs = record.ensure_runs_dir(source.top)
    with record.Lock(runs, top=source.top).acquire('check-gates'):
        directory = os.path.join(runs, 'check-gates', str(uuid.uuid4()))
        os.makedirs(directory)
        print('check-gates runs each command in a clean clone of the committed files: files that are '
              'ignored or untracked are not there', file=sys.stderr)
        by_id = {t['id']: t for t in wf.tasks}
        planned = []
        for task in wf.tasks:
            if task['kind'] == 'produce':
                for n, gate in enumerate(task['gates'], 1):
                    planned.append((task, f"{task['id']}:gate:{n}", gate, task))
            elif task['kind'] == 'check':
                for n, command in enumerate(task['run'], 1):
                    planned.append((task, f"{task['id']}:check:{n}", {'run': command, 'new': False},
                                    by_id.get(task.get('verifies'), {})))
        results = []
        # (command, timeout) -> the result of its first run and the outputs its clone held: the
        # tree is the same; whether a failure is intended depends on the producer it verifies.
        done = {}
        for number, (task, name, gate, producer) in enumerate(planned, 1):
            key = (gate['run'], task['gate_timeout_min'])
            # `new` gates and expected failures are always run on their own: their pass test differs.
            special = gate.get('new') or gate.get('expect') == 'fail'
            if key in done and not special:
                first, present, raw = done[key]
                status, intended, waits, note = _judge(wf, present, task, producer, gate, *raw)
                if first['changed_paths'] or first['embedded_repositories']:
                    status, waits, note = 'error', [], ''
                results.append(dict(first, id=name, new=False, same_as=first['id'], result=status,
                                    fails_as_intended=intended, waits_for=waits, note=note))
                continue
            with tempfile.TemporaryDirectory(prefix='code-smith-gates-') as tmp:
                root = os.path.join(tmp, 'repo')
                cloned = subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', 'clone', '-q',
                                         '--no-hardlinks', '--', source.top, root], capture_output=True)
                if cloned.returncode:
                    raise record.RecordError('check-gates could not clone the repository: '
                                             + cloned.stderr.decode(errors='replace').strip())
                git = gitops.Git(root)
                cwd = os.path.join(root, os.path.relpath(wf.root, source.top))
                log = os.path.join(directory, f'{number}.log')
                ran = checks.run_commands([gate['run']], cwd=cwd, log_path=log,
                                           timeout_s=task['gate_timeout_min'] * 60,
                                           env=checks.command_env(os.environ))[-1]
                if ran['result'] == 'error' and ran['exit'] in (126, 127):
                    raw = ('missing', ran['tail'])           # not run: maybe what it runs is absent
                else:
                    raw = ('error' if ran['result'] in ('timeout', 'error') else ran['result'], ran['tail'])
                present = _present_outputs(wf, cwd)
                status, intended, waits, note = _judge(wf, present, task, producer, gate, *raw)
                litter = git.dirty_paths()
                embedded = git.embedded_repositories()
                if litter or embedded:
                    status, waits, note = 'error', [], ''
                results.append({'id': name, 'command': gate['run'], 'new': gate.get('new', False),
                                'result': status, 'fails_as_intended': intended, 'exit': ran['exit'],
                                'changed_paths': litter, 'embedded_repositories': embedded, 'log': log,
                                'waits_for': waits, 'note': note})
                if gate.get('expect') == 'fail':
                    results[-1]['expect'] = 'fail'
                if not special:
                    done[key] = results[-1], present, raw
        report = {'results': results, 'ok': all(r['result'] in ('pass', 'unconfirmed') or
                  (r['result'] == 'fail' and r['fails_as_intended']) for r in results)}
        record.write_durable(os.path.join(directory, 'results.json'), record.dump_json(report))
        report['directory'] = directory
        return report
