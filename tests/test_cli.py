"""CLI composition runs only its own temporary local process and test principal."""
import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.request import Request,urlopen

ROOT=Path(__file__).resolve().parents[1]

class CLITests(unittest.TestCase):
    def test_init_exclusive_private_files_openapi_and_serve_sigterm(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary)/'owned';auth=folder/'access.json';db=folder/'store.sqlite';spec=folder/'openapi.json'
            command=[sys.executable,'-m','navigator']
            result=subprocess.run(command+['init-local','--auth',str(auth)],cwd=ROOT,capture_output=True,text=True,timeout=5)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(auth.stat().st_mode & 0o777,0o600)
            self.assertEqual(folder.stat().st_mode & 0o777,0o700)
            initial=auth.read_bytes();principals=json.loads(initial);token=next(iter(principals))
            self.assertNotIn(token,result.stdout)
            duplicate=subprocess.run(command+['init-local','--auth',str(auth)],cwd=ROOT,capture_output=True,text=True,timeout=5)
            self.assertNotEqual(duplicate.returncode,0)
            self.assertEqual(initial,auth.read_bytes())
            generated=subprocess.run(command+['openapi','--output',str(spec)],cwd=ROOT,capture_output=True,text=True,timeout=5)
            self.assertEqual(generated.returncode,0,generated.stderr)
            self.assertEqual(json.loads(spec.read_text())['openapi'],'3.1.0')
            process=subprocess.Popen(command+['serve','--auth',str(auth),'--db',str(db),'--port','0'],cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            try:
                line=process.stdout.readline().strip()
                self.assertTrue(line.startswith('NAVIGATOR local modular API: http://127.0.0.1:'),line)
                url=line.split('API: ',1)[1]
                req=Request(url+'/v1/capabilities',headers={'Authorization':'Bearer '+token})
                with urlopen(req,timeout=5) as response:
                    capabilities=json.load(response)
                self.assertFalse(capabilities['assist']['generative'])
                process.send_signal(signal.SIGTERM)
                stdout,stderr=process.communicate(timeout=5)
                self.assertEqual(process.returncode,0,stderr)
                self.assertNotIn(token,line+stdout+stderr)
            finally:
                if process.poll() is None:
                    process.kill();process.communicate(timeout=5)

if __name__=='__main__':
    unittest.main()
