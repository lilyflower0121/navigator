"""Synthetic SQLite maintenance acceptance and release boundaries."""
import base64
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from navigator.api import API,Auth
from navigator.contracts import APIError,canonical,digest
from navigator.governance import etag
from navigator.maintenance import PROFILE
from navigator.store import Store


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'db.sqlite'
        self.auth=Auth({'owner':dict(tenant='A',actor='owner',actor_type='human',revision=1,scopes=['read','write','delete']),
            'reader':dict(tenant='A',actor='reader',actor_type='human',revision=1,scopes=['read','write']),
            'foreign':dict(tenant='B',actor='foreign',actor_type='human',revision=1,scopes=['read','write'])})
        self.store=Store(self.path);self.api=API(self.store,self.auth);self.n=0;self.now=1791936000.
        self.api.governance.clock=lambda:self.now;self.api.maintenance.clock=lambda:self.now
        self.project=self.call('/v1/projects',dict(name='local verified library'))[1]['id']
        self.call('/members',dict(actor='reader',role='reader',active=True),match=self.project_tag())

    def tearDown(self):
        self.store.close();self.tmp.cleanup()

    def key(self): self.n+=1;return 'maintenance-test-'+str(self.n)
    def call(self,suffix,body=None,match=None,token='owner',key=None,method=None):
        path=suffix if suffix.startswith('/v1/') else '/v1/projects/'+self.project+suffix
        return self.api.dispatch(method or ('POST' if body is not None else 'GET'),path,token,body,key or (self.key() if body is not None else None),if_match=match)
    def project_tag(self): return self.call('/v1/projects/'+self.project)[2]['ETag']
    def error(self,code,fn,status=None):
        with self.assertRaises(APIError) as caught: fn()
        self.assertEqual(caught.exception.code,code)
        if status: self.assertEqual(caught.exception.status,status)
    def ingest(self,text='source evidence pressure 12 kPa',required=True,parsed=True,raw=None,media='text/plain'):
        asset=self.call('/v1/assets',dict(name='original',media_type=media,content_base64=base64.b64encode(raw if raw is not None else text.encode()).decode()))[1]['asset_id']
        if parsed: self.assertTrue(self.api.library.run_once())
        self.call('/source-bindings',dict(asset_id=asset,source_version=1,required=required,readers=['owner','reader']),match=self.project_tag())
        return asset
    def maintenance_run(self,finish=True):
        run=self.call('/maintenance-runs',dict(profile=PROFILE))[1]
        if finish: self.assertTrue(self.api.maintenance.run_once());run=self.call('/runs/'+run['id'])[1]
        return run
    def release(self,run): return self.call('/library-releases/'+run['release_id'])[1]
    def validate(self,run):
        r=self.release(run);return self.call('/library-releases/'+r['id']+'/validate',{},match=etag(r))[1]
    def publication(self,run):
        r=self.validate(run);p=self.call('/library-releases/'+r['id']+'/proposals',{})[1]
        self.call('/proposals/'+p['id']+'/reviews',dict(action='select',option_id='publish-release',reason='verified source/index/coverage'),match=etag(p))
        p=self.call('/proposals/'+p['id'])[1]
        a=self.call('/proposals/'+p['id']+'/approvals',dict(revision=p['revision'],option_id='publish-release',payload_digest=p['options'][0]['payload_digest'],basis_digest=p['basis_digest'],expires_at=self.now+60),match=etag(p))[1]
        return r,p,a
    def publish(self,r,p,a,key=None,match=None):
        head=self.call('/library-head')[1]
        return self.call('/library-releases/'+r['id']+'/publish',dict(proposal_id=p['id'],approval_id=a['id'],manifest_digest=r['manifest_digest'],expected_head_revision=r['manifest']['expected_head_revision']),match=match or etag(head),key=key)

    def test_real_index_staged_until_separate_publication_and_pin(self):
        asset=self.ingest();run=self.maintenance_run();r=self.release(run)
        self.assertEqual(run['state'],'succeeded');self.assertEqual(r['state'],'building')
        self.assertIsNone(self.call('/library-head')[1]['release_id'])
        self.error('library_not_published',lambda:self.call('/read-bindings',{}))
        with self.store.tx() as db:
            g=db.execute('SELECT * FROM ph4_index_generations').fetchone()
            self.assertEqual(g['artifact_hash'],r['manifest']['index_hash']);self.assertGreater(g['chunk_count'],0)
            self.assertEqual(db.execute('SELECT count(*) FROM ph4_heads').fetchone()[0],0)
        r,p,a=self.publication(run);head_tag=etag(self.call('/library-head')[1]);key=self.key()
        published=self.publish(r,p,a,key,head_tag)[1]
        self.assertEqual(published['state'],'published');self.assertEqual(published['current_use_state'],'resolved')
        self.now+=1000
        replay=self.publish(r,p,a,key,head_tag)[1];self.assertEqual(replay,published)
        binding=self.call('/read-bindings',{})[1]
        queried=self.call('/release-queries',dict(binding_id=binding['id'],query='pressure',mode='evidence'))[1]
        self.assertEqual(queried['status'],'supported');self.assertEqual(queried['binding']['release_id'],r['id'])
        self.assertEqual(queried['results'][0]['evidence']['asset_id'],asset)
        self.error('operational_authority_unavailable',lambda:self.call('/release-queries',dict(binding_id=binding['id'],query='pressure',mode='operational')),501)
        second=self.maintenance_run();self.assertEqual(self.call('/library-head')[1]['release_id'],r['id'])
        self.assertEqual(self.call('/library-head')[1]['freshness'],'current')
        r2,p2,a2=self.publication(second);self.publish(r2,p2,a2)
        query2=self.call('/release-queries',dict(binding_id=binding['id'],query='pressure',mode='evidence'))[1]
        self.assertEqual(query2['binding']['release_id'],r['id'])
        self.assertNotEqual(self.call('/library-head')[1]['release_id'],r['id'])

    def test_required_failure_retry_success_reuse_and_restart(self):
        self.ingest();pending=self.ingest('pending segment',parsed=False);run=self.maintenance_run()
        self.assertEqual(run['state'],'completed_with_errors');self.assertEqual(run['counts']['succeeded'],1)
        self.assertEqual(self.release(run)['state'],'failed')
        self.error('release_not_ready',lambda:self.validate(run))
        self.store.close();self.store=Store(self.path);self.api=API(self.store,self.auth)
        self.api.governance.clock=lambda:self.now;self.api.maintenance.clock=lambda:self.now
        self.assertTrue(self.api.library.run_once())
        run=self.call('/runs/'+run['id'])[1]
        self.call('/runs/'+run['id']+'/retry-failed-items',{},match=etag(run))
        self.assertTrue(self.api.maintenance.run_once())
        done=self.call('/runs/'+run['id'])[1];self.assertEqual(done['counts']['succeeded'],2)
        items=self.call('/runs/'+run['id']+'/items')[1]['items'];self.assertEqual(sorted(i['attempt'] for i in items),[1,2])
        self.validate(done)
        second=self.maintenance_run();items=self.call('/runs/'+second['id']+'/items')[1]['items'];self.assertTrue(all(i['reused'] for i in items))

    def test_optional_failure_coverage_exclusion_can_be_reviewed(self):
        self.ingest();self.ingest('unparsed optional',required=False,parsed=False);run=self.maintenance_run()
        self.assertEqual(run['state'],'completed_with_errors');r=self.validate(run)
        self.assertTrue(r['manifest']['coverage']['required_complete']);self.assertEqual(r['manifest']['coverage']['optional_exclusions'],1)
        exclusion=next(i for i in r['manifest']['coverage']['items'] if not i['required'])
        self.assertEqual(exclusion['exclusion_reason'],'source_analysis_unavailable')

    def test_lease_recovery_fences_old_worker_and_preserves_succeeded_item(self):
        self.ingest();self.ingest('another source');run=self.maintenance_run(False);old=self.api.maintenance.claim(lease_seconds=10)
        self.assertTrue(self.api.maintenance.process_item(old));self.now+=11
        new=self.api.maintenance.claim();self.assertEqual(new['run_id'],old['run_id']);self.assertGreater(new['epoch'],old['epoch'])
        self.error('worker_fenced',lambda:self.api.maintenance.finish(old))
        while self.api.maintenance.process_item(new): pass
        self.api.maintenance.finish(new);items=self.call('/runs/'+run['id']+'/items')[1]['items']
        self.assertTrue(all(i['attempt']==1 for i in items));self.assertEqual(self.call('/runs/'+run['id'])[1]['attempt'],2)

    def test_cancel_request_fence_and_observed_worker_stop(self):
        self.ingest();run=self.maintenance_run(False);claim=self.api.maintenance.claim();run=self.call('/runs/'+run['id'])[1]
        cancelled=self.call('/runs/'+run['id']+'/cancel',dict(reason='local cancellation'),match=etag(run))[1]
        self.assertEqual(cancelled['state'],'cancel_requested');self.assertEqual(cancelled['processing_stop'],'not_confirmed')
        self.error('worker_fenced',lambda:self.api.maintenance.process_item(claim))
        self.api.maintenance.ack_cancel(claim)
        stopped=self.call('/runs/'+run['id'])[1];self.assertEqual(stopped['state'],'cancelled');self.assertEqual(stopped['processing_stop'],'observed_stopped')
        self.error('resume_not_allowed',lambda:self.call('/runs/'+run['id']+'/resume',{},match=etag(stopped)))
        with self.store.tx() as db: self.assertEqual(db.execute('SELECT count(*) FROM ph4_index_generations').fetchone()[0],0)

    def test_cancel_requested_blocks_new_worker_until_observed_stop(self):
        self.ingest();run=self.maintenance_run(False);old=self.api.maintenance.claim()
        run=self.call('/runs/'+run['id'])[1]
        self.call('/runs/'+run['id']+'/cancel',{},match=etag(run))
        self.error('resource_busy',lambda:self.maintenance_run(False))
        self.assertIsNone(self.api.maintenance.claim())
        self.api.maintenance.ack_cancel(old)
        replacement=self.maintenance_run(False);new=self.api.maintenance.claim()
        self.assertEqual(new['run_id'],replacement['id'])
        self.error('worker_fenced',lambda:self.api.maintenance.finish(old))

    def test_cancel_requested_queues_schedule_until_lease_expires(self):
        self.ingest();run=self.maintenance_run(False);old=self.api.maintenance.claim()
        run=self.call('/runs/'+run['id'])[1]
        self.call('/runs/'+run['id']+'/cancel',{},match=etag(run))
        definition=dict(name='lease waiting tick',timezone='UTC',frequency='manual_ticks',next_scheduled_for=self.now,catch_up='latest',
            allowed_lateness_seconds=60,max_backlog=2,profile=PROFILE,publication_policy='human_review',active=True)
        schedule=self.call('/maintenance-schedules',definition)[1]
        occurrence=self.call('/maintenance-schedules/'+schedule['id']+'/occurrences',dict(scheduled_for=self.now))[1]
        scheduled=self.call('/runs/'+occurrence['run_id'])[1]
        self.assertEqual((scheduled['state'],scheduled['blocker']),('queued','resource_busy'))
        self.assertIsNone(self.api.maintenance.claim())
        self.now+=31
        new=self.api.maintenance.claim();self.assertEqual(new['run_id'],scheduled['id'])
        cancelled=self.call('/runs/'+run['id'])[1]
        self.assertEqual((cancelled['state'],cancelled['processing_stop']),('cancelled','not_confirmed'))
        self.error('worker_fenced',lambda:self.api.maintenance.finish(old))

    def test_epoch_mid_build_blocks_ready_and_missing_cas(self):
        self.ingest();run=self.maintenance_run(False);claim=self.api.maintenance.claim()
        self.api.maintenance.process_item(claim)
        self.call('/contexts',dict(context=dict(scaffold_type='suspended'),reason='changed while worker active'),match=self.project_tag())
        self.api.maintenance.finish(claim);self.assertEqual(self.release(run)['state'],'stale')
        self.error('precondition_required',lambda:self.call('/runs/'+run['id']+'/cancel',{}),428)
        self.error('revision_mismatch',lambda:self.call('/runs/'+run['id']+'/cancel',{},match='"999"'),412)

    def test_two_candidates_head_competition_and_approval_expiry(self):
        self.ingest();one=self.maintenance_run();two=self.maintenance_run();r1,p1,a1=self.publication(one);r2,p2,a2=self.publication(two)
        old=etag(self.call('/library-head')[1]);self.publish(r1,p1,a1,match=old)
        self.error('revision_mismatch',lambda:self.publish(r2,p2,a2,match=old),412)
        self.error('proposal_stale',lambda:self.publish(r2,p2,a2))
        third=self.maintenance_run();r3,p3,a3=self.publication(third);self.now+=60
        self.error('approval_expired',lambda:self.publish(r3,p3,a3))
        with self.store.tx() as db: self.assertEqual(db.execute('SELECT consumed FROM ph4_approvals WHERE id=?',(a3['id'],)).fetchone()[0],0)

    def test_tampered_generation_and_current_acl_block_old_bindings(self):
        asset=self.ingest();run=self.maintenance_run();r,p,a=self.publication(run);self.publish(r,p,a);binding=self.call('/read-bindings',{})[1]
        with self.store.tx() as db:
            chunk=db.execute('SELECT * FROM ph4_index_chunks LIMIT 1').fetchone();tampered=json.loads(chunk['data']);tampered['text']='fabricated'
            db.execute('UPDATE ph4_index_chunks SET data=? WHERE generation=? AND id=?',(canonical(tampered),chunk['generation'],chunk['id']))
        self.error('index_integrity_failed',lambda:self.call('/release-queries',dict(binding_id=binding['id'],query='pressure',mode='evidence')))
        self.call('/source-bindings',dict(asset_id=asset,source_version=1,required=True,readers=['owner']),match=self.project_tag())
        self.error('not_found',lambda:self.call('/read-bindings/'+binding['id'],token='reader'),404)
        self.error('not_found',lambda:self.call('/runs/'+run['id']+'/event-queries',dict(after=0),token='reader'),404)
        self.error('not_found',lambda:self.call('/library-releases/'+r['id'],token='foreign'),404)

    def test_schedule_unique_occurrence_queue_pause_cancel_and_events(self):
        self.ingest();manual=self.maintenance_run(False)
        self.error('resource_busy',lambda:self.maintenance_run(False))
        definition=dict(name='manual ticks only',timezone='UTC',frequency='manual_ticks',next_scheduled_for=self.now,catch_up='latest',
            allowed_lateness_seconds=60,max_backlog=2,profile=PROFILE,publication_policy='human_review',active=True)
        schedule=self.call('/maintenance-schedules',definition)[1]
        path='/maintenance-schedules/'+schedule['id']+'/occurrences'
        first=self.call(path,dict(scheduled_for=self.now))[1];second=self.call(path,dict(scheduled_for=self.now))[1]
        self.assertEqual(first['run_id'],second['run_id']);self.assertEqual(self.call('/runs/'+first['run_id'])[1]['blocker'],'resource_busy')
        self.error('blocked_backlog',lambda:self.call(path,dict(scheduled_for=self.now-1)))
        schedule_tag=etag(schedule);definition['active']=False;self.call('/maintenance-schedules/'+schedule['id']+'/revisions',definition,match=schedule_tag)
        self.error('schedule_paused',lambda:self.call(path,dict(scheduled_for=self.now-2)))
        self.api.maintenance.run_once();self.api.maintenance.run_once()
        events=self.call('/runs/'+first['run_id']+'/event-queries',dict(after=0,limit=2))[1]
        self.assertEqual(len(events['events']),2);self.assertEqual(events['cursor'],2)
        remaining=self.call('/runs/'+first['run_id']+'/event-queries',dict(after=2))[1]
        self.assertTrue(remaining['events']);self.assertGreater(remaining['cursor'],events['cursor'])
        run=self.call('/runs/'+first['run_id'])[1];self.call('/runs/'+run['id']+'/cancel',{},match=etag(run))
        same=self.call(path,dict(scheduled_for=self.now))[1];self.assertEqual(same['state'],'cancelled');self.assertEqual(same['run_id'],run['id'])

    def test_delete_fences_active_run_and_denies_snapshot(self):
        asset=self.ingest();run=self.maintenance_run(False);claim=self.api.maintenance.claim()
        self.call('/v1/assets/'+asset,{},method='DELETE')
        self.error('worker_fenced',lambda:self.api.maintenance.finish(claim))
        self.error('not_found',lambda:self.call('/runs/'+run['id']))

    def test_missing_segment_never_claims_complete_source_coverage(self):
        asset=self.ingest('name,value\npressure,12\ntemperature,20\n',media='text/csv')
        with self.store.tx() as db:
            rows=db.execute('SELECT id FROM segments WHERE asset=?',(asset,)).fetchall()
            self.assertGreaterEqual(len(rows),2)
            db.execute('DELETE FROM segments WHERE id=?',(rows[0][0],))
        run=self.maintenance_run();self.assertEqual(run['state'],'completed_with_errors')
        items=self.call('/runs/'+run['id']+'/items')[1]['items']
        self.assertEqual(items[0]['error'],'analysis_integrity_failed')
        self.error('release_not_ready',lambda:self.validate(run))

    def test_worker_write_gate_is_checked_after_acceptance(self):
        self.ingest();run=self.maintenance_run(False)
        self.store.worker_access=lambda tenant,actor,asset:False
        self.assertTrue(self.api.maintenance.run_once())
        blocked=self.call('/runs/'+run['id'])[1]
        self.assertEqual(blocked['state'],'blocked_permission')
        with self.store.tx() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM ph4_index_generations').fetchone()[0],0)

    def test_lease_expiry_during_real_item_work_rolls_back_result_and_cache(self):
        self.ingest();run=self.maintenance_run(False);claim=self.api.maintenance.claim(lease_seconds=10)
        real=self.api.maintenance._item_chunks
        def delayed(*args):
            result=real(*args);self.now+=11;return result
        self.api.maintenance._item_chunks=delayed
        self.error('worker_fenced',lambda:self.api.maintenance.process_item(claim))
        with self.store.tx() as db:
            item=json.loads(db.execute('SELECT data FROM ph4_run_items WHERE run=?',(run['id'],)).fetchone()[0])
            self.assertEqual((item['state'],item['attempt']),('queued',0))
            self.assertEqual(db.execute('SELECT count(*) FROM ph4_item_results').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM ph4_run_events WHERE run=?',(run['id'],)).fetchone()[0],2)

    def test_permission_revocation_during_real_item_work_rolls_back_result(self):
        self.ingest();run=self.maintenance_run(False);claim=self.api.maintenance.claim()
        real=self.api.maintenance._item_chunks
        def revoked(*args):
            result=real(*args);self.auth.revoke('owner');return result
        self.api.maintenance._item_chunks=revoked
        self.error('permission_revoked',lambda:self.api.maintenance.process_item(claim))
        with self.store.tx() as db:
            item=json.loads(db.execute('SELECT data FROM ph4_run_items WHERE run=?',(run['id'],)).fetchone()[0])
            self.assertEqual((item['state'],item['attempt']),('queued',0))
            self.assertEqual(db.execute('SELECT count(*) FROM ph4_item_results').fetchone()[0],0)

    def test_finish_end_fence_rolls_back_staged_generation(self):
        self.ingest();run=self.maintenance_run(False);claim=self.api.maintenance.claim(lease_seconds=10)
        self.assertTrue(self.api.maintenance.process_item(claim))
        real=self.api.maintenance._current
        def delayed(*args,**kwargs):
            state=real(*args,**kwargs);self.now+=11;return state
        self.api.maintenance._current=delayed
        self.error('worker_fenced',lambda:self.api.maintenance.finish(claim))
        with self.store.tx() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM ph4_index_generations').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM ph4_index_chunks').fetchone()[0],0)
            row=db.execute('SELECT state,data FROM ph4_releases WHERE id=?',(run['release_id'],)).fetchone()
            self.assertEqual(row['state'],'building');self.assertIsNone(json.loads(row['data'])['index_generation_id'])

    def test_both_time_axes_reject_unimplemented_history_in_both_creation_routes(self):
        self.ingest();run=self.maintenance_run();release,proposal,approval=self.publication(run)
        self.publish(release,proposal,approval)
        with self.store.tx() as db:
            before_runs=db.execute('SELECT count(*) FROM ph4_runs').fetchone()[0]
            before_bindings=db.execute('SELECT count(*) FROM ph4_read_bindings').fetchone()[0]
        for route in ('/maintenance-runs','/read-bindings'):
            for axis in ('effective_at','known_at'):
                with self.subTest(route=route,axis=axis):
                    body={axis:self.now-60}
                    if route=='/maintenance-runs': body['profile']=PROFILE
                    self.error('historical_authority_unavailable',lambda:self.call(route,body),501)
        with self.store.tx() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM ph4_runs').fetchone()[0],before_runs)
            self.assertEqual(db.execute('SELECT count(*) FROM ph4_read_bindings').fetchone()[0],before_bindings)
        # A present-time pair is a valid pin; it does not claim historical policy.
        binding=self.call('/read-bindings',dict(effective_at=self.now,known_at=self.now))[1]
        self.assertEqual((binding['effective_at'],binding['known_at']),(self.now,self.now))
