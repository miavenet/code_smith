"""Findings transitions and standing ruling input. Invalid answers never mutate the ledger.

The engine owns persistence and applies reviewers in workflow order. Versions distinguish a
finding kept open from an author response that has not yet been reviewed.
"""
import copy
import fnmatch
import hashlib
import json
import os
import re
import tomllib

from . import proc, validate

OPEN = {'open', 'disputed', 'escalated'}
REPAIR_DROPPED_RESOLUTIONS = 'dropped_resolutions'
REPAIR_DROPPED_HINTS = 'dropped_hints'


class ProtocolError(ValueError):
    pass


def empty(producer):
    return {'producer': producer, 'findings': [], 'reviewers': {}, 'next_ids': {}}


def blockers(ledger, reviewer=None):
    return [f for f in ledger['findings'] if f['severity'] == 'blocking'
            and f['status'] in OPEN and (reviewer is None or f['reviewer'] == reviewer)]


def needing_response(ledger):
    return [f for f in blockers(ledger) if f.get('response_version') != f['version']]


def feedback(ledger):
    needed = needing_response(ledger)
    ids = {f['id'] for f in needed}
    return {'needing': copy.deepcopy(needed),
            'info': copy.deepcopy([f for f in ledger['findings'] if f['id'] not in ids
                                  and (f['status'] in OPEN or f['status'] == 'noted')])}


def respond(ledger, answer, attempt):
    errors = validate.check_produce(answer, [f['id'] for f in needing_response(ledger)])
    by_id = {f['id']: f for f in ledger['findings']}
    if not errors:
        for r in answer['responses']:
            if r['action'] == 'disputed' and by_id[r['finding']].get('upheld'):
                errors.append(f"{r['finding']} was upheld by a person and cannot be disputed again")
    if errors:
        raise ProtocolError('; '.join(errors))
    result = copy.deepcopy(ledger)
    by_id = {f['id']: f for f in result['findings']}
    if answer['outcome'] != 'blocked':
        for response in answer['responses']:
            f = by_id[response['finding']]
            f['response'] = dict(response, attempt=attempt)
            f['response_version'] = f['version']
            f['status'] = 'disputed' if response['action'] == 'disputed' else 'open'
            f['history'].append(dict(event='response', **f['response']))
    return result


def note_advisories(ledger, answer, attempt, candidate):
    """Optional author reports are history, never resolutions. Replay is idempotent."""
    result = copy.deepcopy(ledger)
    by_id = {f['id']: f for f in result['findings'] if f['severity'] == 'advisory'}
    for report in answer.get('advisories_addressed', []):
        f = by_id.get(report['finding'])
        if f is None:
            continue
        event = dict(event='author_addressed', attempt=attempt, candidate=candidate, note=report['note'])
        if event not in f['history']:
            f['history'].append(event)
    return result


def owner_rulings(ledger):
    """Keep each ruling's original candidate, including rulings made before later rework."""
    return [dict(finding=f['id'], title=f['title'], **h)
            for f in (ledger or {}).get('findings', []) for h in f.get('history', [])
            if h.get('event') == 'human']


WHOLE_FILE = (0, 0)   # in a path's changed ranges: the whole file changed, so a bare path names it


def inside(location, changes):
    """Locations are path or path:line[-line]. Unknown sections are not evidence of causality.
    A bare path is inside only when the whole file changed (added, deleted, binary); a path in a
    record saved before `WHOLE_FILE` existed qualifies when it has no line ranges."""
    match = re.fullmatch(r'(.+?):(?:L)?(\d+)(?:-(?:L)?(\d+))?', location)
    if match:
        path, start, end = match.groups()
        start, end = int(start), int(end or start)
        return start > 0 and end >= start and any(start <= hi and end >= lo
                                                 for lo, hi in changes.get(path, []))
    if location not in changes:
        return False
    ranges = [tuple(r) for r in changes[location]]
    return not ranges or WHOLE_FILE in ranges


def _dropped_text(text):
    """Agent text bound for the repair record: redacted, then cut to 200 characters."""
    text = proc.redact(text.encode('utf-8', 'surrogateescape')).decode('utf-8', 'surrogateescape')
    return text if len(text) <= 200 else text[:200] + '…'


def _next_repair(ledger):
    """The discriminator that tells one repaired answer from the next. It is written once onto
    every `repair` event of the answer it belongs to and never rewritten, so the findings of one
    answer share it and no two answers do. It is counted from the repair events the ledger already
    holds rather than from the reviewer's entry, because `restart` clears the reviewers on a retry
    while the histories survive: two repairs either side of a retry are both round 1 and would
    otherwise be indistinguishable. An event written before this field existed counts as 0."""
    seen = [h.get('answer', 0) for f in ledger['findings'] for h in f['history']
            if h['event'] == 'repair']
    return max(seen, default=0) + 1


def apply_review(ledger, reviewer, answer, candidate, changes, findings_cap_bytes=60000):
    """Returns (ledger, verdict, repair). `repair` is None, or the record of the one repair
    this function is allowed to make. Atomic as before: on ProtocolError the caller's ledger is
    untouched, because every mutation happens on a deep copy that is then discarded."""
    errors = validate.check_shape(answer, validate.REVIEW)
    if errors:
        raise ProtocolError('; '.join(errors))
    answer, hints = _drop_hints(answer, findings_cap_bytes)
    rid = reviewer['id']
    required = {f['id'] for f in blockers(ledger, rid)}
    supplied = [r['finding'] for r in answer['resolutions']]
    if len(supplied) != len(set(supplied)) or set(supplied) != required:
        error = ProtocolError('resolutions must cover exactly this reviewer\'s open blocking findings, each once'
                              + ': required ' + (', '.join(sorted(required)) or 'none, so resolutions must be []')
                              + '; supplied ' + (', '.join(map(repr, supplied)) or 'none')
                              + '. A new finding belongs in findings only, never in resolutions')
        # The one repair: [] is provably the only correct value, no entry names any ledger id
        # (never a similarity test), and the answer still derives a block, so it cannot pass.
        known = {f['id'] for f in ledger['findings']}
        if required or not supplied or any(fid in known for fid in supplied):
            raise error
        try:
            result, verdict = _apply(ledger, reviewer, dict(answer, resolutions=[]), candidate, changes)
        except ProtocolError:
            raise error from None
        if verdict != 'block':
            raise error
        repair = {'kind': REPAIR_DROPPED_RESOLUTIONS,
                  'dropped': [{'finding': _dropped_text(r['finding']), 'status': r['status']}
                              for r in answer['resolutions']],
                  'why': 'the round required no resolutions and no entry named a finding in the ledger'}
        dropped_titles = [d['finding'] for d in repair['dropped']]
        number = _next_repair(ledger)
        raised = result['findings'][len(result['findings']) - len(answer['findings']):]
        for f in raised:
            f['history'].append({'event': 'repair', 'round': f['history'][-1]['round'],
                                 'answer': number, 'kind': repair['kind'], 'dropped': dropped_titles})
        return result, verdict, _with_hints(repair, hints)
    result, verdict = _apply(ledger, reviewer, answer, candidate, changes)
    return result, verdict, _with_hints(None, hints)


def _drop_hints(answer, findings_cap_bytes):
    """(answer, hints): the answer without a `gateable` hint on a finding not reported blocking
    and with `reach_audit` cut to the entries that fit in `findings_cap_bytes`, and what was
    dropped. The answer is a copy when anything was."""
    hints = [{'field': 'gateable', 'finding': _dropped_text(f['title']),
              'command_hint': _dropped_text(f['gateable']['command_hint'])}
             for f in answer['findings'] if 'gateable' in f and f['severity'] != 'blocking']
    audit = answer.get('reach_audit')
    size = len(json.dumps(audit, ensure_ascii=False).encode('utf-8')) if audit is not None else 0
    if not hints and size <= findings_cap_bytes:
        return answer, []
    answer = copy.deepcopy(answer)
    for f in answer['findings']:
        if 'gateable' in f and f['severity'] != 'blocking':
            del f['gateable']
    if size > findings_cap_bytes:
        # Each entry is serialized once: a list's JSON is "[", its entries joined by ", ", "]",
        # so the size of every prefix is a running sum, and the work is linear in the audit
        # however many entries a reviewer sends (FND-50).
        total, fits = 2, 0
        for n, entry in enumerate(audit):
            total += len(json.dumps(entry, ensure_ascii=False).encode('utf-8')) + (2 if n else 0)
            if total > findings_cap_bytes:
                break
            fits = n + 1
        kept = list(audit[:fits])
        answer['reach_audit'] = kept
        hints.append({'field': 'reach_audit', 'kept': len(kept), 'dropped': len(audit) - len(kept),
                      'bytes': size, 'cap': findings_cap_bytes})
    return answer, hints


def _with_hints(repair, hints):
    """An optional hint never changes the verdict: one out of place is dropped and recorded as a
    repair, never a rejection that costs the reviewer a try (review V-06)."""
    if not hints:
        return repair
    return dict(repair or {'kind': REPAIR_DROPPED_HINTS, 'dropped': [],
                           'why': 'an optional hint was out of place; the verdict does not depend on it'},
                hints=hints)


def _apply(ledger, reviewer, answer, candidate, changes):
    """The ledger transition for an answer whose resolutions match the required set."""
    rid = reviewer['id']
    result = copy.deepcopy(ledger)
    previous = result['reviewers'].get(rid, {})
    round_no = previous.get('round', 0) + 1
    by_id = {f['id']: f for f in result['findings']}
    for resolution in answer['resolutions']:
        f = by_id[resolution['finding']]
        if resolution['status'] == 'resolved':
            f['status'] = 'resolved'
        elif f.get('response', {}).get('action') == 'disputed':
            f['status'] = 'escalated'
        else:
            f['status'] = 'open'
        f['version'] += 1
        f['history'].append(dict(event='resolution', round=round_no, **resolution))
    outside_rework = []
    for finding in answer['findings']:
        severity = finding['severity']
        if reviewer.get('advisory') or (round_no > 1 and severity == 'blocking'
                and not inside(finding['location'], changes)
                and not inside(finding['caused_by'], changes)):
            severity = 'advisory'
            if not reviewer.get('advisory'):
                outside_rework.append({'location': finding['location'], 'caused_by': finding['caused_by']})
        code = reviewer['persona_code']
        number = result['next_ids'].get(code, 0) + 1
        result['next_ids'][code] = number
        f = dict(finding, id=f"{result['producer']}/{code}-{number}", reviewer=rid,
                 severity=severity, status='open' if severity == 'blocking' else 'noted',
                 version=1, history=[{'event': 'raised', 'round': round_no, 'severity': severity,
                                     'candidate': candidate, 'reported': copy.deepcopy(finding),
                                     **({'seat': 'advisory'} if reviewer.get('advisory') else {})}])
        result['findings'].append(f)
    verdict = 'block' if blockers(result, rid) else 'pass'
    if answer['verdict'] != verdict:
        detail = ''
        if outside_rework:
            detail = (f"; new blockers did not match changed lines: {outside_rework!r}. "
                      "Use an exact repository-relative path:line or path:line-line in location "
                      "or caused_by, without section names, suffixes or explanatory prose. "
                      "Put explanations in detail. If unrelated to the rework, report advisory; "
                      "do not downgrade a regression just to obtain pass.")
        raise ProtocolError(f"verdict disagrees with ledger: expected {verdict}" + detail)
    result['reviewers'][rid] = {'round': round_no, 'last_seen_candidate': candidate,
                               'verdict': verdict}
    if 'reach_audit' in answer:
        result['reviewers'][rid]['reach_audit'] = copy.deepcopy(answer['reach_audit'])
    return result, verdict


def resolve(ledger, fid, decision, note='', who='', candidate=None, at=None, how=None):
    """A person's ruling. `how` says how the name was given and where the command ran
    (`by_source`, `interactive`, `agent_markers`; review V-05 of the v4 runs)."""
    result = copy.deepcopy(ledger)
    f = next((f for f in result['findings'] if f['id'] == fid), None)
    if f is None or f not in blockers(result):
        raise ValueError(f'{fid} is not an open blocking finding')
    if decision not in ('resolved', 'advisory', 'upheld'):
        raise ValueError('decision must be resolved, advisory or upheld')
    # `was`: the severity and status the ruling ended, so counts of the past survive it (RUN-47)
    was = {'severity': f['severity'], 'status': f['status']}
    f['status'] = {'resolved': 'resolved', 'advisory': 'noted', 'upheld': 'open'}[decision]
    if decision == 'advisory':
        f['severity'] = 'advisory'
    if decision == 'upheld':
        f['upheld'] = True
        f['version'] += 1
    f['history'].append({'event': 'human', 'decision': decision, 'was': was, 'note': note,
                         'by': who, **(how or {}), 'candidate': candidate, 'at': at})
    return result


def restart(ledger):
    result = copy.deepcopy(ledger)
    for f in blockers(result):
        f['status'] = 'superseded'
        f['history'].append({'event': 'retry', 'status': 'superseded'})
    result['reviewers'] = {}
    return result


def overlapping(a, b):
    """Exact paths and inclusive line ranges; a bare path names the whole file."""
    def parts(location):
        match = re.fullmatch(r'(.+?):L?(\d+)(?:-L?(\d+))?', location)
        if not match:
            return location, 1, float('inf')
        path, lo, hi = match.groups()
        return path, int(lo), int(hi or lo)
    ap, alo, ahi = parts(a)
    bp, blo, bhi = parts(b)
    return bool(ap) and ap == bp and 0 < alo <= ahi and 0 < blo <= bhi and alo <= bhi and blo <= ahi


def reported_severity(raised):
    """The severity the reviewer reported in a `raised` event, not the one the runner set (an
    advisory seat, a blocker outside the rework); a record without `reported` has only the set one."""
    return (raised.get('reported') or {}).get('severity', raised.get('severity'))


def disagreements(items):
    """Pairs raised on the same candidate and attempt, with different reported severities
    (review V-10). An advisory seat reports every finding advisory by its mode, so it never
    disagrees. A later owner ruling must not manufacture a reviewer disagreement."""
    result = []
    for n, a in enumerate(items):
        ah = next((h for h in a.get('history', []) if h.get('event') == 'raised'), {})
        for b in items[n + 1:]:
            bh = next((h for h in b.get('history', []) if h.get('event') == 'raised'), {})
            if (ah and bh and a.get('reviewer') != b.get('reviewer')
                    and 'advisory' not in (ah.get('seat'), bh.get('seat'))
                    and (ah.get('attempt', ah.get('round')), ah.get('candidate')) ==
                        (bh.get('attempt', bh.get('round')), bh.get('candidate'))
                    and reported_severity(ah) != reported_severity(bh)
                    and overlapping(a.get('location', ''), b.get('location', ''))):
                result.append((a, b))
    return result


def disagreement_note(f):
    raised = next(h for h in f['history'] if h['event'] == 'raised')
    return (f"also raised by {f['reviewer'].split('.review.')[-1]} as "
            f"{reported_severity(raised)} {f['id'].split('/')[-1]}")


# The parts of a task's effective brief (V-07 of the second K–N review), and how STATUS names
# each when a standing ruling is dropped because that part changed.
BRIEF_PARTS = {'prompt': 'prompt', 'rules': 'project rules', 'gates': 'gates',
               'outputs': 'outputs', 'inputs': 'inputs'}


def effective_brief(task, prompt, rules, tasks):
    """What a task asks, as a standing ruling is bound to it: the prompt, the project rules, the
    gates, the declared outputs, and the upstream tasks by id with their declared outputs. Trees,
    summaries and accepted commits are left out: they differ in every run."""
    return {'prompt': prompt, 'rules': rules,
            'gates': [{k: v for k, v in g.items() if not k.startswith('_')} for g in task.get('gates', [])],
            'outputs': sorted(o['path'] for o in task.get('outputs', [])),
            'inputs': {n: sorted(o['path'] for o in tasks[n].get('outputs', []))
                       for n in task.get('needs', [])}}


def _digest(value):
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def brief_hash(effective):
    return _digest(effective)


def brief_parts(effective):
    return {part: _digest(effective[part]) for part in BRIEF_PARTS}


def rulings_path(top, name):
    if not name or os.path.isabs(name):
        raise ValueError('rulings_file must be a nonempty repository-relative path')
    if any(c in name for c in '*?[]{}'):
        raise ValueError('rulings_file must be a plain path, without glob characters')
    path = os.path.realpath(os.path.join(top, name))
    if os.path.commonpath((os.path.realpath(top), path)) != os.path.realpath(top):
        raise ValueError('rulings_file must stay inside the repository')
    return path


def symlink_on_path(top, name):
    """The first part of `name` (the file or a directory on the way) that is a symbolic link in
    the work tree at `top`, or None. The rulings file is owner input: a link there lets a writer
    of the link send the export to a file no protection covers (RUN-79)."""
    parts = [p for p in name.replace(os.sep, '/').split('/') if p not in ('', '.')]
    for n in range(1, len(parts) + 1):
        rel = '/'.join(parts[:n])
        if os.path.islink(os.path.join(top, rel)):
            return rel
    return None


# Keys of a queued standing entry that belong to the run, never to the file: `written` (the
# export is done) and `file` (the rulings file it was bound to when it was queued, RUN-76).
QUEUED_ONLY = ('written', 'file')


def exported_files(state, default=None):
    """The rulings files a run already wrote queued entries into (`written`), each by the file it
    was bound to (`file`, or `default` for an entry queued before it was bound): the only paths
    the owner's commit of an export changes, which `resume` and `replan` take (RUN-81)."""
    names = {e.get('file') or default for e in state.get('standing_pending', []) if e.get('written')}
    return {os.path.normpath(n).replace(os.sep, '/') for n in names if n}


def exported_entry(entry):
    """A queued standing entry as the export writes it."""
    return {k: v for k, v in entry.items() if k not in QUEUED_ONLY}


def check_entry(entry):
    """Raise ValueError, naming the field, when the rulings file's own reader would refuse the
    entry once written: the writer runs its output through that reader (RUN-78)."""
    parse_rulings({'ruling': [exported_entry(entry)]})


# The provenance a ruling carries (REC-49), kept in an exported standing entry too (V-03).
PROVENANCE = {'by_source': {'type': 'string', 'enum': ['flag', 'env']},
              'interactive': {'type': 'boolean'},
              'agent_markers': {'type': 'array', 'items': {'type': 'string'}}}


def parse_rulings(data):
    """The `[[ruling]]` entries of a parsed rulings file; ValueError names what is wrong."""
    required = ('task', 'finding_title', 'location_glob', 'decision', 'note', 'ruled_at', 'by')
    properties = {k: {'type': 'string'} for k in (*required, 'brief_sha256', 'run', 'finding')}
    properties.update(PROVENANCE, brief_parts={'type': 'object', 'required': list(BRIEF_PARTS),
                                               'properties': {p: {'type': 'string'} for p in BRIEF_PARTS}})
    schema = {'type': 'object', 'required': [], 'properties': {'ruling': {
        'type': 'array', 'items': {'type': 'object', 'required': list(required),
                                   'properties': properties}}}}
    errors = validate.check_shape(data, schema, 'rulings_file')
    if not errors:
        for item in data.get('ruling', []):
            if item['decision'] not in ('advisory', 'resolved'):
                errors.append('standing ruling decision must be advisory or resolved')
            empty = [k for k in required if not item[k].strip()]
            if empty:
                errors.append('standing ruling fields must not be empty: ' + ', '.join(empty))
            digests = [item.get('brief_sha256', '0' * 64), *item.get('brief_parts', {}).values()]
            if not all(re.fullmatch('[0-9a-f]{64}', d) for d in digests):
                errors.append('brief_sha256 and brief_parts must be SHA-256 hex digests')
    if errors:
        raise ValueError('; '.join(errors))
    return data.get('ruling', [])


def read_rulings(path):
    """A missing file is an empty list, so the first export can create it."""
    try:
        with open(path, 'rb') as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        return []
    return parse_rulings(data)


def ruling_toml(entry):
    """One `[[ruling]]` table, as the export appends it."""
    def value(v):
        if isinstance(v, dict):
            return '{ ' + ', '.join(f'{k} = {value(x)}' for k, x in v.items()) + ' }'
        return json.dumps(v, ensure_ascii=False)
    return '[[ruling]]\n' + ''.join(f'{k} = {value(v)}\n' for k, v in entry.items())


def same_ruling(a, b):
    """An exported entry is known by the run, finding and moment of its ruling: an export
    repeated after a crash finds it in the file and does not append it again."""
    return all(a.get(k) == b.get(k) for k in ('run', 'finding', 'ruled_at'))


def provenance(entry):
    """"non-interactive, name from the environment; agent session markers: CLAUDECODE" of a
    standing ruling, in the words STATUS uses for an in-run ruling; "provenance unknown" for an
    entry written by hand or exported before provenance travelled with it."""
    if 'by_source' not in entry:
        return 'provenance unknown'
    parts = ['interactive' if entry.get('interactive') else 'non-interactive',
             'name given with --by' if entry['by_source'] == 'flag' else 'name from the environment']
    markers = entry.get('agent_markers') or []
    return ', '.join(parts) + (f"; agent session markers: {', '.join(markers)}" if markers else '')


def standing_rulings(entries, task, effective, known=(), require_interactive=False):
    """(supplied, dropped reasons) of the standing entries of `task`. An entry without a brief
    hash was written by hand to cover any brief, and is supplied. With `require_interactive`
    (the workflow's `rulings_require_interactive`) only an entry recorded as ruled at a terminal
    with no agent-session variable is supplied (RUN-74): the committed file is a ruling path
    like `resolve`, and the same speed bump applies, not authentication. An entry exported by
    the runner before the effective brief (`brief_sha256` the hash of the prompt alone, no
    `brief_parts`) is dropped as such, since its brief may not have changed (RUN-75)."""
    supplied, dropped = [], []
    digest = brief_hash(effective)
    for entry in entries:
        if entry['task'] != task:
            continue
        locations = [f['location'] for f in known
                     if f['title'] == entry['finding_title'] and f.get('location')]
        if locations and not any(fnmatch.fnmatchcase(loc, entry['location_glob']) for loc in locations):
            continue
        if require_interactive and (entry.get('interactive') is not True or entry.get('agent_markers')):
            dropped.append('not ruled at a terminal')
        elif ('brief_parts' not in entry and entry.get('brief_sha256') ==
                hashlib.sha256(effective['prompt'].encode('utf-8')).hexdigest()):
            dropped.append('exported by an older runner; rule it again')
        elif entry.get('brief_sha256', digest) == digest:
            supplied.append(dict(entry, standing=True, provenance=provenance(entry)))
        elif 'brief_parts' in entry:
            parts = brief_parts(effective)
            changed = [name for part, name in BRIEF_PARTS.items()
                       if entry['brief_parts'][part] != parts[part]]
            dropped.append('the brief changed (' + (', '.join(changed) or 'as a whole') + ')')
        else:
            dropped.append('the brief changed')
    return supplied, dropped


def lab_followups(ledger):
    items = []
    for f in (ledger or {}).get('findings', []):
        if f.get('gateable'):
            rounds = sorted({h['round'] for h in f['history']
                             if h.get('event') in ('raised', 'resolution') and 'round' in h})
            items.append(dict(kind='gateable', finding=f['id'], title=f['title'],
                              command_hint=f['gateable']['command_hint'], rounds=rounds))
        for h in f.get('history', []):
            if h.get('event') == 'human' and 'the brief will say so' in h.get('note', '').lower():
                items.append(dict(kind='brief', finding=f['id'], title=f['title'], ruling=dict(h)))
    return items


def followups_json(state):
    return json.dumps({'tasks': {tid: lab_followups(t.get('ledger'))
                                for tid, t in state['tasks'].items() if lab_followups(t.get('ledger'))}},
                      ensure_ascii=False, indent=2) + '\n'
