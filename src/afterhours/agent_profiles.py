"""Local DJ identities, original photographs and reviewed visual references.

The roster is shared by the music and video processes. File locking and atomic
replacement prevent a generated image receipt from overwriting a profile edit.
"""
import base64
import copy
import fcntl
import hashlib
import io
import json
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from PIL import Image, ImageOps

from afterhours.paths import data_root

ROOT = data_root()
VOICES = {'meridian', 'marin'}
TEXT_LIMITS = {'name':60,'city':80,'bio':600,'appearance':1200,'wardrobe':500,
               'booth':500,'rules':1600,'memory':1600,'testLine':300,'crate':120}


def clean_profile(data):
    if not isinstance(data,dict):raise ValueError('Expected a DJ profile.')
    result={k:str(data.get(k,'')).strip() for k in TEXT_LIMITS}
    for k,limit in TEXT_LIMITS.items():
        if len(result[k])>limit or any(ord(c)<32 and c not in '\n\t' for c in result[k]):
            raise ValueError(f'{k} is too long or contains unsupported characters.')
    if not result['name']:raise ValueError('Give this DJ a name.')
    genres=data.get('genres',[])
    if not isinstance(genres,list) or len(genres)>10 or any(not isinstance(v,str) or not 1<=len(v)<=60 for v in genres):
        raise ValueError('Choose up to ten genres.')
    result['genres']=list(dict.fromkeys(genres))
    for k in ('bpmMin','bpmMax'):
        n=data.get(k,118 if k=='bpmMin' else 132)
        if type(n) not in (int,float) or not 40<=n<=300:raise ValueError('Tempo must be between 40 and 300 BPM.')
        result[k]=n
    if result['bpmMin']>result['bpmMax']:raise ValueError('Minimum tempo must not exceed maximum tempo.')
    for k in ('hype','talkative','humor','callouts'):
        n=data.get(k,3)
        if type(n) is not int or not 0<=n<=10:raise ValueError('Personality settings must be 0–10.')
        result[k]=n
    if data.get('voice','meridian') not in VOICES:raise ValueError('Choose an available voice.')
    if data.get('videoSource','avatar') not in ('avatar','camera','hybrid'):raise ValueError('Unknown video source.')
    result.update(voice=data.get('voice','meridian'),videoSource=data.get('videoSource','avatar'))
    scene=data.get('sceneStyle','white-floating')
    if scene not in ('white-floating','custom'):raise ValueError('Unknown performance scene style.')
    result['sceneStyle']=scene
    return result


def base_profile(identifier,name):
    p=clean_profile({'name':name,'genres':['House'],'bpmMin':118,'bpmMax':132,
        'voice':'marin' if identifier=='rivet' else 'meridian',
        'testLine':"Good to see you. Let's find the right groove.",
        'appearance':'Preserve the exact face, proportions, skin texture and distinguishing features in the photographs.',
        'wardrobe':'Keep the outfit and accessories from the references.',
        'booth':'Pure white studio with a thin floating DJ controller along the bottom, no table or booth.'})
    return {**p,'id':identifier,'revision':1,'createdAt':time.time(),'updatedAt':time.time(),
        'photos':[],'boards':[],'frames':[],'approvedBoard':None,'approvedFrame':None,'generation':None}


class AgentProfiles:
    def __init__(self,root=ROOT):
        self.root=Path(root);self.path=self.root/'.runtime/agents.json';self.guard=threading.RLock()

    def seed(self):
        profiles=[base_profile('nova','Nova'), base_profile('rivet','Rivet')]
        return {'version':1,'assigned':'nova','assignedRevision':1,'default':'nova','profiles':profiles}

    @contextmanager
    def transaction(self,write=False):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.guard, open(self.path.with_suffix('.lock'),'a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            data=json.loads(self.path.read_text()) if self.path.exists() else self.seed()
            data.setdefault('default',data['assigned'])
            yield data
            if write:
                temp=self.path.with_suffix('.tmp');fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
                with os.fdopen(fd,'w') as f:json.dump(data,f,ensure_ascii=False)
                os.replace(temp,self.path)

    def snapshot(self):
        with self.transaction() as data:return copy.deepcopy(data)

    def get(self,identifier):
        p=next((p for p in self.snapshot()['profiles'] if p['id']==identifier),None)
        if not p:raise ValueError('This DJ no longer exists. Refresh Agents.')
        return p

    def context(self):
        data=self.snapshot();p=next(p for p in data['profiles'] if p['id']==data['assigned'])
        return {k:p[k] for k in ['id','name','city','bio','genres','bpmMin','bpmMax','crate','voice','hype','talkative','humor','callouts','rules','memory']}

    def mutate(self,action,data):
        with self.transaction(True) as db:
            if action in ('create','duplicate'):
                if len(db['profiles'])>=24:raise ValueError('The roster holds up to 24 DJs.')
                source=next((p for p in db['profiles'] if p['id']==data.get('id')),None) if action=='duplicate' else None
                if action=='duplicate' and not source:raise ValueError('Select a DJ to duplicate.')
                p=copy.deepcopy(source) if source else base_profile('','New DJ')
                p.update(id=uuid.uuid4().hex,name=p['name'][:55]+' copy' if source else 'New DJ',revision=1,createdAt=time.time(),updatedAt=time.time(),generation=None,generationAttempts=[])
                db['profiles'].append(p);return p
            p=next((p for p in db['profiles'] if p['id']==data.get('id')),None)
            if not p:raise ValueError('Select a DJ first.')
            if action=='save':
                if data.get('revision')!=p['revision']:raise ValueError('This profile changed in another window. Reload the saved profile before saving.')
                p.update(clean_profile(data.get('profile')))
            elif action=='assign':
                db['assigned']=p['id'];db['assignedRevision']=p['revision']
            elif action=='default':
                if not p.get('approvedFrame'):raise ValueError('Choose a performance image before setting the default DJ.')
                db['default']=p['id']
            elif action=='approve':
                kind=data.get('kind');key={'board':'boards','frame':'frames'}.get(kind)
                if not key or not any(a['id']==data.get('assetId') for a in p[key]):raise ValueError('Select an existing generated image.')
                p['approvedBoard' if kind=='board' else 'approvedFrame']=data['assetId']
            elif action=='remove-photo':
                p['photos']=[a for a in p['photos'] if a['id']!=data.get('photoId')]
                if 'referenceRoles' in p:
                    roles=p['referenceRoles'];removed=data.get('photoId')
                    if roles.get('master')==removed:roles.update(master=None,body=[])
                    else:roles['body']=[i for i in roles.get('body',[]) if i!=removed]
            elif action=='reference-roles':
                if data.get('revision')!=p['revision']:raise ValueError('This profile changed. Reload before changing reference roles.')
                master=data.get('masterPhotoId');body=data.get('bodyPhotoIds',[])
                ids={a['id'] for a in p['photos']}
                if (not isinstance(master,str) or master not in ids or not isinstance(body,list)
                    or len(body)>3 or any(not isinstance(i,str) or i not in ids or i==master for i in body)
                    or len(set(body))!=len(body)):
                    raise ValueError('Choose one existing master face photo and up to three different body photos.')
                p['referenceRoles']={'master':master,'body':body}
            else:raise ValueError('Unknown profile action.')
            # Roster selection does not change this identity or invalidate its
            # reviewed images. Actual profile/asset edits still advance revision.
            if action not in ('assign','default'):
                p.update(revision=p['revision']+1,updatedAt=time.time())
            return copy.deepcopy(p)

    def upload(self,identifier,encoded,name,role='photo'):
        if role not in ('photo','board','frame'):raise ValueError('Unknown image role.')
        if not isinstance(encoded,str) or len(encoded)>8_000_000:raise ValueError('Choose a photo under 5 MB.')
        try:content=base64.b64decode(encoded,validate=True)
        except Exception as exc:raise ValueError('The photo could not be read.') from exc
        return self.add_image(identifier,content,name,role,original=True)

    def add_image(self,identifier,content,name,role='photo',original=False,receipt=None):
        if not 0<len(content)<=20_000_000:raise ValueError('Image exceeds the size limit.')
        try:
            im=Image.open(io.BytesIO(content))
            # iPhone JPEGs can carry an MPO gain-map/auxiliary image. Decode
            # only the primary photograph; retain the source bytes privately.
            if im.format not in ('JPEG','MPO','PNG','WEBP') or im.width*im.height>25_000_000:raise ValueError()
            if im.width<128 or im.height<128:raise ValueError()
            im.load();im=ImageOps.exif_transpose(im).convert('RGB');im.thumbnail((2400,2400))
        except Exception as exc:raise ValueError('Use a valid JPG, PNG or WebP, at least 128 px and under 25 megapixels.') from exc
        with self.transaction(True) as db:
            p=next((p for p in db['profiles'] if p['id']==identifier),None)
            if not p:raise ValueError('Select a DJ first.')
            if role=='photo' and len(p['photos'])>=4:raise ValueError('Use up to four reference photos per DJ.')
            asset_id=uuid.uuid4().hex;folder=self.root/'web/agent-media';folder.mkdir(exist_ok=True)
            im.save(folder/(asset_id+'.jpg'),quality=95)
            item={'id':asset_id,'url':f'/agent-media/{asset_id}.jpg','name':str(name)[:100], 'createdAt':time.time(),
                  'source':'Uploaded locally' if original else 'Fal generated','sha256':hashlib.sha256(content).hexdigest()}
            if original:
                source=self.root/'.runtime/agent-originals';source.mkdir(exist_ok=True)
                original_path=source/asset_id;original_path.write_bytes(content);original_path.chmod(0o600)
                item['original']=asset_id
            if receipt:item['receipt']=receipt
            p[{'photo':'photos','board':'boards','frame':'frames'}[role]].append(item)
            p.update(revision=p['revision']+1,updatedAt=time.time());return copy.deepcopy(item)

    def image_bytes(self,url):
        if not isinstance(url,str) or not re.fullmatch(r'/agent-media/[a-f0-9]{32}\.jpg',url):raise ValueError('Unknown local reference.')
        return (self.root/'web'/url.lstrip('/')).read_bytes()

    def mentioned(self,text):
        from afterhours.agent_mentions import resolve_mentions
        return resolve_mentions(text,self.snapshot()['profiles'])

    def command(self,text):
        from afterhours.agent_mentions import roster_command
        db=self.snapshot()
        return roster_command(text,db['profiles'],db['assigned'])

    def visual(self,identifier):
        p=self.get(identifier)
        frame=next((a for a in p['frames'] if a['id']==p['approvedFrame']),None)
        scene=p['booth']
        # Existing approved frames retain their scene until a new frame is selected.
        # New frame receipts carry their scene separately from character identity.
        if frame and (frame.get('receipt') or {}).get('sceneStyle')=='white-floating':
            scene='Pure white studio with one thin floating DJ controller along the bottom. No table, tabletop, stand, cabinet or booth.'
        continuity=('Keep the reference face, natural body proportions, wardrobe, fixed eye-level camera and body scale unchanged throughout. '
                    'Keep the head, arms, hands and complete controller inside the frame. No gradual body widening, slimming, cuts, dialogue or text overlays. ')
        return {'id':p['id'],'name':p['name'],'poster':frame['url'] if frame else None,
                'prompt':(f"{p['name']}, the DJ in the reference. "+continuity+f"Scene: {scene} {p['appearance']} {p['wardrobe']} Performance style: energy {p['hype']}/10, warmth and humor {p['humor']}/10, audience acknowledgement {p['callouts']}/10. Express these as restrained visible behavior, not internal emotion or speech. Preserve realistic hands.")[:1800]}
