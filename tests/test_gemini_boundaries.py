"""Independent local-only preparation probes; no provider call or source edits."""
import copy,json,unittest
from pathlib import Path
from unittest.mock import patch,Mock
import urllib.error
from navigator.gemini_reader import rest,_NoRedirect,GeminiReader,request_body,request_hash
from tests import test_gemini_reader as fixtures

class IndependentGeminiPreparation(unittest.TestCase):
 def test_rest_redirect_and_http_error_do_not_leak_body_or_key(self):
  self.assertIsNone(_NoRedirect().redirect_request(None,None,302,'redirect',{},'https://invalid.example'))
  opener=Mock();opener.open.side_effect=urllib.error.HTTPError('https://official.invalid',429,'SECRET_ERROR',{},None)
  with patch.dict('os.environ',{'GEMINI_API_KEY':'SYNTHETIC_SECRET'}),patch('urllib.request.build_opener',return_value=opener):
   with self.assertRaisesRegex(ValueError,'^reader_http_429$'):rest('gemini-3.8-flash','interactions',{})
  self.assertEqual(opener.open.call_count,1)
 def test_default_adapter_never_calls_injected_transport(self):
  f=Mock(side_effect=AssertionError('forbidden network'))
  r=GeminiReader(transport=f).decide('question',{'kind':'reference'},[{'quote':'synthetic'}])
  self.assertFalse(r['complete']);f.assert_not_called()

class IndependentGrantBoundaries(unittest.TestCase):
 setUp=fixtures.GeminiPreparationTests.setUp
 tearDown=fixtures.GeminiPreparationTests.tearDown
 grant=fixtures.GeminiPreparationTests.grant
 fake=fixtures.GeminiPreparationTests.fake
 def test_invalid_budget_is_typed_and_secret_reason_redacted(self):
  r=GeminiReader(grant=self.grant(max_usd='invalid'),transport=self.fake).decide(self.query,{'kind':'reference'},self.spans)
  self.assertFalse(r['complete']);self.assertFalse(self.calls)
  def leak(*a):raise ValueError('reader_SYNTHETICSECRET')
  r=GeminiReader(grant=self.grant(),transport=leak).decide(self.query,{'kind':'reference'},self.spans)
  self.assertEqual(r['reason'],'reader_response_or_policy_failed')
 def test_missing_usage_halts_new_requests_persistently(self):
  q2='different synthetic question';h2=request_hash('gemini-3.8-flash',request_body(q2,self.spans))
  g=self.grant(request_hashes=self.grant().request_hashes+(h2,),max_requests=2,max_usd='.036')
  def missing(model,method,body):
   self.calls.append((model,method,body))
   return {'totalTokens':100} if method=='countTokens' else {'status':'completed','steps':[]}
  r=GeminiReader(grant=g,transport=missing).decide(self.query,{'kind':'reference'},self.spans)
  self.assertEqual(r['reason'],'reader_usage_unverified')
  r=GeminiReader(grant=g,transport=missing).decide(q2,{'kind':'reference'},self.spans)
  self.assertEqual(r['reason'],'reader_provider_halted');self.assertEqual(len(self.calls),2)

 def test_price_expiry_gate_denies_before_transport(self):
  r=GeminiReader(grant=self.grant(expires_utc='2027-02-01T00:00:00Z'),transport=self.fake).decide(self.query,{'kind':'reference'},self.spans)
  self.assertEqual(r['reason'],'reader_pricing_expired');self.assertFalse(self.calls)
 def test_unverified_usage_shape_and_transport_failure_halt(self):
  for kind in ('usage_shape','transport'):
   q2='second query '+kind;h2=request_hash('gemini-3.8-flash',request_body(q2,self.spans))
   g=self.grant(request_hashes=self.grant().request_hashes+(h2,),max_requests=2,max_usd='.036',ledger_path=str(Path(self.temp.name)/(kind+'.sqlite')))
   calls=[]
   def bad(model,method,body):
    calls.append(method)
    if method=='countTokens':return {'totalTokens':100}
    if kind=='transport':raise ValueError('reader_transport_failed_no_retry')
    return {'status':'completed','usage':[],'steps':'malformed too'}
   r=GeminiReader(grant=g,transport=bad).decide(self.query,{'kind':'reference'},self.spans)
   self.assertFalse(r['complete'])
   r=GeminiReader(grant=g,transport=bad).decide(q2,{'kind':'reference'},self.spans)
   self.assertEqual(r['reason'],'reader_provider_halted');self.assertEqual(len(calls),2)
 def test_verified_usage_then_bad_steps_is_not_unknown_usage(self):
  q2='second permitted query';h2=request_hash('gemini-3.8-flash',request_body(q2,self.spans))
  g=self.grant(request_hashes=self.grant().request_hashes+(h2,),max_requests=2,max_usd='.036')
  calls=[]
  def bad(model,method,body):
   calls.append(method)
   if method=='countTokens':return {'totalTokens':100}
   return {'status':'completed','usage':{'total_input_tokens':100,'total_output_tokens':4,'total_thought_tokens':2},'steps':'malformed'}
  for query in (self.query,q2):
   r=GeminiReader(grant=g,transport=bad).decide(query,{'kind':'reference'},self.spans)
   self.assertEqual(r['reason'],'reader_response_steps')
  self.assertEqual(len(calls),4)

if __name__=='__main__':unittest.main()
