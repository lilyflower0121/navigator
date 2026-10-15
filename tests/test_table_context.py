import base64,copy,json,unittest
from unittest.mock import patch
from navigator.api import API,Auth
from navigator.store import Store
from navigator.contracts import canonical,digest,APIError
from navigator.table_context import build,POLICY
from navigator.short_answer import parse,windows,prompt_for,LIMITS,IsolatedShortAnswer
from navigator.text_encoder import IsolatedTextEncoder
from navigator.local_decision import LOCAL_MODEL_LOCK
from tests.test_spreadsheet_boundaries import workbook

class Model:
 def __init__(self):self.calls=0
 def decide(self,*args):self.calls+=1;return dict(status='generated',raw_output='1|synthetic candidate\n',complete=True)

class ContextTests(unittest.TestCase):
 def setUp(self):
  self.s=Store(':memory:');self.model=Model();self.api=API(self.s,Auth({'a':dict(tenant='a',actor='alice',revision=1,scopes=['read','write','delete'])}),short_answer_model=self.model);self.n=0
 def tearDown(self):self.s.close()
 def call(self,m,p,b=None):
  self.n+=1;return self.api.dispatch(m,p,'a',b,'phase12-test-'+str(self.n) if m in ('POST','DELETE') else None)[1]
 def ingest(self,rows):
  formatted=[(int(''.join(c for c in row[0][0] if c.isdigit())),[(''.join(c for c in address if c.isalpha()),'text' if isinstance(value,str) else 'number',value) for address,value in row]) for row in rows]
  raw=workbook([('Synthetic',formatted)])
  a=self.call('POST','/v1/assets',dict(name='synthetic',media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',content_base64=base64.b64encode(raw).decode()));self.api.library.run_once();return a['asset_id']
 def answer(self,asset,query='対象Zの全国値は？'):
  return self.call('POST','/v1/evidence-tasks',dict(query=query,task={'kind':'reference'},asset_ids=[asset],retrieval_policy='static',ranking_mode='bm25',answer_mode='local_short_answer'))
 def test_row_header_unit_note_neighbor_are_original_and_replay(self):
  a=self.ingest([[('A1','表 単位kPa')],[('A2','項目'),('B2','全国'),('C2','地域B')],[('A3','別対象'),('B3',5),('C3',6)],[('A4','対象Z'),('B4',17),('C4',19)],[('A5','注：条件付き')]])
  v=self.answer(a);sp=v['stages']['answer_context'];parts=[p for u in sp for p in u['parts']]
  self.assertTrue({1,2,3,4,5}<={p['locator']['row'] for p in parts});self.assertEqual(v['state'],'answer_candidate');self.assertFalse(v['generated_answer']['semantic_support_verified'])
  for p in parts:self.assertEqual(p['start_char'],0);self.assertEqual(p['end_char'],len(p['quote']));self.assertEqual(p['quote_context_sha256'],digest(p['quote'].encode()))
  self.assertEqual(self.call('GET','/v1/evidence-tasks/'+v['id']),v);self.assertEqual(self.model.calls,1)
  self.call('DELETE','/v1/assets/'+a,{})
  with self.assertRaises(APIError):self.call('GET','/v1/evidence-tasks/'+v['id'])
 def test_long_row_is_explicitly_blocked_and_never_truncated(self):
  a=self.ingest([[('A1','単位 m')],[('A2','対象Z'+('長い条件'*600)),('B2',8)]])
  v=self.answer(a);self.assertEqual(v['reason'],'table_context_budget_exceeded');self.assertFalse(v['stages']['context_selection']['context_truncated']);self.assertEqual(self.model.calls,0)
 def test_bundle_parser_flattens_real_parts_without_composite_fake_evidence(self):
  a=self.ingest([[('A1','単位kg')],[('A2','対象Z'),('B2',3)]])
  v=self.answer(a);sp=v['stages']['answer_context'];p=parse('1|3kg',sp,True);self.assertEqual(len(p['supports']),2);self.assertTrue(all('evidence' in s and 'parts' not in s for s in p['supports']))
 def test_legacy_receipt_replays_original_response(self):
  a=self.ingest([[('A1','単位kg')],[('A2','対象Z'),('B2',3)]])
  # Reconstruct a genuine pre-policy saved receipt/snapshot before new render.
  with self.s.request_scope(self.api.auth.authenticate('a')):
   with self.s.tx() as db:
    ctx=self.api.auth.authenticate('a');internal=dict(id='11111111111111111111111111111111',creator='alice',revision=1,input=dict(query='対象Z',task={'kind':'reference'},asset_ids=[a],retrieval_policy='static',ranking_mode='bm25',answer_mode='local_short_answer'),observations=[],asked=[],prompt_revisions={})
    internal['basis']=self.api.evidence_assist._basis(db,ctx,internal['input']);items,_=self.api.evidence_assist._material(db,ctx,internal);selected,*_=self.api.evidence_assist._retrieve(db,ctx,internal);sp=windows('対象Z',selected)
    from navigator.short_answer import PROFILE,WEIGHT_SHA
    receipt=dict(status='generated',raw_output='UNKNOWN',complete=True);binding=digest(dict(profile=PROFILE,weights=WEIGHT_SHA,limits=LIMITS,prompt=prompt_for('対象Z',sp),spans=sp));internal['short_answer_receipt']=dict(request_sha256=binding,receipt=receipt,receipt_sha256=digest(receipt))
    value=self.api.evidence_assist.compute(db,ctx,internal)
    self.assertNotIn('context_selection',value['stages']);self.assertNotIn('short_answer_context_policy',internal)
    self.s.save_record(db,'a','evidence_task_internals',internal,[a]);snap=dict(id=digest(['11111111111111111111111111111111',1])[:32],state_input=internal,response=value);self.s.save_record(db,'a','evidence_task_snapshots',snap,[a])
  self.assertEqual(self.call('GET','/v1/evidence-tasks/11111111111111111111111111111111'),value);self.assertEqual(self.model.calls,0)
 def test_numeric_period_header_and_mixed_media_do_not_silently_disappear(self):
  rows=[[('A1','年度'),('B1',2034),('C1',2035)]]+[[('A'+str(i),'対象'+str(i)),('B'+str(i),i),('C'+str(i),i+1)] for i in range(2,14)]
  a=self.ingest(rows);v=self.answer(a,'対象12の値は？');parts=[p for u in v['stages']['answer_context'] for p in u['parts']];self.assertIn(1,[p['locator']['row'] for p in parts])
  mixed=build('対象12',v['excerpts']+[dict(locator={'kind':'pdf_page'},quote='適用上の注記')],v['excerpts'])
  self.assertEqual(mixed['blocked_reason'],'mixed_media_table_context_unverified');self.assertEqual(mixed['spans'],[])
 def test_isolated_encoder_and_decoder_share_slot_and_failure_is_typed(self):
  encoder=IsolatedTextEncoder('gemma2','/tmp');API(self.s,self.api.auth,encoder=encoder);self.assertIs(encoder.lock,LOCAL_MODEL_LOCK);self.assertIs(IsolatedShortAnswer().lock,encoder.lock)
  with patch('subprocess.Popen',side_effect=OSError('synthetic')):
   with self.assertRaises(APIError) as e:encoder.query('synthetic query')
  self.assertEqual(e.exception.code,'isolated_encoder_failed')

if __name__=='__main__':unittest.main()
