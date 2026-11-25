"""Bounded continuous H3 continuation queue; no provider calls until explicitly enabled."""
import copy
import json
import math
import re
from pathlib import Path
import threading
import time
import uuid
from urllib.parse import urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler

from afterhours.paths import data_root
from afterhours.web_runtime import web_file

ENDPOINT = 'minimax/h3-max/extend-video'
ROOT = data_root()
OVERLAP = 1.625
SEED_PATH = '/media/rivet-demo.mp4'
CHARACTERS = {
    'rivet': {'name': 'Rivet', 'poster': '/assets/performer.png', 'clip': SEED_PATH, 'duration': 8, 'loopBeats': 16},
    'nova': {'name': 'Nova', 'poster': '/assets/nova-placeholder.jpg', 'clip': '/media/nova-demo.mp4', 'duration': 8, 'loopBeats': 24},
}
DEFAULT_PROMPT = ('Continue the supplied DJ performance in the same room. Preserve the performer, '
                  'lighting and camera position. Keep movements restrained and continuous. No cuts or dialogue.')


def saved_loop(profile):
    """Local, identity-bound media only. Selecting a DJ never generates video."""
    registry=ROOT/'.runtime/performer-loops.json'
    try:
        if registry.stat().st_size>128_000:return None
        data=json.loads(registry.read_text())
        if data.get('version')!=1:return None
        item=data['profiles'][profile['id']]
        if item.get('referencePoster')!=profile['poster']:return None
        clip=item['clip'];duration=item['duration'];beats=item.get('loopBeats',16)
        if not isinstance(clip,str) or not re.fullmatch(r'/media/character-loops/[a-z0-9_-]{1,96}\.mp4',clip):return None
        if type(duration) not in (int,float) or not math.isfinite(duration) or not 0<duration<=30:return None
        if type(beats) is not int or not 8<=beats<=64:return None
        media=ROOT/'web/media/character-loops';path=ROOT/'web'/clip.lstrip('/')
        if not path.resolve().is_relative_to(media.resolve()) or not path.is_file():return None
        return {'clip':clip,'duration':duration,'loopBeats':beats}
    except (OSError,ValueError,KeyError,TypeError,AttributeError):
        return None


def checked_url(value, queue=False):
    p = urlparse(value)
    allowed = p.hostname == 'queue.fal.run' if queue else (p.hostname or '').endswith('.fal.media')
    if p.scheme != 'https' or not allowed or p.username or p.password or p.port not in (None,443):
        raise ValueError('Unexpected provider URL')
    return value


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError('Provider redirects are not followed')


class FalContinuation:
    def __init__(self, key, output_dir, seed_url, poll_seconds=2):
        self.key=key; self.output_dir=Path(output_dir); self.seed_url=checked_url(seed_url)
        self.poll_seconds=poll_seconds; self.opener=build_opener(NoRedirect())

    def json(self, url, method='GET', data=None):
        checked_url(url, queue=True)
        req=Request(url, data=json.dumps(data).encode() if data is not None else None,
                    method=method, headers={'Authorization':'Key '+self.key,'Content-Type':'application/json',
                    'X-Fal-No-Retry':'1','X-Fal-Request-Start-Timeout':'120','x-app-fal-disable-fallback':'true'})
        with self.opener.open(req,timeout=20) as response:
            return json.loads(response.read(1_000_000))

    def generate(self, previous_url, prompt, cancel, on_job):
        # Submission is never retried: a timeout may already have created a paid job.
        job=self.json('https://queue.fal.run/'+ENDPOINT+'?fal_max_queue_length=1','POST',{
            'video_url':checked_url(previous_url or self.seed_url),'prompt':prompt,
            'duration':8,'resolution':'480P','aspect_ratio':'auto','output':'continuation',
            'enable_prompt_expansion':False,'enable_safety_checker':True})
        for key in ('status_url','response_url','cancel_url'): checked_url(job[key],queue=True)
        on_job(job['request_id'])
        deadline=time.monotonic()+600
        while True:
            if cancel.is_set() or time.monotonic()>deadline:
                try: self.json(job['cancel_url'],'PUT')
                except Exception: pass
                raise RuntimeError('Generation stopped; an in-flight job may still be billable.')
            status=self.json(job['status_url'])
            if status.get('status')=='COMPLETED': break
            cancel.wait(self.poll_seconds)
        if status.get('error'): raise RuntimeError('Provider reported a failed generation.')
        result=self.json(job['response_url'])
        duration=result.get('duration')
        if type(duration) not in (int,float) or not math.isfinite(duration) or not OVERLAP < duration <= 20:
            raise ValueError('Invalid continuation duration')
        url=checked_url(result['video']['url'])
        if cancel.is_set(): raise RuntimeError('Generation stopped before download.')
        self.output_dir.mkdir(parents=True,exist_ok=True)
        name=uuid.uuid4().hex+'.mp4'; target=self.output_dir/name; temporary=target.with_suffix('.part')
        try:
            with self.opener.open(Request(url),timeout=30) as response, temporary.open('wb') as output:
                size=0
                while chunk:=response.read(128*1024):
                    if cancel.is_set(): raise RuntimeError('Generation stopped during download.')
                    size+=len(chunk)
                    if size>50_000_000: raise ValueError('Video exceeds 50 MB limit')
                    output.write(chunk)
            with temporary.open('rb') as file: header=file.read(32)
            if b'ftyp' not in header: raise ValueError('Provider result is not an MP4')
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        return {'url':'/performance-media/'+name,'provider_url':url,'duration':duration-OVERLAP,'inPoint':OVERLAP}


class Performance:
    def __init__(self, provider=None, generation_limit=0, now=time.time):
        self.provider=provider; self.limit=generation_limit; self.now=now
        self.lock=threading.RLock(); self.cancel=threading.Event(); self.worker=None
        self.submissions=0; self.displays={}; self.characters=copy.deepcopy(CHARACTERS); self.reset()

    def profile_character(self, profile):
        # A changed approved reference invalidates its registered loop. Do not
        # retain the previous DJ's clip, or an earlier selection's cached entry.
        selected={'clip':'','duration':0,'loopBeats':16}
        selected.update(saved_loop(profile) or {})
        selected.update(name=profile['name'],poster=profile['poster'])
        self.characters[profile['id']]=selected
        return selected

    def activate_saved_loop(self):
        selected=self.characters[self.state['character']]
        if not selected['clip']:return
        self.state.update(mode='playing',elapsed=0,startedAt=self.now(),generation='rehearsal',
            source='existing clip loop',message='Saved DJ loop repeating · no live generation.',
            segments=[{'id':'seed','url':selected['clip'],'start':0,'duration':selected['duration'],'inPoint':0,'kind':'existing clip'}])

    def reset(self):
        self.state={'session':uuid.uuid4().hex,'revision':0,'mode':'paused','elapsed':0,'startedAt':None,
                    'generation':'idle','message':'Ready to rehearse with the existing eight-second clip.',
                    'look':'warm','fit':'contain','caption':'','prompt':DEFAULT_PROMPT,'beatSync':False,'segments':[],
                    'generated':0,'lastJob':None,'lastGenerationSeconds':None,'source':'existing clip'}
        self.state.update(character='rivet', performer='Rivet', poster=CHARACTERS['rivet']['poster'], clipBeats=16, clipDuration=8)
        self.previous_url=None

    def elapsed(self):
        return self.state['elapsed']+(self.now()-self.state['startedAt'] if self.state['mode']=='playing' else 0)

    def snapshot(self):
        with self.lock:
            result=copy.deepcopy(self.state); elapsed=self.elapsed()
            result.update(serverTime=self.now(),timeline=elapsed,submissions=self.submissions,
                generationLimit=self.limit,providerReady=bool(self.provider and self.limit>self.submissions),
                bufferedSeconds=max(0,max((s['start']+s['duration'] for s in result['segments']),default=0)-elapsed),
                displays=[copy.deepcopy(v) for v in self.displays.values() if self.now()-v['seen']<6])
            result['characters'] = [{'id':key,'name':value['name'],
                'available':bool(value['clip']) and web_file(value['clip'], ROOT/'web') is not None}
                for key,value in self.characters.items()]
            return result

    def assign_live_lead(self, profile):
        """Metadata/fallback update after an admitted cast direction is acknowledged.

        The caller validates the approved profile against the session's pinned cast.
        This does not restart or replace the live WebRTC stream, or touch music.
        """
        with self.lock:
            if self.worker and self.worker.is_alive():raise ValueError('Stop generation before changing the performer.')
            existing=self.profile_character(profile)
            self.state.update(character=profile['id'],performer=profile['name'],poster=profile['poster'],
                prompt=profile['prompt'],clipBeats=existing['loopBeats'],clipDuration=existing['duration'],
                mode='paused',elapsed=0,startedAt=None,segments=[],source='approved reference',
                message='New lead assigned; the live scene follows Director’s generation buffer.')
            self.activate_saved_loop()
            self.state['revision']+=1
            return self.snapshot()

    def command(self, data):
        with self.lock:
            action=data.get('action')
            if action=='profile':
                from afterhours.agent_profiles import AgentProfiles
                if 'profileId' in data:
                    if 'profile' in data:raise ValueError('Choose one profile identity.')
                    # Resolve approved visual data in the audience process. A
                    # music-service upgrade must not compare two independently
                    # generated prompt strings and reject an approved frame.
                    p=AgentProfiles().visual(data['profileId'])
                else:
                    p=data.get('profile',{})
                    verified=AgentProfiles().visual(p.get('id'))
                    if p!=verified:raise ValueError('The approved profile changed. Select it again.')
                if not p['poster']:raise ValueError('Approve a booth frame before assigning this profile.')
                if self.worker and self.worker.is_alive():raise ValueError('Stop generation before changing the performer.')
                current=self.profile_character(p)
                settings={key:self.state[key] for key in ('look','fit','caption','beatSync')}
                self.reset();self.state.update(settings,character=p['id'],performer=p['name'],poster=p['poster'],
                    prompt=p['prompt'],clipBeats=current['loopBeats'],clipDuration=current['duration'],
                    mode='paused',generation='idle',source='approved reference',message='Approved DJ reference. Live video starts explicitly in Program.')
                self.activate_saved_loop()
                self.state['revision']+=1
                return self.snapshot()
            if action=='character':
                character=data.get('character')
                if character not in self.characters: raise ValueError('Unknown performer')
                if self.worker and self.worker.is_alive(): raise ValueError('Stop generation before changing the performer.')
                selected=self.characters[character]
                if not selected['clip'] or web_file(selected['clip'], ROOT/'web') is None:
                    raise ValueError('This performer has no verified video sample yet.')
                settings={key:self.state[key] for key in ('look','fit','caption','beatSync')}
                self.reset();self.state.update(settings)
                self.state.update(character=character, performer=selected['name'], poster=selected['poster'],
                    clipBeats=selected['loopBeats'],clipDuration=selected['duration'],
                    prompt=DEFAULT_PROMPT)
                self.state['revision']+=1
                return self.command({'action':'rehearse'})
            if action in ('rehearse','generate'):
                selected=self.characters[self.state['character']]
                if not selected['clip'] or web_file(selected['clip'], ROOT/'web') is None:
                    raise ValueError('This DJ has an approved still reference. Turn on Live video in Program to animate it.')
                if self.worker and self.worker.is_alive(): raise ValueError('Stop the current generation before starting again.')
                if action=='generate' and not (self.provider and self.submissions<self.limit):
                    raise ValueError('Hosted generation is locked. Configure an API key and an approved generation limit on the server first.')
                if action=='generate' and self.state['character']!='rivet':
                    raise ValueError('Use the configured sample slot for the supplied continuation seed.')
                settings={key:self.state[key] for key in ('look','fit','caption','prompt','beatSync','character','performer','poster','clipBeats','clipDuration')}
                self.reset(); self.state.update(settings); self.cancel=threading.Event()
                self.state.update(mode='playing',startedAt=self.now(),generation='rehearsal' if action=='rehearse' else 'generating',
                    source='existing clip loop' if action=='rehearse' else 'H3 continuations',
                    message='Existing clip repeating · no new generation.' if action=='rehearse' else 'Preparing a continuation; the existing clip covers the wait.')
                selected=self.characters[self.state['character']]
                self.state['segments']=[{'id':'seed','url':selected['clip'],'start':0,'duration':selected['duration'],'inPoint':0,'kind':'existing clip'}]
                if action=='generate':
                    self.worker=threading.Thread(target=self.produce,daemon=True);self.worker.start()
            elif action in ('pause','play','blackout'):
                elapsed=self.elapsed(); self.state.update(elapsed=elapsed,mode={'play':'playing','pause':'paused','blackout':'blackout'}[action],startedAt=self.now() if action=='play' else None)
            elif action=='stop-generation':
                self.cancel.set();self.state.update(generation='stopped',message='No more submissions. Any in-flight job may still be billable. Display playback is unchanged.')
            elif action=='settings':
                if data.get('look') not in ('warm','late','mono') or data.get('fit') not in ('contain','cover'): raise ValueError('Invalid display settings')
                caption=data.get('caption','');prompt=data.get('prompt',DEFAULT_PROMPT)
                if not isinstance(caption,str) or len(caption)>160 or not isinstance(prompt,str) or not 1<=len(prompt)<=2000: raise ValueError('Invalid caption or direction')
                beat_sync=data.get('beatSync',self.state['beatSync'])
                if type(beat_sync) is not bool: raise ValueError('Music timing needs a boolean setting')
                self.state.update(look=data['look'],fit=data['fit'],caption=caption,prompt=prompt,beatSync=beat_sync)
            else: raise ValueError('Unknown performance command')
            self.state['revision']+=1
            return self.snapshot()

    def heartbeat(self,data):
        with self.lock:
            identifier=data.get('id')
            if not isinstance(identifier,str) or not 8<=len(identifier)<=80: raise ValueError('Invalid display ID')
            self.displays={k:v for k,v in self.displays.items() if self.now()-v['seen']<10}
            if len(self.displays)>=8 and identifier not in self.displays: raise ValueError('Too many displays')
            self.displays[identifier]={'seen':self.now(),'id':identifier,'state':str(data.get('state','unknown'))[:100],
                                      'segment':str(data.get('segment',''))[:80]}

    def produce(self):
        while not self.cancel.is_set():
            with self.lock:
                if self.submissions>=self.limit:
                    self.state.update(generation='limit',message='Approved submission limit reached. Playback continues with buffered footage or its last clip.');return
                ahead=self.snapshot()['bufferedSeconds']
                if self.state['mode']!='playing' or ahead>24:
                    wait=True
                else:
                    wait=False; self.submissions+=1; prompt=self.state['prompt']; previous=self.previous_url
                    self.state.update(generation='generating',message='Generating the next eight seconds.');started=self.now()
            if wait: self.cancel.wait(.5);continue
            try:
                def job(identifier):
                    with self.lock:self.state['lastJob']=identifier
                segment=self.provider.generate(previous,prompt,self.cancel,job)
                with self.lock:
                    if self.cancel.is_set(): return
                    self.previous_url=segment.pop('provider_url')
                    end=max(s['start']+s['duration'] for s in self.state['segments'])
                    segment.update(id=uuid.uuid4().hex,start=max(end,self.elapsed()+.5),kind='generated continuation')
                    self.state['segments'].append(segment)
                    self.state['segments']=self.state['segments'][-32:]
                    self.state['generated']+=1;self.state['lastGenerationSeconds']=round(self.now()-started,2)
                    self.state['message']='Continuation ready. Waiting for the playback buffer to need more.'
                    self.state['revision']+=1
            except Exception:
                with self.lock:
                    if not self.cancel.is_set():self.state.update(generation='error',message='Generation failed or timed out. No automatic resubmission; inspect the provider request before retrying.')
                return
