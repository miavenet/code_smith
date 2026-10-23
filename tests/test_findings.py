"""FND ledger scenarios, independent of scheduling and model calls."""
import copy
import unittest
from helpers import done
from codesmith import findings as f

R = {'id': 'review-pe', 'persona_code': 'PE', 'advisory': False}
S = {'id': 'review-sc', 'persona_code': 'SC', 'advisory': False}


def finding(location='src/a:2', severity='blocking', caused_by=''):
    return dict(severity=severity, title='Fix this', detail='Evidence', location=location, caused_by=caused_by)


def review(items=(), resolutions=(), verdict=None):
    if verdict is None:
        verdict = 'block' if any(x['severity'] == 'blocking' for x in items) or any(
            x['status'] == 'unresolved' for x in resolutions) else 'pass'
    return dict(verdict=verdict, summary='Reviewed.', findings=list(items), resolutions=list(resolutions))


def resolution(fid='make/PE-1', status='resolved'):
    return dict(finding=fid, status=status, note='Checked')


class Findings(unittest.TestCase):
    def initial(self):
        return f.apply_review(f.empty('make'), R, review([finding()]), 'C1', {})[0]

    def test_unique_ids_and_complete_history(self):
        a = self.initial()
        b, _, _repair = f.apply_review(f.empty('other'), R, review([finding()]), 'C1', {})
        self.assertNotEqual(a['findings'][0]['id'], b['findings'][0]['id'])
        a = f.respond(a, done(responses=[dict(finding='make/PE-1', action='fixed', note='Fixed')]), 2)
        a, verdict, _repair = f.apply_review(a, R, review(resolutions=[resolution()]), 'C2', {})
        self.assertEqual(verdict, 'pass')
        self.assertEqual([x['event'] for x in a['findings'][0]['history']], ['raised','response','resolution'])
        self.assertEqual(a['reviewers'][R['id']]['last_seen_candidate'], 'C2')

    def test_advisory_override_and_noted_resolutions(self):
        a, verdict, _repair = f.apply_review(f.empty('make'), dict(R, advisory=True),
                                  review([finding()], verdict='pass'), 'C1', {})
        self.assertEqual((verdict, a['findings'][0]['status']), ('pass','noted'))
        self.assertEqual(f.needing_response(a), [])
        a, _, _repair = f.apply_review(a, R, review(), 'C2', {})
        self.assertEqual(a['reviewers'][R['id']]['round'], 2)

    def test_invalid_verdict_and_resolution_are_atomic(self):
        a = self.initial(); original = copy.deepcopy(a)
        for answer in [review(), review(resolutions=[resolution(), resolution()]),
                       review(resolutions=[resolution('foreign/SC-1')]),
                       review(resolutions=[resolution(status='unresolved')], verdict='pass')]:
            with self.assertRaises(f.ProtocolError):
                f.apply_review(a, R, answer, 'C2', {})
            self.assertEqual(a, original)
        a, verdict, _repair = f.apply_review(a, R, review(resolutions=[resolution(status='unresolved')]), 'C2', {})
        self.assertEqual(verdict, 'block')

    def test_a_meaningless_resolution_is_dropped_the_finding_is_kept(self):
        """fnd: a meaningless resolution is dropped, the finding is kept (FND-21). The field
        failure: a first-round reviewer put its own new finding, by title, in resolutions."""
        ledger = f.empty('make'); original = copy.deepcopy(ledger)
        answer = review([finding()], resolutions=[dict(finding='Fix this', status='unresolved', note='n')])
        kept = copy.deepcopy(answer)
        result, verdict, repair = f.apply_review(ledger, R, answer, 'C1', {})
        self.assertEqual(verdict, 'block')
        self.assertEqual([(x['id'], x['status']) for x in f.blockers(result)], [('make/PE-1', 'open')])
        self.assertEqual([x['id'] for x in f.needing_response(result)], ['make/PE-1'])
        self.assertEqual(repair, {'kind': f.REPAIR_DROPPED_RESOLUTIONS,
                                  'dropped': [{'finding': 'Fix this', 'status': 'unresolved'}],
                                  'why': 'the round required no resolutions and no entry named '
                                         'a finding in the ledger'})
        # Exactly what the answer with resolutions [] would have produced, plus the repair's own
        # audit trail (FND-25) on the finding it raised, and nothing else moved.
        clean = copy.deepcopy(result)
        clean['findings'][0]['history'].pop()
        self.assertEqual(clean, f.apply_review(ledger, R, dict(answer, resolutions=[]), 'C1', {})[0])
        self.assertEqual((ledger, answer), (original, kept))

    def test_a_meaningless_resolution_is_redacted_and_bounded_in_the_repair_record(self):
        """fnd: a meaningless resolution is dropped, the finding is kept (FND-21, agent text)"""
        from codesmith import proc
        token = 'sk-ant-' + 'a' * 40
        dropped = token + ' ' + 'x' * 300
        answer = review([finding()], resolutions=[dict(finding=dropped, status='resolved', note='n')])
        _ledger, _verdict, repair = f.apply_review(f.empty('make'), R, answer, 'C1', {})
        text = repair['dropped'][0]['finding']
        self.assertNotIn(token, text)
        self.assertIn(proc.REDACTED.decode(), text)
        self.assertEqual(len(text), 201)
        self.assertTrue(text.endswith('…'))

    def test_a_repair_is_recorded_where_it_can_be_audited(self):
        """fnd: a repair is recorded where it can be audited (FND-25, the ledger's half)"""
        answer = review([finding()], resolutions=[dict(finding='Fix this', status='unresolved', note='n')])
        result, verdict, repair = f.apply_review(f.empty('make'), R, answer, 'C1', {})
        self.assertEqual(verdict, 'block')
        self.assertEqual([x['event'] for x in result['findings'][0]['history']], ['raised', 'repair'])
        self.assertEqual(result['findings'][0]['history'][1],
                         {'event': 'repair', 'round': 1, 'answer': 1,
                          'kind': f.REPAIR_DROPPED_RESOLUTIONS, 'dropped': ['Fix this']})

    def test_a_repaired_answer_is_told_apart_from_the_next(self):
        """fnd: a repair is recorded where it can be audited (FND-25, the answer discriminator).
        The ledger records no answer identity, and `restart` clears the reviewers' rounds, so the
        `repair` event carries a counter of its own: the findings of one answer share it, and two
        identical answers either side of a retry, both round 1, do not."""
        answer = review([finding(), dict(finding(), title='Fix that')],
                        resolutions=[dict(finding='Fix this', status='unresolved', note='n')])
        first = f.apply_review(f.empty('make'), R, answer, 'C1', {})[0]
        self.assertEqual([h['answer'] for x in first['findings'] for h in x['history']
                          if h['event'] == 'repair'], [1, 1])
        second = f.apply_review(f.restart(first), R, answer, 'C1', {})[0]
        repairs = [h for x in second['findings'] for h in x['history'] if h['event'] == 'repair']
        self.assertEqual([(h['round'], h['answer']) for h in repairs], [(1, 1), (1, 1), (1, 2), (1, 2)])
        # Write-once: the retry appends to the first answer's findings and rewrites nothing.
        self.assertEqual([x['history'][1] for x in second['findings'][:2]],
                         [x['history'][1] for x in first['findings']])

    def test_a_real_id_is_never_repaired_away(self):
        """fnd: a real id is never repaired away (FND-22). Today's diagnostic, and atomicity."""
        ledger = f.apply_review(f.empty('make'), S, review([finding()]), 'C1', {})[0]
        original = copy.deepcopy(ledger)
        for supplied in (['make/SC-1'], ['Fix this', 'make/SC-1']):
            with self.subTest(supplied=supplied):
                answer = review([finding()], resolutions=[dict(finding=fid, status='unresolved', note='n')
                                                          for fid in supplied])
                with self.assertRaises(f.ProtocolError) as ctx:
                    f.apply_review(ledger, R, answer, 'C1', {})
                self.assertIn('required none, so resolutions must be []', str(ctx.exception))
                self.assertIn("supplied " + ', '.join(map(repr, supplied)), str(ctx.exception))
                self.assertIn('A new finding belongs in findings only, never in resolutions',
                              str(ctx.exception))
                self.assertEqual(ledger, original)
        # A closed finding's id is still a real id.
        closed = copy.deepcopy(ledger); closed['findings'][0]['status'] = 'resolved'
        with self.assertRaises(f.ProtocolError):
            f.apply_review(closed, R, review([finding()], resolutions=[resolution('make/SC-1')]), 'C1', {})

    def test_no_repair_while_a_resolution_is_required(self):
        """fnd: no repair while a resolution is required (FND-23)"""
        a = self.initial(); original = copy.deepcopy(a)
        for answer in (review(resolutions=[dict(finding='Fix this', status='unresolved', note='n')]),
                       review([finding()], resolutions=[dict(finding='Fix this', status='resolved', note='n')])):
            with self.assertRaises(f.ProtocolError) as ctx:
                f.apply_review(a, R, answer, 'C2', {'src/a': [(2, 3)]})
            self.assertIn('required make/PE-1', str(ctx.exception))
            self.assertIn("supplied 'Fix this'", str(ctx.exception))
            self.assertEqual(a, original)
        with self.assertRaises(f.ProtocolError) as ctx:
            f.apply_review(self.initial(), R, review(), 'C2', {})
        self.assertIn('required make/PE-1', str(ctx.exception))

    def test_a_repair_never_produces_a_pass(self):
        """fnd: a repair never produces a pass (FND-24)"""
        junk = [dict(finding='looks fine to me', status='resolved', note='n')]
        advisory = dict(R, advisory=True)
        for reviewer, answer in [(R, review(resolutions=junk, verdict='pass')),
                                 (R, review([finding(severity='advisory')], resolutions=junk, verdict='pass')),
                                 (advisory, review([finding()], resolutions=junk, verdict='pass')),
                                 (advisory, review([finding()], resolutions=junk, verdict='block'))]:
            with self.subTest(reviewer=reviewer, answer=answer):
                ledger = f.empty('make'); original = copy.deepcopy(ledger)
                with self.assertRaises(f.ProtocolError) as ctx:
                    f.apply_review(ledger, reviewer, answer, 'C1', {})
                self.assertIn('required none, so resolutions must be []', str(ctx.exception))
                self.assertIn("supplied 'looks fine to me'", str(ctx.exception))
                self.assertEqual(ledger, original)
        # A later round whose only new blocker falls outside the rework derives a pass: no repair.
        a = f.apply_review(f.empty('make'), R, review(), 'C1', {})[0]
        with self.assertRaises(f.ProtocolError) as ctx:
            f.apply_review(a, R, review([finding('src/a:9')], resolutions=junk), 'C2', {'src/a': [(2, 3)]})
        self.assertIn('required none, so resolutions must be []', str(ctx.exception))

    def test_valid_answers_are_untouched(self):
        """fnd: valid answers are untouched (FND-26)"""
        a, verdict, repair = f.apply_review(f.empty('make'), R, review([finding()]), 'C1', {})
        self.assertEqual((verdict, repair), ('block', None))
        a = f.respond(a, done(responses=[dict(finding='make/PE-1', action='fixed', note='Fixed')]), 2)
        a, verdict, repair = f.apply_review(a, R, review(resolutions=[resolution()]), 'C2', {})
        self.assertEqual((verdict, repair), ('pass', None))
        a, verdict, repair = f.apply_review(a, S, review([finding(severity='advisory')]), 'C2', {})
        self.assertEqual((verdict, repair), ('pass', None))
        for x in a['findings']:
            self.assertNotIn('repair', [h['event'] for h in x['history']])

    def test_later_blockers_require_changed_location_or_cause(self):
        for location, cause, expected in [('src/a:2','','blocking'),('src/a:9','','advisory'),
                                          ('caller:8','src/a:2','blocking'),
                                          ('caller:8','src/a:9','advisory')]:
            with self.subTest(location=location,cause=cause):
                a, _, _repair = f.apply_review(f.empty('make'), R, review(), 'C1', {})
                a, _, _repair = f.apply_review(a, R, review([finding(location,caused_by=cause)],
                                     verdict='block' if expected=='blocking' else 'pass'),
                                     'C4', {'src/a': [(2,3)]})
                self.assertEqual(a['findings'][0]['severity'], expected)
        self.assertFalse(f.inside('src/a#made-up-section', {'src/a': [(1,2)]}))

    def test_a_bare_path_is_inside_only_a_whole_file_change(self):
        """fnd: a bare path names an added, deleted or binary file, never any touched file"""
        self.assertFalse(f.inside('src/a', {'src/a': [(2, 3)]}))
        self.assertTrue(f.inside('src/a', {'src/a': [(1, 4), f.WHOLE_FILE]}))
        self.assertTrue(f.inside('src/a', {'src/a': [[1, 4], list(f.WHOLE_FILE)]}))   # as saved in JSON
        self.assertTrue(f.inside('src/bin', {'src/bin': []}))        # a record saved before WHOLE_FILE
        self.assertFalse(f.inside('src/a:0', {'src/a': [f.WHOLE_FILE]}))
        # The later-round rule then holds for a bare-path blocker on a modified file.
        a, _, _repair = f.apply_review(f.empty('make'), R, review(), 'C1', {})
        a, _, _repair = f.apply_review(a, R, review([finding('src/a')], verdict='pass'),
                                       'C2', {'src/a': [(2, 3)]})
        self.assertEqual(a['findings'][0]['severity'], 'advisory')

    def test_malformed_rework_reference_explains_rejection_without_mutating_ledger(self):
        a = self.initial()
        original = copy.deepcopy(a)
        answer = review([finding('src/a:2 (against section 4)', caused_by='rework hunk')],
                        resolutions=[resolution()])
        with self.assertRaisesRegex(f.ProtocolError, 'without section names, suffixes'):
            f.apply_review(a, R, answer, 'C2', {'src/a': [(2, 3)]})
        self.assertEqual(a, original)
        answer['findings'][0]['location'] = 'src/a:2'
        fixed, verdict, _repair = f.apply_review(a, R, answer, 'C2', {'src/a': [(2, 3)]})
        self.assertEqual(verdict, 'block')
        self.assertEqual(fixed['findings'][-1]['severity'], 'blocking')

    def test_responses_survive_gate_failure_and_renew_on_unresolved(self):
        a = self.initial()
        a = f.respond(a, done(responses=[dict(finding='make/PE-1',action='fixed',note='fixed')]), 2)
        self.assertEqual(f.needing_response(a), [])
        self.assertEqual(len(f.feedback(a)['info']), 1)
        a = f.respond(a, done(), 3)
        a, _, _repair = f.apply_review(a,R,review(resolutions=[resolution(status='unresolved')]),'C3',{})
        self.assertEqual(len(f.needing_response(a)),1)

    def test_dispute_escalation_and_human_decisions(self):
        a = self.initial()
        a = f.respond(a, done(responses=[dict(finding='make/PE-1',action='disputed',note='why')]), 2)
        a, _, _repair = f.apply_review(a,R,review(resolutions=[resolution(status='unresolved')]),'C2',{})
        self.assertEqual(a['findings'][0]['status'],'escalated')
        for decision in ('resolved','advisory'):
            self.assertEqual(f.blockers(f.resolve(a,'make/PE-1',decision)),[])
        b = f.resolve(a,'make/PE-1','upheld')
        self.assertEqual(len(f.needing_response(b)),1)
        with self.assertRaises(f.ProtocolError):
            f.respond(b,done(responses=[dict(finding='make/PE-1',action='disputed',note='again')]),3)

    def test_retry_supersedes_and_preserves_id_sequence(self):
        a = f.restart(self.initial())
        self.assertEqual(a['findings'][0]['status'],'superseded')
        a, _, _repair = f.apply_review(a,R,review([finding()]),'C5',{})
        self.assertEqual(a['findings'][-1]['id'],'make/PE-2')
        self.assertEqual(a['reviewers'][R['id']]['round'],1)

    def test_disagreements_compare_what_reviewers_reported(self):
        """fnd: reviewer disagreements compare the severity each reviewer reported, never the one
        the runner set; an advisory seat never disagrees (FND-49)"""
        seat = dict(S, advisory=True)
        both, _, _ = f.apply_review(f.empty('make'), R, review([finding()]), 'C1', {})
        both, _, _ = f.apply_review(both, seat, review([finding()], verdict='pass'), 'C1', {})
        self.assertEqual([x['severity'] for x in both['findings']], ['blocking', 'advisory'])
        self.assertEqual(f.disagreements(both['findings']), [])
        advisory, _, _ = f.apply_review(f.empty('make'), R, review([finding()]), 'C1', {})
        advisory, _, _ = f.apply_review(advisory, seat, review([finding(severity='advisory')]), 'C1', {})
        self.assertEqual(f.disagreements(advisory['findings']), [])
        split, _, _ = f.apply_review(f.empty('make'), R, review([finding()]), 'C1', {})
        split, _, _ = f.apply_review(split, S, review([finding(severity='advisory')]), 'C1', {})
        [(_a, b)] = f.disagreements(split['findings'])
        self.assertEqual(f.disagreement_note(b), 'also raised by review-sc as advisory SC-1')
        demoted = copy.deepcopy(split)
        demoted['findings'][1]['history'][0]['reported']['severity'] = 'blocking'
        self.assertEqual(f.disagreements(demoted['findings']), [])     # set advisory, reported blocking
