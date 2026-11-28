"""Bounded local memory of exact recordings that could not be prepared.

Catalog metadata is not proof of playable audio. Transport uncertainty never
enters this cache. Measured preparation failures expire; explicit provider
preview restrictions persist until a deliberate full-audio recheck proves
otherwise. Search, ordinary analysis and loaded duration cannot clear them.
"""
import copy
import json
import math
import os
from pathlib import Path
import re
import threading
import time
from afterhours.dj_bridge import BridgeError

LIFETIMES = {'recording-unavailable': 900, 'limited-decoded-audio': 3600, 'native-stream-rejected': 900}
PREVIEW_ONLY = 'provider-preview-only'
LEGACY_BOUNDED_SEARCH_REASON = 'The exact recording is no longer available in the search results. No replacement was loaded.'


class ProviderPreviewRestricted(BridgeError):
    """Explicit account/recording evidence prohibits an automatic Load."""


def exact_id(value):
    return isinstance(value,str) and re.fullmatch(r'virtualdj:[a-f0-9]{64}',value)


def finite(value):
    return type(value) in (int,float) and math.isfinite(value)


def safe_label(value, maximum):
    return isinstance(value,str) and 1<=len(value)<=maximum and not any(ord(c)<32 or ord(c)==127 for c in value)


def valid_evidence(evidence):
    return (isinstance(evidence,dict) and set(evidence)=={'source','reference','sha256'}
            and evidence['source'] in ('native-dialog','operator-confirmed')
            and isinstance(evidence['reference'],str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,159}',evidence['reference'])
            and isinstance(evidence['sha256'],str) and re.fullmatch(r'[a-f0-9]{64}',evidence['sha256']))


class RecordingAvailability:
    def __init__(self, path=None, clock=time.time):
        self.path=Path(path) if path else None
        self.clock=clock;self.lock=threading.RLock();self.entries={};self.clearances={}
        if self.path and self.path.is_file() and not self.path.is_symlink() and self.path.stat().st_size<300000:
            try:self.restore(json.loads(self.path.read_text()))
            except (OSError,ValueError,TypeError):pass

    def restore(self, rows):
        if isinstance(rows,dict) and rows.get('version')==2:
            # Clearance receipts keep an older session checkpoint from
            # resurrecting a restriction already explicitly rechecked.
            clearances=rows.get('clearances',{})
            if isinstance(clearances,dict):
                for tid,row in list(clearances.items())[-128:]:
                    if (exact_id(tid) and isinstance(row,dict) and finite(row.get('verifiedAt'))
                            and 0<=row['verifiedAt']<=self.clock()
                            and isinstance(row.get('reportDigest'),str)
                            and re.fullmatch(r'[a-f0-9]{64}',row['reportDigest'])):
                        self.clearances[tid]={'verifiedAt':row['verifiedAt'],'reportDigest':row['reportDigest']}
            rows=rows.get('entries')
        if not isinstance(rows,list):return
        now=self.clock()
        for row in rows[-128:]:
            if isinstance(row,dict) and row.get('code')==PREVIEW_ONLY:
                if (not exact_id(row.get('trackId')) or not safe_label(row.get('provider'),80)
                        or not finite(row.get('previewSeconds')) or not 1<=row['previewSeconds']<=1200
                        or not finite(row.get('checkedAt')) or not 0<=row['checkedAt']<=now
                        or not valid_evidence(row.get('evidence'))):continue
                if self.clearances.get(row['trackId'],{}).get('verifiedAt',-1)>=row['checkedAt']:continue
                existing=self.entries.get(row['trackId']) or {}
                if existing.get('code')==PREVIEW_ONLY and existing['checkedAt']>=row['checkedAt']:continue
                self._store_preview(row['trackId'],row['provider'],row['previewSeconds'],row['evidence'],row['checkedAt'])
                continue
            if (not isinstance(row,dict) or not isinstance(row.get('trackId'),str)
                    or not re.fullmatch(r'virtualdj:[a-f0-9]{64}',row['trackId'])
                    or row.get('code') not in LIFETIMES
                    or not isinstance(row.get('reason'),str) or not 1<=len(row['reason'])<=1600
                    or type(row.get('checkedAt')) not in (int,float)
                    or type(row.get('retryAfter')) not in (int,float)
                    or not row['checkedAt']<=now<row['retryAfter']
                    or row['retryAfter']-row['checkedAt']>LIFETIMES[row['code']]):continue
            # Older lookup treated the first 101 artist results as a complete
            # catalog. Provider-capped pages cannot establish unavailability.
            if row['code']=='recording-unavailable' and row['reason']==LEGACY_BOUNDED_SEARCH_REASON:continue
            if (self.entries.get(row['trackId']) or {}).get('code')==PREVIEW_ONLY:continue
            self.entries[row['trackId']]={k:row[k] for k in ('trackId','code','reason','checkedAt','retryAfter')}
        self._bound()

    def _bound(self):
        while len(self.entries)>128:
            transient=next((tid for tid,row in self.entries.items() if row['code']!=PREVIEW_ONLY),None)
            if transient is None:raise ValueError('The provider restriction cache is full; no restriction was discarded.')
            self.entries.pop(transient)
        self.clearances=dict(list(self.clearances.items())[-128:])

    def snapshot(self):
        with self.lock:
            now=self.clock()
            self.entries={tid:r for tid,r in self.entries.items() if r['code']==PREVIEW_ONLY or r['checkedAt']<=now<r['retryAfter']}
            return copy.deepcopy(list(self.entries.values()))

    def blocked(self, track_id):
        return next((r for r in self.snapshot() if r['trackId']==track_id),None)

    def require_load(self, track_id):
        row=self.blocked(track_id)
        if row and row['code']==PREVIEW_ONLY:raise ProviderPreviewRestricted(row['reason'])

    def require_track(self, track):
        self.require_load(track.get('id'))
        sid=(track.get('engine') or {}).get('selection_id')
        if isinstance(sid,str):self.require_load('virtualdj:'+sid)

    def _store_preview(self, track_id, provider, seconds, evidence, observed_at):
        if track_id not in self.entries and sum(r['code']==PREVIEW_ONLY for r in self.entries.values())>=128:
            raise ValueError('The provider restriction cache is full; no restriction was discarded.')
        self.entries[track_id]={'trackId':track_id,'code':PREVIEW_ONLY,'provider':provider,
            'previewSeconds':seconds,'checkedAt':observed_at,'retryAfter':None,
            'reason':f'{provider} confirmed only a {seconds:g}-second preview for this recording. Full audio needs an explicit access recheck; automatic loading is held.',
            'evidence':copy.deepcopy(evidence),'requiresExplicitRecheck':True,'playback_verified':False}

    def record_preview_only(self, track_id, provider, seconds, evidence, observed_at=None):
        now=self.clock();observed_at=now if observed_at is None else observed_at
        if (not exact_id(track_id) or not safe_label(provider,80) or not finite(seconds) or not 1<=seconds<=1200
                or not valid_evidence(evidence) or not finite(observed_at) or not 0<=observed_at<=now):
            raise ValueError('A provider preview restriction needs exact recording and explicit observation evidence.')
        with self.lock:
            prior=self.entries.get(track_id) or {}
            if self.clearances.get(track_id,{}).get('verifiedAt',-1)>=observed_at:return
            if prior.get('code')==PREVIEW_ONLY and prior['checkedAt']>observed_at:return
            self._store_preview(track_id,provider,seconds,evidence,observed_at)
            self._bound();self.save()

    def record(self, track_id, code, reason):
        if code not in LIFETIMES:raise ValueError('Only conclusive recording failures may be remembered.')
        if not isinstance(track_id,str) or not re.fullmatch(r'virtualdj:[a-f0-9]{64}',track_id):return
        with self.lock:
            now=self.clock();self.snapshot()
            if (self.entries.get(track_id) or {}).get('code')==PREVIEW_ONLY:return
            self.entries.pop(track_id,None)
            self.entries[track_id]={'trackId':track_id,'code':code,'reason':str(reason)[:1600] or code,
                                    'checkedAt':now,'retryAfter':now+LIFETIMES[code]}
            self._bound()
            self.save()

    def verified(self, track_id):
        with self.lock:
            if (self.entries.get(track_id) or {}).get('code')==PREVIEW_ONLY:return
            if self.entries.pop(track_id,None) is not None:self.save()

    def confirm_full_audio_recheck(self, track_id, duration, receipt, *, operator_requested=False, report_root=None):
        """Internal explicit clearance; never called by search or preparation.

        The operator first resolves access and obtains fresh full-audio proof.
        This method sends no native command, grants no Load bypass and does
        not claim audible playback verification.
        """
        from afterhours.audio_analysis import verified_energy,read_scan,validate_scan,REPORTS
        with self.lock:
            row=self.entries.get(track_id)
            if not row or row['code']!=PREVIEW_ONLY:raise ValueError('No provider restriction is awaiting recheck.')
            if (operator_requested is not True or not finite(duration) or duration<=row['previewSeconds']+10
                    or not isinstance(receipt,dict) or receipt.get('recordingId')!=track_id.removeprefix('virtualdj:')
                    or not finite(receipt.get('verifiedAt'))
                    or not row['checkedAt']<receipt['verifiedAt']<=self.clock()
                    or self.clock()-receipt['verifiedAt']>120):
                raise ValueError('An explicit fresh full-audio recheck is required.')
            sid=track_id.removeprefix('virtualdj:');root=Path(report_root) if report_root is not None else REPORTS
            receipt_path=root/(sid+'.verified.json')
            if (not receipt_path.is_file() or receipt_path.is_symlink() or receipt_path.stat().st_size>16000
                    or json.loads(receipt_path.read_text())!=receipt):
                raise ValueError('The recheck receipt does not match the saved native measurement.')
            energy=verified_energy(sid,duration,root);observed=read_scan(sid,root)
            if not observed:raise ValueError('Full decoded coverage is unconfirmed.')
            data,digest=observed
            if (not energy or energy.get('coverage')!=1 or energy.get('reportDigest')!=digest
                    or receipt.get('reportDigest')!=digest):
                raise ValueError('Full decoded coverage is unconfirmed.')
            details=validate_scan(data,receipt.get('soundEndSeconds'))
            if details['lastSignalSeconds']<max(duration*.9,row['previewSeconds']+2):
                raise ValueError('The recheck still has insufficient audio beyond the preview.')
            self.entries.pop(track_id)
            self.clearances[track_id]={'verifiedAt':receipt['verifiedAt'],'reportDigest':digest}
            self._bound();self.save()

    def found_again(self, track_id):
        """An explicit fresh search can disprove absence, never decoding limits."""
        with self.lock:
            if (self.entries.get(track_id) or {}).get('code')=='recording-unavailable':
                self.entries.pop(track_id);self.save()

    def save(self):
        if not self.path:return
        self.path.parent.mkdir(parents=True,exist_ok=True)
        temporary=self.path.with_suffix('.tmp')
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
        with os.fdopen(fd,'w') as handle:
            json.dump({'version':2,'entries':self.snapshot(),'clearances':self.clearances},handle)
        os.replace(temporary,self.path)
