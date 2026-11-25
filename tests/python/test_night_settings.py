"""Clocked operator plans and real queue policy; no providers or audio."""
import copy
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock
from zoneinfo import ZoneInfo

from afterhours import dj_settings as settings
from afterhours.dj_agent import DJAgent
from test_agent_scenarios import ScenarioSession
from test_dj_agent import decision,settled


class NightSettingsTests(unittest.TestCase):
    zone=ZoneInfo('America/Los_Angeles')
    def timestamp(self,day,hour,minute=0):
        return datetime(2026,11,day,hour,minute,tzinfo=self.zone).timestamp()
    def plan(self):
        return settings.parse('Plan tonight from 10 PM to 2 AM, with a peak at midnight',self.timestamp(7,18),self.zone)['value']
    def make(self,path=None):
        session=ScenarioSession();session.plan(['a','b','c'])
        provider=Mock(return_value=(decision(['b','a','c']),{'responseId':'fixture'}))
        agent=DJAgent(session,path,provider,clock=lambda:self.timestamp(7,23))
        return session,agent,provider
    def change(self,agent,kind,value,identity='preference'):
        return agent.settings_action(identity,{'kind':kind,'value':value},agent.snapshot()['settings']['expected'])

    def test_night_times_are_operator_sourced_and_midnight_crosses_the_date(self):
        plan=self.plan()
        self.assertEqual(plan['startAt'],self.timestamp(7,22))
        self.assertEqual(plan['endAt'],self.timestamp(8,2))
        self.assertEqual(plan['peakAt'],self.timestamp(8,0))
        running=settings.parse('Plan tonight from 10 PM to 2 AM, peak at midnight',self.timestamp(8,1),self.zone)['value']
        self.assertEqual(running,plan)
        tomorrow=settings.parse('Plan tonight from 10 PM to 2 AM, peak at midnight',self.timestamp(8,4),self.zone)['value']
        self.assertEqual(tomorrow['startAt'],self.timestamp(8,22))

    def test_time_parser_does_not_execute_questions_negations_or_partial_speech(self):
        for text in ('Could you explain the night plan?', 'Do not push the peak later',
                     'push the peak later?', 'plan tonight from 10 PM', 'Darker by CamelPhat', 'Peak at midnight'):
            self.assertIsNone(settings.parse(text,self.timestamp(7,18),self.zone))
        for text in ('Plan tonight from 22 PM to 2 AM peak at midnight','Plan tonight from 10 PM to 11 PM peak at 2 AM'):
            with self.assertRaises(ValueError):settings.parse(text,self.timestamp(7,18),self.zone)

    def test_invalid_or_outside_peak_never_changes_state(self):
        _,agent,provider=self.make()
        before=copy.deepcopy(agent.settings.state)
        for plan in ({**self.plan(),'peakAt':self.timestamp(8,3)}, {**self.plan(),'warm':'yes'},
                     {**self.plan(),'endAt':float('nan')}, {**self.plan(),'extra':True}):
            with self.assertRaises(ValueError):self.change(agent,'plan',plan)
            self.assertEqual(agent.settings.state,before)
        with self.assertRaisesRegex(ValueError,'No timed night'):
            self.change(agent,'peakShift',1800)
        provider.assert_not_called()

    def test_plan_shift_and_rules_keep_queue_and_do_not_claim_a_queue_effect(self):
        s,a,p=self.make();order=copy.deepcopy(s.snapshot()['order'])
        self.change(a,'plan',self.plan())
        reply=self.change(a,'peakShift',2400,'later')
        self.assertEqual(reply['settings']['plan']['peakAt'],self.timestamp(8,0,40))
        self.assertIn('prepared tracks stay in place',reply['settings']['receipt']['message'])
        before=settings.energy(reply['settings']['plan'],self.timestamp(7,23))
        reply=self.change(a,'rule',{'id':'warm','on':False},'lift')
        self.assertGreater(settings.energy(reply['settings']['plan'],self.timestamp(7,23)),before)
        self.assertEqual([r['id'] for r in reply['settings']['rules']],['warm','peak','dark','reorder'])
        self.assertFalse(any(r['group']=='learned' for r in reply['settings']['rules']))
        self.assertEqual(s.snapshot()['order'],order);self.assertEqual(s.calls,[])
        p.assert_not_called()

    def test_plain_text_and_voice_use_the_same_clocked_preferences_without_a_provider(self):
        for source in ('typed','voice'):
            s,a,p=self.make()
            a.message('Plan tonight from 10 PM to 2 AM, peak at midnight',source=source,message_id='plan',approved=True)
            first=a.snapshot();self.assertIsNotNone(first['settings']['plan'])
            a.message('push the peak a bit later',source=source,message_id='later',approved=True)
            result=a.snapshot()
            self.assertEqual(result['settings']['plan']['peakAt'],first['settings']['plan']['peakAt']+1800)
            self.assertEqual(result['allowance']['remaining'],0)
            self.assertEqual(result['messages'][-1]['source'],'engine')
            p.assert_not_called()

    def test_reorder_rule_reaches_actual_validation_and_keeps_accepted_plan(self):
        s,a,p=self.make();s.update(executionPlan={'id':'applied','applied':True})
        plan=copy.deepcopy(s.snapshot()['executionPlan'])
        result=self.change(a,'rule',{'id':'reorder','on':False})
        self.assertFalse(s.snapshot()['setlist']['allowReorder'])
        self.assertFalse(result['settings']['rules'][0]['on'])
        self.assertEqual(s.snapshot()['executionPlan'],plan)
        with self.assertRaisesRegex(ValueError,'reordering is off'):
            s.revise_queue(['b','a','c'],reason='New choice')
        self.change(a,'rule',{'id':'reorder','on':True},'unlock')
        s.revise_queue(['b','a','c'],reason='New choice')
        self.assertEqual([t['id'] for t in s.snapshot()['order']],['b','a','c'])
        p.assert_not_called()

    def test_rule_can_change_while_playing_without_mutating_accepted_music(self):
        s,a,p=self.make();s.update(active=True,phase='playing',index=0,telemetry={'decks':[{'playing':True}]})
        before=copy.deepcopy(s.snapshot()['order'])
        self.change(a,'rule',{'id':'reorder','on':False})
        self.assertEqual(s.snapshot()['order'],before);self.assertTrue(s.snapshot()['active'])
        p.assert_not_called()

    def test_planner_receives_the_clocked_intention_and_queue_policy(self):
        s,a,p=self.make()
        self.change(a,'plan',self.plan())
        a.message('Build toward the planned peak',message_id='choose',approved=True)
        settled(a)
        packet=p.call_args.args[0]['settings']
        self.assertEqual(packet['nightPlan'],self.plan())
        self.assertEqual(packet['observedAt'],self.timestamp(7,23))
        self.assertGreater(packet['energyTarget'],1.6)
        self.assertLess(packet['energyTarget'],5)
        self.assertIn('not a measured',packet['meaning'])
        self.assertIsNone(a.settings.planner_context(s.snapshot(),self.timestamp(8,5))['energyTarget'])

    def test_restart_preserves_plan_and_reorder_without_replaying_transport(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'conversation.json';s,a,p=self.make(path)
            self.change(a,'plan',self.plan());self.change(a,'rule',{'id':'reorder','on':False},'locked')
            next_session=ScenarioSession();restored=DJAgent(next_session,path,p)
            restored.restore_settings()
            self.assertEqual(restored.snapshot()['settings']['plan'],self.plan())
            self.assertFalse(next_session.snapshot()['setlist']['allowReorder'])
            self.assertEqual(next_session.calls,[])
            self.assertEqual(restored.snapshot()['allowance']['remaining'],0)
            p.assert_not_called()

    def test_missing_or_corrupt_plan_never_creates_demo_rules(self):
        source=settings.Settings({'version':1,'plan':{'warm':True}})
        self.assertIsNone(source.state['plan'])
        self.assertEqual([r['id'] for r in source.rules({})],['reorder'])


if __name__=='__main__':unittest.main()
