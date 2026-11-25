"""Bounded projection of tonight's intention and exact selected recordings.

No scheduling, provider access or transport lives here. Preparation requires
current numeric receipts; phase positions require a measured execution route.
"""
import copy
import hashlib
import json
import math
import re
from pathlib import Path

from afterhours.audio_analysis import REPORTS,verified_energy
from afterhours.program_timeline import BeatTime
from afterhours import cue_planner
from afterhours import dj_settings

MAX_PINS=12  # A decision can explicitly include/defer every pin within its bounds.


def finite(value):
    return type(value) in (int,float) and math.isfinite(value)


def exact_id(value):
    return isinstance(value,str) and re.fullmatch(r'virtualdj:[a-f0-9]{64}',value)


def played_pins(session):
    """Only observed completed positions fulfill an inclusion request."""
    index=session.get('index',-1)
    end=max(0,index)+(1 if session.get('phase')=='completed' and index>=0 else 0)
    played={t['id'] for t in session.get('order',[])[:end]}
    played.update(session.get('setPlayedPins',[]))
    return [tid for tid in session.get('setPins',[]) if tid in played]


def phase_windows(plan):
    """Actual seconds; shorter nights use proportionately shorter peak windows."""
    length=plan['endAt']-plan['startAt'];peak=plan['peakAt']-plan['startAt']
    before=min(1200,length/12);after=min(1800,length/8)
    warm=plan.get('warmLen')
    if not finite(warm):return None
    return [(0,warm),(warm,peak-before),(peak-before,peak+after),(peak+after,length)]


def peak_bounds(plan):
    length=plan['endAt']-plan['startAt']
    if plan.get('warmLen') is None:return plan['startAt']+1,plan['endAt']-1
    return (plan['startAt']+plan['warmLen']+min(1200,length/12),
            plan['endAt']-min(1800,length/8))


def validate_warm(plan,seconds):
    if not plan:raise ValueError('Plan a start, end and peak time before setting the warm-up length.')
    length=plan['endAt']-plan['startAt']
    maximum=plan['peakAt']-plan['startAt']-min(1200,length/12)
    if type(seconds) is not int or not 60<=seconds<=maximum:
        raise ValueError('The warm-up must last at least one minute and end before the peak window.')
    if plan['peakAt']+min(1800,length/8)>plan['endAt']:
        raise ValueError('The peak needs enough time to land before the end of this night.')
    return seconds


def planned_energy(plan,at):
    windows=phase_windows(plan)
    if not windows:return dj_settings.energy(plan,at)
    t=max(0,min(plan['endAt']-plan['startAt'],at-plan['startAt']))
    peak=plan['peakAt']-plan['startAt'];warm,build,crest,land=windows
    if t<warm[1]:e=1.7+.9*t/warm[1]
    elif t<build[1]:e=2.6+1.6*((t-build[0])/max(1,build[1]-build[0]))**(1.3 if plan['warm'] else .8)
    elif t<crest[1]:e=4.2+(.7 if plan['onePeak'] else .3)*math.exp(-((t-peak)/max(1,(crest[1]-crest[0])/2.7))**2)
    else:e=4.2-(1.6 if plan['onePeak'] else .4)*((t-land[0])/max(1,land[1]-land[0]))**.9
    if plan['darkLate'] and t>land[0]:e-=.4*(t-land[0])/max(1,land[1]-land[0])
    return max(.8,min(5,e))


def parse(text,plan,records,pins):
    """Strict Set commands. Unknown questions/title mentions do not mutate."""
    value=' '.join(text.strip().casefold().replace('’',"'").split())
    value=re.sub(r'[.!]+$','',value);value=re.sub(r'^(?:please |just )+','',value)
    if value in ('peak later','push the peak later','move the peak later','push the peak a bit later'):
        return {'kind':'peakShift','value':900}
    if value in ('peak earlier','bring the peak earlier','move the peak earlier'):
        return {'kind':'peakShift','value':-900}
    match=re.fullmatch(r'(?:set |make )?(?:the )?warm[- ]?up(?: length)?(?: to| for)? (\d{1,3}) (?:minutes?|mins?)',value)
    if match:return {'kind':'warmLength','value':int(match[1])*60}
    changes={'longer warm-up':900,'longer warm up':900,'make the warm-up longer':900,
             'shorter warm-up':-900,'shorter warm up':-900,'make the warm-up shorter':-900}
    if value in changes:
        if not plan or plan.get('warmLen') is None:raise ValueError('No warm-up length is saved yet. Use a duration such as “warm-up 45 minutes”.')
        return {'kind':'warmLength','value':plan['warmLen']+changes[value]}
    match=re.fullmatch(r'(pin|unpin) (.+?)(?: (?:for|in) (?:tonight|this set))?',value)
    if not match:return None
    verb,name=match.groups();name=name.strip('"“”')
    def normalized(raw):return ' '.join(str(raw or '').casefold().replace('’',"'").split())
    matches=[t for t in records if name in (normalized(t.get('title')),normalized(t.get('artist')),
              normalized(t.get('title'))+' by '+normalized(t.get('artist')))]
    if verb=='unpin':matches=[t for t in matches if t['id'] in pins]
    if len(matches)!=1:
        raise ValueError('No pin changed. Use the exact title and artist of one known recording; that name is missing or matches multiple versions.')
    return {'kind':'pin','value':{'trackId':matches[0]['id'],'on':verb=='pin'}}


class PreparedEvidence:
    def __init__(self,root=REPORTS):self.root=Path(root);self.cache={}
    def valid(self,track):
        tid=track.get('id');sid=tid.removeprefix('virtualdj:') if exact_id(tid) else None
        cues=track.get('cueCandidates') or [];energy=track.get('energyMap') or {}
        if not finite(track.get('duration')) or energy.get('coverage')!=1:return False
        def valid_cue(c):
            return (isinstance(c,dict) and c.get('replayVerified') is True and
                all(finite(c.get(k)) for k in ('entryBeat','exitBeat','entrySeconds','introEndSeconds','exitSeconds','endSeconds','recordingDuration')) and
                -10000<=c['entryBeat']<c['exitBeat']<=100000 and
                0<=c['entrySeconds']<c['introEndSeconds']<=c['exitSeconds']<c['endSeconds']<=track['duration'] and
                abs(c['recordingDuration']-track['duration'])<=.1)
        if not sid or not cues or not any(valid_cue(c) for c in cues):return False
        try:
            paths=[self.root/(sid+suffix) for suffix in ('.json','.verified.json')]
            if any(p.is_symlink() for p in paths):return False
            stamps=tuple((p.stat().st_mtime_ns,p.stat().st_size) for p in paths)
            key=(stamps,track['duration'],energy.get('reportDigest'),
                 hashlib.sha256(json.dumps(cues,sort_keys=True,separators=(',',':')).encode()).hexdigest())
            if (self.cache.get(tid) or (None,))[0]==key:return self.cache[tid][1]
            measured=verified_energy(sid,track['duration'],self.root)
            valid=bool(measured and measured['reportDigest']==energy.get('reportDigest'))
            self.cache[tid]=(key,valid)
            while len(self.cache)>128:self.cache.pop(next(iter(self.cache)))
            return valid
        except (OSError,ValueError,TypeError,KeyError):return False


def execution_offsets(session,plan,order,execution,prepared,now):
    cues=execution.get('selections',[]);ids=[t['id'] for t in order]
    if [c.get('trackId') for c in cues]!=ids or not set(ids)<=prepared:return {}
    base=session.get('startedAt')
    if not finite(base):
        if now>plan['startAt']:return {}  # A missed start is not an observed start.
        base=plan['startAt']
    route=execution.get('tempoSchedule')
    if route is not None and [r.get('trackId') for r in route]!=ids:return {}
    route=route or [{} for _ in order]
    if len(route)!=len(order):return {}
    observed={r.get('to'):r for r in session.get('transitions',[]) if finite(r.get('at'))}
    handoff=session.get('activeHandoff') or {}
    if finite(handoff.get('at')):observed[handoff.get('to')]=handoff
    result={};cursor=base-plan['startAt'];length=plan['endAt']-plan['startAt']
    held=(session.get('copilot') or {}).get('held') or session.get('phase') in ('takeover','stopped','error')
    index=session.get('index',-1);started=finite(session.get('startedAt'))
    future_known=not started
    try:
        for i,(track,cue,row) in enumerate(zip(order,cues,route)):
            actual=observed.get(track['id'])
            if actual:cursor=actual['at']-plan['startAt']
            if i>max(0,index) and (held or (not actual and (not future_known or plan['startAt']+cursor<=now))):break
            if 0<=cursor<=length:result[track['id']]=round(cursor,3)
            if not all(finite(cue.get(k)) for k in ('entryBeat','exitBeat')):return {}
            if not -10000<=cue['entryBeat']<cue['exitBeat']<=100000:return {}
            clock=BeatTime(cue,row,execution['targetBpm'])
            entry_beat=cue['entryBeat']
            startup=session.get('startup') or {}
            if (i==0 and startup.get('status')=='ready' and startup.get('openingId')==track['id']
                    and finite(startup.get('entryBeat'))):
                # Fast-start plays the beginning before the cue plan exists.
                # Include those actually heard beats in its planned interval.
                if startup['entryBeat']>entry_beat or clock.length:return {}
                entry_beat=startup['entryBeat']
            exit_beat=cue['exitBeat']
            timing=session.get('timing') or {}
            if i==session.get('index') and finite(timing.get('mixAtBeat')):exit_beat=timing['mixAtBeat']
            span=clock.seconds(exit_beat)-clock.seconds(entry_beat)
            if not finite(span) or span<=0:return {}
            cursor+=span
            if started and i==index and not held:
                # startedAt predates any pauses. Future starts need an exact
                # fresh playing clock, otherwise leave those positions absent.
                decks=[d for d in (session.get('telemetry') or {}).get('decks',[])
                    if d.get('id')==track['id'] and d.get('deck') in (1,2)
                    and d.get('loaded') is True and d.get('playing') is True]
                if len(decks)!=1:continue
                deck=decks[0];beat=deck.get('beatPosition');bpm=deck.get('bpm')
                sampled=deck.get('sampledAt',(session.get('telemetry') or {}).get('sampledAt'))
                if (session.get('active') is not True or session.get('phase')!='playing'
                        or not finite(sampled) or not -2<=now-sampled<3
                        or not finite(beat) or not entry_beat<=beat<exit_beat
                        or not finite(bpm) or not 40<=bpm<=300):continue
                if timing.get('basis')=='operator-steered-measured-exit':
                    remaining=(exit_beat-beat)*60/bpm
                else:
                    progress=max(0,min(1,(beat-cue['entryBeat']-clock.start)/clock.length)) if clock.length else 0
                    expected=clock.before+(clock.after-clock.before)*progress
                    if abs(bpm-expected)>.2:continue
                    remaining=clock.seconds(exit_beat)-clock.seconds(beat)
                future_at=sampled+remaining
                if finite(future_at) and future_at>now:
                    cursor=future_at-plan['startAt'];future_known=True
    except (KeyError,ValueError,TypeError):return {}
    return result


class Projection:
    def __init__(self,report_root=REPORTS):self.prepared=PreparedEvidence(report_root)
    def snapshot(self,settings,session,direction,direction_revision,now):
        queued=session.get('queuedUpdate') or {};order=queued.get('tracks') or session.get('order',[])
        pins=settings.state.get('pins',[])
        known={t['id']:t for t in session.get('candidates',[])}
        chosen={t['id']:t for t in order};chosen.update({tid:known[tid] for tid in pins if tid in known})
        # Keep pin controls accessible even if a future larger queue exceeds the
        # metadata fan's bound. Counts still describe the full chosen set.
        pinned=set(pins)&chosen.keys()
        kept=set(list(tid for tid in chosen if tid not in pinned)[:max(0,64-len(pinned))])|pinned
        shortlist=[track for tid,track in chosen.items() if tid in kept]
        blocked={r['trackId'] for r in session.get('recordingAvailability',[])}
        reports=[session.get('analysis') or {},(queued.get('prepared') or {}).get('analysis') or {}]
        evidence={}
        for report in reports:
            if (report.get('cueVersion')==cue_planner.VERSION
                    and report.get('secondsBetweenEntries')==session.get('secondsPerTrack')
                    and report.get('fingerprint')==cue_planner.fingerprint(report.get('tracks',[]))):
                evidence.update({r['id']:r for r in report.get('tracks',[])})
        prepared={tid for tid in chosen if tid not in blocked and tid in evidence and self.prepared.valid(evidence[tid])}
        plan=settings.state['plan'];offsets={}
        if plan:
            execution=((queued.get('prepared') or {}).get('executionPlan') if queued.get('status')=='ready' else session.get('executionPlan')) or {}
            execution_order=order if queued.get('status')=='ready' else session.get('order',[])
            offsets=execution_offsets(session,plan,execution_order,execution,prepared,now)
        def row(t):
            result={k:copy.deepcopy(t[k]) for k in ('id','title','artist','artworkUrl','bpm') if k in t}
            result.update(prepared=t['id'] in prepared)
            if t['id'] in offsets:result['plannedOffset']=offsets[t['id']]
            return result
        status=('preparing' if session.get('active') and session.get('phase')=='analyzing' or
                (session.get('backgroundAnalysis') or {}).get('status')=='analyzing' else
                'blocked' if queued.get('status')=='blocked' else 'idle')
        result={'version':1,'expected':settings.expected(session,direction_revision),'brief':direction,
            'preparation':{'shortlistCount':len(chosen),'preparedCount':len(prepared),'status':status},
            'shortlist':[row(t) for t in shortlist],'phases':[],'pins':[{'trackId':tid,
                **({'plannedOffset':offsets[tid]} if tid in offsets else {})} for tid in pins],
            'energySamples':[]}
        if not plan:return result
        low,high=peak_bounds(plan)
        result['schedule']={k:plan[k] for k in ('startAt','endAt','peakAt','warmLen') if k in plan}
        result['schedule'].update(peakMinAt=low,peakMaxAt=high)
        length=plan['endAt']-plan['startAt'];step=max(50,math.ceil(length/510))
        points=sorted(set([0,length,plan['peakAt']-plan['startAt']]+list(range(0,math.ceil(length),step))))
        result['energySamples']=[{'offset':t,'energy':round(planned_energy(plan,plan['startAt']+t),3)} for t in points]
        windows=phase_windows(plan)
        if windows and all(0<=a<=b<=length for a,b in windows):
            names=(('warm','Warm up'),('build','Build'),('peak','Peak'),('land','Land'))
            for (identifier,name),(a,b) in zip(names,windows):
                tracks=[t for t in chosen.values() if t['id'] in offsets and a<=offsets[t['id']]<b]
                bpms=[t['bpm'] for t in tracks if finite(t.get('bpm')) and 40<=t['bpm']<=300]
                energies=[p['energy'] for p in result['energySamples'] if a<=p['offset']<=b]
                phase={'id':identifier,'name':name,'start':a,'end':b,'records':[row(t) for t in tracks[:8]],
                    'more':max(0,len(tracks)-8),'note':'Planned placement from the prepared queue.' if tracks else 'Track placement awaits prepared timing.'}
                if energies:phase['energyText']=f'Planned energy {min(energies):.1f}–{max(energies):.1f}'
                if bpms:phase['bpmText']=f'{min(bpms):g}–{max(bpms):g} BPM'
                result['phases'].append(phase)
        return result
