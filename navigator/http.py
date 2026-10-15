"""Bounded HTTP host; background work remains an explicit local-only option."""
from http.server import BaseHTTPRequestHandler,HTTPServer
from urllib.parse import urlsplit
import re
import socket
import threading
import time
import uuid
from .contracts import APIError,MAX_BODY,canonical,json_load,required_scopes

class Host:
    def __init__(self,api,port=4340,worker=True,*,bind_address='127.0.0.1',allowed_hosts=None,allowed_origins=None):
        if bind_address not in ('127.0.0.1','0.0.0.0'):
            raise ValueError('invalid_bind_address')
        if type(port) is not int or not 0<=port<=65535 or type(worker) is not bool:
            raise ValueError('invalid_host_settings')
        def authority(value):
            if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9.-]+(?::[0-9]+)?',value):
                raise ValueError('invalid_allowed_host')
            parsed=urlsplit('http://'+value)
            try:
                selected_port=parsed.port
            except ValueError:
                raise ValueError('invalid_allowed_host') from None
            if not parsed.hostname or selected_port==0:
                raise ValueError('invalid_allowed_host')
            return value
        if allowed_hosts is not None:
            if isinstance(allowed_hosts,str) or not allowed_hosts:
                raise ValueError('explicit_allowed_hosts_required')
            allowed_hosts=frozenset(authority(value) for value in allowed_hosts)
        if bind_address!='127.0.0.1' and (allowed_hosts is None or worker):
            raise ValueError('external_bind_requires_hosts_and_no_worker')
        if allowed_origins is not None:
            if isinstance(allowed_origins,str):
                raise ValueError('invalid_allowed_origin')
            origins=set()
            for value in allowed_origins:
                if not isinstance(value,str):
                    raise ValueError('invalid_allowed_origin')
                parsed=urlsplit(value)
                if parsed.scheme not in ('http','https') or value!=parsed.scheme+'://'+parsed.netloc:
                    raise ValueError('invalid_allowed_origin')
                authority(parsed.netloc)
                if allowed_hosts is None or parsed.netloc not in allowed_hosts:
                    raise ValueError('origin_host_not_allowed')
                origins.add(value)
            allowed_origins=frozenset(origins)
        self.api=api
        self.stop=threading.Event()
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(3)
            def handle_one_request(self):
                self._input_lock=threading.Lock()
                self._input_active=True
                self._input_expired=False
                self._input_deadline=time.monotonic()+3
                def expire():
                    with self._input_lock:
                        if self._input_active:
                            self._input_expired=True
                            try:
                                # End reads even when individual bytes arrive
                                # within the socket's per-read timeout. Keep
                                # writes open for a body-timeout JSON response.
                                self.connection.shutdown(socket.SHUT_RD)
                            except OSError:
                                pass
                self._input_timer=threading.Timer(3,expire)
                self._input_timer.daemon=True
                self._input_timer.start()
                try:
                    super().handle_one_request()
                finally:
                    self.end_input(check=False)
            def end_input(self,check=True):
                timer=getattr(self,'_input_timer',None)
                if timer is None:
                    return
                with self._input_lock:
                    expired=self._input_expired or time.monotonic()>=self._input_deadline
                    self._input_active=False
                    timer.cancel()
                if check and expired:
                    raise TimeoutError('request_input_deadline')
            def log_message(self,*args):
                pass  # Bearer tokens, source names and questions never enter logs.
            def handle_method(self):
                request_id=uuid.uuid4().hex
                try:
                    if len(self.headers.get_all('Host',[]))!=1 or self.headers.get('Host') not in owner.allowed_hosts:
                        raise APIError(403,'host_rejected')
                    origin=self.headers.get('Origin')
                    if len(self.headers.get_all('Origin',[]))>1 or (origin is not None and origin not in owner.allowed_origins) or self.headers.get('Sec-Fetch-Site')=='cross-site':
                        raise APIError(403,'origin_rejected')
                    target=urlsplit(self.path)
                    if target.scheme or target.netloc or target.query or target.fragment:
                        raise APIError(404,'not_found')
                    auth=self.headers.get('Authorization','')
                    token=auth[7:] if auth.startswith('Bearer ') else None
                    if len(self.headers.get_all('Authorization',[]))>1:
                        raise APIError(401,'authentication_required')
                    if len(self.headers.get_all('If-Match',[]))>1 or len(self.headers.get_all('Idempotency-Key',[]))>1:
                        raise APIError(422,'duplicate_precondition')
                    body=None
                    if self.command in ('POST','DELETE'):
                        if len(self.headers.get_all('Authorization',[]))>1:
                            raise APIError(401,'authentication_required')
                        context=owner.api.auth.authenticate(token)
                        route,_,_,scope,_=owner.api.match(self.command,target.path)
                        if not required_scopes(route,scope)<=set(context['scopes']):
                            raise APIError(403,'scope_denied')
                        if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length',[]))!=1:
                            raise APIError(411,'content_length_required')
                        try:
                            size=int(self.headers.get('Content-Length',''))
                        except ValueError:
                            raise APIError(422,'invalid_content_length')
                        if not 0<size<=MAX_BODY:
                            raise APIError(413,'http_body_limit')
                        if self.headers.get_content_type()!='application/json':
                            raise APIError(415,'json_content_type_required')
                        raw=self.rfile.read(size)
                        if getattr(self,'_input_expired',False):
                            raise TimeoutError('request_input_deadline')
                        if len(raw)!=size:
                            raise APIError(422,'incomplete_body')
                        body=json_load(raw)
                    self.end_input()
                    if self.command=='GET' and target.path=='/healthz':
                        status,value,headers=200,dict(status='ok'),{}
                    else:
                        preconditions={'if_match':self.headers['If-Match']} if self.headers.get('If-Match') is not None else {}
                        status,value,headers=owner.api.dispatch(self.command,target.path,token,body,self.headers.get('Idempotency-Key'),**preconditions)
                except APIError as error:
                    status,value,headers=error.status,dict(code=error.code,request_id=request_id),{}
                except (TimeoutError,ConnectionError):
                    status,value,headers=408,dict(code='request_timeout',request_id=request_id),{}
                except Exception:
                    status,value,headers=500,dict(code='internal_error',request_id=request_id),{}
                finally:
                    self.end_input(check=False)
                raw=value if isinstance(value,bytes) else canonical(value).encode()
                self.send_response(status)
                self.send_header('Content-Type','application/octet-stream' if isinstance(value,bytes) else 'application/json; charset=utf-8')
                self.send_header('Content-Length',str(len(raw)))
                self.send_header('Cache-Control','no-store')
                self.send_header('X-Content-Type-Options','nosniff')
                self.send_header('Connection','close')
                self.send_header('X-Request-ID',request_id)
                for name,val in headers.items():
                    self.send_header(name,val)
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (ConnectionError,OSError):
                    pass
                self.close_connection=True
            do_GET=handle_method
            do_POST=handle_method
            do_DELETE=handle_method
            do_PUT=handle_method
            do_OPTIONS=handle_method
        self.server=HTTPServer((bind_address,port),Handler)
        self.bind_address=bind_address
        self.allowed_hosts=allowed_hosts if allowed_hosts is not None else frozenset([f'127.0.0.1:{self.server.server_port}'])
        self.allowed_origins=allowed_origins if allowed_origins is not None else (frozenset(['http://'+value for value in self.allowed_hosts]) if allowed_hosts is None else frozenset())
        self.http_thread=None
        self.worker_thread=None
        self.enable_worker=worker

    @property
    def url(self):
        return f'http://{self.bind_address}:{self.server.server_port}'

    def start(self):
        self.http_thread=threading.Thread(target=self.server.serve_forever,kwargs=dict(poll_interval=0.05),daemon=True)
        self.http_thread.start()
        if self.enable_worker:
            def work():
                while not self.stop.is_set():
                    worked=self.api.library.run_once()
                    if not worked and self.api.maintenance is not None:
                        worked=self.api.maintenance.run_once()
                    if not worked:
                        self.stop.wait(0.05)
            self.worker_thread=threading.Thread(target=work,daemon=True)
            self.worker_thread.start()
        return self.url

    def close(self):
        self.stop.set()
        if self.http_thread:
            self.server.shutdown()
            self.http_thread.join(timeout=5)
        self.server.server_close()
        if self.worker_thread:
            # A PDF has two separately bounded 5-second CLI stages. Close the
            # store only after its owned worker has finished either stage.
            self.worker_thread.join()
        self.api.store.checkpoint()
