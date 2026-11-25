"""Bounded tempo routes and native beat-timed ramps. No provider calls.

The agent may request a tempo for each track's outgoing handoff. The engine
validates playable overlap, measured space outside blends, and slew limits.
Legacy plans retain their fixed tempo until deliberately prepared again.
"""
import math
from afterhours.dj_bridge import BridgeError, as_bool
from afterhours.vdj_catalog import identity

VERSION = 'afterhours.tempo.v1'
BPM_PER_BEAT = 1 / 32  # at most 2 BPM over sixteen bars
MAX_CHANGE = 4.0
SETTLE_BEATS = 8

class TempoRouteError(ValueError):
    def __init__(self, track, message):
        self.track = track
        super().__init__(message)

def finite(value, low, high):
    if type(value) not in (int,float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError('Invalid tempo plan value.')
    return float(value)

def schedule(order, cues, transitions, opening=None, limit=8, targets=None, frozen=None, _estimated=False):
    """Use exact measured spans, never ramp during a two-track overlap.

    frozen holds already heard/playing/cued rows when extending a running set.
    Explicit targets outside feasible space are held, never silently clipped.
    """
    finite(limit,0,8)
    if not order or len(cues)!=len(order):raise ValueError('Tempo planning needs every measured cue.')
    requests={}
    if targets is not None:
        if not isinstance(targets,list) or len(targets)>len(order):raise ValueError('Invalid tempo targets.')
        for row in targets:
            if not isinstance(row,dict) or row.get('trackId') not in {t['id'] for t in order} or row['trackId'] in requests:
                raise ValueError('Tempo targets must name distinct selected tracks.')
            requests[row['trackId']]=finite(row.get('bpm'),40,300)
    bpm=finite(opening if opening is not None else order[0]['bpm'],40,300)
    rows=[];frozen=frozen or []
    for i,track in enumerate(order):
        native=finite(track['bpm'],40,300);lo=native*(1-limit/100);hi=native*(1+limit/100)
        if not lo-1e-6<=bpm<=hi+1e-6:
            raise TempoRouteError(track,f'{track["title"]} needs {(bpm/native-1)*100:+.1f}% pitch at {bpm:g} BPM. Add intermediate tracks or use a separate set; the limit is {limit:g}%.')
        cue=cues[i];span=finite(cue['exitBeat']-cue['entryBeat'],8,4096)
        previous_blend=transitions[i-1]['fadeBeats'] if i else 0
        start=math.ceil((previous_blend+4)/4)*4
        beats=max(0,math.floor((span-SETTLE_BEATS-start)/4)*4)
        allowed=min(MAX_CHANGE,beats*BPM_PER_BEAT)
        if _estimated and i<len(frozen):
            # A running prefix was compiled against its actual cue/overlap.
            # The generic selection estimate must not replace those timings.
            allowed=min(MAX_CHANGE,finite(frozen[i].get('durationBeats'),0,4096)*BPM_PER_BEAT)
        next_track=order[i+1] if i+1<len(order) else None
        # The outgoing target must be playable by both recordings. The ramp
        # itself stays within this deck's pitch range at every point.
        if next_track:
            n=finite(next_track['bpm'],40,300)
            lo=max(lo,n*(1-limit/100));hi=min(hi,n*(1+limit/100))
        lo=max(lo,bpm-allowed);hi=min(hi,bpm+allowed)
        if lo>hi+1e-6:
            t=next_track or track
            raise TempoRouteError(t,f'{track["title"]} → {t["title"]} has no safe tempo route in this excerpt. Add an intermediate track or allow longer playback; {beats//4} bars permit at most {allowed:g} BPM of change.')
        desired=requests.get(track['id'], next_track['bpm'] if next_track else bpm)
        if i<len(frozen):
            row=dict(frozen[i])
            if row.get('trackId')!=track['id'] or abs(row['fromBpm']-bpm)>.001:
                raise ValueError('Protected tempo route changed.')
            desired=row['toBpm']
            if track['id'] in requests and abs(requests[track['id']]-desired)>.001:
                raise ValueError('The playing and cued tempo targets are protected.')
            frozen_duration=finite(row.get('durationBeats'),0,4096 if _estimated else beats)
            frozen_start=finite(row.get('startOffsetBeats'),0 if _estimated or not frozen_duration else start,4096 if _estimated else span)
            if (abs(desired-bpm)>frozen_duration*BPM_PER_BEAT+1e-6 or
                    not _estimated and frozen_start+frozen_duration>span-SETTLE_BEATS or row.get('maxPitchPercent')!=limit):
                raise ValueError('Protected tempo ramp is outside measured limits.')
            if not lo-1e-6<=desired<=hi+1e-6:raise TempoRouteError(next_track or track,'The new recording cannot join the protected tempo route. Keep the cued handoff and choose an intermediate track.')
            rows.append(row);bpm=desired;continue
        if track['id'] in requests and not lo-1e-6<=desired<=hi+1e-6:
            raise TempoRouteError(track,f'Requested {desired:g} BPM is outside the measured ramp for {track["title"]}; this excerpt permits {lo:.2f}–{hi:.2f} BPM.')
        target=round(max(lo,min(hi,desired)),6)
        change=target-bpm
        # Native BPM readings are floats. Rounding a target to six decimals
        # can put an exact bar-length ramp a fraction of a micro-BPM above
        # its boundary; do not invent another four beats for that noise.
        ramp_beats=math.ceil((abs(change)-1e-6)/(BPM_PER_BEAT*4))*4 if abs(change)>.02 else 0
        if ramp_beats==0:target=bpm
        if ramp_beats>beats:raise ValueError('Tempo ramp exceeds the available measured body.')
        rows.append({'trackId':track['id'],'fromBpm':round(bpm,6),'toBpm':target,
            'startOffsetBeats':start,'durationBeats':ramp_beats,'settleBeats':SETTLE_BEATS,
            'maxPitchPercent':limit,'reason':('Hold tempo through the handoff.' if not ramp_beats else
            f'Gradually move {bpm:.2f} → {target:.2f} BPM over {ramp_beats//4} bars, outside the blend.')})
        bpm=target
    return rows

def estimate(order,seconds=30,limit=8,targets=None,opening=None,frozen=None):
    # Selection-time feasibility; compilation rechecks actual measured spans.
    from afterhours.cue_planner import excerpt_beats
    opening=opening if opening is not None else order[0]['bpm']
    span=excerpt_beats(seconds,opening);blend=min(32 if seconds<=30 else 64,span//2)
    cues=[{'entryBeat':0,'exitBeat':span} for t in order]
    return schedule(order,cues,[{'fadeBeats':blend} for _ in order[1:]],opening,limit,targets,frozen,_estimated=True)

class TempoRamp:
    """One named native ramp; readbacks detect drift/takeover, never catch up."""
    NAME='afterhours_tempo'
    def __init__(self,q,send,event=lambda *a,**k:None):
        self.q=q;self.send=send;self.event=event;self.running=False;self.done=False
        self.row=None;self.samples=[]
    def arm(self,track,deck,row,entry):
        self.track=track;self.deck=deck;self.row=dict(row);self.entry=entry
        self.running=False;self.done=not row['durationBeats'];self.samples=[]
    def tick(self,client,current,incoming):
        if not self.row:return None
        row=self.row;beat=current['beatPosition'];start=self.entry+row['startOffsetBeats']
        if current.get('id')!=self.track['id'] or not current.get('playing'):
            raise BridgeError('Tempo scheduling stopped after a deck or transport change.')
        if self.running and incoming and incoming.get('playing'):
            self.cancel(client);raise BridgeError('The other deck started during a solo tempo ramp. Scheduling stopped.')
        if self.done:
            if abs(current['bpm']-row['toBpm'])>.2:raise BridgeError('Tempo changed outside the prepared route. Take over or prepare again.')
            return {'status':'complete','targetBpm':row['toBpm']}
        if not self.running:
            if abs(current['bpm']-row['fromBpm'])>.2:raise BridgeError('Playing tempo changed before its ramp. No tempo command sent.')
            if beat<start:return {'status':'waiting','targetBpm':row['toBpm']}
            if beat-start>2:raise BridgeError('The tempo ramp window was missed. No compressed ramp was sent.')
            if incoming and incoming.get('playing'):raise BridgeError('Tempo ramps require a solo playing deck.')
            if as_bool(self.q(client,'pitch_lock','bool')):raise BridgeError('Turn off linked pitch before the set; the tempo ramp did not change it.')
            native=finite(self.q(client,f'deck {self.deck} get_bpm absolute'),40,300)
            ratio=row['toBpm']/native
            if abs(ratio-1)>row['maxPitchPercent']/100+1e-6:raise BridgeError('Native tempo refinement exceeds the ramp pitch limit.')
            width=finite(self.q(client,f'deck {self.deck} pitch_range'),.001,1)
            slider=finite(self.q(client,f'deck {self.deck} pitch'),0,1)
            goal=.5+(ratio-1)/(2*width)
            if not 0<=goal<=1:raise BridgeError('The native pitch slider range cannot reach the planned tempo.')
            if identity(str(self.q(client,f'deck {self.deck} get_filepath','text')))!=self.track['engine']['selection_id']:
                raise BridgeError('The ramp recording changed.')
            # Native timing continues smoothly between network polls. Register
            # ownership BEFORE dispatch so errors/takeover always cancel it.
            self.running=True;self.start=beat
            # Bind the repeat's beat clock as well as its pitch action to the
            # playing deck; background catalog/loading can change selection.
            self.send(client,f"deck {self.deck} repeat_start_fade '{self.NAME}' {slider:.9f} {goal:.9f} {row['durationBeats']}bt & deck {self.deck} pitch")
            self.event(row['reason'],'tempo')
        progress=max(0,min(1,(beat-self.start)/row['durationBeats']))
        expected=row['fromBpm']+(row['toBpm']-row['fromBpm'])*progress
        if abs(current['bpm']-expected)>.45:
            self.cancel(client);raise BridgeError('Native tempo diverged from its ramp. Scheduling stopped for manual control.')
        self.samples.append({'beat':beat,'bpm':current['bpm']});self.samples=self.samples[-64:]
        if beat>=self.start+row['durationBeats']:
            if abs(current['bpm']-row['toBpm'])>.03:
                if beat>self.start+row['durationBeats']+2:
                    self.cancel(client);raise BridgeError('Tempo ramp endpoint was not confirmed. No transition was started.')
            else:
                self.cancel(client);self.done=True
                self.event(f'Tempo confirmed at {current["bpm"]:.2f} BPM.','tempo-complete')
        return {'status':'complete' if self.done else 'ramping','fromBpm':row['fromBpm'],
                'targetBpm':row['toBpm'],'progress':progress,'observedBpm':current['bpm']}
    def cancel(self,client):
        if self.running:
            # Cleanup must work even after the session cancellation flag is set.
            client.send(f"repeat_stop '{self.NAME}'")
            self.running=False
