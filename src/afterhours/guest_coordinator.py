"""Guest outcomes from cached engine facts, outside the audio control lane.

The agent reads a small memory cache and submits bounded planner events. This
worker alone reconciles those events and exact queue/program facts to the guest
ledger. It cannot call transport, a provider, or authorize an agent allowance.
"""
from collections import deque
import copy
import math
import sqlite3
import threading
import time

from afterhours.guest_requests import GuestError,exact_id,plain,safe_art,track_ref
from afterhours.recording_availability import PREVIEW_ONLY


def finite(value):return type(value) in (int,float) and math.isfinite(value)


def public_facts(snapshot,profile,now):
    facts={'nightId':snapshot['id'],'night':{},'performer':{
        'id':plain(profile.get('id'),100) or 'dj','name':plain(profile.get('name'),100) or 'The DJ'},'program':None}
    # Current source has no saved venue/name contract; never use demo names or
    # private free-form operator direction as public venue metadata.
    art=safe_art(profile.get('imageUrl'))
    if art:facts['performer']['imageUrl']=art
    telemetry=snapshot.get('telemetry') or {};stamp=telemetry.get('sampledAt')
    if not finite(stamp) or not -2<=now-stamp<3:return facts,None
    cross=telemetry.get('crossfader')
    if not finite(cross) or not 0<=cross<=1:return facts,None
    known={t['id']:t for t in snapshot.get('candidates',[])+snapshot.get('order',[])}
    decks=[]
    for deck in telemetry.get('decks',[]):
        if deck.get('deck') not in (1,2) or deck.get('loaded') is not True or deck.get('unavailable'):continue
        if not exact_id(deck.get('id')):continue
        observed=deck.get('sampledAt',stamp)
        if not finite(observed) or not -2<=now-observed<3:continue
        controls=deck.get('controls') or {};control_stamp=controls.get('sampledAt');volume=controls.get('volume')
        control_fresh=finite(control_stamp) and -2<=now-control_stamp<3 and finite(volume) and 0<=volume<=1
        gain=(1-cross if deck['deck']==1 else cross)*(volume if control_fresh else 0)
        playing=deck.get('playing') is True and control_fresh and gain>.01
        source=known.get(deck['id']) or deck
        try:track=track_ref(source)
        except GuestError:continue
        decks.append((playing,gain,deck,track))
    if not decks:return facts,None
    playing,gain,deck,track=max(decks,key=lambda row:(row[0],row[1]))
    program={'track':track,'playing':playing,'sampledAt':deck.get('sampledAt',stamp)}
    position=deck.get('position');duration=(deck.get('controls') or {}).get('durationMs')
    if finite(position) and 0<=position<=86400:program['positionSeconds']=position
    if finite(duration) and 0<duration<=86400000:program['durationSeconds']=duration/1000
    facts['program']=program
    active=(snapshot.get('active') is True and snapshot.get('phase')=='playing'
            and (snapshot.get('copilot') or {}).get('mode')=='leading'
            and not (snapshot.get('copilot') or {}).get('held'))
    index=snapshot.get('index',-1);order=snapshot.get('order',[])
    plan=snapshot.get('executionPlan') or {};pending=snapshot.get('queuedUpdate') or {}
    if (playing and active and plan.get('applied') is True and type(index) is int and 0<=index<len(order)-1
            and order[index]['id']==track['id'] and pending.get('status')!='blocked'):
        candidate=order[index+1]
        if any(d.get('id')==candidate['id'] and d.get('loaded') is True and d.get('playing') is False
               and d.get('deck') in (1,2) for d in telemetry.get('decks',[])):
            facts['next']={'track':track_ref(candidate)}
    return facts,track['id'] if playing else None


def verdicts(value,candidates,selected):
    if value is None:return []
    if not isinstance(value,list) or len(value)>8:raise ValueError('Guest request decisions exceeded their bound.')
    supplied={r['requestId']:r['trackId'] for r in candidates};seen=set();result=[]
    for row in value:
        if (not isinstance(row,dict) or set(row)!={'requestId','trackId','verdict'}
                or not isinstance(row['requestId'],str) or row['requestId'] in seen
                or supplied.get(row['requestId'])!=row['trackId'] or row['verdict'] not in ('wait','decline')
                or row['trackId'] in selected):
            raise ValueError('A guest verdict was not bound to this exact supplied request.')
        seen.add(row['requestId']);result.append(copy.deepcopy(row))
    return result


class GuestCoordinator:
    def __init__(self,ledger,session,profile=lambda:{},clock=time.time):
        self.ledger=ledger;self.session=session;self.profile=profile;self.clock=clock
        self.lock=threading.RLock();self.events=deque(maxlen=128);self.wake_event=threading.Event();self.stop_event=threading.Event()
        self.thread=None;self._pending=[];self._facts={};self._night=None;self.error=None;self.polls=None
    def night(self):
        # Copy only the identity; no growing state, DB read or native query.
        with self.session.lock:return self.session.state['id']
    def wake(self):self.wake_event.set()
    def context(self):
        with self.lock:return copy.deepcopy(self._facts)
    def pending(self,night):
        with self.lock:return copy.deepcopy(self._pending) if self._night==night else []
    def submit(self,kind,night,round_id,rows):
        with self.lock:
            if len(self.events)==self.events.maxlen:return False
            self.events.append((kind,night,round_id,copy.deepcopy(rows[:8])))
        self.wake();return True
    def start(self):
        if self.thread:return
        self.step();self.thread=threading.Thread(target=self.run,name='guest-request-ledger',daemon=True);self.thread.start()
    def close(self):
        self.stop_event.set();self.wake()
        if self.thread:self.thread.join(4)
    def run(self):
        while not self.stop_event.is_set():
            try:self.step();self.error=None
            except (ValueError,OSError,KeyError,TypeError,sqlite3.Error):self.error='Guest request reconciliation unavailable.'
            self.wake_event.wait(1);self.wake_event.clear()
    def step(self):
        snapshot=self.session.snapshot();night=snapshot['id']
        if self.night()!=night:return
        if self._night!=night:self.ledger.ensure_night(night)
        facts,playing=public_facts(snapshot,self.profile(),self.clock())
        selected={t['id'] for t in snapshot.get('order',[])+(snapshot.get('queuedUpdate') or {}).get('tracks',[])}
        selected.update(d.get('id') for d in (snapshot.get('telemetry') or {}).get('decks',[])
                        if d.get('deck') in (1,2) and d.get('loaded') is True)
        with self.lock:events=list(self.events);self.events.clear()
        for kind,event_night,round_id,rows in events:
            if event_night!=night:continue
            current={row['id']:row for row in self.ledger.reconciliation_rows(night)}
            for event in rows:
                row=current.get(event['requestId'])
                if not row or row['track_id']!=event['trackId']:continue
                if kind=='begin' and row['stage']=='sent':
                    self.ledger.stage(night,row['id'],'listening',round_id=round_id)
                elif kind=='finish' and row['round_id']==round_id:
                    # A validated decline can only reject an unselected future
                    # request. Queue/program reconciliation below takes priority.
                    decline=event.get('verdict')=='decline' and row['track_id'] not in selected
                    self.ledger.stage(night,row['id'],'declined' if decline else 'sent',
                        reason='not-tonight' if decline else None,require_round=round_id)
        queued=snapshot.get('queuedUpdate') or {};order=snapshot.get('order',[])
        index=snapshot.get('index',-1);index=index if type(index) is int else -1
        # Past selected titles aren't observed plays. Only future membership
        # can establish coming; actual A/B program evidence establishes playing.
        applied_future={t['id'] for t in order[max(0,index+1):]}
        pending_future={t['id'] for t in queued.get('tracks',[])[max(0,index+1):]}
        held=(queued.get('status')=='blocked' or snapshot.get('phase') in ('takeover','error')
              or (snapshot.get('copilot') or {}).get('mode')=='off'
              or (snapshot.get('copilot') or {}).get('held'))
        unavailable={r['trackId']:r for r in snapshot.get('recordingAvailability',[])}
        for row in self.ledger.reconciliation_rows(night):
            tid=row['track_id'];restriction=unavailable.get(tid)
            if restriction and restriction.get('code')==PREVIEW_ONLY and row['played_at'] is None:
                self.ledger.stage(night,row['id'],'declined',reason='unavailable')
            elif tid==playing:
                self.ledger.stage(night,row['id'],'playing',playing=True)
            elif row['played_at'] is not None:
                self.ledger.stage(night,row['id'],'playing',playing=False)
            elif not held and not restriction and (tid in applied_future or tid in pending_future):
                self.ledger.stage(night,row['id'],'coming')
            elif row['stage']=='coming':
                self.ledger.stage(night,row['id'],'sent',reason='preparing' if queued.get('status')=='blocked' else 'waiting')
        pending=self.ledger.pending(night,limit=256)
        rows=[{'requestId':r['id'],'trackId':r['track']['id'],'title':r['track']['title'],'artist':r['track']['artist'],
               'asks':r['asks']} for r in pending if r['stage']=='sent']
        with self.lock:self._night=night;self._facts=facts;self._pending=rows
        if self.polls:self.polls.step()
