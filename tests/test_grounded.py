"""Pinned actual sources plus semantic/adversarial extraction boundaries."""
import base64
import json
from pathlib import Path
import shutil
import unittest

from navigator.api import API, Auth
from navigator.contracts import APIError, canonical, digest
from navigator.grounded import Grounded
from navigator.store import Store


class GroundedFixture:
    def setUp(self):
        self.store = Store(':memory:')
        self.auth = Auth({
            'a': dict(tenant='A', actor='reader-A', revision=1,
                      scopes=['read', 'write', 'delete']),
            'b': dict(tenant='B', actor='reader-B', revision=1,
                      scopes=['read', 'write', 'delete']),
        })
        self.api = API(self.store, self.auth)
        self.grounded = Grounded(self.store, self.api.library)
        self.keys = 0

    def tearDown(self):
        self.store.close()

    def key(self):
        self.keys += 1
        return 'grounded-key-' + str(self.keys)

    def ingest(self, raw, media='text/plain', token='a'):
        status, result, _ = self.api.dispatch('POST', '/v1/assets', token,
            dict(name='grounded-test', media_type=media,
                 content_base64=base64.b64encode(raw).decode()), self.key())
        self.assertEqual(status, 202)
        self.assertTrue(self.api.library.run_once())
        _, job, _ = self.api.dispatch('GET', '/v1/jobs/' + result['job']['id'], token)
        self.assertEqual(job['state'], 'succeeded')
        return result['asset_id']

    def answer(self, field=None, query='maximum allowable pressure', asset=None,
               token='a', key=None, **extra):
        body = dict(query=query, limit=20, **extra)
        if field:
            body['requested_field'] = field
        if asset:
            body['filters'] = dict(asset_ids=[asset])
        ctx = self.auth.authenticate(token)
        return self.store.mutate(ctx, key or self.key(),
            'POST /v1/grounded-answers', body,
            lambda db: self.grounded.answer(db, ctx, body))

    def assert_error(self, status, code, fn):
        with self.assertRaises(APIError) as error:
            fn()
        self.assertEqual((error.exception.status, error.exception.code), (status, code))

    def assert_support(self, answer):
        self.assertEqual(answer['status'], 'supported')
        self.assertIsNone(answer['model_used'])
        self.assertIsNone(answer['generated_answer'])
        self.assertEqual(answer['mode'], 'bounded_fact_extraction')
        support = answer['claims'][0]['support_evidence']
        context = next(x for x in answer['excerpts'] if x['evidence'] == support['evidence'])
        self.assertEqual(context['quote'][support['start_char']:support['end_char']], support['quote'])
        self.assertEqual(digest(context['quote'].encode('utf-8')), support['quote_context_sha256'])
        with self.store.tx() as db:
            self.assertEqual(self.grounded.verify_saved(db, 'A', answer), answer)
            original = self.api.library.resolve(db, 'A', support['evidence'])
            self.assertEqual(original['quote'], context['quote'])


class GroundedTests(GroundedFixture, unittest.TestCase):
    def test_values_are_extracted_not_answer_table(self):
        # Different numeric values ensure the frozen original-document expected values cannot
        # be hidden behind a field-name lookup. Original unit spelling survives.
        asset = self.ingest(b'As maximum allowable pressure is 725 psi (5.00 MPa), select piping.')
        _, answer = self.answer('maximum_allowable_pressure', asset=asset)
        self.assert_support(answer)
        self.assertEqual(answer['claims'][0]['values'], [
            dict(value='725', unit='psi', operator='equals'),
            dict(value='5.00', unit='MPa', operator='equals')])
        self.assertEqual(answer['claims'][0]['value_role'], 'source_specification')

    def test_number_and_unit_alone_do_not_support_wait_relation(self):
        for source in (
                'Always wait for at least 9 minutes before turning off the power.',
                'Do not turn off the power immediately after stopping operation. '
                'Always wait for at most 9 minutes before turning off the power.',
                'Do not turn off the power immediately after stopping operation. '
                'Always wait for at least 9 seconds before turning off the power.',
                'Do not turn off the power immediately after stopping operation. '
                'Always wait for at least 9 minutes after turning off the power.'):
            with self.subTest(source=source):
                asset = self.ingest(source.encode())
                _, answer = self.answer('power_off_wait', query='turn off the power', asset=asset)
                self.assertEqual(answer['status'], 'hold')
                self.assertEqual(answer['claims'], [])
        asset = self.ingest(('Do not turn off the power immediately after stopping operation. '
            'Always wait for at least 9 minutes before turning off the power.').encode())
        _, answer = self.answer('power_off_wait', query='turn off the power', asset=asset)
        self.assert_support(answer)
        self.assertEqual(answer['claims'][0]['values'], [dict(value='9', unit='minutes', operator='at_least')])
        self.assertIn('after stopping operation', answer['claims'][0]['support_evidence']['quote'])
        self.assertEqual(answer['claims'][0]['relation'], 'after_stopping_operation_before_power_off')

    def test_midrail_requires_example_role_units_both_inclusive_operators(self):
        for source in ('中さんの高さは30センチメートル以上45センチメートル以下か',
                       '点検の内容例 中さんの高さは30センチメートル以上45ミリメートル以下か',
                       '点検の内容例 中さんの高さは30センチメートル未満45センチメートル以下か',
                       '点検の内容例 中さんの高さは50センチメートル以上35センチメートル以下か',
                       '点検の内容例 中さんの高さは30センチメートル以上45センチメートル以下です'):
            with self.subTest(source=source):
                asset = self.ingest(source.encode())
                _, answer = self.answer('midrail_height', query='中さん', asset=asset)
                self.assertEqual(answer['status'], 'hold')
        asset = self.ingest('点検の内容例 中さんの高さは30センチメートル以上45センチメートル以下か'.encode())
        _, answer = self.answer('midrail_height', query='中さん', asset=asset)
        self.assert_support(answer)
        self.assertEqual(answer['claims'][0]['values'], [dict(value='30', unit='センチメートル', operator='gte'),
                                                      dict(value='45', unit='センチメートル', operator='lte')])
        self.assertEqual(answer['claims'][0]['value_role'], 'inspection_example_criterion')

    def test_broad_question_and_negated_specification_hold(self):
        asset = self.ingest(b'maximum allowable pressure is 580 psi (4.0 MPa)')
        _, answer = self.answer(query='Is maximum allowable pressure safe?', asset=asset)
        self.assertEqual(answer['reason'], 'unsupported_question_scope')
        self.assertEqual(answer['claims'], [])
        for statement in ('It is incorrect that maximum allowable pressure is 580 psi (4.0 MPa)',
                          'If maximum allowable pressure is 580 psi (4.0 MPa)',
                          'maximum allowable pressure is not 580 psi (4.0 MPa)',
                          'maximum allowable pressure is 580 kPa (4.0 MPa)'):
            with self.subTest(statement=statement):
                item = self.ingest(statement.encode())
                _, held = self.answer('maximum_allowable_pressure', asset=item)
                self.assertEqual(held['status'], 'hold')

    def test_duplicate_or_conflicting_records_and_tenant_boundary(self):
        asset = self.ingest(b'maximum allowable pressure is 580 psi (4.0 MPa)\n'
                            b'maximum allowable pressure is 580 psi (4.0 MPa)')
        _, answer = self.answer('maximum_allowable_pressure', asset=asset)
        self.assertEqual(answer['reason'], 'ambiguous_evidence_scope')
        foreign = self.ingest(b'maximum allowable pressure is 900 psi (6.2 MPa)', token='b')
        _, answer = self.answer('maximum_allowable_pressure', asset=foreign)
        self.assertEqual(answer['reason'], 'no_matching_evidence_in_scope')

    def test_replayed_support_rechecks_values_operators_relation_role_and_spans(self):
        asset = self.ingest(b'maximum allowable pressure is 580 psi (4.0 MPa)')
        _, answer = self.answer('maximum_allowable_pressure', asset=asset)
        mutations = []
        for field, replacement in (('value_role', 'source_instruction'),
                                   ('relation', 'after_stopping_operation_before_power_off')):
            forged = json.loads(canonical(answer))
            forged['claims'][0][field] = replacement
            mutations.append(forged)
        for field, replacement in (('value', '999'), ('unit', 'kPa'), ('operator', 'at_least')):
            forged = json.loads(canonical(answer))
            forged['claims'][0]['values'][0][field] = replacement
            mutations.append(forged)
        for field, replacement in (('start_char', 1), ('end_char', 4), ('quote', '580 psi'),
                                   ('quote_context_sha256', '0' * 64)):
            forged = json.loads(canonical(answer))
            forged['claims'][0]['support_evidence'][field] = replacement
            mutations.append(forged)
        for forged in mutations:
            with self.subTest(claim=forged['claims'][0]), self.store.tx() as db:
                self.assert_error(409, 'grounded_answer_integrity_failed',
                    lambda: self.grounded.verify_saved(db, 'A', forged))

    def test_spoofed_search_and_caller_evidence_rejected(self):
        self.ingest(b'maximum allowable pressure is 580 psi (4.0 MPa)')
        _, search, _ = self.api.dispatch('POST', '/v1/search', 'a', dict(query='maximum allowable pressure'))
        original = self.api.library.search
        self.api.library.search = lambda *args: {**search, 'results': [{**search['results'][0], 'quote': 'maximum allowable pressure is 900 psi (6.2 MPa)'}]}
        try:
            self.assert_error(409, 'evidence_integrity_failed',
                lambda: self.answer('maximum_allowable_pressure'))
        finally:
            self.api.library.search = original
        self.assert_error(422, 'invalid_input_fields',
            lambda: self.answer('maximum_allowable_pressure', evidence=search['results'][0]['evidence']))

    def test_dependency_deletion_invalidates_fact_and_idempotency(self):
        asset = self.ingest(b'maximum allowable pressure is 580 psi (4.0 MPa)')
        key = self.key()
        first = self.answer('maximum_allowable_pressure', asset=asset, key=key)
        self.assertEqual(self.answer('maximum_allowable_pressure', asset=asset, key=key), first)
        self.api.dispatch('DELETE', '/v1/assets/' + asset, 'a', {}, self.key())
        self.assert_error(410, 'deleted_dependency',
            lambda: self.answer('maximum_allowable_pressure', asset=asset, key=key))
        with self.store.tx() as db:
            self.assert_error(410, 'deleted_dependency',
                lambda: self.store.record(db, 'A', 'grounded_answers', first[1]['id']))




if __name__ == '__main__':
    unittest.main()
