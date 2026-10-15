"""Independent post-implementation adversarial boundary regressions.

The vector fixture is synthetic software, never a quality/model benchmark.
HTTP tests use the real loopback host and do not substitute mocked sockets.
"""
import base64
import http.client
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from navigator.api import API, Auth
from navigator.contracts import APIError, MAX_BODY, canonical, digest
from navigator.governance import etag
from navigator.http import Host
from navigator.maintenance import PROFILE
from navigator.store import Store
from tests.test_text_retrieval import DeterministicVectorFixture


class IndependentBoundaryReviewTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        self.encoder = DeterministicVectorFixture()
        scopes = ['read', 'write', 'delete']
        self.auth = Auth({
            'owner': dict(tenant='A', actor='owner', actor_type='human', revision=1, scopes=scopes),
            'reader': dict(tenant='A', actor='reader', actor_type='human', revision=1, scopes=scopes),
            'machine-owner': dict(tenant='A', actor='owner', actor_type='machine', revision=1, scopes=scopes),
            'foreign': dict(tenant='B', actor='owner', actor_type='human', revision=1, scopes=scopes),
        })
        self.api = API(self.store, self.auth, self.encoder)
        self.now = 1791936000.
        self.api.governance.clock = lambda: self.now
        self.api.maintenance.clock = lambda: self.now
        self.counter = 0
        self.project = self.post('/v1/projects', dict(name='independent synthetic boundary'))[1]['id']
        self.post(self.path('/members'), dict(actor='reader', role='reader', active=True), tag=self.project_tag())
        self.asset = self.ingest('pump pressure recorded 12 kPa')
        self.bind(['owner', 'reader'])

    def tearDown(self):
        self.store.close()

    def key(self):
        self.counter += 1
        return 'independent-boundary-' + str(self.counter)

    def path(self, suffix=''):
        return '/v1/projects/' + self.project + suffix

    def get(self, path, token='owner'):
        return self.api.dispatch('GET', path, token)

    def post(self, path, body, token='owner', tag=None, key=None):
        return self.api.dispatch('POST', path, token, body, key or self.key(), if_match=tag)

    def project_tag(self):
        return self.get(self.path())[2]['ETag']

    def ingest(self, text, asset=None):
        body = dict(name='synthetic fixture', media_type='text/plain',
                    content_base64=base64.b64encode(text.encode()).decode())
        if asset:
            body['asset_id'] = asset
        value = self.post('/v1/assets', body)[1]
        self.assertTrue(self.api.library.run_once())
        return value['asset_id']

    def bind(self, readers):
        return self.post(self.path('/source-bindings'), dict(asset_id=self.asset,
                         source_version=1, required=True, readers=readers), tag=self.project_tag())

    def error(self, code, fn, status=None):
        with self.assertRaises(APIError) as caught:
            fn()
        self.assertEqual(caught.exception.code, code)
        if status is not None:
            self.assertEqual(caught.exception.status, status)

    def published(self):
        run = self.post(self.path('/maintenance-runs'), dict(profile=PROFILE))[1]
        self.assertTrue(self.api.maintenance.run_once())
        path = self.path('/library-releases/' + run['release_id'])
        release = self.get(path)[1]
        release = self.post(path + '/validate', {}, tag=etag(release))[1]
        proposal = self.post(path + '/proposals', {})[1]
        proposal_path = self.path('/proposals/' + proposal['id'])
        self.post(proposal_path + '/reviews', dict(action='select', option_id='publish-release',
                  reason='independent synthetic generation review'), tag=etag(proposal))
        proposal = self.get(proposal_path)[1]
        approval = self.post(proposal_path + '/approvals', dict(revision=proposal['revision'],
            option_id='publish-release', payload_digest=proposal['options'][0]['payload_digest'],
            basis_digest=proposal['basis_digest'], expires_at=self.now + 60), tag=etag(proposal))[1]
        published = self.post(path + '/publish', dict(proposal_id=proposal['id'], approval_id=approval['id'],
            manifest_digest=release['manifest_digest'], expected_head_revision=release['manifest']['expected_head_revision']),
            tag=etag(self.get(self.path('/library-head'))[1]))[1]
        binding = self.post(self.path('/read-bindings'), {})[1]
        return run, published, binding

    def test_identity_transition_cannot_mix_authentication_and_acl_context(self):
        self.bind(['owner'])
        self.auth.principals['transition'] = dict(self.auth.principals['owner'])
        authenticate = self.auth.authenticate
        calls = []

        def transition(token):
            value = authenticate(token)
            if token == 'transition' and not calls:
                self.auth.principals[token] = dict(self.auth.principals['reader'])
            if token == 'transition':
                calls.append(value['actor'])
            return value

        with patch.object(self.auth, 'authenticate', side_effect=transition):
            self.error('authorization_changed', lambda: self.post('/v1/search',
                       dict(query='pump', ranking_mode='bm25'), token='transition'), 403)
        self.assertIsNone(getattr(self.store.request_local, 'context', None))

    def test_machine_token_cannot_borrow_same_actor_human_authority(self):
        count = self.store.db.execute('SELECT count(*) FROM ph4_projects').fetchone()[0]
        self.error('human_approval_required', lambda: self.post('/v1/projects',
                   dict(name='must not be created'), token='machine-owner'), 403)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM ph4_projects').fetchone()[0], count)
        self.error('human_approval_required', lambda: self.post(self.path('/contexts'),
                   dict(context={}, reason='machine forged management'), token='machine-owner', tag=self.project_tag()), 403)

    def test_warm_hybrid_cache_obeys_current_acl_before_any_encoding(self):
        self.post('/v1/search', dict(query='pump', ranking_mode='hybrid'))
        self.assertTrue(self.api.library.ranking.cache)
        self.encoder.inputs.clear()
        self.bind(['owner'])
        with patch.object(self.encoder, 'query', side_effect=AssertionError('hidden corpus encoded')):
            result = self.post('/v1/search', dict(query='pump', ranking_mode='hybrid'), token='reader')[1]
        self.assertEqual(result['results'], [])
        self.assertEqual(self.encoder.inputs, [])

    def test_project_and_tenant_ids_do_not_grant_release_or_binding_access(self):
        run, release, binding = self.published()
        other = self.post('/v1/projects', dict(name='separate same tenant project'))[1]['id']
        for suffix in ('/library-releases/' + release['id'], '/read-bindings/' + binding['id'], '/runs/' + run['id']):
            self.error('not_found', lambda suffix=suffix: self.get('/v1/projects/' + other + suffix), 404)
            self.error('not_found', lambda suffix=suffix: self.get(self.path(suffix), token='foreign'), 404)

    def test_replay_preserves_receipt_headers_but_membership_is_checked_first(self):
        body = dict(profile=PROFILE)
        key = self.key()
        first = self.post(self.path('/maintenance-runs'), body, key=key)
        self.assertEqual(first[2]['ETag'], etag(first[1]))
        self.assertEqual(first[2]['Location'], self.path('/runs/' + first[1]['id']))
        self.assertTrue(self.api.maintenance.run_once())
        replay = self.post(self.path('/maintenance-runs'), body, tag='"obsolete"', key=key)
        self.assertEqual(replay, first)
        with self.store.tx() as db:
            db.execute("UPDATE ph4_project_members SET active=0 WHERE tenant='A' AND project=? AND id='owner'", (self.project,))
        self.error('not_found', lambda: self.post(self.path('/maintenance-runs'), body, key=key), 404)

    def test_receipt_headers_survive_store_restart(self):
        body = dict(profile=PROFILE)
        key = self.key()
        first = self.post(self.path('/maintenance-runs'), body, key=key)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        restored = Store(Path(temporary.name) / 'restored.sqlite')
        self.store.db.backup(restored.db)
        self.store.close()
        self.store = restored
        self.api = API(self.store, self.auth, self.encoder)
        self.api.governance.clock = lambda: self.now
        self.api.maintenance.clock = lambda: self.now
        self.assertEqual(self.post(self.path('/maintenance-runs'), body, tag='"old"', key=key), first)

    def test_readonly_token_cannot_create_binding_or_saved_answer(self):
        _, _, binding = self.published()
        self.auth.principals['read-only'] = dict(self.auth.principals['owner'], scopes=['read'])
        before = self.store.db.execute('SELECT count(*) FROM idempotency').fetchone()[0]
        self.error('scope_denied', lambda: self.post(self.path('/read-bindings'), {}, token='read-only'), 403)
        self.error('scope_denied', lambda: self.post(self.path('/release-answers'),
                   dict(binding_id=binding['id'], query='pump pressure'), token='read-only'), 403)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM idempotency').fetchone()[0], before)
        result = self.api.dispatch('POST', self.path('/release-queries'), 'read-only',
                                  dict(binding_id=binding['id'], query='pump', mode='evidence'))[1]
        self.assertEqual(result['status'], 'supported')

    def test_expired_worker_cannot_persist_item_after_extraction(self):
        run = self.post(self.path('/maintenance-runs'), dict(profile=PROFILE))[1]
        claim = self.api.maintenance.claim(lease_seconds=5)
        original = self.api.maintenance._item_chunks

        def expire_after_extraction(*args):
            value = original(*args)
            self.now += 5
            return value

        with patch.object(self.api.maintenance, '_item_chunks', side_effect=expire_after_extraction):
            self.error('worker_fenced', lambda: self.api.maintenance.process_item(claim), 409)
        self.assertEqual(self.get(self.path('/runs/' + run['id'] + '/items'))[1]['items'][0]['state'], 'queued')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM ph4_item_results').fetchone()[0], 0)

    def test_revoked_worker_cannot_persist_item_after_extraction(self):
        self.post(self.path('/maintenance-runs'), dict(profile=PROFILE))
        claim = self.api.maintenance.claim()
        original = self.api.maintenance._item_chunks

        def revoke_after_extraction(*args):
            value = original(*args)
            self.auth.revoke('owner')
            # Revoke every active write credential for the run's actor.
            self.auth.revoke('machine-owner')
            return value

        with patch.object(self.api.maintenance, '_item_chunks', side_effect=revoke_after_extraction):
            self.error('permission_revoked', lambda: self.api.maintenance.process_item(claim), 403)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM ph4_item_results').fetchone()[0], 0)
        item = json.loads(self.store.db.execute('SELECT data FROM ph4_run_items').fetchone()[0])
        self.assertEqual((item['state'], item['attempt']), ('queued', 0))

    def test_release_answer_uses_only_the_persisted_binding_generation(self):
        _, release, binding = self.published()
        self.ingest('HIDDEN_UNBOUND_LATEST pump pressure 999 kPa')
        with patch.object(self.api.library, 'search', side_effect=AssertionError('live latest search mixed in')):
            answer = self.post(self.path('/release-answers'), dict(binding_id=binding['id'], query='pump pressure'))[1]
        self.assertEqual(answer['binding']['release_id'], release['id'])
        self.assertTrue(answer['answer']['excerpts'])
        self.assertNotIn('HIDDEN_UNBOUND_LATEST', canonical(answer))
        self.assertIsNone(answer['model_used'])
        self.assertIsNone(answer['answer']['generated_answer'])
        self.assertEqual(answer['role'], 'source_reference_answer')

    def test_saved_answer_and_receipt_close_when_pinned_source_is_hidden(self):
        _, _, binding = self.published()
        body = dict(binding_id=binding['id'], query='pump pressure')
        key = self.key()
        answer = self.post(self.path('/release-answers'), body, token='reader', key=key)[1]
        self.bind(['owner'])
        self.error('permission_revoked', lambda: self.get(self.path('/release-answers/' + answer['id']), token='reader'), 403)
        self.error('permission_revoked', lambda: self.post(self.path('/release-answers'), body, token='reader', key=key), 403)

    def test_receipt_recovery_is_distinct_from_new_execution_after_epoch_change(self):
        _, _, binding = self.published()
        body = dict(binding_id=binding['id'], query='pump pressure')
        key = self.key()
        first = self.post(self.path('/release-answers'), body, key=key)
        self.ingest('pump changed reference 99 kPa', asset=self.asset)
        self.assertEqual(self.post(self.path('/release-answers'), body, key=key), first)
        self.error('prerequisites_unverifiable', lambda: self.post(self.path('/release-answers'), body), 409)
        self.error('prerequisites_unverifiable', lambda: self.get(self.path('/release-answers/' + first[1]['id'])), 409)

    def test_unknown_fields_limits_and_arbitrary_urls_never_enter_sources(self):
        self.error('invalid_input_fields', lambda: self.post(self.path('/maintenance-runs'), dict(profile=PROFILE, tenant='B')), 422)
        self.error('invalid_input_text', lambda: self.post(self.path('/release-queries'), dict(binding_id='a'*32,
                   query='x'*1001, mode='evidence')), 422)
        self.error('invalid_input_fields', lambda: self.post('/v1/assets', dict(name='remote', media_type='text/plain',
                   source_url='https://example.invalid/private')), 422)


class IndependentHTTPBoundaryReviewTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(':memory:')
        auth = Auth({'local': dict(tenant='A', actor='owner', actor_type='human', revision=1, scopes=['read', 'write'])})
        self.host = Host(API(self.store, auth), port=0, worker=False)
        self.host.start()

    def tearDown(self):
        self.host.close()
        self.store.close()

    def raw(self, headers, body=b'{"name":"HTTP independent"}', path='/v1/projects'):
        conn = http.client.HTTPConnection('127.0.0.1', self.host.server.server_port, timeout=5)
        conn.putrequest('POST', path, skip_host=True)
        conn.putheader('Host', '127.0.0.1:' + str(self.host.server.server_port))
        conn.putheader('Authorization', 'Bearer local')
        conn.putheader('Content-Type', 'application/json')
        for name, value in headers:
            conn.putheader(name, value)
        conn.endheaders(body)
        response = conn.getresponse()
        result = response.status, json.loads(response.read()), dict(response.getheaders())
        conn.close()
        return result

    def test_duplicate_headers_rejected_before_any_project_write(self):
        for headers, code in (([('Authorization', 'Bearer local')], 'authentication_required'),
                              ([('If-Match', '"one"'), ('If-Match', '"two"')], 'duplicate_precondition'),
                              ([('Idempotency-Key', 'header-one'), ('Idempotency-Key', 'header-two')], 'duplicate_precondition')):
            status, value, returned = self.raw(headers)
            self.assertEqual(value['code'], code)
            self.assertIn(status, (401, 422))
            self.assertEqual(returned['Cache-Control'], 'no-store')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM ph4_projects').fetchone()[0], 0)

    def test_shape_limits_and_real_response_strong_etag(self):
        status, value, _ = self.raw([('Content-Length', str(MAX_BODY + 1))])
        self.assertEqual((status, value['code']), (413, 'http_body_limit'))
        body = b'{"name":"HTTP independent"}'
        status, value, headers = self.raw([('Content-Length', str(len(body))), ('Idempotency-Key', 'http-review-key')], body)
        self.assertEqual(status, 201)
        self.assertEqual(headers['ETag'], '"' + digest(value) + '"')
        self.assertFalse(headers['ETag'].startswith('W/'))
        replay = self.raw([('Content-Length', str(len(body))), ('Idempotency-Key', 'http-review-key'), ('If-Match', '"old"')], body)
        self.assertEqual((replay[0], replay[1], replay[2]['ETag']), (status, value, headers['ETag']))
        status, value, _ = self.raw([('Content-Length', str(len(body)))], body, '/v1/projects?tenant=B')
        self.assertEqual((status, value['code']), (404, 'not_found'))


if __name__ == '__main__':
    unittest.main()
