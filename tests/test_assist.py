"""Assist uses actual API ingestion and deterministic worker extraction."""
import base64
import hashlib
import json
import unittest

from navigator.api import API, Auth
from navigator.assist import Assist
from navigator.contracts import APIError, canonical
from navigator.store import Store


class AssistTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.auth = Auth({
            'a': dict(tenant='A', actor='reader-A', revision=1,
                      scopes=['read', 'write', 'delete']),
            'b': dict(tenant='B', actor='reader-B', revision=1,
                      scopes=['read', 'write', 'delete']),
        })
        self.api = API(self.store, self.auth)
        self.assist = Assist(self.store, self.api.library)
        self.keys = 0

    def tearDown(self):
        self.store.close()

    def key(self):
        self.keys += 1
        return 'assist-key-' + str(self.keys)

    def ingest(self, raw, media='text/csv', token='a', **extra):
        body = dict(name='synthetic-fixture', media_type=media,
                    content_base64=base64.b64encode(raw).decode(), **extra)
        status, result, _ = self.api.dispatch('POST', '/v1/assets', token,
                                             body, self.key())
        self.assertEqual(status, 202)
        self.assertTrue(self.api.library.run_once())
        _, job, _ = self.api.dispatch('GET', '/v1/jobs/' + result['job']['id'], token)
        self.assertEqual(job['state'], 'succeeded')
        return result['asset_id']

    def answer(self, body, token='a', key=None):
        ctx = self.auth.authenticate(token)
        return self.store.mutate(ctx, key or self.key(), 'POST /v1/answers', body,
                                 lambda db: self.assist.answer(db, ctx, body))

    def assert_error(self, status, code, fn):
        with self.assertRaises(APIError) as caught:
            fn()
        self.assertEqual((caught.exception.status, caught.exception.code), (status, code))

    def test_real_csv_source_quote_and_pinned_provenance(self):
        raw = b'pressure_mpa,site\n0.68,alpha\n'
        asset = self.ingest(raw, labels=['pressure'])
        body = dict(query='pressure_mpa', filters=dict(asset_ids=[asset],
                    media_types=['text/csv'], labels=['pressure'], versions='latest'))
        status, answer = self.answer(body)
        self.assertEqual(status, 201)
        self.assertEqual(answer['status'], 'evidence_found')
        self.assertEqual(answer['filters'], body['filters'])
        self.assertEqual(answer['conclusion_support'], 'unassessed')
        self.assertIsNone(answer['generated_answer'])
        self.assertIsNone(answer['model_used'])
        excerpt = answer['excerpts'][0]
        self.assertEqual(excerpt['quote'], '0.68,alpha')
        self.assertEqual(excerpt['fields']['pressure_mpa'], '0.68')
        self.assertEqual(excerpt['locator']['line_start'], 2)
        self.assertEqual(excerpt['source_sha256'], hashlib.sha256(raw).hexdigest())
        self.assertEqual(excerpt['evidence']['source_version'], 1)
        self.assertTrue(excerpt['evidence']['analysis_version'].startswith('structured-text-v1:'))
        with self.store.tx() as db:
            self.assertEqual(self.store.record(db, 'A', 'answers', answer['id']), answer)
            self.assertEqual(self.api.library.resolve(db, 'A', excerpt['evidence'])['quote'], excerpt['quote'])

    def test_real_txt_question_does_not_generate_a_conclusion(self):
        self.ingest('圧力 0.68 MPa は記録値です。\n'.encode(), 'text/plain')
        _, answer = self.answer(dict(query='圧力は安全ですか'))
        self.assertEqual(answer['excerpts'][0]['quote'], '圧力 0.68 MPa は記録値です。')
        self.assertEqual(answer['reason'], 'source_quotes_only')
        self.assertIsNone(answer['generated_answer'])
        self.assertEqual(answer['conclusion_support'], 'unassessed')

    def test_insufficient_evidence_and_generative_unavailable(self):
        self.ingest(b'pressure_mpa\n0.68\n')
        _, answer = self.answer(dict(query='unmatchedterm'))
        self.assertEqual(answer['status'], 'insufficient_evidence')
        self.assertEqual(answer['excerpts'], [])
        self.assertIsNone(answer['model_used'])
        self.assert_error(503, 'generation_model_unavailable',
                          lambda: self.answer(dict(query='pressure_mpa', mode='generative')))

    def test_conflicting_and_identical_distinct_records_require_review(self):
        for rows in (b'0.68\n0.72\n', b'0.68\n0.68\n'):
            with self.subTest(rows=rows):
                asset = self.ingest(b'pressure_mpa\n' + rows)
                _, answer = self.answer(dict(query='pressure_mpa', filters=dict(asset_ids=[asset])))
                self.assertEqual(answer['status'], 'review_required')
                self.assertEqual(answer['reason'], 'conflicting_or_distinct_records')
                self.assertEqual(len(answer['excerpts']), 2)
                self.assertEqual(answer['assessment_scope'], 'returned_excerpts')
                self.assertIsNone(answer['generated_answer'])

    def test_versions_and_filters_are_preserved(self):
        asset = self.ingest(b'pressure_mpa\n0.68\n', labels=['original'])
        self.ingest(b'pressure_mpa\n0.72\n', asset_id=asset, labels=['updated'])
        _, current = self.answer(dict(query='pressure_mpa', filters=dict(asset_ids=[asset])))
        self.assertEqual(current['excerpts'][0]['evidence']['source_version'], 2)
        _, all_versions = self.answer(dict(query='pressure_mpa', filters=dict(asset_ids=[asset], versions='all')))
        self.assertEqual(all_versions['status'], 'review_required')
        self.assertEqual({x['evidence']['source_version'] for x in all_versions['excerpts']}, {1, 2})
        _, original = self.answer(dict(query='pressure_mpa', filters=dict(asset_ids=[asset], versions='all', labels=['original'])))
        self.assertEqual(original['excerpts'][0]['fields']['pressure_mpa'], '0.68')

    def test_tenant_search_and_saved_record_boundary(self):
        asset = self.ingest(b'pressure_mpa\n0.68\n', token='b')
        _, answer = self.answer(dict(query='pressure_mpa', filters=dict(asset_ids=[asset])))
        self.assertEqual(answer['status'], 'insufficient_evidence')
        _, own = self.answer(dict(query='pressure_mpa'), token='b')
        self.assert_error(404, 'not_found', lambda: self.api.dispatch('GET', '/v1/answers/' + own['id'], 'a'))

    def test_spoofed_search_reference_or_content_rejected_without_record(self):
        self.ingest(b'pressure_mpa\n0.68\n', token='b')
        self.ingest(b'pressure_mpa\n0.72\n')
        _, foreign, _ = self.api.dispatch('POST', '/v1/search', 'b', dict(query='pressure_mpa'))
        _, own, _ = self.api.dispatch('POST', '/v1/search', 'a', dict(query='pressure_mpa'))
        search = self.api.library.search
        try:
            for output, status, code in ((foreign, 404, 'not_found'),
                                         ({**own, 'results': [{**own['results'][0], 'quote': '0.99'}]}, 409, 'evidence_integrity_failed'),
                                         ({**own, 'results': [{**own['results'][0], 'fields': {'pressure_mpa': '0.99'}}]}, 409, 'evidence_integrity_failed')):
                with self.subTest(code=code):
                    self.api.library.search = lambda *args, output=output: output
                    self.assert_error(status, code, lambda: self.answer(dict(query='pressure_mpa')))
        finally:
            self.api.library.search = search
        with self.store.tx() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM records WHERE kind='answers'").fetchone()[0], 0)

    def test_forged_pinned_versions_and_segment_rejected(self):
        self.ingest(b'pressure_mpa\n0.68\n')
        _, actual, _ = self.api.dispatch('POST', '/v1/search', 'a', dict(query='pressure_mpa'))
        item = actual['results'][0]
        search = self.api.library.search
        try:
            for patch in (dict(source_version=2), dict(analysis_version='forged-analysis'),
                          dict(segment_id='0' * 32)):
                with self.subTest(patch=patch):
                    forged = {**item, 'evidence': {**item['evidence'], **patch}}
                    output = {**actual, 'results': [forged]}
                    self.api.library.search = lambda *args, output=output: output
                    self.assert_error(404, 'evidence_not_found',
                                      lambda: self.answer(dict(query='pressure_mpa')))
        finally:
            self.api.library.search = search
        with self.store.tx() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM records WHERE kind='answers'").fetchone()[0], 0)

    def test_public_input_cannot_supply_its_own_evidence(self):
        self.ingest(b'pressure_mpa\n0.68\n')
        _, search, _ = self.api.dispatch('POST', '/v1/search', 'a', dict(query='pressure_mpa'))
        self.assert_error(422, 'invalid_input_fields', lambda: self.api.dispatch(
            'POST', '/v1/answers', 'a', dict(query='pressure_mpa', evidence=search['results'][0]['evidence']), self.key()))

    def test_idempotency_and_deleted_dependency_cannot_restore_quotes(self):
        asset = self.ingest(b'pressure_mpa\n0.68\n')
        body, key = dict(query='pressure_mpa'), self.key()
        first = self.answer(body, key=key)
        self.assertEqual(self.answer(body, key=key), first)
        self.assert_error(409, 'idempotency_conflict',
                          lambda: self.answer(dict(query='site'), key=key))
        self.api.dispatch('DELETE', '/v1/assets/' + asset, 'a', {}, self.key())
        self.assert_error(410, 'deleted_dependency', lambda: self.answer(body, key=key))
        self.assert_error(410, 'deleted_dependency',
                          lambda: self.api.dispatch('GET', '/v1/answers/' + first[1]['id'], 'a'))
        with self.store.tx() as db:
            payload = db.execute("SELECT data FROM records WHERE kind='answers'").fetchone()[0]
            self.assertNotIn('0.68', payload)
            cached = db.execute('SELECT data FROM idempotency WHERE key=?', (key,)).fetchone()[0]
            self.assertEqual(json.loads(cached), dict(code='deleted_dependency'))
        _, later = self.answer(body)
        self.assertEqual(later['status'], 'insufficient_evidence')

    def saved_answer_mutations(self, original):
        mutations = []
        for field, replacement in (
                ('quote', '0.99'), ('fields', {'pressure_mpa': '0.99'}),
                ('locator', dict(kind='lines', line_start=1, line_end=1)),
                ('source_sha256', '0' * 64), ('id', '0' * 32)):
            value = json.loads(canonical(original))
            value['excerpts'][0][field] = replacement
            mutations.append((field, value, 409, 'evidence_integrity_failed'))
        for field, replacement in (
                ('mode', 'generative'), ('model_used', 'invented-model'),
                ('generated_answer', 'The pressure is safe.'),
                ('conclusion_support', 'supported'), ('assessment_scope', 'all_sources'),
                ('status', 'review_required'), ('reason', 'safe'),
                ('notice', 'The original proves this pressure is safe.')):
            value = json.loads(canonical(original))
            value[field] = replacement
            mutations.append((field, value, 409, 'answer_integrity_failed'))
        value = json.loads(canonical(original))
        del value['excerpts']
        mutations.append(('missing_excerpts', value, 409, 'answer_integrity_failed'))
        value = json.loads(canonical(original))
        value['excerpts'][0]['evidence']['segment_id'] = '0' * 32
        mutations.append(('pinned_evidence', value, 404, 'evidence_not_found'))
        return mutations

    def test_verify_saved_checks_quote_and_answer_semantics(self):
        self.ingest(b'pressure_mpa\n0.68\n')
        _, answer = self.answer(dict(query='pressure_mpa'))
        with self.store.tx() as db:
            self.assertEqual(self.assist.verify_saved(db, 'A', answer), answer)
            for name, value, status, code in self.saved_answer_mutations(answer):
                with self.subTest(name=name):
                    self.assert_error(status, code,
                                      lambda: self.assist.verify_saved(db, 'A', value))

    def test_verify_saved_preserves_review_and_insufficient_states(self):
        self.ingest(b'pressure_mpa\n0.68\n0.72\n')
        for body, expected in ((dict(query='pressure_mpa'), 'review_required'),
                               (dict(query='unmatchedterm'), 'insufficient_evidence')):
            with self.subTest(expected=expected):
                _, answer = self.answer(body)
                self.assertEqual(answer['status'], expected)
                with self.store.tx() as db:
                    self.assertEqual(self.assist.verify_saved(db, 'A', answer), answer)
                    forged = {**answer, 'status': 'evidence_found'}
                    self.assert_error(409, 'answer_integrity_failed',
                                      lambda: self.assist.verify_saved(db, 'A', forged))

    def test_get_and_cache_reject_saved_answer_tampering(self):
        self.ingest(b'pressure_mpa\n0.68\n')
        body, key = dict(query='pressure_mpa'), self.key()
        status, answer, _ = self.api.dispatch('POST', '/v1/answers', 'a', body, key)
        self.assertEqual(status, 201)
        for target in ('records', 'idempotency'):
            for name, value, status, code in self.saved_answer_mutations(answer):
                with self.subTest(target=target, name=name):
                    with self.store.tx() as db:
                        if target == 'records':
                            db.execute("UPDATE records SET data=? WHERE tenant='A' AND kind='answers' AND id=?",
                                       (canonical(value), answer['id']))
                        else:
                            db.execute("UPDATE idempotency SET data=? WHERE tenant='A' AND actor='reader-A' AND key=?",
                                       (canonical(value), key))
                    if target == 'records':
                        fn = lambda: self.api.dispatch('GET', '/v1/answers/' + answer['id'], 'a')
                    else:
                        fn = lambda: self.api.dispatch('POST', '/v1/answers', 'a', body, key)
                    self.assert_error(status, code, fn)
            with self.store.tx() as db:
                if target == 'records':
                    db.execute("UPDATE records SET data=? WHERE tenant='A' AND kind='answers' AND id=?",
                               (canonical(answer), answer['id']))
                else:
                    db.execute("UPDATE idempotency SET data=? WHERE tenant='A' AND actor='reader-A' AND key=?",
                               (canonical(answer), key))
        self.assertEqual(self.api.dispatch('GET', '/v1/answers/' + answer['id'], 'a')[1], answer)
        self.assertEqual(self.api.dispatch('POST', '/v1/answers', 'a', body, key)[1], answer)


if __name__ == '__main__':
    unittest.main()
