"""State and guard regressions. Synthetic engine; live evidence is separate."""
import copy
import re
import threading
import unittest
from contextlib import nullcontext
from unittest.mock import Mock, call, patch
from afterhours.dj_bridge import BridgeError
from afterhours.vdj_session import DJSession, Cancelled, CueReplayMismatch, NativeStreamRejected, validate_order, number, search_terms, song_identity, segment_window
from test_vdj_catalog import CatalogPeer
from afterhours.vdj_catalog import load_selected
from afterhours.vdj_catalog import identity


def tracks():
    return [{'id':'a','bpm':120,'title':'A'},{'id':'b','bpm':124,'title':'B'},
            {'id':'c','bpm':140,'title':'C'}]


class SessionTests(unittest.TestCase):
    def test_fractional_native_cue_shift_rejects_the_paused_incoming_recording(self):
        session = DJSession(threading.Lock())
        candidate = {'id':'offshore','title':'Offshore','bpm':127.88,'camelot':'8A',
                     'engine':{'selection_id':identity('track')}}
        cue = {'entryBeat':0.0, 'entrySeconds':8.29, 'recordingDuration':417.816}
        client = Mock()
        with patch.object(session, 'analyze_deck', return_value=candidate), \
                patch.object(session, 'sleep'), patch.object(session, 'send') as send, \
                patch.object(session, 'q', side_effect=[-14.51896095, 8289]), \
                patch.object(session, 'match_tempo') as tempo:
            with self.assertRaises(CueReplayMismatch) as raised:
                session.prepare_deck(client, candidate, 2, 122, cue)
        self.assertEqual(raised.exception.expected, {'beat':0.0, 'seconds':8.29})
        self.assertEqual(raised.exception.observed, {'beat':-14.51896095, 'seconds':8.289})
        self.assertTrue(all('play' not in args[1] for args, _ in send.call_args_list))
        tempo.assert_not_called()

    def test_timed_cue_mismatch_refits_only_an_unpinned_planner_selection(self):
        for source, pinned in [('planner',False),('planner',True),('operator',False)]:
            with self.subTest(source=source, pinned=pinned):
                session=DJSession(threading.Lock())
                a,b,c=tracks()
                session.update(order=[a,b,c],index=0,active=True,phase='playing',
                    entryCadence=[60,30],rollingQueue={'status':'ready'},setPins=['b'] if pinned else [],
                    setlist={'sourceById':{'a':'operator','b':source,'c':'planner'},
                             'allowReorder':True,'lockedIds':[]})
                session.state['copilot']['mode']='leading'
                error=CueReplayMismatch(b,{'entryBeat':0.0,'entrySeconds':8.29},-14.51896,8.289)
                with patch('afterhours.cue_planner.complete_background_queue',
                           return_value={'analysis':{},'executionPlan':{'transitions':[]}}) as fit, \
                        patch('afterhours.cue_planner.verify_receipts'), \
                        patch.object(session.availability,'record') as blocked:
                    if source=='operator' or pinned:
                        with self.assertRaises(CueReplayMismatch):
                            session.substitute_rejected_preload(1,cue_mismatch=error)
                        fit.assert_not_called()
                        self.assertEqual(session.state['order'],[a,b,c])
                    else:
                        retained,plan=session.substitute_rejected_preload(1,cue_mismatch=error)
                        self.assertEqual(retained,[a,c])
                        self.assertTrue(plan['applied'])
                        self.assertEqual(fit.call_args.args[4],1)
                        self.assertTrue(session.state['active'])
                        self.assertEqual(session.state['events'][-1]['kind'],'queue-skipped')
                        self.assertEqual(session.state['events'][-1]['observedCue']['beat'],-14.51896)
                    blocked.assert_not_called()

    def test_suspect_playing_transport_is_confirmed_without_dispatching_controls(self):
        session = DJSession(threading.Lock())
        client = Mock()
        paused = {'deck': 2, 'loaded': True, 'id': 'expected', 'playing': False}
        playing = {**paused, 'playing': True, 'beatPosition': 133}
        fresh = {'decks': [{}, playing]}
        for suspect in (BridgeError('OSC read timed out'), {'decks': [{}, paused]},
                        {'decks': [{}, {'deck': 2, 'unavailable': True}]}):
            with self.subTest(suspect=suspect), patch.object(session, 'telemetry', side_effect=[suspect, fresh]) as telemetry:
                self.assertIs(session.read_playing_transport(client, 2, 'expected'), fresh)
                self.assertEqual(telemetry.call_args_list, [call(client, clock_only=False), call(client, clock_only=True)])
        client.send.assert_not_called()

    def test_confirmed_transport_change_or_failed_read_stops_scheduling(self):
        session = DJSession(threading.Lock())
        client = Mock()
        paused = {'deck': 2, 'loaded': True, 'id': 'expected', 'playing': False}
        for row, reason in ((paused, 'Deck B is paused'),
                            ({**paused, 'id': 'replacement', 'playing': True}, 'Deck B no longer holds the expected recording'),
                            ({'deck': 2, 'unavailable': True}, 'Deck B transport could not be verified')):
            with self.subTest(row=row), patch.object(session, 'telemetry', side_effect=[{'decks': [{}, paused]}, {'decks': [{}, row]}]):
                with self.assertRaisesRegex(BridgeError, reason):
                    session.read_playing_transport(client, 2, 'expected')
        client.send.assert_not_called()

    def test_unexpected_worker_failure_logs_code_locations_without_request_data(self):
        s = DJSession(threading.Lock(), client_factory=lambda *args, **kwargs: nullcontext(None))
        invoked = []

        def fail_once(client):
            invoked.append(True)
            raise TypeError('fixture private request')

        with self.assertLogs('afterhours.vdj_session', level='ERROR') as logs:
            s._launch(fail_once, 'preparing')
            s.worker.join(2)
        self.assertEqual(invoked, [True])
        self.assertEqual(s.state['phase'], 'error')
        self.assertIn('TypeError', logs.output[0])
        self.assertIn('fail_once', logs.output[0])
        self.assertNotIn('fixture private request', logs.output[0])

    def test_control_state_is_a_small_isolated_cache_read(self):
        s=DJSession(threading.Lock())
        telemetry={'sampledAt':123,'crossfader':.25,'decks':[{'deck':1,'playing':False,'controls':{'volume':.8}}]}
        s.update(telemetry=telemetry,candidates=[{'id':'large-library','data':'x'*100000}],analysis={'data':'y'*100000})
        # A control read must not copy the library, map the program, or request
        # a new OSC sample. It remains available while the engine owns OSC.
        with patch.object(s,'snapshot',side_effect=AssertionError('full snapshot')),patch.object(s,'program',side_effect=AssertionError('full program')),patch.object(s,'factory',side_effect=AssertionError('OSC')):
            with s.control_lock:
                reading=s.control_state()
        self.assertEqual(set(reading),{'sessionId','telemetry'})
        self.assertEqual(reading['sessionId'],s.state['id'])
        self.assertEqual(reading['telemetry'],telemetry)
        reading['telemetry']['decks'][0]['controls']['volume']=0
        self.assertEqual(s.state['telemetry']['decks'][0]['controls']['volume'],.8)

    def test_control_state_preserves_absent_readings_and_new_session_identity(self):
        s=DJSession(threading.Lock())
        before=s.control_state()
        self.assertIsNone(before['telemetry'])
        s.new_set()
        after=s.control_state()
        self.assertNotEqual(after['sessionId'],before['sessionId'])
        self.assertIsNone(after['telemetry'])

    def test_worker_waits_for_read_only_poll_without_losing_or_replaying_command(self):
        commands=[]
        class Peer:
            def __enter__(self):return self
            def __exit__(self,*args):pass
        control=threading.Lock();control.acquire()
        s=DJSession(control,client_factory=lambda *args,**kwargs:Peer());s.observing=True
        s._launch(lambda client:commands.append('prepare-once'),'preparing')
        self.assertTrue(s.snapshot()['active']);self.assertEqual(commands,[])
        with self.assertRaisesRegex(ValueError,'already running'):
            s._launch(lambda client:commands.append('duplicate'),'preparing')
        with s.lock:s.observing=False;control.release()
        s.worker.join(2)
        self.assertFalse(s.worker.is_alive());self.assertEqual(commands,['prepare-once'])
        self.assertFalse(s.snapshot()['active'])

    def test_relocated_record_waits_for_native_browser_selection(self):
        s=DJSession(threading.Lock());s.folder={'id':'folder'}
        track={'id':'record','engine':{'selection_id':'wanted'}}
        s.positions={'record':{'query':'Daft Punk','position':0}}
        state={'row':0,'pending':False};rows=['first','wanted']
        def send(client,script):
            if script=='browser_scroll +1':state['pending']=True
            elif 'top' in script:state['row']=0
        def settle(seconds):
            if state['pending']:state.update(row=min(1,state['row']+1),pending=False)
        with patch('afterhours.vdj_session.folder_state',return_value={'id':'folder'}),patch('afterhours.vdj_session.selection',side_effect=lambda c:{'id':rows[state['row']]}),patch.object(s,'q',return_value=2),patch.object(s,'send',side_effect=send),patch.object(s,'sleep',side_effect=settle),patch.object(s,'wait_observed'),patch.object(s,'service_telemetry'):
            s.select(None,track)
        self.assertEqual(s.positions['record']['position'],1)

    def test_idle_stop_waits_for_telemetry_owner_then_pauses_once(self):
        commands=[]
        class Peer:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def send(self,script):commands.append(script)
            def query(self,script,kind=None):return False
        control=threading.Lock();control.acquire()
        s=DJSession(control,client_factory=lambda *args,**kwargs:Peer())
        stopped=s.stop()
        self.assertTrue(stopped['active']);self.assertEqual(commands,[])
        control.release();s.worker.join(2)
        self.assertFalse(s.worker.is_alive());self.assertFalse(s.snapshot()['active'])
        self.assertEqual(commands,['deck 1 pause & deck 2 pause'])
        self.assertEqual(s.snapshot()['phase'],'stopped')

    def test_stop_confirms_paused_program_even_when_spare_cleanup_fails(self):
        for resumed in (False, True):
            with self.subTest(resumed=resumed):
                client = Mock()
                client.query.side_effect = [False, False, resumed] + ([] if resumed else [False])
                session = DJSession(threading.Lock(), client_factory=lambda *args, **kwargs: nullcontext(client))
                session.update(telemetry={'decks':[{'deck':1,'playing':True},{'deck':2,'playing':True}]})
                with patch.object(session, 'retry_paused_cleanup', side_effect=BridgeError('Spare identity changed')):
                    session.confirm_native_stop()
                client.send.assert_called_once_with('deck 1 pause & deck 2 pause')
                current = session.snapshot()
                self.assertEqual(current['telemetry']['decks'][0]['playing'], resumed)
                self.assertIn('unconfirmed' if resumed else 'Both decks are paused', current['message'])

    def test_takeover_from_paused_ready_set_changes_mode_without_audio_command(self):
        s=DJSession(threading.Lock());s.update(phase='ready',active=False,executionPlan={'applied':True})
        with patch.object(s,'factory') as engine:
            result=s.stop(pause=False)
            self.assertEqual(result['phase'],'takeover')
            self.assertTrue(result['executionPlan']['applied'])
            self.assertFalse(result['active'])
            engine.assert_not_called()

    def test_setlist_locks_reorder_policy_and_plan_invalidation(self):
        s=DJSession(threading.Lock());pool=[{'id':str(i),'title':str(i),'bpm':155} for i in range(4)];s.update(candidates=pool);s.plan(['0','1','2','3'])
        with patch.object(s,'factory') as engine:
            s.edit_setlist('lock','1',True)
            with self.assertRaisesRegex(ValueError,'locked'):s.edit_setlist('reorder',ids=['1','0','2','3'])
            with self.assertRaisesRegex(ValueError,'locked'):s.plan(['0','2','1','3'])
            s.edit_setlist('allow-reorder',value=False)
            with self.assertRaisesRegex(ValueError,'off'):s.plan(['3','1','2','0'])
            s.update(analysis={'fixture':True},aiReview={'fixture':True},executionPlan={'applied':True})
            changed=s.edit_setlist('reorder',ids=['0','1','3','2'])
            self.assertIsNone(changed['analysis']);self.assertIsNone(changed['executionPlan']);self.assertEqual(changed['setlist']['sourceById']['3'],'operator');self.assertIn('1',changed['setlist']['lockedIds'])
            s.update(active=True)
            with self.assertRaisesRegex(ValueError,'Stop'):s.edit_setlist('lock','1',False)
            s.update(active=False,telemetry={'decks':[{'playing':True}]})
            with self.assertRaisesRegex(ValueError,'Stop'):s.edit_setlist('allow-reorder',value=True)
            engine.assert_not_called()

    def test_remove_future_track_keeps_live_and_cued_decks_and_other_staged_edits(self):
        s=DJSession(threading.Lock())
        s.update(candidates=[{'id':str(i),'title':str(i),'bpm':124} for i in range(5)])
        s.plan(['0','1','2','3'],mix_style='quick')
        s.set_reorder_preference(False)
        s.update(active=True,index=0,phase='playing')
        with patch.object(s,'factory') as engine:
            s.revise_queue(['0','1','2','3','4'],source='operator')
            for protected in ('0','1'):
                with self.assertRaisesRegex(ValueError,'protected'):
                    s.edit_setlist('remove',protected)
            result=s.edit_setlist('remove','2')
            self.assertEqual([t['id'] for t in result['order']],['0','1','2','3'])
            self.assertEqual([t['id'] for t in result['queuedUpdate']['tracks']],['0','1','3','4'])
            self.assertEqual(result['queuedUpdate']['sourceById']['4'],'operator')
            order,_=s.adopt_queue(1)
            self.assertEqual([t['id'] for t in order],['0','1','3','4'])
            self.assertFalse(s.snapshot()['setlist']['allowReorder'])
            engine.assert_not_called()

    def test_remove_while_paused_preserves_locks_and_requires_a_complete_set(self):
        s=DJSession(threading.Lock())
        s.update(candidates=[{'id':str(i),'title':str(i),'bpm':124} for i in range(3)])
        s.plan(['0','1','2'])
        s.edit_setlist('lock','1',True)
        with self.assertRaisesRegex(ValueError,'Unlock'):
            s.edit_setlist('remove','1')
        result=s.edit_setlist('remove','2')
        self.assertEqual([t['id'] for t in result['order']],['0','1'])
        with self.assertRaisesRegex(ValueError,'at least two'):
            s.edit_setlist('remove','0')

    def test_phrase_window_includes_blend_and_anchors_to_cue(self):
        w=segment_window(63.9999,64.5,60,155)
        self.assertEqual((w['mixAtBeat'],w['endBeat'],w['fadeBeats']),(192,256,64))
        # Incoming was audible before the previous blend/load finished.
        self.assertEqual(segment_window(64,150,60,155),w)
        short=segment_window(64,64,20,155)
        self.assertEqual((short['mixAtBeat'],short['fadeBeats']),(116,24))
        self.assertEqual(short['mixAtBeat']%4,0)
        late=segment_window(64,202,60,155)
        self.assertEqual(late['mixAtBeat'],256)
        self.assertEqual(late['endBeat'],320)
        final=segment_window(64,140,60,155,final=True)
        self.assertEqual((final['mixAtBeat'],final['endBeat'],final['fadeBeats']),(256,256,0))

    def test_style_validation_and_quick_preview(self):
        s=DJSession(threading.Lock());s.update(candidates=tracks())
        for style in ('unknown',{},None):
            with self.assertRaises(ValueError):s.plan(['a','b'],mix_style=style)
        with self.assertRaises(ValueError):s.plan(['a','b'],excerpt_bars=3)
        self.assertEqual(s.plan(['a','b'],mix_style='quick')['fadeBeats'],8)
        self.assertEqual(segment_window(64,64,20,155,'quick')['fadeBeats'],8)

    def test_wait_services_clock_and_cancels_without_transport(self):
        s=DJSession(threading.Lock());s.update(phase='playing',index=0)
        now=[0.0];samples=[]
        def sleep(seconds):now[0]+=seconds
        def telemetry(c,preloading=False):
            samples.append(now[0]);s.update(telemetry={'sampledAt':now[0]})
        with patch('afterhours.vdj_session.time.monotonic',side_effect=lambda:now[0]),patch('afterhours.vdj_session.time.time',side_effect=lambda:now[0]),patch.object(s,'sleep',side_effect=sleep),patch.object(s,'telemetry',side_effect=telemetry),patch.object(s,'send') as send:
            s.wait_observed(None,5)
            self.assertGreaterEqual(len(samples),8)
            self.assertLessEqual(max(b-a for a,b in zip(samples,samples[1:])),.81)
            send.assert_not_called()
            s.cancel.set()
            with self.assertRaises(Cancelled):s.wait_observed(None,5)

    def test_loading_deck_does_not_erase_valid_audible_clock(self):
        s=DJSession(threading.Lock())
        readings={'loaded':True,'get_beatpos':88,'get_beat_bar':.25,'get_filepath':'a',
                  'get_title':'A','get_artist':'Artist','play':True,'get_bpm':155,
                  'get_time elapsed absolute':12345,'get_vu_meter':.2,'is_sync':False}
        def q(c,script,kind=None):
            if script=='crossfader':return 0
            if script.startswith('deck 2'):raise BridgeError('Track still loading')
            return readings[script.removeprefix('deck 1 ')]
        with patch.object(s,'q',side_effect=q):
            t=s.telemetry(None,preloading=True)
            self.assertTrue(s.program()['playing'])
            self.assertTrue(t['decks'][1]['unavailable'])
            self.assertIsNone(t['decks'][1]['playing'])
            with self.assertRaises(BridgeError):s.telemetry(None)
        with patch.object(s,'q',side_effect=lambda c,v,k=None:1 if v=='crossfader' else q(c,v,k)):
            with self.assertRaises(BridgeError):s.telemetry(None,preloading=True)

    def test_explicit_restore_validates_identity_and_never_starts_audio(self):
        pool=[{'id':'virtualdj:'+identity(str(i)),'title':str(i),'artist':'Fixture Artist','bpm':155,
               'engine':{'selection_id':identity(str(i)),'folder_id':identity('fixture-folder')}} for i in range(4)]
        saved={'final':{'candidates':pool,'order':pool,'secondsPerTrack':20}}
        s=DJSession(threading.Lock())
        with patch.object(s,'factory') as factory,patch.object(s,'send') as send:
            result=s.restore(saved)
            self.assertEqual(result['phase'],'stopped');self.assertFalse(result['active'])
            self.assertIsNone(result['telemetry']);self.assertEqual(len(result['order']),4)
            factory.assert_not_called();send.assert_not_called()
        broken=copy.deepcopy(saved);broken['final']['candidates'][0]['engine']['selection_id']='bad'
        with self.assertRaises(ValueError):s.restore(broken)
        self.assertEqual(s.snapshot()['order'],result['order'])

    def test_catalog_search_without_an_order_can_be_restored_without_engine_commands(self):
        pool=[{'id':'virtualdj:'+identity(str(i)),'title':str(i),'artist':'Fixture Artist','bpm':125,
               'engine':{'selection_id':identity(str(i)),'folder_id':identity('fixture-folder')}} for i in range(3)]
        s=DJSession(threading.Lock())
        with patch.object(s,'factory') as factory,patch.object(s,'send') as send:
            result=s.restore({'candidates':pool,'order':[],'query':'Fresh house'})
            self.assertEqual(result['phase'],'catalog-ready');self.assertEqual(result['order'],[])
            self.assertEqual(result['candidates'],pool);self.assertEqual(result['query'],'Fresh house')
            self.assertFalse(result['active']);factory.assert_not_called();send.assert_not_called()

    def test_multi_catalog_recovery_preserves_provenance_and_requires_an_explicit_search_scope(self):
        peer=CatalogPeer()
        pool=[{'id':'virtualdj:'+identity(locator),'title':str(i),'artist':'Fixture Artist','bpm':120,
               'engine':{'selection_id':identity(locator),'folder_id':identity('origin-'+str(i))}}
              for i,locator in enumerate([peer.locator,'synthetic://track/43'])]
        saved={'candidates':pool,'order':pool,'secondsPerTrack':20}
        s=DJSession(threading.Lock())
        with patch.object(s,'factory') as engine,patch.object(s,'send') as send:
            with self.assertRaisesRegex(ValueError,'folder binding'):s.restore(saved)
            saved['catalogFolder']={'id':identity(peer.folder)}
            result=s.restore(saved)
            self.assertEqual(result['candidates'],pool)
            self.assertEqual(result['catalogFolder'],saved['catalogFolder'])
            self.assertFalse(result['active']);self.assertIsNone(result['executionPlan'])
            engine.assert_not_called();send.assert_not_called()
            invalid=copy.deepcopy(saved);invalid['catalogFolder']['id']='unverified'
            with self.assertRaisesRegex(ValueError,'binding is invalid'):s.restore(invalid)
            self.assertEqual(s.snapshot()['catalogFolder'],saved['catalogFolder'])
        with patch.object(s,'sleep'):
            s.load(peer,pool[0],2)
        self.assertEqual(peer.loaded_locator,peer.locator)
        self.assertFalse(peer.playing)
        self.assertEqual(sum(command.endswith('deck 2 load') for command in peer.sent),1)

    def test_recovered_multi_catalog_scope_cannot_change_during_exact_identity_load(self):
        peer=CatalogPeer()
        pool=[{'id':'virtualdj:'+identity(locator),'title':str(i),'artist':'Fixture Artist','bpm':120,
               'engine':{'selection_id':identity(locator),'folder_id':identity('origin-'+str(i))}}
              for i,locator in enumerate([peer.locator,'synthetic://track/43'])]
        s=DJSession(threading.Lock());s.restore({'candidates':pool,'order':pool,'catalogFolder':{'id':identity(peer.folder)}})
        original_send=peer.send
        def move_folder(command):
            original_send(command)
            if command.startswith('search '):peer.folder='synthetic://another-folder'
        with patch.object(s,'sleep'),patch.object(peer,'send',side_effect=move_folder):
            with self.assertRaisesRegex(BridgeError,'folder changed'):s.load(peer,pool[0],2)
        self.assertEqual(peer.loaded_locator,'')
        self.assertFalse(any(command.endswith('deck 2 load') for command in peer.sent))

    def test_fresh_set_clears_old_program_without_transport_and_refuses_active_playback(self):
        s=DJSession(threading.Lock());old=s.state['id'];record={'id':'virtualdj:'+identity('old-track'),'title':'Old track'};s.update(candidates=[record])
        with patch.object(s,'send') as send:
            fresh=s.new_set();self.assertNotEqual(fresh['id'],old);self.assertEqual(fresh['candidates'],[record])
            self.assertEqual(fresh['order'],[])
            self.assertEqual(s.program()['library'],[]);self.assertFalse(s.program()['playing']);send.assert_not_called()
        s.update(telemetry={'decks':[{'playing':True}]})
        with self.assertRaises(ValueError):s.new_set()

    def test_paid_plan_recovery_revalidates_receipts_without_network_or_transport(self):
        from test_cue_planner import fixture
        from afterhours import ai_mix, cue_planner
        _,report,answer=fixture()
        ids={t['id']:'virtualdj:'+identity('recover-'+t['id']) for t in report['tracks']}
        for track in report['tracks']:
            track['id']=ids[track['id']]
            track['engine']={'selection_id':track['id'].removeprefix('virtualdj:'),'folder_id':identity('fixture-folder')}
        for row in answer['selections']:row['trackId']=ids[row['trackId']]
        for row in answer['transitions']:
            row['fromId']=ids[row['fromId']];row['toId']=ids[row['toId']]
        report['fingerprint']=cue_planner.fingerprint(report['tracks'])
        review=ai_mix.validate_plan(answer,report,30)
        saved={'candidates':report['tracks'],'order':report['tracks'],'secondsPerTrack':30,
               'mixStyle':'phrase','analysis':report,'aiReview':{**review,'appliedToPlayback':True},
               'executionPlan':{'applied':True},'active':True}
        s=DJSession(threading.Lock())
        with patch('afterhours.cue_planner.verify_receipts') as receipts,patch.object(s,'factory') as engine,patch('afterhours.ai_mix.plan') as paid:
            result=s.restore(saved)
            receipts.assert_called_once_with(report);engine.assert_not_called();paid.assert_not_called()
            self.assertEqual(result['aiReview']['selections'],review['selections'])
            self.assertFalse(result['aiReview']['appliedToPlayback']);self.assertFalse(result['active'])
            self.assertIsNone(result['executionPlan'])
            with self.assertRaisesRegex(ValueError,'Apply'):s.start()
            for mutation in ('changed-report','stale-plan','changed-receipt'):
                bad=copy.deepcopy(saved)
                if mutation=='changed-report':bad['analysis']['tracks'][0]['duration']+=1
                elif mutation=='stale-plan':bad['aiReview']['analysisFingerprint']='old'
                else:receipts.side_effect=ValueError('changed receipt')
                with self.subTest(mutation=mutation),self.assertRaises(ValueError):s.restore(bad)
                self.assertEqual(s.snapshot()['aiReview'],result['aiReview'])
            engine.assert_not_called();paid.assert_not_called()

    def test_four_track_phrase_run_survives_preload_and_long_fades(self):
        self.simulate_run(60,64,192)

    def test_thirty_second_trial_keeps_entry_spacing_after_preload(self):
        self.simulate_run(30,32,128)

    def test_a_four_deck_set_plays_a_b_c_d_in_rotation(self):
        self.simulate_run(30,8,128,four=True)

    def test_a_four_deck_set_keeps_long_blends_inside_each_pair(self):
        self.simulate_run(60,64,192,four=True)

    def test_four_track_two_bar_handoffs_preserve_preload_and_spacing(self):
        self.simulate_run(30,8,128)

    def test_live_queue_revision_is_executed_after_handoff_by_the_same_worker(self):
        self.simulate_run(30,8,128,revise=True)

    def test_gradual_tempo_route_survives_preloading_and_three_real_executor_handoffs(self):
        self.simulate_run(30,8,128,bpms=[125,125.5,126,126.5])

    def test_background_additions_ready_during_last_track_extend_running_set(self):
        self.simulate_run(30,8,128,background=True)

    def test_four_deck_preparation_selection_cannot_receive_the_performance_handoff(self):
        for spare in (3,4):
            with self.subTest(selected=spare):
                self.simulate_run(30,8,128,background=True,selected_spare=spare)

    def test_held_worker_passes_original_exit_then_go_uses_verified_later_exit(self):
        self.simulate_run(30,8,128,control='hold-go')

    def test_skip_replaces_only_the_silent_deck_then_executes_the_revised_set(self):
        self.simulate_run(30,8,128,control='skip')

    def test_resume_keeps_the_playing_recording_without_reloading_or_restarting_it(self):
        self.simulate_run(30,8,128,control='resume')

    def test_explicit_handback_executes_three_handoffs_from_changed_live_eq(self):
        self.simulate_run(30,8,128,control='handback')

    def test_paused_alignment_handback_resumes_current_position_and_completes_three_handoffs(self):
        self.simulate_run(30,8,128,control='paused-handback')

    def test_phrase_start_requires_measurements_after_order_changes(self):
        s=DJSession(threading.Lock());pool=[{'id':str(i),'title':str(i),'bpm':155} for i in range(4)]
        s.update(candidates=pool);s.plan(['0','1','2','3'])
        with patch.object(s,'_launch') as launch:
            with self.assertRaisesRegex(ValueError,'Analyze'):s.start()
            s.update(analysis={'cueVersion':'fixture'},executionPlan={'applied':True})
            s.edit_setlist('reorder',ids=['0','1','3','2'])
            with self.assertRaisesRegex(ValueError,'Analyze'):s.start()
            launch.assert_not_called()
            s.plan(['0','1','3','2'],mix_style='quick');s.start()
            launch.assert_called_once()

    def test_quick_playback_adopts_an_insertion_during_a_fade_and_extends_its_final_track(self):
        self.simulate_run(15,8,104,quick_insert=True)

    def test_final_record_keeps_runway_when_native_duration_is_temporarily_missing(self):
        s=DJSession(threading.Lock())
        track={'id':'current'}
        s.update(queuedUpdate={'status':'analyzing','tracks':[track,{'id':'next'}]},
                 analysis={'tracks':[{'id':'previous','duration':400},{'id':'current','duration':336.149}]})
        s.state['copilot']['mode']='leading'
        for controls in ({},{'durationMs':0},{'durationMs':None}):
            with self.subTest(controls=controls):
                self.assertGreater(s.final_preparation_runway(track,0,{'position':99.584,'controls':controls}),200)
        self.assertLess(s.final_preparation_runway(track,0,{'position':330,'controls':{}}),16)
        self.assertEqual(s.final_preparation_runway(track,0,{'position':100,'controls':{'durationMs':98000}}),0)
        self.assertEqual(s.final_preparation_runway({'id':'previous'},0,{'position':100,'controls':{}}),0)
        s.update(rollingQueue={'status':'held'}, queuedUpdate=None)
        self.assertGreater(s.final_preparation_runway(track,0,{'position':100,'controls':{}}),200)
        s.update(rollingQueue=None)
        self.assertEqual(s.final_preparation_runway(track,0,{'position':100,'controls':{}}),0)

    def test_measured_set_keeps_playing_until_the_staged_handoff_is_verified(self):
        self.simulate_run(30,8,128,background=True,late_background=True)

    def test_measured_set_crossfades_on_time_when_stems_drop_before_play(self):
        self.simulate_run(30,8,128,control='stems-drop')

    def test_delayed_mixer_read_retimes_without_starting_the_incoming_deck_twice(self):
        self.simulate_run(30,8,128,late_mixer=True)

    def simulate_run(self,seconds,blend,mix_at,revise=False,bpms=None,background=False,control=None,selected_spare=None,four=False,quick_insert=False,late_background=False,late_mixer=False):
        """Deterministic peer simulation; this does not play or assess music."""
        s=DJSession(threading.Lock());pool=[{'id':str(i),'title':str(i),'bpm':155,'genre':'Drum & Bass','camelot':'2A'} for i in range(4)]
        for track in pool:
            track['engine']={'selection_id':identity(track['id'])}
        s.update(candidates=pool);s.plan([t['id'] for t in pool],seconds=seconds)
        from test_cue_planner import fixture
        from afterhours import ai_mix, cue_planner
        _,report,response=fixture(seconds=seconds)
        if control == 'hold-go' or late_background or late_mixer:
            extended=report['tracks'][1 if late_background else 0]
            later=copy.deepcopy(extended['cueCandidates'][0])
            later.update(id='verified-later',entryBeat=128,exitBeat=192,endBeat=224,
                         entrySeconds=128*60/155,exitSeconds=192*60/155,endSeconds=224*60/155)
            if late_mixer:
                later.update(exitBeat=144,endBeat=176,exitSeconds=144*60/155,endSeconds=176*60/155)
            extended['cueCandidates'].append(later)
            report['fingerprint']=cue_planner.fingerprint(report['tracks'])
        if bpms:
            for t,e,bpm in zip(pool,report['tracks'],bpms):
                t.update(bpm=bpm,engine={'selection_id':identity(t['id'])});e['bpm']=bpm
            report['targetBpm']=bpms[0];report['fingerprint']=cue_planner.fingerprint(report['tracks'])
        for row in response['transitions']:
            row['blendBeats']=blend
            if blend==8:row['technique']='short-handoff'
        review=ai_mix.validate_plan(response,report,seconds)
        plan=cue_planner.compile_plan(review,report,pool,seconds);plan['applied']=True
        s.update(analysis=report,aiReview=review,executionPlan=plan)
        full_report=copy.deepcopy(report)
        if background:
            report=copy.deepcopy(report);report['tracks']=report['tracks'][:2];report['fingerprint']=cue_planner.fingerprint(report['tracks'])
            response=copy.deepcopy(response);response['selections']=response['selections'][:2];response['transitions']=response['transitions'][:1]
            review=ai_mix.validate_plan(response,report,seconds);plan=cue_planner.compile_plan(review,report,pool[:2],seconds);plan['applied']=True
            s.update(order=pool[:2],analysis=report,aiReview=review,executionPlan=plan)
        if quick_insert:s.plan(['0','1'],seconds=seconds,excerpt_bars=0,mix_style='quick')
        now=[0.0];cross=[0.0];fade=[None];sent=[];reads=[];ramps={};revised=[False];steered=[];loads=[]
        insertions=[]; delayed_readbacks=[]
        master=[selected_spare or 1];sync_sources=[]
        from afterhours import deck_rotation
        numbers=deck_rotation.FOUR if four else deck_rotation.TWO
        controls={n:{'eq_low_freq':.5,'eq_mid_freq':.5,'eq_high_freq':.5,'level':1,'stem Vocal':.5,'stem Instru':.5} for n in numbers}
        decks=[{'deck':n,'loaded':True,'playing':False,'id':'','bpm':155,'beatPosition':64,'phase':0,'level':.2,'sync':True} for n in numbers]
        if four:
            # C and D start as track preparation left them: fader down, filter off centre.
            for n in (3,4):controls[n].update(level=0,filter=.3)
            s.choose_rotation=lambda client:deck_rotation.FOUR
        if selected_spare:
            decks.extend({'deck':n,'loaded':True,'playing':False,'id':f'spare-{n}',
                          'bpm':111 if n==3 else 90,'beatPosition':0,'phase':0,'level':0,'sync':False} for n in (3,4))
        if control in ('resume','handback','paused-handback'):
            s.update(phase='takeover',index=0,startedAt=-10,mixerBaselines=copy.deepcopy(controls))
            decks[0].update(id='0',playing=True,beatPosition=80)
            decks[1].update(id='1')
            if control=='handback':
                s.state['copilot']['mode']='off'
                controls[1].update(eq_low_freq=.77,eq_mid_freq=.86,eq_high_freq=1,level=.48)
            if control=='paused-handback':
                s.update(phase='error',lastError='Deck tempos moved after alignment. No transition was sent.')
                decks[0]['playing']=False;cross[0]=.46
        class Peer:
            def status(self):return {'automix':False,'decks':decks[:2]}
            def send(self,script):return send(self,script)
        def sleep(seconds):
            now[0]+=seconds
            for d in decks:
                if d['playing']:d['beatPosition']+=seconds*d['bpm']/60
            if fade[0]:
                start,incoming,tempo,actual_blend=fade[0];fraction=min(1,(now[0]-start)/(actual_blend*60/tempo))
                cross[0]=fraction if incoming==2 else 1-fraction
            for start,deck,key,a,b,duration in ramps.values():
                fraction=min(1,(now[0]-start)/duration)
                controls[deck][key]=a+(b-a)*fraction
                if key=='pitch':
                    d=decks[deck-1];d['bpm']=pool[int(d['id'])]['bpm']*(1+(controls[deck][key]-.5)*.16)
        def telemetry(c,**kwargs):
            if late_background:
                for deck in decks:
                    # Optional native controls timed out on the final playing
                    # deck while the next waveform was still being measured.
                    deck.update(position=deck['beatPosition']*60/155,controls={})
            t={'decks':copy.deepcopy(decks[:4 if four else 2]),'crossfader':cross[0],'sampledAt':now[0]}
            s.update(telemetry=t);reads.append(now[0])
            if control in ('hold-go','skip') and s.state['phase']=='playing' and s.state['index']==0:
                from afterhours.copilot_commands import context
                if not steered and decks[0]['beatPosition']>=80:
                    action='hold' if control=='hold-go' else 'skip'
                    s.copilot_command(action,action,context(s.snapshot()));steered.append(action)
                elif control=='hold-go' and steered==['hold'] and decks[0]['beatPosition']>=140:
                    self.assertFalse(any('play_sync' in command or command.startswith('mix_now') for command,_,_ in sent))
                    s.copilot_command('go','go',context(s.snapshot()));steered.append('go')
            if revise and not revised[0] and s.state['phase']=='playing':
                revised[0]=True;ids=['0','1','3','2']
                s.revise_queue(ids,mix_transitions=[{'fromId':a,'toId':b,'technique':'short-handoff',
                    'blendBeats':blend,'reason':'Synthetic live steer.'} for a,b in zip(ids,ids[1:])])
                self.assertEqual([t['id'] for t in s.state['order']],['0','1','2','3'])
            if quick_insert and not insertions and s.state['phase']=='mixing' and s.state['activeHandoff']:
                staged=s.revise_queue(['0','1','2'])
                self.assertEqual(staged['queuedUpdate']['protectedIds'],['0','1'])
                insertions.append('2')
            if (quick_insert and insertions==['2'] and s.state['phase']=='playing'
                    and s.state['index']==2 and s.state['fadeBeats']==0):
                staged=s.revise_queue(['0','1','2','3'])
                self.assertEqual(staged['queuedUpdate']['protectedIds'],['0','1','2'])
                insertions.append('3')
            if background and not revised[0] and s.state['phase']=='playing':
                revised[0]=True;ids=['0','1','2','3']
                s.revise_queue(ids,mix_transitions=[{'fromId':a,'toId':b,'technique':'short-handoff',
                    'blendBeats':blend,'reason':'Synthetic new selections.'} for a,b in zip(ids,ids[1:])])
                self.assertEqual(s.state['queuedUpdate']['status'],'needs-analysis')
            return t
        def prepare(c,track,deck,target,cue=None,checked=None):
            if quick_insert and track['id']=='3' and checked is None:
                from afterhours.vdj_session import TrackMetadataChanged
                raise TrackMetadataChanged({**track,'camelot':'1A'})
            loads.append((track['id'],deck))
            s.wait_observed(c,5)
            if cue:self.assertEqual(cue['trackId'],track['id'])
            else:self.assertTrue(quick_insert)
            decks[deck-1].update(id=track['id'],bpm=target,playing=False,beatPosition=cue['entryBeat'] if cue else 64)
        def send(c,script):
            sent.append((script,now[0],copy.deepcopy(decks)))
            if script=='crossfader 0%':cross[0]=0
            elif script=='deck 1 play':decks[0]['playing']=True
            elif script.startswith('mix_now'):
                # Reproduce the native failure: a global mix selected C/D.
                incoming=selected_spare or (2 if cross[0]<.5 else 1)
                decks[incoming-1]['playing']=True;fade[0]=(now[0],incoming,decks[incoming-1]['bpm'],int(script.split()[1][:-2]))
            elif script.endswith(' masterdeck on'):
                master[0]=int(script.split()[1])
            elif script.endswith(' play_sync'):
                incoming=int(script.split()[1]);sync_sources.append((incoming,master[0]))
                decks[incoming-1].update(playing=True,bpm=decks[master[0]-1]['bpm'])
            elif script.endswith(' pause'):decks[int(script.split()[1])-1]['playing']=False
            elif script.endswith(' unload'):
                n=int(script.split()[1]);self.assertFalse(decks[n-1]['playing']);decks[n-1].update(id='')
            elif "repeat_start_fade 'afterhours_crossfade'" in script:
                clock_deck,a,b,beats=re.fullmatch(r"deck (\d) repeat_start_fade 'afterhours_crossfade' ([\d.]+) ([\d.]+) (\d+)bt & crossfader",script).groups()
                fade[0]=(now[0],2 if float(b)==1 else 1,decks[int(clock_deck)-1]['bpm'],int(beats))
            elif 'repeat_start_fade' in script:
                clock_deck,name,a,b,beats,deck,key=re.fullmatch(r"(?:deck (\d) )?repeat_start_fade '([^']+)' ([\d.]+) ([\d.]+) (\d+)bt & deck (\d) ([\w ]+)",script).groups()
                self.assertEqual(clock_deck,deck)
                ramps[name]=(now[0],int(deck),key,float(a),float(b),int(beats)*60/decks[int(deck)-1]['bpm'])
            elif 'repeat_stop ' in script:
                for command in script.split(' & '):
                    name=command.split("'")[1]
                    if name=='afterhours_crossfade':fade[0]=None
                    else:ramps.pop(name,None)
            else:
                for command in script.split(' & '):
                    head,amount=command.rsplit(' ',1);_,deck,key=head.split(maxsplit=2)
                    controls[int(deck)][key]=float(amount.rstrip('%'))/100
        def query(c,script,kind=None):
            if script=='crossfader':return cross[0]
            if script=='mixermode':return True
            if script=='pitch_lock':return False
            if script in ('fake_eq','fake_mixer'):return False
            _,deck,key=script.split(maxsplit=2);deck=int(deck)
            if key=='masterdeck':return master[0]==deck
            if key.startswith('cross_assign'):return key==("cross_assign 'left'" if deck%2 else "cross_assign 'right'")
            if key.startswith('has_stems'):
                return not (control=='stems-drop' and s.state['index']==0 and deck==2
                            and decks[0]['beatPosition']>=mix_at-9)
            if key=='get_beatpos':
                if (late_mixer and not delayed_readbacks and deck==1 and s.state['index']==0
                        and decks[0]['playing'] and decks[0]['beatPosition']>=mix_at):
                    self.assertFalse(decks[1]['playing'])
                    delayed_readbacks.append(True)
                    sleep(2*60/155)
                return decks[deck-1]['beatPosition']
            if key=='get_bpm absolute':return pool[int(decks[deck-1]['id'])]['bpm']
            if key=='get_filepath':return decks[deck-1]['id']
            if key=='pitch_range':return .08
            if key=='pitch':return .5+(decks[deck-1]['bpm']/pool[int(decks[deck-1]['id'])]['bpm']-1)/.16
            return decks[deck-1]['playing'] if key=='play' else controls[deck][key]
        class MeasuredJob:
            def __init__(self,session,revision,tracks,cache):
                self.revision=revision;self.cache={};self.done=False;self.error=None;self.result=copy.deepcopy(full_report['tracks'][2:])
            def step(self,c,slack):
                self.done=(s.state['index']==1 and slack>=16
                           and (not late_background or decks[1]['beatPosition']>mix_at+4))
            def close(self,c):pass
            def snapshot(self):return {'status':'analyzing','message':'Synthetic preparation','completed':0,'total':2}
        from afterhours.background_analysis import BackgroundAnalysis
        def launch(fn,phase):
            s.update(active=True)
            fn(Peer())
        with patch('afterhours.vdj_session.BackgroundAnalysis',MeasuredJob if background else BackgroundAnalysis),patch('afterhours.cue_planner.verify_receipts'),patch.object(s,'_launch',side_effect=launch),patch.object(s,'prepare_deck',side_effect=prepare),patch.object(s,'sleep',side_effect=sleep),patch.object(s,'telemetry',side_effect=telemetry),patch.object(s,'send',side_effect=send),patch.object(s,'q',side_effect=query),patch('afterhours.vdj_session.time.monotonic',side_effect=lambda:now[0]),patch('afterhours.vdj_session.time.time',side_effect=lambda:now[0]):
            if control in ('handback','paused-handback'):
                from afterhours.dj_agent import DJAgent
                telemetry(Peer());provider=Mock();agent=DJAgent(s,evaluator=provider)
                agent.message('take back over' if control=='paused-handback' else 'go ahead and beat match into the next track',message_id='handback')
                self.assertEqual(s.state['copilot']['command']['status'],'confirmed')
                provider.assert_not_called()
            else:s.start(resume=control=='resume')
        result=s.snapshot();self.assertEqual(result['phase'],'completed')
        outro = next(i for i,(command,_,_) in enumerate(sent) if "'afterhours_outro'" in command and 'repeat_start' in command)
        self.assertTrue(any(command.endswith(' pause') for command,_,_ in sent[outro+1:]))
        if quick_insert:
            self.assertEqual(insertions,['2','3'])
            self.assertEqual(loads,[('0',1),('1',2),('2',1),('3',2)])
            self.assertEqual([(t['from'],t['to']) for t in result['transitions']],[('0','1'),('1','2'),('2','3')])
            self.assertTrue(all(t['timing']['basis']=='native-beat-grid' and t['timing']['fadeBeats']==8
                                for t in result['transitions']))
            self.assertIsNone(result['queuedUpdate'])
            self.assertEqual(result['order'][3]['camelot'],'1A')
            self.assertFalse(any(d['playing'] for d in decks))
            return
        if revise:
            self.assertEqual([t['id'] for t in result['order']],['0','1','3','2'])
            self.assertEqual([(t['from'],t['to']) for t in result['transitions']],[('0','1'),('1','3'),('3','2')])
        self.assertEqual(len(result['transitions']),3)
        blends=[16]*3 if control=='skip' else [blend]*3
        fades=[command for command,_,_ in sent if "repeat_start_fade 'afterhours_crossfade'" in command]
        self.assertEqual(len(fades),3)
        self.assertEqual([int(command.split('bt')[0].rsplit(' ',1)[1]) for command in fades],blends)
        self.assertEqual(sync_sources,[(2,1),(3,2),(4,3)] if four else [(2,1),(1,2),(2,1)])
        self.assertFalse(any(command.startswith('mix_now') for command,_,_ in sent))
        if selected_spare:
            self.assertTrue(all(d['beatPosition']==0 and not d['playing'] for d in decks[2:]))
            self.assertFalse(any('deck 3 ' in command or 'deck 4 ' in command for command,_,_ in sent))
        windows=([128,192,128] if late_background else [192,128,128] if control=='hold-go' else [mix_at]*3)
        actual=[t['timing']['mixAtBeat'] for t in result['transitions']]
        if late_mixer:
            self.assertTrue(delayed_readbacks)
            self.assertGreater(actual[0],mix_at)
            self.assertLessEqual(actual[0],mix_at+32)
            self.assertEqual(actual[1:],windows[1:])
        else:
            self.assertEqual(actual,windows)
        if late_background:
            self.assertEqual(result['transitions'][1]['timing']['basis'],'measured-continuation-exit')
        self.assertTrue(all(t['completedAt']-t['at']>=t['timing']['fadeBeats']*60/(t.get('tempoPlan',{}).get('toBpm',155))-.1 for t in result['transitions']))
        if control=='skip':
            self.assertEqual([t['id'] for t in result['order']],['0','2','1','3'])
            self.assertEqual(loads[:3],[('0',1),('1',2),('2',2)])
        if control in ('resume','handback'):
            self.assertFalse(any(command=='deck 1 play' for command,_,_ in sent))
            self.assertNotIn(('0',1),loads)
            self.assertEqual(result['startedAt'],-10)
        if control=='paused-handback':
            starts=[(command,state) for command,_,state in sent if command=='deck 1 play']
            self.assertEqual(len(starts),1);self.assertEqual(starts[0][1][0]['beatPosition'],80)
            self.assertNotIn(('0',1),loads)
            self.assertEqual(result['startedAt'],-10)
        if bpms:
            self.assertEqual(len([x for x in sent if "repeat_start_fade 'afterhours_tempo'" in x[0]]),3)
            self.assertAlmostEqual(result['telemetry']['decks'][1]['bpm'],bpms[-1],delta=.15)
        if background:self.assertEqual([t['id'] for t in result['order']],['0','1','2','3'])
        if seconds==30 and not bpms and not control and not late_background and not late_mixer:
            for a,b in zip(result['transitions'],result['transitions'][1:]):
                self.assertAlmostEqual(b['at']-a['at'],64*60/155,delta=.3)
        self.assertTrue(all(t['timing']['basis'] in ('measured-audio-candidate','operator-steered-measured-exit',
                                                  'measured-continuation-exit') for t in result['transitions']))
        self.assertFalse(any(d['playing'] for d in decks))
        if four:
            # Each record is loaded once, on the next deck round; each blend
            # fades to the incoming deck's side and stays inside its pair.
            self.assertEqual(loads,[('0',1),('1',2),('2',3),('3',4)])
            self.assertEqual([command.split()[-4] for command in fades],['1.000000','0.000000','1.000000'])
            self.assertEqual([command.split()[1] for command in fades],['1','2','3'])
            pauses=[command for command,_,_ in sent if command.endswith(' pause')]
            self.assertEqual(pauses,['deck 1 pause','deck 2 pause','deck 3 pause','deck 4 pause'])
            for (a,b),first,last in zip(((1,2),(2,3),(3,4)),fades,fades[1:]+[None]):
                begin=next(i for i,(command,_,_) in enumerate(sent) if command==first)
                end=next((i for i,(command,_,_) in enumerate(sent) if command==f'deck {a} pause'),len(sent))
                inside=[command for command,_,_ in sent[begin:end]]
                outside=[n for n in (1,2,3,4) if n not in (a,b)]
                self.assertFalse(any(f'deck {n} ' in command for n in outside for command in inside),(a,b))
            self.assertEqual(result['deckRotation'],[1,2,3,4])
            # A finished record leaves its deck empty for track preparation.
            self.assertEqual([command for command,_,_ in sent if command.endswith(' unload')],
                             [f'deck {n} play ? nothing : deck {n} unload' for n in (1,2,3)])
            self.assertEqual(controls[3]['level'],1);self.assertEqual(controls[4]['filter'],.5)
        observed=reads
        if control=='paused-handback':
            playing_at=next(at for command,at,_ in sent if command=='deck 1 play')
            observed=[at for at in reads if at>=playing_at]
        self.assertLess(max(b-a for a,b in zip(observed,observed[1:])),1)
        self.assertEqual(controls[1]['eq_low_freq'],.5)
        self.assertEqual(controls[2]['eq_low_freq'],.5)
        for index,transition in enumerate(result['transitions']):
            fallback=control=='stems-drop' and index==0
            self.assertEqual(transition['recipe']['leadControl'],'frequency-eq' if fallback else 'stems')
            for sample in transition['mixerSamples']:
                for lead in ('eq_mid_freq',) if fallback else ('stem Vocal','stem Instru'):
                    self.assertFalse(sample['outgoing'][lead]>.03 and sample['incoming'][lead]>.03)
        for deck in (1,2):
            self.assertEqual(controls[deck]['stem Vocal'],.5)
            self.assertEqual(controls[deck]['stem Instru'],.5)
        self.assertTrue(all(t['mixerSamples'] for t in result['transitions']))

    def test_prepare_refines_small_tempo_change_and_matches_after_seek(self):
        s=DJSession(threading.Lock());source={'id':'a','title':'Fixture','bpm':162.5,'camelot':'2A','engine':{'selection_id':'fixture'}}
        s.update(order=[source],excerptBars=16);sent=[]
        def match(c,t,d,b):sent.append('MATCH')
        with patch.object(s,'analyze_deck',return_value={**source,'bpm':163.0}),patch.object(s,'send',side_effect=lambda c,v:sent.append(v)),patch.object(s,'sleep'),patch.object(s,'match_tempo',side_effect=match):
            s.prepare_deck(None,source,1,155)
        self.assertEqual(sent[-2:],["deck 1 goto +64",'MATCH'])
        self.assertEqual(s.snapshot()['order'][0]['bpm'],163)
        self.assertFalse(any(' play' in c for c in sent))

    def test_large_tempo_or_key_change_still_requires_replanning(self):
        s=DJSession(threading.Lock());source={'id':'a','title':'Fixture','bpm':162.5,'camelot':'2A'}
        for actual in ({**source,'bpm':170},{**source,'camelot':'9B'}):
            with self.subTest(actual=actual),patch.object(s,'analyze_deck',return_value=actual),patch.object(s,'send') as send:
                with self.assertRaises(BridgeError):s.prepare_deck(None,source,1,155)
                send.assert_not_called()

    def test_tempo_alignment_checks_identity_pause_and_pitch_bound(self):
        s=DJSession(threading.Lock());path='fixture';track={'engine':{'selection_id':identity(path)}}
        readings={'deck 1 play':False,'deck 1 get_filepath':path,'deck 1 get_bpm absolute':163,'deck 1 get_bpm':155,'deck 1 is_fluid':False}
        with patch.object(s,'q',side_effect=lambda c,v,k=None:readings[v]),patch.object(s,'sleep'),patch.object(s,'send') as send:
            s.match_tempo(None,track,1,155)
            send.assert_called_once_with(None,'deck 1 pitch 95.092025%')
            for field,value in [('deck 1 play',True),('deck 1 get_filepath','different'),('deck 1 get_bpm absolute',180)]:
                before=readings[field];readings[field]=value;send.reset_mock()
                with self.assertRaises(BridgeError):s.match_tempo(None,track,1,155)
                send.assert_not_called();readings[field]=before

    def test_fluid_grid_stabilizes_before_pitch_without_starting_audio(self):
        s=DJSession(threading.Lock());path='fixture';track={'engine':{'selection_id':identity(path)}}
        readings={'deck 1 play':False,'deck 1 get_filepath':path,'deck 1 get_bpm absolute':163,'deck 1 get_bpm':155,'deck 1 is_fluid':True,'deck 1 bpm_stabilizer':False};sent=[]
        def send(c,script):
            sent.append(script)
            if script=='deck 1 bpm_stabilizer on':readings['deck 1 bpm_stabilizer']=True
        with patch.object(s,'q',side_effect=lambda c,v,k=None:readings[v]),patch.object(s,'sleep'),patch.object(s,'send',side_effect=send):
            s.match_tempo(None,track,1,155)
        self.assertEqual(sent,['deck 1 bpm_stabilizer on','deck 1 pitch 95.092025%'])

    def test_tempo_confirmation_survives_one_matching_sample_then_native_grid_refinement(self):
        s=DJSession(threading.Lock());path='fixture';track={'engine':{'selection_id':identity(path)}}
        sent=[];native=[128.332857];tempo=[127.000885];reads=[0]
        def query(c,script,kind=None):
            if script=='deck 2 get_bpm':
                reads[0]+=1
                if reads[0]==2:native[0]=127.999229;tempo[0]=126.671577
                return tempo[0]
            return {'deck 2 play':False,'deck 2 get_filepath':path,'deck 2 get_bpm absolute':native[0],
                    'deck 2 is_fluid':False}[script]
        def send(c,script):
            sent.append(script)
            if len(sent)==2:tempo[0]=127.000885
        with patch.object(s,'q',side_effect=query),patch.object(s,'sleep'),patch.object(s,'send',side_effect=send):
            s.match_tempo(None,track,2,127.000885)
        self.assertEqual(len(sent),2)
        self.assertTrue(all(command.startswith('deck 2 pitch ') for command in sent))
        self.assertGreaterEqual(reads[0],6)
        self.assertTrue(s.snapshot()['events'][-1]['gridRefined'])

    def test_tempo_confirmation_rechecks_identity_before_refinement(self):
        s=DJSession(threading.Lock());path='fixture';track={'engine':{'selection_id':identity(path)}}
        sent=[]
        def query(c,script,kind=None):
            return {'deck 2 play':bool(sent),'deck 2 get_filepath':path,'deck 2 get_bpm absolute':128,
                    'deck 2 is_fluid':False}[script]
        with patch.object(s,'q',side_effect=query),patch.object(s,'sleep'),patch.object(s,'send',side_effect=lambda c,v:sent.append(v)):
            with self.assertRaisesRegex(BridgeError,'Incoming deck changed'):s.match_tempo(None,track,2,127)
        self.assertEqual(len(sent),1)

    def test_half_time_analysis_changes_grid_without_starting_or_pitching_audio(self):
        s=DJSession(threading.Lock());path='fixture-track';source={'id':'a','title':'Fixture','bpm':165,'camelot':'11A','engine':{'selection_id':identity(path)}}
        s.update(candidates=[source]);bpm=[86.0];sent=[]
        def query(client,script,kind=None):
            return {'deck 1 loaded':True,'deck 1 play':False,'deck 1 get_filepath':path,'deck 1 get_bpm absolute':bpm[0],
                    'deck 1 get_key':'F#m','deck 1 get_firstbeat':9000,'deck 1 get_time total absolute':264509}[script]
        def send(client,script):
            sent.append(script)
            if 'set_bpm' in script:bpm[0]=172
        with patch.object(s,'q',side_effect=query),patch.object(s,'send',side_effect=send),patch.object(s,'sleep'):
            actual=s.analyze_deck(None,source,1)
        self.assertEqual(actual['bpm'],172)
        self.assertTrue(actual['engineChecked'])
        self.assertEqual(actual['nativeDuration'],264.509)
        self.assertFalse(any(' play' in c or ' pitch' in c for c in sent))

    def test_negative_first_grid_anchor_is_valid_without_a_negative_audio_seek(self):
        s=DJSession(threading.Lock());path='free-your-mind';source={'id':'a','title':'Fixture','bpm':128,'camelot':'12A','engine':{'selection_id':identity(path)}}
        s.update(candidates=[source]);sent=[]
        readings={'deck 1 loaded':True,'deck 1 play':False,'deck 1 get_filepath':path,'deck 1 get_bpm absolute':128,
                  'deck 1 get_key':'C#m','deck 1 get_firstbeat':-2.494,'deck 1 get_time total absolute':264509}
        with patch.object(s,'q',side_effect=lambda c,v,k=None:readings[v]),patch.object(s,'send',side_effect=lambda c,v:sent.append(v)),patch.object(s,'sleep'):
            actual=s.analyze_deck(None,source,1)
        self.assertEqual(actual['bpm'],128);self.assertTrue(actual['engineChecked'])
        self.assertEqual(sent,['deck 1 master_tempo on','deck 1 goto_first_beat'])

    def test_changed_preparation_returns_to_planner_without_playback(self):
        s=DJSession(threading.Lock());pool=tracks()[:2];s.update(candidates=pool,order=pool)
        class Peer:
            def status(self):return {'automix':False,'decks':[{'playing':False},{'playing':False}]}
        sent=[]
        with patch.object(s,'_launch',side_effect=lambda fn,phase:fn(Peer())),patch.object(s,'analyze_deck',side_effect=lambda c,t,d:{**t,'bpm':t['bpm']+1,'camelot':None}),patch.object(s,'q',return_value=False),patch.object(s,'send',side_effect=lambda c,x:sent.append(x)),patch.object(s,'sleep'):
            s.prepare()
        self.assertEqual(s.snapshot()['phase'],'catalog-ready')
        self.assertFalse(any(' play' in c for c in sent))

    def test_fresh_discovery_drops_the_previous_blocked_queue_before_searching(self):
        s = DJSession(threading.Lock())
        pool = tracks()[:2]
        s.update(order=pool, queuedUpdate={'status':'blocked','tracks':pool},
                 executionPlan={'applied':True}, startedAt=15, entryCadence=[60,30])
        peer = Mock()
        peer.status.return_value = {'automix':False,'decks':[{'playing':False}]}
        found = [{'id':row['id'],'artist':'','metadata':row} for row in pool]
        with patch.object(s,'_launch',side_effect=lambda work,phase:work(peer)), \
                patch.object(s,'send'), patch.object(s,'sleep'), \
                patch('afterhours.vdj_session.folder_state',return_value={'id':'crate','name':'Catalog'}), \
                patch('afterhours.vdj_session.selection',side_effect=found+[found[-1]]*2):
            result = s.discover('Fixture')
        self.assertEqual(result['phase'],'catalog-ready')
        self.assertEqual(result['order'],[])
        self.assertIsNone(result['queuedUpdate'])
        self.assertIsNone(result['executionPlan'])
        self.assertIsNone(result['startedAt'])
        self.assertIsNone(result['entryCadence'])

    def test_multi_search_is_bounded_and_deduplicated(self):
        self.assertEqual(search_terms('Goldie; Photek; LTJ Bukem; Goldie'), ['Goldie','Photek','LTJ Bukem'])
        for value in ('a;b;c;d;e', ' ; ; ', 'a" & play'):
            with self.assertRaises(ValueError): search_terms(value)

    def test_different_remixes_are_distinct(self):
        self.assertNotEqual(song_identity({'title':'Same','metadata':{'remix':'Original'}}),
                            song_identity({'title':'Same','metadata':{'remix':'Remix'}}))

    def test_rejects_stale_duplicate_and_excessive_tempo_order(self):
        for ids in (['a','a'],['a','missing'],['a','c'],['a']):
            with self.subTest(ids=ids),self.assertRaises(ValueError):
                validate_order(ids,tracks())
        self.assertEqual([t['id'] for t in validate_order(['b','a'],tracks())],['b','a'])

    def test_extended_rolling_window_requires_measured_timed_playback(self):
        session = DJSession(threading.Lock())
        records = [{'id': str(i), 'title': f'Song {i}', 'bpm': 120} for i in range(21)]
        session.update(candidates=records, phase='catalog-ready')
        selected = [track['id'] for track in records[:16]]
        with self.assertRaisesRegex(ValueError, '2–12'):
            session.plan(selected)
        with self.assertRaisesRegex(ValueError, '12–20'):
            session.plan(selected, mix_style='quick', queue_window=16)
        with self.assertRaisesRegex(ValueError, '12–20'):
            session.plan(selected, queue_window=21)
        self.assertEqual(len(session.plan(selected, queue_window=16)['order']), 16)
        with self.assertRaisesRegex(ValueError, 'requires a measured entry cadence'):
            session.prepare_analysis()
        with self.assertRaisesRegex(ValueError, 'requires a measured rolling set'):
            session.start(entry_cadence=[60, 30])
        self.assertEqual(session.plan(selected[:2])['queueWindow'], 12)

    def test_measured_queue_rejects_tracks_beyond_native_scan_limit(self):
        catalog=tracks()
        catalog[0]['duration']=3374.95
        with self.assertRaisesRegex(ValueError,'measured-mix scan limit'):
            validate_order(['a','b'],catalog)

    def test_plan_does_not_send_engine_commands_and_preserves_catalog(self):
        s=DJSession(threading.Lock());s.update(candidates=tracks(),phase='catalog-ready')
        result=s.plan(['a','b'],20,16)
        self.assertEqual(result['phase'],'ready')
        self.assertEqual(result['secondsPerTrack'],20)
        self.assertEqual(len(s.snapshot()['candidates']),3)
        self.assertFalse(s.control_lock.locked())
        for seconds in (float('nan'),14,301,True):
            with self.assertRaises(ValueError):s.plan(['a','b'],seconds)

    def test_session_snapshot_cannot_mutate_runtime(self):
        s=DJSession(threading.Lock());s.update(candidates=tracks())
        snapshot=s.snapshot();snapshot['candidates'][0]['bpm']=999
        self.assertEqual(s.snapshot()['candidates'][0]['bpm'],120)

    def test_busy_engine_rejects_worker_without_queuing(self):
        lock=threading.Lock();lock.acquire();s=DJSession(lock)
        with self.assertRaises(ValueError):s.discover('test')
        self.assertFalse(s.active)
        self.assertIsNone(s.worker)
        lock.release()

    def test_cancel_prevents_the_next_command(self):
        s=DJSession(threading.Lock());s.cancel.set()
        with self.assertRaises(Exception):s.send(None,'mix_now 8bt')

    def test_program_uses_engine_clock_and_expires(self):
        s=DJSession(threading.Lock())
        deck={'id':'a','playing':True,'bpm':120,'beatPosition':20}
        s.update(order=tracks(),telemetry={'decks':[deck,deck], 'crossfader':0,'sampledAt':100})
        with patch('afterhours.vdj_session.time.time',return_value=101):
            p=s.program();self.assertTrue(p['connected']);self.assertTrue(p['playing']);self.assertEqual(p['clock']['beatPosition'],20)
        with patch('afterhours.vdj_session.time.time',return_value=104):
            p=s.program();self.assertFalse(p['connected']);self.assertFalse(p['playing'])

    def test_program_countdown_follows_go_without_applying_old_window_to_incoming(self):
        s=DJSession(threading.Lock())
        decks=[{'id':'a','playing':True,'bpm':120,'beatPosition':100},
               {'id':'b','playing':False,'bpm':120,'beatPosition':40}]
        s.update(order=tracks(),active=True,phase='playing',index=0,timing={'mixAtBeat':120},
                 executionPlan={'applied':True,'selections':[{'trackId':'a','exitBeat':160},{'trackId':'b','exitBeat':180}]},
                 telemetry={'decks':decks,'crossfader':0,'sampledAt':100})
        with patch('afterhours.vdj_session.time.time',return_value=101):
            self.assertEqual(s.program()['transitionInSeconds'],10)
            decks[1]['playing']=True
            s.update(phase='mixing',telemetry={'decks':decks,'crossfader':.75,'sampledAt':100})
            self.assertEqual(s.program()['current'],'b')
            self.assertEqual(s.program()['transitionInSeconds'],70)

    def test_completed_program_does_not_switch_tracks_when_only_crossfader_moves(self):
        s=DJSession(threading.Lock())
        decks=[{'id':'a','playing':False,'loaded':True},{'id':'b','playing':False,'loaded':True}]
        s.update(order=tracks()[:2],index=1,phase='completed',telemetry={'decks':decks,'crossfader':0,'sampledAt':100})
        with patch('afterhours.vdj_session.time.time',return_value=101):
            p=s.program();self.assertEqual(p['current'],'b');self.assertIsNone(p['nextTrack']);self.assertFalse(p['playing'])
            decks[0]['id']='manual-recording'
            s.update(telemetry={'decks':decks,'crossfader':0,'sampledAt':100})
            self.assertEqual(s.program()['current'],'manual-recording')

    def test_transient_load_read_timeout_recovers_without_second_load(self):
        peer=CatalogPeer();original=peer.query;failed=False
        def query(script,kind=None):
            nonlocal failed
            if peer.sent and not failed:
                failed=True;raise BridgeError('Stream is still preparing')
            return original(script,kind)
        with patch.object(peer,'query',side_effect=query),patch('afterhours.vdj_catalog.time.sleep'):
            result=load_selected(peer,2,**peer.options())
        self.assertTrue(result['verified']);self.assertEqual(len(peer.sent),1)
        self.assertNotIn(' play',peer.sent[0].split(' : ')[-1])

    def test_rejected_foreground_load_is_recoverable_only_for_the_paused_empty_or_exact_deck(self):
        session = DJSession(threading.Lock())
        session.folder = {'id':'catalog'}
        track = {'engine':{'selection_id':identity('exact')}}
        client = Mock()
        for playing, loaded, position, expected in (
                (False,False,'',NativeStreamRejected), (False,True,'exact',NativeStreamRejected),
                (False,True,'another',BridgeError), (True,False,'',BridgeError)):
            with self.subTest(playing=playing,loaded=loaded,position=position):
                readings = {'deck 2 play':playing,'deck 2 loaded':loaded,
                            'deck 2 get_filepath':position,'deck 2 deck_has_error':True}
                client.query.side_effect = lambda script, kind=None: readings[script]
                with patch.object(session,'locate_for_load',return_value=None),patch.object(session,'wait_observed'), \
                        patch('afterhours.vdj_session.load_selected',return_value={'sent':True,'verified':False}) as load:
                    with self.assertRaises(expected) as result:
                        session.load(client,track,2)
                self.assertIs(type(result.exception),expected)
                load.assert_called_once()

    def test_nonfinite_engine_measurements_are_not_live_values(self):
        for value in (float('nan'),float('inf'),True,'',-1):
            with self.assertRaises(BridgeError):number(value,0,1)

if __name__=='__main__':unittest.main()
