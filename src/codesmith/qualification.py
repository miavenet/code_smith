"""Observed capabilities and their durable cache. Probes run only in scratch repositories.

The profile, model, read-only mode, binary contents/version and host identity form the cache key.
An answer or event claiming tool use grants no capability: each probe has its own observed effect.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import platform
import secrets
import shutil
import subprocess
import tempfile
import uuid

from . import agents, budgets, gitops, proc, record, validate, activity

PROBE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["value"],
                "properties": {"value": {"type": "string"}}}
CACHE_VERSION = 2
CAPABILITIES = ("answer", "read", "execute", "write", "resume", "boundary")


def host_identity():
    try:
        machine = Path('/etc/machine-id').read_text().strip()
    except OSError:
        machine = platform.node()
    return hashlib.sha256((machine + '|' + platform.system() + '|' + platform.release()
                           + '|' + platform.machine()).encode()).hexdigest()


def effective_profile(profile, root):
    profile = dict(profile)
    argv = list(profile.get('argv') or [profile['kind']])
    for n, arg in enumerate(argv):
        path = os.path.join(root, arg)
        if os.path.isfile(path):
            argv[n] = os.path.abspath(path)
    profile['argv'] = argv
    return profile


def fingerprint(profile, model, read_only, root):
    # Prices only turn observed tokens into an estimate; they change nothing a probe observes,
    # so adding or changing them never asks for a new qualification.
    profile = {k: v for k, v in effective_profile(profile, root).items()
               if k not in ('price_per_mtok', 'estimated_counts')}
    argv = profile['argv']
    binary = shutil.which(argv[0]) or argv[0]
    files = {}
    for arg in [binary] + argv[1:]:
        if os.path.isfile(arg):
            files[os.path.realpath(arg)] = record.sha256_file(arg)
    version = 'command (identified by executable and argument-file hashes)'
    if profile['kind'] in ('claude', 'codex', 'copilot'):
        try:
            res = subprocess.run(argv + ['--version'], stdin=subprocess.DEVNULL,
                                 capture_output=True, timeout=5, check=False)
            version = proc.redact((res.stdout + res.stderr)[:4000]).decode('utf-8', errors='replace')
        except (OSError, subprocess.TimeoutExpired) as exc:
            version = str(exc)
    config_hashes = {}
    candidates = []
    if profile['kind'] == 'codex':
        if not profile.get('ignore_user_config'):
            candidates.append(Path(os.environ.get('CODEX_HOME', str(Path.home()/'.codex'))) / 'config.toml')
        candidates.append(Path(root)/'.codex'/'config.toml')
    elif profile['kind'] == 'claude':
        if not profile.get('ignore_user_config'):
            candidates.append(Path.home()/'.claude'/'settings.json')
        candidates.extend([Path(root)/'.claude'/'settings.json', Path(root)/'.claude'/'settings.local.json'])
    elif profile['kind'] == 'copilot':
        # User settings (model, effort) and MCP servers; config.json is login state and is left out.
        home = Path(os.environ.get('COPILOT_HOME', str(Path.home()/'.copilot')))
        candidates.extend([home/'settings.json', home/'mcp-config.json'])
    candidates.extend(activity.assets(root, profile["kind"]))
    for path in candidates:
        if path.is_file():
            config_hashes[str(path)] = record.sha256_file(path)
    metadata = {'cache_version': CACHE_VERSION, 'profile': profile, 'model': model,
                'read_only': read_only, 'host': host_identity(), 'version': version, 'root': os.path.abspath(root),
                'binaries': files, 'config_hashes': config_hashes, 'capabilities': list(CAPABILITIES),
                'adapter_sha256': record.sha256_file(agents.__file__),
                'observer_sha256': record.sha256_file(activity.__file__)}
    key = hashlib.sha256(record.dump_json(metadata)).hexdigest()
    return key, metadata


def empty_spend():
    return {'known_usd': 0.0, 'unpriced': {'calls': 0, 'unknown_calls': 0,
                                        'tokens_in': 0, 'tokens_out': 0}}


def add_spend(spend, result, held=None):
    """Add one probe's spend. `held` is None for `doctor` on its own, which has no run: an
    environment failure without usage counts nothing, as it always has. A probe for a run passes
    `held` (the dollars and tokens its call was held to, `RunBudget.hold`) and is settled by the
    one evidence rule of the run's calls (PROC-31): nothing is spent only with positive evidence
    that the provider refused before any work (`before_work`) and no usage; otherwise a call
    without a price keeps its dollar reservation as unsettled spend, and one of an agent that
    reports no cost counts its token reservation against the stop line while its usage stays
    unknown (`budgets.settle`)."""
    if result.cost_usd is not None:
        spend['known_usd'] += result.cost_usd
        return
    if held is None:
        if result.status != agents.ENVIRONMENT or result.usage:    # observed usage is kept
            _unpriced(spend, result)
        return
    if (result.status in (agents.ENVIRONMENT, agents.QUOTA, agents.TRANSIENT)
            and result.before_work and not result.usage):
        return                                       # refused before any work: nothing was spent
    usd, tokens = held
    if usd > 0:
        unsettled = spend.setdefault('unsettled', {'usd': 0.0, 'calls': 0, 'tokens_in': 0,
                                                   'tokens_out': 0})
        unsettled['usd'] = round(unsettled['usd'] + usd, 6)
        unsettled['calls'] += 1
        unsettled['tokens_in'] += int(result.usage.get('tokens_in', 0))
        unsettled['tokens_out'] += int(result.usage.get('tokens_out', 0))
        return
    _unpriced(spend, result)
    if not result.usage and tokens > 0:
        unpriced = spend['unpriced']
        unpriced['counted_calls'] = int(unpriced.get('counted_calls', 0)) + 1
        unpriced['counted_tokens'] = int(unpriced.get('counted_tokens', 0)) + int(tokens)


def _unpriced(spend, result):
    unpriced = spend['unpriced']
    unpriced['calls'] += 1
    unpriced['unknown_calls'] += not bool(result.usage)
    unpriced['tokens_in'] += result.usage.get('tokens_in', 0)
    unpriced['tokens_out'] += result.usage.get('tokens_out', 0)


def merge_spend(into, spend):
    """Add a probe report's spend to a run's or a report's: known dollars, the unpriced counters
    (the held ones included) and unsettled dollars."""
    into['known_usd'] += spend['known_usd']
    for field, value in spend['unpriced'].items():
        into['unpriced'][field] = into['unpriced'].get(field, 0) + value
    if spend.get('unsettled'):
        unsettled = into.setdefault('unsettled', {'usd': 0.0, 'calls': 0, 'tokens_in': 0,
                                                  'tokens_out': 0})
        for field, value in spend['unsettled'].items():
            unsettled[field] = round(unsettled.get(field, 0) + value, 6)


BUDGET = "budget"             # a probe not made: the run's limits could not cover it


class RunBudget:
    """A run's limits applied to qualification. Probes for a run (`start`, and `resume`
    when a fingerprint changed) are calls like any other: before each one the dollar limit must
    cover its reservation and, for an agent that reports no cost, the token stop line must not be
    reached, so at most one probe crosses the line. Their spend is added here as each one ends.
    `doctor` on its own has no run and qualifies without one."""

    def __init__(self, run_budget_usd, run_budget_tokens=0, spend=None, call_reserve=0,
                 probe_reserve=budgets.PROBE_RESERVE_TOKENS):
        spend = copy.deepcopy(spend) if spend else {}
        spend.setdefault('known_usd', 0.0)
        spend.setdefault('reserved_usd', 0.0)
        spend.setdefault('unpriced', empty_spend()['unpriced'])
        self.state = {'run_budget_usd': float(run_budget_usd),
                      'run_budget_tokens': int(run_budget_tokens or 0), 'spend': spend,
                      'unpriced_call_reserve': call_reserve}
        self.probe_reserve = probe_reserve
        self.stop = None                     # the budgets.Exhausted that ended qualification

    @classmethod
    def of_run(cls, state, probe_reserve=budgets.PROBE_RESERVE_TOKENS):
        return cls(state['run_budget_usd'], state.get('run_budget_tokens', 0), state['spend'],
                   state.get('unpriced_call_reserve', 0), probe_reserve)

    def check(self, agent, amount):
        reservation = amount if agent.reports_cost else 0.0
        if not budgets.fits(self.state, reservation):
            raise budgets.Exhausted(f"budget cannot cover the next qualification probe (${reservation:g})")
        if not budgets.fits_tokens(self.state, agent):
            raise budgets.token_stop(self.state, "the next qualification probe")

    def hold(self, agent, amount):
        """(dollars, tokens) a probe that is about to start is held to, as a run's call is: its
        dollar cap for an agent that reports cost, else the probe-sized token bound (BUD-24),
        whatever bound the run's own calls have."""
        return (budgets.cap_for(agent, {'budget_usd': amount}),
                budgets.probe_reservation(self.state, agent, self.probe_reserve))

    def charge(self, result, held=(0.0, 0)):
        add_spend(self.state['spend'], result, held)


def qualify(name, metadata, directory, timeout_s=60, budget_usd=1, guard=None):
    profile, model, read_only = metadata['profile'], metadata['model'], metadata['read_only']
    agent = agents.make(name, profile)
    capabilities, probes = [], {}
    spend = empty_spend()
    os.makedirs(directory)
    with tempfile.TemporaryDirectory(prefix='code-smith-doctor-') as tmp:
        root = Path(tmp)
        # Qualify with the workflow's project settings as well as the inherited user settings.
        # Store only hashes in metadata; configuration contents never enter logs or prompts.
        project = Path(metadata['root'])
        for source in activity.assets(project, profile['kind']):
            relative = source.relative_to(project)
            if source.is_file():
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(source.read_bytes())
        subprocess.run(['git', '-c', 'core.hooksPath=/dev/null', 'init', '-q', tmp], check=True)
        sequence = 0
        unavailable = None
        cleanups = []

        def call(probe, instruction, *, session_id=None, **data):
            nonlocal sequence, unavailable
            if unavailable is not None:
                return agents.AgentResult(unavailable[0],
                    error=("not probed: " if unavailable[0] == BUDGET else
                           "not probed after provider failure: ") + unavailable[1]), None
            held = None
            if guard is not None:
                try:
                    guard.check(agent, budget_usd)
                except budgets.Exhausted as exc:
                    guard.stop = guard.stop or exc
                    unavailable = BUDGET, str(exc)
                    return agents.AgentResult(BUDGET, error=str(exc)), None
                held = guard.hold(agent, budget_usd)
            sequence += 1
            inv = os.path.join(directory, f'invocation-{sequence}')
            os.mkdir(inv)
            prompt = json.dumps({'code_smith_probe': probe, 'instruction': instruction,
                                 'answer_format': {'value': 'string'}, **data})
            record.write_durable(os.path.join(inv, 'prompt.md'), prompt.encode())
            result = agent.run(prompt, cwd=tmp, invocation_dir=inv, schema=PROBE_SCHEMA,
                               session_id=session_id, model=model, timeout_s=timeout_s,
                               budget_usd=budget_usd, read_only=read_only,
                               env=agents.agent_env(os.environ, 'doctor', name))
            add_spend(spend, result, held)
            if guard is not None:
                guard.charge(result, held)
            if result.cleanup and result.cleanup.get('status') == 'open':
                # The probe's own group outlived its stop: it may still be running and spending.
                # A problem naming the group, for a run and for `doctor` on its own, never a
                # cached qualification (PROC-31).
                cleanups.append({'probe': probe, 'invocation': inv, **result.cleanup})
            if result.status in (agents.ENVIRONMENT, agents.QUOTA):
                unavailable = result.status, result.error
            record.write_durable(os.path.join(inv, 'outcome.json'), record.dump_json(result.outcome()))
            valid = result.status == agents.OK and not validate.check_shape(result.structured, PROBE_SCHEMA)
            return result, result.structured['value'] if valid else None

        def observe(cap, result, passed):
            probes[cap] = {'passed': bool(passed), 'status': result.status, 'error': result.error}
            if passed:
                capabilities.append(cap)

        notes = []
        if hasattr(agent, 'preflight'):                                 # PRE-09: free, before any model call
            notes, error = agent.preflight(tmp, agents.agent_env(os.environ, 'doctor', name), read_only,
                                           timeout_s=timeout_s)
            if error:
                unavailable = agents.ENVIRONMENT, error
                probes['sandbox'] = {'passed': False, 'status': agents.ENVIRONMENT, 'error': error}
        nonce = secrets.token_hex(24)
        first, value = call('answer', 'Return exactly the value below. Remember it for a later turn.', value=nonce)
        observe('answer', first, value == nonce)
        # Do not repeatedly call a broken environment. No untested capability is granted.
        if first.status not in (agents.ENVIRONMENT, agents.QUOTA, BUDGET):
            secret = secrets.token_hex(24)
            (root / 'read-probe.txt').write_text(secret)
            result, value = call('read', 'Read the file and return its exact contents as value.', path='read-probe.txt')
            observe('read', result, value == secret)
            data, key = secrets.token_hex(32), secrets.token_hex(16)
            (root / 'execute-input.txt').write_text(data)
            (root / 'execute-probe.py').write_text(
                'import hashlib, pathlib\n'
                'data = pathlib.Path("execute-input.txt").read_bytes()\n'
                f'pathlib.Path("execute-result.txt").write_text(hashlib.sha256(data).hexdigest()+":"+{key!r})\n')
            result, value = call('execute', 'Run python3 execute-probe.py. Return value done.', command='python3 execute-probe.py')
            expected = hashlib.sha256(data.encode()).hexdigest() + ':' + key
            observe('execute', result, result.status == agents.OK and _read(root / 'execute-result.txt') == expected)
            if not read_only:
                nonce_write = secrets.token_hex(24)
                result, value = call('write', 'Write exactly the UTF-8 bytes of contents to path, with no trailing newline '
                                     'or other extra bytes. Verify the file bytes before returning value done.',
                                     path='write-probe.txt', contents=nonce_write)
                observe('write', result, result.status == agents.OK and _read(root / 'write-probe.txt') == nonce_write)
            if first.session_id and 'resume' in agent.capabilities():
                result, value = call('resume', 'Return the value you were asked to remember in the first turn.',
                                     session_id=first.session_id)
                observe('resume', result, value == nonce)
            if read_only:
                sentinel = secrets.token_hex(24)
                (root / 'sentinel.txt').write_text(sentinel)
                sentinel_mode = (root / 'sentinel.txt').stat().st_mode
                result, value = call('boundary', 'Attempt to replace sentinel.txt with CHANGED using your tools. '
                                     'If denied, report value denied. Do not merely say it is read-only.',
                                     path='sentinel.txt', contents='CHANGED')
                observe('boundary', result, result.status == agents.OK and _read(root / 'sentinel.txt') == sentinel
                        and (root / 'sentinel.txt').stat().st_mode == sentinel_mode)
    observed = sorted({str(row.get('event') or row.get('hook_event_name'))
                       for inv in Path(directory).glob('invocation-*')
                       for row in activity.read_events(inv / 'hooks', 1000)})
    result = {'capabilities': capabilities, 'probes': probes, 'spend': spend, 'metadata': metadata,
              'observed_activity': observed, 'notes': notes,
              'directory': directory, 'orphan_detection': 'strong' if platform.system() in ('Linux', 'Darwin') else 'weaker (ps fallback)'}
    if any(p.get('status') == BUDGET for p in probes.values()):
        result['budget_stop'] = True         # incomplete: never cached
    if cleanups:
        result['cleanup_open'] = cleanups    # never cached either
    if profile['kind'] == 'claude' and not observed:
        cause, message = activity.diagnose_claude_silence(metadata['root'])
        result['activity_cause'] = {'cause': cause, 'message': message}
    return result


def _read(path):
    try:
        if path.is_symlink() or not path.is_file():
            return None
        return path.read_text()
    except (OSError, UnicodeError):
        return None


def required(task, profile):
    if task['kind'] == 'review' and profile.get('review_mode') == 'provided_context':
        return {'answer'}
    return {'answer'} | set(task.get('requires', [])) | ({'boundary'} if task['kind'] == 'review' else set())


def check_workflow(wf, force=False, locked=False, guard=None):
    """Qualify every profile the workflow's agent tasks may use. With `guard` (a RunBudget) the
    probes are held to a run's limits: the first probe that does not fit ends qualification, and
    the report says so in `budget_stop` (reason and hint) with the spend of the probes made."""
    top = gitops.Git(wf.root).top
    runs = record.ensure_runs_dir(top)
    if locked:
        return _check_workflow(wf, force, guard)
    with record.Lock(runs, top=top).acquire('doctor'):
        return _check_workflow(wf, force, guard)


def _check_workflow(wf, force=False, guard=None):
    git = gitops.Git(wf.root)
    runs = record.ensure_runs_dir(git.top)
    cache_path = os.path.join(runs, 'qualification-cache.json')
    try:
        cache = record.read_json(cache_path)
    except (OSError, ValueError):
        cache = {'entries': {}}
    report = {'profiles': {}, 'tasks': {}, 'problems': [], 'spend': empty_spend()}
    report_dir = os.path.join(runs, 'doctor', str(uuid.uuid4()))
    from .providers import model_for
    for task in wf.tasks:
        if task['kind'] not in ('produce', 'review'):
            continue
        problems = []
        usable = False
        for name in [task['agent']] + task.get('fallback_agents', []):
            profile = wf.agents[name]
            model = model_for(task, name, wf.agents)
            key, metadata = fingerprint(profile, model, task['kind'] == 'review', wf.root)
            if key not in report['profiles']:
                prior = cache['entries'].get(key)
                cached = bool(not force and prior and not any(
                    probe.get('status') in (agents.QUOTA, BUDGET) for probe in prior['probes'].values()))
                entry = cache['entries'].get(key) if cached else qualify(
                    name, metadata, os.path.join(report_dir, key),
                    timeout_s=min(60, task['timeout_min'] * 60), budget_usd=min(1, task['budget_usd']),
                    guard=guard)
                if not cached:
                    if not entry.get('budget_stop') and not entry.get('cleanup_open'):
                        cache['entries'][key] = entry
                    merge_spend(report['spend'], entry['spend'])
                    report['problems'].extend(cleanup_problem(name, c)
                                              for c in entry.get('cleanup_open', []))
                report['profiles'][key] = dict(entry, cached=cached)
            entry = report['profiles'][key]
            if name == task['agent']:
                report['tasks'][task['id']] = key
            report.setdefault('alternatives', {}).setdefault(task['id'], {})[name] = key
            missing = required(task, profile) - set(entry['capabilities'])
            if missing:
                cause = '; '.join(p['error'] for p in entry['probes'].values() if p.get('error'))
                problems.append(f"'{task['type']}' needs {', '.join(sorted(missing))}; profile '{name}' "
                                          f"is qualified for {', '.join(entry['capabilities']) or 'nothing'}"
                                          + (f": {cause}" if cause else ''))
            usable = usable or not missing
            if guard is not None and guard.stop is not None:
                break
        if guard is not None and guard.stop is not None:
            report['budget_stop'] = {'reason': str(guard.stop), 'hint': guard.stop.hint}
            report['problems'].append(f"qualification stopped: {guard.stop}")
            break
        if not usable:
            report['problems'].extend(problems)
    # Repairs may use a read-only mode already qualified by doctor or a reviewer. Do not
    # spend extra probe calls merely to offer an optional protocol retry.
    for task in wf.tasks:
        if task['kind'] != 'produce':
            continue
        for name in [task['agent']] + task.get('fallback_agents', []):
            key, _ = fingerprint(wf.agents[name], model_for(task, name, wf.agents), True, wf.root)
            entry = cache['entries'].get(key)
            if entry and {'answer', 'boundary'} <= set(entry['capabilities']):
                report.setdefault('repairs', {}).setdefault(task['id'], {})[name] = key
                report['profiles'].setdefault(key, dict(entry, cached=True))
    record.write_durable(cache_path, proc.redact(record.dump_json(cache)))
    record.write_durable(os.path.join(runs, 'qualification.json'), proc.redact(record.dump_json(report)))
    return report


def cleanup_problem(name, cleanup):
    """A probe whose group could not be stopped (PROC-31): `start`, `resume` and `doctor` fail
    naming it (V-05)."""
    group = cleanup.get('group') or {}
    which = (f"process group {group['pgid']}" if group.get('pgid') else
             f"process {group['pid']}" if group.get('pid') else "its process group")
    return (f"qualification probe '{cleanup.get('probe')}' of profile '{name}' left {which} "
            f"running: {cleanup.get('error')} ({cleanup.get('invocation')}). It may still be "
            f"spending; stop it yourself (kill -TERM -<group>), then run this command again: it "
            "qualifies again")


def charge_run(run, report):
    """Add the probes' spend to the run, durably, whether or not qualification succeeded."""
    merge_spend(run.state['spend'], report['spend'])
    run.save()


def attach_run(run, report):
    run.write_decision(os.path.join(run.path, 'qualification.json'), report)
    for tid, key in report['tasks'].items():
        entry = report['profiles'][key]
        run.state['tasks'][tid]['qualification_key'] = key
        run.state['tasks'][tid]['qualified'] = entry['capabilities']
        run.state['tasks'][tid]['provider_qualifications'] = {
            name: {'key': candidate, 'capabilities': report['profiles'][candidate]['capabilities']}
            for name, candidate in report.get('alternatives', {}).get(tid, {}).items()}
        run.state['tasks'][tid]['repair_qualifications'] = report.get('repairs', {}).get(tid, {})

    info = run.info
    info['agents'] = {key: entry['metadata'] for key, entry in report['profiles'].items()}
    record.write_durable(os.path.join(run.path, 'run.json'), record.dump_json(info))
    charge_run(run, report)


def invalidate(run, task_id):
    key = run.state['tasks'][task_id].get('qualification_key')
    if not key:
        return
    path = os.path.join(record.runs_dir_for(run.info['git_toplevel']), 'qualification-cache.json')
    try:
        cache = record.read_json(path)
    except (OSError, ValueError):
        return
    cache['entries'].pop(key, None)
    record.write_durable(path, record.dump_json(cache))
    run.state['tasks'][task_id]['qualified'] = []
    run.state['needs_qualification'] = True
    run.save()
