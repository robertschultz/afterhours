"""GPT-Live WebRTC handshake and bounded local session lifecycle.

Browser media goes directly to OpenAI. The project key, session policy and
delegated decision budget stay here. No recording or automatic reconnection.
"""
import json
import re
import threading
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from afterhours import ai_mix
from afterhours.dj_agent import music_context

MODEL='gpt-live-1'
SECONDS=1800
DECISION_REQUESTS=60
VOICE_RATE_PER_MINUTE=.05


class LiveVoice:
    def __init__(self,agent,opener=urlopen):
        self.agent=agent;self.opener=opener;self.lock=threading.RLock();self.timer=None
        self.state={'status':'idle','sessionId':None,'expiresAt':None,'usageSeconds':None,'finalized':False}
        self.attempts={}
        self.cancel_connect=False

    def status(self):
        with self.lock:
            return {**self.state,'configured':bool(ai_mix.api_key()),'model':MODEL,
                    'maxSeconds':self.state.get('maxSeconds') or SECONDS,'voiceUSDPerMinute':VOICE_RATE_PER_MINUTE,
                    'maxDecisionRequests':self.state.get('maxDecisionRequests') or DECISION_REQUESTS,'recording':False}

    def create(self,sdp,request_id,performer='nova',approved=False,max_seconds=120):
        if approved is not True:raise ValueError('Turn on Talk before connecting.')
        # Older open pages approved only the original two-minute session.
        # A longer session must be explicitly requested by the new Talk control.
        if type(max_seconds) is not int or max_seconds not in (120,SECONDS):
            raise ValueError('Choose a supported voice session duration.')
        decisions=6 if max_seconds==120 else DECISION_REQUESTS
        if not isinstance(sdp,str) or not sdp.startswith('v=0') or len(sdp)>64000:
            raise ValueError('A valid WebRTC offer is required.')
        if not isinstance(request_id,str) or not re.fullmatch(r'[a-zA-Z0-9_-]{8,80}',request_id):
            raise ValueError('A connection request ID is required.')
        profile=getattr(self.agent,'profile_context',lambda:{})()
        if performer not in ('nova','rivet',profile.get('id')):raise ValueError('Select the assigned DJ for the voice session.')
        key=ai_mix.api_key()
        if not key:raise ValueError('OpenAI key is not configured.')
        with self.lock:
            if request_id in self.attempts:raise ValueError('This connection was already attempted. Check its outcome before starting another.')
            if self.state['status'] not in ('idle','closed','error'):raise ValueError('A voice conversation is already active or closing.')
            agent_state=self.agent.snapshot()
            if getattr(self.agent,'external_work_active',lambda:False)():
                raise ValueError('Finish the external music direction before starting Talk.')
            if getattr(self.agent,'operator_allowance_active',lambda:False)():
                raise ValueError('Finish the active text evaluation before starting voice.')
            if ((agent_state['allowance']['remaining'] and not agent_state['allowance'].get('background'))
                    or agent_state['evaluating']):raise ValueError('Finish the active text evaluation before starting voice.')
            self.attempts[request_id]=time.time()
            if len(self.attempts)>50:self.attempts.pop(next(iter(self.attempts)))
            self.state.update(status='connecting',sessionId=None,expiresAt=None,usageSeconds=None,finalized=False,
                              maxSeconds=max_seconds,maxDecisionRequests=decisions)
            self.cancel_connect=False
        a=self.agent.snapshot();context=music_context(self.agent.session.snapshot())
        instructions=f'''You are {profile.get('name') or performer.title()}, the AI DJ in Afterhours. Use a relaxed, concise,
knowledgeable DJ voice. You are an AI persona, not the human operator and not their cloned voice.
Listen naturally and accept interruptions. Delegate any musical direction, track request, change to
the set, a visible gesture or movement for the live video performer, or a question about actual playback to the DJ backend. It owns the current truth and music
decisions. Explicit requests such as "start playing", "play music" or "play CamelPhat" can
prepare and start music through the backend. Delegate the request once; never promise playback
until its deck confirmation arrives. Selecting or discussing music alone does not start it.
"Mix into the next track", "transition to the next track", and "just mix it in" are explicit
operator commands too: delegate them once without asking for another Play confirmation.
"Take back over" returns mixing control to Copilot. "Have the guest DJ take over from the resident DJ"
changes the DJ character: delegate it once, preserving the named people. After the application
confirms a character handoff, continue as the newly assigned AI DJ. The current audio voice stays
the same until Talk reconnects; never claim a cloned voice or a generated entrance.
If the backend reports an engine error, describe that actual fault; do not call it missing consent.
Do not delegate unrelated room conversation. Do not invent catalog availability, audience emotions,
camera observations or completed actions. The DJ backend owns musical scope and clarification.
Relay its question naturally, then delegate the answer once with that context; do not ask
a second Play confirmation. Let it choose a continuing set, finite selection, or a useful
follow-up from the conversation instead of guessing a fixed playlist length.
You can talk while the backend works. Discuss results concisely; avoid reading metadata lists.
Delegate each new operator request once. A backend search or preparation update is not a new
request: do not repeatedly delegate to find better tracks. Get a few suitable tracks queued first;
the backend handles bounded refills. Distinguish found, selected, prepared and actually playing.
Use incoming application context for progress. Wait for it instead of narrating imaginary searches.
This is private operator talkback, not audience speech. The display may show saved or live generated video, without lip sync. Visible actions require an active Director session and appear in future buffered segments; never claim an action happened until the application confirms acceptance.
Talk continues until the operator stops it or the {max_seconds//60}-minute session limit is reached.
Stopping Talk closes the microphone. An explicitly started DJ set can keep choosing music
within this same decision allowance and original deadline; it does not buy more time or calls.
No camera or direct music audio is supplied.'''
        history=[{'type':'message','role':'developer','content':[{'type':'input_text','text':
            'Application facts and operator-owned DJ preferences. Safety and execution constraints above take precedence. Past sets are reference only, not the current direction: '+json.dumps({'djProfile':profile,'direction':a['direction'],'music':context,'pastSets':self.agent.past_context()},ensure_ascii=True)}]}]
        history += [{'type':'message','role':m['role'],'content':[{'type':'input_text' if m['role']=='user' else 'output_text','text':m['text']}]} for m in a['messages'][-8:]]
        body={'session':{'model':MODEL,'instructions':instructions,'input':history,'store':False,
                         'audio':{'output':{'voice':profile.get('voice') or ('meridian' if performer=='nova' else 'marin')}},
                         'delegation':{'type':'client'}},'transport':{'type':'webrtc','sdp':sdp}}
        try:
            with self.opener(Request('https://api.openai.com/v1/live/sessions',data=json.dumps(body).encode(),
                 headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'}),timeout=25) as r:
                raw=r.read(128001)
            if len(raw)>128000:raise ValueError('Voice response exceeded the limit.')
            result=json.loads(raw)
            if not isinstance(result,dict) or not isinstance(result.get('session'),dict) or not isinstance(result.get('transport'),dict):
                raise ValueError('Invalid voice startup response.')
            sid=result['session'].get('id');answer=result['transport'].get('sdp')
            if not isinstance(sid,str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,160}',sid) or not isinstance(answer,str) or not answer.startswith('v=0'):
                raise ValueError('Voice startup response was invalid; its outcome is uncertain.')
        except (HTTPError,URLError,OSError,ValueError) as exc:
            with self.lock:self.state.update(status='error')
            if isinstance(exc,HTTPError):raise ValueError(ai_mix.provider_error(exc)) from None
            raise ValueError('Voice startup was not confirmed. No automatic retry. Check usage before another attempt.') from None
        with self.lock:
            self.state.update(status='active',sessionId=sid,startedAt=time.time(),expiresAt=time.time()+max_seconds)
            if self.cancel_connect:
                self.end(sid)
                raise ValueError('Voice connection was cancelled.')
            self.agent.enable(True,max_seconds,decisions)
            self.timer=threading.Timer(max_seconds,self.end,args=(sid,));self.timer.daemon=True;self.timer.start()
        return {'session':{'id':sid},'transport':{'type':'webrtc','sdp':answer},'expiresAt':self.state['expiresAt'],
                'startedAt':self.state['startedAt'],'maxSeconds':max_seconds}

    def require(self,sid):
        with self.lock:
            if (not isinstance(sid,str) or sid!=self.state['sessionId'] or self.state['status']!='active'
                    or time.time()>=self.state['expiresAt']):
                raise ValueError('This voice conversation is no longer active.')

    def end(self,sid=None):
        with self.lock:
            if sid and sid!=self.state['sessionId']:raise ValueError('Unknown voice session.')
            if self.state['status'] in ('idle','closed'):return self.status()
            if self.state['status']=='connecting':
                self.cancel_connect=True
                return self.status()
            sid=self.state['sessionId'];self.state['status']='closing'
            if self.timer:self.timer.cancel()
            self.agent.disable(preserve_playback=True)
        if sid:
            try:
                with self.opener(Request('https://api.openai.com/v1/live/sessions/'+sid+'/hangup',data=b'',
                    headers={'Authorization':'Bearer '+ai_mix.api_key()}),timeout=10) as r:r.read(1024)
            except (HTTPError,OSError,URLError):
                with self.lock:self.state.update(status='close-unconfirmed')
                return self.status()
        with self.lock:self.state['status']='closed'
        return self.status()

    def receipt(self,sid,seconds,finalized):
        with self.lock:
            if sid!=self.state['sessionId']:raise ValueError('Unknown voice session receipt.')
            if type(seconds) not in (int,float) or not 0<=seconds<=7200:raise ValueError('Invalid voice usage.')
            self.state['usageSeconds']=max(self.state.get('usageSeconds') or 0,seconds)
            if finalized is True:
                self.state.update(status='closed',finalized=True)
                if self.timer:self.timer.cancel()
                self.agent.disable(preserve_playback=True)
            return self.status()
