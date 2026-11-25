"""Metadata-only queue checkpoints. Recovery never restores transport or budgets."""
import copy
import hashlib
import json
import math
import os
import re
from pathlib import Path
import threading
import uuid
from afterhours.dj_bridge import BridgeError
from afterhours.background_analysis import merge_cleanup_pending

FIELDS=('candidates','order','setlist','secondsPerTrack','excerptBars','maxTempoShift',
        'mixStyle','tempoTargets','query','artistFilter','genreFilter','bpmMin','bpmMax',
        'analysis','aiReview','resumeBlocked','cleanupPending','recoveryHistory','recoveryRemainder')
MAX_BYTES=2_000_000


def recovery_state(session):
    # Copy only recovery metadata while holding the state lock. Live telemetry,
    # waveform samples, command receipts and running workers are never saved.
    with session.lock:
        saved={key:copy.deepcopy(session.state[key]) for key in FIELDS if key in session.state}
        saved['catalogFolder']=copy.deepcopy(session.folder)
        pending=copy.deepcopy(session.state.get('queuedUpdate') or {})
        index=session.state.get('index',-1)
        current_ids=[t['id'] for t in session.state.get('order',[])]
    if len(saved.get('candidates',[]))<2:return None
    saved['recordingAvailability']=session.availability.snapshot()
    if pending.get('tracks'):
        # A selected tail belongs to the user's queue even while its audio is
        # unprepared. Rebuild its cues after recovery; never revive its worker.
        saved.update(order=pending['tracks'],analysis=None,aiReview=None,
                     tempoTargets=pending.get('tempoTargets'))
        saved['setlist']['sourceById'].update(pending.get('sourceById',{}))
    order_ids=[track['id'] for track in saved['order']]
    if len(order_ids)>12:
        if (type(index) is not int or not 0<index<len(order_ids) or index>256 or
                not 1<=len(order_ids)-index<=12 or order_ids[:index]!=current_ids[:index]):
            raise ValueError('The long queue has no valid heard-history boundary.')
        saved['recoveryWindow']={'heardIds':order_ids[:index],'remainingIds':order_ids[index:]}
    ids=set(order_ids)
    saved['setlist']['sourceById']={key:value for key,value in saved['setlist']['sourceById'].items() if key in ids}
    return saved



def _audit_tracks(rows):
    if (not isinstance(rows,list) or len(rows)>256 or
            any(not isinstance(t,dict) or set(t)!={'id','title','artist'} or
                not isinstance(t['id'],str) or not re.fullmatch(r'virtualdj:[a-f0-9]{64}',t['id']) or
                not isinstance(t['title'],str) or not t['title'].strip() or not isinstance(t['artist'],str)
                for t in rows) or len({t['id'] for t in rows})!=len(rows)):
        raise ValueError('Saved recovery history is invalid.')
    return copy.deepcopy(rows)


def _restore_window(saved):
    """Retain heard identities for audit, never restore a running index/grant."""
    saved=copy.deepcopy(saved)
    history=_audit_tracks(saved.get('recoveryHistory',[]))
    remainder=_audit_tracks(saved.get('recoveryRemainder',[]))
    if len(remainder)>1:raise ValueError('Saved recovery remainder is invalid.')
    window=saved.get('recoveryWindow')
    if window is None:
        # A new selected queue supersedes the old final-single recovery note.
        if saved.get('order'):remainder=[]
        return saved,history,remainder,False
    order=saved.get('order')
    if (not isinstance(window,dict) or set(window)!={'heardIds','remainingIds'} or
            not isinstance(order,list) or not 13<=len(order)<=268 or
            not isinstance(window['heardIds'],list) or not 1<=len(window['heardIds'])<=256 or
            not isinstance(window['remainingIds'],list) or not 1<=len(window['remainingIds'])<=12 or
            any(not isinstance(t,dict) or not isinstance(t.get('id'),str) for t in order)):
        raise ValueError('Saved long queue history is invalid.')
    ids=[t['id'] for t in order]
    catalog=saved.get('candidates')
    if not isinstance(catalog,list) or any(not isinstance(t,dict) or not isinstance(t.get('id'),str) for t in catalog):
        raise ValueError('Saved long queue catalog is invalid.')
    catalog_ids={t['id'] for t in catalog}
    if window['heardIds']+window['remainingIds']!=ids or len(set(ids))!=len(ids) or any(tid not in catalog_ids for tid in ids):
        raise ValueError('Saved long queue history does not match its exact order.')
    offset=len(window['heardIds'])
    refs=[{'id':t['id'],'title':t.get('title'),'artist':t.get('artist')} for t in order]
    history=_audit_tracks(refs[:offset]);tail=order[offset:]
    remainder=_audit_tracks(refs[offset:]) if len(tail)==1 else []
    # One last recording stays in Catalog with an explicit recovery receipt;
    # do not invent a second recording or replay a heard one to make a pair.
    saved.update(order=tail if len(tail)>=2 else [],analysis=None,aiReview=None)
    kept={t['id'] for t in saved['order']}
    policy=saved.get('setlist')
    if not isinstance(policy,dict):raise ValueError('Saved setlist preferences are invalid.')
    if (type(policy.get('allowReorder')) is not bool or not isinstance(policy.get('lockedIds'),list) or
            any(not isinstance(tid,str) for tid in policy['lockedIds']) or len(set(policy['lockedIds']))!=len(policy['lockedIds']) or
            not isinstance(policy.get('sourceById'),dict)):
        raise ValueError('Saved setlist preferences are invalid.')
    # Validate the full policy before projecting its heard prefix away.
    if any(tid not in ids for tid in policy['lockedIds']) or any(tid not in ids or value not in ('planner','operator') for tid,value in policy['sourceById'].items()):
        raise ValueError('Saved setlist preferences are invalid.')
    policy['lockedIds']=[tid for tid in policy['lockedIds'] if tid in kept]
    policy['sourceById']={tid:value for tid,value in policy['sourceById'].items() if tid in kept}
    targets=saved.get('tempoTargets')
    if targets is not None:
        if (not isinstance(targets,list) or any(not isinstance(row,dict) or set(row)!={'trackId','bpm'} or
                row.get('trackId') not in ids or type(row.get('bpm')) not in (int,float) or
                not math.isfinite(row['bpm']) or not 40<=row['bpm']<=300 for row in targets) or
                len({row['trackId'] for row in targets})!=len(targets)):
            raise ValueError('Saved tempo targets are invalid.')
        saved['tempoTargets']=[row for row in targets if row['trackId'] in kept] or None
    return saved,history,remainder,True


def _restore_cleanup_metadata(session, saved):
    """Restore an unresolved ownership latch, never a worker or engine command."""
    if not isinstance(saved,dict) or saved.get('resumeBlocked') is not True:return
    with session.lock:
        sources=[saved]
        if session.state.get('resumeBlocked'):
            sources.append(session.state)
        # Restored descriptors are evidence only. Explicit Stop revalidates
        # their exact native identity/isolation before any bounded cleanup.
        session.update(resumeBlocked=True,cleanupPending=merge_cleanup_pending(
            *(source.get('cleanupPending') for source in sources)))


class SessionRecovery:
    def __init__(self,path):
        self.path=Path(path);self.digest=None;self.cancel=threading.Event();self.thread=None
        self.last_error=None

    def save(self,session):
        with session.lock:
            state=recovery_state(session)
            # A search can leave fewer than two candidates. Ownership of
            # uncertain native automation must survive even without a queue.
            cleanup=({'resumeBlocked':True,'cleanupPending':copy.deepcopy(session.state.get('cleanupPending'))}
                     if session.state.get('resumeBlocked') else None)
        record={'version':1,'session':state,'cleanup':cleanup}
        body=json.dumps(record,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
        if len(body)>MAX_BYTES and state is not None:
            state['analysis']=None;state['aiReview']=None
            body=json.dumps(record,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
        if len(body)>MAX_BYTES:raise ValueError('The queue recovery record exceeds its size limit.')
        digest=hashlib.sha256(body).hexdigest()
        if digest==self.digest:return False
        if self.path.is_symlink():raise ValueError('The queue recovery path must be a regular local file.')
        self.path.parent.mkdir(parents=True,exist_ok=True)
        temporary=self.path.with_name(self.path.name+'.'+uuid.uuid4().hex+'.tmp')
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        try:
            with os.fdopen(fd,'wb') as stream:
                stream.write(body);stream.flush();os.fsync(stream.fileno())
            os.replace(temporary,self.path)
        finally:
            temporary.unlink(missing_ok=True)
        self.digest=digest;self.last_error=None
        return True

    def restore(self,session):
        if not self.path.exists() and not self.path.is_symlink():return {'status':'empty','restored':False}
        if self.path.is_symlink() or not self.path.is_file() or self.path.stat().st_size>MAX_BYTES:
            raise ValueError('The queue recovery record is not a bounded regular file.')
        record=json.loads(self.path.read_bytes())
        if not isinstance(record,dict) or record.get('version')!=1 or 'session' not in record:
            raise ValueError('The queue recovery record has an unsupported format.')
        _restore_cleanup_metadata(session,record.get('cleanup'))
        state=record['session']
        if state is None:
            if session.state.get('resumeBlocked'):
                session.update(message='Native automation cleanup remains unconfirmed. Stop playback and verify cleanup before starting another set.')
                return {'status':'cleanup-required','restored':False,'cleanupRequired':True,'autoplay':False}
            return {'status':'empty','restored':False}
        if not isinstance(state,dict):raise ValueError('The saved queue is invalid.')
        # Legacy version 1 records stored cleanup only inside the queue. Keep
        # that safety evidence even if unrelated queue validation then fails.
        _restore_cleanup_metadata(session,state)
        state,history,remainder,windowed=_restore_window(state)
        # The existing restore transaction validates exact recording identity,
        # catalog scope, ordering, cue digests and policy before committing.
        try:
            session.restore(state)
            session.update(recoveryHistory=history,recoveryRemainder=remainder)
            if windowed or remainder:
                message=('The final remaining recording is retained in Catalog. Add another recording to prepare a new queue; playback is paused.' if remainder else
                    f'The remaining {len(state["order"])} records are restored, paused and awaiting fresh preparation. Earlier played records are retained in recovery history.')
                session.update(message=message)
                return {'status':'needs-analysis' if not remainder else 'catalog-only','restored':True,'tracks':len(state['order']),
                    'playedHistoryCount':len(history),'remainingInCatalog':len(remainder),'analysisPreserved':False,'autoplay':False}
            return {'status':'restored','restored':True,'tracks':len(state['order']),
                    'analysisPreserved':bool(state.get('analysis')),'autoplay':False}
        except (ValueError,OSError,BridgeError):
            if not state.get('analysis') and not state.get('aiReview'):raise
            crate=copy.deepcopy(state);crate.update(analysis=None,aiReview=None)
            session.restore(crate)
            session.update(recoveryHistory=history,recoveryRemainder=remainder)
            session.update(message='The saved queue is restored. Its previous mix analysis needs refreshing.')
            return {'status':'needs-analysis','restored':True,'tracks':len(crate['order']),
                    'analysisPreserved':False,'autoplay':False}

    def retain_invalid(self):
        if self.path.is_file() and not self.path.is_symlink():
            backup=self.path.with_name(self.path.name+'.invalid-'+uuid.uuid4().hex)
            os.replace(self.path,backup)
            return backup
        return None

    def start(self,session,interval=1):
        if self.thread and self.thread.is_alive():return
        def save():
            try:self.save(session)
            except (OSError,ValueError,TypeError,KeyError):
                if self.last_error is None:
                    self.last_error='The current queue could not be saved for recovery.'
                    session.event(self.last_error,'recovery')
        save()
        def run():
            while not self.cancel.wait(interval):save()
        self.thread=threading.Thread(target=run,name='queue-checkpoint',daemon=True);self.thread.start()

    def close(self,session):
        self.cancel.set()
        if self.thread:self.thread.join(2)
        self.save(session)
