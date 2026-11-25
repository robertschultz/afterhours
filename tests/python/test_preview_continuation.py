"""Known provider previews leave a bounded playable tail; no live transports."""
import copy
import threading
import unittest
from unittest.mock import Mock,patch

from afterhours import ai_mix
from afterhours import cue_planner
from afterhours.dj_agent import DJAgent
from afterhours.dj_bridge import BridgeError
from afterhours.queue_eligibility import preview_tail,QueueEligibilityHeld
from afterhours.vdj_session import DJSession
from test_cue_planner import fixture
from test_provider_eligibility import restrict
from afterhours import conversation_feed


def measured_session():
    _,report,answer=fixture(6)
    mapping={str(i):'virtualdj:'+format(i+1,'064x') for i in range(6)}
    def exact(value):
        if isinstance(value,list):return [exact(v) for v in value]
        if isinstance(value,dict):return {k:exact(v) for k,v in value.items()}
        return mapping.get(value,value) if isinstance(value,str) else value
    report,answer=exact(report),exact(answer)
    tracks=report['tracks']
    for i,t in enumerate(tracks):
        t.update(title=f'Selected {i}',engine={'selection_id':t['id'].removeprefix('virtualdj:')})
    pair={'tracks':copy.deepcopy(tracks[:2]),**{k:v for k,v in report.items() if k!='tracks'}}
    pair=cue_planner.attach_candidates({'tracks':tracks[:2]},
        {t['id']:t['cueCandidates'] for t in tracks[:2]},30,155)
    answer={**answer,'selections':answer['selections'][:2],'transitions':answer['transitions'][:1]}
    review=ai_mix.validate_plan(answer,pair,30)
    plan=cue_planner.compile_plan(review,pair,tracks[:2],30);plan['applied']=True
    session=DJSession(threading.Lock(),client_factory=Mock(side_effect=AssertionError('No native connection')))
    ids=[t['id'] for t in tracks]
    session.update(candidates=tracks,order=copy.deepcopy(tracks[:2]),active=True,phase='playing',index=0,
        analysis=pair,aiReview=review,executionPlan=plan,
        telemetry={'decks':[{'deck':1,'id':ids[0],'playing':True},{'deck':2,'id':ids[1],'playing':False}]},
        setlist={'allowReorder':True,'lockedIds':[],'sourceById':{tid:'operator' for tid in ids}},
        queuedUpdate={'id':'selected-tail','status':'needs-analysis','tracks':copy.deepcopy(tracks),
            'protectedIds':ids[:2],'prepared':None,'tempoTargets':None,'sourceById':{tid:'operator' for tid in ids},
            'reason':'Continue the explicit selection.'})
    return session,tracks


class PreviewContinuationTests(unittest.TestCase):
    def test_known_preview_is_omitted_once_then_remaining_tail_fits_and_adopts_without_model_or_transport(self):
        s,tracks=measured_session();ids=[t['id'] for t in tracks];restrict(s.availability,ids[3])
        old=copy.deepcopy(s.snapshot());peer=Mock();provider=Mock();agent=DJAgent(s,evaluator=provider)
        s.service_background(peer,40)
        revised=s.snapshot()['queuedUpdate'];retained=ids[:3]+ids[4:]
        self.assertEqual([t['id'] for t in revised['tracks']],retained)
        self.assertEqual(revised['status'],'needs-analysis')
        self.assertEqual(s.snapshot()['order'],old['order'])
        self.assertEqual(s.snapshot()['executionPlan'],old['executionPlan'])
        self.assertEqual(s.snapshot()['telemetry'],old['telemetry'])
        self.assertEqual(s.snapshot()['lastQueueRepair']['skipped'],[{'trackId':ids[3],'title':'Selected 3',
            'code':'provider-preview-only','reason':s.availability.blocked(ids[3])['reason']}])
        self.assertEqual(s.crate.summary()['count'],6)
        self.assertIsNotNone(s.availability.blocked(ids[3]));self.assertEqual(peer.mock_calls,[])
        created=[]
        class ReadyPool:
            def __init__(job,session,revision,selected,cache):
                created.extend(t['id'] for t in selected);job.revision=revision;job.cache={};job.error=None;job.done=False
                job.result=copy.deepcopy(selected)
            def step(job,client,available):job.done=True
            def close(job,client):pass
            def snapshot(job):return {'revision':job.revision,'status':'complete','message':'Measured fixture.','decks':[]}
        with patch('afterhours.vdj_session.BackgroundAnalysis',ReadyPool),patch('afterhours.cue_planner.verify_receipts'):
            s.service_background(peer,40)
            self.assertEqual(s.snapshot()['queuedUpdate']['status'],'ready')
            adopted=s.adopt_queue(0)
        self.assertIsNotNone(adopted)
        self.assertEqual([t['id'] for t in s.snapshot()['order']],retained)
        self.assertEqual(s.snapshot()['executionPlan']['selections'][:2],old['executionPlan']['selections'])
        self.assertEqual(created,[ids[2],ids[4],ids[5]])
        self.assertNotIn(ids[3],created)
        self.assertEqual(peer.mock_calls,[]);provider.assert_not_called();s.factory.assert_not_called()
        self.assertEqual(agent.remaining,0)

    def test_exact_skip_receipt_is_visible_once_in_existing_mind_journal_without_overriding_playing_copy(self):
        s,tracks=measured_session();restrict(s.availability,tracks[3]['id'])
        agent=DJAgent(s,evaluator=Mock());agent.state.update(status='playing',message='Playing Selected 0.')
        s.service_background(None,40)
        first=agent.snapshot();second=agent.snapshot();rid=s.snapshot()['lastQueueRepair']['id']
        turns=[row for row in first['conversation']['turns'] if row['id']=='queue-repair:'+rid]
        self.assertEqual(len(turns),1);turn=turns[0]
        self.assertEqual(turn['status'],'confirmed');self.assertTrue(turn['done'])
        self.assertIn('Selected 3 is preview-only',turn['text'])
        self.assertEqual(turn['action']['verb'],'Skipped');self.assertEqual(turn['action']['receiptId'],rid)
        self.assertEqual(first['conversation']['turns'],second['conversation']['turns'])
        self.assertEqual(first['message'],'Playing Selected 0.');self.assertEqual(first['status'],'playing')
        self.assertEqual(agent.remaining,0);agent.evaluator.assert_not_called()
        receipt=copy.deepcopy(s.snapshot()['lastQueueRepair'])
        for bad in ({'retainedIds':receipt['requestedIds']},{'basis':'guessed-preview'},
                    {'skipped':[{**receipt['skipped'][0],'code':'load-timeout'}]}):
            self.assertEqual(conversation_feed.queue_repair([],{**receipt,**bad}),[])

    def test_pins_locks_reorder_and_protected_positions_hold_exact_identity(self):
        for case in ('pinned','locked','later-locked','reorder-off','protected','mixing-only'):
            s,tracks=measured_session();ids=[t['id'] for t in tracks];target=ids[1] if case=='protected' else ids[3]
            if case=='pinned':s.update(setPins=[target])
            if case=='locked':s.state['setlist']['lockedIds']=[target]
            if case=='later-locked':s.state['setlist']['lockedIds']=[ids[4]]
            if case=='reorder-off':s.state['setlist']['allowReorder']=False
            if case=='mixing-only':s.state['copilot']['mode']='mix'
            restrict(s.availability,target);old=copy.deepcopy(s.snapshot());peer=Mock()
            s.service_background(peer,40);q=s.snapshot()['queuedUpdate']
            self.assertEqual(q['status'],'blocked',case)
            self.assertEqual(q['tracks'],old['queuedUpdate']['tracks'],case)
            self.assertEqual(s.snapshot()['order'],old['order'],case)
            self.assertEqual(q['failure']['trackId'],target,case)
            self.assertEqual(q['failure']['reason'],q['error'],case)
            self.assertEqual(q['failure']['title'],next(t['title'] for t in tracks if t['id']==target))
            self.assertEqual(peer.mock_calls,[],case)
            self.assertIsNone(s.snapshot()['lastQueueRepair'])

    def test_generic_unavailability_and_uncertain_blocked_jobs_never_auto_retry_or_omit(self):
        s,tracks=measured_session();ids=[t['id'] for t in tracks]
        s.availability.record(ids[3],'recording-unavailable','A bounded lookup could not confirm access.')
        self.assertIsNone(preview_tail(s.availability,s.snapshot(),tracks,2))
        restrict(s.availability,ids[3]);s.state['queuedUpdate'].update(status='blocked',error='Load acknowledgement uncertain.')
        before=copy.deepcopy(s.snapshot());peer=Mock()
        for _ in range(4):s.service_background(peer,40)
        self.assertEqual(s.snapshot(),before);self.assertEqual(peer.mock_calls,[])

    def test_new_preview_does_not_mask_an_existing_uncertain_load_failure(self):
        s,tracks=measured_session();restrict(s.availability,tracks[3]['id'])
        before=copy.deepcopy(s.snapshot()['queuedUpdate']);job=Mock()
        job.revision=before['id'];job.cache={};job.error='Load acknowledgement uncertain.';job.done=True
        job.snapshot.return_value={'revision':before['id'],'status':'blocked','message':job.error,'decks':[]}
        s.background_job=job
        s.service_background(None,40)
        self.assertEqual(s.snapshot()['queuedUpdate']['tracks'],before['tracks'])
        self.assertEqual(s.snapshot()['queuedUpdate']['error'],job.error)
        self.assertNotIn('failure',s.snapshot()['queuedUpdate'])
        self.assertIsNone(s.snapshot()['lastQueueRepair']);job.close.assert_called_once()

    def test_owned_preparation_cleanup_must_finish_before_any_tail_change(self):
        for fail in (False,True):
            s,tracks=measured_session();restrict(s.availability,tracks[3]['id'])
            before=copy.deepcopy(s.snapshot()['queuedUpdate']);job=Mock()
            job.revision=before['id'];job.cache={};job.error=None;job.cleanup_descriptors.return_value=[]
            def close(client):
                self.assertEqual(s.snapshot()['queuedUpdate'],before)
                if fail:raise BridgeError('Owned cleanup unconfirmed.')
            job.close.side_effect=close;s.background_job=job
            s.service_background(None,11);job.close.assert_not_called()
            if fail:
                with self.assertRaisesRegex(BridgeError,'cleanup unconfirmed'):s.service_background(None,40)
                self.assertEqual(s.snapshot()['queuedUpdate'],before)
                self.assertTrue(s.snapshot()['resumeBlocked'])
            else:
                s.service_background(None,40)
                self.assertNotEqual(s.snapshot()['queuedUpdate']['id'],before['id'])
            job.close.assert_called_once()

    def test_retry_and_success_clear_old_failure_binding(self):
        s,tracks=measured_session();restrict(s.availability,tracks[3]['id']);s.update(setPins=[tracks[3]['id']])
        s.service_background(None,40);old_id=s.snapshot()['queuedUpdate']['id']
        self.assertIn('failure',s.snapshot()['queuedUpdate'])
        s.retry_background();q=s.snapshot()['queuedUpdate']
        self.assertNotEqual(q['id'],old_id);self.assertNotIn('failure',q);self.assertNotIn('error',q)
        s.update(setPins=[]);s.service_background(None,40)
        self.assertNotIn('failure',s.snapshot()['queuedUpdate']);self.assertNotIn('error',s.snapshot()['queuedUpdate'])

    def test_stopped_pending_queue_start_filters_only_known_preview_and_keeps_receipt(self):
        s,tracks=measured_session();ids=[t['id'] for t in tracks];restrict(s.availability,ids[3])
        s.update(active=False,phase='stopped',telemetry={'decks':[]})
        s.start=Mock(side_effect=lambda **kw:s.update(active=True,phase='playing',
            telemetry={'decks':[{'playing':True,'id':ids[0]}]}))
        provider=Mock();agent=DJAgent(s,evaluator=provider)
        agent.message('Start the current queue.',message_id='start',approved=True,approve_preparation=True)
        agent.advance_playback()
        self.assertEqual(s.start.call_count,1);provider.assert_not_called()
        self.assertEqual(agent.remaining,0)
        self.assertEqual(agent.playback_request['requestedIds'],ids)
        self.assertEqual(agent.playback_request['skipped'][0]['trackId'],ids[3])
        self.assertEqual(agent.playback_request['orderIds'],ids[:3]+ids[4:])
        self.assertEqual(agent.playback_request['status'],'playing')
        self.assertEqual(s.crate.summary()['count'],6)

    def test_stopped_pending_pinned_preview_retains_exact_request_with_bound_failure(self):
        s,tracks=measured_session();ids=[t['id'] for t in tracks];restrict(s.availability,ids[3])
        s.update(active=False,phase='stopped',setPins=[ids[3]],telemetry={'decks':[]});s.start=Mock()
        before=copy.deepcopy(s.snapshot()['queuedUpdate']['tracks']);provider=Mock();agent=DJAgent(s,evaluator=provider)
        with self.assertRaisesRegex(QueueEligibilityHeld,'pinned'):
            agent.message('Start the current queue.',message_id='start')
        self.assertEqual(s.snapshot()['queuedUpdate']['tracks'],before)
        self.assertEqual(s.snapshot()['queuedUpdate']['failure']['trackId'],ids[3])
        self.assertEqual(agent.playback_request['status'],'failed')
        s.start.assert_not_called();provider.assert_not_called()

    def test_explicit_fast_start_skips_only_known_future_preview_and_receipts_start_without_another_request(self):
        from afterhours.opening_playback import start_and_prepare
        s,tracks=measured_session();ids=[t['id'] for t in tracks]
        s.update(order=copy.deepcopy(tracks),phase='ready',analysis=None,aiReview=None,executionPlan=None,queuedUpdate=None)
        restrict(s.availability,ids[3]);agent=DJAgent(s,evaluator=Mock())
        agent.playback_request={'id':'start','sessionId':s.state['id'],'revision':agent.state['revision'],
            'status':'starting','orderIds':ids,'expiresAt':agent.clock()+900,'source':'typed','prepareApproved':False}
        playing=[False];prepared=[False];trace=[]
        def telemetry(client):
            value={'decks':[{'deck':1,'id':ids[0],'playing':playing[0],'bpm':155,'beatPosition':1 if playing[0] else 0,'position':.4 if playing[0] else 0},
                            {'deck':2,'id':ids[1] if prepared[0] else None,'playing':False}], 'crossfader':0}
            s.update(telemetry=value);return value
        def send(client,script):
            trace.append(script)
            if script=='deck 1 play':playing[0]=True
        def q(client,script,kind=None):
            return {'deck 1 loaded':False,'deck 1 deck_has_error':False,
                'deck 1 play':playing[0],'deck 2 play':False,'deck 1 get_bpm absolute':155,
                'deck 1 get_time total absolute':400000}[script]
        def prepare(client,track,deck,target,**kwargs):
            trace.append('prepare '+track['id']);prepared[0]=True
        class OpeningPool:
            def __init__(job,session,revision,selected,cache):
                self.assertTrue(playing[0]);trace.append('analysis')
                self.assertEqual([t['id'] for t in selected],ids[:2])
                job.done=False;job.cache={};job.error=None;job.result=copy.deepcopy(selected)
            def step(job,client,remaining):job.done=True
            def close(job,client):pass
            def snapshot(job):return {'message':'Measured fixture.','status':'complete','decks':[]}
        s.q=q;s.send=send;s.telemetry=telemetry;s.sleep=lambda _:None;s.match_tempo=Mock();s.prepare_deck=prepare
        s.load=Mock(side_effect=lambda client,track,deck:trace.append('load '+track['id']))
        with patch('afterhours.opening_playback.BackgroundAnalysis',OpeningPool),patch('afterhours.opening_playback.TransitionMixer') as mixer,patch('afterhours.cue_planner.verify_receipts'):
            mixer.return_value.baselines={}
            start_and_prepare(s,None,tracks)
        self.assertEqual(trace.count('deck 1 play'),1)
        self.assertLess(trace.index('deck 1 play'),trace.index('analysis'))
        self.assertNotIn('load '+ids[3],trace);self.assertNotIn('prepare '+ids[3],trace)
        self.assertEqual([t['id'] for t in s.snapshot()['queuedUpdate']['tracks']],ids[:3]+ids[4:])
        self.assertEqual(s.snapshot()['startup']['requestedIds'],ids)
        self.assertEqual(s.snapshot()['startup']['skipped'][0]['trackId'],ids[3])
        agent.advance_playback();self.assertEqual(agent.playback_request['status'],'playing')
        agent.evaluator.assert_not_called();self.assertEqual(agent.remaining,0)

    def test_opening_preview_discovered_after_preflight_holds_without_loading_the_next_record(self):
        from afterhours.opening_playback import start_and_prepare
        s,tracks=measured_session();ids=[t['id'] for t in tracks]
        s.update(order=copy.deepcopy(tracks),active=False,phase='ready',analysis=None,
            aiReview=None,executionPlan=None,queuedUpdate=None)
        before=copy.deepcopy(s.state['order']);update=s.update
        def restrict_after_preflight(**values):
            update(**values)
            if values.get('phase')=='loading':restrict(s.availability,ids[0])
        s.update=restrict_after_preflight
        s.q=Mock(side_effect=AssertionError('No native read before the protected-record hold'))
        s.load=Mock(side_effect=AssertionError('No replacement Load after preview evidence'))
        with self.assertRaises(QueueEligibilityHeld) as held:start_and_prepare(s,None,tracks)
        s.q.assert_not_called();s.load.assert_not_called();s.factory.assert_not_called()
        self.assertEqual(s.state['order'],before)
        startup=s.snapshot()['startup']
        self.assertEqual(startup['status'],'blocked');self.assertEqual(startup['skipped'],[])
        self.assertEqual(startup['requestedIds'],ids)
        self.assertEqual(startup['failure'],held.exception.failure)
        self.assertEqual(startup['failure']['trackId'],ids[0])
        self.assertEqual(startup['failure']['title'],tracks[0]['title'])
        self.assertEqual(startup['failure']['code'],'provider-preview-only')
        self.assertIsNotNone(s.availability.blocked(ids[0]))
        self.assertIsNone(s.snapshot()['lastQueueRepair']);self.assertEqual(s.crate.summary()['count'],6)

    def test_first_pair_preview_discovered_during_analysis_holds_without_replacing_pair_or_replaying_uncertain_work(self):
        from afterhours.opening_playback import start_and_prepare
        from test_opening_playback import OpeningTests
        for mode,uncertain in (('mix',False),('leading',False),('leading',True)):
            s,report,trace,playing,beat=OpeningTests().setup_session()
            s.state['copilot']['mode']=mode
            # Use exact canonical IDs for the observed restriction, while the
            # fake telemetry preserves this test's original A/B identities.
            target='virtualdj:'+'b'*64;s.state['order'][1]['id']=target;s.state['candidates'][1]['id']=target
            before=copy.deepcopy(s.snapshot()['order']);pools=[]
            class FailingPool:
                def __init__(job,session,revision,tracks,cache):
                    pools.append([t['id'] for t in tracks]);job.done=False;job.cache={};job.error=None;job.result=[]
                    job.analysis_timed_out=False
                def step(job,client,remaining):
                    restrict(s.availability,target)
                    job.error='Load acknowledgement uncertain.' if uncertain else s.availability.blocked(target)['reason']
                    job.done=True
                def close(job,client):pass
                def snapshot(job):return {'message':job.error or 'Measuring fixture.','status':'blocked','decks':[]}
            with patch('afterhours.opening_playback.BackgroundAnalysis',FailingPool),patch('afterhours.opening_playback.TransitionMixer') as mixer:
                mixer.return_value.baselines={}
                with self.assertRaises(BridgeError):start_and_prepare(s,None,s.snapshot()['order'])
            self.assertEqual(len(pools),1,(mode,uncertain))
            self.assertEqual(s.state['order'],before,(mode,uncertain))
            self.assertEqual(trace.count('deck 1 play'),1)
            self.assertNotIn('prepare-next',trace)


if __name__=='__main__':unittest.main()
