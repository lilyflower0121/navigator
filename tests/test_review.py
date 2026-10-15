"""Independent adversarial review: synthetic inputs, actual parser and API.

These fixtures establish local software boundaries, not model-quality evidence.
DB edits deliberately simulate stored payload corruption while keeping valid refs.
"""
import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import stat
import tempfile
import unittest

from navigator.api import API, Auth
from navigator.contracts import APIError, canonical, json_load
from navigator.store import Store


class IndependentReviewTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        scopes = ['read', 'write', 'delete', 'diagnose', 'export']
        self.api = API(self.store, Auth({
            'owner': dict(tenant='review', actor='reviewer', revision=1, scopes=scopes),
            'foreign': dict(tenant='other', actor='reviewer', revision=1, scopes=scopes),
            'reader': dict(tenant='review', actor='reader', revision=1, scopes=['read']),
            'writer': dict(tenant='review', actor='writer', revision=1, scopes=['write']),
        }))
        self.now = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
        self.api.diagnosis.clock = lambda: self.now
        self.counter = 0

    def tearDown(self):
        self.store.close()

    def key(self):
        self.counter += 1
        return f'independent-{self.counter:04d}'

    def post(self, path, body, key=None):
        return self.api.dispatch('POST', path, 'owner', body, key or self.key())[1]

    def ingest(self, raw=b'pressure_mpa,unit\n0.68,MPa\n', media='text/csv'):
        return self.post('/v1/assets', dict(name='synthetic independent review',
                         media_type=media, content_base64=base64.b64encode(raw).decode()))

    def parsed(self, raw=b'pressure_mpa,unit\n0.68,MPa\n', media='text/csv'):
        source = self.ingest(raw, media)
        self.assertTrue(self.api.library.run_once())
        job = self.api.dispatch('GET', '/v1/jobs/' + source['job']['id'], 'owner')[1]
        self.assertEqual(job['state'], 'succeeded')
        return source, job, job['result']['segments'][0]['evidence']

    def process(self):
        return self.post('/v1/processes', dict(name='synthetic rule', required_media=['document'],
                         max_delay_ms=1000,
                         criterion=dict(field='pressure_mpa', operator='gte', threshold=.65, unit='MPa')))

    def diagnosis_body(self, process, ref, sequence=1, delay=500):
        return dict(process_id=process['id'], stream_id='synthetic-review', sequence=sequence,
                    event_time=(self.now - timedelta(milliseconds=delay)).isoformat(),
                    input_status='complete', inputs=[ref], mode='structured_rules')

    def corruption(self, kind, record, altered, key=None):
        with self.store.tx() as db:
            if key:
                db.execute('UPDATE idempotency SET data=? WHERE tenant=? AND actor=? AND key=?',
                           (canonical(altered), 'review', 'reviewer', key))
            else:
                db.execute('UPDATE records SET data=? WHERE tenant=? AND kind=? AND id=?',
                           (canonical(altered), 'review', kind, record['id']))

    def rejects_corruption(self, fn):
        with self.assertRaises(APIError) as caught:
            fn()
        self.assertEqual(caught.exception.status, 409)

    def test_escaped_unpaired_unicode_cannot_kill_worker_or_block_next_job(self):
        for raw in (b'{"bad":"\\ud800"}', b'{"\\udfff":1}'):
            with self.subTest(raw=raw):
                source = self.ingest(raw, 'application/json')
                next_source = self.ingest(b'pressure_mpa,unit\n0.70,MPa\n')
                self.assertTrue(self.api.library.run_once())
                failed = self.api.dispatch('GET', '/v1/jobs/' + source['job']['id'], 'owner')[1]
                self.assertEqual(failed['state'], 'failed')
                self.assertIsInstance(failed['error'], str)
                self.assertTrue(self.api.library.run_once())
                next_job = self.api.dispatch('GET', '/v1/jobs/' + next_source['job']['id'], 'owner')[1]
                self.assertEqual(next_job['state'], 'succeeded')

    def test_valid_unicode_surrogate_pair_is_preserved(self):
        raw = b'{"symbol":"\\ud83d\\ude00"}'
        _, job, ref = self.parsed(raw, 'application/json')
        self.assertEqual(job['result']['segments'][0]['fields']['/symbol'], '\U0001f600')
        with self.store.tx() as db:
            self.assertEqual(self.api.library.resolve(db, 'review', ref)['evidence'], ref)

    def test_large_json_integer_cannot_create_unservable_success(self):
        source = self.ingest(b'{"value":' + b'9' * 4000 + b'}', 'application/json')
        self.assertTrue(self.api.library.run_once())
        job = self.api.dispatch('GET', '/v1/jobs/' + source['job']['id'], 'owner')[1]
        self.assertIn(job['state'], ('failed', 'succeeded'))
        if job['state'] == 'succeeded':
            self.assertEqual(job['result']['segments'][0]['fields']['/value'], int('9' * 4000))

    def test_json_locator_limits_are_rejected_before_success_commit(self):
        raw = ('{"' + 'x' * 2100 + '":1}').encode()
        source = self.ingest(raw, 'application/json')
        self.assertTrue(self.api.library.run_once())
        job = self.api.dispatch('GET', '/v1/jobs/' + source['job']['id'], 'owner')[1]
        self.assertEqual(job['state'], 'failed')
        with self.store.tx() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM segments WHERE asset=?',
                                       (source['asset_id'],)).fetchone()[0], 0)

    def test_json_quote_is_actual_original_text(self):
        for raw, expected in ((b'{"pressure":0.6800}', '0.6800'),
                              (b'{"label":"\\u5468"}', '"\\u5468"')):
            with self.subTest(raw=raw):
                _, job, _ = self.parsed(raw, 'application/json')
                for segment in job['result']['segments']:
                    self.assertIn(segment['quote'].encode(), raw)
                    self.assertEqual(segment['quote'], expected)
                    self.assertEqual(segment['claim_kind'], 'source_quote')

    def test_json_nested_unicode_offsets_pin_the_original_scalar(self):
        text = '  {"猫": [{}, [], {"a/b": 2e+1, "~key": "\\u5468"}, -0.00, true, null]} '
        _, job, _ = self.parsed(text.encode(), 'application/json')
        quotes = {segment['locator']['pointer']: segment['quote']
                  for segment in job['result']['segments']}
        self.assertEqual(quotes, {'/猫/2/a~1b': '2e+1', '/猫/2/~0key': '"\\u5468"',
                                  '/猫/3': '-0.00', '/猫/4': 'true', '/猫/5': 'null'})
        for segment in job['result']['segments']:
            locator = segment['locator']
            self.assertEqual(locator['offset_unit'], 'unicode_codepoints')
            self.assertEqual(text[locator['char_start']:locator['char_end']], segment['quote'])

    def test_json_underflow_does_not_silently_change_measurement_to_zero(self):
        source = self.ingest(b'{"pressure":1e-400}', 'application/json')
        self.assertTrue(self.api.library.run_once())
        job = self.api.dispatch('GET', '/v1/jobs/' + source['job']['id'], 'owner')[1]
        self.assertEqual(job['state'], 'failed')

    def test_extreme_json_exponent_is_machine_readable_rejection(self):
        for raw in (b'1e-9999999999999999999999999999999999',
                    b'0e9999999999999999999999999999999999'):
            with self.subTest(raw=raw):
                with self.assertRaises(APIError) as caught:
                    json_load(raw)
                self.assertEqual((caught.exception.status, caught.exception.code), (422, 'invalid_json'))

    def test_saved_answer_payload_matches_evidence_on_get_and_replay(self):
        self.parsed()
        body = dict(query='pressure_mpa')
        key = self.key()
        answer = self.post('/v1/answers', body, key)
        for field, value in [('quote', '99.99,MPa'), ('fields', {'pressure_mpa': '99.99', 'unit': 'MPa'}),
                             ('source_sha256', '0' * 64), ('locator', dict(kind='lines', line_start=1, line_end=1))]:
            for cache in (False, True):
                with self.subTest(field=field, cache=cache):
                    altered = deepcopy(answer)
                    altered['excerpts'][0][field] = value
                    self.corruption('answers', answer, altered, key if cache else None)
                    fn = (lambda: self.post('/v1/answers', body, key)) if cache else (
                        lambda: self.api.dispatch('GET', '/v1/answers/' + answer['id'], 'owner'))
                    self.rejects_corruption(fn)
                    self.corruption('answers', answer, answer, key if cache else None)

    def test_saved_job_excerpts_match_source_even_with_valid_refs(self):
        source, job, _ = self.parsed()
        altered = deepcopy(job['result'])
        altered['segments'][0]['quote'] = '99.99,MPa'
        with self.store.tx() as db:
            db.execute('UPDATE jobs SET result=? WHERE tenant=? AND id=?',
                       (canonical(altered), 'review', source['job']['id']))
        self.rejects_corruption(lambda: self.api.dispatch('GET', '/v1/jobs/' + job['id'], 'owner'))

    def test_saved_job_boolean_cannot_be_replaced_with_equal_number(self):
        _, job, _ = self.parsed(b'{"value":true}', 'application/json')
        altered = deepcopy(job['result'])
        altered['segments'][0]['fields']['/value'] = 1
        with self.store.tx() as db:
            db.execute('UPDATE jobs SET result=? WHERE tenant=? AND id=?',
                       (canonical(altered), 'review', job['id']))
        self.rejects_corruption(lambda: self.api.dispatch('GET', '/v1/jobs/' + job['id'], 'owner'))

    def test_saved_diagnosis_observations_and_decision_cannot_be_forged(self):
        _, _, ref = self.parsed()
        process = self.process()
        body = self.diagnosis_body(process, ref)
        key = self.key()
        diagnosis = self.post('/v1/diagnoses', body, key)
        for change in ('original_value', 'value', 'unit', 'decision'):
            for cache in (False, True):
                with self.subTest(change=change, cache=cache):
                    altered = deepcopy(diagnosis)
                    if change == 'decision':
                        altered['decision'] = 'outside_rule'
                    else:
                        altered['observations'][0][change] = 'kPa' if change == 'unit' else (
                            '99.99' if change == 'original_value' else 99.99)
                    self.corruption('diagnoses', diagnosis, altered, key if cache else None)
                    fn = (lambda: self.post('/v1/diagnoses', body, key)) if cache else (
                        lambda: self.api.dispatch('GET', '/v1/diagnoses/' + diagnosis['id'], 'owner'))
                    self.rejects_corruption(fn)
                    self.corruption('diagnoses', diagnosis, diagnosis, key if cache else None)

    def test_immutable_process_hash_is_verified_before_evaluation(self):
        _, _, ref = self.parsed()
        process = self.process()
        altered = deepcopy(process)
        altered['criterion']['threshold'] = .99
        self.corruption('processes', process, altered)
        self.rejects_corruption(lambda: self.post('/v1/diagnoses', self.diagnosis_body(process, ref)))

    def test_historical_hold_cannot_be_erased_by_coherent_payload_change(self):
        _, _, ref = self.parsed()
        process = self.process()
        self.post('/v1/diagnoses', self.diagnosis_body(process, ref, sequence=10))
        body = self.diagnosis_body(process, ref, sequence=9)
        key = self.key()
        held = self.post('/v1/diagnoses', body, key)
        self.assertEqual(held['decision'], 'hold')
        altered = deepcopy(held)
        altered.update(decision='meets_rule', hold_reasons=[], order_status='in_order')
        for cache in (False, True):
            with self.subTest(cache=cache):
                self.corruption('diagnoses', held, altered, key if cache else None)
                fn = (lambda: self.post('/v1/diagnoses', body, key)) if cache else (
                    lambda: self.api.dispatch('GET', '/v1/diagnoses/' + held['id'], 'owner'))
                self.rejects_corruption(fn)
                self.corruption('diagnoses', held, held, key if cache else None)

    def test_deleted_history_does_not_allow_backward_event_as_normal(self):
        source, _, ref = self.parsed()
        process = self.process()
        self.post('/v1/diagnoses', self.diagnosis_body(process, ref, sequence=10, delay=200))
        self.api.dispatch('DELETE', '/v1/assets/' + source['asset_id'], 'owner', {}, self.key())
        _, _, new_ref = self.parsed()
        later = self.post('/v1/diagnoses', self.diagnosis_body(process, new_ref, sequence=11, delay=500))
        self.assertEqual(later['decision'], 'hold')
        self.assertTrue(later['hold_reasons'])

    def test_sqlite_main_and_wal_sidecars_have_private_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'isolated' / 'module.sqlite'
            persistent = Store(path)
            try:
                for file in (path, Path(str(path) + '-wal'), Path(str(path) + '-shm')):
                    with self.subTest(file=file.name):
                        self.assertEqual(stat.S_IMODE(file.stat().st_mode) & 0o077, 0)
            finally:
                persistent.close()

    def test_read_only_and_write_only_actors_cannot_persist_source_answer(self):
        self.parsed()
        for token in ('reader', 'writer'):
            with self.subTest(token=token):
                with self.assertRaises(APIError) as caught:
                    self.api.dispatch('POST', '/v1/answers', token,
                                      dict(query='pressure_mpa'), self.key())
                self.assertEqual(caught.exception.status, 403)
        with self.store.tx() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM records WHERE kind='answers'").fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
