"""Conversation-to-ready scenarios with real coordinator/queue validation.

Catalog, OpenAI, decoding and cue application are simulated. These tests never
open a native socket, upload media, start audio or purchase model requests.
"""
import copy
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

from afterhours.dj_agent import DJAgent
from afterhours.vdj_session import DJSession
from test_dj_agent import decision, settled


class ScenarioSession(DJSession):
    def __init__(self):
        super().__init__(threading.Lock(), client_factory=Mock(side_effect=AssertionError('No native connection')))
        self.calls=[]
        self.records=[{'id':c,'title':f'House {c.upper()}','artist':'Scenario artist',
                       'bpm':125+i*.5,'camelot':'8A','genre':'House'} for i,c in enumerate('abcd')]
        self.update(candidates=copy.deepcopy(self.records), telemetry={'decks':[]},maxTempoShift=8)

    def discover_more(self,*args):
        self.calls.append(('search',args));self.update(phase='discovering',active=True)

    def finish_search(self,records=None,error=None):
        self.update(candidates=copy.deepcopy(self.records if records is None else records),active=False,
                    phase='catalog-ready',catalogSearch={'status':'error' if error else 'complete',
                    'added':len(self.records if records is None else records),'message':error or 'Catalog complete.'})

    def prepare_analysis(self):
        self.calls.append(('analyze',));self.update(analysis={'tracks':copy.deepcopy(self.state['order'])},phase='ready')

    def review_mix(self,approved=False):
        assert approved is True
        self.calls.append(('review',));self.update(aiReview={'decision':'propose'},phase='ready')

    def apply_plan(self):
        self.calls.append(('apply',));self.update(executionPlan={'applied':True,'targetBpm':125},phase='ready')


class AutoPreparationScenarios(unittest.TestCase):
    def make(self,result=None):
        s=ScenarioSession();e=Mock(return_value=(result or decision(['a','b','c']),{'responseId':'simulated'}))
        a=DJAgent(s,evaluator=e);return s,a,e

    def send(self,a,text='Build a house set',message_id='seed',**kwargs):
        a.message(text,message_id=message_id,approved=True,approve_preparation=True,**kwargs);settled(a)

    def drain(self,a,n=10):
        for _ in range(n):a.tick(force=True);settled(a)

    def test_one_send_searches_selects_analyzes_plans_and_applies_without_prepare_click(self):
        q={'query':'Dom Dolla','artist':'Dom Dolla','genre':'','bpmMin':40,'bpmMax':300}
        s,a,e=self.make(decision(search=q));s.update(candidates=[])
        self.send(a,'dom dolala');self.assertEqual(a.snapshot()['allowance']['turnCeilingUSD'],.04)
        self.assertTrue(e.call_args.args[0]['preparationPolicy']['automatic'])
        s.finish_search();e.return_value=(decision(['a','b','c']),{'responseId':'selection'})
        self.drain(a)
        self.assertEqual([c[0] for c in s.calls],['search','analyze','review','apply'])
        self.assertEqual(e.call_count,2);self.assertTrue(s.state['executionPlan']['applied'])
        self.assertEqual(a.state['status'],'ready');self.assertIn('Playback is paused',a.state['message'])
        self.assertFalse(s.state['active']);s.factory.assert_not_called()

    def test_existing_catalog_goes_straight_to_preparation_once(self):
        s,a,e=self.make();self.send(a);self.drain(a)
        self.assertEqual([c[0] for c in s.calls],['analyze','review','apply']);e.assert_called_once()
        self.send(a,message_id='seed');a.apply(a.state['revision']);self.drain(a)
        self.assertEqual([c[0] for c in s.calls],['analyze','review','apply']);e.assert_called_once()

    def test_repeated_apply_during_preparation_does_not_regrant_review(self):
        s,a,e=self.make();self.send(a);a.tick(force=True);a.tick(force=True)
        self.assertEqual(a.pipeline['reviewRemaining'],0)
        a.apply(a.state['revision']);self.drain(a)
        self.assertEqual([c[0] for c in s.calls],['analyze','review','apply'])

    def test_legacy_or_missing_approval_never_expands_spending(self):
        for approval,prepare in [(False,True),(True,False),(True,'true')]:
            with self.subTest(approval=approval,prepare=prepare):
                s,a,e=self.make();a.message('House',message_id='old-client',approved=approval,approve_preparation=prepare)
                settled(a);self.drain(a)
                self.assertNotIn(('review',),s.calls);self.assertNotIn(('apply',),s.calls)
                if not approval:e.assert_not_called()
                else:self.assertEqual(a.snapshot()['allowance']['turnCeilingUSD'],.02)

    def test_prepared_order_is_reused_without_analysis_or_paid_review(self):
        s,a,e=self.make();s.plan(['a','b','c']);s.update(executionPlan={'applied':True})
        self.send(a,'Keep this selection');self.drain(a)
        self.assertEqual(s.calls,[]);self.assertIsNone(a.pipeline)

    def test_existing_measurements_and_review_are_reused(self):
        for reviewed,expected in [(False,['review','apply']),(True,['apply'])]:
            with self.subTest(reviewed=reviewed):
                s,a,e=self.make();s.plan(['a','b','c']);s.update(analysis={'tracks':s.state['order']})
                if reviewed:s.update(aiReview={'decision':'propose'})
                self.send(a,'Continue this selection');self.drain(a)
                self.assertEqual([c[0] for c in s.calls],expected)

    def test_question_or_performance_only_never_prepares_music(self):
        for performance in (None,{'prompt':'Look up briefly, then return to mixing.','reason':'Operator direction.'}):
            result={**decision(),'performance':performance}
            s,a,e=self.make(result);self.send(a,'What is next?' if performance is None else 'Look up');self.drain(a)
            self.assertEqual(s.calls,[]);self.assertIsNone(a.pipeline)
            if performance:self.assertEqual(a.state['performanceCommand']['status'],'unavailable')

    def test_director_direction_and_music_can_share_one_message_without_starting_video(self):
        result={**decision(['a','b','c']),'performance':{'prompt':'Look up and give a small nod.','reason':'Requested gesture.'}}
        s,a,e=self.make(result)
        self.send(a,'Build house and give a nod',performance_context={'ready':True,'runId':'a'*32,'ownerId':'browser-owner','character':'nova'})
        self.drain(a);self.assertEqual(a.state['performanceCommand']['status'],'pending')
        self.assertTrue(s.state['executionPlan']['applied']);self.assertEqual(len(s.calls),3)

    def test_manual_play_cancels_preparation_before_review(self):
        s,a,e=self.make();self.send(a);a.tick(force=True)
        s.update(telemetry={'decks':[{'deck':1,'playing':True}]});self.drain(a)
        self.assertEqual(s.calls,[('analyze',)]);self.assertEqual(a.state['status'],'held')
        self.assertIn('manually',a.state['message'])

    def test_takeover_and_queue_edits_cancel_before_review(self):
        for change in ('takeover','queue'):
            with self.subTest(change=change):
                s,a,e=self.make();self.send(a);a.tick(force=True)
                if change=='takeover':s.update(phase='takeover')
                else:s.plan(['a','c'])
                self.drain(a);self.assertEqual(s.calls,[('analyze',)])
                self.assertEqual(a.state['status'],'held')

    def test_allowance_expiry_cannot_launch_delayed_review(self):
        s,a,e=self.make();self.send(a);a.tick(force=True);a.pipeline['deadline']=0;self.drain(a)
        self.assertEqual(s.calls,[('analyze',)]);self.assertEqual(a.state['status'],'held')

    def test_new_direction_during_native_analysis_replaces_only_after_native_finishes(self):
        s,a,e=self.make();original=s.prepare_analysis
        def slow():s.calls.append(('analyze',));s.update(active=True,phase='analyzing')
        s.prepare_analysis=slow;self.send(a);a.tick(force=True)
        e.return_value=(decision(['b','c','d']),{'responseId':'new-selection'})
        self.send(a,'Actually start with B',message_id='new');self.assertEqual(e.call_count,1)
        s.update(active=False,phase='ready',analysis={'tracks':s.state['order']});s.prepare_analysis=original
        self.drain(a)
        self.assertEqual([t['id'] for t in s.state['order']],['b','c','d'])
        self.assertEqual([c[0] for c in s.calls],['analyze','analyze','review','apply'])
        self.assertEqual(e.call_count,2)

    def test_stop_cancels_future_preparation_and_does_not_restart_it(self):
        s,a,e=self.make();self.send(a);a.tick(force=True);a.disable();self.drain(a)
        self.assertEqual(s.calls,[('analyze',)]);self.assertIsNone(a.preparation_grant)

    def test_question_or_gesture_during_preparation_preserves_the_music_job(self):
        for performance in (None,{'prompt':'Give a small nod.','reason':'Operator asked.'}):
            with self.subTest(performance=performance):
                s,a,e=self.make();self.send(a);a.tick(force=True)
                e.return_value=({**decision(),'performance':performance},{'responseId':'question'})
                self.send(a,'Why this direction?' if performance is None else 'Give a nod',message_id='aside')
                self.drain(a)
                self.assertEqual([c[0] for c in s.calls],['analyze','review','apply'])
                self.assertTrue(s.state['executionPlan']['applied']);self.assertIsNone(a.suspended_preparation)

    def test_metadata_correction_is_revalidated_once_before_paid_review(self):
        s,a,e=self.make();original=s.prepare_analysis
        def corrected():
            if not s.calls:
                s.calls.append(('analyze',));s.state['candidates'][1]['camelot']='9A'
                s.update(analysis=None,phase='catalog-ready')
            else:original()
        s.prepare_analysis=corrected;self.send(a);self.drain(a)
        self.assertEqual(s.state['order'][1]['camelot'],'9A')
        self.assertEqual([c[0] for c in s.calls],['analyze','analyze','review','apply'])
        e.assert_called_once()

    def test_unstable_metadata_does_not_loop_or_spend(self):
        s,a,e=self.make()
        def unstable():s.calls.append(('analyze',));s.update(analysis=None,phase='catalog-ready')
        s.prepare_analysis=unstable;self.send(a);self.drain(a,20)
        self.assertEqual(s.calls,[('analyze',),('analyze',)]);self.assertEqual(a.state['status'],'error')

    def test_corrected_tempo_outside_bounds_is_held_before_review(self):
        s,a,e=self.make()
        def corrected():
            s.calls.append(('analyze',));s.state['candidates'][1]['bpm']=150
            s.update(analysis=None,phase='catalog-ready')
        s.prepare_analysis=corrected;self.send(a);self.drain(a)
        self.assertEqual(s.calls,[('analyze',)]);self.assertEqual(a.state['status'],'error')
        self.assertIn('pitch',a.state['message'].lower())

    def test_native_failure_model_failure_and_hold_never_auto_retry_or_start(self):
        for stage in ('analysis','review','apply','hold'):
            with self.subTest(stage=stage):
                s,a,e=self.make()
                fn={'analysis':'prepare_analysis','review':'review_mix','apply':'apply_plan','hold':'review_mix'}[stage]
                if stage=='hold':s.review_mix=Mock(side_effect=lambda **kw:s.update(aiReview={'decision':'hold','summary':'Both excerpts contain competing leads.'}))
                else:setattr(s,fn,Mock(side_effect=ValueError('Simulated '+stage+' failure.')))
                self.send(a);self.drain(a,20)
                getattr(s,fn).assert_called_once();self.assertEqual(a.state['status'],'error')
                self.assertFalse((s.state.get('executionPlan') or {}).get('applied'));self.assertFalse(s.state['active'])
                if stage=='hold':self.assertIn('competing leads',a.state['message'])

    def test_voice_uses_its_existing_shared_allowance_for_automatic_preparation(self):
        s,a,e=self.make();a.enable(True,1800,60)
        a.message('House please',source='voice',message_id='delegated');settled(a);self.drain(a)
        self.assertEqual([c[0] for c in s.calls],['analyze','review','apply'])
        self.assertEqual(a.remaining,58);self.assertFalse(a.auto_prepare_turn)

    def test_typing_during_talk_reuses_shared_allowance_instead_of_adding_spend(self):
        s,a,e=self.make();a.enable(True,1800,60);self.send(a);self.drain(a)
        self.assertEqual(a.remaining,58);self.assertIsNone(a.snapshot()['allowance']['turnCeilingUSD'])

    def test_live_addition_stages_new_recording_without_reloading_playing_or_cued_decks(self):
        s,a,e=self.make(decision(['a','b','d']));s.plan(['a','b','c'])
        s.update(active=True,phase='playing',index=0,executionPlan={'applied':True,'targetBpm':125},
                 telemetry={'decks':[{'id':'a','playing':True},{'id':'b','playing':False}]})
        before=copy.deepcopy(s.state['telemetry']);self.send(a,'Add D to the queue');self.drain(a)
        self.assertEqual([t['id'] for t in s.state['order']],['a','b','c'])
        q=s.state['queuedUpdate'];self.assertEqual([t['id'] for t in q['tracks']],['a','b','d'])
        self.assertEqual(q['status'],'needs-analysis');self.assertEqual(q['protectedIds'],['a','b'])
        self.assertEqual(s.state['telemetry'],before);self.assertEqual(s.calls,[])

    def test_locked_tracks_and_disabled_reordering_cannot_be_changed_by_chat(self):
        for policy in ({'allowReorder':False,'lockedIds':[]},{'allowReorder':True,'lockedIds':['c']}):
            with self.subTest(policy=policy):
                s,a,e=self.make(decision(['c','b','a']));s.plan(['a','b','c']);s.state['setlist'].update(policy)
                self.send(a,'Put C first');self.drain(a)
                self.assertEqual([t['id'] for t in s.state['order']],['a','b','c']);self.assertEqual(s.calls,[])
                self.assertEqual(a.state['status'],'error')

    def test_auto_preparation_permission_is_never_restored_from_disk(self):
        with tempfile.TemporaryDirectory() as directory:
            s,a,e=self.make();a.path=Path(directory)/'agent.json';self.send(a);a.tick(force=True)
            restored=DJAgent(s,a.path,evaluator=e);self.drain(restored)
            self.assertEqual(s.calls,[('analyze',)]);self.assertIsNone(restored.pipeline)
            self.assertIsNone(restored.preparation_grant);self.assertFalse(restored.auto_prepare_turn)

    def test_explicit_chat_retry_can_recover_the_same_order_after_a_native_failure(self):
        s,a,e=self.make();s.plan(['a','b','c'])
        s.update(phase='error',message='Previous native scan failed.',analysis=None)
        self.send(a,'Try preparing this same set again');self.drain(a)
        self.assertEqual([c[0] for c in s.calls],['analyze','review','apply'])
        self.assertTrue(s.state['executionPlan']['applied'])


if __name__=='__main__':unittest.main()
