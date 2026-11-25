"""Local DJ services and the client command API."""
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from urllib.request import urlopen, Request
import argparse
import json
import secrets
import threading
import time
import os
import signal
import sqlite3
from afterhours import ai_mix, playback_requests, queue_planning
from afterhours.planner_policy import track_seconds
from afterhours.dj_bridge import BridgeError
from afterhours.vdj_api import request as vdj_request
from afterhours.vdj_session import DJSession
from afterhours.dj_agent import DJAgent
from afterhours.live_voice import LiveVoice
from afterhours.agent_profiles import AgentProfiles
from afterhours.library_actions import library_action
from afterhours.recording_availability import RecordingAvailability
from afterhours.session_recovery import SessionRecovery
from afterhours.room_requests import RoomRequests
from afterhours.track_artwork import TrackArtwork
from afterhours.record_crate import RecordCrate, page_parameters
from afterhours.run_archive import RunArchive
from afterhours.guest_requests import GuestLedger
from afterhours.guest_coordinator import GuestCoordinator
from afterhours.guest_protocol import GuestProtocol
from afterhours.guest_polls import GuestPolls
from afterhours.guest_poll_coordinator import PollCoordinator
from afterhours.web_runtime import RuntimeStaticHandler
from afterhours import mcp_http
from afterhours.mcp_control import MCPControl
from afterhours.mcp_receipts import ReceiptStore

# A local candidate can reuse the installed workspace's profile/media and
# conversation store, including the roster shared with the audience service.
from afterhours.paths import data_root

ROOT = data_root()
WEB = ROOT / "web"
RUNTIME = ROOT / ".runtime"
LOCK = threading.Lock()
VDJ_LOCK = threading.Lock()
RUN_ARCHIVE = RunArchive(RUNTIME/'recaps')
DJ_SESSION = DJSession(VDJ_LOCK, availability=RecordingAvailability(RUNTIME/'recording-availability.json'),
                       crate=RecordCrate(RUNTIME/'record-crate.sqlite3'),archive=RUN_ARCHIVE)
DJ_AGENT = DJAgent(DJ_SESSION, RUNTIME/'dj-conversation.json')
TRACK_ARTWORK = TrackArtwork(on_refresh=DJ_SESSION.crate.refresh_artwork)
PROFILES = AgentProfiles(ROOT)
GUEST_LEDGER = GUEST_COORDINATOR = GUEST_PROTOCOL = GUEST_POLLS = POLL_COORDINATOR = None
GUEST_ERROR = None
# Browser and phone optional-poll decoders passed their shared-contract gate.
# This flag never enables the venue listener or grants a planner allowance.
POLL_CLIENTS_READY = True
def crowd_poll_enabled():
    return bool(POLL_CLIENTS_READY and ROOM_REQUESTS.server is not None and ROOM_REQUESTS.protocol is GUEST_PROTOCOL)
try:
    GUEST_LEDGER = GuestLedger(RUNTIME/'guest-requests.sqlite3')
    GUEST_POLLS = GuestPolls(GUEST_LEDGER)
    GUEST_COORDINATOR = GuestCoordinator(GUEST_LEDGER,DJ_SESSION,lambda:PROFILES.context())
    POLL_COORDINATOR = PollCoordinator(GUEST_POLLS,GUEST_COORDINATOR.night,crowd_poll_enabled,
        DJ_AGENT.valid_crowd_offer,DJ_AGENT.adopt_crowd_choice,
        scope_generation=lambda:(GUEST_PROTOCOL.scope or {}).get('generation') if GUEST_PROTOCOL else None)
    GUEST_COORDINATOR.polls=POLL_COORDINATOR
    GUEST_PROTOCOL = GuestProtocol(GUEST_LEDGER,DJ_SESSION.crate,DJ_SESSION.availability,
        GUEST_COORDINATOR.context,GUEST_COORDINATOR.night,on_request=GUEST_COORDINATOR.wake,
        polls=GUEST_POLLS,poll_supported=lambda:bool(POLL_CLIENTS_READY and POLL_COORDINATOR and POLL_COORDINATOR.supported))
except (sqlite3.Error,OSError,ValueError):
    if GUEST_LEDGER:GUEST_LEDGER.close()
    GUEST_LEDGER = GUEST_COORDINATOR = GUEST_PROTOCOL = GUEST_POLLS = POLL_COORDINATOR = None
    GUEST_ERROR = 'Guest request storage is unavailable. Saved requests were retained; music controls remain available.'
DJ_AGENT.guest_requests = GUEST_COORDINATOR
DJ_AGENT.crowd_polls = POLL_COORDINATOR
ROOM_REQUESTS = RoomRequests(DJ_AGENT.audience_request,protocol=GUEST_PROTOCOL,unavailable_reason=GUEST_ERROR)
DJ_AGENT.profile_context = PROFILES.context
DJ_AGENT.resolve_mentions = PROFILES.mentioned
DJ_AGENT.roster_command = PROFILES.command
DJ_AGENT.default_profile = lambda identifier: PROFILES.mutate('default',{'id':identifier})
LIVE_VOICE = LiveVoice(DJ_AGENT)
TOKEN = secrets.token_urlsafe(32)
MCP_CONTROL = None
STATE = {"connected":False, "playing":False, "message":"Open the app and enable audio."}
COMMANDS = []
OWNER = {"id":None,"seen":0}
SEQ = 0
ALLOWED = {"start", "pause", "resume", "stop", "mix", "auto", "energy", "queue", "play-draft", "takeover"}


def start_guest_requests():
    global GUEST_LEDGER,GUEST_COORDINATOR,GUEST_PROTOCOL,GUEST_ERROR,GUEST_POLLS,POLL_COORDINATOR
    if GUEST_COORDINATOR is None:return
    try:GUEST_COORDINATOR.start()
    except (sqlite3.Error,OSError,ValueError):
        # This optional ingress must not stop core music startup or silently
        # fall back to the old untracked form when its durable store is down.
        GUEST_COORDINATOR.close();GUEST_LEDGER.close()
        GUEST_LEDGER = GUEST_COORDINATOR = GUEST_PROTOCOL = GUEST_POLLS = POLL_COORDINATOR = None
        DJ_AGENT.guest_requests = DJ_AGENT.crowd_polls = None
        GUEST_ERROR = 'Guest request storage is unavailable. Saved requests were retained; music controls remain available.'
        ROOM_REQUESTS.close()
        with ROOM_REQUESTS.lock:
            ROOM_REQUESTS.protocol=None;ROOM_REQUESTS.unavailable_reason=GUEST_ERROR


class Handler(RuntimeStaticHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB), **kwargs)

    def log_message(self, fmt, *args):
        if not self.path.startswith('/api/'):
            super().log_message(fmt, *args)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; frame-src 'self' http://127.0.0.1:8768; worker-src 'self'; object-src 'none'; frame-ancestors 'none'")
        super().end_headers()

    def valid_host(self):
        return self.headers.get("Host", "").split(":")[0] in {"127.0.0.1", "localhost"}

    def reply(self, data, status=200):
        payload = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers(); self.wfile.write(payload)

    def virtualdj(self, data):
        if DJ_SESSION.active and data.get('action') in {'takeover', 'automix-stop'} and data.get('execute'):
            return self.reply(DJ_SESSION.stop(pause=False),202)
        # Serial execution avoids simultaneous search/load operations in this
        # process. Never queue stale hardware commands behind a slow request.
        if not VDJ_LOCK.acquire(blocking=False):
            return self.reply({"error":"VirtualDJ is busy; inspect state before retrying."},409)
        try:
            if DJ_SESSION.active and data.get('execute') and data.get('action') in {'play', 'pause'}:
                return self.reply({'error':'Take over the scheduled set before using individual deck transport.'},409)
            result = vdj_request(data, port=getattr(self.server, 'vdj_port', 7474))
            return self.reply(result)
        except ValueError as exc:
            return self.reply({"error":str(exc)},400)
        except (BridgeError, OSError) as exc:
            return self.reply({"connected":False,"error":str(exc)},503)
        finally:
            VDJ_LOCK.release()

    def agent_video_request(self,path,data=None):
        base='http://127.0.0.1:8768'
        try:
            with urlopen(base+'/api/performance/config',timeout=2) as r:config=json.load(r)
            request=Request(base+path,data=json.dumps(data).encode() if data is not None else None,
                headers={'Content-Type':'application/json','X-Performance-Token':config['token']})
            with urlopen(request,timeout=5) as r:return json.load(r)
        except OSError as exc:
            from urllib.error import HTTPError
            if isinstance(exc,HTTPError):
                try:message=json.loads(exc.read(4000)).get('error')
                except Exception:message=None
                raise ValueError(message or 'The video service could not confirm this change.') from None
            raise ValueError('The video service is unavailable. Start it before assigning a DJ or generating a board.') from None

    def commit_handoff(self,change,context):
        current=PROFILES.context()
        if current['id']!=change['fromAgentId']:raise ValueError('The leading DJ changed while this handoff was prepared. Ask again.')
        visual=PROFILES.visual(change['toAgentId'])
        if not visual['poster']:raise ValueError('Approve a booth frame in Agents first.')
        if context.get('ready'):
            self.agent_video_request('/api/director/lead',{'runId':context.get('runId'),'fromId':current['id'],'toId':visual['id']})
        else:
            self.agent_video_request('/api/performance/command',{'action':'profile','profileId':visual['id']})
        PROFILES.mutate('assign',{'id':visual['id']})
        DJ_SESSION.event(f"{visual['name']} leads future decisions. Existing music and queue are unchanged.",'agent-handoff')

    def reset_default_profile(self):
        db=PROFILES.snapshot();target=PROFILES.get(db['default'])
        if db['assigned']!=target['id']:
            self.commit_handoff({'fromAgentId':db['assigned'],'toAgentId':target['id']},{'ready':False})
        return target['name']

    def agent_video(self,action,data=None):
        try:return self.reply(self.agent_video_request('/api/characters/status' if action=='status' else '/api/characters/generate',data))
        except ValueError as exc:return self.reply({'error':str(exc)},409)

    def do_GET(self):
        if not self.valid_host(): return self.reply({"error":"Local access only"},403)
        path = urlparse(self.path)
        if path.path == '/api/agents':
            if self.headers.get('X-DJ-Token') != TOKEN:return self.reply({'error':'Missing local session token'},403)
            return self.reply(PROFILES.snapshot())
        if path.path == '/api/agents/generation-status':
            if self.headers.get('X-DJ-Token') != TOKEN:return self.reply({'error':'Missing local session token'},403)
            return self.agent_video('status')
        if path.path == '/api/set-session':
            return self.reply(TRACK_ARTWORK.decorate(DJ_SESSION.snapshot()))
        if path.path == '/api/catalog-crate':
            if self.headers.get('X-DJ-Token') != TOKEN:
                return self.reply({'error':'Missing local session token'},403)
            try:
                page,size,include=page_parameters(parse_qs(path.query))
                result=DJ_SESSION.crate.page(page,size,include)
                # Existing provider artwork stays authoritative on every page;
                # this read never starts a provider request or changes a queue.
                result['records']=TRACK_ARTWORK.decorate({'candidates':result['records']})['candidates']
                return self.reply(result)
            except ValueError as exc:
                return self.reply({'error':str(exc)},400)
        if path.path == '/api/recap':
            if self.headers.get('X-DJ-Token') != TOKEN:
                return self.reply({'error':'Missing local session token'},403)
            try:
                query=parse_qs(path.query,keep_blank_values=True)
                if set(query)!={'archiveId'} or len(query['archiveId'])!=1:
                    raise ValueError('Choose one exact archived run.')
                return self.reply(DJ_AGENT.recap(query['archiveId'][0]))
            except ValueError as exc:return self.reply({'error':str(exc)},400)
        if path.path == '/api/guest-requests':
            if self.headers.get('X-DJ-Token') != TOKEN:
                return self.reply({'error':'Missing local session token'},403)
            if GUEST_LEDGER is None or GUEST_COORDINATOR is None:
                return self.reply({'error':GUEST_ERROR or 'Guest requests are unavailable.','code':'temporarily-unavailable'},503)
            try:
                query=parse_qs(path.query,keep_blank_values=True,max_num_fields=3)
                if set(query)-{'page','pageSize','nightId'} or any(len(v)!=1 for v in query.values()):
                    raise ValueError('Choose one request page.')
                night=GUEST_COORDINATOR.night()
                if query.get('nightId',[night])[0]!=night:raise ValueError('Choose the current night’s requests.')
                return self.reply(GUEST_LEDGER.operator_page(night,int(query.get('page',['0'])[0]),int(query.get('pageSize',['50'])[0])))
            except ValueError as exc:return self.reply({'error':str(exc)},400)
        if path.path == '/api/control-state':
            # Cached readbacks only: an HTTP poll never sends an OSC command.
            result=DJ_SESSION.control_state()
            if parse_qs(path.query).get('program',['1'])[0] != '0':
                result['program']=DJ_SESSION.program()
            return self.reply(result)
        if path.path == '/api/waveform':
            data=DJ_SESSION.waveform(parse_qs(path.query).get('track',[''])[0])
            return self.reply(data if data else {'error':'Detailed waveform not prepared for this recording.'},200 if data else 404)
        if path.path == '/api/ai/status':
            return self.reply(ai_mix.status())
        if path.path in ('/api/dj-agent','/api/voice'):
            if self.headers.get('X-DJ-Token') != TOKEN:
                return self.reply({'error':'Missing local session token'},403)
            return self.reply({**DJ_AGENT.snapshot(),'roomRequests':ROOM_REQUESTS.snapshot()} if path.path=='/api/dj-agent' else LIVE_VOICE.status())
        if path.path == '/api/program':
            program = DJ_SESSION.program()
            if program is not None:
                return self.reply(program)
            with LOCK:
                return self.reply(dict(STATE, connected=time.time()-OWNER['seen'] < 5))
        read_actions = {"/api/vdj/status": "status", "/api/vdj/catalog": "catalog-status",
                        "/api/vdj/queue": "queue-status"}
        if path.path in read_actions:
            if self.headers.get("X-DJ-Token") != TOKEN:
                return self.reply({"error":"Missing local session token"},403)
            return self.virtualdj({"action":read_actions[path.path]})
        if path.path == "/api/config":
            return self.reply({"token":TOKEN,"version":"0.1.0","backend":"VirtualDJ","service":"afterhours-music","guardedResume":True})
        if path.path == "/api/state":
            with LOCK:
                state = dict(STATE, connected=time.time()-OWNER["seen"] < 5)
            return self.reply(state)
        if path.path == "/api/events":
            self.send_response(200)
            self.send_header('Content-Type','text/event-stream')
            self.send_header('Connection','keep-alive')
            self.end_headers()
            last = None
            try:
                while True:
                    with LOCK:
                        data = {k:STATE.get(k) for k in ('clock','transition','current','next','energyTarget','playing','paused','completedTransitions','updated')}
                        data['connected'] = time.time()-OWNER['seen'] < 5
                    serialized=json.dumps(data)
                    if serialized != last:
                        self.wfile.write(('event: transport\ndata: '+serialized+'\n\n').encode())
                        self.wfile.flush(); last=serialized
                    time.sleep(.2)
            except (BrokenPipeError, ConnectionResetError):
                return
        if path.path == "/api/commands":
            if self.headers.get("X-DJ-Token") != TOKEN:
                return self.reply({"error":"Missing local session token"},403)
            q = parse_qs(path.query)
            try: after = int(q.get("after", ["0"])[0])
            except ValueError: return self.reply({"error":"Invalid cursor"},400)
            with LOCK:
                if q.get("client", [None])[0] != OWNER['id']:
                    return self.reply({"error":"This tab is not the active audio session"},409)
                OWNER['seen'] = time.time()
                result = [c for c in COMMANDS if c["id"] > after and time.time()-c['created'] < 15]
            return self.reply({"commands":result})
        if path.path.startswith('/api/'):
            return self.reply({"error":"Unknown API path"},404)
        if '..' in path.path.split('/') or any(p.startswith('.') for p in path.path.split('/') if p):
            return self.reply({"error":"Not found"},404)
        super().do_GET()

    def do_POST(self):
        global SEQ
        if not self.valid_host(): return self.reply({"error":"Local access only"},403)
        origin = self.headers.get("Origin")
        expected = {f"http://127.0.0.1:{self.server.server_port}",f"http://localhost:{self.server.server_port}"}
        if origin and origin not in expected: return self.reply({"error":"Foreign origin"},403)
        if mcp_http.handle(self,MCP_CONTROL,TOKEN):return
        if self.headers.get("X-DJ-Token") != TOKEN:
            return self.reply({"error":"Missing local session token"},403)
        try:
            size = int(self.headers.get("Content-Length",0))
            if not 0 < size <= (8_100_000 if self.path=='/api/agents/photo' else 200_000): raise ValueError("Invalid request length")
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict): raise ValueError("Expected object")
        except (ValueError, json.JSONDecodeError) as e:
            return self.reply({"error":str(e)},400)
        if self.path == "/api/vdj/command":
            return self.virtualdj(data)
        if self.path == '/api/room-requests':
            try:return self.reply(ROOM_REQUESTS.configure(data.get('enabled')))
            except (ValueError,OSError) as exc:return self.reply({'error':str(exc)},409)
        if self.path == '/api/vdj/control':
            try:return self.reply(DJ_SESSION.console_control(data))
            except ValueError as exc:return self.reply({'error':str(exc)},409)
            except (BridgeError,OSError) as exc:return self.reply({'error':str(exc)},503)
        if self.path == '/api/library':
            try:return self.reply(library_action(DJ_SESSION,data))
            except (ValueError,BridgeError) as exc:return self.reply({'error':str(exc)},409)
        if self.path.startswith('/api/agents'):
            try:
                if self.path=='/api/agents/photo':
                    PROFILES.upload(data.get('id'),data.get('content'),data.get('name'),data.get('role','photo'))
                elif self.path=='/api/agents/generate':return self.agent_video('generate',data)
                elif self.path=='/api/agents':
                    action=data.get('action')
                    if action=='assign':
                        if LIVE_VOICE.status()['status'] not in ('idle','closed','error') or DJ_AGENT.snapshot()['evaluating']:
                            raise ValueError('Finish the conversation and stop Talk before assigning a different DJ.')
                        visual=PROFILES.visual(data.get('id'))
                        if not visual['poster']:raise ValueError('Approve a booth frame in Character before assigning this DJ to the show.')
                        self.agent_video_request('/api/performance/command',{'action':'profile','profileId':visual['id']})
                    profile=PROFILES.mutate(action,data)
                    DJ_SESSION.event(f"{profile['name']} · {'assigned to the show' if action=='assign' else 'profile '+action+' saved'}.",'agent-profile')
                    return self.reply({**PROFILES.snapshot(),'selected':profile['id']})
                else:raise ValueError('Unknown agent endpoint.')
                return self.reply(PROFILES.snapshot())
            except (ValueError,OSError,BridgeError) as exc:return self.reply({'error':str(exc)},409)
        if self.path in ('/api/dj-agent','/api/voice'):
            try:
                action=data.get('action')
                if self.path=='/api/dj-agent':
                    if action in ('message','start-new-queue'):
                        source='typed' if action=='start-new-queue' else data.get('source','typed')
                        text=data.get('text','Start a fresh queue.') if action=='start-new-queue' else data.get('text')
                        mode='fresh-start' if action=='start-new-queue' else data.get('queueMode','steer')
                        if source=='voice':LIVE_VOICE.require(data.get('sessionId'))
                        if (mode in ('replace','fresh-start') or playback_requests.intent(text)=='fresh-start') and LIVE_VOICE.status()['status'] not in ('idle','closed','error'):
                            raise ValueError('Stop Talk before starting a fresh queue. You can still steer this queue.')
                        DJ_AGENT.handoff_commit=self.commit_handoff
                        DJ_AGENT.reset_profile=self.reset_default_profile
                        result=DJ_AGENT.message(text,source,data.get('messageId'),data.get('approveRequest') is True,mode,data.get('performanceContext'),data.get('approvePreparation') is True,data.get('surface'),data.get('nativeSettingsContext'),data.get('recapContext'),data.get('approveSessionCuration') is True)
                    elif action in ('catalog-play-next','catalog-put-back'):
                        result=DJ_AGENT.catalog_action(action,data.get('requestId'),data.get('trackId'),data.get('expected'),data.get('searchId'))
                    elif action=='track-feedback':
                        result=DJ_AGENT.record_track_feedback(data.get('trackId'),data.get('preference'),data.get('reason',''))
                    elif action=='settings':
                        result=DJ_AGENT.settings_action(data.get('requestId'),data.get('change'),data.get('expected'))
                    elif action=='performance-delivery':
                        result=DJ_AGENT.performance_delivery(data.get('commandId'),data.get('ownerId'),data.get('runId'),data.get('status'))
                    elif action=='native-settings-delivery':
                        result=DJ_AGENT.native_settings_delivery(data.get('commandId'),data.get('ownerId'),data.get('sessionId'),data.get('status'),data.get('message'))
                    elif action=='apply':result=DJ_AGENT.apply(data.get('revision'))
                    elif action=='prepare-set':result=DJ_AGENT.prepare_set(data.get('approveRequest') is True,data.get('requestId'))
                    elif action=='stop':result=DJ_AGENT.disable()
                    elif action=='fresh':
                        if LIVE_VOICE.status()['status'] not in ('idle','closed','error'):
                            raise ValueError('Stop Talk before starting a fresh set.')
                        DJ_AGENT.reset_profile=self.reset_default_profile
                        result=DJ_AGENT.fresh()
                    else:raise ValueError('Unknown conversation action.')
                elif action=='start':
                    result=LIVE_VOICE.create(data.get('sdp'),data.get('requestId'),data.get('performer','nova'),data.get('approveRequest') is True,data.get('maxSeconds',120))
                elif action=='end':result=LIVE_VOICE.end(data.get('sessionId'))
                elif action=='usage':result=LIVE_VOICE.receipt(data.get('sessionId'),data.get('seconds'),data.get('finalized') is True)
                else:raise ValueError('Unknown voice action.')
                return self.reply(result)
            except (ValueError,BridgeError) as exc:return self.reply({'error':str(exc)},409)
        if self.path == '/api/stage':
            # Only the existing local clip and blackout are available here.
            # Paid generation/signaling are deliberately not proxied.
            if data.get('action') not in {'rehearse','blackout','character'}:
                return self.reply({'error':'Unknown stage action'},400)
            try:
                base='http://127.0.0.1:8768'
                with urlopen(base+'/api/performance/config',timeout=2) as r:
                    stage_config=json.load(r)
                if stage_config.get('director',{}).get('active'):
                    raise ValueError('Stop Director in the performance desk before changing the stage source.')
                def stage_command(payload):
                    req=Request(base+'/api/performance/command',data=json.dumps(payload).encode(),
                        headers={'Content-Type':'application/json','X-Performance-Token':stage_config['token']})
                    with urlopen(req,timeout=2) as r:return json.load(r)
                if data['action'] in {'rehearse','character'}:
                    with urlopen(base+'/api/performance',timeout=2) as r: current=json.load(r)
                    stage_command({**{key:current[key] for key in ('look','fit','caption','prompt')},
                                   'action':'settings','beatSync':True})
                result=stage_command({'action':data['action'], **({'character':data.get('character')} if data['action']=='character' else {})})
                return self.reply({'mode':result['mode'],'generation':result['generation'],'beatSync':result['beatSync'],'performer':result['performer'],'character':result['character']})
            except (OSError, ValueError, KeyError):
                return self.reply({'error':'Stage control unavailable. Open the performance desk and check its connection.'},503)
        if self.path == '/api/set-session':
            try:
                action = data.get('action')
                if action == 'discover':
                    result = DJ_SESSION.discover(data.get('query'), data.get('artist',''),data.get('genre',''),data.get('bpmMin',40),data.get('bpmMax',300))
                elif action == 'plan':
                    result = DJ_SESSION.plan(data.get('ids'), data.get('seconds',track_seconds()), data.get('excerptBars',16),data.get('maxTempoShift',8),data.get('mixStyle','phrase'))
                elif action == 'start':
                    result = DJ_SESSION.start()
                elif action in ('insert-track','preview-insertion'):
                    track_id=data.get('trackId')
                    if not isinstance(track_id,str) or not track_id:raise ValueError('Choose an exact recording to insert.')
                    with DJ_SESSION.lock:
                        DJ_SESSION.require_available([track_id])
                        snapshot=DJ_SESSION.snapshot()
                        if not any(track['id']==track_id for track in snapshot['candidates']):
                            stored=DJ_SESSION.crate.get(track_id)
                            if stored:snapshot['candidates'].append(stored)
                        placement=queue_planning.placement(snapshot,track_id)
                        receipt={key:placement[key] for key in ('slot','afterId','beforeId','options')}
                        if action=='preview-insertion':
                            return self.reply({'sessionId':snapshot['id'],'trackId':track_id,'insertion':receipt})
                        DJ_SESSION.hydrate_records([track_id],keep=placement['ids'])
                        result=DJ_SESSION.revise_queue(placement['ids'],reason='Operator requested a fitted queue insertion.',
                                                       tempo_targets=placement['tempoTargets'])
                        result={**result,'insertion':receipt}
                elif action == 'prepare':
                    result = DJ_SESSION.prepare()
                elif action == 'prepare-analysis':
                    result = DJ_SESSION.prepare_analysis()
                elif action == 'record-provider-preview':
                    result = DJ_SESSION.record_provider_preview(data.get('trackId'),data.get('provider'),
                        data.get('previewSeconds'),data.get('evidence'),data.get('observedAt'))
                elif action == 'prepare-waveforms':
                    result = DJ_SESSION.prepare_waveforms()
                elif action == 'ai-review':
                    result = DJ_SESSION.review_mix(data.get('approveRequest') is True)
                elif action == 'apply-plan':
                    result = DJ_SESSION.apply_plan()
                elif action == 'setlist':
                    result = DJ_SESSION.edit_setlist(data.get('operation'),data.get('trackId'),data.get('value'),data.get('ids'))
                elif action == 'retry-background':
                    result = DJ_SESSION.retry_background()
                elif action == 'copilot':
                    guard = {'held_recording': data['heldRecording']} if 'heldRecording' in data else {}
                    result = DJ_AGENT.copilot_action(data.get('command'), data.get('commandId'), data.get('expected'), **guard)
                elif action in {'stop','takeover'}:
                    result = DJ_SESSION.stop(pause=action=='stop')
                else:
                    raise ValueError('Unknown set-session action')
                return self.reply(result,202 if result['active'] else 200)
            except (ValueError, BridgeError) as exc:
                return self.reply({'error':str(exc)},409)
        with LOCK:
            if self.path == "/api/claim":
                client = data.get('client')
                if not isinstance(client,str) or not 8 <= len(client) <= 80:
                    return self.reply({"error":"Invalid client"},400)
                if OWNER['id'] and OWNER['id'] != client and time.time()-OWNER['seen'] < 5:
                    return self.reply({"error":"Another tab owns the audio session. Close it, wait five seconds, then reload."},409)
                OWNER.update(id=client, seen=time.time())
                return self.reply({"ok":True,"cursor":SEQ})
            if self.path == "/api/state":
                if data.get("client") != OWNER['id']:
                    return self.reply({"error":"Inactive session"},409)
                OWNER['seen']=time.time()
                STATE.clear(); STATE.update(data); STATE.pop('client',None)
                STATE['updated']=time.time()
                return self.reply({"ok":True})
            if self.path == "/api/command":
                action = data.get("action")
                if action not in ALLOWED: return self.reply({"error":"Unknown action"},400)
                if time.time()-OWNER['seen'] >= 5:
                    return self.reply({"error":"Open the local app first."},409)
                if action in {'play-draft','takeover'}:
                    if not STATE.get('sessionId') or data.get('sessionId') != STATE['sessionId']:
                        return self.reply({"error":"The audio session changed. Check local playback."},409)
                    request_id=data.get('requestId')
                    if not isinstance(request_id,str) or not 8 <= len(request_id) <= 80:
                        return self.reply({"error":"Invalid playback request ID"},400)
                    if action == 'play-draft':
                        ids=data.get('trackIds')
                        if (not isinstance(ids,list) or not 1 <= len(ids) <= 13 or
                            any(not isinstance(i,str) or not 1 <= len(i) <= 160 for i in ids) or len(set(ids)) != len(ids)):
                            return self.reply({"error":"Choose 1–13 distinct local tracks."},400)
                        if not STATE.get('audioEnabled') or 'set-run-v1' not in STATE.get('capabilities',[]):
                            return self.reply({"error":"Enable audio in the current console first."},409)
                        library={t.get('id') for t in STATE.get('library',[]) if isinstance(t,dict)}
                        if any(i not in library for i in ids):
                            return self.reply({"error":"A planned track is missing. Read local crate and rebuild."},409)
                    if any(c.get('requestId')==request_id for c in COMMANDS):
                        return self.reply({"error":"This request was already submitted. Check local playback."},409)
                if action == "energy" and (type(data.get('value')) not in (float,int) or not 0 <= data['value'] <= 1):
                    return self.reply({"error":"Energy must be 0..1"},400)
                if action == 'auto' and type(data.get('value')) is not bool:
                    return self.reply({"error":"Auto needs a boolean value"},400)
                if action == 'queue' and not isinstance(data.get('trackId'),str):
                    return self.reply({"error":"Queue needs a trackId"},400)
                if 'reason' in data and (not isinstance(data['reason'],str) or len(data['reason']) > 600):
                    return self.reply({"error":"Reason must be text under 600 characters"},400)
                SEQ += 1
                command = dict(data,id=SEQ,created=time.time())
                COMMANDS.append(command); del COMMANDS[:-100]
                return self.reply({"accepted":True,"id":SEQ,"note":"Queued; inspect state.lastCommand for execution result."},202)
        self.reply({"error":"Unknown API path"},404)


def main():
    global MCP_CONTROL
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--vdj-port',type=int,default=7474)
    parser.add_argument('--vdj-reply-port',type=int,default=7475)
    parser.add_argument('--restore-set',type=Path,help='Restore a local session JSON without starting playback')
    parser.add_argument('--no-recovery',action='store_true',help='Do not read or write the automatic queue checkpoint')
    args=parser.parse_args()
    if not 1024 <= args.vdj_port <= 65535:
        parser.error('--vdj-port must be 1024..65535')
    if not 1024<=args.vdj_reply_port<=65535 or args.vdj_reply_port==args.vdj_port:
        parser.error('--vdj-reply-port must be a different port in 1024..65535')
    recovery=None if args.no_recovery else SessionRecovery(RUNTIME/'native-set.json')
    if args.restore_set:
        try:
            if args.restore_set.stat().st_size > 2_000_000:
                raise ValueError('Saved session is larger than 2 MB.')
            DJ_SESSION.restore(json.loads(args.restore_set.read_text()))
        except (OSError, ValueError, BridgeError) as exc:
            parser.error(f'Cannot restore saved set: {exc}')
    elif recovery:
        try:DJ_SESSION.update(recovery=recovery.restore(DJ_SESSION))
        except (OSError,ValueError,TypeError,KeyError,BridgeError):
            try:recovery.retain_invalid()
            except OSError:
                # Do not overwrite an unreadable checkpoint with a fresh set.
                recovery=None
            DJ_SESSION.update(recovery={'status':'unavailable','restored':False},
                message='The saved queue could not be restored. A fresh set is ready; the saved record was retained.')
            print('Queue recovery unavailable; saved data retained. Playback remains off.',flush=True)
    DJ_AGENT.restore_settings()
    httpd=ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
    try:
        mcp_store=ReceiptStore(RUNTIME/'mcp'/'receipts.sqlite3')
        MCP_CONTROL=MCPControl(DJ_AGENT,mcp_store)
    except (OSError,ValueError,sqlite3.Error):
        if 'mcp_store' in locals():mcp_store.close()
        print('MCP control unavailable; saved receipts retained. Local DJ controls remain available.',flush=True)
    httpd.vdj_port=args.vdj_port
    DJ_SESSION.port=args.vdj_port
    if args.vdj_reply_port!=7475:
        from afterhours.dj_bridge import VirtualDJ
        DJ_SESSION.factory=lambda *a,**kwargs:VirtualDJ(*a,reply_port=args.vdj_reply_port,**kwargs)
    if recovery:recovery.start(DJ_SESSION)
    TRACK_ARTWORK.start()
    start_guest_requests()
    threading.Thread(target=DJ_SESSION.observe,daemon=True).start()
    threading.Thread(target=DJ_AGENT.run,daemon=True).start()
    RUNTIME.mkdir(exist_ok=True)
    p=RUNTIME/'session.json'
    fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as f:
        json.dump({'url':f'http://127.0.0.1:{httpd.server_port}','token':TOKEN},f)
    print(f'Afterhours music service: http://127.0.0.1:{httpd.server_port}',flush=True)
    def interrupted(*_):raise KeyboardInterrupt
    signal.signal(signal.SIGTERM,interrupted)
    try: httpd.serve_forever()
    except KeyboardInterrupt: pass
    finally:
        ROOM_REQUESTS.close()
        if GUEST_COORDINATOR:GUEST_COORDINATOR.close()
        TRACK_ARTWORK.close()
        LIVE_VOICE.end()
        if recovery:
            try:recovery.close(DJ_SESSION)
            except (OSError,ValueError,TypeError,KeyError):pass
        RUN_ARCHIVE.close()
        if GUEST_LEDGER:GUEST_LEDGER.close()
        if MCP_CONTROL:MCP_CONTROL.store.close()
        httpd.server_close()


if __name__=='__main__': main()
