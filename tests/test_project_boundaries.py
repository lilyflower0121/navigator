"""Project ACL remains current on legacy reads, cache receipts and workers."""
import base64
import threading
import unittest
from unittest.mock import patch
from navigator.api import API,Auth
from navigator.contracts import APIError
from navigator.store import Store


class ProjectBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.store=Store(':memory:')
        scopes=['read','write','delete']
        self.auth=Auth({actor:dict(tenant='local-test',actor=actor,actor_type='human',revision=1,scopes=scopes)
                       for actor in ('owner','reader')})
        self.api=API(self.store,self.auth)
        self.n=0
        self.project=self.post('/v1/projects',dict(name='synthetic reference'))[1]['id']
        self.post(self.path('/members'),dict(actor='reader',role='reader',active=True),tag=self.project_tag())
        self.asset=self.post('/v1/assets',dict(name='tiny synthetic',media_type='text/plain',
            content_base64=base64.b64encode(b'synthetic pressure record').decode()))[1]
        self.assertTrue(self.api.library.run_once())
        self.bind(['owner','reader'])

    def tearDown(self):
        self.store.close()

    def key(self):
        self.n+=1
        return 'project-boundary-'+str(self.n)

    def path(self,suffix):
        return '/v1/projects/'+self.project+suffix

    def post(self,path,body,actor='owner',tag=None,key=None):
        return self.api.dispatch('POST',path,actor,body,key or self.key(),if_match=tag)

    def project_tag(self):
        return self.api.dispatch('GET',self.path(''),'owner')[2]['ETag']

    def bind(self,readers,required=True):
        return self.post(self.path('/source-bindings'),dict(asset_id=self.asset['asset_id'],source_version=1,
            required=required,readers=readers),tag=self.project_tag())

    def denied(self,fn,code='permission_revoked',status=403):
        with self.assertRaises(APIError) as caught:
            fn()
        self.assertEqual((caught.exception.status,caught.exception.code),(status,code))

    def test_revocation_closes_original_job_saved_answer_and_receipt(self):
        body=dict(query='synthetic pressure',mode='extractive',filters=dict(asset_ids=[self.asset['asset_id']]))
        key=self.key()
        answer=self.post('/v1/answers',body,actor='reader',key=key)[1]
        self.bind(['owner'])
        for path in ('/v1/assets/'+self.asset['asset_id'],
                     '/v1/assets/'+self.asset['asset_id']+'/versions/1/original',
                     '/v1/jobs/'+self.asset['job']['id'],'/v1/answers/'+answer['id']):
            self.denied(lambda path=path:self.api.dispatch('GET',path,'reader'))
        self.denied(lambda:self.post('/v1/answers',body,actor='reader',key=key))

    def test_filter_precedes_bm25_and_hidden_evidence_is_not_returned(self):
        self.bind(['owner'])
        with patch.object(self.api.library.ranking,'rank',side_effect=AssertionError('hidden source ranked')):
            result=self.post('/v1/search',dict(query='pressure',ranking_mode='bm25'),actor='reader')[1]
        self.assertEqual(result['results'],[])
        self.assertEqual(self.api.library.last_search_stats['eligible_segments'],0)

    def test_reader_cannot_write_new_source_version_delete_or_cancel(self):
        self.denied(lambda:self.post('/v1/assets',dict(asset_id=self.asset['asset_id'],name='revised',
            media_type='text/plain',content_base64=base64.b64encode(b'synthetic update').decode()),actor='reader'),
            'project_role_denied')
        self.denied(lambda:self.api.dispatch('DELETE','/v1/assets/'+self.asset['asset_id'],'reader',{},self.key()),
            'project_role_denied')
        self.denied(lambda:self.post('/v1/jobs/'+self.asset['job']['id']+'/cancel',dict(expected_revision=3),actor='reader'),
            'project_role_denied')

    def test_new_received_version_changes_epoch_without_rebinding_old_source(self):
        before=self.api.dispatch('GET',self.path(''),'owner')[1]
        self.post('/v1/assets',dict(asset_id=self.asset['asset_id'],name='second synthetic',media_type='text/plain',
            content_base64=base64.b64encode(b'synthetic changed record').decode()))
        after=self.api.dispatch('GET',self.path(''),'owner')[1]
        self.assertEqual(after['scope_epoch'],before['scope_epoch']+1)
        with self.store.tx() as db:
            self.assertEqual(db.execute('SELECT version FROM ph4_project_sources').fetchone()[0],1)

    def test_same_receipt_returns_original_project_response_before_stale_etag(self):
        key=self.key();tag=self.project_tag()
        body=dict(context=dict(scaffold_type='suspended'),reason='synthetic context')
        first=self.post(self.path('/contexts'),body,tag=tag,key=key)
        self.post(self.path('/contexts'),dict(context={},reason='new context'),tag=self.project_tag())
        replay=self.post(self.path('/contexts'),body,tag=tag,key=key)
        self.assertEqual(replay,first)
        self.assertEqual(self.post(self.path('/contexts'),body,tag=self.project_tag(),key=key),first)
        self.assertEqual(self.post(self.path('/contexts'),body,key=key),first)
        self.denied(lambda:self.post(self.path('/contexts'),dict(context={},reason='changed payload'),tag=self.project_tag(),key=key),
            'idempotency_conflict',409)

    def test_current_membership_is_checked_before_old_receipt(self):
        body=dict(query='pressure',mode='extractive')
        key=self.key();self.post('/v1/answers',body,actor='reader',key=key)
        self.post(self.path('/members'),dict(actor='reader',role='reader',active=False),tag=self.project_tag())
        self.denied(lambda:self.post('/v1/answers',body,actor='reader',key=key))

    def test_queued_worker_loses_write_rights_before_claim(self):
        second=self.post('/v1/assets',dict(asset_id=self.asset['asset_id'],name='queued',media_type='text/plain',
            content_base64=base64.b64encode(b'synthetic queued').decode()))[1]
        self.bind(['reader'])
        self.assertFalse(self.api.library.run_once())
        with self.store.tx() as db:
            row=db.execute('SELECT state,error FROM jobs WHERE id=?',(second['job']['id'],)).fetchone()
            self.assertEqual(tuple(row),('blocked','authorization_revoked'))

    def test_parallel_requests_do_not_share_actor_context(self):
        self.bind(['owner'])
        observed=[];errors=[]
        barrier=threading.Barrier(2)
        def request(actor):
            try:
                barrier.wait(timeout=3)
                result=self.api.dispatch('POST','/v1/search',actor,dict(query='pressure',ranking_mode='bm25'))[1]
                observed.append((actor,len(result['results'])))
                self.assertIsNone(getattr(self.store.request_local,'context',None))
            except BaseException as error:
                errors.append(error)
        threads=[threading.Thread(target=request,args=(actor,)) for actor in ('reader','owner')]
        for thread in threads:thread.start()
        for thread in threads:thread.join(timeout=5)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors,[])
        self.assertEqual(sorted(observed),[('owner',1),('reader',0)])


if __name__=='__main__':
    unittest.main()
