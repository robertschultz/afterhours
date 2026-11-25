"""Opt-in venue request page, isolated from the authenticated DJ control server.

A short-lived URL admits only bounded musical preferences. It grants no transport,
provider, library-file, microphone, or operator-chat authority.
"""
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
import html
import ipaddress
import json
import secrets
import socket
import threading
import time
from afterhours.guest_requests import GuestError
from afterhours import guest_web


def venue_host():
    # Selecting the route does not send a datagram or contact the destination.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect(('192.0.2.1', 9))
        host = sock.getsockname()[0]
    address = ipaddress.ip_address(host)
    if not address.is_private or address.is_loopback or address.is_unspecified:
        raise ValueError('Connect this Mac to the venue network before enabling requests.')
    return host


class RoomRequests:
    def __init__(self, receive, clock=time.monotonic, resolve_host=venue_host, protocol=None, unavailable_reason=None):
        self.receive=receive;self.clock=clock;self.resolve_host=resolve_host
        self.lock=threading.RLock();self.server=None;self.thread=None;self.token=None
        self.configuration_lock=threading.Lock()
        self.url=None;self.recent=deque(maxlen=100);self.clients={};self.received=0
        self.protocol=protocol
        self.unavailable_reason=unavailable_reason

    def refresh_guest(self):
        # Fresh set revokes the old invitation, without opening another socket.
        if self.protocol and self.server:
            night=self.protocol.current_night()
            if not self.protocol.scope or night!=self.protocol.scope['nightId']:
                self.token=secrets.token_urlsafe(24)
                origin='http://'+urlsplit(self.url).netloc
                self.url=origin+'/r/'+self.token
                self.protocol.activate(origin,secrets.token_hex(16),night)

    def snapshot(self):
        with self.lock:
            self.refresh_guest()
            return {'supported':not bool(self.unavailable_reason),'enabled':self.server is not None,'url':self.url,'received':self.received,
                    **({'error':self.unavailable_reason} if self.unavailable_reason else {}),
                    'scope':'Musical requests only; the DJ controls the set.'}

    def configure(self, enabled, host=None, port=8769):
        # Serialize listener replacement without holding the request-handler lock
        # during shutdown; otherwise simultaneous settings changes can orphan it.
        with self.configuration_lock:
            return self._configure(enabled,host,port)

    def _configure(self, enabled, host, port):
        if type(enabled) is not bool:raise ValueError('Choose whether requests are on or off.')
        with self.lock:
            if enabled and self.unavailable_reason:raise ValueError(self.unavailable_reason)
            if enabled and self.server:return self.snapshot()
            old=self.server;self.server=None;self.token=None;self.url=None
            if self.protocol:self.protocol.deactivate()
        if old:old.shutdown();old.server_close()
        if not enabled:return self.snapshot()
        host=host or self.resolve_host()
        address=ipaddress.ip_address(host)
        if address.version!=4 or not address.is_private or address.is_unspecified or address.is_multicast:
            raise ValueError('Use this Mac’s local venue address.')
        try:server=ThreadingHTTPServer((host,port),RequestHandler)
        except OSError:raise ValueError('The request page could not open on this venue address.') from None
        server.daemon_threads=True;server.manager=self
        with self.lock:
            self.server=server;self.token=secrets.token_urlsafe(24)
            self.url=f'http://{host}:{server.server_port}/r/{self.token}'
            if self.protocol:self.protocol.activate('http://'+urlsplit(self.url).netloc,secrets.token_hex(16),self.protocol.current_night())
            self.clients={};self.recent.clear();self.received=0
            self.thread=threading.Thread(target=server.serve_forever,daemon=True);self.thread.start()
            return self.snapshot()

    def accept(self, text, peer, token):
        if not isinstance(text,str) or not 1<=len(text.strip())<=300:
            raise ValueError('Use 1–300 characters for a song, artist or musical mood.')
        text=' '.join(text.split())
        if any(ord(c)<32 for c in text):raise ValueError('Use a plain-text song request.')
        with self.lock:
            if self.server is None or not token or not secrets.compare_digest(token,self.token or ''):
                raise PermissionError('Requests are closed.')
            now=self.clock()
            self.clients={k:v for k,v in self.clients.items() if now-v<60}
            if (peer in self.clients and now-self.clients[peer]<20) or sum(now-t<60 for t in self.recent)>=30 or len(self.clients)>=256:
                raise ValueError('Give the DJ a moment before sending another request.')
            self.clients[peer]=now;self.recent.append(now)
            request_id=secrets.token_hex(16)
            self.receive(text,request_id)
            self.received+=1
            return {'id':request_id,'status':'received'}

    def close(self):self.configure(False)


class RequestHandler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_GET(self):
        if '/v1/' in self.path:return self.guest_api('GET')
        if '/assets/' in self.path:return self.guest_asset()
        if not self.valid():return self.page('This request page is closed.',404,False)
        if self.server.manager.protocol:return self.web_reply(guest_web.page(self.path),'text/html; charset=utf-8')
        self.page('',200,True)
    def valid(self):
        manager=self.server.manager
        with manager.lock:
            manager.refresh_guest()
            return manager.server is self.server and self.headers.get('Host')==urlsplit(manager.url or '').netloc and self.path==f'/r/{manager.token}'
    def do_POST(self):
        if '/v1/' in self.path:return self.guest_api('POST')
        if not self.valid():return self.page('This request page is closed.',404,False)
        if self.server.manager.protocol:
            return self.json_reply({'version':1,'code':'exact-recording-required',
                'message':'Choose an exact recording in the request page.'},405)
        origin=self.headers.get('Origin')
        expected='http://'+self.headers.get('Host','')
        if origin and origin!=expected:return self.page('Open the request code again.',403,False)
        if self.headers.get('Content-Type','').split(';')[0]!='application/x-www-form-urlencoded':return self.page('Use the song request form.',415,False)
        try:
            length=int(self.headers.get('Content-Length',0))
            if not 0<length<=4096:raise ValueError('That request is too long.')
            data=parse_qs(self.rfile.read(length).decode('utf-8'),max_num_fields=4)
            if set(data)!={'request'} or len(data['request'])!=1:raise ValueError('Enter one song request.')
            self.server.manager.accept(data['request'][0],self.client_address[0],self.path.rsplit('/',1)[-1])
            return self.page('Request received. The DJ will choose where it fits.',202,False)
        except PermissionError:return self.page('Requests are closed.',403,False)
        except (ValueError,UnicodeError) as error:return self.page(str(error),400,True)
        except Exception:return self.page('The DJ could not receive that request. Try again shortly.',503,True)

    def guest_asset(self):
        manager=self.server.manager
        try:
            with manager.lock:
                manager.refresh_guest();prefix=f'/r/{manager.token}/assets/'
                if (not manager.protocol or manager.server is not self.server
                        or self.headers.get('Host')!=urlsplit(manager.url or '').netloc
                        or not self.path.startswith(prefix)):
                    raise ValueError('Closed invitation.')
                body,content_type=guest_web.asset(self.path[len(prefix):])
            return self.web_reply(body,content_type)
        except (ValueError,OSError):return self.page('This request page is closed.',404,False)

    def web_reply(self,body,content_type):
        self.send_response(200);self.send_header('Content-Type',content_type)
        self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store')
        self.send_header('Referrer-Policy','no-referrer');self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Security-Policy',guest_web.CSP)
        self.end_headers();self.wfile.write(body)

    def guest_api(self,method):
        manager=self.server.manager;scope=None
        try:
            # Rotation and the exact route admission form one operation. No
            # operator/session lock is held while doing ledger work.
            with manager.lock:
                manager.refresh_guest();parsed=urlsplit(self.path)
                prefix=f'/r/{manager.token}/v1'
                if (manager.server is not self.server or not manager.protocol
                        or self.headers.get('Host')!=urlsplit(manager.url or '').netloc
                        or not parsed.path.startswith(prefix+'/')):
                    raise GuestError('invitation-closed','This request page is closed.',404)
                scope=manager.protocol.scope.copy()
                origin=self.headers.get('Origin')
                if origin is not None and origin!=scope['origin']:
                    raise GuestError('origin-denied','Open the room invitation again.',403)
                if self.headers.get('Sec-Fetch-Site')=='cross-site':
                    raise GuestError('origin-denied','Open the room invitation again.',403)
                query=parse_qs(parsed.query,keep_blank_values=True,max_num_fields=4)
                payload=None
                if method=='POST':
                    if self.headers.get('Content-Type','').split(';')[0]!='application/json':
                        raise GuestError('json-required','Use a JSON request.',415)
                    lengths=self.headers.get_all('Content-Length') or []
                    if self.headers.get('Transfer-Encoding') or len(lengths)!=1 or not lengths[0].isdigit():
                        raise GuestError('invalid-request','This request length is invalid.')
                    length=int(lengths[0])
                    if not 0<length<=8192:raise GuestError('body-too-large','This request is too large.',413)
                    self.connection.settimeout(3)
                    raw=self.rfile.read(length)
                    if len(raw)!=length:raise GuestError('invalid-request','The request was incomplete.')
                    def pairs(items):
                        result={}
                        for key,value in items:
                            if key in result:raise ValueError('Duplicate key')
                            result[key]=value
                        return result
                    payload=json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(ValueError('Invalid number')))
                auth=self.headers.get_all('Authorization') or []
                if len(auth)>1:raise GuestError('guest-required','Use one guest session.',401)
                bearer=None
                if auth:
                    if not auth[0].startswith('Bearer '):raise GuestError('guest-required','Use a guest session.',401)
                    bearer=auth[0][7:]
                result=manager.protocol.handle(method,parsed.path[len(prefix):],query,payload,bearer,self.client_address[0])
                return self.json_reply(result,200)
        except GuestError as exc:
            return self.json_reply({'version':1,**({'scope':scope} if scope else {}),'code':exc.code,'message':str(exc),
                **({'retryAfter':exc.retry_after} if exc.retry_after is not None else {})},exc.status)
        except (ValueError,UnicodeError):
            return self.json_reply({'version':1,**({'scope':scope} if scope else {}),'code':'invalid-request','message':'This request is not valid.'},400)
        except Exception:
            return self.json_reply({'version':1,**({'scope':scope} if scope else {}),'code':'temporarily-unavailable','message':'Requests are temporarily unavailable.'},503)

    def json_reply(self,value,status):
        body=json.dumps(value,ensure_ascii=True,allow_nan=False).encode()
        self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8')
        self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store')
        self.send_header('Referrer-Policy','no-referrer');self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Security-Policy',"default-src 'none'; frame-ancestors 'none'")
        self.end_headers();self.wfile.write(body)
    def page(self,message,status,form):
        action=html.escape(self.path,quote=True)
        controls=f'<form method="post" action="{action}"><label for="request">Song, artist or mood</label><textarea id="request" name="request" maxlength="300" required rows="3" autocomplete="off"></textarea><button type="submit">Send request</button></form>' if form else ''
        body=f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Request a song · Cue</title><style>html{{color-scheme:light}}*{{box-sizing:border-box}}body{{margin:0;background:#f4f4f1;color:#1d1d1f;font:17px/1.5 system-ui}}main{{max-width:560px;margin:12vh auto;padding:28px}}h1{{font-size:38px;letter-spacing:-.03em;line-height:1.1;font-weight:600}}p{{color:#636366}}label{{display:block;margin:28px 0 10px}}textarea{{width:100%;border:1px solid #cfcfca;border-radius:12px;background:white;padding:16px;font:inherit;resize:vertical}}button{{display:block;margin-top:18px;border:0;border-radius:24px;padding:13px 24px;background:#1d1d1f;color:white;font:inherit;min-height:48px}}:focus-visible{{outline:3px solid #636366;outline-offset:3px}}.receipt{{color:#1d1d1f}}</style><main><h1>Request a song</h1><p>The DJ plays what fits the night.</p><p class="receipt" role="status">{html.escape(message)}</p>{controls}</main></html>'''.encode()
        self.send_response(status);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Content-Length',str(len(body)))
        # WebKit sends Origin: null on a form POST under no-referrer. Preserve
        # the same-origin form origin without leaking the room token elsewhere.
        self.send_header('Cache-Control','no-store');self.send_header('Referrer-Policy','same-origin');self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Security-Policy',"default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers();self.wfile.write(body)
