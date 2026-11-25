"""Explicit, one-shot photographic board/frame jobs through the existing Fal key.

Only a user's Generate action admits a job. A restart or uncertain result never
submits again. Originals stay local; sanitized selected references are sent.
"""
import base64
import copy
import json
import re
import threading
import time
from urllib.request import Request
from urllib.error import HTTPError

MODEL='fal-ai/nano-banana-pro/edit'
ROOT_URL='https://queue.fal.run/'+MODEL
SHOTS=['Front portrait','Three-quarter portrait','Side portrait','FULL-LENGTH FRONT','FULL-LENGTH SIDE','FULL-LENGTH BACK']


def reference_inputs(p,kind):
    """The prompt's image numbers and the upload order use this same selection."""
    photos=list(p['photos']);roles=p.get('referenceRoles') or {};master=roles.get('master')
    ids={a['id'] for a in photos};body=roles.get('body',[])
    if (master is not None and master not in ids) or any(i not in ids or i==master for i in body):
        raise ValueError('A selected master or body reference is missing. Review the reference photos.')
    if master:photos.sort(key=lambda a:a['id']!=master)
    if kind=='frame':photos += [a for a in p['boards'] if a['id']==p.get('approvedBoard')]
    return photos


def board_prompt(p,kind):
    refs=reference_inputs(p,kind);roles=p.get('referenceRoles') or {};master=roles.get('master')
    body=set(roles.get('body',[]));guide=[]
    for i,a in enumerate(refs,1):
        if a['id']==p.get('approvedBoard'):
            guide.append(f'Image {i} is a DERIVED CHARACTER BOARD for wardrobe and continuity only; it never overrides the original master face or body photos. Ignore all equipment, furniture and scenery in that sheet.')
        elif a['id']==master:
            guide.append(f'Image {i} is the MASTER FACE: the sole authority for facial identity, hair, facial hair, apparent age and face accessories.')
        elif a['id'] in body:
            guide.append(f'Image {i} is BODY PROPORTIONS ONLY: shoulder, torso, waist, arm and posture reference. Do not copy its face, clothing, background or camera distortion.')
        else:
            guide.append(f'Image {i} is a supporting photograph of the same person; preserve identity and use the declared master face when present.')
    anchor=(f"Create photographic production references for DJ {p['name']}. "+' '.join(guide)+' '+
        f"Preserve exact identity, apparent age, facial geometry, skin texture, hair and natural body proportions. {p['appearance']} "
        f"Wardrobe: {p['wardrobe']}. Clothing follows the body; do not slim, enlarge or muscularize it. "
        "Natural pores, fine hair, realistic eyes and anatomically credible hands. No illustration, plastic skin, beauty retouching or identity drift. ")
    if kind=='board':
        return anchor+('One polished 3-column by 2-row character board, with six equal photographic panels: '+', '.join(SHOTS)+
            '. Only the person on pure seamless white with neutral soft studio light. Consistent face, clothing and accessories in every panel. '
            'Full-length views keep the complete head, both hands and both feet in frame with generous margins. Small unobtrusive view labels. '
            'No DJ equipment, controller, table, booth, furniture, headphones added as props, scenery or other people. This is reusable character identity, not a performance scene.')
    scene=(f"Scene: {p['booth']}. " if p.get('sceneStyle')=='custom' else
        'Pure seamless white studio. Add one thin floating DJ controller along the bottom, with two jog wheels and reachable mixer controls. '
        'The controller is a separate scene prop: no table, tabletop, stand, cabinet or booth. White space remains below its shallow front lip. ')
    return anchor+scene+('One continuous 16:9 photographic performance scene, waist-up DJ, both hands resting naturally on reachable controls. '
        'Locked eye-level camera, fixed normal lens and body scale, soft neutral light. Keep the entire head, arms, hands and controller inside a safe margin throughout motion. '
        'This is the first frame for video: no collage, labels or text. A supplied character sheet is an identity aid only; output a single person in a single scene.')


class CharacterBoards:
    def __init__(self,profiles,gate):
        self.profiles=profiles;self.gate=gate;self.guard=threading.RLock();self.workers={}
        # Uncertain jobs are never submitted again after a service restart.
        if any(p.get('generation') and p['generation']['status'] in ('submitting','queued','generating') for p in profiles.snapshot()['profiles']):
            with profiles.transaction(True) as db:
                for p in db['profiles']:
                    if p.get('generation') and p['generation']['status'] in ('submitting','queued','generating'):
                        p['generation'].update(status='interrupted',message='The service restarted before this result was saved. Check the existing job in Fal before generating again; you can import its image here.')

    def status(self):
        return {'supported':True,'keyConfigured':self.gate.snapshot()['keyConfigured'],'model':MODEL,
                'estimateUSD':.15,'resolution':'2K','shots':SHOTS,'noAutomaticRetry':True}

    def start(self,data):
        if data.get('consent') is not True:raise ValueError('Confirm the photo upload and one paid image generation.')
        kind=data.get('kind');rid=data.get('requestId')
        if kind not in ('board','frame') or not isinstance(rid,str) or not re.fullmatch(r'[a-zA-Z0-9-]{16,80}',rid):raise ValueError('Invalid generation request.')
        with self.guard,self.gate.lock:
            if not self.gate.key:raise ValueError('Connect your Fal key in Video options first.')
            if self.gate.lease:raise ValueError('Stop live video before preparing a new character reference.')
            key=self.gate.key
            with self.profiles.transaction(True) as db:
                p=next((p for p in db['profiles'] if p['id']==data.get('id')),None)
                if not p:raise ValueError('Select a DJ.')
                if not p['photos']:raise ValueError('Add at least one reference photo first.')
                if kind=='frame' and not p.get('approvedBoard'):raise ValueError('Review and approve a character board before generating the booth frame.')
                reference_inputs(p,kind)
                if p.get('generation') and p['generation']['status'] in ('submitting','queued','generating'):raise ValueError('A character image is already being prepared. Check its result before trying again.')
                attempts=p.setdefault('generationAttempts',[])
                if rid in attempts:raise ValueError('This generation was already submitted. It will not be repeated.')
                if data.get('revision')!=p['revision']:raise ValueError('The profile changed. Reload it before generation.')
                attempts.append(rid);p['generationAttempts']=attempts[-100:]
                p['generation']={'requestId':rid,'kind':kind,'status':'submitting','startedAt':time.time(), 'message':'Sending the selected photos for one image generation.'}
                profile=copy.deepcopy(p)
            thread=threading.Thread(target=self.run,args=(profile,kind,rid,key),daemon=True);self.workers[rid]=thread;thread.start()
        return self.profiles.get(profile['id'])

    def update(self,identifier,rid,**changes):
        with self.profiles.transaction(True) as db:
            p=next(p for p in db['profiles'] if p['id']==identifier)
            if p.get('generation',{}).get('requestId')==rid:p['generation'].update(changes)

    def read(self,url,key=None,body=None,limit=100000):
        from afterhours.performance import checked_url
        checked_url(url,queue=True)
        headers={'Authorization':'Key '+key,'Content-Type':'application/json','X-Fal-No-Retry':'1'}
        request=Request(url,data=json.dumps(body).encode() if body is not None else None,headers=headers)
        with self.gate.opener.open(request,timeout=35) as response:raw=response.read(limit+1)
        if len(raw)>limit:raise ValueError('Provider response was too large.')
        return json.loads(raw)

    def run(self,p,kind,rid,key):
        try:
            inputs=reference_inputs(p,kind)
            images=[]
            for a in inputs:
                content=self.profiles.image_bytes(a['url'])
                mime='image/png' if content.startswith(b'\x89PNG') else 'image/jpeg'
                images.append('data:'+mime+';base64,'+base64.b64encode(content).decode())
            prompt=board_prompt(p,kind)
            # Exactly one submit; subsequent requests only inspect this job.
            submitted=self.read(ROOT_URL,key,{'prompt':prompt,'image_urls':images,'num_images':1,'resolution':'2K',
                'aspect_ratio':'3:2' if kind=='board' else '16:9','output_format':'jpeg','limit_generations':True,'enable_web_search':False})
            request_id=submitted['request_id'];status_url=submitted['status_url'];result_url=submitted['response_url']
            self.update(p['id'],rid,status='queued',providerRequestId=request_id,message='Queued at Fal. This request will not be resubmitted.')
            deadline=time.monotonic()+600
            while time.monotonic()<deadline:
                response=self.read(status_url,key)
                status=response.get('status')
                if status=='COMPLETED':break
                if status not in ('IN_QUEUE','IN_PROGRESS'):raise ValueError('Provider returned an unrecognized job state.')
                self.update(p['id'],rid,status='queued' if status=='IN_QUEUE' else 'generating',message='Waiting at Fal.' if status=='IN_QUEUE' else 'Generating the photographic reference.')
                time.sleep(3)
            else:raise ValueError('The provider did not finish within ten minutes. Check Fal before submitting another job.')
            result=self.read(result_url,key);url=result['images'][0]['url']
            from afterhours.performance import checked_url
            checked_url(url)
            with self.gate.opener.open(Request(url),timeout=30) as response:content=response.read(20_000_001)
            asset=self.profiles.add_image(p['id'],content,'Generated character board' if kind=='board' else 'Generated booth frame',kind,
                receipt={'model':MODEL,'requestId':request_id,'prompt':prompt,'profileRevision':p['revision'],
                         'referenceIds':[a['id'] for a in inputs],'referenceRoles':p.get('referenceRoles'),
                         'assetRole':'character-identity' if kind=='board' else 'performance-scene',
                         'sceneStyle':None if kind=='board' else p.get('sceneStyle','white-floating')})
            self.update(p['id'],rid,status='review',assetId=asset['id'],message='Ready to review. Approve the likeness before using this reference.')
        except HTTPError as exc:
            self.update(p['id'],rid,status='error',message=f'Fal returned HTTP {exc.code}. No automatic retry was started. Check the request in your Fal dashboard before generating again.')
        except Exception as exc:
            self.update(p['id'],rid,status='error',message=str(exc) if isinstance(exc,ValueError) else 'The provider result was not confirmed. No automatic retry was started. Check Fal before generating again.')
        finally:
            with self.guard:self.workers.pop(rid,None)
