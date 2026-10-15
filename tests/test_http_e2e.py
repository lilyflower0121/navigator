"""Real HTTP extraction E2E over small synthetic bytes, never mocked AI."""
import base64
from datetime import datetime,timezone
import json
from pathlib import Path
import tempfile
import time
import unittest
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from navigator.api import API,Auth
from navigator.contracts import digest,openapi,validate,SCHEMAS
from navigator.http import Host
from navigator.store import Store

ROOT=Path(__file__).resolve().parents[1]
TOKEN='synthetic-owner-token-000000000000000000'

class HTTPIntegration(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=Path(self.temp.name)/'module.sqlite'
        self.start()
        self.key=0

    def start(self):
        self.store=Store(self.db)
        self.auth=Auth({TOKEN:dict(tenant='tenant-A',actor='owner',revision=1,scopes=['read','write','delete','diagnose','export'])})
        self.api=API(self.store,self.auth)
        self.host=Host(self.api,0)
        self.host.start()

    def tearDown(self):
        self.host.close();self.store.close();self.temp.cleanup()

    def request(self,method,path,body=None,status=200,key=None,headers=None,decode=True):
        self.key+=1
        h={'Authorization':'Bearer '+TOKEN}
        if body is not None:
            h['Content-Type']='application/json'
            h['Idempotency-Key']=key or f'http-test-{self.key:04d}'
        h.update(headers or {})
        req=Request(self.host.url+path,method=method,headers=h,data=json.dumps(body,ensure_ascii=False).encode() if body is not None else None)
        try:
            response=urlopen(req,timeout=5)
        except HTTPError as error:
            response=error
        with response:
            raw=response.read();actual=response.status
        self.assertEqual(actual,status,raw)
        if decode and actual<300 and path!='/openapi.json':
            route,_,_,_,_=self.api.match(method,path)
            from navigator.contracts import response_schema
            validate(json.loads(raw),SCHEMAS[response_schema(method,route)])
        return json.loads(raw) if decode else raw

    def ingest(self,file,media,key=None):
        raw=(ROOT/'fixtures'/file).read_bytes()
        body=dict(name=file,media_type=media,content_base64=base64.b64encode(raw).decode(),labels=['synthetic','inspection'])
        result=self.request('POST','/v1/assets',body,202,key)
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            job=self.request('GET','/v1/jobs/'+result['job']['id'])
            if job['state'] not in ('queued','running'):
                break
            time.sleep(0.01)
        self.assertEqual(job['state'],'succeeded',job)
        return raw,body,result,job

    def test_actual_ingest_structure_search_quote_and_restart(self):
        raw,body,result,job=self.ingest('inspection.csv','text/csv','csv-e2e-0001')
        self.ingest('handover.txt','text/plain')
        segment=job['result']['segments'][0]
        self.assertEqual(segment['fields']['pressure_mpa'],'0.68')
        self.assertEqual(segment['source_sha256'],digest(raw))
        self.assertEqual(segment['locator']['line_start'],2)
        self.assertEqual(segment['locator']['line_end'],2)
        original=self.request('GET',f"/v1/assets/{result['asset_id']}/versions/1/original",decode=False)
        self.assertEqual(original,raw)
        search=self.request('POST','/v1/search',dict(query='PIPE-012',filters=dict(labels=['synthetic'],media_types=['text/csv'])))
        self.assertEqual(len(search['results']),1)
        self.assertEqual(search['mode'],'lexical')
        answer=self.request('POST','/v1/answers',dict(query='PIPE-012 の pressure_mpa',filters=dict(asset_ids=[result['asset_id']]),mode='extractive'),201,'answer-e2e-0001')
        self.assertEqual(answer['status'],'evidence_found')
        self.assertIsNone(answer['model_used']);self.assertIsNone(answer['generated_answer'])
        self.assertIn('0.68',answer['excerpts'][0]['quote'])
        self.assertEqual(answer['excerpts'][0]['evidence'],segment['evidence'])
        # Durable state survives closing every connection and starting a new host.
        self.host.close();self.store.close();self.start()
        saved=self.request('GET','/v1/answers/'+answer['id'])
        self.assertEqual(saved,answer)
        same=self.request('POST','/v1/assets',body,202,'csv-e2e-0001')
        self.assertEqual(same['asset_id'],result['asset_id'])

    def test_diagnosis_report_then_delete_propagates(self):
        raw,body,result,job=self.ingest('inspection.csv','text/csv')
        ref=job['result']['segments'][0]['evidence']
        process=self.request('POST','/v1/processes',dict(name='synthetic pressure',required_media=['document'],max_delay_ms=10000,
                             criterion=dict(field='pressure_mpa',operator='gte',threshold=0.7,unit='MPa')),201)
        diagbody=dict(process_id=process['id'],stream_id='synthetic-stream',sequence=1,event_time=datetime.now(timezone.utc).isoformat(),input_status='complete',inputs=[ref],mode='structured_rules')
        diag=self.request('POST','/v1/diagnoses',diagbody,201,'diagnose-0001')
        self.assertEqual(diag['decision'],'outside_rule')
        template_raw=(ROOT/'fixtures/report-template.json').read_bytes()
        template=self.request('POST','/v1/templates',dict(name='inspection JSON layout',content_base64=base64.b64encode(template_raw).decode()),201)
        reportbody=dict(template_id=template['id'],assignments=[dict(field='pressure',evidence=ref,source_field='pressure_mpa')])
        report=self.request('POST','/v1/reports',reportbody,201,'report-e2e-0001')
        self.assertEqual(report['cells'][0]['value'],'0.68')
        self.assertEqual(report['cells'][1]['status'],'unknown')
        self.assertEqual(base64.b64decode(report['template_original_base64']),template_raw)
        answerbody=dict(query='PIPE-012')
        answer=self.request('POST','/v1/answers',answerbody,201,'answer-del-0001')
        self.request('DELETE','/v1/assets/'+result['asset_id'],{},200)
        self.request('GET','/v1/answers/'+answer['id'],status=410)
        self.request('GET','/v1/diagnoses/'+diag['id'],status=410)
        self.request('GET','/v1/reports/'+report['id'],status=410)
        self.request('POST','/v1/reports',reportbody,410,'report-e2e-0001')
        self.request('POST','/v1/answers',answerbody,410,'answer-del-0001')
        self.request('GET',f"/v1/assets/{result['asset_id']}/versions/1/original",status=410)
        self.assertEqual(self.request('POST','/v1/search',dict(query='PIPE-012'))['results'],[])
        self.assertNotIn(b'PIPE-012',self.db.read_bytes())

    def test_http_contract_limits_host_origin_and_unavailable_model(self):
        spec=self.request('GET','/openapi.json')
        self.assertEqual(spec,openapi())
        self.request('GET','/v1/capabilities',status=403,headers={'Host':'evil.example'})
        self.request('GET','/v1/capabilities',status=403,headers={'Origin':'https://evil.example'})
        self.request('GET','/v1/capabilities',status=401,headers={'Authorization':'Bearer unknown'})
        self.request('POST','/v1/search',dict(query='synthetic',modality='image'),501)
        self.request('POST','/v1/answers',dict(query='synthetic',mode='generative'),503)
        self.request('POST','/v1/assets',dict(name='too-big',media_type='text/plain',content_base64=base64.b64encode(b'x'*65537).decode()),413)
        self.request('POST','/v1/search',dict(query='x',tenant='tenant-B'),422)

if __name__=='__main__':
    unittest.main()
