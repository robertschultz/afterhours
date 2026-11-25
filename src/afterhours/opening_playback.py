"""Start the selected opening recording before preparing its handoff.

Runs inside the session's sole OSC worker. A/B stay under that owner while
the existing isolated Deck 3 mailbox measures the opening pair. No model
request, audible-deck scan, speculative transition, or repeated Play occurs.
"""
import copy
import time
import uuid
from afterhours.dj_bridge import BridgeError,as_bool
from afterhours.vdj_catalog import identity
from afterhours.vdj_mixing import TransitionMixer
from afterhours.preparation_pool import PreparationPool as BackgroundAnalysis
from afterhours import cue_planner
from afterhours.queue_eligibility import preview_tail, QueueEligibilityHeld, skipped_message
from afterhours.recording_availability import PREVIEW_ONLY


class OpeningHandoffHeld(BridgeError):
    """The opening is confirmed playing, but its next handoff needs evidence."""
    pass


def measured_plan(snapshot, order, measured, current, lead_seconds=45):
    """Fit a future verified exit while leaving the already playing audio alone."""
    evidence={row['id']:copy.deepcopy(row) for row in measured}
    first=evidence[order[0]['id']]
    earliest=current['beatPosition']+lead_seconds*current['bpm']/60
    first['cueCandidates']=[c for c in first['cueCandidates'] if c.get('replayVerified') and c['exitBeat']>=earliest]
    if not first['cueCandidates']:
        raise ValueError('The opening track is playing, but no verified exit remains with enough time to prepare the next deck.')
    tracks=[evidence[t['id']] for t in order]
    report=cue_planner.attach_candidates({'tracks':tracks},
        {t['id']:t['cueCandidates'] for t in tracks},snapshot['secondsPerTrack'],current['bpm'],
        snapshot.get('tempoTargets'),snapshot.get('maxTempoShift',8))
    fixed={'trackId':order[0]['id'],'fromBpm':current['bpm'],'toBpm':current['bpm'],
           'startOffsetBeats':4,'durationBeats':0,'settleBeats':8,
           'maxPitchPercent':snapshot.get('maxTempoShift',8),'reason':'Opening recording is already playing at its observed tempo.'}
    seed={**snapshot,'analysis':report,
          'executionPlan':{'applied':True,'orderIds':[order[0]['id']],'targetBpm':current['bpm'],'tempoSchedule':[fixed]},
          'aiReview':{'decision':'propose','secondsBetweenEntries':snapshot['secondsPerTrack'],
                      'selections':[],'transitions':[]}}
    prepared=cue_planner.complete_background_queue(seed,order,tracks,
        'Opening music started first; the upcoming handoff uses verified spare-deck measurements.',1,
        tempo_targets=snapshot.get('tempoTargets'))
    prepared['executionPlan']['source']='Operator-requested start; local measured cue planning'
    prepared['aiReview']['source']=prepared['executionPlan']['source']
    # Recompile after setting provenance so the execution identity is reproducible.
    prepared['executionPlan']=cue_planner.compile_plan(prepared['aiReview'],prepared['analysis'],order,
                                                       snapshot['secondsPerTrack'],snapshot['mixStyle'])
    return prepared


def start_and_prepare(session,client,requested):
    from afterhours.vdj_session import RecordingUnavailable,TrackMetadataChanged,number,validate_order
    s=session;snapshot=s.snapshot();order=copy.deepcopy(requested);skipped=[]
    startup={'id':uuid.uuid4().hex,'status':'loading','requestedIds':[t['id'] for t in order],
             'requestedAt':time.time(),'skipped':skipped}
    try:
        omission=preview_tail(s.availability,snapshot,order,min(2,len(order)))
        if omission:
            order=validate_order(omission['retainedIds'],omission['tracks'],snapshot.get('maxTempoShift',8),
                                 snapshot['secondsPerTrack'],omission['tempoTargets'])
            snapshot={**snapshot,'tempoTargets':omission['tempoTargets']}
            skipped.extend(omission['skipped'])
    except QueueEligibilityHeld as exc:
        startup.update(status='blocked',failure=exc.failure)
        s.update(startup=copy.deepcopy(startup));raise
    s.update(startup=copy.deepcopy(startup),index=0,transitions=[],activeHandoff=None,
             analysis=None,aiReview=None,executionPlan=None,queuedUpdate=None,phase='loading',
             message='Loading the opening recording. Mix analysis follows playback.')
    if omission:
        s.update(order=order,tempoTargets=snapshot.get('tempoTargets'),lastQueueRepair={'id':uuid.uuid4().hex,'queueRevision':startup['id'],
            'requestedIds':omission['requestedIds'],'retainedIds':omission['retainedIds'],
            'skipped':copy.deepcopy(skipped),'at':time.time(),'basis':'known-provider-preview-only'})
        s.event(skipped_message(skipped),'queue-skipped',skipped=copy.deepcopy(skipped))
    policy=snapshot.get('setlist') or {}
    can_omit=policy.get('allowReorder',True) and not policy.get('lockedIds')
    mixing_only=(snapshot.get('copilot') or {}).get('mode')=='mix'
    pinned=set(snapshot.get('setPins',[]))
    # Only a conclusive missing recording can fall through to the next pick.
    # A load timeout or other uncertain transport result is never retried.
    while order:
        first=order[0]
        try:
            blocked=s.availability.blocked(first['id'])
            if blocked:
                if blocked['code']==PREVIEW_ONLY:
                    failure={'trackId':first['id'],'title':first.get('title','Recording'),
                        'code':PREVIEW_ONLY,'reason':blocked['reason']+' The current or prepared opening pair stays in place.'}
                    startup.update(status='blocked',failure=failure)
                    s.update(startup=copy.deepcopy(startup))
                    raise QueueEligibilityHeld(failure)
                if not can_omit or mixing_only or first['id'] in pinned or len(order)<3:raise BridgeError(blocked['reason'])
                skipped.append(copy.deepcopy(blocked));order.pop(0);continue
            s.availability.require_track(first)
            same=(as_bool(s.q(client,'deck 1 loaded','bool')) and
                  identity(str(s.q(client,'deck 1 get_filepath','text')))==first['engine']['selection_id'])
            if not same:s.load(client,first,1)
            if as_bool(s.q(client,'deck 1 deck_has_error','bool')):raise BridgeError('The opening recording could not be loaded.')
            break
        except RecordingUnavailable as exc:
            s.availability.record(first['id'],'recording-unavailable',str(exc))
            if not can_omit or mixing_only or first['id'] in pinned or len(order)<3:raise
            skipped.append({'trackId':first['id'],'code':'recording-unavailable','reason':str(exc)})
            order.pop(0)
    first=order[0]
    if any(as_bool(s.q(client,f'deck {deck} play','bool')) for deck in (1,2)):
        raise BridgeError('A deck started manually. The opening start was cancelled.')
    s.send(client,'deck 1 master_tempo on & deck 1 goto_first_beat')
    target=number(s.q(client,'deck 1 get_bpm absolute'),40,300)
    s.match_tempo(client,first,1,target)
    s.send(client,'crossfader 0%')
    s.mixer=TransitionMixer(s.q,s.send);s.mixer.initialize_opening(client)
    before=s.telemetry(client)
    if before['crossfader']>.02 or before['decks'][0].get('id')!=first['id'] or any(d.get('playing') for d in before['decks']):
        raise BridgeError('The opening recording and paused mixer could not be confirmed.')
    duration=number(s.q(client,'deck 1 get_time total absolute'),1000,86_400_000)/1000
    startup.update(entryBeat=before['decks'][0]['beatPosition'],entrySeconds=before['decks'][0]['position'],
                   openingId=first['id'],targetBpm=target,status='starting')
    s.update(order=order,startup=copy.deepcopy(startup),mixerBaselines=copy.deepcopy(s.mixer.baselines))
    s.availability.require_track(first)
    s.send(client,'deck 1 play')
    s.sleep(.2);t=s.telemetry(client)
    if not t['decks'][0].get('playing') or t['decks'][0].get('id')!=first['id']:
        raise BridgeError('Opening playback was not confirmed. No second Play was sent.')
    startup.update(status='analyzing',playingAt=time.time(),skipped=copy.deepcopy(skipped))
    s.update(startup=copy.deepcopy(startup),startedAt=startup['playingAt'],endedAt=None,phase='playing',
             message=f'Playing {first["title"]}. Preparing the next handoff on muted spare decks.')
    with s.lock:
        s.state['copilot'].update(mode='mix' if s.state['copilot']['mode']=='mix' else 'leading',held=False)
    s.event('Opening recording confirmed playing before audio analysis.','playing',
            secondsToPlayback=startup['playingAt']-startup['requestedAt'])
    initial_incoming=t['decks'][1].get('id')
    try:
        while True:
            pair=order[:2] if can_omit else order
            job=BackgroundAnalysis(s,startup['id'],pair,s.analysis_cache);s.background_job=job
            previous=None
            while not job.done:
                s.check();t=s.telemetry(client);current=t['decks'][0]
                if (current.get('id')!=first['id'] or not current.get('playing') or t['crossfader']>.02
                        or t['decks'][1].get('playing') or t['decks'][1].get('id')!=initial_incoming):
                    raise BridgeError('The opening decks changed. Background preparation yielded to manual control.')
                remaining=duration-current['position']
                if remaining<12:raise BridgeError('The opening recording is nearly finished; no unverified handoff was started.')
                job.step(client,remaining)
                progress=job.snapshot();s.update(backgroundAnalysis=progress)
                if progress['message']!=previous:
                    previous=progress['message'];s.event(previous,'analysis')
                s.sleep(.04)
            s.analysis_cache.update(job.cache);job.close(client);s.background_job=None
            if not job.error:break
            if job.analysis_timed_out:
                t=s.telemetry(client)
                if (t['decks'][0].get('id')==first['id'] and t['decks'][0].get('playing')
                        and t['crossfader']<=.02 and not any(deck.get('playing') for deck in t['decks'][1:])):
                    raise OpeningHandoffHeld(job.error)
            # Only this exact conclusive preparation failure can omit a pick.
            # A new preview restriction on the protected opening pair holds;
            # it cannot mask another uncertain Load or authorize a replay.
            unavailable=[r for r in pair[1:] if (failure:=s.availability.blocked(r['id']))
                         and failure['reason']==job.error
                         and failure['code'] in ('recording-unavailable','limited-decoded-audio')]
            if (not can_omit or mixing_only or not unavailable or any(t['id'] in pinned for t in unavailable)
                    or len(order)-len(unavailable)<2):raise BridgeError(job.error)
            for track in unavailable:
                failure=s.availability.blocked(track['id']);skipped.append(copy.deepcopy(failure))
                order=[t for t in order if t['id']!=track['id']]
            startup['skipped']=copy.deepcopy(skipped);s.update(order=order,startup=copy.deepcopy(startup))
        measured={row['id']:row for row in job.result}
        for track in pair:
            row=measured[track['id']];track.update(bpm=row['bpm'],camelot=row.get('camelot'),engineChecked=True)
        t=s.telemetry(client);current=t['decks'][0]
        if abs(current['bpm']-target)>.15:raise BridgeError('Opening tempo changed during analysis. Music keeps playing; the handoff is held.')
        targets=snapshot.get('tempoTargets')
        subset={**snapshot,'tempoTargets':[r for r in targets if r['trackId'] in {t['id'] for t in pair}] if targets else targets}
        prepared=measured_plan(subset,pair,job.result,current)
        cue_planner.verify_receipts(prepared['analysis'])
        plan=prepared['executionPlan']
        try:s.prepare_deck(client,pair[1],2,plan['tempoSchedule'][1]['fromBpm'],cue=plan['selections'][1])
        except TrackMetadataChanged as exc:
            pair,prepared=s.refresh_key_plan({**s.snapshot(),**prepared,'order':pair,
                'executionPlan':{**plan,'applied':True}},exc.track,1)
            plan=prepared['executionPlan']
            s.prepare_deck(client,pair[1],2,plan['tempoSchedule'][1]['fromBpm'],cue=plan['selections'][1],checked=exc.track)
        t=s.telemetry(client)
        if (t['decks'][0].get('id')!=first['id'] or not t['decks'][0].get('playing') or t['crossfader']>.02
                or t['decks'][1].get('id')!=pair[1]['id'] or t['decks'][1].get('playing')):
            raise BridgeError('Opening pair was not confirmed; no handoff was started.')
        window=cue_planner.execution_window(plan['selections'][0],t['decks'][0]['beatPosition'],plan['transitions'][0]['fadeBeats'])
        window.update(entryBeat=startup['entryBeat'],entrySeconds=startup['entrySeconds'],
                      segmentBeats=window['endBeat']-startup['entryBeat'])
        plan['applied']=True;startup.update(status='ready',preparedAt=time.time(),skipped=copy.deepcopy(skipped))
        future=None
        if len(order)>len(pair):
            checked={t['id']:t for t in pair};order=[copy.deepcopy(checked.get(t['id'],t)) for t in order]
            future={'id':uuid.uuid4().hex,'tracks':order,'protectedIds':[t['id'] for t in pair],
                    'status':'needs-analysis','reason':'Continue the selected session after the opening pair.',
                    'prepared':None,'mixTransitions':None,'tempoTargets':targets,
                    'startId':startup['id'],'skipped':copy.deepcopy(skipped),
                    'sourceById':copy.deepcopy(policy.get('sourceById',{}))}
        current_ids={t['id'] for t in pair}
        policy={**policy,'sourceById':{tid:source for tid,source in policy.get('sourceById',{}).items() if tid in current_ids}}
        s.update(order=pair,**prepared,queuedUpdate=future,setlist=policy,startup=copy.deepcopy(startup),
                 tempoTargets=subset['tempoTargets'],mixPlans=plan['transitions'],
                 backgroundAnalysis={**job.snapshot(),'status':'ready','message':'Opening handoff verified. Music has continued playing.'})
        with s.lock:
            checked={t['id']:t for t in pair}
            s.state['candidates']=[copy.deepcopy(checked.get(t['id'],t)) for t in s.state['candidates']]
        s.event('Opening handoff verified without stopping or seeking the playing deck.','queue-ready')
        return pair,plan,t,window
    except OpeningHandoffHeld:
        startup['status']='handoff-held';s.update(startup=copy.deepcopy(startup));raise
    except Exception:
        startup['status']='blocked';s.update(startup=copy.deepcopy(startup));raise
