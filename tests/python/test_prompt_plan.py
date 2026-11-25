"""Prompt-plan model fixtures; no provider calls, real service or audio output."""
import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

from afterhours.dj_agent import DJAgent, MAX_REQUEST_BYTES, planning_request, validate_decision
from afterhours import prompt_plan
from test_dj_agent import decision, settled
from test_dj_request import populated_packet
from test_session_curation import RunSession


def interpreted(minutes=120, **changes):
    return {'durationMinutes':minutes,'peakAfterMinutes':90 if minutes==120 else 60,
            'warmupMinutes':20,'warm':True,'onePeak':True,'darkLate':True,
            'explanation':'Two hours requested; a patient opening and later peak suit the house brief.',**changes}


def planned(minutes=120, *, start=True, ids=None, search=None, **changes):
    return {**decision(ids,search), 'sessionPlan':interpreted(minutes,**changes),
            'sessionScope':{'mode':'ongoing','message':'A continuing house set.'} if start else None,
            'playbackAction':{'requestId':'prompt','action':'start-now'} if start else None}


class PromptPlanTests(unittest.TestCase):
    def make(self, result, text='Begin a two-hour house journey now.', **flags):
        session=RunSession();clock=[1000.]
        evaluator=Mock(return_value=(result,{}))
        agent=DJAgent(session,evaluator=evaluator,clock=lambda:clock[0])
        agent.message(text,message_id='prompt',approved=True,approve_preparation=True,
                      approve_session_curation=True,**flags)
        settled(agent)
        return session,agent,evaluator,clock

    def test_two_hour_interpretation_applies_before_bounded_curation_and_starts_via_engine(self):
        session,agent,evaluator,clock=self.make(planned(ids=['t00','t01','t02']))
        packet=evaluator.call_args.args[0]
        self.assertEqual(packet['operatorRequest']['text'],'Begin a two-hour house journey now.')
        self.assertTrue(packet['sessionPlanPolicy']['canSetPlan'])
        self.assertEqual(agent.settings.state['plan'],{'startAt':1000.,'endAt':8200.,'peakAt':6400.,
            'warmLen':1200,'warm':True,'onePeak':True,'darkLate':True})
        state=agent.snapshot()
        self.assertEqual(state['curation']['expiresAt'],8200.)
        self.assertEqual(state['curation']['remaining'],60)
        self.assertEqual(state['curation']['costCeilingUSD'],2.90)
        self.assertEqual(state['settings']['interpretedPlan'],{'requestId':'prompt','durationMinutes':120,
            'peakAfterMinutes':90,'warmupMinutes':20,'explanation':interpreted()['explanation'],
            'appliedAt':1000.,'restored':False})
        self.assertIn('prompt-plan',state['settings']['capabilities'])
        self.assertEqual(len(agent.settings.state['receipts']),1)
        self.assertIsNone(agent.state['handoff'])
        self.assertEqual(agent.state['direction'],decision()['direction'])
        for _ in range(2):agent.tick(force=True);settled(agent)
        self.assertEqual(agent.playback_request['status'],'playing')
        self.assertEqual(session.calls.count(('start',)),1)
        # Native timed curation ends at the plan horizon, not the initial 30m allowance.
        clock[0]=8100.;agent.curation.reconcile(session.snapshot(),agent.playback_request,'all')
        self.assertTrue(agent.curation.available())
        clock[0]=8200.;agent.curation.reconcile(session.snapshot(),agent.playback_request,'all')
        self.assertFalse(agent.curation.available())
        self.assertTrue(session.state['active'])

    def test_ninety_minutes_is_not_rounded_to_a_preset(self):
        session,agent,_,_=self.make(planned(90,ids=['t00','t01','t02'],peakAfterMinutes=58,
            explanation='Ninety minutes requested; the brief calls for a peak after 58 minutes.'),
            text='Begin an hour and a half of house now, with a peak around minute 58.')
        plan=agent.settings.state['plan']
        self.assertEqual(plan['endAt']-plan['startAt'],5400)
        self.assertEqual(plan['peakAt']-plan['startAt'],3480)
        self.assertEqual(agent.snapshot()['curation']['expiresAt'],6400.)

    def test_plan_and_preparation_only_do_not_infer_transport_or_ongoing_grant(self):
        for ids,text in (([], 'Plan a two-hour set, without selecting or playing records yet.'),
                         (['t00','t01','t02'],'Prepare two hours of house but do not start yet.'),
                         (['t00','t01','t02'],'Play a two-hour house set — just prepare it for now.'),
                         (['t00','t01','t02'],'Play a two-hour house set, preparation only.')):
            with self.subTest(text=text):
                session,agent,_,_=self.make(planned(start=False,ids=ids),text=text)
                self.assertEqual(agent.settings.state['plan']['endAt'],8200.)
                self.assertIsNone(agent.state['handoff'])
                self.assertEqual(agent.state['direction'],decision()['direction'])
                for _ in range(2):agent.tick(force=True);settled(agent)
                self.assertNotIn(('start',),session.calls)
                self.assertFalse(session.state['active'])
                self.assertIsNone(agent.snapshot()['curation'])
                self.assertIsNone(agent.playback_request)

    def test_unrelated_negative_future_ambiguous_and_conditional_model_fixtures_hold(self):
        # These are semantic model-output fixtures, not claims of live model accuracy.
        for text in ('Do not start a two-hour set.', 'Could we play a two-hour set tomorrow?',
                     'If people arrive later, play for two hours.', 'Maybe one or two hours, I am not sure.',
                     'What is a two-hour DJ set?', 'Show me the rooftop camera.',
                     'Play a two-hour house set — actually, do not start.',
                     'Play a two-hour house set, but only tomorrow.',
                     'Play a two-hour house set, once our friends arrive.'):
            with self.subTest(text=text):
                result={**decision(), 'sessionPlan':None,'playbackAction':None,
                    'sessionScope':{'mode':'clarify','message':'Should this be a current set plan or just a future idea?'}}
                session,agent,_,_=self.make(result,text=text)
                self.assertIsNone(agent.settings.state['plan'])
                self.assertIsNone(agent.snapshot()['curation'])
                self.assertNotIn(('start',),session.calls)
                self.assertFalse(session.state['active'])

    def test_search_followup_keeps_original_absolute_plan_without_resetting_clock(self):
        query={'query':'House','artist':'','genre':'House','bpmMin':120,'bpmMax':130}
        session,agent,evaluator,clock=self.make(planned(search=query))
        original=copy.deepcopy(agent.settings.state['plan'])
        self.assertEqual(len(agent.settings.state['receipts']),1)
        clock[0]+=60
        evaluator.return_value=({**decision(['t00','t01','t02']),
            'sessionScope':{'mode':'ongoing','message':'Continue the requested set.'}}, {})
        session.update(phase='catalog-ready',catalogSearch={'status':'complete','added':3})
        agent.tick(force=True);settled(agent)
        packet=evaluator.call_args.args[0]
        self.assertEqual(packet['reason'],'choose from catalog results')
        self.assertEqual(packet['settings']['nightPlan'],original)
        self.assertEqual(agent.settings.state['plan'],original)
        self.assertEqual(agent.snapshot()['curation']['expiresAt'],8200.)
        self.assertEqual(len(agent.settings.state['receipts']),1)

    def test_replayed_direction_id_does_not_reapply_or_extend_a_plan(self):
        session,agent,evaluator,clock=self.make(planned(start=False))
        original=copy.deepcopy(agent.settings.state['plan']);clock[0]+=120
        agent.message('Begin a six-hour techno set.',message_id='prompt',approved=True,
                      approve_preparation=True,approve_session_curation=True)
        self.assertEqual(evaluator.call_count,1)
        self.assertEqual(agent.settings.state['plan'],original)
        self.assertEqual(len(agent.settings.state['receipts']),1)

    def test_null_plan_preserves_non_start_music_direction_and_existing_timing(self):
        session=RunSession();evaluator=Mock(return_value=(decision(['t00','t01','t02']),{}))
        agent=DJAgent(session,evaluator=evaluator,clock=lambda:1000.)
        original=prompt_plan.native(interpreted(90),900.)
        agent.settings.state['plan']=copy.deepcopy(original)
        agent.message('Use deeper house in the upcoming selection; do not start yet.',
            message_id='prompt',approved=True,approve_preparation=True,approve_session_curation=True)
        settled(agent)
        self.assertEqual(agent.settings.state['plan'],original)
        self.assertEqual(agent.state['direction'],decision()['direction'])
        self.assertEqual(agent.settings.state['receipts'],[])
        self.assertIsNone(agent.snapshot()['curation'])
        self.assertIsNone(agent.state['handoff'])
        self.assertNotIn(('start',),session.calls)

    def test_invalid_start_identity_cannot_commit_an_otherwise_valid_plan(self):
        result=planned(ids=['t00','t01','t02'])
        result['playbackAction']['requestId']='another-request'
        session,agent,_,_=self.make(result)
        self.assertIsNone(agent.settings.state['plan'])
        self.assertEqual(agent.settings.state['receipts'],[])
        self.assertIsNone(agent.snapshot()['curation'])
        self.assertNotIn(('start',),session.calls)

    def test_search_followup_cannot_reapply_a_model_plan(self):
        query={'query':'House','artist':'','genre':'House','bpmMin':120,'bpmMax':130}
        session,agent,evaluator,clock=self.make(planned(search=query))
        original=copy.deepcopy(agent.settings.state['plan']);clock[0]+=60
        evaluator.return_value=(planned(ids=['t00','t01','t02']),{})
        session.update(phase='catalog-ready',catalogSearch={'status':'complete','added':3})
        agent.tick(force=True);settled(agent)
        self.assertEqual(agent.settings.state['plan'],original)
        self.assertEqual(len(agent.settings.state['receipts']),1)
        self.assertNotIn(('start',),session.calls)

    def test_current_playback_cannot_be_retimed_by_new_prompt_plan(self):
        session=RunSession();session.update(active=True,phase='playing',index=0,
            telemetry={'decks':[{'id':'t00','playing':True}]})
        evaluator=Mock(return_value=(planned(start=False),{}))
        agent=DJAgent(session,evaluator=evaluator,clock=lambda:1000.)
        agent.message('Change this set to two hours.',message_id='prompt',approved=True,
                      approve_preparation=True,approve_session_curation=True);settled(agent)
        self.assertFalse(evaluator.call_args.args[0]['sessionPlanPolicy']['canSetPlan'])
        self.assertIsNone(agent.settings.state['plan'])
        self.assertTrue(session.state['active'])
        self.assertEqual(session.calls,[])

    def test_response_racing_manual_plan_playback_new_session_or_direction_has_no_plan_effect(self):
        for mutation in ('manual-plan','playback','session','new-direction'):
            with self.subTest(mutation=mutation):
                entered=threading.Event();release=threading.Event();session=RunSession()
                def evaluate(packet):
                    entered.set();release.wait(2)
                    return planned(ids=['t00','t01','t02']),{}
                agent=DJAgent(session,evaluator=evaluate,clock=lambda:1000.)
                agent.message('Begin a two-hour set now.',message_id='prompt',approved=True,
                              approve_preparation=True,approve_session_curation=True)
                self.assertTrue(entered.wait(1))
                replacement=None
                if mutation=='manual-plan':
                    replacement=prompt_plan.native(interpreted(90),1000.)
                    agent.settings_action('manual',{'kind':'plan','value':replacement},agent.snapshot()['settings']['expected'])
                elif mutation=='playback':
                    session.update(active=True,phase='playing',index=0,telemetry={'decks':[{'id':'t00','playing':True}]})
                elif mutation=='session':session.update(id='new-session')
                else:
                    with agent.lock:agent.state['revision']+=1;agent.generation+=1
                release.set();settled(agent)
                self.assertEqual(agent.settings.state['plan'],replacement)
                self.assertIsNone(agent.snapshot()['settings']['interpretedPlan'])
                self.assertIsNone(agent.snapshot()['curation'])
                self.assertNotIn(('start',),session.calls)

    def test_restored_plan_is_inert_history_and_manual_replacement_hides_its_summary(self):
        session,agent,_,_=self.make(planned(start=False))
        with tempfile.TemporaryDirectory() as folder:
            agent.path=Path(folder)/'agent.json';agent.save()
            evaluator=Mock();restored=DJAgent(session,path=agent.path,evaluator=evaluator,clock=lambda:1100.)
            self.assertTrue(restored.snapshot()['settings']['interpretedPlan']['restored'])
            self.assertIsNone(restored.playback_request);self.assertIsNone(restored.prompt_plan_request)
            self.assertIsNone(restored.snapshot()['curation']);evaluator.assert_not_called()
            # An unrelated preference preserves the still-valid interpretation.
            restored.settings_action('style',{'kind':'style','value':{'id':'energy','stop':2}},
                                     restored.snapshot()['settings']['expected'])
            self.assertIsNotNone(restored.snapshot()['settings']['interpretedPlan'])
            restored.settings_action('peak',{'kind':'peakShift','value':300},
                                     restored.snapshot()['settings']['expected'])
            self.assertIsNone(restored.snapshot()['settings']['interpretedPlan'])

    def test_readback_rejects_metadata_that_does_not_describe_the_applied_plan(self):
        _,agent,_,_=self.make(planned(start=False))
        agent.settings.state['receipts'][-1]['interpretation']['durationMinutes']=180
        self.assertIsNone(agent.snapshot()['settings']['interpretedPlan'])

    def test_invalid_model_timing_never_starts_or_applies_a_plan(self):
        changes=[{'durationMinutes':361},{'durationMinutes':29},{'durationMinutes':float('nan')},
                 {'durationMinutes':True},{'peakAfterMinutes':120},{'peakAfterMinutes':float('inf')},
                 {'warmupMinutes':85},{'warmupMinutes':.5},{'warmupMinutes':1.111},
                 {'warm':False},{'onePeak':1},{'explanation':''},{'extra':'unsupported'}]
        changes += [{key:value} for key in ('durationMinutes','peakAfterMinutes','warmupMinutes')
                    for value in (1e308,10**400)]
        for change in changes:
            with self.subTest(change=change):
                session,agent,_,_=self.make(planned(ids=['t00','t01','t02'],**change))
                self.assertIsNone(agent.settings.state['plan'])
                self.assertIsNone(agent.snapshot()['curation'])
                self.assertNotIn(('start',),session.calls)
                self.assertEqual(agent.settings.state['receipts'],[])
                self.assertEqual(agent.state['status'],'error')
                self.assertFalse(agent.snapshot()['evaluating'])

    def test_clarification_cannot_apply_timing(self):
        result=planned(start=False)
        result['sessionScope']={'mode':'clarify','message':'Which duration?'}
        with self.assertRaisesRegex(ValueError,'Clarify'):
            validate_decision(result,RunSession().snapshot())

    def test_plan_requires_original_approved_typed_turn(self):
        for source,approve in (('typed',False),('voice',True)):
            with self.subTest(source=source,approve=approve):
                session=RunSession();evaluator=Mock(return_value=(planned(start=False),{}))
                agent=DJAgent(session,evaluator=evaluator,clock=lambda:1000.)
                agent.enable(True,180,2)
                agent.message('Plan a two-hour house set.',message_id='prompt',source=source,
                              approved=True,approve_preparation=approve)
                settled(agent)
                self.assertFalse(evaluator.call_args.args[0]['sessionPlanPolicy']['canSetPlan'])
                self.assertIsNone(agent.settings.state['plan'])
                self.assertEqual(agent.settings.state['receipts'],[])

    def test_schema_is_null_without_current_plan_permission_and_request_stays_bounded(self):
        for reason,allowed in (('operator message',True),('operator message',False),
                               ('choose from catalog results',True),('queue needs tracks',True)):
            with self.subTest(reason=reason,allowed=allowed):
                packet=populated_packet();packet['reason']=reason
                packet['sessionPlanPolicy']={'canSetPlan':allowed,'minMinutes':30,'maxMinutes':360}
                packet['canRequestStart']=True
                encoded,sent=planning_request(packet);body=json.loads(encoded)
                self.assertLessEqual(len(encoded),MAX_REQUEST_BYTES)
                schema=body['text']['format']['schema']['properties']['sessionPlan']
                if reason=='operator message' and allowed:
                    self.assertIn('anyOf',schema)
                    self.assertIn('two hours=120',body['instructions'])
                else:self.assertEqual(schema,{'type':'null'})
                self.assertEqual(sent['operatorRequest'],packet['operatorRequest'])

    def test_timed_full_queue_with_long_names_fits_without_losing_ids_or_constraints(self):
        packet=populated_packet();music=packet['music']
        tracks=copy.deepcopy(music['catalog'][:12])
        for index,row in enumerate(tracks):
            row['title']=f'Recording {index} '+('Extended café mix — '*12)
            row['artist']='Fixture collaboration '+('Artist name, '*5)
            if index>=8:row['searchMatch']=True
        music.update(catalog=tracks,currentOrder=[row['id'] for row in tracks],
            protectedIds=[row['id'] for row in tracks[:10]],requestedIds=[row['id'] for row in tracks[-2:]],
            deferredTracks=[],queueFacts={kind:[{'id':row['id'],'title':row['title']} for row in tracks]
                for kind in ('current','staged')})
        packet['operatorRequest']['text']='Begin a two-hour set of house.'
        packet['sessionPlanPolicy']={'canSetPlan':True,'minMinutes':30,'maxMinutes':360}
        packet['canRequestStart']=True
        packet['settings'].update(style={'known':True,'vocal':'instrumental'},discover=True)
        packet['searchPolicy'].update(previousQueries=['daft punk'],lastResult={'query':'Daft Punk',
            'filters':{'artist':'','genre':'','bpmMin':40,'bpmMax':300},
            'status':'complete','examined':7,'matched':4,'added':4})
        encoded,sent=planning_request(packet)
        self.assertLessEqual(len(encoded),MAX_REQUEST_BYTES)
        self.assertEqual(sent['operatorRequest'],packet['operatorRequest'])
        self.assertEqual(sent['settings'],packet['settings'])
        self.assertEqual(sent['sessionPlanPolicy'],packet['sessionPlanPolicy'])
        for key in ('currentOrder','protectedIds','requestedIds'):
            self.assertEqual(sent['music'][key],music[key])
        restored=copy.deepcopy(sent['music']['catalog'])
        for row in restored:
            if 'artistRef' in row:row['artist']=sent['music']['catalogArtists'][row.pop('artistRef')]
        self.assertEqual(restored,[{key:value for key,value in row.items() if value is not None} for row in tracks])
        self.assertIn('two hours=120',json.loads(encoded)['instructions'])


if __name__=='__main__':unittest.main()
