"""Reviewed live opening frames, kept in the local data root, never inferred.

This module only validates existing assets. It cannot generate, crop, recolor or
silently substitute a character image. Uploads require the existing explicit
reference consent; live admission remains owned by DirectorGate.
"""
import hashlib
import io
import json
import re
from pathlib import Path

from PIL import Image


BACKGROUNDS={'light':'#FFFFFF','dark':'#18181B'}
ASPECTS={'1:1':(1,1),'16:9':(16,9),'9:16':(9,16)}
FIELDS={'profileRevision','sourceSha256','poster','width','height','aspectRatio','background','scene','reviewed'}


class DirectorStages:
    def __init__(self,profiles,root):
        self.profiles=profiles;self.root=Path(root)
        self.path=self.root/'.runtime/director-stage-references.json'

    def resolve(self,character,appearance):
        if appearance not in BACKGROUNDS:raise ValueError('Choose a light or dark live stage.')
        profile=self.profiles.get(character)
        missing=f'Prepare and review a {appearance} live opening frame for this DJ before starting video.'
        try:
            if self.path.stat().st_size>128_000:raise ValueError('The live stage manifest is too large.')
            manifest=json.loads(self.path.read_text())
            if not isinstance(manifest,dict) or type(manifest.get('version')) is not int or manifest['version']!=1:
                raise ValueError('The live stage manifest is invalid.')
            item=manifest.get('profiles',{}).get(character,{}).get(appearance)
        except FileNotFoundError:raise ValueError(missing) from None
        except (OSError,TypeError,AttributeError,json.JSONDecodeError):raise ValueError('The live stage manifest could not be read. Review its local configuration.') from None
        if not isinstance(item,dict) or item.get('reviewed') is not True:raise ValueError(missing)
        if set(item)!=FIELDS:raise ValueError('The reviewed live frame metadata is invalid. Review it again.')
        revision=item['profileRevision'];width=item['width'];height=item['height']
        if type(revision) is not int or revision<1 or revision!=profile.get('revision'):
            raise ValueError('This DJ changed after the live frame was reviewed. Review the current profile and frame again.')
        if (type(width) is not int or type(height) is not int or not 64<=width<=8192 or not 64<=height<=8192
                or width*height>25_000_000):raise ValueError('The reviewed live frame dimensions are invalid.')
        aspect=item['aspectRatio']
        if not isinstance(aspect,str) or aspect not in ASPECTS:raise ValueError('The live frame needs a supported 1:1, 16:9 or 9:16 composition.')
        horizontal,vertical=ASPECTS[aspect]
        # Only reviewed compositions with the exact requested ratio qualify.
        # Never stretch/crop the original to make a near-match pass.
        if width*vertical!=height*horizontal:
            raise ValueError('The live frame does not match its stream aspect ratio. Prepare a matching composition without cropping the DJ.')
        if item['background']!=BACKGROUNDS[appearance]:raise ValueError('The reviewed frame background does not match this live appearance.')
        scene=item['scene']
        if (not isinstance(scene,str) or not 1<=len(scene.strip())<=1000
                or any(ord(c)<32 and c not in '\n\t' for c in scene)):
            raise ValueError('The live stage scene must contain 1–1,000 characters.')
        digest=item['sourceSha256'];poster=item['poster']
        if not isinstance(digest,str) or not re.fullmatch(r'[a-f0-9]{64}',digest):raise ValueError('The reviewed frame checksum is invalid.')
        if poster!='/director-stage/'+digest+'.jpg':raise ValueError('The reviewed frame must be a local, content-addressed live-stage JPEG.')
        base=(self.root/'web/director-stage').resolve();path=self.root/'web'/poster.lstrip('/')
        if not base.is_relative_to(self.root.resolve()) or not path.resolve().is_relative_to(base):
            raise ValueError('The reviewed live frame path is invalid.')
        try:
            if not path.is_file() or path.stat().st_size>5_000_000:raise ValueError('The reviewed live frame is missing or exceeds 5 MB.')
            content=path.read_bytes()
            if len(content)>5_000_000 or hashlib.sha256(content).hexdigest()!=digest:
                raise ValueError('The live frame changed after review. Review the current file again.')
            with Image.open(io.BytesIO(content)) as image:
                if image.format!='JPEG' or image.size!=(width,height) or image.getexif().get(274,1)!=1:
                    raise ValueError('The live frame dimensions, orientation or JPEG format do not match its review.')
                image.load() # Decode the bounded JPEG; header-only verification misses truncated scans.
        except OSError:raise ValueError('The reviewed live frame could not be read. Prepare it again.') from None
        metadata={k:item[k] for k in ('profileRevision','sourceSha256','poster','width','height','aspectRatio','background','scene')}
        metadata.update(character=character,appearance=appearance)
        key='stage-'+hashlib.sha256(json.dumps(metadata,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        return {'referenceKey':key,**metadata},content

    def preflight(self,character,appearance):
        try:
            reference,_=self.resolve(character,appearance)
            return {'ready':True,'stageReference':reference}
        except ValueError as error:return {'ready':False,'message':str(error),'stageReference':None}

    def expected(self,character,appearance,key):
        reference,content=self.resolve(character,appearance)
        if not isinstance(key,str) or key!=reference['referenceKey']:
            raise ValueError('The live stage changed since preview. Refresh the preview before starting video.')
        return reference,content

    def prepare(self,gate,character,appearance,key):
        reference,content=self.expected(character,appearance,key)
        gate.prepare_reference(reference['referenceKey'],content)
        # A changed manifest/profile during upload cannot publish an accepted
        # preparation. The uploaded old content stays harmless under its old key.
        current,_=self.expected(character,appearance,key)
        if current!=reference:raise ValueError('The live stage changed during preparation. Refresh its preview.')
        with gate.lock:gate.stage_references[key]=reference
        return {'ok':True,'character':character,'stageReference':reference}
