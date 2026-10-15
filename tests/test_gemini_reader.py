import base64
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from navigator.api import API, Auth
from navigator.contracts import APIError, canonical
from navigator.gemini_reader import GeminiReader, ExecutionGrant, SOURCE_SHA, request_body, request_hash, decode_response, rest
from navigator.store import Store


class GeminiPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.spans = [dict(quote='合成試験の上限は40度。')]
        self.query = '上限は？'
        self.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def grant(self, spans=None, query=None, **changes):
        body = request_body(query or self.query, spans or self.spans)
        values = dict(approval_reference='synthetic-test-double-only', paid_tier_confirmed=True,
                      billing_project='synthetic-no-live-billing', expires_utc=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(),
                      model='gemini-3.8-flash', source_sha256=SOURCE_SHA,
                      request_hashes=(request_hash('gemini-3.8-flash', body),), max_requests=1, max_usd='.018',
                      ledger_path=str(Path(self.temp.name)/'attempts.sqlite'))
        values.update(changes)
        return ExecutionGrant(**values)

    def fake(self, model, method, body):
        self.calls.append((model, method, body))
        if method == 'countTokens':return {'totalTokens':100}
        return dict(status='completed',steps=[dict(type='model_output',content=[dict(type='text',text='1|40度')])],
                    usage=dict(total_input_tokens=100,total_output_tokens=7,total_thought_tokens=23,total_tokens=130))

    def test_default_denies_before_key_access_or_network(self):
        with patch('navigator.gemini_reader.rest',side_effect=AssertionError('network')), patch.dict('os.environ',{},clear=True):
            r=GeminiReader().decide(self.query,{'kind':'reference'},self.spans)
        self.assertEqual(r['reason'],'reader_execution_not_authorized')

    def test_grant_exact_payload_and_shared_prompt_and_no_identifiers(self):
        rich=[dict(quote=self.spans[0]['quote'],evidence={'asset_id':'local-private-id'},source_sha256=SOURCE_SHA)]
        reader=GeminiReader(grant=self.grant(spans=rich),transport=self.fake)
        r=reader.decide(self.query,{'kind':'reference'},rich)
        self.assertTrue(r['complete']);self.assertEqual(r['usage']['total_thought_tokens'],23)
        body=self.calls[-1][2];self.assertNotIn('local-private-id',canonical(body));self.assertNotIn(SOURCE_SHA,canonical(body))
        self.assertEqual(set(body),{'input','generation_config','store','background','stream','service_tier'});self.assertEqual(body['generation_config']['max_output_tokens'],4000)
        self.assertFalse(body['store']);self.assertFalse(body['background']);self.assertEqual(body['generation_config']['thinking_summaries'],'none')
        from navigator.short_answer import prompt_for
        self.assertEqual(body['input'],prompt_for(self.query,rich))
        self.assertEqual(len(self.calls),2)

    def test_incorrect_request_tier_expiry_and_budget_do_not_send(self):
        for changes in ({'paid_tier_confirmed':False},{'expires_utc':'2000-01-01T00:00:00Z'},
                        {'request_hashes':('a'*64,)},{'max_usd':'.001'},{'source_sha256':'b'*64},
                        {'max_requests':9},{'model':'gemini-4-argon'}):
            r=GeminiReader(grant=self.grant(**changes),transport=self.fake).decide(self.query,{'kind':'reference'},self.spans)
            self.assertFalse(r['complete']);self.assertFalse(self.calls)

    def test_count_limit_prevents_generation_and_consumes_attempt(self):
        def transport(*a):self.calls.append(a);return {'totalTokens':4001}
        reader=GeminiReader(grant=self.grant(),transport=transport)
        self.assertEqual(reader.decide(self.query,{'kind':'reference'},self.spans)['reason'],'reader_input_token_limit')
        self.assertEqual(reader.decide(self.query,{'kind':'reference'},self.spans)['reason'],'reader_request_already_attempted')
        self.assertEqual(len(self.calls),1)

    def test_failed_or_successful_attempt_never_retried(self):
        def failed(*a):self.calls.append(a);raise ValueError('reader_http_429')
        reader=GeminiReader(grant=self.grant(),transport=failed)
        self.assertEqual(reader.decide(self.query,{'kind':'reference'},self.spans)['reason'],'reader_http_429')
        self.assertEqual(reader.decide(self.query,{'kind':'reference'},self.spans)['reason'],'reader_request_already_attempted')
        self.assertEqual(len(self.calls),1)

    def test_ledger_count_and_dollar_reservation_persist_across_instances(self):
        q2='合成試験の温度上限は？';h2=request_hash('gemini-3.8-flash',request_body(q2,self.spans))
        g=self.grant(request_hashes=self.grant().request_hashes+(h2,))
        self.assertTrue(GeminiReader(grant=g,transport=self.fake).decide(self.query,{'kind':'reference'},self.spans)['complete'])
        r=GeminiReader(grant=g,transport=self.fake).decide(q2,{'kind':'reference'},self.spans)
        self.assertEqual(r['reason'],'reader_request_budget_exhausted');self.assertEqual(len(self.calls),2)

    def test_finish_budget_safety_and_thoughts_are_separate(self):
        for finish,status,thoughts in [('completed','generated',3),('incomplete','budget_incomplete',3990),('incomplete','incomplete',20),('failed','blocked',0)]:
            d=dict(status=finish,steps=[dict(type='model_output',content=[dict(type='thought',text='hidden-thought'),dict(type='text',text='1|40度')])],
                   usage=dict(total_input_tokens=100,total_output_tokens=10,total_thought_tokens=thoughts))
            r=decode_response(d);self.assertEqual(r['status'],status);self.assertNotIn('hidden-thought',canonical(r));self.assertEqual(r['complete'],finish=='completed')

    def test_rest_uses_header_only_and_fixed_host_and_no_redirects(self):
        class Response:
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def read(self,n):return b'{"totalTokens":1}'
        from unittest.mock import Mock
        opener=Mock();opener.open.return_value=Response()
        with patch.dict('os.environ',{'GEMINI_API_KEY':'synthetic-secret'}),patch('urllib.request.build_opener',return_value=opener):
            self.assertEqual(rest('gemini-3.8-flash','countTokens',{'contents':[]}),{'totalTokens':1})
        req=opener.open.call_args.args[0]
        self.assertEqual(req.full_url,'https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:countTokens')
        self.assertNotIn('synthetic-secret',req.full_url);self.assertNotIn('synthetic-secret',req.data.decode())
        self.assertEqual(dict(req.header_items())['X-goog-api-key'],'synthetic-secret')

    def test_api_candidate_citations_model_pin_replay_and_acl(self):
        # API seam only: this model is a double, never an enabled cloud provider.
        class Double:
            request_binding=GeminiReader().request_binding
            calls=0
            def decide(self,*a):self.calls+=1;return dict(raw_output='1|40度',complete=True,status='generated')
        s=Store(':memory:');model=Double()
        try:
            api=API(s,Auth({'a':dict(tenant='a',actor='alice',revision=1,scopes=['read','write','delete']),
                            'b':dict(tenant='b',actor='alice',revision=1,scopes=['read'])}),short_answer_model=model)
            def call(method,path,body=None,key=None,token='a'):return api.dispatch(method,path,token,body,'phase13-test-'+key if key else None)[1]
            ing=call('POST','/v1/assets',dict(name='synthetic',media_type='text/plain',content_base64=base64.b64encode(self.spans[0]['quote'].encode()).decode()),'ingest')
            api.library.run_once()
            body=dict(query=self.query,task={'kind':'reference'},asset_ids=[ing['asset_id']],retrieval_policy='static',answer_mode='local_short_answer')
            v=call('POST','/v1/evidence-tasks',body,'task')
            self.assertEqual(v['model_used'],'gemini-3.8-flash');self.assertEqual(v['claims'],[])
            self.assertEqual(v['generated_answer']['supports'][0]['source_sha256'],v['scope'][0]['source_sha256'])
            self.assertFalse(v['generated_answer']['semantic_support_verified']);self.assertTrue(v['generated_answer']['review_required'])
            api.evidence_assist.short_answer_model=GeminiReader('gemini-4-argon')
            self.assertEqual(call('GET','/v1/evidence-tasks/'+v['id']),v)
            self.assertEqual(call('POST','/v1/evidence-tasks',body,'task'),v);self.assertEqual(model.calls,1)
            with self.assertRaises(APIError):call('GET','/v1/evidence-tasks/'+v['id'],token='b')
            call('DELETE','/v1/assets/'+ing['asset_id'],{},'delete')
            with self.assertRaises(APIError):call('GET','/v1/evidence-tasks/'+v['id'])
        finally:s.close()

    def test_api_retains_budget_incomplete_reason(self):
        class Double:
            request_binding=GeminiReader().request_binding
            def decide(self,*a):return dict(raw_output='1|40',complete=False,status='budget_incomplete',reason='reader_output_budget_incomplete')
        s=Store(':memory:')
        try:
            api=API(s,Auth({'a':dict(tenant='a',actor='a',revision=1,scopes=['read','write'])}),short_answer_model=Double())
            ing=api.dispatch('POST','/v1/assets','a',dict(name='synthetic',media_type='text/plain',content_base64=base64.b64encode(b'40').decode()),'phase13-test-ing')[1];api.library.run_once()
            v=api.dispatch('POST','/v1/evidence-tasks','a',dict(query='40?',task={'kind':'reference'},asset_ids=[ing['asset_id']],retrieval_policy='static',answer_mode='local_short_answer'),'phase13-test-ans')[1]
            self.assertEqual(v['reason'],'reader_output_budget_incomplete');self.assertEqual(v['state'],'model_hold');self.assertIsNone(v['generated_answer'])
        finally:s.close()


if __name__=='__main__':unittest.main()
