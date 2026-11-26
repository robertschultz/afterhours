"""Measured excerpt candidates and identity-bound executable mix plans.

Numerical structure estimates are not vocal recognition or a musical audition.
All positions exposed to the model are measured again on VirtualDJ's native grid.
"""
import copy
import hashlib
import json
import math
import statistics
from afterhours import tempo_planner

from afterhours.audio_analysis import read_scan
from afterhours.vdj_mixing import transition_recipe


VERSION = 'afterhours.cues.v1'


class LimitedAudio(ValueError):
    """Verified decoded sound is only a short part of the advertised recording."""


def limited_audio(evidence, rows):
    # A catalog duration alone is not proof of a full playable recording. Some
    # provider loads expose a 30-second preview inside a full-length zero buffer.
    # Require the verified PCM and native sound-end marker to agree; do not
    # classify unknown/unscanned tracks or normal musical breaks as previews.
    duration=evidence.get('duration') or 0
    end=next((p['seconds'] for p in evidence.get('points',[]) if p['label']=='realEnd'),None)
    signal=[r for r in rows if r['rmsDb']>-55]
    observed=signal[-1]['seconds']+signal[-1]['duration'] if signal else 0
    if duration>90 and end is not None and 0<observed<=35 and abs(observed-end)<1 and observed<duration*.35:
        return f"{evidence['title']} has only {end:.0f} seconds of decoded audio available. It cannot fill this mix."
    return None


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def measured_rows(evidence):
    energy = evidence.get('energyMap')
    observed = read_scan(evidence['id'].removeprefix('virtualdj:'))
    if not energy or not observed or observed[1] != energy.get('reportDigest'):
        raise ValueError('Verified audio changed. Prepare track analysis again.')
    return observed[0]['features']


def window_stats(rows, start, end):
    if not (0 <= start < end) or end > rows[-1]['seconds'] + rows[-1]['duration'] + .05:
        raise ValueError('Cue window is outside the measured audio.')
    selected = [(r, max(0, min(end, r['seconds']+r['duration'])-max(start, r['seconds'])))
                for r in rows if r['seconds'] < end and r['seconds']+r['duration'] > start]
    weight = sum(w for _, w in selected)
    if weight < end-start-.05:
        raise ValueError('Cue window is not fully measured.')
    def energy(key):
        return 10*math.log10(max(1e-12, sum(10**(r[key]/10)*w for r,w in selected)/weight))
    rms = energy('rmsDb')
    return {'rmsDb': round(rms, 2), 'bassDb': round(energy('lowDb'), 2),
            'highDb': round(energy('highDb'), 2),
            'crestDb': round(sum(r['crestDb']*w for r,w in selected)/weight, 2),
            'levelVariationDb': round(statistics.pstdev(r['rmsDb'] for r,_ in selected), 2),
            'silentFraction': round(sum(w for r,w in selected if r['rmsDb'] < -55)/weight, 3)}


def excerpt_beats(seconds, target_bpm):
    # Choose a complete 8/16/32-bar span closest to the requested entry spacing.
    desired = seconds*target_bpm/60
    return min((32,64,128,256,512,1024,2048), key=lambda n: abs(n-desired))


def propose_offsets(evidence, rows, anchor_seconds, bpm, target_bpm, seconds, limit=8):
    """Rank candidate grid offsets locally; native seek/readback follows."""
    span = excerpt_beats(seconds, target_bpm)
    blend = min(32 if seconds <= 30 else 64, span//2)
    duration = evidence['duration']
    end = next((p['seconds'] for p in evidence['points'] if p['label']=='realEnd'), duration)
    period = 60/bpm
    levels = [r['rmsDb'] for r in rows if r['rmsDb'] > -55]
    if not levels:
        raise ValueError('The measured recording has no usable signal.')
    median = statistics.median(levels)
    possibilities=[]
    # Four-bar steps let the evidence move away from a misleading fixed offset.
    for offset in range(0, min(4000, int((end-anchor_seconds)/period)), 16):
        entry = anchor_seconds+offset*period
        exit_at = entry+span*period
        finish = exit_at+blend*period
        if entry < .01 or finish > end-.5:
            continue
        a=window_stats(rows,entry,entry+blend*period)
        b=window_stats(rows,exit_at,finish)
        before=window_stats(rows,max(entry,exit_at-16*period),exit_at)
        if a['rmsDb'] < max(-40,median-7) or a['silentFraction'] > .02 or b['silentFraction'] > .05:
            continue
        if a['highDb']-a['rmsDb'] < -31 or a['levelVariationDb'] > 9:
            continue
        # A steady entrance and a measurable change near the exit outrank a
        # quiet intro. This score is a signal heuristic, never a vocal label.
        change=abs(b['rmsDb']-before['rmsDb'])+abs(b['bassDb']-before['bassDb'])*.4
        score=change*.6-a['levelVariationDb']*.4-abs(a['rmsDb']-median)*.15
        possibilities.append((score, offset))
    # Keep early eligible exits available alongside the highest-scoring
    # windows. A live opener cannot seek to a better-scoring late entrance.
    ranked=[]
    for _,offset in possibilities:
        if len(ranked)>=min(2,limit):break
        if all(abs(offset-other)>=32 for other in ranked):
            ranked.append(offset)
    for _,offset in sorted(possibilities,reverse=True):
        if len(ranked)>=limit:break
        if all(abs(offset-other)>=32 for other in ranked):
            ranked.append(offset)
    return {'offsets':sorted(ranked), 'spanBeats':span, 'blendBeats':blend,
            'medianRmsDb':round(median,2)}


def candidate(evidence, rows, native, policy):
    """native holds readbacks, not guessed milliseconds or model timestamps."""
    entry, exit_at, end = native['entrySeconds'], native['exitSeconds'], native['endSeconds']
    first_blend_end = native['introEndSeconds']
    if not entry < first_blend_end <= exit_at < end <= evidence['duration']:
        raise ValueError('Native cue positions are not in a usable order.')
    a=window_stats(rows,entry,first_blend_end)
    b=window_stats(rows,exit_at,end)
    before=window_stats(rows,max(entry,exit_at-(end-exit_at)),exit_at)
    body=window_stats(rows,entry,end)
    # Native fluid-grid timing can differ from the initial estimate. Reject a
    # candidate that became silent or left its measured bounds after readback.
    if a['rmsDb'] < max(-40,policy['medianRmsDb']-7) or a['silentFraction'] > .02 or b['silentFraction'] > .05:
        raise ValueError('The native cue lands in a low-signal region.')
    if a['highDb']-a['rmsDb'] < -31 or a['levelVariationDb'] > 9:
        raise ValueError('The native cue lacks a stable, broadband entrance.')
    return {'id': fingerprint([VERSION,evidence['id'],evidence['energyMap']['reportDigest'],native])[:20],
            **{k:round(v,6) for k,v in native.items()},
            'spanBeats':policy['spanBeats'],'maxBlendBeats':policy['blendBeats'],
            'recordingDuration':evidence['duration'],
            'entry':a,'exit':b,'body':body,
            'exitChangeDb':round(b['rmsDb']-before['rmsDb'],2),
            'exitBassChangeDb':round(b['bassDb']-before['bassDb'],2),
            'gridSource':'VirtualDJ paused seek and readback',
            'phraseConfidence':'estimated','vocals':'unknown'}


def attach_candidates(report, candidates, seconds, target_bpm, tempo_targets=None, max_pitch=8):
    result=copy.deepcopy(report)
    for track in result['tracks']:
        track['cueCandidates']=candidates.get(track['id'],[])
    result.update(cueVersion=VERSION,secondsBetweenEntries=seconds,targetBpm=target_bpm)
    result.update(tempoVersion=tempo_planner.VERSION,tempoTargets=tempo_targets,maxPitchPercent=max_pitch)
    result['fingerprint']=fingerprint(result['tracks'])
    return result


def compile_plan(review, report, order, seconds, style='phrase'):
    if style != 'phrase':
        raise ValueError('Choose genre-aware EQ mixing before applying an analyzed plan.')
    if (review.get('decision')!='propose' or review.get('analysisFingerprint')!=report.get('fingerprint')
            or report.get('cueVersion')!=VERSION or review.get('secondsBetweenEntries')!=seconds
            or report.get('secondsBetweenEntries')!=seconds):
        raise ValueError('The mix plan is held or stale. Prepare and plan this exact set again.')
    ids=[t['id'] for t in order]
    if ids != [t['id'] for t in report['tracks']]:
        raise ValueError('The track order changed after analysis.')
    selections=review.get('selections',[])
    transitions=review.get('transitions',[])
    if len(selections)!=len(ids) or len(transitions)!=len(ids)-1:
        raise ValueError('The AI plan does not cover the whole set.')
    cues=[]
    for selected,track,source in zip(selections,order,report['tracks']):
        if selected.get('trackId')!=track['id']:
            raise ValueError('The AI plan changed a recording identity.')
        cue=next((c for c in source.get('cueCandidates',[]) if c['id']==selected.get('candidateId')),None)
        if not cue:
            raise ValueError('The AI plan cited a cue absent from this recording.')
        if cue.get('replayVerified') is not True:
            raise ValueError('The selected cue has not passed native replay verification.')
        if not source.get('energyMap') or source['energyMap'].get('coverage')!=1:
            raise ValueError('The whole-track analysis is no longer available.')
        cues.append({'trackId':track['id'],'reason':selected.get('reason',''),**copy.deepcopy(cue)})
    recipes=[]
    for i,(row,a,b) in enumerate(zip(transitions,order,order[1:])):
        if row.get('fromId')!=a['id'] or row.get('toId')!=b['id']:
            raise ValueError('The AI plan changed a transition identity.')
        beats=row.get('blendBeats')
        if type(beats) is not int or beats not in (8,16,32,64,128) or beats > min(cues[i]['maxBlendBeats'],cues[i+1]['maxBlendBeats']):
            raise ValueError('The AI overlap exceeds the measured cue windows.')
        recipe=transition_recipe(a,b,style,seconds,report['targetBpm'])
        technique=row.get('technique')
        if technique not in ('bass-swap','restrained-bass-swap','short-handoff'):
            raise ValueError('The AI requested an unsupported technique.')
        if technique=='short-handoff' and beats>16:
            raise ValueError('A short handoff must use four bars or less.')
        label={'bass-swap':'Bass swap','restrained-bass-swap':'Restrained bass swap','short-handoff':'Short handoff'}[technique]
        # Preserve the paid model response. Local execution policy bounds it;
        # key compatibility alone must never authorize competing lead parts.
        adjustment='One lead at a time: use ready native stems, otherwise a restricted midrange handoff. Vocal phrase boundaries remain unverified.'
        recipe.update(technique=technique,label=label,modelReason=row.get('reason',''),
                      executionAdjustment=adjustment,reason=(row.get('reason','')+' '+adjustment).strip(),
                      fadeBeats=beats,tonalOverlap=False,phraseBeats=4,
                      basis='AI selection from measured native cue candidates')
        recipes.append(recipe)
    result={'version':VERSION,'analysisFingerprint':report['fingerprint'],
            'secondsBetweenEntries':seconds,'orderIds':ids,'targetBpm':report['targetBpm'],
            'selections':cues,'transitions':recipes,'source':review.get('source','OpenAI'),
            'model':review.get('model'),'summary':review.get('summary',''),
            'musicalAcceptance':'not-auditioned','applied':False}
    if report.get('tempoVersion')==tempo_planner.VERSION:
        result['tempoSchedule']=tempo_planner.schedule(order,cues,recipes,report['targetBpm'],
            report.get('maxPitchPercent',8),report.get('tempoTargets'),report.get('frozenTempo'))
    result['id']=fingerprint(result)
    return result


UNFIT='That window does not fit the planned tempo or blend.'


def window_choices(choices, ids, evidence, protected, transitions):
    """Check the planner's mix-window picks against what the engine measured.

    A pick is used only for a queued recording that is not playing or cued,
    naming by exact ID one of its replay-verified windows that can carry the
    blends planned either side of it. Everything else is refused with a plain
    reason and the engine keeps its own window. Returns (picks by track, report).
    """
    picked={};ignored=[]
    for row in choices if isinstance(choices,list) else []:
        if not isinstance(row,dict):continue
        track=row.get('trackId');wanted=row.get('windowId');reason=None
        if not isinstance(track,str) or track not in ids:reason='That record is not in the queue.'
        elif track in picked:reason='A window was already chosen for that record.'
        elif ids.index(track)<protected:reason='That record is already playing or cued.'
        else:
            cue=next((c for c in (evidence.get(track) or {}).get('cueCandidates',[])
                      if isinstance(wanted,str) and wanted and c.get('id')==wanted),None)
            at=ids.index(track)
            beside=[transitions[i] for i in (at-1,at) if 0<=i<len(transitions) and isinstance(transitions[i],dict)]
            limit=(cue or {}).get('maxBlendBeats')
            if not cue:reason='That is not one of this recording’s measured windows.'
            elif cue.get('replayVerified') is not True:reason='That window has not passed replay verification.'
            elif type(limit) is not int:reason=UNFIT
            elif any(type(b.get('blendBeats')) is int and b['blendBeats']>limit for b in beside):
                reason='That window is too short for the planned blend.'
        if reason:ignored.append({'trackId':track if isinstance(track,str) else '','reason':reason})
        else:picked[track]={'trackId':track,'candidateId':cue['id'],'reason':str(row.get('reason',''))[:400]}
    return picked,{'applied':list(picked),'ignored':ignored}


def with_choices(build, picked, told):
    """Build the plan with the picks; if it cannot be built with them, build it without.

    A window can pass every check above and still not fit the tempo route or
    the handoff the plan needs. The engine's own windows are always the fallback.
    """
    if picked:
        try:return build(picked),told
        except (ValueError,KeyError,TypeError,ArithmeticError):
            told={'applied':[],'ignored':told['ignored']+[{'trackId':track,'reason':UNFIT} for track in picked]}
    return build({}),told


def revise_prepared_queue(snapshot, order, transitions, reason, protected, tempo_targets=None, cue_choices=None):
    """Compile a new future order using only already verified, selected cues.

    No timestamps are guessed and no new audio is decoded. Unmeasured tracks
    return None so they remain explicitly staged instead of entering playback.
    """
    old=snapshot.get('executionPlan');report=snapshot.get('analysis');review=snapshot.get('aiReview')
    if not old or not old.get('applied') or not report or not review:return None
    ids=[t['id'] for t in order];old_ids=old['orderIds']
    if ids[:protected]!=old_ids[:protected]:raise ValueError('The playing and cued prefix changed.')
    cues={s['trackId']:s for s in old['selections']}
    evidence={t['id']:t for t in report['tracks']}
    if any(i not in cues or i not in evidence for i in ids):return None
    if not isinstance(transitions,list) or len(transitions)!=len(ids)-1:
        raise ValueError('The queue needs a measured transition decision for each new pair.')
    selected={s['trackId']:s for s in review['selections']}
    revised_report=copy.deepcopy(report);revised_report['tracks']=[copy.deepcopy(evidence[i]) for i in ids]
    revised_report['tempoTargets']=tempo_targets
    if old.get('tempoSchedule'):revised_report['frozenTempo']=copy.deepcopy(old['tempoSchedule'][:protected])
    revised_report['fingerprint']=fingerprint(revised_report['tracks'])
    revised_review=copy.deepcopy(review)
    fixed=copy.deepcopy(transitions)
    # These transitions are already scheduled or audible. A new model response
    # may steer only after the frozen prefix, even if it proposes other values.
    fixed[:max(0,protected-1)]=copy.deepcopy(review['transitions'][:max(0,protected-1)])
    picked,told=window_choices(cue_choices,ids,evidence,protected,fixed)
    def build(picks):
        review=copy.deepcopy(revised_review)
        review.update(selections=[copy.deepcopy(picks.get(i) or selected[i]) for i in ids],transitions=copy.deepcopy(fixed),
                      analysisFingerprint=revised_report['fingerprint'],summary=reason,
                      source='AI queue revision using verified cues')
        plan=compile_plan(review,revised_report,order,snapshot['secondsPerTrack'],snapshot['mixStyle'])
        return {'analysis':revised_report,'aiReview':review,'executionPlan':plan}
    result,told=with_choices(build,picked,told)
    if cue_choices:result['cueChoices']=told
    return result


def complete_background_queue(snapshot, order, measured, reason, protected, transitions=None, tempo_targets=None, standing_style=None, cue_choices=None):
    """Fit the queue with the planner's window choices; without them if they cannot be fitted."""
    arguments=(snapshot,order,measured,reason,protected,transitions,tempo_targets,standing_style)
    if not cue_choices:return _background_fit(*arguments)
    try:return _background_fit(*arguments,cue_choices=cue_choices)
    except (ValueError,KeyError,TypeError,ArithmeticError):
        # The engine's own windows are always the fallback; a failure that is
        # not about the choices is raised again here, exactly as before.
        result=_background_fit(*arguments)
        tracks=list(dict.fromkeys(row.get('trackId') for row in cue_choices if isinstance(row,dict) and isinstance(row.get('trackId'),str))) if isinstance(cue_choices,list) else []
        result['cueChoices']={'applied':[],'ignored':[{'trackId':track,'reason':UNFIT} for track in tracks]}
        return result


def _background_fit(snapshot, order, measured, reason, protected, transitions=None, tempo_targets=None, standing_style=None, cue_choices=None):
    """Keep the agent's selection; fit verified cues locally without a paid call.

    This transparent fallback is signal-based, not another model decision or
    vocal classifier. It never changes an already cued/armed selection.
    """
    preferred_beats=16
    if standing_style is not None:
        from afterhours.dj_settings import style_context
        # Unknown lead activity keeps the existing conservative ceiling. A
        # short preference can tighten it; a long preference cannot bypass it.
        preferred_beats=min(16,style_context(standing_style)['preferredBlendBeats'])
    old=snapshot['executionPlan'];report=copy.deepcopy(snapshot['analysis'])
    evidence={t['id']:copy.deepcopy(t) for t in report['tracks']}
    evidence.update({t['id']:copy.deepcopy(t) for t in measured})
    review=copy.deepcopy(snapshot['aiReview']);selected={s['trackId']:s for s in review['selections']}
    ids=[t['id'] for t in order]
    if ids[:protected]!=old['orderIds'][:protected]:raise ValueError('The protected queue changed during background preparation.')
    offered=[row if transitions and len(transitions)==len(order)-1 and i>=protected-1 and isinstance(row,dict)
             and row.get('fromId')==a['id'] and row.get('toId')==b['id'] else None
             for i,(row,a,b) in enumerate(zip(transitions or [None]*(len(order)-1),order,order[1:]))] if len(order)>1 else []
    picked,told=window_choices(cue_choices,ids,evidence,protected,offered)
    chosen=[];cues=[]
    for i,track in enumerate(order):
        source=evidence.get(track['id'])
        if not source:raise ValueError('A queued recording is not measured yet.')
        previous=picked.get(track['id']) or selected.get(track['id'])
        candidates=[c for c in source.get('cueCandidates',[]) if c.get('replayVerified')]
        if not candidates:raise ValueError('No replay-verified cue for a queued recording.')
        cue=next((c for c in candidates if previous and c['id']==previous['candidateId']),None)
        if cue is None:
            def cost(c):
                before=cues[-1]['exit'] if cues else c['entry']
                return (abs(c['entry']['rmsDb']-before['rmsDb']) + .4*abs(c['entry']['bassDb']-before['bassDb'])
                        + c['entry']['levelVariationDb'] + c['entry']['silentFraction']*100)
            cue=min(candidates,key=cost)
            previous={'trackId':track['id'],'candidateId':cue['id'],
                'reason':'Local measured cue fit: stable entrance, similar level and controlled bass overlap; vocals remain unknown.'}
        chosen.append(copy.deepcopy(previous));cues.append(cue)
    pairs=[]
    for i,(a,b) in enumerate(zip(order,order[1:])):
        if i<protected-1:pairs.append(copy.deepcopy(review['transitions'][i]));continue
        proposal=transitions[i] if transitions and len(transitions)==len(order)-1 else None
        if proposal and proposal.get('fromId')==a['id'] and proposal.get('toId')==b['id']:
            pairs.append(copy.deepcopy(proposal));continue
        recipe=transition_recipe(a,b,snapshot['mixStyle'],snapshot['secondsPerTrack'],report['targetBpm'])
        # Conservative one-lead transition. Do not represent signal energy as
        # proof of instrumental/vocal sections or a listening judgment.
        max_beats=min(cues[i]['maxBlendBeats'],cues[i+1]['maxBlendBeats'],preferred_beats)
        beats=max((n for n in (8,16) if n<=max_beats),default=0)
        if not beats:raise ValueError('Measured cue windows do not support a background handoff.')
        pairs.append({'fromId':a['id'],'toId':b['id'],'blendBeats':beats,'technique':'restrained-bass-swap',
            'reason':'Local measured transition for an AI-selected track. '+recipe['reason']})
    report['tracks']=[evidence[i] for i in ids];report['fingerprint']=fingerprint(report['tracks'])
    report.update(tempoVersion=tempo_planner.VERSION,tempoTargets=tempo_targets,
                  maxPitchPercent=snapshot.get('maxTempoShift',8))
    # Legacy fixed-tempo playback is also frozen; new tracks may ramp later.
    report['frozenTempo']=copy.deepcopy(old.get('tempoSchedule') or [
        {'trackId':t,'fromBpm':old['targetBpm'],'toBpm':old['targetBpm'],'startOffsetBeats':4,
         'durationBeats':0,'settleBeats':8,'maxPitchPercent':snapshot.get('maxTempoShift',8),'reason':'Protected fixed-tempo handoff.'}
        for t in old['orderIds']])[:protected]
    review.update(selections=chosen,transitions=pairs,analysisFingerprint=report['fingerprint'],missingEvidence=[],
        summary=reason,source='AI queue selection; local measured cue and transition planning')
    plan=compile_plan(review,report,order,snapshot['secondsPerTrack'],snapshot['mixStyle'])
    result={'analysis':report,'aiReview':review,'executionPlan':plan}
    if cue_choices:result['cueChoices']=told
    return result


def verify_receipts(report):
    """Check the exact local measurements again before paid planning or transport."""
    for track in report['tracks']:
        measured_rows(track)


def execution_window(cue, current, fade_beats, final=False):
    goal=cue['exitBeat']
    if current > goal-4:
        raise ValueError('The analyzed transition window was missed. Take over; no substitute cue was used.')
    end=goal+(0 if final else fade_beats)
    return {'entryBeat':cue['entryBeat'],'mixAtBeat':goal,'endBeat':end,
            'segmentBeats':end-cue['entryBeat'],'phraseBeats':4,
            'fadeBeats':0 if final else fade_beats,'basis':'measured-audio-candidate',
            'candidateId':cue['id'],'entrySeconds':cue['entrySeconds'],'exitSeconds':cue['exitSeconds']}
