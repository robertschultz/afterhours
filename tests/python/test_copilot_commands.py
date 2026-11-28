"""Operator command integration against measured fixtures, never live audio."""
import copy
import threading
import unittest
from unittest.mock import Mock, patch

from afterhours import ai_mix
from afterhours import copilot_commands as cc
from afterhours import cue_planner
from afterhours.dj_agent import DJAgent
from afterhours.dj_bridge import BridgeError
from test_cue_planner import fixture
from test_dj_agent import decision, settled
from afterhours.vdj_session import DJSession, Cancelled


def running():
    _, report, answer = fixture()
    review = ai_mix.validate_plan(answer, report, 30)
    plan = cue_planner.compile_plan(review, report, report['tracks'], 30)
    plan['applied'] = True
    s = DJSession(threading.Lock())
    s.update(phase='playing', active=True, startedAt=10, index=0,
        candidates=copy.deepcopy(report['tracks']), order=copy.deepcopy(report['tracks']),
        analysis=report, aiReview=review, executionPlan=plan, maxTempoShift=8,
        telemetry={'sampledAt':100, 'crossfader':0, 'decks':[
            {'deck':1,'id':'0','playing':True,'bpm':155,'beatPosition':80},
            {'deck':2,'id':'1','playing':False,'bpm':155,'beatPosition':64}]},
        timing=cue_planner.execution_window(plan['selections'][0], 80, 32), fadeBeats=32)
    return s


class CopilotControlTests(unittest.TestCase):
    def setUp(self):
        self.clock = patch('time.time', return_value=100)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.receipts = patch('afterhours.cue_planner.verify_receipts')
        self.receipts.start()
        self.addCleanup(self.receipts.stop)

    def command(self, s, name, identity=None):
        return s.copilot_command(name, identity or name, cc.context(s.snapshot()))

    def test_guarded_resume_keeps_the_explicit_off_owner_at_acceptance(self):
        s = running()
        s.update(active=False, phase='takeover')
        s.state['copilot'].update(mode='off', command={'id': 'first-off', 'action': 'off', 'status': 'confirmed'})
        for deck in s.state['telemetry']['decks']:
            deck.update(loaded=True, playing=False)
        expected = cc.context(s.snapshot())
        held = cc.held_recording(s.snapshot())
        self.assertEqual(held, ['first-off', 1, '0'])
        s.state['copilot']['command']['id'] = 'second-off'
        self.assertEqual(cc.context(s.snapshot()), expected)
        agent = DJAgent(s, evaluator=Mock())
        with patch.object(s, 'start') as start:
            with self.assertRaisesRegex(ValueError, 'held recording changed'):
                agent.copilot_action('resume', 'stale-resume', expected, held_recording=held)
            start.assert_not_called()
            valid = cc.held_recording(s.snapshot())
            result = agent.copilot_action('resume', 'safe-resume', expected, held_recording=valid)
            start.assert_called_once_with(resume=True, adopt_mixer=True, resume_paused=True, held_recording=valid)
            self.assertEqual(result['copilot']['command']['heldRecording'], valid)
            with self.assertRaisesRegex(ValueError, 'different request'):
                agent.copilot_action('resume', 'safe-resume', expected, held_recording=held)

    def test_guarded_resume_rechecks_the_paused_deck_before_worker_writes(self):
        s = running()
        s.update(active=False, phase='takeover')
        s.state['copilot'].update(mode='off', command={'id': 'off', 'action': 'off', 'status': 'confirmed'})
        for deck in s.state['telemetry']['decks']:
            deck.update(loaded=True, playing=False)
        reading = copy.deepcopy(s.state['telemetry'])
        reading['decks'][0]['playing'] = True
        peer = Mock()
        peer.status.return_value = {'automix': False, 'decks': reading['decks']}
        with patch.object(s, '_launch', side_effect=lambda work, phase: work(peer)), \
                patch.object(s, 'telemetry', return_value=reading), \
                patch.object(s, 'send') as send, patch.object(s, 'prepare_deck') as prepare:
            with self.assertRaisesRegex(BridgeError, 'held recording changed before handback'):
                s.copilot_command('resume', 'guarded', cc.context(s.snapshot()), held_recording=['off', 1, '0'])
            send.assert_not_called()
            prepare.assert_not_called()

    def test_only_explicit_controls_bypass_the_language_model(self):
        for text, action in [('please hold.', 'hold'),('go now', 'go'),('something else', 'skip'),
                             ('copilot off', 'off'),('turn copilot on, please', 'on')]:
            self.assertEqual(cc.intent(text), action)
        for text in ['why hold?', 'go deeper', 'skip the warnings', 'Goa trance',
                     'do not go', 'do not hold', 'could you explain hold', None]:
            self.assertIsNone(cc.intent(text))

    def test_spoken_mix_requests_use_the_direct_control_without_extra_confirmation(self):
        phrases=['start mixing into the next track',
                 'you, can you um transition up to the next track',
                 'Just just mix it in. Go for it', 'could you please mix in the next track?']
        for text in phrases:
            self.assertEqual(cc.intent(text),'go',text)
            s=running();provider=Mock();agent=DJAgent(s,evaluator=provider)
            result=agent.message(text,source='voice',message_id='spoken')
            provider.assert_not_called()
            self.assertEqual(result['copilotControl']['action'],'go')
            self.assertEqual(result['copilotControl']['status'],'pending')
        for text in ['do not mix it in','can you explain the next transition?',
                     'mix into the next track later','when will you transition now',
                     'I said mix it in yesterday','mix now or wait?']:
            self.assertIsNone(cc.intent(text),text)

    def test_hold_freezes_countdown_without_sending_audio_commands(self):
        s = running()
        with patch.object(s, 'send') as send, patch.object(s, 'factory') as factory:
            result = self.command(s, 'hold')
        self.assertTrue(result['copilot']['held'])
        self.assertEqual(result['copilot']['heldCountdownBeats'], 48)
        self.assertEqual(result['copilot']['command']['status'], 'confirmed')
        self.assertTrue(result['telemetry']['decks'][0]['playing'])
        send.assert_not_called(); factory.assert_not_called()

    def test_semantic_spoken_request_uses_the_same_native_hold_primitive(self):
        session=running()
        def model(packet):
            self.assertIn('hold',packet['boothControls'])
            return {**decision(),'boothControl':'hold'},{}
        self.assertIsNone(cc.intent('Let this one breathe a little longer'))
        agent=DJAgent(session,evaluator=model)
        agent.enable(True,seconds=120,requests=6)
        agent.message('Let this one breathe a little longer',source='voice',message_id='voice-hold')
        state=settled(agent)
        self.assertEqual(state['copilotControl']['status'],'confirmed')
        self.assertEqual(state['copilotControl']['id'],'voice-hold')
        self.assertTrue(session.snapshot()['copilot']['held'])

    def test_engine_error_is_reported_instead_of_another_start_confirmation(self):
        s=running();s.update(active=False,phase='error',lastError='Incoming cue moved.',message='Catalog refreshed.')
        provider=Mock();agent=DJAgent(s,evaluator=provider)
        result=agent.message('start mixing into the next track',source='voice',message_id='mix')
        self.assertEqual(result['copilotControl']['status'],'blocked')
        self.assertIn('Incoming cue moved.',result['messages'][-1]['text'])
        self.assertNotIn('confirmation',result['messages'][-1]['text']);provider.assert_not_called()
        from afterhours.dj_agent import music_context,playback_policy
        self.assertEqual(music_context(s.snapshot())['executionError'],'Incoming cue moved.')
        self.assertIn('no confirmation is missing',playback_policy(s.snapshot(),None))

    def test_stale_context_or_start_identity_never_accepts_a_control(self):
        for field, value in [('sessionId','old'),('startedAt',9),('planId','old'),('index',1),('trackIds',['0','2'])]:
            s = running(); expected = cc.context(s.snapshot()); expected[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'set changed'):
                s.copilot_command('hold','one',expected)
            self.assertIsNone(s.snapshot()['copilot']['command'])

    def test_stale_incomplete_or_manual_decks_fail_before_a_command(self):
        mutations = [lambda t:t.update(sampledAt=96), lambda t:t.update(sampledAt=101),
                     lambda t:t.update(sampledAt=float('nan')), lambda t:t.update(crossfader=.5),
                     lambda t:t['decks'][1].update(playing=True), lambda t:t['decks'][1].update(playing=None),
                     lambda t:t['decks'][0].update(id='changed'), lambda t:t['decks'][1].update(id='changed')]
        for mutate in mutations:
            s=running(); mutate(s.state['telemetry'])
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):self.command(s,'hold')
            self.assertIsNone(s.snapshot()['copilot']['command'])

    def test_retime_preserves_actual_entry_and_uses_only_verified_future_exits(self):
        s=running(); current=copy.deepcopy(s.state['telemetry']['decks'][0]); current['beatPosition']=140
        original=s.state['executionPlan']['selections'][0]
        later=copy.deepcopy(original); later.update(id='later-measured',entryBeat=128,entrySeconds=128*60/155,exitBeat=192,exitSeconds=192*60/155,endBeat=224)
        s.state['analysis']['tracks'][0]['cueCandidates'].append(later)
        window=cc.retime(s.snapshot(),current,32)
        self.assertEqual(window['entryBeat'],64); self.assertEqual(window['mixAtBeat'],192)
        self.assertEqual(window['candidateId'],'later-measured')
        self.assertEqual(window['originalCandidateId'],original['id'])
        later['replayVerified']=False
        with self.assertRaisesRegex(ValueError,'No verified'):cc.retime(s.snapshot(),current,32)

    def test_timed_exit_uses_nearest_verified_option_and_reports_interval(self):
        s = running()
        current = s.state['telemetry']['decks'][0]
        original = s.state['executionPlan']['selections'][0]
        candidates = s.state['analysis']['tracks'][0]['cueCandidates']
        for beat in (140, 220, 280):
            candidates.append({**original, 'id': f'candidate-{beat}', 'exitBeat': beat,
                               'exitSeconds': beat*60/155, 'endBeat': beat+16,
                               'endSeconds': (beat+16)*60/155, 'replayVerified': beat != 280})
        first = cc.retime(s.snapshot(), current, 16, entry_interval=30)
        second = cc.retime(s.snapshot(), current, 16, entry_interval=60)
        self.assertEqual(first['mixAtBeat'], 140)
        self.assertEqual(second['mixAtBeat'], 220)
        self.assertEqual(second['requestedEntryInterval'], 60)
        self.assertAlmostEqual(second['plannedEntryInterval'], (220-64)*60/155)
        opening = cc.retime(s.snapshot(), current, 16, entry_interval=60,
                           actual_entry={'entryBeat':0, 'entrySeconds':0})
        self.assertEqual(opening['entryBeat'], 0)
        self.assertEqual(opening['mixAtBeat'], 144)

    def test_timed_exit_leaves_a_full_ramp_and_settle_window_after_preload(self):
        s = running()
        current = {**s.state['telemetry']['decks'][0], 'beatPosition': 85}
        nearest = cc.retime(s.snapshot(), current, 8, entry_interval=30)
        window = cc.retime(s.snapshot(), current, 8, entry_interval=30, earliest_beat=nearest['mixAtBeat']+4)
        self.assertEqual(window['mixAtBeat'], nearest['mixAtBeat']+4)

    def test_timed_exit_refuses_a_verified_window_minutes_after_the_requested_entry(self):
        s = running()
        current = {**s.state['telemetry']['decks'][0], 'beatPosition': 156}
        original = s.state['executionPlan']['selections'][0]
        s.state['analysis']['tracks'][0]['cueCandidates'].append({
            **original, 'id': 'much-later', 'exitBeat': 1024, 'endBeat': 1056,
            'exitSeconds': 1024 * 60 / 155, 'endSeconds': 1056 * 60 / 155,
        })
        with self.assertRaisesRegex(ValueError, 'within the requested cadence'):
            cc.retime(s.snapshot(), current, 16, entry_interval=30)

    def test_timed_exit_moves_one_bar_inside_a_verified_outro_after_late_preload(self):
        s = running()
        current = {**s.state['telemetry']['decks'][0], 'beatPosition': 123}
        window = cc.retime(s.snapshot(), current, 16, entry_interval=25)
        self.assertEqual(window['mixAtBeat'], 132)
        self.assertEqual(window['endBeat'], 148)
        self.assertLessEqual(window['endBeat'], s.state['executionPlan']['selections'][0]['endBeat'])

    def test_delayed_mixer_check_can_only_retry_within_the_bounded_verified_outro(self):
        s = running()
        current = {**s.state['telemetry']['decks'][0], 'beatPosition': 123}
        window = cc.retime(s.snapshot(), current, 16, entry_interval=25, latest_beat=132)
        self.assertEqual((window['mixAtBeat'], window['endBeat']), (132, 148))
        with self.assertRaisesRegex(ValueError, 'No verified handoff'):
            cc.retime(s.snapshot(), current, 16, entry_interval=25, latest_beat=131)

    def test_live_set_follows_a_verified_outro_when_the_requested_interval_is_missed(self):
        session = running()
        current = {**session.state['telemetry']['decks'][0], 'beatPosition':123}
        recovered = cc.automatic_timed_window(session.snapshot(), current, 16, 15, latest_beat=132)
        self.assertEqual(recovered['mixAtBeat'], 132)
        self.assertEqual(recovered['basis'], 'cadence-recovery-measured-exit')
        self.assertEqual(recovered['requestedEntryInterval'], 15)
        self.assertEqual(recovered['scheduledFromBeat'], current['beatPosition'])
        self.assertEqual(recovered['guardedFromBeat'], current['beatPosition']+8)
        self.assertEqual(recovered['cadenceTargetBeat'], recovered['entryBeat']+15*current['bpm']/60)
        self.assertGreater(recovered['plannedEntryInterval'], 15)
        self.assertEqual(recovered['endBeat']-recovered['mixAtBeat'], 16)
        with self.assertRaises(cc.NoVerifiedHandoff):
            cc.automatic_timed_window(session.snapshot(), current, 16, 15, latest_beat=131)
        with self.assertRaises(cc.NoVerifiedHandoff):
            cc.automatic_timed_window(session.snapshot(), current, 32, 15)

    def test_timed_recovery_uses_the_closest_measured_bar_when_the_first_exit_is_early(self):
        snapshot = running().snapshot()
        current = {**snapshot['telemetry']['decks'][0], 'beatPosition':23.34,'bpm':127.5967636}
        first = {**snapshot['executionPlan']['selections'][0], 'entryBeat':0, 'exitBeat':64,
                 'endBeat':96, 'maxBlendBeats':32}
        later = {**first, 'id':'later', 'exitBeat':259, 'endBeat':323, 'maxBlendBeats':64}
        snapshot['executionPlan']['selections'][0] = first
        snapshot['analysis']['tracks'][0]['cueCandidates'] = [first, later]
        recovered = cc.automatic_timed_window(snapshot, current, 8, 60, earliest_beat=63.55)
        self.assertEqual(recovered['basis'], 'cadence-recovery-measured-exit')
        self.assertEqual((recovered['mixAtBeat'],recovered['endBeat']),(88,96))
        self.assertAlmostEqual(recovered['plannedEntryInterval'],41.38,places=2)
        bounded = cc.automatic_timed_window(snapshot, current, 8, 60, earliest_beat=63.55, latest_beat=80)
        self.assertEqual(bounded['mixAtBeat'],80)

    def test_untimed_tempo_recovery_can_use_a_later_verified_bar(self):
        s = running()
        current = {**s.state['telemetry']['decks'][0], 'beatPosition': 123}
        window = cc.retime(s.snapshot(), current, 16, earliest_beat=132, latest_beat=136,
                           allow_outro_bars=True)
        self.assertEqual((window['entryBeat'],window['mixAtBeat'],window['endBeat']), (64,132,148))
        self.assertNotIn('requestedEntryInterval',window)
        with self.assertRaisesRegex(ValueError, 'No verified handoff'):
            cc.retime(s.snapshot(),current,32,earliest_beat=132,latest_beat=136,allow_outro_bars=True)

    def test_retime_keeps_loop_evidence_only_at_the_alternative_cues_exact_exit(self):
        from afterhours.vdj_mixing import measured_bridge_loop
        s = running()
        candidates=s.state['analysis']['tracks'][0]['cueCandidates']
        original=candidates[0]
        candidates.append({**original,'id':'later','exitBeat':144,'endBeat':176,
                           'exitSeconds':144*60/155,'endSeconds':176*60/155,'loopBridgeBeats':8})
        current={**s.state['telemetry']['decks'][0],'beatPosition':136}
        exact=cc.retime(s.snapshot(),current,16,allow_outro_bars=True)
        self.assertEqual(exact['candidateId'],'later')
        self.assertEqual(measured_bridge_loop({'genreFamily':'house'},exact),8)
        shifted=cc.retime(s.snapshot(),{**current,'beatPosition':140},16,allow_outro_bars=True)
        self.assertEqual(shifted['mixAtBeat'],148)
        self.assertIsNone(measured_bridge_loop({'genreFamily':'house'},shifted))

    def test_go_failure_keeps_hold_and_does_not_seek_or_mix(self):
        s=running(); self.command(s,'hold')
        s.state['telemetry']['decks'][0]['beatPosition']=140
        self.command(s,'go')
        with patch.object(s,'send') as send:
            result=s.service_copilot(None,s.state['telemetry']['decks'][0],2,Mock(),155,s.state['timing'])
        self.assertIsNone(result);self.assertTrue(s.state['copilot']['held'])
        self.assertEqual(s.state['copilot']['command']['status'],'failed');send.assert_not_called()

    def test_next_intents_advance_the_cued_record_instead_of_skipping_it(self):
        for phrase in ('next','please play the next track','skip to the next track','transition now'):
            self.assertEqual(cc.intent(phrase),'go')
        self.assertEqual(cc.intent('skip the next track'),'skip')

    def test_quick_go_uses_the_cued_native_grid_and_keeps_the_fade(self):
        s=running()
        s.update(mixStyle='quick',executionPlan=None,analysis=None,
                 timing={'entryBeat':64,'mixAtBeat':128,'endBeat':136,'phraseBeats':4,
                         'fadeBeats':8,'basis':'native-beat-grid','segmentBeats':72})
        self.command(s,'go')
        current=s.state['telemetry']['decks'][0]
        with patch.object(s,'send') as send:
            window=s.service_copilot(None,current,2,Mock(),155,s.state['timing'])['window']
        self.assertEqual((window['mixAtBeat'],window['endBeat']),(84,92))
        self.assertEqual(s.state['copilot']['command']['status'],'confirmed')
        send.assert_not_called()

    def test_go_is_consumed_once_and_clears_hold_after_a_valid_window(self):
        s=running();self.command(s,'hold');expected=cc.context(s.snapshot());self.command(s,'go')
        current=s.state['telemetry']['decks'][0]
        change=s.service_copilot(None,current,2,Mock(),155,s.state['timing'])
        self.assertEqual(change['window']['mixAtBeat'],128);self.assertFalse(s.state['copilot']['held'])
        s.copilot_command('go','go',expected)
        self.assertIsNone(s.service_copilot(None,current,2,Mock(),155,s.state['timing']))
        with self.assertRaisesRegex(ValueError,'different request'):s.copilot_command('skip','go',expected)

    def test_skip_replans_verified_tracks_and_preserves_current_cue(self):
        s=running(); order, prepared=cc.skip_plan(s.snapshot())
        self.assertEqual([t['id'] for t in order],['0','2','1','3'])
        self.assertEqual(prepared['executionPlan']['selections'][0],s.state['executionPlan']['selections'][0])
        self.assertEqual([t['id'] for t in s.state['order']],['0','1','2','3'])
        self.assertEqual(prepared['executionPlan']['transitions'][0]['fadeBeats'],16)

    def test_skip_is_not_published_until_the_silent_deck_load_is_confirmed(self):
        s=running(); self.command(s,'skip');ready=Mock()
        def load(*args):
            self.assertEqual([t['id'] for t in s.state['order']],['0','1','2','3'])
            self.assertEqual(args[2],2)
            self.assertEqual(args[1]['id'],'2')
        ready.side_effect=load
        reading=copy.deepcopy(s.state['telemetry']);reading['decks'][1]['id']='2'
        with patch('afterhours.cue_planner.verify_receipts'),patch.object(s,'telemetry',return_value=reading):
            change=s.service_copilot(None,s.state['telemetry']['decks'][0],2,ready,155,s.state['timing'])
        self.assertEqual([t['id'] for t in s.state['order']],['0','2','1','3'])
        self.assertEqual(change['plan']['orderIds'],['0','2','1','3'])
        self.assertTrue(change['plan']['applied']);ready.assert_called_once()
        self.assertEqual(s.state['copilot']['command']['status'],'confirmed')

    def test_uncertain_skip_is_never_retried_or_published_as_a_queue_change(self):
        s=running(); self.command(s,'skip');expected=cc.context(s.snapshot())
        ready=Mock(side_effect=BridgeError('Native load unconfirmed'))
        with patch('afterhours.cue_planner.verify_receipts'), self.assertRaises(BridgeError):
            s.service_copilot(None,s.state['telemetry']['decks'][0],2,ready,155,s.state['timing'])
        self.assertEqual([t['id'] for t in s.state['order']],['0','1','2','3'])
        self.assertEqual(s.state['copilot']['command']['status'],'unconfirmed')
        s.copilot_command('skip','skip',expected)
        self.assertIsNone(s.service_copilot(None,s.state['telemetry']['decks'][0],2,ready,155,s.state['timing']))
        ready.assert_called_once()

    def test_changed_current_during_skip_cannot_be_retimed(self):
        s=running(); self.command(s,'skip');reading=copy.deepcopy(s.state['telemetry'])
        reading['decks'][0]['id']='3';reading['decks'][1]['id']='2'
        with patch('afterhours.cue_planner.verify_receipts'),patch.object(s,'telemetry',return_value=reading),self.assertRaises(ValueError):
            s.service_copilot(None,s.state['telemetry']['decks'][0],2,Mock(),155,s.state['timing'])
        self.assertEqual([t['id'] for t in s.state['order']],['0','1','2','3'])

    def test_skip_respects_locks_and_pending_background_revision(self):
        s=running();s.state['setlist']['lockedIds']=['1']
        with self.assertRaisesRegex(ValueError,'locked'):self.command(s,'skip')
        s.state['setlist']['lockedIds']=[];s.state['queuedUpdate']={'status':'analyzing'}
        with self.assertRaisesRegex(ValueError,'pending queue'):self.command(s,'skip')
        s.state['queuedUpdate']=None;s.state['setlist']['allowReorder']=False
        with self.assertRaisesRegex(ValueError,'reordering'):self.command(s,'skip')

    def test_off_cancels_a_pending_command_and_preserves_takeover_receipt(self):
        s=running();self.command(s,'skip');self.command(s,'off')
        self.assertTrue(s.cancel.is_set());self.assertEqual(s.state['copilot']['mode'],'off')
        self.assertFalse(s.stop_audio)
        self.assertEqual(s.copilot_requests['skip']['status'],'cancelled')
        s._copilot_result('unconfirmed','Old result','skip')
        self.assertEqual(s.state['copilot']['command']['action'],'off')
        self.assertEqual(s.state['copilot']['command']['status'],'pending')

    def test_off_confirms_only_after_worker_cleanup_and_reports_uncertain_ramp_cancellation(self):
        from contextlib import nullcontext
        for failed in (False,True):
            s=running();self.command(s,'off')
            s.factory=lambda *args,**kwargs:nullcontext(Mock())
            if failed:s.mixer=Mock(cancel_ramps=Mock(side_effect=BridgeError('No acknowledgement')))
            s.control_lock.acquire()
            s._work(lambda client:s.check())
            self.assertEqual(s.state['phase'],'takeover');self.assertFalse(s.active)
            self.assertEqual(s.state['copilot']['command']['status'],'unconfirmed' if failed else 'confirmed')

    def test_takeover_during_skip_never_commits_a_late_revision(self):
        s=running();self.command(s,'skip');reading=copy.deepcopy(s.state['telemetry']);reading['decks'][1]['id']='2'
        def load(*args):self.command(s,'off')
        with patch('afterhours.cue_planner.verify_receipts'),patch.object(s,'telemetry',return_value=reading),self.assertRaises(Cancelled):
            s.service_copilot(None,s.state['telemetry']['decks'][0],2,load,155,s.state['timing'])
        self.assertEqual([t['id'] for t in s.state['order']],['0','1','2','3'])
        self.assertEqual(s.state['copilot']['command']['action'],'off')

    def test_on_while_paused_does_not_start_playback(self):
        s=running();s.update(active=False,phase='takeover')
        for deck in s.state['telemetry']['decks']:deck['playing']=False
        with patch.object(s,'start') as start,patch.object(s,'send') as send:
            result=self.command(s,'on')
        self.assertEqual(result['copilot']['mode'],'leading');start.assert_not_called();send.assert_not_called()

    def test_voice_and_typed_controls_share_the_same_provider_free_path(self):
        for source in ('typed','voice'):
            s=running();provider=Mock();agent=DJAgent(s,evaluator=provider)
            with self.subTest(source=source):
                first=agent.message('hold',source=source,message_id='message',approved=True)
                second=agent.message('hold',source=source,message_id='message',approved=True)
                self.assertTrue(s.state['copilot']['held']);provider.assert_not_called()
                self.assertEqual(first['messages'],second['messages'])
                self.assertEqual(first['copilotControl']['status'],'confirmed')
                self.assertEqual(first['allowance']['remaining'],0)

    def test_agent_updates_the_same_reply_when_async_control_finishes(self):
        s=running();agent=DJAgent(s,evaluator=Mock());agent.message('go',message_id='one',approved=True)
        s.service_copilot(None,s.state['telemetry']['decks'][0],2,Mock(),155,s.state['timing'])
        reply=agent.snapshot()
        self.assertEqual(reply['copilotControl']['status'],'confirmed')
        self.assertEqual(reply['messages'][-1]['id'],'one-reply')
        self.assertIn('verified cue',reply['messages'][-1]['text'])


class CopilotHTTPTests(unittest.TestCase):
    def test_native_endpoint_and_chat_voice_path_keep_auth_identity_and_no_provider_calls(self):
        import http.client
        import json
        from http.server import ThreadingHTTPServer
        from afterhours import server
        s=running();provider=Mock();agent=DJAgent(s,evaluator=provider)
        httpd=ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        thread=threading.Thread(target=httpd.serve_forever,daemon=True);thread.start()
        def call(path,body,token=True,origin=None):
            connection=http.client.HTTPConnection('127.0.0.1',httpd.server_port,timeout=3)
            headers={'Content-Type':'application/json'}
            if token:headers['X-DJ-Token']=server.TOKEN
            if origin:headers['Origin']=origin
            connection.request('POST',path,json.dumps(body),headers)
            response=connection.getresponse();result=response.status,json.loads(response.read());connection.close();return result
        try:
            with patch('time.time',return_value=100),patch.object(server,'DJ_SESSION',s),patch.object(server,'DJ_AGENT',agent),patch.object(server,'LIVE_VOICE') as voice:
                body={'action':'copilot','command':'hold','commandId':'native','expected':cc.context(s.snapshot())}
                self.assertEqual(call('/api/set-session',body,token=False)[0],403)
                self.assertEqual(call('/api/set-session',body,origin='https://example.com')[0],403)
                wrong={**body,'expected':{**body['expected'],'planId':'stale'}}
                self.assertEqual(call('/api/set-session',wrong)[0],409)
                status,result=call('/api/set-session',body)
                self.assertEqual(status,202);self.assertTrue(result['copilot']['held'])
                duplicate=call('/api/set-session',body)[1]
                self.assertEqual(duplicate['copilot']['command']['id'],'native')
                status,result=call('/api/dj-agent',{'action':'message','text':'go','messageId':'voice-control','source':'voice','sessionId':'voice-session'})
                self.assertEqual(status,200);self.assertEqual(result['copilotControl']['status'],'pending')
                voice.require.assert_called_once_with('voice-session')
                provider.assert_not_called()

                s.update(active=False, phase='takeover')
                s.state['copilot'].update(mode='off', command={'id':'new-off','action':'off','status':'confirmed'})
                for deck in s.state['telemetry']['decks']:
                    deck.update(loaded=True, playing=False)
                resume={'action':'copilot','command':'resume','commandId':'http-resume',
                        'expected':cc.context(s.snapshot()),'heldRecording':['old-off',1,'0']}
                with patch.object(s, 'start') as start:
                    self.assertEqual(call('/api/set-session',resume)[0],409)
                    start.assert_not_called()
                    status,result=call('/api/set-session',{**resume,'heldRecording':['new-off',1,'0']})
                    self.assertEqual(status,200)
                    self.assertEqual(result['copilot']['command']['heldRecording'],['new-off',1,'0'])
                    start.assert_called_once()
        finally:
            httpd.shutdown();httpd.server_close();thread.join(2)


if __name__ == '__main__':unittest.main()
