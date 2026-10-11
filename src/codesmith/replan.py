"""Freeze a revised plan, revert reopened work, then install it as one resumable operation."""
import copy
import hashlib
import shlex
import os
from pathlib import Path
import shutil

from . import engine, findings, gitops, record, workflow


class Refused(ValueError):
    pass


def _executed_files(task, expanded):
    """Words of the commands that guard `task`: its own gates or run, and the run of every check that
    verifies it. The expansion adds those words to `protected` when they name a tracked file, so
    the set depends on the tree: a gate that names a producer's own output protects it only once the
    output exists. Comparing definitions must ignore that, or an accepted task 'changes' by its own
    work at the first replan after it."""
    commands = [g['run'] for g in task.get('gates', [])] + list(task.get('run') or [])
    for other in expanded['tasks']:
        if other.get('kind') == 'check' and other.get('verifies') == task['id']:
            commands += list(other.get('run') or [])
    words = set()
    for cmd in commands:
        try:
            words |= {os.path.normpath(w).replace(os.sep, '/') for w in shlex.split(cmd)}
        except ValueError:
            continue
    return words


def _definition(task, directory):
    value = {k: v for k, v in task.items() if k not in ('order', 'prompt_file')}
    root = Path(directory)
    expanded = record.read_json(root / 'workflow.expanded.json')
    executed = _executed_files(task, expanded)
    value['protected'] = [p for p in task.get('protected', []) if p not in executed]
    files = [root / 'library' / 'types' / (task['type'] + '.toml')]
    if task.get('perspective'):
        files.append(root / 'library' / 'personas' / (task['perspective'] + '.toml'))
    if task.get('prompt_file'):
        files.append(root / 'briefs' / (task['id'] + '.md'))
    if task.get('rules_file'):
        files.append(root / 'briefs' / (task['id'] + '.rules.md'))
    value['frozen_contents'] = [hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
                                for p in files]
    if task.get('agent'):
        value['agent_profile'] = expanded['agents'].get(task['agent'])
        if task.get('fallback_agents'):
            value['fallback_profiles'] = {name: expanded['agents'].get(name) for name in task['fallback_agents']}
    return value


def _closure(run, before, roots):
    affected = set(roots)
    changed = True
    while changed:
        changed = False
        for task in before['tasks']:
            dependencies = set(task['needs']) | {task.get('reviews'), task.get('verifies')}
            st = run.state['tasks'][task['id']]
            if st['status'] == 'accepted' and task['kind'] == 'produce':
                dependencies.update(run.accepted_inputs(task['id']))
            elif st.get('attempt_dir'):
                path = Path(run.path, st['attempt_dir'], 'inputs.json')
                if path.is_file():
                    dependencies.update(record.read_json(path).get('inputs', {}))
            if task['id'] not in affected and dependencies & affected:
                affected.add(task['id']); changed = True
    return affected


def _definition_paths(run, wf, before):
    paths = {run.info['workflow_file'], wf.workflow_file}
    for task in before['tasks'] + wf.tasks:
        if task.get('prompt_file'):
            paths.add(task['prompt_file'])
    for definition in list(wf.types.values()) + list(wf.personas.values()):
        if definition.get('_where'):
            paths.add(definition['_where'])
    return {os.path.relpath(path, run.info['git_toplevel']).replace(os.sep, '/') for path in paths}


def prepare(run, git, source, reopen=()):
    if git.current_branch() != run.info['branch']:
        raise Refused(f"replan needs the run branch '{run.info['branch']}' checked out")
    if run.state.get('active_producer'):
        raise Refused(f"'{run.state['active_producer']}' holds the work tree; settle its transaction before replan")
    if run.state['intents']:
        raise Refused('resume must reconcile unfinished operations before another replan')
    if not git.is_clean():
        raise Refused('replan requires a clean tree; commit workflow/brief edits first, or use --workflow outside the repository')
    wf = workflow.load(source, rulings_at=run.info['base_commit'])
    if wf.errors:
        raise Refused('\n'.join(wf.errors))
    before = record.read_json(os.path.join(run.path, 'workflow.expanded.json'))
    if wf.name != before['name'] or os.path.realpath(wf.root) != os.path.realpath(before['paths']['root']):
        raise Refused('replan cannot change the workflow name or root')
    # A queued standing ruling is written to the file it was bound to, which stays protected only
    # while it is the workflow's rulings_file: a replan may not drop or move it first (RUN-76).
    bound = [(e.get('finding', '?'), e.get('file') or before['defaults'].get('rulings_file'))
             for e in run.state.get('standing_pending', []) if not e.get('written')]
    moved = [(f, name) for f, name in bound if name != wf.defaults.get('rulings_file')]
    if moved:
        raise Refused('standing rulings are queued for ' + ', '.join(sorted({n or '?' for _f, n in moved}))
                      + ' (' + ', '.join(f for f, _n in moved) + '), and the revised workflow '
                      + (f"names {wf.defaults['rulings_file']} as its rulings_file"
                         if wf.defaults.get('rulings_file') else 'drops rulings_file')
                      + '. Keep rulings_file as it is in the revised workflow while rulings are '
                      'queued for it. Nothing was changed')
    problems = run.record_problems()
    if problems:
        raise Refused('the run record was changed: ' + '; '.join(problems))
    if run.cleanup_problems():
        raise Refused(record.cleanup_refusal(run))
    expected = (run.state.get('expect') or {}).get('tip') or run.state.get('last_tip')
    if not expected:
        expected = next((run.state['tasks'][tid]['commit'] for tid in reversed(run.state['order'])
                         if run.state['tasks'][tid].get('commit')), run.info['base_commit'])
    if git.head() != expected:
        if git.run('merge-base', '--is-ancestor', expected, 'HEAD', check=False).returncode:
            raise Refused('the run branch moved away from its recorded history')
        changed = {p for _s,p,_o,_n in git.changed_paths(git.tree_of(expected),git.tree_of('HEAD'))}
        # The owner's commit of an export (`runner export-rulings`) is taken too (RUN-81).
        forbidden = (changed - _definition_paths(run,wf,before)
                     - findings.exported_files(run.state, before['defaults'].get('rulings_file')))
        if forbidden:
            raise Refused('commits since the pause changed more than workflow definitions: ' + ', '.join(sorted(forbidden)))
    parent = Path(run.path, 'replans')
    parent.mkdir(exist_ok=True)
    number = max((int(p.name) for p in parent.iterdir() if p.name.isdigit()), default=0)+1
    directory = parent / f'{number:03d}'
    directory.mkdir()
    new = directory / 'after'; new.mkdir()
    record.write_durable(new/'workflow.toml', Path(wf.workflow_file).read_bytes())
    record.write_durable(new/'workflow.expanded.json',record.dump_json(wf.expanded()))
    record.Run._freeze_library(wf,str(new));record.Run._freeze_briefs(wf,str(new))
    old_tasks = {t['id']: t for t in before['tasks']}
    new_tasks = {t['id']: t for t in wf.tasks}
    changes = {tid for tid in old_tasks.keys() | new_tasks.keys()
               if tid not in old_tasks or tid not in new_tasks
               or _definition(old_tasks[tid],run.path) != _definition(new_tasks[tid],new)}
    requested = set(reopen)
    unknown = requested - old_tasks.keys()
    if unknown:
        raise Refused('cannot reopen unknown tasks: '+', '.join(sorted(unknown)))
    # Changing a verifier of accepted work changes that producer's acceptance contract too.
    changed_roots = set(changes)
    for tid in changes & old_tasks.keys():
        target = old_tasks[tid].get('reviews') or old_tasks[tid].get('verifies')
        if target:
            changed_roots.add(target)
    for tid in changes & new_tasks.keys():
        target = new_tasks[tid].get('reviews') or new_tasks[tid].get('verifies')
        if target:
            changed_roots.add(target)
    closure = _closure(run,before,requested)
    accepted_changes = {tid for tid in changed_roots & old_tasks.keys()
                        if run.state['tasks'][tid]['status']=='accepted'}
    missing = accepted_changes - closure
    if missing:
        raise Refused('accepted work would change; use --reopen for: '+', '.join(sorted(missing)))
    affected = _closure(run,before,requested | (changed_roots & old_tasks.keys()))
    reopened_commits = {run.state['tasks'][tid]['commit'] for tid in affected
                        if run.state['tasks'][tid].get('commit')}
    commits = [sha for sha in git.out('rev-list','HEAD').splitlines() if sha in reopened_commits]
    if set(commits) != reopened_commits:
        raise Refused('a reopened task commit is missing from the run branch')
    old = directory/'before'; old.mkdir()
    for name in ('workflow.toml','workflow.expanded.json'):
        shutil.copyfile(Path(run.path,name),old/name)
    for name in ('library','briefs'):
        path=Path(run.path,name)
        if path.exists():shutil.copytree(path,old/name)
    plan = {'source':wf.workflow_file, 'changes':sorted(changes), 'affected':sorted(affected),
            'commits':commits, 'since':git.head(), 'new_tasks':sorted(new_tasks.keys()-old_tasks.keys()),
            'removed_tasks':sorted(old_tasks.keys()-new_tasks.keys())}
    run.protect(*(p for root in (old, new) for p in root.rglob('*') if p.is_file() and p.name != 'index.json'))
    run.write_decision(directory/'plan.json',plan)
    return directory,plan


def _keep_way_back(run,git,tid,crash):
    """Before an affected task's state is reduced: publish the set-aside record an older runner
    left only in the state, while `base` is still there to derive it from. (A legacy
    `apply_patch: true` was migrated on load to the queued `recover` the reduction carries.)
    Replayed after a crash, the record is found on disk and nothing is published twice."""
    path=os.path.join(run.task_dir(tid),'set-aside.json')
    if not os.path.exists(path):
        rec=engine.set_aside_record(run,git,tid)
        if rec is not None:
            run.publish_decision(path,rec,crash=crash)


def queued(run,affected):
    """What `replan` tells the owner about each affected task that still has a recovery queued."""
    return [f"{tid}: pending; the set-aside work of attempt {st['recover'].get('attempt')} is still "
            "queued to be put back"
            for tid in affected
            for st in [run.state['tasks'].get(tid) or {}] if st.get('recover')]


def apply(run,git,intent,crash=record._no_crash):
    directory=Path(run.path,intent['directory'])
    plan=record.read_json(directory/'plan.json')
    after=record.read_json(directory/'after'/'workflow.expanded.json')
    if git.run('merge-base', '--is-ancestor', plan['since'], 'HEAD', check=False).returncode:
        raise Refused('the branch moved away from the replan starting point')
    for sha in git.out('rev-list', plan['since'] + '..HEAD').splitlines():
        trailers = git.trailers(sha)
        if not trailers.get('Operation', '').startswith(intent['op'] + '/'):
            raise Refused('the branch changed during replan: unexpected commit ' + sha)
    reverted=git.revert_commits(plan['commits'],intent['op'],run.state['run_id'],plan['since'],crash=crash)
    # All reverts must finish before any definition or acceptance state changes.
    for name in ('workflow.toml','workflow.expanded.json'):
        record.write_durable(Path(run.path,name),(directory/'after'/name).read_bytes())
    for name in ('library','briefs'):
        source=directory/'after'/name
        if source.exists():
            for file in source.rglob('*'):
                if file.is_file():
                    target=Path(run.path,name,file.relative_to(source));target.parent.mkdir(parents=True,exist_ok=True)
                    record.write_durable(target,file.read_bytes())
    # The installed definitions replace the protected ones; replayed after a crash, the same bytes
    # are installed and hashed again, and nothing checks integrity before this replay.
    run.protect_definitions()
    crash('replan:definitions-installed')
    new_names=record.task_dir_names(after['tasks'])
    old_states=run.state['tasks']
    for task in after['tasks']:
        if task['id'] in plan['affected'] and task['id'] in old_states:
            _keep_way_back(run,git,task['id'],crash)
    updated={}
    for task in after['tasks']:
        tid=task['id']
        st=copy.deepcopy(old_states.get(tid,{'status':'pending','dir':new_names[tid],
            'kind':task['kind'],'type':task['type'],'attempts':0,'cost_usd':0.0,'commit':None,'reason':''}))
        if tid in plan['affected']:
            ledger=findings.restart(st['ledger']) if 'ledger' in st else None
            recover=st.get('recover')
            st={k:st[k] for k in ('dir','attempts','cost_usd')}
            if recover:st['recover']=recover
            st.update(status='pending',kind=task['kind'],type=task['type'],commit=None,reason='')
            if ledger is not None:st['ledger']=ledger
        updated[tid]=st
        task_dir=Path(run.path,'tasks',st['dir']);task_dir.mkdir(parents=True,exist_ok=True)
        record.write_durable(task_dir/'task.json',record.dump_json(task))
    retired=run.state.setdefault('retired_tasks',{})
    for tid in plan['removed_tasks']:
        retired[tid]=old_states[tid]
    run.state.update(tasks=updated,order=[t['id'] for t in after['tasks']],status='running',
                     active_producer=None,expect={'tip':git.head(),'tree':git.tree_of('HEAD')},
                     last_tip=git.head(),workflow_file=plan['source'])
    run.state.pop('stop_reason',None)
    run.write_decision(directory/'result.json',{'reverts':reverted,'affected':plan['affected'],'tip':git.head()})
    run.finish(intent['op'],reverts=reverted,affected=plan['affected'])
    run.write_findings()
    run.regenerate_safely(full=True)
    return f"{intent['op']} replan: installed the revised workflow; {len(reverted)} revert commit(s)"


def execute(run,git,directory,crash=record._no_crash):
    run.state.update(expect=None, status='running')
    op=run.begin('replan',directory=os.path.relpath(directory,run.path))
    crash('replan:intent-recorded')
    return apply(run,git,run.intent(op),crash)
