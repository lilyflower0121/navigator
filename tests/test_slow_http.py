"""Small real loopback sockets verify the absolute request-input deadline."""
import http.client
import socket
import threading
import time
import unittest
from navigator.api import API,Auth
from navigator.http import Host
from navigator.store import Store


class SlowHTTPTests(unittest.TestCase):
    def setUp(self):
        self.store=Store(':memory:')
        self.token='synthetic-deadline-test-token'
        self.auth=Auth({self.token:dict(tenant='synthetic',actor='a',revision=1,scopes=['read'])})
        self.host=Host(API(self.store,self.auth),0,worker=False);self.host.start()
        self.port=self.host.server.server_port
    def tearDown(self):
        self.host.close();self.store.close()
    def exercise(self,body=False):
        slow=socket.create_connection(('127.0.0.1',self.port),timeout=6)
        slow.settimeout(6)
        header=(f'POST /v1/search HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n'
                f'Authorization: Bearer {self.token}\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n{{'
                if body else f'GET /healthz HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nX-Drip: x')
        slow.sendall(header.encode());stop=threading.Event()
        def drip():
            while not stop.wait(.15):
                try:slow.sendall(b' ')
                except OSError:return
        sender=threading.Thread(target=drip);sender.start()
        started=time.monotonic()
        health=http.client.HTTPConnection('127.0.0.1',self.port,timeout=6)
        try:
            health.request('GET','/healthz')
            response=health.getresponse()
            self.assertEqual(response.status,200);self.assertEqual(response.read(),b'{"status":"ok"}')
            self.assertLess(time.monotonic()-started,4.5)
            received=slow.recv(4096)
            self.assertIn(b'408',received)
            self.assertEqual(self.store.db.execute('SELECT count(*) FROM records').fetchone()[0],0)
        finally:
            stop.set();sender.join(timeout=1);slow.close();health.close()
    def test_unauthenticated_drip_headers_release_handler_and_health(self):
        self.exercise()
    def test_authenticated_drip_body_never_dispatches_and_releases_health(self):
        self.exercise(body=True)
    def test_unauthenticated_body_is_rejected_before_read(self):
        client=socket.create_connection(('127.0.0.1',self.port),timeout=2)
        try:
            client.sendall((f'POST /v1/search HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n').encode())
            started=time.monotonic();result=client.recv(4096)
            self.assertIn(b'401',result);self.assertLess(time.monotonic()-started,1)
        finally:client.close()

if __name__=='__main__':unittest.main()
