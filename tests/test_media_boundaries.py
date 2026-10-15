"""Independent Phase 2 boundary review; fixtures are never AI-quality evidence."""
import base64
import copy
from decimal import Decimal
import io
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile

from navigator.api import API, Auth
from navigator.contracts import APIError, canonical, digest
from navigator.store import Store
from navigator import grounded, image_adapter, pdf_adapter, table_adapter, xlsx_adapter
from tests.test_image_adapter import png
from tests.test_table_adapter import layout_fixture

ROOT = Path(__file__).resolve().parents[1]


class IndependentPhase2Review(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.auth = Auth({'a': dict(tenant='A', actor='review:A', revision=1,
            scopes=['read', 'write', 'delete', 'export']),
            'b': dict(tenant='B', actor='review:B', revision=1,
            scopes=['read', 'write', 'delete', 'export']),
            'read': dict(tenant='A', actor='review:read', revision=1, scopes=['read'])})
        self.api = API(self.store, self.auth)
        self.n = 0

    def tearDown(self):
        self.store.close()

    def key(self):
        self.n += 1
        return 'phase2-review-' + str(self.n)

    def error(self, status, fn, code=None):
        with self.assertRaises(APIError) as caught:
            fn()
        self.assertEqual(caught.exception.status, status)
        if code:
            self.assertEqual(caught.exception.code, code)

    def ingest(self, raw, media='text/plain', name='review-source'):
        _, value, _ = self.api.dispatch('POST', '/v1/assets', 'a', dict(name=name,
            media_type=media, content_base64=base64.b64encode(raw).decode()), self.key())
        self.api.library.run_once()
        return value['asset_id']



    def test_missing_query_decoder_is_explicit_capability_error(self):
        body = dict(media_type='image/png', content_base64=base64.b64encode(png()).decode(),
            filters=dict(asset_ids=['a' * 32]))
        with patch.object(image_adapter, '_cli_paths', return_value=(None, None)):
            self.error(503, lambda: self.api.dispatch('POST', '/v1/image-search', 'a', body))






    def test_table_timeout_terminates_owned_descendant(self):
        self.assert_owned_descendant_stops(lambda argv: table_adapter._run(argv, 1024, time.monotonic() + .15), 'table_cli_timeout')

    def test_pdf_timeout_terminates_owned_descendant(self):
        with patch.object(pdf_adapter, 'CLI_TIMEOUT_SECONDS', .15):
            self.assert_owned_descendant_stops(lambda argv: pdf_adapter._run_cli(argv, 1024), 'pdf_cli_timeout')

    def assert_owned_descendant_stops(self, runner, code):
        with tempfile.TemporaryDirectory(prefix='navigator-review-child-') as directory:
            pidfile = Path(directory) / 'pid'
            heartbeat = Path(directory) / 'heartbeat'
            script = ('import os,time\np=os.fork()\n'
                f'if p:\n open({str(pidfile)!r},"w").write(str(p))\n os._exit(0)\n'
                f'while True:\n open({str(heartbeat)!r},"w").write(str(time.monotonic()))\n time.sleep(.02)\n')
            child = None
            try:
                self.error(408, lambda: runner([sys.executable, '-c', script]), code)
                child = int(pidfile.read_text())
                before = heartbeat.read_text()
                time.sleep(.1)
                self.assertEqual(heartbeat.read_text(), before, 'descendant still updates heartbeat')
            finally:
                if child:
                    try:
                        os.kill(child, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_inverted_header_assignment_window_holds(self):
        candidate = layout_fixture().replace(b'xMax="70"', b'xMax="560"', 1)
        result = table_adapter.parse_layout(candidate)
        self.assertEqual(result['status'], 'hold')
        self.assertEqual(result['rows'], [])

    def test_high_confidence_ocr_never_supports_normative_claim(self):
        excerpt = dict(quote='maximum allowable pressure is 580 psi (4.0 MPa)',
            evidence=dict(analysis_version='tesseract-image-v1:' + 'a' * 64),
            locator=dict(kind='image_region'), confidence=99, review_required=True)
        status, reason, claims = grounded._assessment([excerpt], 'maximum_allowable_pressure')
        self.assertEqual((status, reason, claims), ('hold', 'ocr_claim_support_unreviewed', []))

    def test_empty_ocr_job_visual_metadata_tampering_rejected(self):
        asset = self.ingest(png(), 'image/png')
        with self.store.tx() as db:
            row = db.execute('SELECT id,result FROM jobs WHERE tenant=? AND asset=?', ('A', asset)).fetchone()
            result = json.loads(row['result'])
            self.assertEqual(result['segments'], [])
            self.assertEqual(result['ocr_state'], 'unknown')
            result['pixel_sha256'] = '0' * 64
            result['visual_signature']['values'] = [0] * 256
            db.execute('UPDATE jobs SET result=? WHERE tenant=? AND id=?', (canonical(result), 'A', row['id']))
            job_id = row['id']
        self.error(409, lambda: self.api.dispatch('GET', '/v1/jobs/' + job_id, 'a'))
        self.error(409, lambda: self.api.dispatch('GET', '/v1/assets/' + asset, 'a'))

    def test_auth_revocation_after_record_save_rolls_back_record_and_idempotency(self):
        asset = self.ingest(b'maximum allowable pressure is 580 psi (4.0 MPa)')
        original = self.api.grounded.answer
        def revoked_after_save(db, ctx, body):
            value = original(db, ctx, body)
            self.auth.revoke('a')
            return value
        key = self.key()
        with patch.object(self.api.grounded, 'answer', side_effect=revoked_after_save):
            self.error(401, lambda: self.api.dispatch('POST', '/v1/grounded-answers', 'a', dict(
                query='maximum allowable pressure', requested_field='maximum_allowable_pressure',
                filters=dict(asset_ids=[asset])), key))
        with self.store.tx() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM records WHERE tenant='A' AND kind='grounded_answers'").fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM idempotency WHERE tenant=? AND key=?', ('A', key)).fetchone()[0], 0)





if __name__ == '__main__':
    unittest.main()
