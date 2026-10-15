"""Stopping during both bounded PDF stages must not strand a worker/DB write."""
import base64
import threading
import time
import unittest
from unittest.mock import patch
from navigator.api import API,Auth
from navigator.http import Host
from navigator.store import Store
from navigator import pdf_adapter as pdf
from tests.test_pdf_adapter import native_pdf

class ShutdownTests(unittest.TestCase):
    def test_shutdown_waits_for_two_bounded_pdf_stages_before_store_close(self):
        if not all(pdf._cli_paths()):
            self.skipTest('Installed native PDF tools required')
        store=Store(':memory:')
        api=API(store,Auth({'owner':dict(tenant='stop-test',actor='owner',revision=1,scopes=['read','write'])}))
        raw=native_pdf([['Owned bounded PDF stop test']])
        created=api.dispatch('POST','/v1/assets','owner',dict(name='shutdown PDF',media_type='application/pdf',content_base64=base64.b64encode(raw).decode()),'shutdown-0001')[1]
        started=threading.Event();real=pdf._run_cli
        def bounded_stage(*args,**kwargs):
            started.set()
            time.sleep(2.6)  # each CLI stage remains below its 5-second bound
            return real(*args,**kwargs)
        host=Host(api,0)
        try:
            with patch.object(pdf,'_run_cli',side_effect=bounded_stage):
                host.start();self.assertTrue(started.wait(2))
                host.close()
                self.assertFalse(host.worker_thread.is_alive())
            job=api.dispatch('GET','/v1/jobs/'+created['job']['id'],'owner')[1]
            self.assertEqual(job['state'],'succeeded')
        finally:
            if host.http_thread is None:
                host.server.server_close()
            store.close()

if __name__=='__main__':
    unittest.main()
