"""Local performance control and clean second-screen output. Audio server stays untouched."""
from http.server import SimpleHTTPRequestHandler
from pathlib import Path
import argparse
import json
import os
import re
import secrets
from urllib.parse import urlparse, unquote, parse_qs
from urllib.request import urlopen
from afterhours.performance import Performance, FalContinuation
from afterhours.director import DirectorGate
from afterhours.director_cast import DirectorCast
from afterhours.director_stage import DirectorStages
from afterhours.director_signaling import signaling_error
from urllib.error import HTTPError
from afterhours.agent_profiles import AgentProfiles
from afterhours.character_board import CharacterBoards
from afterhours.web_runtime import LoopbackHTTPServer, RuntimeStaticHandler

from afterhours.paths import data_root

ROOT=data_root()
WEB=ROOT/'web'
MEDIA=ROOT/'.runtime/performance-media'
CAPTURES=ROOT/'.runtime/director-captures'


def reference_file(poster):
    """Resolve the selected approved image through the same local media roots."""
    if isinstance(poster,str) and re.fullmatch(r'/agent-media/[a-f0-9]{32}\.jpg',poster):
        base=WEB/'agent-media';path=base/Path(poster).name
    elif isinstance(poster,str) and re.fullmatch(r'/assets/[A-Za-z0-9_-]+\.(jpg|jpeg|png|webp)',poster):
        base=WEB/'assets';path=base/Path(poster).name
    else:raise ValueError('Selected reference is unavailable')
    if not path.resolve().is_relative_to(base.resolve()) or not path.is_file():raise ValueError('Selected reference is unavailable')
    return path


class Handler(RuntimeStaticHandler):
    def __init__(self,*args,**kwargs):super().__init__(*args,directory=str(WEB),**kwargs)
    def log_message(self,*args):pass
    def end_headers(self):
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; object-src 'none'; frame-ancestors 'self' http://127.0.0.1:8765 http://localhost:8765")
        super().end_headers()
    def reply(self,data,status=200):
        payload=json.dumps(data).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
    def local(self):return self.headers.get('Host') in (f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}')
    def do_GET(self):
        if not self.local():return self.reply({'error':'Local access only'},403)
        path=unquote(urlparse(self.path).path)
        if path=='/api/performance/config':return self.reply({'service':'afterhours-performance','token':self.server.token,'director':self.server.director.snapshot()})
        if path=='/api/performance':return self.reply({**self.server.performance.snapshot(),'director':self.server.director.snapshot()})
        if path=='/api/characters/status':return self.reply(self.server.boards.status())
        if path=='/api/director/cast':return self.reply(self.server.cast.roster())
        if path=='/api/director/stage':
            query=parse_qs(urlparse(self.path).query)
            character=query.get('character',[''])[0];appearance=query.get('appearance',[''])[0]
            if character!=self.server.performance.snapshot()['character']:
                return self.reply({'ready':False,'stageReference':None,'message':'The selected DJ changed. Refresh the live preview.'})
            return self.reply(self.server.stages.preflight(character,appearance))
        if path.startswith('/director-captures/'):
            name=path.removeprefix('/director-captures/')
            if not re.fullmatch(r'[a-f0-9]{32}\.(mp4|webm|json)',name):return self.reply({'error':'Not found'},404)
            target=self.server.captures/name
            if name.endswith('.json'):
                if not target.is_file():return self.reply({'error':'Not found'},404)
                return self.reply(json.loads(target.read_text()))
            return self.media(target)
        if path=='/api/state':
            study=self.server.director.program()
            if study:return self.reply(study)
            try:
                with urlopen(f'http://127.0.0.1:{self.server.audio_port}/api/program',timeout=1) as r:data=json.loads(r.read(500_000))
                return self.reply(data)
            except Exception:return self.reply({'connected':False,'message':'Audio console is unavailable'})
        if path.startswith('/api/'):return self.reply({'error':'Unknown endpoint'},404)
        if path.startswith('/performance-media/'):
            name=path.removeprefix('/performance-media/')
            if not re.fullmatch(r'[a-f0-9]{32}\.mp4',name):return self.reply({'error':'Not found'},404)
            return self.media(MEDIA/name)
        if '..' in path.split('/') or any(x.startswith('.') for x in path.split('/') if x):return self.reply({'error':'Not found'},404)
        if path.lower().endswith(('.mp4','.mov','.webm')):
            target=self.static_file(path)
            if target is None:return self.reply({'error':'Not found'},404)
            return self.media(target)
        super().do_GET()
    def media(self,path):
        if not path.is_file():return self.reply({'error':'Video unavailable'},404)
        size=path.stat().st_size;start=0;end=size-1;partial=False
        header=self.headers.get('Range')
        if header:
            m=re.fullmatch(r'bytes=(\d*)-(\d*)',header)
            if not m or not any(m.groups()):return self.reply({'error':'Invalid byte range'},416)
            a,b=m.groups();start=int(a) if a else max(0,size-int(b));end=min(size-1,int(b)) if a and b else size-1
            if start>end or start>=size:return self.reply({'error':'Range unavailable'},416)
            partial=True
        mime={'.mp4':'video/mp4','.mov':'video/quicktime','.webm':'video/webm'}[path.suffix.lower()]
        self.send_response(206 if partial else 200);self.send_header('Content-Type',mime);self.send_header('Accept-Ranges','bytes');self.send_header('Content-Length',str(end-start+1))
        if partial:self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
        self.end_headers()
        try:
            with path.open('rb') as f:
                f.seek(start);remaining=end-start+1
                while remaining:
                    chunk=f.read(min(128*1024,remaining))
                    if not chunk:break
                    self.wfile.write(chunk);remaining-=len(chunk)
        except (BrokenPipeError,ConnectionResetError):pass
    def do_POST(self):
        origin=self.headers.get('Origin');allowed={f'http://127.0.0.1:{self.server.server_port}',f'http://localhost:{self.server.server_port}'}
        if not self.local() or (origin and origin not in allowed) or self.headers.get('X-Performance-Token')!=self.server.token:
            return self.reply({'error':'Missing local performance authorization'},403)
        try:
            length=int(self.headers.get('Content-Length',0))
            if self.path=='/api/director/capture':return self.capture(length)
            if not 0<length<(200_000 if self.path=='/api/fal/proxy' else 10_000):raise ValueError('Invalid request size')
            data=json.loads(self.rfile.read(length))
            if not isinstance(data,dict):raise ValueError('Expected an object')
            if self.path=='/api/characters/generate':return self.reply(self.server.boards.start(data))
            if self.path=='/api/director/key':
                self.server.director.set_key(data.get('key'));return self.reply({'ok':True})
            if self.path=='/api/director/account':
                return self.reply(self.server.director.check_account())
            if self.path=='/api/director/cast':
                lead=self.server.performance.snapshot()['character']
                return self.reply(self.server.cast.prepare(data.get('ids'),lead,data.get('consent')))
            if self.path=='/api/director/lead':
                gate=self.server.director
                with gate.lock:
                    live=gate.snapshot()
                    if not live['active'] or not live['referenceMode'] or data.get('runId')!=gate.run:
                        raise ValueError('The named-cast video session is no longer active.')
                    if data.get('fromId')!=gate.lead:raise ValueError('The leading DJ changed. Ask again.')
                    pinned=next((p for p in gate.cast if p['id']==data.get('toId')),None)
                    if not pinned:raise ValueError('This DJ was not admitted to the live cast.')
                    visual=self.server.profiles.visual(pinned['id'])
                    if any(visual[k]!=pinned[k] for k in visual):raise ValueError('The character changed after this session began. Restart video with the updated cast.')
                    result=self.server.performance.assign_live_lead(visual)
                    gate.lead=pinned['id']
                    return self.reply(result)
            if self.path=='/api/director/reference':
                selected=self.server.performance.snapshot();character=selected['character']
                if data.get('consent') is not True or data.get('character')!=character:raise ValueError('Confirm the selected reference upload')
                if 'appearance' in data or 'expectedReferenceKey' in data:
                    result=self.server.stages.prepare(self.server.director,character,data.get('appearance'),data.get('expectedReferenceKey'))
                    if character!=self.server.performance.snapshot()['character']:
                        raise ValueError('The selected DJ changed while the reference was preparing. Refresh the live preview.')
                    return self.reply(result)
                poster=reference_file(selected['poster'])
                content=poster.read_bytes()
                if len(content)>5_000_000:raise ValueError('Reference is too large')
                self.server.director.prepare_reference(character,content)
                return self.reply({'ok':True,'character':character})
            if self.path=='/api/director/start':
                # Serialize selection with admission. Reference preparation can
                # take time; never spend an admission on a different performer.
                with self.server.director.lock:
                    character=self.server.performance.snapshot()['character']
                    if 'expectedCharacter' in data and data['expectedCharacter']!=character:
                        raise ValueError('The selected DJ changed while live video was preparing. Start again with the current DJ.')
                    stage=None
                    if 'appearance' in data or 'expectedReferenceKey' in data:
                        if data.get('castIds'):raise ValueError('Named casts do not use a single-performer live stage.')
                        stage,_=self.server.stages.expected(character,data.get('appearance'),data.get('expectedReferenceKey'))
                    if data.get('profile')=='director-live-v1':
                        cast=self.server.cast.admission(data['castIds'],character) if data.get('castIds') else None
                        admission=self.server.director.start(character,mode='live',consent=data.get('consent'),minutes=data.get('minutes'),cast=cast,stage_reference=stage)
                    elif data.get('profile')=='director-study-v1':admission=self.server.director.start(character,stage_reference=stage)
                    else:raise ValueError('Use the current live video controls')
                return self.reply(admission)
            if self.path=='/api/director/music':
                self.server.director.music(self.headers.get('X-Director-Lease'),data)
                return self.reply({'ok':True})
            if self.path=='/api/director/stop':
                self.server.director.stop();return self.reply({'ok':True})
            if self.path=='/api/fal/proxy':return self.proxy(data)
            if self.path=='/api/performance/command':
                with self.server.director.lock:
                    if self.server.director.snapshot()['active'] and data.get('action') in ('rehearse','generate','pause','character','profile'):
                        raise ValueError('Stop Director before switching to clip rehearsal')
                    result=self.server.performance.command(data)
                    if data.get('action')=='profile':
                        # A reviewed revision must never reuse an older hosted image.
                        self.server.director.references.pop(result['character'],None)
                return self.reply(result)
            if self.path=='/api/performance/display':
                self.server.performance.heartbeat(data);return self.reply({'ok':True})
            return self.reply({'error':'Unknown endpoint'},404)
        except ValueError as exc:return self.reply({'error':str(exc)},400)
        except (KeyError,TypeError):return self.reply({'error':'Command unavailable or invalid. Check provider readiness and stop generation before restarting.'},400)
        except Exception:return self.reply({'error':'The provider operation was not confirmed. No automatic retry was submitted.'},502)

    def capture(self,length):
        run=self.headers.get('X-Director-Run','');capture_token=self.headers.get('X-Director-Capture','')
        if not self.server.director.authorize_capture(run,capture_token):return self.reply({'error':'Unknown recording session'},403)
        mime=self.headers.get('Content-Type','').split(';')[0]
        extension={'video/mp4':'mp4','video/webm':'webm','application/json':'json'}.get(mime)
        limit=60_000 if extension=='json' else 60_000_000
        if not extension or not 0<length<=limit:raise ValueError('Invalid recording size or type')
        self.server.captures.mkdir(parents=True,exist_ok=True)
        target=self.server.captures/(run+'.'+extension)
        # One receipt and one recording per admission; a retry never overwrites evidence.
        if target.exists() or (extension!='json' and any((self.server.captures/(run+'.'+ext)).exists() for ext in ('webm','mp4'))):
            return self.reply({'error':'This recording was already saved'},409)
        body=self.rfile.read(length)
        if len(body)!=length:raise ValueError('Incomplete recording')
        if extension=='json':
            receipt=json.loads(body)
            if not isinstance(receipt,dict) or receipt.get('runId')!=run:raise ValueError('Invalid receipt')
            # Accept only locally measured fields, never credentials or arbitrary nested payloads.
            allowed={'runId','character','model','mode','resolution','source','targetSeconds','generatedLimitSeconds','startedAt','startupMs','elapsedSeconds',
                'presentedSeconds','generatedSeconds','frames','stallCount','maxFrameGapMs','reason','events','recordingMime'}
            if set(receipt)-allowed:raise ValueError('Unexpected receipt fields')
            events=receipt.get('events',[])
            if not isinstance(events,list) or len(events)>120:raise ValueError('Invalid event log')
            for event in events:
                if not isinstance(event,dict) or set(event)-{'atMs','type','version','source','label','generationSeconds','bufferSeconds','latencyMs'}:raise ValueError('Invalid event')
                if any(not isinstance(v,(str,int,float,type(None))) or (isinstance(v,str) and len(v)>240) for v in event.values()):raise ValueError('Invalid event value')
            for key,value in receipt.items():
                if key!='events' and (not isinstance(value,(str,int,float,type(None))) or (isinstance(value,str) and len(value)>240)):raise ValueError('Invalid receipt value')
            body=json.dumps(receipt,allow_nan=False,indent=2).encode()
        elif extension=='mp4' and b'ftyp' not in body[:32]:raise ValueError('Invalid MP4 header')
        elif extension=='webm' and not body.startswith(b'\x1aE\xdf\xa3'):raise ValueError('Invalid WebM header')
        with target.open('xb') as output:output.write(body)
        return self.reply({'url':'/director-captures/'+target.name,'bytes':len(body)})

    def proxy(self,data):
        target=self.headers.get('x-fal-target-url','');lease=self.headers.get('X-Director-Lease','')
        try:kind=self.server.director.authorize(lease,target,data)
        except ValueError:return self.reply({'error':'Director request blocked: check session approval, ownership and deadline.'},403)
        headers_sent=False
        try:
            with self.server.director.upstream(target,data) as response:
                self.send_response(response.status)
                self.send_header('Content-Type',response.headers.get('Content-Type','application/json'))
                # HTTP/1.0 closes on completion; read1 and flush preserve streaming keepalives.
                self.end_headers();headers_sent=True;body=bytearray()
                while chunk:=response.read1(16_384):
                    if len(body)+len(chunk)>500_000:raise ValueError('Oversized signaling response')
                    body.extend(chunk);self.wfile.write(chunk);self.wfile.flush()
                if kind=='session':self.server.director.bind(lease,body)
        except HTTPError as error:
            try:self.reply(signaling_error(error),error.code if 400<=error.code<=599 else 502)
            finally:error.close()
        except (BrokenPipeError,ConnectionResetError):pass
        except Exception:
            # Do not expose credential-bearing URLs or provider exception text.
            if headers_sent:self.close_connection=True
            else:self.reply({'error':'Fal signaling failed or timed out. No automatic retry was submitted.'},502)



def make_server(port=8768,audio_port=8765,provider=None,generation_limit=0,director=None,captures=None,profiles=None,stages=None):
    httpd=LoopbackHTTPServer(('127.0.0.1',port),Handler);httpd.token=secrets.token_urlsafe(32)
    httpd.performance=Performance(provider,generation_limit);httpd.audio_port=audio_port
    httpd.director=director or DirectorGate()
    httpd.profiles=profiles or AgentProfiles()
    httpd.boards=CharacterBoards(httpd.profiles,httpd.director)
    httpd.cast=DirectorCast(httpd.profiles,httpd.director,WEB)
    httpd.stages=stages or DirectorStages(httpd.profiles,ROOT)
    httpd.captures=Path(captures) if captures else CAPTURES
    return httpd


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--port',type=int,default=8768);p.add_argument('--audio-port',type=int,default=8765)
    p.add_argument('--approved-h3-jobs',type=int,default=0,help='Explicit operator-approved maximum submissions, including failed or uncertain jobs (0 disables paid generation).')
    p.add_argument('--approved-director-sessions',type=int,default=0,help='Cumulative explicitly approved Director attempts, including failed or uncertain attempts. Restarting does not reset the ledger.')
    p.add_argument('--director-seconds',type=int,default=120)
    p.add_argument('--restore-performance',type=Path)
    p.add_argument('--h3-seed-url', help='User-provided Fal media URL for optional video continuation')
    args=p.parse_args()
    if not 15<=args.director_seconds<=900:p.error('Director duration must be 15..900 seconds')
    if not 0<=args.approved_director_sessions<=10:p.error('Approved Director attempts must be 0..10; increase only after explicit approval')
    if not 0<=args.approved_h3_jobs<=30:p.error('Approved jobs must be 0..30')
    provider=None
    if args.approved_h3_jobs:
        key=os.environ.get('FAL_KEY')
        if not key:p.error('Set FAL_KEY privately in the server environment; never in the browser or source.')
        if not args.h3_seed_url:p.error('Provide --h3-seed-url for video continuation.')
        try:provider=FalContinuation(key,MEDIA,args.h3_seed_url)
        except ValueError as exc:p.error(str(exc))
    httpd=make_server(args.port,args.audio_port,provider,args.approved_h3_jobs,DirectorGate(os.environ.get('FAL_KEY'),args.approved_director_sessions,args.director_seconds,ledger=ROOT/'.runtime/director-admission.json'))
    if args.restore_performance:
        saved=json.loads(args.restore_performance.read_text())
        profiles=AgentProfiles()
        if saved['character'] in ('rivet','nova') and saved.get('source')!='approved reference':
            httpd.performance.command({'action':'character','character':saved['character']})
        else:httpd.performance.command({'action':'profile','profile':profiles.visual(saved['character'])})
        httpd.performance.command({'action':'settings',**{k:saved[k] for k in ('look','fit','caption','prompt','beatSync')}})
        if saved['mode'] in ('paused','blackout'):httpd.performance.command({'action':'pause' if saved['mode']=='paused' else 'blackout'})
    print(f'Afterhours performance service: http://127.0.0.1:{httpd.server_port}',flush=True)
    print(f'Hosted submission limit: {args.approved_h3_jobs}',flush=True)
    try:httpd.serve_forever()
    except KeyboardInterrupt:pass
    finally:httpd.performance.cancel.set();httpd.server_close()


if __name__=='__main__':main()
