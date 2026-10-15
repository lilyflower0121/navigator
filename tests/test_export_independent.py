"""Independent portable export checks; synthetic bytes and no model/network."""
import base64,json,os,subprocess,sys,tempfile,tomllib,unittest
from pathlib import Path
from unittest.mock import patch
from navigator.api import API,Auth
from navigator.store import Store
from navigator.contracts import APIError,canonical,openapi
from navigator import xlsx_adapter
ROOT=Path(__file__).resolve().parents[1]

class ExportIndependentTests(unittest.TestCase):
 def test_default_api_real_text_roundtrip_without_process_or_network(self):
  s=Store(':memory:');api=API(s,Auth({'synthetic':dict(tenant='t',actor='a',revision=1,scopes=['read','write'])}))
  try:
   with patch('subprocess.Popen',side_effect=AssertionError('default spawned a process')),patch('urllib.request.OpenerDirector.open',side_effect=AssertionError('default network')):
    v=api.dispatch('POST','/v1/assets','synthetic',dict(name='synthetic',media_type='text/plain',content_base64=base64.b64encode(b'alpha temperature limit forty').decode()),'independent-ingest')[1]
    api.library.run_once();job=api.dispatch('GET','/v1/jobs/'+v['job']['id'],'synthetic')[1]
    self.assertEqual(job['state'],'succeeded')
    r=api.dispatch('POST','/v1/search','synthetic',dict(query='temperature',limit=5),'independent-search')[1]
    self.assertTrue(r['results']);self.assertIn('temperature',r['results'][0]['quote'])
  finally:s.close()
 def test_native_worker_is_opt_in_and_packaged(self):
  with patch.dict(os.environ,{},clear=True),patch('subprocess.Popen',side_effect=AssertionError('not opted in')):
   self.assertFalse(xlsx_adapter.worker_available())
   with self.assertRaises(APIError) as cm:xlsx_adapter._run_worker('/tmp')
   self.assertEqual(cm.exception.code,'xlsx_capability_unavailable')
  config=tomllib.loads((ROOT/'pyproject.toml').read_text());self.assertIn('workers/*.mjs',config['tool']['setuptools']['package-data']['navigator'])
  self.assertTrue((ROOT/'navigator/workers/xlsx_builder.mjs').is_file())
 def test_fresh_process_import_does_not_load_model_packages(self):
  code="import sys; from navigator.api import API; from navigator.contracts import openapi; assert not any(n.split('.')[0] in ('torch','transformers','safetensors') for n in sys.modules); assert openapi()['info']['title'].casefold().startswith('navigator')"
  r=subprocess.run([sys.executable,'-c',code],cwd=ROOT,env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'},capture_output=True,text=True,timeout=5)
  self.assertEqual(r.returncode,0,r.stderr)
 def test_exported_openapi_matches_live_contract(self):
  self.assertEqual(json.loads((ROOT/'openapi.json').read_text()),openapi())

if __name__=='__main__':unittest.main()
