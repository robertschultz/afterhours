"""Display-only projection of measured excerpts onto one continuous set clock.

Future placements are planned; completed/active handoffs anchor their observed
starts. This never schedules audio and never calls an engine or model.
"""
import math
import copy
import re
import time
from functools import lru_cache
from afterhours.audio_analysis import read_scan,verified_energy


def finite(value):
    return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)


@lru_cache(maxsize=32)
def detailed_contour(track_id,digest):
    """Cache only digest-matched numeric features; no PCM or provider access."""
    measured=read_scan(track_id.removeprefix('virtualdj:'))
    if not measured or measured[1]!=digest:return ()
    return tuple((r['seconds'],r['duration'],r['rmsDb']) for r in measured[0]['features'])


def bounded(value, low, high):
    if not finite(value) or not low <= value <= high:
        raise ValueError('Invalid timeline measurement.')
    return value


class BeatTime:
    """Integrate seconds over the prepared beat-linear native pitch ramp.

    This is a display estimate of a validated route, not an audio scheduler or
    a measured wall clock. In particular, it must not re-time a mix command.
    """
    def __init__(self, cue, row, opening):
        self.entry=cue['entryBeat']
        self.before=bounded(row.get('fromBpm',opening),40,300)
        self.after=bounded(row.get('toBpm',opening),40,300)
        self.start=bounded(row.get('startOffsetBeats',0),0,4096)
        self.length=bounded(row.get('durationBeats',0),0,4096)
        if not self.length and abs(self.before-self.after) > 1e-6:
            raise ValueError('An instantaneous tempo change has no display route.')
        if self.length and self.start+self.length > cue['exitBeat']-self.entry+1e-5:
            raise ValueError('Tempo ramp extends past the measured body.')

    def seconds(self, beat):
        offset=beat-self.entry
        if offset <= self.start:
            return offset*60/self.before
        ramp=min(offset-self.start,self.length)
        change=self.after-self.before
        if self.length and abs(change)>1e-9:
            # log1p remains accurate for very small positive/negative ramps.
            ramp_seconds=60*self.length/change*math.log1p(change*ramp/(self.length*self.before))
        else:
            ramp_seconds=ramp*60/self.before
        return self.start*60/self.before+ramp_seconds+max(0,offset-self.start-self.length)*60/self.after


def source_beat(cue, seconds):
    """Piecewise mapping through native cue readbacks, including the outro."""
    if seconds <= cue['exitSeconds']:
        a,b='entry','exit'
        beat_span=cue['exitBeat']-cue['entryBeat']
    else:
        a,b='exit','end'
        beat_span=cue['maxBlendBeats']
    return cue[a+'Beat']+(seconds-cue[a+'Seconds'])*beat_span/(cue[b+'Seconds']-cue[a+'Seconds'])


def clip_contour(rows, start, end):
    result=[]
    for at,duration,db in rows:
        left=max(start,at);right=min(end,at+duration)
        if right>left:result.append([left,right-left,db])
    return result


def display_segments(segments):
    """A single ownership lane; preserve overlapping audible intervals above.

    Ownership switches halfway through the projected blend. It describes
    presentation only, not a new cue, a measured crossfader curve or output PCM.
    """
    boundaries=[segments[0]['startSeconds']]
    boundaries.extend((a['endSeconds']+b['startSeconds'])/2 for a,b in zip(segments,segments[1:]))
    boundaries.append(segments[-1]['endSeconds'])
    result=[]
    for item,left,right in zip(segments,boundaries,boundaries[1:]):
        if right<=left:raise ValueError('Invalid timeline ownership interval.')
        result.append({**item,'startSeconds':left,'endSeconds':right,
                       'audibleStartSeconds':item['startSeconds'],'audibleEndSeconds':item['endSeconds'],
                       'contour':clip_contour(item['contour'],left,right),'timing':'display-ownership'})
    return result


def held_display(rows, current, position):
    """Leave room for a held handoff without inventing an audio schedule.

    Only display ownership moves. Measured audible intervals, cues and source
    plan remain untouched; the blank extension has no fabricated amplitude.
    Queued intervals are provisional until Go selects a verified exit.
    """
    result=copy.deepcopy(rows)
    extension=max(0,position+60-result[current]['endSeconds'])
    result[current]['endSeconds']+=extension
    result[current]['timing']='held-open'
    for row in result[current+1:]:
        row['startSeconds']+=extension;row['endSeconds']+=extension
        row['contour']=[[at+extension,duration,db] for at,duration,db in row['contour']]
        row['timing']='unscheduled-while-held'
    return result


def set_timeline(state, deck):
    """Reject incomplete projections rather than publishing plausible fake data."""
    if not state.get('active') and (state.get('phase')=='takeover' or deck.get('playing')):
        observed=observed_deck_timeline(state,deck)
        if observed is not None:return observed
    try:
        projected=project_timeline(state,deck)
        if projected is not None:return projected
    except (KeyError,TypeError,ValueError,ZeroDivisionError):
        pass
    return observed_deck_timeline(state,deck)


def observed_deck_timeline(state,deck):
    """A manual track still has a real playhead, without a prepared set route.

    Use native duration/position and only receipt-verified cached amplitudes.
    No next-track timing, invented waveform, transport or scanning is involved.
    """
    controls=deck.get('controls') or {};now=time.time()
    sid=deck.get('id');duration=controls.get('durationMs');position=deck.get('position')
    sample=controls.get('sampledAt')
    if (not isinstance(sid,str) or not re.fullmatch(r'virtualdj:[a-f0-9]{64}',sid)
            or deck.get('loaded') is not True or deck.get('unavailable')
            or not finite(sample) or not -2<=now-sample<3
            or not finite(duration) or not 0<duration<=86_400_000
            or not finite(position) or not 0<=position<=duration/1000+.1):return None
    duration/=1000
    energy=verified_energy(sid.removeprefix('virtualdj:'),duration) or {}
    measured=detailed_contour(sid,energy['reportDigest']) if energy else ()
    contour=clip_contour(measured or [r[:3] for r in energy.get('contour',[])],0,duration)
    row={'trackId':sid,'title':deck.get('title','Untitled'),'artist':deck.get('artist',''),
         'deck':deck.get('deck',1),'startSeconds':0,'endSeconds':duration,
         'contour':contour,'timing':'observed-deck','waveformAvailable':bool(contour)}
    return {'runId':state.get('id','')+':manual:'+sid,'kind':'observed-deck',
            'positionSeconds':min(duration,position),'durationSeconds':duration,
            'timingBasis':'observed deck position; future mix not prepared',
            'segments':[row],'displaySegments':[row]}


def project_timeline(state, deck):
    plan=state.get('executionPlan') or {}
    cues=copy.deepcopy(plan.get('selections',[]))
    order=state.get('order',[])
    bpm=bounded(plan.get('targetBpm'),40,300)
    if not cues or len(cues)!=len(order):return None
    if [c.get('trackId') for c in cues]!=[t['id'] for t in order]:return None
    if len({t['id'] for t in order})!=len(order):return None
    # Operator retiming changes only the exit of the actually playing excerpt.
    # Resolve it back to replay-verified analysis; arbitrary timestamps in a
    # receipt must never become a plausible-looking future timeline.
    timings={v.get('from'):v.get('timing') for v in state.get('transitions',[])}
    active=state.get('activeHandoff') or {}
    if active:timings[active.get('from')]=active.get('timing')
    index=state.get('index',-1);current_timing=state.get('timing') or {}
    if (0<=index<len(cues) and current_timing.get('originalCandidateId') == cues[index].get('id')):
        timings[cues[index]['trackId']]=current_timing
    evidence={v['id']:v for v in (state.get('analysis') or {}).get('tracks',[])}
    for cue in cues:
        timing=timings.get(cue['trackId']) or {}
        if timing.get('basis') != 'operator-steered-measured-exit':continue
        chosen=next((c for c in evidence.get(cue['trackId'],{}).get('cueCandidates',[])
                     if c.get('id') == timing.get('candidateId') and c.get('replayVerified') is True),None)
        if (not chosen or timing.get('originalCandidateId') != cue.get('id')
                or timing.get('mixAtBeat') != chosen.get('exitBeat')):return None
        cue.update({k:chosen[k] for k in ('exitBeat','exitSeconds','endSeconds','maxBlendBeats')})
    recipes=plan.get('transitions',[])
    if len(recipes)<len(order)-1:return None
    route=plan.get('tempoSchedule')
    if route is not None and (len(route)!=len(order) or
            [r.get('trackId') for r in route]!=[t['id'] for t in order]):return None
    startup=state.get('startup') or {}
    if (startup.get('status')=='ready' and startup.get('openingId')==cues[0]['trackId']
            and finite(startup.get('entryBeat')) and finite(startup.get('entrySeconds'))):
        # Display the whole audible opening, including the time before its mix
        # plan existed. Scheduling still uses the original verified exit cue.
        if (startup['entryBeat']>cues[0]['entryBeat'] or startup['entrySeconds']>cues[0]['entrySeconds']
                or not route or route[0]['durationBeats']):return None
        cues[0].update(entryBeat=startup['entryBeat'],entrySeconds=startup['entrySeconds'])
    clocks=[];previous_bpm=bpm
    for i,cue in enumerate(cues):
        for field in ('entryBeat','exitBeat'):
            bounded(cue.get(field),-10000,100000)
        for field in ('entrySeconds','exitSeconds','endSeconds'):
            bounded(cue.get(field),0,86400)
        bounded(cue.get('maxBlendBeats'),1,4096)
        if cue['exitBeat']<=cue['entryBeat'] or not cue['entrySeconds']<cue['exitSeconds']<cue['endSeconds']:
            return None
        clock=BeatTime(cue,route[i] if route else {},bpm)
        if abs(clock.before-previous_bpm)>.001:return None
        previous_bpm=clock.after
        clocks.append(clock)
    measured={t['id']:t.get('energyMap') for t in (state.get('analysis') or {}).get('tracks',[])}
    observed={(v['from'],v['to']):v for v in state.get('transitions',[])}
    active=state.get('activeHandoff')
    if active:observed[(active['from'],active['to'])]=active
    segments=[];anchors=[]
    start=0
    for i,(track,cue,clock) in enumerate(zip(order,cues,clocks)):
        handoff=observed.get((order[i-1]['id'],track['id']),{}) if i else {}
        entry=handoff.get('incomingCueBeat',cue['entryBeat'])
        if not finite(entry):entry=cue['entryBeat']
        bounded(entry,cue['entryBeat']-2,cue['entryBeat']+2)
        if i:
            beat=handoff.get('observedStartBeat',cues[i-1]['exitBeat'])
            if not finite(beat):beat=cues[i-1]['exitBeat']
            start=segments[-1]['startSeconds']+clocks[i-1].seconds(beat)-anchors[-1]
        offset=clock.seconds(entry);anchors.append(offset)
        fade=bounded(recipes[i]['fadeBeats'],0,cue['maxBlendBeats']) if i<len(order)-1 else 0
        outgoing=observed.get((track['id'],order[i+1]['id']),{}) if i<len(order)-1 else {}
        mix_beat=outgoing.get('observedStartBeat',cue['exitBeat'])
        if not finite(mix_beat):mix_beat=cue['exitBeat']
        bounded(mix_beat,cue['exitBeat']-2,cue['exitBeat']+2)
        mix_at=start+clock.seconds(mix_beat)-offset
        end=start+clock.seconds(mix_beat+fade)-offset
        if end<=start or i and start<=segments[-1]['startSeconds']:return None
        rows=[];energy=measured.get(track['id']) or {}
        if energy.get('coverage')==1:
            contour=detailed_contour(track['id'],energy.get('reportDigest')) or energy.get('contour',[])
            for row in contour:
                if not isinstance(row,(list,tuple)) or len(row)<3 or not all(finite(v) for v in row[:3]) or row[1]<=0:continue
                left=max(cue['entrySeconds'],row[0]);right=min(cue['endSeconds'],row[0]+row[1])
                if right<=left:continue
                a=max(start,start+clock.seconds(source_beat(cue,left))-offset)
                b=min(end,start+clock.seconds(source_beat(cue,right))-offset)
                if b>a:rows.append([a,b-a,row[2]])
        segments.append({'trackId':track['id'],'title':track.get('title','Untitled'),
                         'artist':track.get('artist',''),'deck':i%2+1,
                         'startSeconds':start,'endSeconds':end,'mixAtSeconds':mix_at,
                         'entryBeat':entry,'contour':rows,
                         'placement':'observed' if i==0 and state.get('startedAt') or handoff else 'planned'})
    position=0
    current=next((i for i,v in enumerate(segments) if v['trackId']==deck.get('id')),None)
    if state.get('startedAt') and plan.get('applied') and current is not None and finite(deck.get('beatPosition')):
        position=segments[current]['startSeconds']+clocks[current].seconds(deck['beatPosition'])-anchors[current]
    held=(state.get('active') is True and state.get('phase')=='playing'
          and (state.get('copilot') or {}).get('held') is True and current is not None)
    display=display_segments(segments)
    if held:display=held_display(display,current,position)
    return {'version':1,'runId':f"{state['id']}:{state.get('startedAt') or 'prepared'}",
            'positionSeconds':max(0,position if held else min(segments[-1]['endSeconds'],position)),
            'durationSeconds':display[-1]['endSeconds'],'segments':segments,
            'displaySegments':display,
            'timingBasis':'prepared-tempo-route' if route else 'fixed-prepared-tempo',
            'displayBasis':('held handoff; queued placements are unscheduled until a verified exit is selected' if held
                            else 'one track per interval; ownership changes at the projected blend midpoint'),
            'basis':'measured recording energy; future placements are planned, not master-output audio'}
