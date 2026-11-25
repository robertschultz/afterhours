"""Verified 20 ms display envelopes. No PCM persistence or model/provider calls.

The waveform scan has its own file and receipt so drawing a waveform cannot
invalidate the approved musical analysis or cue-plan fingerprints.
"""
import hashlib
import json
import math
import re
import time
from afterhours.audio_analysis import REPORTS, PLUGIN, read_scan, validate_scan, verified_energy, restore_effect
from afterhours.dj_bridge import BridgeError, as_bool
from afterhours.vdj_catalog import identity


def read_waveform(recording_id, root=REPORTS):
    if not isinstance(recording_id,str) or not re.fullmatch('[a-f0-9]{64}',recording_id):
        raise ValueError('Invalid waveform recording identity.')
    path=root/(recording_id+'.waveform.json')
    if path.is_symlink() or not path.is_file() or path.stat().st_size>2_000_000:return None
    try:
        raw=path.read_bytes();data=json.loads(raw)
        if (not isinstance(data,dict) or data.get('version')!=1 or
                data.get('recordingId')!=recording_id or data.get('audioSaved') is not False):return None
        return data,hashlib.sha256(raw).hexdigest()
    except (OSError,ValueError):return None


def validate_waveform(data,sound_end,source_feature_digest):
    details=validate_scan(data,sound_end)
    if details['featureDigest']!=source_feature_digest:
        raise ValueError('Waveform audio does not match the verified recording analysis.')
    env=data.get('envelope')
    if not isinstance(env,dict):raise ValueError('Detailed waveform is unavailable in this bridge version.')
    step=env.get('framesPerBin');peaks=env.get('peak');rms=env.get('rms')
    if type(step) is not int or step!=data['sampleRate']//50:
        raise ValueError('Invalid waveform sample spacing.')
    count=math.ceil(data['totalFrames']/step)
    if (not isinstance(peaks,list) or not isinstance(rms,list) or
            not 1<=count<=60010 or len(peaks)!=count or len(rms)!=count or
            any(type(p) is not int or type(r) is not int or not 0<=r<=p<=32768 for p,r in zip(peaks,rms))):
        raise ValueError('Invalid waveform coverage or amplitudes.')
    details['envelopeDigest']=hashlib.sha256(json.dumps(env,sort_keys=True).encode()).hexdigest()
    return details


def source_evidence(sid,duration,digest,root):
    source=read_scan(sid,root)
    energy=verified_energy(sid,duration,root)
    if not source or not energy or source[1]!=digest:
        raise BridgeError('Prepare verified audio analysis for this exact recording first.')
    return source[0],hashlib.sha256(json.dumps(source[0]['features'],sort_keys=True).encode()).hexdigest()


def verified_waveform(sid,duration,source_digest,root=REPORTS):
    observed=read_waveform(sid,root);path=root/(sid+'.waveform.verified.json')
    if not observed or path.is_symlink() or not path.is_file() or path.stat().st_size>16000:return None
    try:
        _,feature_digest=source_evidence(sid,duration,source_digest,root)
        data,digest=observed;receipt=json.loads(path.read_text())
        if (receipt.get('version')!=1 or receipt.get('recordingId')!=sid or
                receipt.get('sourceReportDigest')!=source_digest or receipt.get('reportDigest')!=digest or
                receipt.get('audioSaved') is not False or receipt.get('method')!='two-stable-waveforms-and-verified-analysis' or
                type(receipt.get('passes')) is not int or receipt['passes']<2 or
                type(receipt.get('stableForSeconds')) not in (int,float) or
                not 1<=receipt['stableForSeconds']<=40 or abs(data['duration']-duration)>.05):return None
        details=validate_waveform(data,receipt['soundEndSeconds'],feature_digest)
        if details['envelopeDigest']!=receipt.get('envelopeDigest'):return None
        return {'trackId':'virtualdj:'+sid,'sourceReportDigest':source_digest,'reportDigest':digest,
                'duration':data['duration'],'resolutionSeconds':data['envelope']['framesPerBin']/data['sampleRate'],
                'peak':data['envelope']['peak'],'rms':data['envelope']['rms'],'amplitudeScale':32768,
                'source':'Verified decoded-audio peak and RMS envelope','audioIncluded':False}
    except (OSError,ValueError,KeyError,TypeError,BridgeError):return None


def scan_waveform(client,track,evidence,check=lambda:None,wait=time.sleep,
                  root=REPORTS,clock=time.monotonic,timeout=40, deck=1, isolated=None):
    isolated=deck in (3,4) if isolated is None else isolated is True
    if deck not in ((1,2,3,4) if isolated else (1,)):raise ValueError('Analysis uses paused deck A or an isolated spare deck.')
    sid=track['engine']['selection_id'];duration=evidence['duration']
    source_digest=(evidence.get('energyMap') or {}).get('reportDigest')
    _,feature_digest=source_evidence(sid,duration,source_digest,root)
    sound_end=next((p['seconds'] for p in evidence['points'] if p['label']=='realEnd'),None)
    if sound_end is None:raise BridgeError('A native sound-end marker is required for the waveform scan.')
    deadline=clock()+timeout
    def guard():
        check()
        if isolated:
            from afterhours.background_analysis import guard_isolation
            guard_isolation(client,deck)
        if clock()>=deadline:raise BridgeError('Waveform scan timed out. Keep the recording loaded and try preparing again.')
        if any(as_bool(client.query(f'deck {d} play','bool')) for d in ((deck,) if isolated else (1,2))):
            raise BridgeError('Playback began. Waveform scanning stopped without changing transport.')
        if identity(str(client.query(f'deck {deck} get_filepath','text')))!=sid:
            raise BridgeError('The recording changed during waveform preparation.')
    guard()
    previous=client.query(f'deck {deck} get_effect_name 1','text')
    if not isinstance(previous,str) or not re.fullmatch(r'[A-Za-z0-9 _.\-]{1,80}',previous):
        raise BridgeError('Choose a named inactive effect in deck A slot 1 before preparing waveforms.')
    if as_bool(client.query(f'deck {deck} effect_active 1','bool')):
        raise BridgeError('Turn off deck A effect slot 1 before preparing waveforms.')
    selected=False;last=None;last_time=None;passes=0;began=clock()
    last_reason='Waiting for two matching whole-track waveforms.'
    try:
        client.send(f'deck {deck} effect_select 1 "{PLUGIN}"')
        until=clock()+3
        while client.query(f'deck {deck} get_effect_name 1','text')!=PLUGIN:
            guard()
            if clock()>=until:raise BridgeError('The updated analysis bridge is not available. Restart VirtualDJ after updating it.')
            wait(.1)
        selected=True;client.send(f'deck {deck} effect_active 1 on');wait(.1)
        if not as_bool(client.query(f'deck {deck} effect_active 1','bool')):raise BridgeError('The waveform bridge did not activate.')
        while clock()<deadline:
            guard();old=read_waveform(sid,root);old_id=old[0]['scanStartedAtMs'] if old else None;fresh=False
            while clock()<deadline:
                guard()
                if client.query(f'deck {deck} get_effect_name 1','text')!=PLUGIN:raise BridgeError('The effect slot changed. Waveform preparation stopped.')
                client.send(f'deck {deck} effect_button 1 2');wait(.05)
                label=client.query(f'deck {deck} get_effect_string 1 1','text');current=read_waveform(sid,root)
                if current and (fresh or current[0]['scanStartedAtMs']!=old_id):
                    fresh=True
                    if current[0].get('bufferReadComplete') is True:break
                if clock()-began>3 and not fresh:
                    raise BridgeError('Detailed waveform scanning needs Afterhours Analysis 0.1.2 or later loaded in VirtualDJ.')
                if label and not any(s in label for s in ('scanned','incomplete','Buffer read finished','Ready')):
                    raise BridgeError('Waveform preparation stopped: '+str(label)[:192])
                wait(.15)
            else:break
            passes+=1;data,digest=current
            try:details=validate_waveform(data,sound_end,feature_digest)
            except ValueError as exc:
                last=last_time=None
                last_reason=str(exc)
            else:
                if details['envelopeDigest']==last and clock()-last_time>=1:
                    guard();source_evidence(sid,duration,source_digest,root)
                    receipt={'version':1,'recordingId':sid,'reportDigest':digest,'sourceReportDigest':source_digest,
                             'method':'two-stable-waveforms-and-verified-analysis','passes':passes,
                             'stableForSeconds':round(clock()-last_time,3),'soundEndSeconds':sound_end,
                             'scanWallSeconds':round(clock()-began,3),'verifiedAt':time.time(),'audioSaved':False,**details}
                    path=root/(sid+'.waveform.verified.json');tmp=root/(sid+'.waveform.verified.partial')
                    if path.is_symlink() or tmp.is_symlink():raise BridgeError('The waveform report path is not a regular local file.')
                    tmp.write_text(json.dumps(receipt,indent=2));tmp.replace(path);return receipt
                last=details['envelopeDigest'];last_time=clock()
            wait(1.05)
        raise BridgeError('Waveform preparation remains unverified: '+last_reason+' No display data was published.')
    finally:
        if selected:restore_effect(client,deck,previous,wait=wait,clock=clock)
