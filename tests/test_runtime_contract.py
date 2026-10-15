"""Local execution fences; no durable agent or approval implementation claimed."""
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
import unittest
from navigator.api import API,Auth
from navigator.contracts import APIError
from navigator.parsing import parse_source
from navigator.store import Store


class RuntimeContractTests(unittest.TestCase):
    def setUp(self):
        self.store=Store(':memory:')
        self.auth=Auth({'owner':dict(tenant='local',actor='a',revision=1,scopes=['read','write','diagnose'])})
        self.api=API(self.store,self.auth)
        self.key=0
    def tearDown(self):self.store.close()
    def post(self,path,body,key=None):
        self.key+=1
        return self.api.dispatch('POST',path,'owner',body,key or f'runtime-key-{self.key}')[1]
    def ingest(self):
        return self.post('/v1/assets',dict(name='synthetic',media_type='text/csv',
            content_base64=base64.b64encode(b'height_cm,unit\n40,cm\n').decode()))
    def test_cancel_revokes_result_without_claiming_actual_parser_stop(self):
        item=self.ingest();claim=self.store.claim(self.auth.authorized_worker)
        job=self.api.dispatch('GET','/v1/jobs/'+claim['id'],'owner')[1]
        value=self.post('/v1/jobs/'+claim['id']+'/cancel',dict(expected_revision=job['revision']))
        self.assertEqual(value['cancel_effect'],dict(result_publication='revoked',processing_stop='not_confirmed'))
        self.assertFalse(self.store.finish(claim,parse_source(claim),self.auth.authorized_worker))
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM segments').fetchone()[0],0)
    def test_cancel_before_first_claim_and_double_retry_have_explicit_effects(self):
        item=self.ingest();id=item['job']['id']
        cancel=self.post('/v1/jobs/'+id+'/cancel',dict(expected_revision=1))
        self.assertEqual(cancel['cancel_effect']['processing_stop'],'not_started')
        body=dict(expected_revision=cancel['revision'])
        def retry(key):
            try:return self.post('/v1/jobs/'+id+'/retry',body,key)['state']
            except APIError as error:return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes=list(pool.map(retry,['retry-event-1','retry-event-2']))
        self.assertEqual(sorted(outcomes),['queued','revision_conflict'])
        claim=self.store.claim(self.auth.authorized_worker)
        self.assertIsNone(self.store.claim(self.auth.authorized_worker))
        self.auth.principals['owner']['expires_at']=0
        self.assertTrue(self.store.finish(claim,parse_source(claim),self.auth.authorized_worker))
        row=self.store.db.execute('SELECT state,error FROM jobs WHERE id=?',(id,)).fetchone()
        self.assertEqual(tuple(row),('blocked','authorization_revoked'))
    def test_simultaneous_same_stream_sequence_rejects_second_input(self):
        item=self.ingest();self.api.library.run_once()
        job=self.api.dispatch('GET','/v1/jobs/'+item['job']['id'],'owner')[1]
        process=self.post('/v1/processes',dict(name='synthetic criterion',required_media=['document'],max_delay_ms=1000,
              criterion=dict(field='height_cm',operator='gte',threshold=35,unit='cm')))
        now=datetime.now(timezone.utc);self.api.diagnosis.clock=lambda:now
        body=dict(process_id=process['id'],stream_id='same-local-stream',sequence=1,event_time=now.isoformat(),input_status='complete',
                  inputs=[job['result']['segments'][0]['evidence']],mode='structured_rules')
        def event(key):
            try:return self.post('/v1/diagnoses',body,key)['decision']
            except APIError as error:return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes=list(pool.map(event,['stream-event-1','stream-event-2']))
        self.assertEqual(sorted(outcomes),['meets_rule','sequence_conflict'])

if __name__=='__main__':unittest.main()
