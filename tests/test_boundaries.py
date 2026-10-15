"""Synthetic contract fixtures, with evidence extracted from actual CSV bytes.

Direct claim/finish calls and transaction-entry hooks make races deterministic;
no external service, model, sensor, or HTTP server participates in these tests.
"""
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from navigator.api import API, Auth
from navigator.contracts import APIError, LIMITS, digest
from navigator.parsing import parse_source
from navigator.store import Store


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / 'boundary.sqlite')
        all_scopes = ['read', 'write', 'delete', 'diagnose', 'export']
        self.auth = Auth({
            'a': dict(tenant='A', actor='alice', revision=1, scopes=all_scopes[:]),
            'b': dict(tenant='B', actor='bob', revision=1, scopes=all_scopes[:]),
            'reader': dict(tenant='A', actor='reader', revision=1, scopes=['read']),
            'other': dict(tenant='A', actor='other', revision=1, scopes=all_scopes[:]),
        })
        self.api = API(self.store, self.auth)
        self.counter = 0
        self.csv = b'pressure_mpa,note\n0.68,synthetic boundary fixture\n'

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def key(self):
        self.counter += 1
        return 'boundary_%04d' % self.counter

    def call(self, method, path, token='a', body=None, key=None):
        return self.api.dispatch(method, path, token, body, key or self.key())

    def error(self, status, fn, code=None):
        with self.assertRaises(APIError) as caught:
            fn()
        self.assertEqual(caught.exception.status, status)
        if code:
            self.assertEqual(caught.exception.code, code)

    def ingest(self, token='a', raw=None, asset=None, key=None):
        body = dict(name='synthetic.csv', media_type='text/csv',
                    content_base64=base64.b64encode(self.csv if raw is None else raw).decode())
        if asset:
            body['asset_id'] = asset
        return self.call('POST', '/v1/assets', token, body, key)[1], body

    def parsed(self, token='a'):
        value, body = self.ingest(token)
        self.assertTrue(self.api.library.run_once())
        result = self.call('POST', '/v1/search', token, dict(query='pressure_mpa'))[1]
        segment = next(s for s in result['results'] if s['evidence']['asset_id'] == value['asset_id'])
        self.assertEqual(segment['fields']['pressure_mpa'], '0.68')
        self.assertEqual(segment['locator']['line_start'], 2)
        self.assertEqual(segment['source_sha256'], digest(self.csv))
        return value, body, segment['evidence']

    def definitions(self, token='a'):
        process_body = dict(name='synthetic process', required_media=['document'], max_delay_ms=3600000,
                            criterion=dict(field='pressure_mpa', operator='gte', threshold=0.5, unit='MPa'))
        process = self.call('POST', '/v1/processes', token, process_body)[1]
        original = json.dumps(dict(format='navigator-cell-layout-v1', fields=[
            dict(field='pressure', cell='B2', required=True),
            dict(field='unknown', cell='C3', required=False)])).encode()
        template_body = dict(name='synthetic template', content_base64=base64.b64encode(original).decode())
        template = self.call('POST', '/v1/templates', token, template_body)[1]
        return process, template, process_body, template_body

    def derived(self, token, ref, process, template):
        answer_body = dict(query='pressure_mpa', filters=dict(asset_ids=[ref['asset_id']]))
        answer_key = self.key()
        answer = self.call('POST', '/v1/answers', token, answer_body, answer_key)[1]
        diagnosis_body = dict(process_id=process['id'], stream_id='synthetic', sequence=1,
                              event_time=datetime.now(timezone.utc).isoformat(), input_status='complete',
                              inputs=[ref], mode='structured_rules')
        diagnosis_key = self.key()
        diagnosis = self.call('POST', '/v1/diagnoses', token, diagnosis_body, diagnosis_key)[1]
        report_body = dict(template_id=template['id'], assignments=[
            dict(field='pressure', evidence=ref, source_field='pressure_mpa')])
        report_key = self.key()
        report = self.call('POST', '/v1/reports', token, report_body, report_key)[1]
        return dict(answers=(answer, answer_body, answer_key), diagnoses=(diagnosis, diagnosis_body, diagnosis_key),
                    reports=(report, report_body, report_key))

    @contextmanager
    def at_transaction(self, change):
        """Change injected authorization after dispatch authenticated, before lock."""
        original = self.store.tx
        fired = False

        @contextmanager
        def hook():
            nonlocal fired
            if not fired:
                fired = True
                change()
            with original() as db:
                yield db

        with patch.object(self.store, 'tx', hook):
            yield

    def test_bilateral_tenant_reads_search_and_evidence_mutations(self):
        fixtures = {}
        for token in ('a', 'b'):
            asset, _, ref = self.parsed(token)
            process, template, _, _ = self.definitions(token)
            fixtures[token] = (asset, ref, process, template, self.derived(token, ref, process, template))
        for owner, intruder in (('a', 'b'), ('b', 'a')):
            asset, ref, process, template, derived = fixtures[owner]
            own_process, own_template = fixtures[intruder][2:4]
            paths = ['/v1/assets/' + asset['asset_id'], '/v1/jobs/' + asset['job']['id'],
                     '/v1/assets/' + asset['asset_id'] + '/versions/1/original',
                     '/v1/processes/' + process['id'], '/v1/templates/' + template['id']]
            paths += ['/v1/' + kind + '/' + row[0]['id'] for kind, row in derived.items()]
            for path in paths:
                with self.subTest(owner=owner, intruder=intruder, path=path):
                    self.error(404, lambda: self.call('GET', path, intruder))
            self.assertEqual(self.call('POST', '/v1/search', intruder,
                             dict(query='pressure_mpa', filters=dict(asset_ids=[asset['asset_id']])))[1]['results'], [])
            self.error(404, lambda: self.call('DELETE', '/v1/assets/' + asset['asset_id'], intruder, {}))
            self.error(404, lambda: self.ingest(intruder, asset=asset['asset_id']))
            self.error(404, lambda: self.call('POST', '/v1/jobs/' + asset['job']['id'] + '/cancel',
                                           intruder, dict(expected_revision=3)))
            diagnosis_body = dict(derived['diagnoses'][1], process_id=own_process['id'], sequence=2)
            self.error(404, lambda: self.call('POST', '/v1/diagnoses', intruder, diagnosis_body))
            diagnosis_body = dict(derived['diagnoses'][1], inputs=[])
            self.error(404, lambda: self.call('POST', '/v1/diagnoses', intruder, diagnosis_body))
            report_body = dict(derived['reports'][1], template_id=own_template['id'])
            self.error(404, lambda: self.call('POST', '/v1/reports', intruder, report_body))
            report_body = dict(derived['reports'][1], assignments=[])
            self.error(404, lambda: self.call('POST', '/v1/reports', intruder, report_body))
            with self.store.tx() as db:
                self.error(404, lambda: self.api.library.resolve(db, self.auth.principals[intruder]['tenant'], ref))

    def test_spoofed_tenant_is_rejected_by_strict_input(self):
        asset, body = self.ingest('b')
        self.error(422, lambda: self.call('POST', '/v1/assets', 'a', dict(body, tenant='B')))
        self.error(422, lambda: self.call('POST', '/v1/search', 'a', dict(query='x', tenant='B')))
        self.error(404, lambda: self.call('GET', '/v1/assets/' + asset['asset_id'], 'a'))

    def test_scope_separation_and_expired_revoked_tokens(self):
        asset, _, ref = self.parsed()
        process, template, process_body, template_body = self.definitions()
        derived = self.derived('a', ref, process, template)
        mutations = [('POST', '/v1/assets', self.ingest()[1]),
                     ('DELETE', '/v1/assets/' + asset['asset_id'], {}),
                     ('POST', '/v1/processes', process_body), ('POST', '/v1/templates', template_body),
                     ('POST', '/v1/diagnoses', dict(derived['diagnoses'][1], sequence=2)),
                     ('POST', '/v1/reports', derived['reports'][1])]
        for method, path, body in mutations:
            with self.subTest(path=path):
                self.error(403, lambda: self.call(method, path, 'reader', body))
        self.call('GET', '/v1/assets/' + asset['asset_id'], 'reader')
        self.auth.revoke('reader')
        self.error(401, lambda: self.call('GET', '/v1/assets/' + asset['asset_id'], 'reader'))
        self.auth.principals['other']['expires_at'] = time.time() - 1
        self.error(401, lambda: self.call('GET', '/v1/capabilities', 'other'))

    def test_idempotency_payload_route_actor_and_tenant_boundaries(self):
        key = 'shared_key_01'
        value, body = self.ingest(key=key)
        replay = self.call('POST', '/v1/assets', body=body, key=key)
        self.assertEqual(replay[:2], (202, value))
        self.error(409, lambda: self.call('POST', '/v1/assets', body=dict(body, name='changed'), key=key))
        self.error(409, lambda: self.call('POST', '/v1/answers', body=dict(query='x'), key=key))
        foreign, _ = self.ingest('b', key=key)
        other, _ = self.ingest('other', key=key)
        self.assertNotEqual(foreign['asset_id'], value['asset_id'])
        self.assertNotEqual(other['asset_id'], value['asset_id'])
        with self.store.tx() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM sources').fetchone()[0], 3)

    def test_simultaneous_same_key_creates_exactly_one_source_and_job(self):
        body = dict(name='synthetic.csv', media_type='text/csv',
                    content_base64=base64.b64encode(self.csv).decode())
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(lambda _: self.api.dispatch('POST', '/v1/assets', 'a', body, 'concurrent_01'), range(16)))
        self.assertTrue(all(response == responses[0] for response in responses))
        with self.store.tx() as db:
            for table in ('assets', 'sources', 'jobs', 'idempotency'):
                self.assertEqual(db.execute('SELECT count(*) FROM ' + table).fetchone()[0], 1)

    def test_same_payload_same_key_on_different_job_routes_conflicts(self):
        asset, _ = self.ingest()
        path = '/v1/jobs/' + asset['job']['id']
        key = self.key()
        body = dict(expected_revision=1)
        cancelled = self.call('POST', path + '/cancel', body=body, key=key)[1]
        self.assertEqual(self.call('POST', path + '/cancel', body=body, key=key)[1], cancelled)
        self.error(409, lambda: self.call('POST', path + '/retry', body=body, key=key), 'idempotency_conflict')

    def test_cache_rechecks_revocation_after_waiting_for_lock(self):
        key = self.key()
        _, body = self.ingest(key=key)
        with self.at_transaction(lambda: self.auth.revoke('a')):
            self.error(401, lambda: self.call('POST', '/v1/assets', body=body, key=key))

    def test_cache_rechecks_scope_after_waiting_for_lock(self):
        key = self.key()
        _, body = self.ingest(key=key)
        with self.at_transaction(lambda: self.auth.principals['a']['scopes'].remove('write')):
            self.error(403, lambda: self.call('POST', '/v1/assets', body=body, key=key))

    def test_cache_rechecks_identity_and_revision_after_waiting_for_lock(self):
        key = self.key()
        _, body = self.ingest(key=key)
        for field, changed in [('tenant', 'B'), ('actor', 'bob'), ('revision', 2)]:
            with self.subTest(field=field):
                original = self.auth.principals['a'][field]
                with self.at_transaction(lambda: self.auth.principals['a'].__setitem__(field, changed)):
                    self.error(403, lambda: self.call('POST', '/v1/assets', body=body, key=key))
                self.auth.principals['a'][field] = original

    def test_read_rechecks_revocation_after_waiting_for_lock(self):
        asset, _, _ = self.parsed()
        with self.at_transaction(lambda: self.auth.revoke('a')):
            self.error(401, lambda: self.call('GET', '/v1/assets/' + asset['asset_id']))

    def test_search_rechecks_scope_after_waiting_for_lock(self):
        self.parsed()
        with self.at_transaction(lambda: self.auth.principals['a']['scopes'].remove('read')):
            self.error(403, lambda: self.call('POST', '/v1/search', body=dict(query='pressure_mpa')))

    def test_mutation_rechecks_tenant_and_actor_after_waiting_for_lock(self):
        for field, changed in [('tenant', 'B'), ('actor', 'bob'), ('revision', 2)]:
            with self.subTest(field=field):
                original = self.auth.principals['a'][field]
                with self.at_transaction(lambda: self.auth.principals['a'].__setitem__(field, changed)):
                    self.error(403, lambda: self.ingest())
                self.auth.principals['a'][field] = original
        with self.store.tx() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM sources').fetchone()[0], 0)

    def test_worker_rechecks_revocation_on_claim_and_commit(self):
        asset, _ = self.ingest()
        self.auth.revoke('a')
        self.assertIsNone(self.store.claim(self.auth.authorized_worker))
        blocked = self.call('GET', '/v1/jobs/' + asset['job']['id'], 'other')[1]
        self.assertEqual((blocked['state'], blocked['error']), ('blocked', 'authorization_revoked'))
        self.auth.principals['a']['active'] = True
        self.call('POST', '/v1/jobs/' + asset['job']['id'] + '/retry', body=dict(expected_revision=blocked['revision']))
        claim = self.store.claim(self.auth.authorized_worker)
        self.auth.principals['a']['scopes'].remove('write')
        self.assertTrue(self.store.finish(claim, parse_source(claim), self.auth.authorized_worker))
        blocked = self.call('GET', '/v1/jobs/' + claim['id'], 'other')[1]
        self.assertEqual((blocked['state'], blocked['error']), ('blocked', 'authorization_revoked'))
        with self.store.tx() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM segments').fetchone()[0], 0)

    def test_worker_commit_checks_token_revocation_expiry_and_revision(self):
        for changed in ('revoked', 'expired', 'revision'):
            with self.subTest(changed=changed):
                self.ingest()
                claim = self.store.claim(self.auth.authorized_worker)
                principal = self.auth.principals['a']
                if changed == 'revoked':
                    self.auth.revoke('a')
                elif changed == 'expired':
                    principal['expires_at'] = time.time() - 1
                else:
                    principal['revision'] = 2
                self.assertTrue(self.store.finish(claim, parse_source(claim), self.auth.authorized_worker))
                blocked = self.call('GET', '/v1/jobs/' + claim['id'], 'other')[1]
                self.assertEqual((blocked['state'], blocked['error']), ('blocked', 'authorization_revoked'))
                principal.update(active=True, revision=1)
                principal.pop('expires_at', None)
        with self.store.tx() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM segments').fetchone()[0], 0)

    def test_cancel_after_claim_rejects_late_completion(self):
        asset, _ = self.ingest()
        claim = self.store.claim(self.auth.authorized_worker)
        job = self.call('GET', '/v1/jobs/' + claim['id'])[1]
        self.error(409, lambda: self.call('POST', '/v1/jobs/' + claim['id'] + '/cancel', body=dict(expected_revision=1)))
        cancelled = self.call('POST', '/v1/jobs/' + claim['id'] + '/cancel', body=dict(expected_revision=job['revision']))[1]
        self.assertGreater(cancelled['epoch'], claim['epoch'])
        self.assertFalse(self.store.finish(claim, parse_source(claim), self.auth.authorized_worker))
        with self.store.tx() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM segments').fetchone()[0], 0)

    def test_lease_takeover_rejects_old_epoch_completion(self):
        self.ingest()
        with patch('navigator.store.time.time', return_value=100.0):
            old = self.store.claim(self.auth.authorized_worker)
        with patch('navigator.store.time.time', return_value=131.0):
            new = self.store.claim(self.auth.authorized_worker)
            self.assertGreater(new['epoch'], old['epoch'])
            self.assertFalse(self.store.finish(old, parse_source(old), self.auth.authorized_worker))
            self.assertTrue(self.store.finish(new, parse_source(new), self.auth.authorized_worker))
            self.assertFalse(self.store.finish(old, parse_source(old), self.auth.authorized_worker))
        with self.store.tx() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM segments').fetchone()[0], 1)

    def test_expired_lease_cannot_commit_without_takeover(self):
        self.ingest()
        with patch('navigator.store.time.time', return_value=100.0):
            claim = self.store.claim(self.auth.authorized_worker)
        with patch('navigator.store.time.time', return_value=131.0):
            self.assertFalse(self.store.finish(claim, parse_source(claim), self.auth.authorized_worker))
        with self.store.tx() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM segments').fetchone()[0], 0)

    def test_retry_revision_and_three_attempt_limit(self):
        asset, _ = self.ingest(raw=b'broken,header\nonly-one-column\n')
        path = '/v1/jobs/' + asset['job']['id']
        for attempt in range(1, 4):
            self.assertTrue(self.api.library.run_once())
            failed = self.call('GET', path)[1]
            self.assertEqual((failed['state'], failed['attempt']), ('failed', attempt))
            self.error(409, lambda: self.call('POST', path + '/retry', body=dict(expected_revision=failed['revision'] - 1)))
            if attempt < 3:
                self.call('POST', path + '/retry', body=dict(expected_revision=failed['revision']))
            else:
                self.error(409, lambda: self.call('POST', path + '/retry', body=dict(expected_revision=failed['revision'])), 'retry_not_allowed')
        self.assertFalse(self.api.library.run_once())

    def test_three_lease_attempts_are_bounded(self):
        asset, _ = self.ingest()
        for now in (100, 131, 162):
            self.assertIsNotNone(self.store.claim(self.auth.authorized_worker, now=now))
        self.assertIsNone(self.store.claim(self.auth.authorized_worker, now=193))
        job = self.call('GET', '/v1/jobs/' + asset['job']['id'])[1]
        self.assertEqual((job['attempt'], job['state'], job['error']), (3, 'failed', 'attempt_limit'))

    def test_source_versions_and_definitions_are_immutable(self):
        first, _, ref = self.parsed()
        original_sha = digest(self.csv)
        changed = self.csv.replace(b'0.68', b'0.91')
        second, _ = self.ingest(raw=changed, asset=first['asset_id'])
        self.assertEqual(second['source_version'], 2)
        self.assertTrue(self.api.library.run_once())
        for version, expected in ((1, self.csv), (2, changed)):
            _, raw, headers = self.call('GET', '/v1/assets/' + first['asset_id'] + '/versions/%d/original' % version)
            self.assertEqual(raw, expected)
            self.assertEqual(headers['X-Source-SHA256'], digest(expected))
        with self.store.tx() as db:
            self.assertEqual(self.api.library.resolve(db, 'A', ref)['source_sha256'], original_sha)
            self.error(404, lambda: self.api.library.resolve(db, 'A', dict(ref, source_version=2)))
            self.error(404, lambda: self.api.library.resolve(db, 'A', dict(ref, analysis_version='wrong-analysis')))
            self.error(404, lambda: self.api.library.resolve(db, 'A', dict(ref, segment_id='f' * 32)))
        process, template, process_body, template_body = self.definitions()
        for kind, record, body in [('processes', process, process_body), ('templates', template, template_body)]:
            self.error(404, lambda: self.call('PUT', '/v1/' + kind + '/' + record['id'], body=body))
            self.assertEqual(self.call('GET', '/v1/' + kind + '/' + record['id'])[1], record)
        latest = self.call('POST', '/v1/search', body=dict(query='pressure_mpa'))[1]['results']
        self.assertEqual([s['evidence']['source_version'] for s in latest], [2])
        all_versions = self.call('POST', '/v1/search', body=dict(query='pressure_mpa', filters=dict(versions='all')))[1]['results']
        self.assertEqual({s['evidence']['source_version'] for s in all_versions}, {1, 2})

    def test_deletion_invalidates_all_versions_records_caches_and_late_jobs(self):
        ingest_key = self.key()
        asset, ingest_body = self.ingest(key=ingest_key)
        self.api.library.run_once()
        ref = self.call('POST', '/v1/search', body=dict(query='pressure_mpa'))[1]['results'][0]['evidence']
        process, template, _, _ = self.definitions()
        derived = self.derived('a', ref, process, template)
        second, second_body = self.ingest(asset=asset['asset_id'])
        claim = self.store.claim(self.auth.authorized_worker)
        self.assertEqual(claim['id'], second['job']['id'])
        foreign, _, _ = self.parsed('b')
        self.call('DELETE', '/v1/assets/' + asset['asset_id'], body={})
        self.assertFalse(self.store.finish(claim, parse_source(claim), self.auth.authorized_worker))
        self.error(410, lambda: self.call('GET', '/v1/assets/' + asset['asset_id']))
        for version in (1, 2):
            self.error(410, lambda: self.call('GET', '/v1/assets/' + asset['asset_id'] + '/versions/%d/original' % version))
        self.error(410, lambda: self.call('POST', '/v1/assets', body=ingest_body, key=ingest_key))
        for kind, (record, body, key) in derived.items():
            with self.subTest(kind=kind):
                self.error(410, lambda: self.call('GET', '/v1/' + kind + '/' + record['id']))
                self.error(410, lambda: self.call('POST', '/v1/' + kind, body=body, key=key))
        self.assertEqual(self.call('POST', '/v1/search', body=dict(query='pressure_mpa', filters=dict(versions='all')))[1]['results'], [])
        self.call('GET', '/v1/assets/' + foreign['asset_id'], 'b')
        self.call('GET', '/v1/processes/' + process['id'])
        self.call('GET', '/v1/templates/' + template['id'])
        with self.store.tx() as db:
            for table in ('sources', 'segments', 'dependencies'):
                self.assertEqual(db.execute('SELECT count(*) FROM ' + table + ' WHERE tenant=?', ('A',)).fetchone()[0], 0)
            for row in db.execute('SELECT result FROM jobs WHERE tenant=?', ('A',)):
                self.assertIsNone(row['result'])
            for row in db.execute('SELECT data FROM records WHERE tenant=? AND kind IN (?,?,?)', ('A', 'answers', 'diagnoses', 'reports')):
                self.assertNotIn('0.68', row['data'])
                self.assertNotIn('synthetic boundary fixture', row['data'])
            for row in db.execute('SELECT data FROM idempotency WHERE tenant=?', ('A',)):
                self.assertNotIn('0.68', row['data'])
                self.assertNotIn('synthetic boundary fixture', row['data'])
        self.assertIsNone(self.store.claim(self.auth.authorized_worker))

    def test_deleted_dependency_cache_handles_colon_in_server_actor(self):
        self.auth.principals['a']['actor'] = 'organization:alice'
        asset, _, ref = self.parsed()
        key = self.key()
        body = dict(query='pressure_mpa')
        self.call('POST', '/v1/answers', body=body, key=key)
        self.call('DELETE', '/v1/assets/' + asset['asset_id'], body={})
        with self.store.tx() as db:
            cached = db.execute('SELECT status,data FROM idempotency WHERE tenant=? AND actor=? AND key=?',
                                ('A', 'organization:alice', key)).fetchone()
            self.assertEqual(cached['status'], 410)
            self.assertNotIn('0.68', cached['data'])
        self.error(410, lambda: self.call('POST', '/v1/answers', body=body, key=key))

    def test_cached_answers_recheck_extracted_fields_and_source_integrity(self):
        asset, _, ref = self.parsed()
        key = self.key()
        body = dict(query='pressure_mpa')
        answer = self.call('POST', '/v1/answers', body=body, key=key)[1]
        with self.store.tx() as db:
            stored = db.execute('SELECT data FROM segments WHERE tenant=? AND id=?', ('A', ref['segment_id'])).fetchone()
            original = stored['data']
            corrupt = json.loads(original)
            corrupt['fields']['pressure_mpa'] = '99.99'
            db.execute('UPDATE segments SET data=? WHERE tenant=? AND id=?', (json.dumps(corrupt), 'A', ref['segment_id']))
        for operation in [lambda: self.call('GET', '/v1/answers/' + answer['id']),
                          lambda: self.call('POST', '/v1/answers', body=body, key=key),
                          lambda: self.call('POST', '/v1/search', body=body)]:
            self.error(409, operation, 'evidence_integrity_failed')
        with self.store.tx() as db:
            db.execute('UPDATE segments SET data=? WHERE tenant=? AND id=?', (original, 'A', ref['segment_id']))
            db.execute('UPDATE sources SET raw=? WHERE tenant=? AND asset=?', (self.csv.replace(b'0.68', b'9.99'), 'A', asset['asset_id']))
        self.error(409, lambda: self.call('POST', '/v1/answers', body=body, key=key), 'source_integrity_failed')
        self.error(409, lambda: self.call('GET', '/v1/assets/' + asset['asset_id'] + '/versions/1/original'), 'source_integrity_failed')

    def test_capacity_rejection_rolls_back_partial_asset_and_idempotency(self):
        with patch.dict(LIMITS, active_jobs=1):
            self.ingest()
            self.error(413, lambda: self.ingest(), 'job_capacity_limit')
        with self.store.tx() as db:
            for table in ('assets', 'sources', 'jobs', 'idempotency'):
                self.assertEqual(db.execute('SELECT count(*) FROM ' + table).fetchone()[0], 1)

    def test_idempotency_capacity_allows_replay_without_new_write(self):
        key = self.key()
        with patch.dict(LIMITS, idempotency_keys=1):
            value, body = self.ingest(key=key)
            self.assertEqual(self.call('POST', '/v1/assets', body=body, key=key)[1], value)
            self.error(413, lambda: self.ingest(), 'idempotency_capacity_limit')
        with self.store.tx() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM assets').fetchone()[0], 1)


if __name__ == '__main__':
    unittest.main()
