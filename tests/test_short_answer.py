import base64,copy,unittest
from navigator.api import API,Auth
from navigator.store import Store
from navigator.contracts import APIError,canonical
from navigator.short_answer import parse,windows,prompt_for

class Model:
    def __init__(self,raw='1|上限は40度です。\n',complete=True):self.raw,self.complete,self.calls=raw,complete,0
    def decide(self,*args):self.calls+=1;return dict(raw_output=self.raw,complete=self.complete,status='generated',output_tokens=8)

class ShortAnswerTests(unittest.TestCase):
    def setUp(self):
        self.s=Store(':memory:');self.model=Model();self.auth=Auth({'a':dict(tenant='a',actor='alice',revision=1,scopes=['read','write','delete']),'b':dict(tenant='b',actor='alice',revision=1,scopes=['read']),'other':dict(tenant='a',actor='other',revision=1,scopes=['read'])});self.api=API(self.s,self.auth,short_answer_model=self.model);self.n=0
        self.aid=self.ingest('温度上限は40度です。')
    def tearDown(self):self.s.close()
    def call(self,m,p,b=None,key=None,token='a'):
        self.n+=1;return self.api.dispatch(m,p,token,b,key or ('test-phase10-'+str(self.n) if m in ('POST','DELETE') else None))[1]
    def ingest(self,text,asset=None):
        body=dict(name='synthetic',media_type='text/plain',content_base64=base64.b64encode(text.encode()).decode())
        if asset:body['asset_id']=asset
        v=self.call('POST','/v1/assets',body);self.api.library.run_once();return v['asset_id']
    def request(self):return dict(query='温度の上限は？',task=dict(kind='reference'),asset_ids=[self.aid],retrieval_policy='static',answer_mode='local_short_answer')
    def answer(self):return self.call('POST','/v1/evidence-tasks',self.request(),key='answer-replay')
    def test_candidate_citations_and_receipt_replay_without_regeneration(self):
        v=self.answer();self.assertEqual(v['state'],'answer_candidate');self.assertFalse(v['generated_answer']['semantic_support_verified']);self.assertEqual(v['claims'],[])
        ref=v['generated_answer']['supports'][0];self.assertIn('40度',ref['quote']);self.assertEqual(ref['source_sha256'],v['scope'][0]['source_sha256'])
        self.api.evidence_assist.short_answer_model=None
        self.assertEqual(self.call('GET','/v1/evidence-tasks/'+v['id']),v);self.assertEqual(self.answer(),v);self.assertEqual(self.model.calls,1)
    def test_model_unknown_conflict_format_and_budget_are_different(self):
        for raw,complete,reason in [('UNKNOWN\n',True,'model_reported_unknown'),('CONFLICT\n',True,'model_reported_conflict'),('4|40',True,'short_answer_citation_bounds'),('1|40',False,'short_answer_not_terminated'),('40',True,'short_answer_format')]:
            self.model.raw,self.model.complete=raw,complete
            v=self.call('POST','/v1/evidence-tasks',self.request());self.assertEqual(v['state'],'model_hold');self.assertEqual(v['reason'],reason);self.assertIsNone(v['generated_answer'])
    def test_unavailable_is_saved_not_regenerated_on_get(self):
        self.api.evidence_assist.short_answer_model=None;v=self.answer();self.assertEqual(v['reason'],'short_answer_model_unavailable')
        self.api.evidence_assist.short_answer_model=self.model;self.assertEqual(self.call('GET','/v1/evidence-tasks/'+v['id']),v);self.assertEqual(self.model.calls,0)
    def test_version_acl_tenant_and_deletion_apply_to_candidate(self):
        v=self.answer();path='/v1/evidence-tasks/'+v['id']
        for token in ('b','other'):
            with self.assertRaises(APIError):self.call('GET',path,token=token)
        access=self.s.resource_access;self.s.resource_access=lambda *a:False
        with self.assertRaises(APIError):self.call('GET',path)
        with self.assertRaises(APIError):self.answer()
        self.s.resource_access=access;self.ingest('上限30度',self.aid)
        with self.assertRaises(APIError):self.call('GET',path)
        self.call('DELETE','/v1/assets/'+self.aid,{})
        with self.assertRaises(APIError):self.answer()
    def test_raw_and_citation_tamper_rejected(self):
        v=self.answer()
        with self.s.tx() as db:
            row=db.execute("SELECT data FROM records WHERE kind='evidence_task_internals' AND id=?",(v['id'],)).fetchone();data=__import__('json').loads(row[0]);data['short_answer_receipt']['receipt']['raw_output']='1|999';db.execute("UPDATE records SET data=? WHERE kind='evidence_task_internals' AND id=?",(canonical(data),v['id']))
        with self.assertRaises(APIError):self.call('GET','/v1/evidence-tasks/'+v['id'])
    def test_no_explicit_selectors_or_conditional_mode(self):
        b=self.request();b['task']={'kind':'table_lookup','selectors':[]}
        with self.assertRaises(APIError):self.call('POST','/v1/evidence-tasks',b)
    def test_windows_are_bounded_original_substrings_and_prompt_bound(self):
        v=self.answer();spans=windows('温度',v['excerpts']);self.assertLessEqual(len(spans),4)
        for x in spans:
            original=next(s['quote'] for s in v['excerpts'] if s['evidence']==x['evidence']);self.assertEqual(original[x['start_char']:x['end_char']],x['quote']);self.assertLessEqual(len(x['quote']),240)
        self.assertIn('Answer:',prompt_for('温度',spans))
    def test_parse_rejects_duplicate_invalid_and_multiline_refs(self):
        spans=[dict(quote='40度')]
        for raw in ('1,1|40度','0|40度','1|40度\nother','1|','1|\x00'):
            self.assertEqual(parse(raw,spans,True)['status'],'rejected')

if __name__=='__main__':unittest.main()
