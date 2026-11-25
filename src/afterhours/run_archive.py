"""Bounded observed playback journals and immutable local recap records.

Observation never queries or controls a deck. Persistence and optional numerical
source-feature reads happen on a separate worker, outside the control lock.
"""
import bisect
import copy
from datetime import datetime
import hashlib
import json
import itertools
import math
import os
from pathlib import Path
import re
import threading
import time
import uuid

MAX_INTERVALS=128
MAX_BYTES=2_000_000
MAX_ANCHORS=2048
ID=re.compile(r'[a-f0-9]{32}')
TRACK=re.compile(r'virtualdj:[a-f0-9]{64}')


def finite(value):return type(value) in (int,float) and math.isfinite(value)


def metadata(row):
    if not isinstance(row,dict) or not TRACK.fullmatch(str(row.get('id',''))):return None
    result={'id':row['id'],'title':str(row.get('title') or 'Untitled recording')[:300],
            'artist':str(row.get('artist') or '')[:300]}
    art=row.get('artworkUrl')
    if isinstance(art,str) and len(art)<=2048 and (art.startswith(('https://','http://','/api/'))):result['artworkUrl']=art
    if finite(row.get('bpm')) and 40<=row['bpm']<=300:result['bpm']=row['bpm']
    return result


def observation(state,now):
    """Called under the session state lock; copies only bounded known facts."""
    telemetry=state.get('telemetry') or {}
    known={r.get('id'):r for r in state.get('candidates',[])}
    known.update({r.get('id'):r for r in state.get('order',[])})
    decks=[]
    # A four-deck set plays on all four; otherwise C and D are spare decks
    # whose activity is never a play.
    rotation=tuple(state.get('deckRotation') or (1,2))
    if rotation not in ((1,2),(1,2,3,4)):rotation=(1,2)
    for row in telemetry.get('decks',[])[:len(rotation)]:
        if row.get('deck') not in rotation:continue
        deck={k:row.get(k) for k in ('deck','id','loaded','playing','position','beatPosition','bpm','sampledAt')}
        deck['metadata']=metadata({**known.get(row.get('id'),{}),**{k:v for k,v in row.items() if k in ('id','title','artist','bpm') and v is not None and v!=''}})
        decks.append(deck)
    proof={}
    reports=[state.get('analysis') or {},((state.get('queuedUpdate') or {}).get('prepared') or {}).get('analysis') or {}]
    for report in reports:
        for row in report.get('tracks',[])[:24]:
            digest=(row.get('energyMap') or {}).get('reportDigest')
            if TRACK.fullmatch(str(row.get('id',''))) and isinstance(digest,str) and re.fullmatch('[a-f0-9]{64}',digest) and finite(row.get('duration')):
                proof[row['id']]={'duration':row['duration'],'digest':digest}
    return {'sessionId':state.get('id'),'phase':state.get('phase'),'active':state.get('active') is True,
        'startedAt':state.get('startedAt'),'sampledAt':telemetry.get('sampledAt'),
        'crossfader':telemetry.get('crossfader'),'decks':decks,'proof':proof,'observedAt':now}


class RunArchive:
    def __init__(self,path=None,*,clock=time.time,monotonic=time.monotonic,report_root=None):
        self.path=Path(path) if path is not None else None
        self.clock,self.monotonic=clock,monotonic;self.report_root=report_root
        self.lock=threading.RLock();self.condition=threading.Condition(self.lock)
        self.active=None;self.pending=None;self.finalizing=[];self.busy=False;self.closed=False
        self.last_checkpoint=0;self.summary=None;self.cache={};self.error=None
        self.sequence=0;self.last_ordinal=-1
        if self.path:
            if self.path.is_symlink():raise ValueError('The recap directory must be a regular local directory.')
            self.path.mkdir(parents=True,exist_ok=True)
            # Ordinary startup reads one pointer. The bounded legacy fallback
            # neither scans the growing music crate nor opens every old recap.
            files=[]
            try:
                pointer=self._read(self.path/'latest.json')
                if ID.fullmatch(str(pointer.get('id',''))):files=[self.path/(pointer['id']+'.json')]
            except (OSError,ValueError,TypeError):pass
            if not files:
                candidates=list(itertools.islice(self.path.glob('*.json'),512))
                files=sorted((p for p in candidates if ID.fullmatch(p.stem) and not p.is_symlink()),key=lambda p:p.stat().st_mtime,reverse=True)
            for file in files[:128]:
                try:
                    record=self._read(file)
                    if record.get('version')==1 and record.get('id')==file.stem:
                        self._validate(record)
                        self.summary=self._summary(record);self.cache[record['id']]=record
                        self.last_ordinal=record.get('archiveOrdinal',0);self.sequence=self.last_ordinal;break
                except (OSError,ValueError,TypeError,KeyError):continue
            journal=self.path/'active.json'
            if journal.exists():
                try:
                    stale=self._read(journal)
                    if stale.get('version')==1 and ID.fullmatch(stale.get('id','')) and stale.get('played'):
                        with self.condition:self._queue_final(stale,'interrupted',False)
                except (OSError,ValueError,TypeError,KeyError):self.error='The interrupted playback journal could not be read.'
        self.worker=threading.Thread(target=self._writer,name='recap-archive',daemon=True)
        self.worker.start()

    def _read(self,path):
        if path.is_symlink() or not path.is_file() or path.stat().st_size>MAX_BYTES:raise ValueError('Invalid recap record.')
        value=json.loads(path.read_bytes())
        if not isinstance(value,dict):raise ValueError('Invalid recap record.')
        return value

    def _write(self,path,value):
        body=json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
        if len(body)>MAX_BYTES:raise ValueError('The recap record exceeds its bound.')
        if path.is_symlink():raise ValueError('The recap destination must be a regular file.')
        temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        try:
            with os.fdopen(fd,'wb') as stream:stream.write(body);stream.flush();os.fsync(stream.fileno())
            os.replace(temporary,path)
        finally:temporary.unlink(missing_ok=True)

    def _copy(self):
        return copy.deepcopy({k:v for k,v in self.active.items() if k not in ('_mono','_open','_lastSeen')})

    def observe(self,view):
        now=self.clock();mono=self.monotonic()
        with self.condition:
            if self.closed:return
            if self.active and self.active['sessionId']!=view.get('sessionId'):self._seal('interrupted',False)
            sampled=view.get('sampledAt');global_fresh=finite(sampled) and -2<=now-sampled<3
            fresh=[]
            for deck in view.get('decks',[]):
                stamp=deck.get('sampledAt')
                # A terminal global pause receipt may refresh play state only.
                if deck.get('playing') is False and global_fresh:stamp=sampled
                if finite(stamp) and -2<=now-stamp<3:fresh.append({**deck,'_stamp':stamp})
            playing=[d for d in fresh if d.get('playing') is True and d.get('loaded') is True and d.get('metadata')]
            if self.active is None:
                if view.get('phase')!='playing' or not view.get('active') or not playing:return
                first=min(d['_stamp'] for d in playing);age=max(0,now-first)
                self.sequence+=1
                self.active={'version':1,'id':uuid.uuid4().hex,'revision':1,'archiveOrdinal':self.sequence,'sessionId':view.get('sessionId'),
                    'title':'Set · '+datetime.fromtimestamp(first).strftime('%b %d, %Y · %H:%M'),
                    'startAt':first,'observedUntil':first,'durationSeconds':0.0,'endedAt':None,
                    'played':[],'moments':[],'landed':[],'lessons':[],'proof':{},'anchors':[],
                    'coverage':{'truncated':False,'unconfirmedGaps':0},'_mono':mono-age,'_open':{},'_lastSeen':{}}
                self.last_checkpoint=-1e9
                self._moment(0,'Opening playback confirmed.','exact native A/B playing readback')
            run=self.active;changed=False
            wanted={r['id'] for r in run['played']}|{r['id'] for r in playing}
            for tid,proof in (view.get('proof') or {}).items():
                if tid in wanted and (tid in run['proof'] or len(run['proof'])<MAX_INTERVALS):run['proof'][tid]=proof
            for deck in fresh:
                number=deck['deck'];index=run['_open'].get(number)
                offset=max(0,mono-run['_mono']-max(0,now-deck['_stamp']))
                if index is not None:
                    previous=run['played'][index]
                    gap=offset-run['_lastSeen'].get(number,offset)>3
                    different=deck.get('id')!=previous['id']
                    if deck.get('playing') is False or different or gap:
                        if not gap:
                            previous['end']=max(previous['end'],offset);previous['endConfirmed']=deck.get('playing') is False or different
                        else:run['coverage']['unconfirmedGaps']+=1
                        run['_open'].pop(number,None);changed=True;index=None
                if deck.get('playing') is True and deck.get('loaded') is True and deck.get('metadata'):
                    if index is None:
                        if len(run['played'])>=MAX_INTERVALS:
                            run['coverage']['truncated']=True;continue
                        row={**deck['metadata'],'intervalId':run['id']+':'+str(len(run['played'])),'deck':number,
                            'start':offset,'end':offset,'endConfirmed':False}
                        run['played'].append(row);index=len(run['played'])-1;run['_open'][number]=index;changed=True
                        if index:self._moment(offset,'Playing '+row['title']+'.','exact native A/B playing readback')
                    run['played'][index]['end']=max(run['played'][index]['end'],offset)
                    run['_lastSeen'][number]=offset
                    cross=view.get('crossfader');position=deck.get('position')
                    program=finite(cross) and ((number%2==1 and cross<=.02) or (number%2==0 and cross>=.98))
                    if program and finite(position) and position>=0:
                        previous=next((r for r in reversed(run['anchors']) if r['intervalId']==run['played'][index]['intervalId']),None)
                        if previous is None or offset-previous['t']>=1:
                            run['anchors'].append({'t':offset,'position':position,'trackId':deck['id'],'intervalId':run['played'][index]['intervalId']})
                            if len(run['anchors'])>MAX_ANCHORS:run['anchors']=run['anchors'][::2]
                run['durationSeconds']=max(run['durationSeconds'],offset)
                run['observedUntil']=run['startAt']+run['durationSeconds']
            paused=global_fresh and len(view.get('decks',[])) in (2,4) and all(d.get('playing') is False for d in view['decks'])
            phase=view.get('phase')
            if phase=='completed' and paused:self._seal('completed',True);return
            if phase=='stopped' and not view.get('active'):
                self._seal('stopped' if paused else 'interrupted',paused);return
            if phase in ('error','takeover') and not view.get('active'):
                self._seal('interrupted',False);return
            if changed or mono-self.last_checkpoint>=1:
                self.pending=self._copy();self.last_checkpoint=mono;self.condition.notify_all()

    def _moment(self,at,label,basis):
        if len(self.active['moments'])<16:
            self.active['moments'].append({'id':uuid.uuid4().hex,'t':at,'label':label[:350],'basis':basis})

    def preference(self,change,receipt):
        from afterhours.learned_preferences import valid_effect,describe
        if not valid_effect(change):return
        with self.condition:
            if not self.active:return
            effect=copy.deepcopy(change);title,detail=describe(effect)
            key=hashlib.sha256(json.dumps(effect,sort_keys=True).encode()).hexdigest()[:24]
            lessons=self.active['lessons']
            if any(row['id']==key for row in lessons) or len(lessons)>=32:return
            lessons.append({'id':key,'title':title,'detail':detail,'effect':effect,'keep':False,
                            'evidence':{'kind':'explicit-operator-preference','requestId':receipt['id'],'at':receipt['at']}})
            self.pending=self._copy();self.condition.notify_all()

    def transition(self,evidence):
        if evidence.get('verified') is not True:return
        with self.condition:
            if not self.active:return
            run=self.active;tid=evidence.get('to')
            track=next((r for r in reversed(run['played']) if r['id']==tid),None)
            if not track or len(run['landed'])>=16:return
            key=str(evidence.get('from'))+':'+str(tid)+':'+str(evidence.get('completedAt'))
            identifier=hashlib.sha256(key.encode()).hexdigest()[:24]
            if any(row['id']==identifier for row in run['landed']):return
            at=run['durationSeconds']
            run['landed'].append({'id':identifier,'track':{k:track[k] for k in ('id','title','artist','artworkUrl') if k in track},
                'start':track['start'],'end':max(track['start'],at),'why':'The incoming recording and crossfader endpoint were confirmed.',
                'basis':'native handoff verification'})
            self._moment(at,'Handoff confirmed · '+track['title']+'.','native handoff verification')
            self.pending=self._copy();self.condition.notify_all()

    def boundary(self):
        """Seal before metadata reset. This never tries to stop or inspect audio."""
        with self.condition:
            if self.active:self._seal('interrupted',False)

    def _seal(self,status,confirmed):
        record=self._copy();self.active=None;self.pending=None
        self._queue_final(record,status,confirmed)

    def _queue_final(self,record,status,confirmed):
        if not record.get('played'):return
        ordinal=record.get('archiveOrdinal',0)
        if type(ordinal) is not int or ordinal<0:raise ValueError('Invalid recap ordering identity.')
        self.sequence=max(self.sequence,ordinal)
        record.update(status=status,endedAt=record['observedUntil'] if confirmed else None)
        for row in record['played']:
            row['start']=round(row['start'],3);row['end']=round(max(row['start'],row['end']),3)
        record['durationSeconds']=round(record['durationSeconds'],3)
        record['observedUntil']=record['startAt']+record['durationSeconds']
        if confirmed:record['endedAt']=record['observedUntil']
        if len(self.finalizing)>=16:
            self.error='Recap storage is behind. This run could not be queued for archival.';return
        self.finalizing.append(record)
        self.condition.notify_all()

    def _music(self,record):
        from afterhours.audio_analysis import REPORTS,read_scan,verified_energy
        root=Path(self.report_root) if self.report_root is not None else REPORTS
        features={};provenance=[]
        for tid,proof in record.get('proof',{}).items():
            if not any(row['id']==tid for row in record['played']):continue
            try:
                verified=verified_energy(tid.removeprefix('virtualdj:'),proof['duration'],root)
                read=read_scan(tid.removeprefix('virtualdj:'),root) if verified else None
                if not read or read[1]!=proof['digest'] or verified['reportDigest']!=proof['digest']:continue
                rows=read[0]['features'];features[tid]=([r['seconds'] for r in rows],rows)
                provenance.append({'trackId':tid,'reportDigest':proof['digest']})
            except (OSError,ValueError,KeyError,TypeError):continue
        points=[]
        for anchor in record.get('anchors',[]):
            data=features.get(anchor['trackId'])
            if not data:continue
            starts,rows=data;i=bisect.bisect_right(starts,anchor['position'])-1
            if i<0:continue
            row=rows[i]
            if not row['seconds']<=anchor['position']<row['seconds']+row['duration']:continue
            points.append({'t':round(anchor['t'],3),'e':round(max(0,min(5,(row['rmsDb']+60)/12)),4)})
        unique={r['t']:r for r in points};points=[unique[t] for t in sorted(unique)]
        if len(points)>512:points=[points[round(i*(len(points)-1)/511)] for i in range(512)]
        return ({'basis':'Verified source-recording loudness at observed program-deck positions; scale clamp((dBFS+60)/12,0,5), not master-output or room energy.',
                 'samples':points,'provenance':provenance} if points else None)

    def _finish(self,record):
        record=copy.deepcopy(record)
        record['music']=self._music(record);record['room']=None
        record['trackCount']=len({r['id'] for r in record['played']});record['intervalCount']=len(record['played'])
        record['capabilities']={'copyTracklist':True,'keepLesson':bool(record['lessons']),
                               'exportRecording':False,'previewMoment':False,'makeClips':False}
        lines=[record['title'],record['status'].capitalize()+' · native deck observations']
        for row in record['played']:
            def stamp(t):return f'{int(t)//60:02d}:{int(t)%60:02d}'
            lines.append(f"{stamp(row['start'])}–{stamp(row['end'])}{'' if row['endConfirmed'] else ' (last observed)'}  {row['artist']} — {row['title']}")
        text='\n'.join(lines).encode('utf-8')[:65536].decode('utf-8',errors='ignore')
        record['tracklist']={'format':'text/plain','text':text}
        record.pop('anchors',None);record.pop('proof',None)
        for lesson in record['lessons']:lesson['ruleId']='lesson:'+record['id']+':'+lesson['id']
        record['digest']=hashlib.sha256(json.dumps(record,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        return record

    @staticmethod
    def _summary(record):
        return {k:copy.deepcopy(record[k]) for k in ('id','revision','title','startAt','observedUntil','endedAt',
            'durationSeconds','status','trackCount','intervalCount')}

    @staticmethod
    def _validate(record):
        """Fail closed on a corrupt private file instead of publishing partial facts."""
        try:
            if (record['version']!=1 or not ID.fullmatch(record['id']) or record['revision']!=1
                    or type(record.get('archiveOrdinal',0)) is not int or record.get('archiveOrdinal',0)<0
                    or not isinstance(record['title'],str) or len(record['title'])>400
                    or record['status'] not in ('completed','stopped','interrupted')
                    or not all(finite(record[k]) for k in ('startAt','observedUntil','durationSeconds'))
                    or record['durationSeconds']<0
                    or abs(record['observedUntil']-record['startAt']-record['durationSeconds'])>.002
                    or (record['endedAt'] is None)!=(record['status']=='interrupted')):raise ValueError()
            if record['endedAt'] is not None and (not finite(record['endedAt']) or abs(record['endedAt']-record['observedUntil'])>.002):raise ValueError()
            played=record['played'];lessons=record['lessons']
            if not isinstance(played,list) or not 1<=len(played)<=MAX_INTERVALS or not isinstance(lessons,list) or len(lessons)>32:raise ValueError()
            seen=set()
            for row in played:
                if (metadata(row) is None or not isinstance(row['intervalId'],str) or row['intervalId'] in seen
                        or row['deck'] not in (1,2,3,4) or type(row['endConfirmed']) is not bool
                        or not finite(row['start']) or not finite(row['end'])
                        or not 0<=row['start']<=row['end']<=record['durationSeconds']+.002):raise ValueError()
                seen.add(row['intervalId'])
            from afterhours.learned_preferences import valid_effect
            for lesson in lessons:
                if (not re.fullmatch('[a-f0-9]{24}',lesson['id'])
                        or lesson['ruleId']!='lesson:'+record['id']+':'+lesson['id']
                        or not valid_effect(lesson['effect']) or not isinstance(lesson['title'],str)
                        or len(lesson['title'])>300 or not isinstance(lesson['detail'],str) or len(lesson['detail'])>800):raise ValueError()
            if record['trackCount']!=len({r['id'] for r in played}) or record['intervalCount']!=len(played):raise ValueError()
            if not isinstance(record['moments'],list) or len(record['moments'])>16 or not isinstance(record['landed'],list) or len(record['landed'])>16:raise ValueError()
            if record['room'] is not None or record['capabilities']!={'copyTracklist':True,'keepLesson':bool(lessons),'exportRecording':False,'previewMoment':False,'makeClips':False}:raise ValueError()
            if record['tracklist']['format']!='text/plain' or len(record['tracklist']['text'].encode('utf-8'))>65536:raise ValueError()
        except (KeyError,TypeError,ValueError,AttributeError):raise ValueError('The archived run is malformed or unsupported.') from None

    def _writer(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda:self.finalizing or self.pending is not None or self.closed,timeout=1)
                if self.closed and not self.finalizing and self.pending is None:return
                final=self.finalizing.pop(0) if self.finalizing else None
                pending=None if final else self.pending
                if not final:self.pending=None
                if final is None and pending is None:continue
                self.busy=True
            try:
                if final:
                    record=self._finish(final)
                    self._validate(record)
                    if self.path:
                        target=self.path/(record['id']+'.json')
                        if target.exists():
                            existing=self._read(target)
                            if existing.get('id')!=record['id']:raise ValueError('Archive identity mismatch.')
                            self._validate(existing)
                            record=existing
                        else:self._write(target,record)
                        # A stale finalized journal may be encountered after a
                        # crash. Persisting it again must not move Last night
                        # behind a newer run, even if the wall clock changed.
                        if record.get('archiveOrdinal',0)>self.last_ordinal:
                            self._write(self.path/'latest.json',{'id':record['id']})
                        journal=self.path/'active.json'
                        if journal.exists() and self._read(journal).get('id')==record['id']:journal.unlink()
                    with self.condition:
                        self.cache[record['id']]=record
                        while len(self.cache)>8:self.cache.pop(next(iter(self.cache)))
                        if record.get('archiveOrdinal',0)>self.last_ordinal:
                            self.summary=self._summary(record);self.last_ordinal=record.get('archiveOrdinal',0)
                        self.error=None
                elif self.path:self._write(self.path/'active.json',pending)
            except (OSError,ValueError,TypeError,KeyError):
                with self.condition:
                    self.error='The recap could not be saved. Music control is unchanged.'
                    # Keep failed work in bounded memory; storage retry has no
                    # transport effect and is throttled outside control locks.
                    if final and len(self.finalizing)<16:self.finalizing.insert(0,final)
                    if (pending is not None and self.pending is None and self.active is not None
                            and self.active['id']==pending['id']):self.pending=pending
                time.sleep(1)
                if self.closed:
                    with self.condition:self.finalizing.clear();self.pending=None
            finally:
                with self.condition:self.busy=False;self.condition.notify_all()

    def latest(self,settings_revision=0):
        with self.lock:return {**copy.deepcopy(self.summary),'settingsRevision':settings_revision} if self.summary else None

    def get(self,identifier):
        if not isinstance(identifier,str) or not ID.fullmatch(identifier):raise ValueError('Choose an exact archived run.')
        with self.lock:record=copy.deepcopy(self.cache.get(identifier))
        if record is None and self.path:
            try:record=self._read(self.path/(identifier+'.json'))
            except OSError:raise ValueError('That archived run is unavailable.') from None
        if record is None or record.get('id')!=identifier:raise ValueError('That archived run is unavailable.')
        self._validate(record)
        return record

    def flush(self,timeout=3):
        with self.condition:
            self.condition.notify_all()
            return self.condition.wait_for(lambda:not self.busy and not self.finalizing and self.pending is None,timeout)

    def close(self):
        self.boundary();self.flush()
        with self.condition:self.closed=True;self.condition.notify_all()
        self.worker.join(3)
