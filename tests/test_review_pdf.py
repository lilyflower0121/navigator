"""Independent PDF integration boundaries; synthetic bytes, real installed CLIs.

These tests establish parser/API contracts, not original-document or model-quality scores.
"""
import base64
from copy import deepcopy
import http.client
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.request import Request, urlopen

from navigator.api import API, Auth
from navigator.contracts import APIError, MAX_BODY, MAX_PDF, MAX_SOURCE, canonical, digest
from navigator.http import Host
from navigator import pdf_adapter as pdf
from navigator.parsing import parse_source
from navigator.store import Store
from tests.test_pdf_adapter import native_pdf


class IndependentPDFReview(unittest.TestCase):
    def setUp(self):
        if not all(pdf._cli_paths()):
            self.skipTest('Independent real native execution requires installed Poppler CLIs')
        self.store = Store(':memory:')
        scopes = ['read', 'write', 'delete', 'diagnose', 'export']
        self.api = API(self.store, Auth({
            'owner': dict(tenant='pdf-review', actor='reviewer', revision=1, scopes=scopes),
            'foreign': dict(tenant='other', actor='reviewer', revision=1, scopes=scopes),
        }))
        self.counter = 0

    def tearDown(self):
        if hasattr(self, 'store'):
            self.store.close()

    def key(self):
        self.counter += 1
        return f'pdf-independent-{self.counter:04d}'

    def post(self, path, body, key=None):
        return self.api.dispatch('POST', path, 'owner', body, key or self.key())[1]

    def ingest(self, raw, media='application/pdf'):
        return self.post('/v1/assets', dict(name='synthetic native PDF integration', media_type=media,
                         content_base64=base64.b64encode(raw).decode()))

    def parsed(self, raw=None):
        raw = raw or native_pdf([['Pressure: 0.68 MPa'], ['Flow: 19.25']])
        source = self.ingest(raw)
        self.assertTrue(self.api.library.run_once())
        job = self.api.dispatch('GET', '/v1/jobs/' + source['job']['id'], 'owner')[1]
        self.assertEqual(job['state'], 'succeeded')
        return raw, source, job

    def error(self, status, fn, code=None):
        with self.assertRaises(APIError) as caught:
            fn()
        self.assertEqual(caught.exception.status, status)
        if code:
            self.assertEqual(caught.exception.code, code)

    def test_actual_large_pdf_api_preserves_original_pages_quotes_and_tenant(self):
        raw = native_pdf([['Pressure: 0.68 MPa'], ['Flow: 19.25']])
        raw += b' ' * (MAX_SOURCE + 1 - len(raw))
        raw, source, job = self.parsed(raw)
        self.assertEqual(job['processor_version'], 'native-pdf-text-v1')
        original = self.api.dispatch('GET', f"/v1/assets/{source['asset_id']}/versions/1/original", 'owner')
        self.assertEqual(original[1], raw)
        self.assertEqual(original[2]['X-Source-SHA256'], digest(raw))
        asset = self.api.dispatch('GET', '/v1/assets/' + source['asset_id'], 'owner')[1]
        self.assertEqual(asset['versions'][0]['bytes'], len(raw))
        self.assertEqual([s['locator']['page'] for s in job['result']['segments']], [1, 2])
        answer = self.post('/v1/answers', dict(query='Pressure', filters=dict(media_types=['application/pdf'])))
        self.assertEqual(answer['excerpts'][0]['locator']['page'], 1)
        self.assertIn('0.68', answer['excerpts'][0]['quote'])
        self.assertIsNone(answer['generated_answer'])
        self.assertEqual(answer['conclusion_support'], 'unassessed')
        self.error(404, lambda: self.api.dispatch('GET', '/v1/assets/' + source['asset_id'], 'foreign'))
        self.assertEqual(self.api.dispatch('POST', '/v1/search', 'foreign', dict(query='Pressure'))[1]['results'], [])

    def test_pdf_source_extension_does_not_expand_text_or_template_limits(self):
        self.error(413, lambda: self.ingest(b'x' * (MAX_SOURCE + 1), 'text/plain'), 'source_byte_limit')
        self.error(413, lambda: self.ingest(b'x' * (MAX_PDF + 1)), 'source_byte_limit')
        self.error(413, lambda: self.post('/v1/templates', dict(name='oversize',
                   content_base64=base64.b64encode(b'x' * (MAX_SOURCE + 1)).decode())), 'source_byte_limit')
        with self.store.tx() as db:
            for table in ('assets', 'sources', 'jobs', 'idempotency'):
                self.assertEqual(db.execute('SELECT count(*) FROM ' + table).fetchone()[0], 0)

    def test_real_twenty_mib_tenant_quota_is_atomic(self):
        raw = native_pdf([['Synthetic quota input']])
        raw += b' ' * (MAX_PDF - len(raw))
        for _ in range(5):
            self.ingest(raw)
        self.error(413, lambda: self.ingest(native_pdf([['overflow']])), 'tenant_source_byte_limit')
        with self.store.tx() as db:
            self.assertEqual(db.execute('SELECT sum(length(raw)) FROM sources').fetchone()[0], 20 * 1024 * 1024)
            for table in ('assets', 'sources', 'jobs', 'idempotency'):
                self.assertEqual(db.execute('SELECT count(*) FROM ' + table).fetchone()[0], 5)

    def test_parser_cache_reuses_source_only_inside_current_transaction(self):
        _, _, job = self.parsed()
        refs = [segment['evidence'] for segment in job['result']['segments']]
        with patch('navigator.library.parse_source', wraps=parse_source) as parser:
            with self.store.tx() as db:
                for ref in refs:
                    self.api.library.resolve(db, 'pdf-review', ref)
                self.assertEqual(parser.call_count, 1)
                self.assertTrue(self.store.parse_cache)
            self.assertEqual(self.store.parse_cache, {})
            with self.store.tx() as db:
                self.api.library.resolve(db, 'pdf-review', refs[0])
                self.assertEqual(parser.call_count, 2)
            self.assertEqual(self.store.parse_cache, {})

    def test_parser_cache_clears_after_rollback_and_cannot_hide_raw_corruption(self):
        _, source, job = self.parsed()
        ref = job['result']['segments'][0]['evidence']
        with self.assertRaises(RuntimeError):
            with self.store.tx() as db:
                self.api.library.resolve(db, 'pdf-review', ref)
                raise RuntimeError('synthetic transaction rollback')
        self.assertEqual(self.store.parse_cache, {})
        with self.store.tx() as db:
            db.execute('UPDATE sources SET raw=? WHERE tenant=? AND asset=?',
                       (native_pdf([['Forged pressure 99.99']]), 'pdf-review', source['asset_id']))
        with self.store.tx() as db:
            self.error(409, lambda: self.api.library.resolve(db, 'pdf-review', ref), 'source_integrity_failed')

    def test_answer_shares_native_parse_within_each_transaction(self):
        self.parsed()
        with patch('navigator.library.parse_source', wraps=parse_source) as parser:
            answer = self.post('/v1/answers', dict(query='Pressure Flow', limit=20))
        self.assertEqual(len(answer['excerpts']), 2)
        # One mutation transaction, then one post-commit verification transaction.
        self.assertEqual(parser.call_count, 2)
        self.assertEqual(self.store.parse_cache, {})

    def test_changed_native_output_changes_analysis_pin_and_fails_old_evidence(self):
        _, source, job = self.parsed()
        ref = job['result']['segments'][0]['evidence']
        changed = b'Synthetic changed runtime output: 99.99\n\fpage two\n\f'
        claimed = dict(raw=self.api.dispatch('GET', f"/v1/assets/{source['asset_id']}/versions/1/original", 'owner')[1],
                       sha=job['result']['segments'][0]['source_sha256'], asset=source['asset_id'], version=1,
                       meta=dict(media_type='application/pdf'))
        with patch.object(pdf, '_run_cli', side_effect=[b'Pages: 2\n', changed]):
            new = parse_source(claimed)
        self.assertEqual(new['extracted_sha256'], digest(changed))
        self.assertNotEqual(new['analysis_version'], ref['analysis_version'])
        self.assertNotEqual(new['segments'][0]['id'], ref['segment_id'])
        with patch.object(pdf, '_run_cli', side_effect=[b'Pages: 2\n', changed]):
            with self.store.tx() as db:
                self.error(409, lambda: self.api.library.resolve(db, 'pdf-review', ref), 'evidence_integrity_failed')

    def test_capabilities_distinguish_adapter_from_installed_runtime(self):
        with patch('shutil.which', return_value=None):
            absent = self.api.dispatch('GET', '/v1/capabilities', 'owner')[1]['library']
        self.assertTrue(absent['native_pdf_adapter'])
        self.assertFalse(absent['native_pdf_runtime_available'])
        present = self.api.dispatch('GET', '/v1/capabilities', 'owner')[1]['library']
        self.assertTrue(present['native_pdf_runtime_available'])

    def test_saved_pdf_page_and_quote_corruption_rejected(self):
        _, _, job = self.parsed()
        body, key = dict(query='Pressure'), self.key()
        answer = self.post('/v1/answers', body, key)
        for cache in (False, True):
            altered = deepcopy(answer)
            altered['excerpts'][0]['locator']['page'] = 2
            altered['excerpts'][0]['quote'] = 'Pressure: 99.99 MPa'
            with self.store.tx() as db:
                if cache:
                    db.execute('UPDATE idempotency SET data=? WHERE tenant=? AND actor=? AND key=?',
                               (canonical(altered), 'pdf-review', 'reviewer', key))
                else:
                    db.execute("UPDATE records SET data=? WHERE tenant=? AND kind='answers' AND id=?",
                               (canonical(altered), 'pdf-review', answer['id']))
            fn = (lambda: self.post('/v1/answers', body, key)) if cache else (
                lambda: self.api.dispatch('GET', '/v1/answers/' + answer['id'], 'owner'))
            self.error(409, fn, 'evidence_integrity_failed')

    def test_optional_cli_absence_blocks_and_preserves_original(self):
        raw = native_pdf([['Synthetic unavailable text']])
        source = self.ingest(raw)
        with patch.object(pdf, '_cli_paths', return_value=(None, None)):
            self.assertTrue(self.api.library.run_once())
        job = self.api.dispatch('GET', '/v1/jobs/' + source['job']['id'], 'owner')[1]
        self.assertEqual((job['state'], job['error']), ('blocked', 'capability_unavailable'))
        self.assertEqual(job['result']['capability'], 'native_pdf_text')
        original = self.api.dispatch('GET', f"/v1/assets/{source['asset_id']}/versions/1/original", 'owner')[1]
        self.assertEqual(original, raw)

    def test_pdf_text_is_not_claimed_as_structured_measurement_or_table_cells(self):
        _, _, job = self.parsed()
        ref = job['result']['segments'][0]['evidence']
        self.assertEqual(job['result']['segments'][0]['fields'], {})
        from datetime import datetime, timezone
        process = self.post('/v1/processes', dict(name='synthetic PDF rule', required_media=['document'],
                       max_delay_ms=1000, criterion=dict(field='pressure_mpa', operator='gte', threshold=.65, unit='MPa')))
        diagnosis = self.post('/v1/diagnoses', dict(process_id=process['id'], stream_id='synthetic-pdf', sequence=1,
                       event_time=datetime.now(timezone.utc).isoformat(), input_status='complete', inputs=[ref], mode='structured_rules'))
        self.assertEqual(diagnosis['decision'], 'hold')
        self.assertIn('measurement_missing', diagnosis['hold_reasons'])
        raw = json.dumps(dict(format='navigator-cell-layout-v1', fields=[dict(field='pressure', cell='A1', required=True)])).encode()
        template = self.post('/v1/templates', dict(name='synthetic PDF draft', content_base64=base64.b64encode(raw).decode()))
        self.error(422, lambda: self.post('/v1/reports', dict(template_id=template['id'], assignments=[
                   dict(field='pressure', evidence=ref, source_field='pressure_mpa')])), 'source_field_not_found')

    def test_pdf_deleted_after_claim_cannot_restore_any_text(self):
        source = self.ingest(native_pdf([['Deleted synthetic native PDF marker']]))
        claimed = self.store.claim(self.api.auth.authorized_worker)
        self.api.dispatch('DELETE', '/v1/assets/' + source['asset_id'], 'owner', {}, self.key())
        result = parse_source(claimed)
        self.assertFalse(self.store.finish(claimed, result, self.api.auth.authorized_worker))
        self.assertEqual(self.api.dispatch('POST', '/v1/search', 'owner', dict(query='Deleted'))[1]['results'], [])
        self.assertEqual(self.store.parse_cache, {})

    def test_cli_timeout_kills_and_reaps_direct_child(self):
        captured = []
        original = subprocess.Popen
        def launch(*args, **kwargs):
            process = original(*args, **kwargs)
            captured.append(process)
            return process
        with patch.object(pdf.subprocess, 'Popen', side_effect=launch), patch.object(pdf, 'CLI_TIMEOUT_SECONDS', .05):
            self.error(408, lambda: pdf._run_cli([sys.executable, '-c', 'import time;time.sleep(30)']), 'pdf_cli_timeout')
        self.assertEqual(len(captured), 1)
        self.assertIsNotNone(captured[0].poll())
        self.assertTrue(captured[0].stdout.closed)


class IndependentPDFHTTPReview(unittest.TestCase):
    def test_actual_four_mib_original_http_ingestion_and_six_mib_http_cap(self):
        if not all(pdf._cli_paths()):
            self.skipTest('Installed native PDF CLIs are required')
        token = 'synthetic-independent-pdf-http-owner'
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / 'owned.sqlite')
            api = API(store, Auth({token: dict(tenant='review', actor='reviewer', revision=1,
                                             scopes=['read', 'write', 'delete'])}))
            host = Host(api, 0)
            host.start()
            try:
                raw = native_pdf([['Synthetic exact four MiB original: 0.68 MPa']])
                raw += b' ' * (MAX_PDF - len(raw))
                body = dict(name='exact bound synthetic PDF', media_type='application/pdf',
                            content_base64=base64.b64encode(raw).decode())
                payload = json.dumps(body).encode()
                self.assertLess(len(payload), MAX_BODY)
                headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json',
                           'Idempotency-Key': 'pdf-http-exact-bound'}
                with urlopen(Request(host.url + '/v1/assets', data=payload, headers=headers, method='POST'), timeout=5) as response:
                    self.assertEqual(response.status, 202)
                    source = json.load(response)
                deadline = time.monotonic() + 5
                while True:
                    with urlopen(Request(host.url + '/v1/jobs/' + source['job']['id'],
                                         headers={'Authorization': 'Bearer ' + token}), timeout=5) as response:
                        job = json.load(response)
                    if job['state'] not in ('queued', 'running') or time.monotonic() >= deadline:
                        break
                    time.sleep(.01)
                self.assertEqual(job['state'], 'succeeded')
                with urlopen(Request(host.url + f"/v1/assets/{source['asset_id']}/versions/1/original",
                                     headers={'Authorization': 'Bearer ' + token}), timeout=5) as response:
                    self.assertEqual(response.read(), raw)
                    self.assertEqual(response.headers['X-Source-SHA256'], digest(raw))
                connection = http.client.HTTPConnection('127.0.0.1', host.server.server_port, timeout=5)
                try:
                    connection.request('POST', '/v1/assets', body=b'{}', headers={**headers, 'Content-Length': str(MAX_BODY + 1)})
                    response = connection.getresponse()
                    self.assertEqual(response.status, 413)
                    self.assertEqual(json.loads(response.read())['code'], 'http_body_limit')
                finally:
                    connection.close()
            finally:
                host.close()
                store.close()


if __name__ == '__main__':
    unittest.main()
