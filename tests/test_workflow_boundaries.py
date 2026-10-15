"""Independent adversarial review of the real project API/SQLite workflow.

Existing acceptance helpers supply only local setup and fixture bytes. Assertions
below cover additional failure sequences, never a synthetic release adapter.
"""
import json
from pathlib import Path
import shutil
import unittest
from tests import test_maintenance as fixture
from navigator.contracts import APIError, canonical, digest
from navigator.governance import etag


class WorkflowReviewTests(unittest.TestCase):
    setUp = fixture.MaintenanceTests.setUp
    tearDown = fixture.MaintenanceTests.tearDown
    key = fixture.MaintenanceTests.key
    call = fixture.MaintenanceTests.call
    project_tag = fixture.MaintenanceTests.project_tag
    error = fixture.MaintenanceTests.error
    ingest = fixture.MaintenanceTests.ingest
    maintenance_run = fixture.MaintenanceTests.maintenance_run
    release = fixture.MaintenanceTests.release
    validate = fixture.MaintenanceTests.validate
    publication = fixture.MaintenanceTests.publication
    publish = fixture.MaintenanceTests.publish


    def adopt(self, proposal):
        path = '/proposals/' + proposal['id']
        self.call(path+'/reviews', dict(action='select', option_id='source-criterion', reason='checked original'), match=etag(proposal))
        selected = self.call(path)[1]
        approval = self.call(path+'/approvals', dict(revision=selected['revision'], option_id='source-criterion', payload_digest=selected['options'][0]['payload_digest'], basis_digest=selected['basis_digest'], expires_at=self.now+60), match=etag(selected))[1]
        approved = self.call(path)[1]
        return self.call(path+'/apply', dict(approval_id=approval['id'], target_revision=approved['target']['revision'], manifest_digest=approved['target']['manifest_digest']), match=etag(approved))[1]


    def test_release_review_cannot_select_structure_option(self):
        self.ingest()
        run = self.maintenance_run()
        release = self.validate(run)
        proposal = self.call('/library-releases/'+release['id']+'/proposals', {})[1]
        with self.assertRaises(APIError):
            self.call('/proposals/'+proposal['id']+'/reviews', dict(action='select', option_id='source-criterion', reason='wrong publication option'), match=etag(proposal))


    def test_cancelled_unexpired_worker_retains_target_lease(self):
        self.ingest()
        run = self.maintenance_run(False)
        old = self.api.maintenance.claim(lease_seconds=30)
        active = self.call('/runs/'+run['id'])[1]
        cancelled = self.call('/runs/'+run['id']+'/cancel', {}, match=etag(active))[1]
        self.assertEqual((cancelled['state'], cancelled['processing_stop']), ('cancel_requested', 'not_confirmed'))
        self.assertGreater(cancelled['lease_until'], self.now)
        self.error('resource_busy', lambda: self.maintenance_run(False))
        self.assertIsNone(self.api.maintenance.claim())
        self.api.maintenance.ack_cancel(old)
        self.assertEqual(self.call('/runs/'+run['id'])[1]['processing_stop'], 'observed_stopped')
        self.maintenance_run(False)
        self.assertIsNotNone(self.api.maintenance.claim())

    def test_late_publication_failure_rolls_back_every_commit_component(self):
        self.ingest()
        run = self.maintenance_run()
        release, proposal, approval = self.publication(run)
        epoch = self.call('/v1/projects/'+self.project)[1]['scope_epoch']
        event = self.api.maintenance._event
        def fail_at_final_event(db, ctx, project, run_id, kind, final=False):
            if kind == 'published':
                raise APIError(409, 'injected_commit_failure')
            return event(db, ctx, project, run_id, kind, final)
        self.api.maintenance._event = fail_at_final_event
        key = self.key()
        try:
            self.error('injected_commit_failure', lambda: self.publish(release, proposal, approval, key=key))
        finally:
            self.api.maintenance._event = event
        with self.store.tx() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM ph4_heads').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT consumed FROM ph4_approvals WHERE id=?', (approval['id'],)).fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT state FROM ph4_proposal_heads WHERE id=?', (proposal['id'],)).fetchone()[0], 'approved')
            self.assertEqual(db.execute('SELECT state FROM ph4_releases WHERE id=?', (release['id'],)).fetchone()[0], 'ready')
            self.assertIsNone(db.execute('SELECT 1 FROM idempotency WHERE key=?', (key,)).fetchone())
            self.assertFalse(any(json.loads(r[0])['type']=='published' for r in db.execute('SELECT data FROM ph4_run_events')))
        self.assertEqual(self.call('/v1/projects/'+self.project)[1]['scope_epoch'], epoch)
        self.assertEqual(self.publish(release, proposal, approval, key=key)[1]['state'], 'published')

    def test_receipt_recovery_with_missing_new_etag_after_external_change(self):
        self.ingest()
        run = self.maintenance_run()
        release, proposal, approval = self.publication(run)
        key = self.key()
        body = dict(proposal_id=proposal['id'], approval_id=approval['id'], manifest_digest=release['manifest_digest'], expected_head_revision=0)
        path = '/library-releases/'+release['id']+'/publish'
        first = self.call(path, body, key=key, match=etag(self.call('/library-head')[1]))
        self.now += 120
        self.call('/contexts', dict(context=dict(scaffold_type='frame'), reason='later context change'), match=self.project_tag())
        for match in (None, etag(self.call('/library-head')[1]), '"arbitrary-old-etag"'):
            self.assertEqual(self.call(path, body, key=key, match=match), first)
        with self.store.tx() as db:
            self.assertEqual(db.execute('SELECT revision FROM ph4_heads').fetchone()[0], 1)
            self.assertEqual(sum(json.loads(r[0])['type']=='published' for r in db.execute('SELECT data FROM ph4_run_events')), 1)

    def test_duplicate_cancel_never_repeats_fence_or_claims_physical_stop(self):
        self.ingest()
        run = self.maintenance_run(False)
        self.api.maintenance.claim(lease_seconds=30)
        active = self.call('/runs/'+run['id'])[1]
        key = self.key()
        first = self.call('/runs/'+run['id']+'/cancel', {}, key=key, match=etag(active))
        self.assertEqual(self.call('/runs/'+run['id']+'/cancel', {}, key=key), first)
        current = self.call('/runs/'+run['id'])[1]
        fresh = self.call('/runs/'+run['id']+'/cancel', {}, match=etag(current))[1]
        self.assertEqual((fresh['epoch'], fresh['state_version'], fresh['processing_stop']), (current['epoch'], current['state_version'], 'not_confirmed'))

    def test_occurrence_receipt_cannot_be_resurrected_by_schedule_revision(self):
        self.ingest()
        definition = dict(name='explicit occurrence', timezone='UTC', frequency='manual_ticks', next_scheduled_for=self.now, catch_up='latest', allowed_lateness_seconds=60, max_backlog=2, profile=fixture.PROFILE, publication_policy='human_review', active=True)
        schedule = self.call('/maintenance-schedules', definition)[1]
        path = '/maintenance-schedules/'+schedule['id']+'/occurrences'
        occurrence = self.call(path, dict(scheduled_for=self.now))[1]
        run = self.call('/runs/'+occurrence['run_id'])[1]
        self.call('/runs/'+run['id']+'/cancel', {}, match=etag(run))
        updated = dict(definition, active=False)
        self.call('/maintenance-schedules/'+schedule['id']+'/revisions', updated, match=etag(schedule))
        repeated = self.call(path, dict(scheduled_for=self.now))[1]
        self.assertEqual((repeated['run_id'], repeated['state'], repeated['schedule_revision']), (run['id'], 'cancelled', 1))
        with self.store.tx() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM ph4_runs').fetchone()[0], 1)

    def test_missing_durable_generation_blocks_publish_without_consumption(self):
        self.ingest()
        run = self.maintenance_run()
        release, proposal, approval = self.publication(run)
        generation = release['manifest']['index_generation_id']
        with self.store.tx() as db:
            db.execute('DELETE FROM ph4_index_generations WHERE id=?', (generation,))
        self.error('index_integrity_failed', lambda: self.publish(release, proposal, approval))
        with self.store.tx() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM ph4_heads').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT consumed FROM ph4_approvals WHERE id=?', (approval['id'],)).fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT state FROM ph4_releases WHERE id=?', (release['id'],)).fetchone()[0], 'ready')

    def test_new_bound_source_invalidates_publication_without_advancing_watermark(self):
        self.ingest()
        run = self.maintenance_run()
        release, proposal, approval = self.publication(run)
        captured = run['captured_watermark']
        self.ingest('new source potentially contradicting existing evidence')
        self.assertEqual(self.call('/runs/'+run['id'])[1]['captured_watermark'], captured)
        self.assertIsNone(self.call('/library-head')[1]['published_watermark'])
        self.error('proposal_stale', lambda: self.publish(release, proposal, approval))
        with self.store.tx() as db:
            self.assertEqual(db.execute('SELECT consumed FROM ph4_approvals WHERE id=?', (approval['id'],)).fetchone()[0], 0)

    def test_published_receipt_and_binding_obey_new_source_acl(self):
        asset = self.ingest()
        run = self.maintenance_run()
        release, proposal, approval = self.publication(run)
        key = self.key()
        body = dict(proposal_id=proposal['id'], approval_id=approval['id'], manifest_digest=release['manifest_digest'], expected_head_revision=0)
        path = '/library-releases/'+release['id']+'/publish'
        self.call(path, body, key=key, match=etag(self.call('/library-head')[1]))
        binding = self.call('/read-bindings', {})[1]
        self.call('/source-bindings', dict(asset_id=asset, source_version=1, required=True, readers=['reader']), match=self.project_tag())
        with self.assertRaises(APIError) as caught:
            self.call(path, body, key=key)
        self.assertIn(caught.exception.status, (403, 404))
        self.error('not_found', lambda: self.call('/read-bindings/'+binding['id']))
        state = self.call('/states')[1]
        self.assertEqual((state['current_use_state'], state['blockers']), ('blocked', ['prerequisites_unverifiable']))
        self.assertNotIn(asset, canonical(state))

    def test_expired_lease_during_source_work_cannot_commit_checkpoint(self):
        self.ingest()
        self.maintenance_run(False)
        claim = self.api.maintenance.claim(lease_seconds=10)
        original = self.api.maintenance._item_chunks
        def expires_during_work(*args):
            result = original(*args)
            self.now += 11
            return result
        self.api.maintenance._item_chunks = expires_during_work
        try:
            self.error('worker_fenced', lambda: self.api.maintenance.process_item(claim))
        finally:
            self.api.maintenance._item_chunks = original
        with self.store.tx() as db:
            item = json.loads(db.execute('SELECT data FROM ph4_run_items').fetchone()[0])
            self.assertEqual((item['state'], item['attempt']), ('queued', 0))
            self.assertEqual(db.execute('SELECT count(*) FROM ph4_item_results').fetchone()[0], 0)
            self.assertFalse(any(json.loads(r[0])['type']=='item_succeeded' for r in db.execute('SELECT data FROM ph4_run_events')))

    def test_authority_revoked_during_source_work_cannot_commit_checkpoint(self):
        self.ingest()
        self.maintenance_run(False)
        claim = self.api.maintenance.claim()
        original = self.api.maintenance._item_chunks
        def revokes_during_work(*args):
            result = original(*args)
            self.auth.revoke('owner')
            return result
        self.api.maintenance._item_chunks = revokes_during_work
        try:
            with self.assertRaises(APIError) as caught:
                self.api.maintenance.process_item(claim)
            self.assertEqual(caught.exception.status, 403)
        finally:
            self.api.maintenance._item_chunks = original
        with self.store.tx() as db:
            item = json.loads(db.execute('SELECT data FROM ph4_run_items').fetchone()[0])
            self.assertEqual((item['state'], item['attempt']), ('queued', 0))
            self.assertEqual(db.execute('SELECT count(*) FROM ph4_item_results').fetchone()[0], 0)

    def test_expiry_during_final_integrity_check_rolls_back_generation(self):
        self.ingest()
        self.maintenance_run(False)
        claim = self.api.maintenance.claim(lease_seconds=10)
        self.assertTrue(self.api.maintenance.process_item(claim))
        original = self.api.maintenance._current
        def expires_during_final_validation(*args, **kwargs):
            result = original(*args, **kwargs)
            self.now += 11
            return result
        self.api.maintenance._current = expires_during_final_validation
        try:
            self.error('worker_fenced', lambda: self.api.maintenance.finish(claim))
        finally:
            self.api.maintenance._current = original
        with self.store.tx() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM ph4_index_generations').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM ph4_index_chunks').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT state FROM ph4_runs').fetchone()[0], 'running')
            manifest = json.loads(db.execute('SELECT data FROM ph4_releases').fetchone()[0])
            self.assertIsNone(manifest['index_generation_id'])
            self.assertFalse(any(json.loads(r[0])['type']=='completed' for r in db.execute('SELECT data FROM ph4_run_events')))


if __name__ == '__main__':
    unittest.main()
