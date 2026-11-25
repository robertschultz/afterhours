"""Single-session, local-only admission and signaling for Fal H3 Max Director.

No paid calls by default. The deadline revokes proxy/heartbeat access; the browser
also sends stop and closes WebRTC. This is not a provider-enforced dollar cap.
"""
import copy
import json
import secrets
import threading
import time
import uuid
from pathlib import Path
from urllib.request import Request, build_opener
from afterhours.performance import NoRedirect, checked_url
from afterhours.director_account import check_account

APP = 'minimax/h3-max/director'
REFERENCE_APP = 'minimax/h3-max/reference-director'
TARGETS = {
    'https://wma.fal.run/ice': 'ice',
    'https://fal.run/minimax/h3-max/director/ice': 'ice-fallback',
    'https://fal.run/minimax/h3-max/reference-director/ice': 'ice-fallback',
    'https://wma.fal.run/session': 'session',
    'https://wma.fal.run/session/heartbeat': 'heartbeat',
}


class DirectorGate:
    def __init__(self, key=None, approved_sessions=0, seconds=120, now=time.time, ledger=None):
        self.key=key; self.limit=approved_sessions; self.seconds=seconds; self.now=now
        self.lock=threading.RLock(); self.used=0; self.lease=None; self.deadline=0
        self.submitted=False; self.session_id=None; self.reason='Not started'
        self.opener=build_opener(NoRedirect())
        self.ledger=Path(ledger) if ledger else None
        if self.ledger and self.ledger.exists():
            # Fail closed on a damaged ledger. Restarting never renews an approval.
            try:self.used=max(0,int(json.loads(self.ledger.read_text())['used']))
            except Exception:self.used=self.limit
        self.references={}; self.run=None; self.capture_token=None
        self.stage_references={};self.stage_reference=None
        self.reference_busy=False
        self.model=APP; self.cast=[]; self.lead=None
        self.study_program=None; self.study_seen=0

    def music(self, lease, program):
        with self.lock:
            self.expire()
            if not self.lease or not secrets.compare_digest(lease or '',self.lease):raise ValueError('Expired Director owner')
            # The original study is the only local audio this isolated video service owns.
            position=program.get('positionSeconds')
            if type(position) not in (int,float) or not 0<=position<=40:raise ValueError('Invalid study position')
            self.study_seen=self.now()
            self.study_program={'connected':True,'playing':position<30,'current':'original-study','positionSeconds':position,
                'durationSeconds':30,'key':'9B','clock':{'bpm':124,'sampledAtUnixMs':self.now()*1000},
                'library':[{'id':'original-study','title':'Original house study','artist':'Afterhours','camelot':'9B'}]}

    def program(self):
        with self.lock:
            self.expire()
            return self.study_program if self.lease and self.now()-self.study_seen<3 else None

    def set_key(self, key):
        with self.lock:
            if self.lease:raise ValueError('Stop Director before changing credentials')
            if not isinstance(key,str) or not 8<=len(key.strip())<=512 or any(ord(c)<33 or ord(c)>126 for c in key.strip()):
                raise ValueError('Enter a valid Fal API key')
            self.key=key.strip() # Memory only. Never included in a snapshot or ledger.

    def check_account(self):
        with self.lock:
            self.expire()
            if self.lease:raise ValueError('Stop Director before checking the account')
            return check_account(self.key,self.opener)

    def prepare_reference(self, character, content):
        """Explicitly consented upload of one fixed application poster, never an arbitrary file."""
        with self.lock:
            if not self.key or self.lease or self.reference_busy:raise ValueError('Reference setup is unavailable')
            if character in self.references:return
            self.reference_busy=True
        try:
            request=Request('https://rest.fal.ai/storage/upload/initiate?storage_type=fal-cdn-v3',
                data=json.dumps({'file_name':character+'-director-reference.jpg','content_type':'image/jpeg'}).encode(),
                method='POST',headers={'Authorization':'Key '+self.key,'Content-Type':'application/json',
                    'X-Fal-Object-Lifecycle':json.dumps({'expiration_duration_seconds':86400})})
            with self.opener.open(request,timeout=20) as response:result=json.loads(response.read(20_000))
            # Both URLs must be on Fal's CDN; never forward the API key to signed storage.
            upload=checked_url(result['upload_url']);reference=checked_url(result['file_url'])
            with self.opener.open(Request(upload,data=content,method='PUT',headers={'Content-Type':'image/jpeg'}),timeout=30) as response:
                if not 200<=response.status<300:raise ValueError('Reference upload failed')
            with self.lock:self.references[character]=reference
        finally:
            with self.lock:self.reference_busy=False

    def expire(self):
        if self.lease and self.now()>=self.deadline:
            self.lease=None; self.reason='Session time limit reached'

    def snapshot(self):
        with self.lock:
            self.expire()
            return {'ready':bool(self.key and self.used<self.limit and not self.lease),
                    'liveReady':bool(self.key and not self.lease),'liveSupported':True,
                    'keyConfigured':bool(self.key),'approvedSessions':self.limit,'used':self.used,
                    'active':bool(self.lease),'deadline':self.deadline if self.lease else None,
                    'maxSeconds':self.seconds,'reason':self.reason,'sampleSeconds':30,'resolution':'768p',
                    'minimumUSD':4.80,'generatedLimitSeconds':45,'references':list(self.references),
                    'referenceBusy':self.reference_busy,'runId':self.run,
                    'referenceMode':self.model==REFERENCE_APP,'castIds':[p['id'] for p in self.cast],
                    'lead':self.lead,'stageReference':copy.deepcopy(self.stage_reference) if self.lease else None}

    def start(self, character='rivet', *, mode='sample', consent=False, minutes=None, cast=None,stage_reference=None):
        with self.lock:
            state=self.snapshot()
            if mode not in ('sample','live'):raise ValueError('Unknown video mode')
            live=mode=='live'
            if live:
                # Every live admission comes from the user's explicit On action.
                # There is no background renewal, reconnect or standing allowance.
                if consent is not True or type(minutes) is not int or minutes not in (2,5,15,30):raise ValueError('Confirm a bounded live session')
                if not state['liveReady']:raise ValueError('Live video is unavailable or already owned')
            elif not state['ready']: raise ValueError('Director is locked or another session owns the stream.')
            reference_key=character
            if stage_reference is not None:
                if (cast or not isinstance(stage_reference,dict) or stage_reference.get('character')!=character
                        or self.stage_references.get(stage_reference.get('referenceKey'))!=stage_reference):
                    raise ValueError('Prepare the exact reviewed live stage before starting video.')
                reference_key=stage_reference['referenceKey']
            if cast:
                if not live or not 2<=len(cast)<=4 or character not in [p['id'] for p in cast]:raise ValueError('Invalid live cast')
                if any(self.references.get(p.get('referenceKey'))!=p.get('url') for p in cast):raise ValueError('Prepare the approved cast references first')
            elif reference_key not in self.references:raise ValueError('Prepare the selected performer reference first')
            # Count admission even if negotiation fails; no automatic paid replacement.
            if self.ledger:
                self.ledger.parent.mkdir(parents=True,exist_ok=True)
                temporary=self.ledger.with_suffix('.tmp')
                temporary.write_text(json.dumps({'used':self.used+1,'admittedAt':self.now(),'mode':mode,'minutes':minutes if live else None}))
                temporary.replace(self.ledger)
            self.used+=1; self.lease=secrets.token_urlsafe(32); self.deadline=self.now()+(minutes*60+120 if live else self.seconds)
            self.run=uuid.uuid4().hex; self.capture_token=secrets.token_urlsafe(32)
            self.submitted=False; self.session_id=None; self.reason='Session admitted'
            self.model=REFERENCE_APP if cast else APP;self.cast=cast or [];self.lead=character
            self.stage_reference=copy.deepcopy(stage_reference)
            return {'lease':self.lease,'deadline':self.deadline,'imageUrl':None if cast else self.references[reference_key],
                    'model':self.model,'referenceImages':[{'name':p['name'],'url':p['url']} for p in self.cast],
                    'castProfiles':[{'id':p['id'],'name':p['name'],'prompt':p['prompt']} for p in self.cast],
                    'runId':self.run,'captureToken':self.capture_token,'character':character,
                    'mode':mode,'sampleSeconds':None if live else 30,'resolution':'768p',
                    'generatedLimitSeconds':minutes*60-15 if live else 45,
                    'budgetMinutes':minutes if live else None,'stageReference':copy.deepcopy(stage_reference)}

    def authorize_capture(self, run, token):
        return bool(self.run and run==self.run and secrets.compare_digest(token or '',self.capture_token or ''))

    def stop(self):
        with self.lock:self.lease=None; self.reason='Stopped by operator'

    def authorize(self, lease, target, data):
        with self.lock:
            self.expire()
            if not self.lease or not secrets.compare_digest(lease or '',self.lease):raise ValueError('Director session expired or unauthorized')
            kind=TARGETS.get(target)
            if not kind:raise ValueError('Endpoint is not allowed')
            if kind=='ice':
                if data!={'app_id':self.model}:raise ValueError('Invalid ICE request')
            elif kind=='ice-fallback':
                if data!={} or target!=f'https://fal.run/{self.model}/ice':raise ValueError('Invalid ICE fallback')
            elif kind=='session':
                if self.submitted:raise ValueError('Session negotiation was already submitted; no replay allowed')
                if set(data)!={'app_id','sdp','type'} or data['app_id']!=self.model or data['type']!='offer' or not isinstance(data['sdp'],str) or not 10<len(data['sdp'])<150_000:raise ValueError('Invalid session offer')
                self.submitted=True
            elif kind=='heartbeat':
                if not self.session_id or data!={'session_id':self.session_id}:raise ValueError('Wrong session heartbeat')
            return kind

    def upstream(self,target,data):
        request=Request(target,data=json.dumps(data).encode(),method='POST',headers={
            'Authorization':'Key '+self.key,'Content-Type':'application/json',
            'X-Fal-No-Retry':'1','x-app-fal-disable-fallback':'true'})
        return self.opener.open(request,timeout=max(1,min(60,self.deadline-self.now())))

    def bind(self,lease,body):
        with self.lock:
            self.expire()
            if self.lease!=lease:return
            data=json.loads(body)
            if not isinstance(data.get('session_id'),str):raise ValueError('No session identifier returned')
            self.session_id=data['session_id']
