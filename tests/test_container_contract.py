"""Socket-free checks of admin configuration and container HTTP boundaries."""
from email.message import Message
from io import BytesIO
import json
import os
import socket
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
from http.server import BaseHTTPRequestHandler
from navigator import __main__ as cli
from navigator.contracts import APIError
from navigator.http import Host


def make_host(**settings):
    def server(address,handler):
        return SimpleNamespace(server_port=address[1] or 54321,handler=handler,address=address)
    with patch('navigator.http.HTTPServer',side_effect=server):
        return Host(Mock(),**settings)


def make_handler(host,path='/healthz',headers=()):
    handler=object.__new__(host.server.handler)
    handler.command='GET';handler.path=path;handler.headers=Message()
    for name,value in headers:
        handler.headers.add_header(name,value)
    handler.rfile=BytesIO();handler.wfile=BytesIO()
    handler.send_response=Mock();handler.send_header=Mock();handler.end_headers=Mock()
    handler.connection=Mock()
    return handler


def request(host,path='/healthz',headers=()):
    handler=make_handler(host,path,headers)
    handler.handle_method()
    return handler.send_response.call_args.args[0],json.loads(handler.wfile.getvalue())


class ContainerContractTests(unittest.TestCase):
    def test_absolute_timer_covers_header_parse_and_interrupts_reads(self):
        host=make_host();handler=make_handler(host)
        with patch('navigator.http.threading.Timer') as timer,\
             patch.object(BaseHTTPRequestHandler,'handle_one_request') as parse:
            def header_parse():
                timer.return_value.start.assert_called_once()
                self.assertEqual(timer.call_args.args[0],3)
                timer.call_args.args[1]()
                handler.connection.shutdown.assert_called_once_with(socket.SHUT_RD)
                with self.assertRaises(TimeoutError):
                    handler.end_input()
            parse.side_effect=header_parse
            handler.handle_one_request()
            self.assertFalse(handler._input_active)
            self.assertTrue(timer.return_value.cancel.called)

    def test_input_timer_is_cancelled_before_api_and_cannot_interrupt_accepted_work(self):
        host=make_host();handler=make_handler(host,'/v1/capabilities',[('Host','127.0.0.1:4340')])
        with patch('navigator.http.threading.Timer') as timer,\
             patch.object(BaseHTTPRequestHandler,'handle_one_request',side_effect=handler.handle_method):
            def accepted_work(*args):
                self.assertFalse(handler._input_active)
                timer.return_value.cancel.assert_called()
                timer.call_args.args[1]()  # callback already queued before cancel
                handler.connection.shutdown.assert_not_called()
                return 200,{'status':'accepted'},{}
            host.api.dispatch.side_effect=accepted_work
            handler.handle_one_request()
            self.assertEqual(handler.send_response.call_args.args[0],200)

    def test_body_deadline_returns_408_without_dispatch_and_late_timer_check_fails_closed(self):
        host=make_host()
        host.api.auth.authenticate.return_value={'scopes':['write']}
        host.api.match.return_value=('/v1/assets',None,202,'write',{})
        headers=[('Host','127.0.0.1:4340'),('Authorization','Bearer owner'),
                 ('Content-Length','2'),('Content-Type','application/json')]
        handler=make_handler(host,'/v1/assets',headers);handler.command='POST';handler.rfile=Mock()
        with patch('navigator.http.threading.Timer') as timer,\
             patch.object(BaseHTTPRequestHandler,'handle_one_request',side_effect=handler.handle_method):
            def partial_body(size):
                timer.call_args.args[1]()
                return b'{'
            handler.rfile.read.side_effect=partial_body
            handler.handle_one_request()
        self.assertEqual(handler.send_response.call_args.args[0],408)
        self.assertEqual(json.loads(handler.wfile.getvalue())['code'],'request_timeout')
        host.api.dispatch.assert_not_called()
        # The absolute clock also rejects expiry if the timer thread runs late.
        handler=make_handler(host,headers=[('Host','127.0.0.1:4340')])
        with patch('navigator.http.threading.Timer'),\
             patch('navigator.http.time.monotonic',side_effect=[100,104,104,104]),\
             patch.object(BaseHTTPRequestHandler,'handle_one_request',side_effect=handler.handle_method):
            handler.handle_one_request()
        self.assertEqual(handler.send_response.call_args.args[0],408)

    def test_post_auth_and_scopes_are_checked_before_body_reads(self):
        for denied in ('authentication','scope'):
            with self.subTest(denied=denied):
                host=make_host()
                if denied=='authentication':
                    host.api.auth.authenticate.side_effect=APIError(401,'authentication_required')
                else:
                    host.api.auth.authenticate.return_value={'scopes':['read']}
                    host.api.match.return_value=('/v1/assets',None,202,'write',{})
                handler=make_handler(host,'/v1/assets',[('Host','127.0.0.1:4340'),
                     ('Content-Length','65536'),('Content-Type','application/json')])
                handler.command='POST';handler.rfile=Mock()
                handler.handle_method()
                self.assertEqual(handler.send_response.call_args.args[0],401 if denied=='authentication' else 403)
                handler.rfile.read.assert_not_called()
                host.api.dispatch.assert_not_called()

    def test_default_loopback_and_shallow_health_preserve_host_origin_checks(self):
        host=make_host(port=0)
        self.assertEqual(host.server.address,('127.0.0.1',0))
        good=[('Host','127.0.0.1:54321')]
        self.assertEqual(request(host,headers=good),(200,{'status':'ok'}))
        host.api.dispatch.assert_not_called()
        for extra in ([('Host','evil.example')],[('Origin','https://evil.example')],
                      [('Origin','')],[('Sec-Fetch-Site','cross-site')]):
            self.assertEqual(request(host,headers=good+extra)[0],403)
        self.assertEqual(request(host,headers=[])[0],403)
        self.assertEqual(request(host,path='/healthz?config=1',headers=good)[0],404)
        self.assertEqual(request(host,headers=good+[('Origin','http://127.0.0.1:54321')])[0],200)

    def test_explicit_external_hosts_are_exact_and_origins_require_allowlist(self):
        host=make_host(port=8080,worker=False,bind_address='0.0.0.0',
                       allowed_hosts=['service.example','127.0.0.1:8080'],
                       allowed_origins=['https://service.example'])
        self.assertEqual(host.server.address,('0.0.0.0',8080))
        self.assertFalse(host.enable_worker)
        good=[('Host','service.example')]
        self.assertEqual(request(host,headers=good+[('Origin','https://service.example')])[0],200)
        for extra in ([('Origin','http://service.example')],[('Origin','https://evil.example')],
                      [('Origin','https://service.example'),('Origin','https://service.example')]):
            self.assertEqual(request(host,headers=good+extra)[0],403)
        for value in ('service.example.evil','service.example:8080','*','evil.example'):
            self.assertEqual(request(host,headers=[('Host',value)])[0],403)
        host.api.dispatch.return_value=(401,{'code':'authentication_required'},{})
        self.assertEqual(request(host,path='/v1/capabilities',headers=good)[0],401)
        self.assertIsNone(host.api.dispatch.call_args.args[2])

    def test_external_bind_and_invalid_settings_fail_before_socket_creation(self):
        variants=[dict(bind_address='0.0.0.0'),
                  dict(bind_address='0.0.0.0',worker=False),
                  dict(bind_address='0.0.0.0',allowed_hosts=['service.example']),
                  dict(allowed_hosts=['*']),dict(allowed_hosts=['https://service.example']),
                  dict(allowed_hosts=['service.example:65536']),dict(allowed_hosts=[]),
                  dict(bind_address='evil.example'),dict(port=-1),
                  dict(allowed_hosts=['service.example'],allowed_origins=['https://evil.example']),
                  dict(allowed_hosts=['service.example'],allowed_origins=['https://service.example/'])]
        with patch('navigator.http.HTTPServer') as server:
            for settings in variants:
                with self.subTest(settings=settings),self.assertRaises(ValueError):
                    Host(Mock(),**settings)
            server.assert_not_called()

    def test_cli_reads_admin_environment_and_explicit_options_override_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            auth=Path(temporary)/'access.json';db=Path(temporary)/'module.sqlite'
            auth.write_text(json.dumps({'x'*32:dict(tenant='test',actor='owner',revision=1,scopes=['read'])}))
            settings=dict(PORT='8080',NAVIGATOR_BIND_ADDRESS='0.0.0.0',NAVIGATOR_WORKER='0',
                          NAVIGATOR_ALLOWED_HOSTS='service.example,127.0.0.1:8080',
                          NAVIGATOR_ALLOWED_ORIGINS='https://service.example',
                          NAVIGATOR_AUTH_PATH=str(auth),NAVIGATOR_DB_PATH=str(db))
            for options,expected in (([],(8080,False,'0.0.0.0',['service.example','127.0.0.1:8080'])),
                                      (['--port','4341','--bind-address','127.0.0.1','--allowed-host','127.0.0.1:4341','--allowed-origin','http://127.0.0.1:4341'],
                                       (4341,False,'127.0.0.1',['127.0.0.1:4341']))):
                with patch.dict(os.environ,settings,clear=True),patch('sys.argv',['navigator','serve']+options),\
                     patch.object(cli,'Host') as constructor,patch.object(cli.threading,'Event'),\
                     patch.object(cli.signal,'signal'),patch('builtins.print'):
                    cli.main()
                    call=constructor.call_args
                    self.assertEqual((call.args[1],call.args[2],call.kwargs['bind_address'],call.kwargs['allowed_hosts']),expected)
                    constructor.return_value.close.assert_called_once()
            self.assertTrue(db.exists())

    def test_cli_incomplete_external_settings_close_store_without_binding(self):
        with tempfile.TemporaryDirectory() as temporary:
            auth=Path(temporary)/'access.json'
            auth.write_text(json.dumps({'x'*32:dict(tenant='test',actor='owner',revision=1,scopes=['read'])}))
            settings=dict(PORT='8080',NAVIGATOR_BIND_ADDRESS='0.0.0.0',NAVIGATOR_WORKER='0',
                          NAVIGATOR_AUTH_PATH=str(auth))
            with patch.dict(os.environ,settings,clear=True),patch('sys.argv',['navigator','serve']),\
                 patch.object(cli,'Store') as store,patch('navigator.http.HTTPServer') as server:
                with self.assertRaisesRegex(ValueError,'external_bind_requires_hosts_and_no_worker'):
                    cli.main()
                server.assert_not_called()
                store.return_value.close.assert_called_once()


if __name__=='__main__':
    unittest.main()
