"""Real-byte extraction and library boundaries using tiny synthetic sources."""
import base64
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from navigator.contracts import APIError, LIMITS, MAX_SOURCE, canonical, digest
from navigator.library import Library
from navigator.parsing import parse_source
from navigator.store import Store


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.library = Library(self.store)
        self.ctx = dict(tenant='synthetic-a', actor='writer', revision=1)
        self.counter = 0

    def tearDown(self):
        self.store.close()

    def ingest(self, raw, media='text/plain', **extra):
        self.counter += 1
        body = dict(name='synthetic', media_type=media,
                    content_base64=base64.b64encode(raw).decode(), **extra)
        return self.store.mutate(self.ctx, 'library_%08d' % self.counter,
                                 'ingest', body,
                                 lambda db: self.library.ingest(db, self.ctx, body))[1]

    def extracted(self, raw, media='text/plain', **extra):
        value = self.ingest(raw, media, **extra)
        self.assertTrue(self.library.run_once())
        with self.store.tx() as db:
            job = self.store.job(db, self.ctx['tenant'], value['job']['id'])
        self.assertEqual(job['state'], 'succeeded')
        return value, job['result']['segments']

    def search(self, query, **extra):
        with self.store.tx() as db:
            return self.library.search(db, self.ctx['tenant'], dict(query=query, **extra))

    def resolve(self, ref, tenant=None):
        with self.store.tx() as db:
            return self.library.resolve(db, tenant or self.ctx['tenant'], ref)

    def assert_error(self, code, call, status=None):
        with self.assertRaises(APIError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)
        if status is not None:
            self.assertEqual(caught.exception.status, status)

    def test_original_utf8_bom_hash_and_text_lines(self):
        raw = b'\xef\xbb\xbf' + '合成の点検\r\npressure_mpa=0.68\r\n'.encode()
        value, segments = self.extracted(raw)
        self.assertEqual([s['quote'] for s in segments], ['合成の点検', 'pressure_mpa=0.68'])
        self.assertEqual(segments[1]['locator'], dict(kind='lines', line_start=2, line_end=2))
        with self.store.tx() as db:
            original, sha = self.library.original(db, self.ctx['tenant'], value['asset_id'], 1)
        self.assertEqual(original, raw)
        self.assertEqual(sha, digest(raw))
        self.assertEqual(self.resolve(segments[1]['evidence']), segments[1])

    def test_csv_multiline_crlf_uses_physical_lines_and_real_values(self):
        raw = b'pressure_mpa,note\r\n0.68,"synthetic\r\nrecord"\r\n0.70,other\r\n'
        _, segments = self.extracted(raw, 'text/csv')
        self.assertEqual(segments[0]['fields'], {'pressure_mpa': '0.68', 'note': 'synthetic\r\nrecord'})
        self.assertEqual(segments[0]['quote'], '0.68,"synthetic\r\nrecord"')
        self.assertEqual(segments[0]['locator'], dict(kind='lines', line_start=2, line_end=3, header_line_end=1))
        self.assertEqual(segments[1]['locator']['line_start'], 4)
        for segment in segments:
            self.assertEqual(self.resolve(segment['evidence']), segment)

    def test_csv_unicode_separators_are_cell_content(self):
        raw = 'pressure_mpa,note\n0.68,synthetic\u2028record\n0.7,last\n'.encode()
        _, segments = self.extracted(raw, 'text/csv')
        self.assertEqual(segments[0]['locator']['line_start'], 2)
        self.assertEqual(segments[0]['locator']['line_end'], 2)
        self.assertEqual(segments[0]['fields']['note'], 'synthetic\u2028record')
        self.assertEqual(segments[1]['quote'], '0.7,last')
        self.resolve(segments[0]['evidence'])

    def test_csv_multiline_header_is_included_in_validation(self):
        _, segments = self.extracted(b'"synthetic\nfield",pressure_mpa\nokay,0.68\n', 'text/csv')
        self.assertEqual(segments[0]['locator']['header_line_end'], 2)
        self.assertEqual(segments[0]['locator']['line_start'], 3)
        self.assertEqual(segments[0]['fields'], {'synthetic\nfield': 'okay', 'pressure_mpa': '0.68'})
        self.resolve(segments[0]['evidence'])

    def test_json_pointers_escape_keys_arrays_and_root_scalar(self):
        _, segments = self.extracted(b'{"a/b":{"~key":[0.68,null,true]},"":"synthetic"}', 'application/json')
        self.assertEqual([s['locator']['pointer'] for s in segments], ['/a~1b/~0key/0', '/a~1b/~0key/1', '/a~1b/~0key/2', '/'])
        self.assertEqual(segments[0]['fields'], {'/a~1b/~0key/0': 0.68})
        for segment in segments:
            self.resolve(segment['evidence'])
        _, scalar = self.extracted(b'"synthetic root"', 'application/json')
        self.assertEqual(scalar[0]['locator']['kind'], 'json_pointer')
        self.assertEqual(scalar[0]['locator']['pointer'], '')
        self.resolve(scalar[0]['evidence'])

    def test_versions_are_immutable_latest_and_all_filters(self):
        first, segments = self.extracted(b'synthetic old pressure')
        second, newer = self.extracted(b'synthetic new pressure', asset_id=first['asset_id'])
        self.assertEqual(second['source_version'], 2)
        self.assertNotEqual(segments[0]['evidence']['analysis_version'], newer[0]['evidence']['analysis_version'])
        self.assertEqual(self.resolve(segments[0]['evidence']), segments[0])
        self.assertEqual([s['evidence']['source_version'] for s in self.search('pressure')['results']], [2])
        self.assertEqual(len(self.search('pressure', filters={'versions': 'all'})['results']), 2)
        with self.store.tx() as db:
            self.assertEqual(self.library.original(db, self.ctx['tenant'], first['asset_id'], 1)[0], b'synthetic old pressure')

    def test_metadata_filters_are_exact_and_independent_of_lexical_match(self):
        first, _ = self.extracted(b'synthetic pressure', labels=['red', 'shared'])
        self.extracted(b'synthetic pressure', 'text/markdown', labels=['blue', 'shared'])
        self.assertEqual(len(self.search('pressure', filters={'labels': ['shared']})['results']), 2)
        filtered = self.search('pressure', filters={'labels': ['red', 'shared'], 'asset_ids': [first['asset_id']], 'media_types': ['text/plain']})
        self.assertEqual(len(filtered['results']), 1)
        self.assertEqual(filtered['mode'], 'lexical')
        self.assertIsNone(filtered['model_used'])
        self.assertEqual(self.search('absent', filters={'labels': ['red']})['results'], [])
        self.assertEqual(self.search('pressure', filters={'labels': ['r']})['results'], [])

    def test_latest_pending_source_does_not_return_obsolete_analysis(self):
        first, _ = self.extracted(b'synthetic pressure')
        self.ingest(b'new synthetic pressure', asset_id=first['asset_id'])
        self.assertEqual(self.search('pressure')['results'], [])
        self.assertEqual(len(self.search('pressure', filters={'versions': 'all'})['results']), 1)

    def test_tenant_isolation_and_multimodal_is_unavailable(self):
        _, segments = self.extracted(b'synthetic pressure')
        self.assert_error('not_found', lambda: self.resolve(segments[0]['evidence'], 'synthetic-b'), 404)
        with self.store.tx() as db:
            self.assertEqual(self.library.search(db, 'synthetic-b', {'query': 'pressure'})['results'], [])
        self.assert_error('multimodal_search_unavailable', lambda: self.search('pressure', modality='image'), 501)

    def test_fields_locator_identity_and_quote_tampering_are_rejected(self):
        _, segments = self.extracted(b'pressure_mpa,note\n0.68,synthetic\n', 'text/csv')
        segment = segments[0]
        variants = []
        forged = deepcopy(segment); forged['fields']['pressure_mpa'] = '99'; variants.append(forged)
        forged = deepcopy(segment); forged['locator']['line_start'] = 1; variants.append(forged)
        forged = deepcopy(segment); forged['evidence']['source_version'] = 2; variants.append(forged)
        forged = deepcopy(segment); forged['id'] = 'f' * 32; variants.append(forged)
        forged = deepcopy(segment); forged['quote'] = '99,synthetic'; variants.append(forged)
        for forged in variants:
            with self.subTest(forged=forged):
                with self.store.tx() as db:
                    db.execute('UPDATE segments SET data=? WHERE tenant=? AND id=?', (canonical(forged), self.ctx['tenant'], segment['id']))
                self.assert_error('evidence_integrity_failed', lambda: self.resolve(segment['evidence']), 409)
                self.assert_error('evidence_integrity_failed', lambda: self.search('pressure_mpa'), 409)
        with self.store.tx() as db:
            db.execute('UPDATE segments SET data=? WHERE tenant=? AND id=?', (canonical(segment), self.ctx['tenant'], segment['id']))
        self.resolve(segment['evidence'])

    def test_source_hash_tampering_is_rejected(self):
        value, segments = self.extracted(b'synthetic pressure')
        with self.store.tx() as db:
            db.execute('UPDATE sources SET raw=? WHERE tenant=? AND asset=?', (b'synthetic changed!', self.ctx['tenant'], value['asset_id']))
        self.assert_error('source_integrity_failed', lambda: self.resolve(segments[0]['evidence']), 409)

    def test_analysis_column_tampering_cannot_authorize_a_different_pin(self):
        _, segments = self.extracted(b'synthetic pressure')
        ref = dict(segments[0]['evidence'], analysis_version='forged-analysis')
        with self.store.tx() as db:
            db.execute('UPDATE segments SET analysis=? WHERE tenant=? AND id=?',
                       (ref['analysis_version'], self.ctx['tenant'], ref['segment_id']))
        self.assert_error('evidence_integrity_failed', lambda: self.resolve(ref), 409)
        self.assert_error('evidence_integrity_failed', lambda: self.search('pressure'), 409)

    def test_search_bounds_reparse_and_reuses_source_in_transaction(self):
        self.extracted(b'pressure first\npressure second\npressure third\n')
        with patch('navigator.library.parse_source', wraps=parse_source) as parser:
            self.assertEqual(len(self.search('pressure', limit=2)['results']), 2)
            self.assertEqual(parser.call_count, 1)

    def test_invalid_parser_inputs_fail_without_partial_segments(self):
        cases = [(b'\xff', 'text/plain', 'invalid_utf8'),
                 (b'a\x00b', 'text/plain', 'binary_text_rejected'),
                 (b' \r\n', 'text/plain', 'no_extractable_content'),
                 (b'a,a\n1,2\n', 'text/csv', 'invalid_csv_header'),
                 (b'a,b\n1\n', 'text/csv', 'csv_column_mismatch'),
                 (b'a\n"unterminated', 'text/csv', 'invalid_csv'),
                 (b'{"a":1,"a":2}', 'application/json', 'invalid_json'),
                 (b'{"a":NaN}', 'application/json', 'invalid_json'),
                 (b'{"a":1e999}', 'application/json', 'invalid_json'),
                 (b'{}', 'application/json', 'no_extractable_content')]
        for raw, media, code in cases:
            with self.subTest(raw=raw):
                value = self.ingest(raw, media)
                self.library.run_once()
                with self.store.tx() as db:
                    job = self.store.job(db, self.ctx['tenant'], value['job']['id'])
                    count = db.execute('SELECT count(*) FROM segments WHERE asset=?', (value['asset_id'],)).fetchone()[0]
                self.assertEqual((job['state'], job['error'], count), ('failed', code, 0))

    def test_parser_limits_reject_instead_of_truncating(self):
        cases = [(b'x\n' * (LIMITS['segments'] + 1), 'text/plain', 'extraction_limit'),
                 (b'x' * 8193, 'text/plain', 'extraction_limit'),
                 (json.dumps([[[[[[[[[[[[[1]]]]]]]]]]]]]).encode(), 'application/json', 'json_nesting_limit')]
        for raw, media, code in cases:
            with self.subTest(code=code):
                self.assert_error(code, lambda: parse_source(dict(raw=raw, sha=digest(raw), asset='a'*32, version=1, meta={'media_type': media})), 413)
        self.assert_error('source_byte_limit', lambda: self.ingest(b'x' * (MAX_SOURCE + 1)), 413)
        self.assert_error('source_byte_limit', lambda: self.ingest(b''), 413)
        with self.store.tx() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM assets').fetchone()[0], 0)

    def test_connected_image_parser_rejects_invalid_pixels(self):
        raw = b'\x89PNG\r\n\x1a\nsynthetic'
        value = self.ingest(raw, 'image/png')
        self.library.run_once()
        with self.store.tx() as db:
            job = self.store.job(db, self.ctx['tenant'], value['job']['id'])
            self.assertEqual(self.library.original(db, self.ctx['tenant'], value['asset_id'], 1)[0], raw)
        self.assertEqual((job['state'], job['error']), ('failed', 'image_invalid'))
        self.assertIsNone(job['model_used'])

    def test_cancel_and_delete_fence_old_worker_results(self):
        value = self.ingest(b'synthetic pressure')
        claim = self.store.claim(self.library.authorize)
        result = parse_source(claim)
        with self.store.tx() as db:
            job = self.store.job(db, self.ctx['tenant'], value['job']['id'])
            self.library.control_job(db, self.ctx, job['id'], 'cancel', {'expected_revision': job['revision']})
        self.assertFalse(self.store.finish(claim, result, self.library.authorize))
        with self.store.tx() as db:
            job = self.store.job(db, self.ctx['tenant'], value['job']['id'])
            self.library.control_job(db, self.ctx, job['id'], 'retry', {'expected_revision': job['revision']})
        second_claim = self.store.claim(self.library.authorize)
        with self.store.tx() as db:
            self.library.delete(db, self.ctx['tenant'], value['asset_id'])
        self.assertFalse(self.store.finish(second_claim, parse_source(second_claim), self.library.authorize))
        self.assertEqual(self.search('pressure')['results'], [])

    def test_lease_reclaim_rejects_previous_epoch(self):
        self.ingest(b'synthetic pressure')
        now = time.time()
        first = self.store.claim(self.library.authorize, now=now)
        second = self.store.claim(self.library.authorize, now=now+31)
        self.assertGreater(second['epoch'], first['epoch'])
        self.assertFalse(self.store.finish(first, parse_source(first), self.library.authorize))
        self.assertTrue(self.store.finish(second, parse_source(second), self.library.authorize))

    def test_delete_invalidates_records_and_idempotent_ingest(self):
        value, segments = self.extracted(b'synthetic pressure')
        with self.store.tx() as db:
            self.store.save_record(db, self.ctx['tenant'], 'answers', {'id': 'b'*32, 'quote': 'synthetic pressure'}, [value['asset_id']])
            self.library.delete(db, self.ctx['tenant'], value['asset_id'])
        self.assert_error('source_deleted', lambda: self.resolve(segments[0]['evidence']), 410)
        with self.store.tx() as db:
            self.assert_error('deleted_dependency', lambda: self.store.record(db, self.ctx['tenant'], 'answers', 'b'*32), 410)
            cached = db.execute('SELECT status,data FROM idempotency').fetchone()
            self.assertEqual(cached['status'], 410)
            self.assertNotIn('synthetic pressure', cached['data'])
            self.assertEqual(db.execute('SELECT count(*) FROM sources').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM segments').fetchone()[0], 0)

    def test_retry_respects_active_job_capacity_and_revision(self):
        value = self.ingest(b'synthetic pressure')
        with self.store.tx() as db:
            self.library.control_job(db, self.ctx, value['job']['id'], 'cancel', {'expected_revision': 1})
        for index in range(LIMITS['active_jobs']):
            self.ingest(('synthetic %d' % index).encode())
        def retry(expected):
            with self.store.tx() as db:
                self.library.control_job(db, self.ctx, value['job']['id'], 'retry', {'expected_revision': expected})
        self.assert_error('revision_conflict', lambda: retry(1), 409)
        self.assert_error('job_capacity_limit', lambda: retry(2), 413)

    def test_delete_invalidates_idempotency_for_namespaced_actor(self):
        self.ctx['actor'] = 'synthetic:writer'
        value, _ = self.extracted(b'synthetic pressure')
        with self.store.tx() as db:
            self.library.delete(db, self.ctx['tenant'], value['asset_id'])
            cached = db.execute('SELECT status FROM idempotency WHERE actor=?', (self.ctx['actor'],)).fetchone()
            self.assertEqual(cached['status'], 410)
        body = dict(name='synthetic', media_type='text/plain',
                    content_base64=base64.b64encode(b'synthetic pressure').decode())
        self.assert_error('deleted_dependency',
                          lambda: self.store.mutate(self.ctx, 'library_00000001', 'ingest', body,
                                                   lambda db: self.library.ingest(db, self.ctx, body)), 410)

    def test_original_and_analysis_survive_store_reopen(self):
        self.store.close()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'synthetic.sqlite3'
            self.store = Store(path); self.library = Library(self.store)
            value, segments = self.extracted(b'synthetic pressure')
            self.store.close()
            self.store = Store(path); self.library = Library(self.store)
            self.assertEqual(self.resolve(segments[0]['evidence']), segments[0])
            with self.store.tx() as db:
                self.assertEqual(self.library.original(db, self.ctx['tenant'], value['asset_id'], 1)[0], b'synthetic pressure')


if __name__ == '__main__':
    unittest.main()
