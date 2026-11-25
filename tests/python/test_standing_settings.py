"""Standing preferences reach bounded planners without touching accepted audio."""
import copy
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from afterhours import ai_mix
from afterhours import cue_planner
from afterhours import dj_settings
from afterhours.dj_agent import DJAgent, context_key, planning_request, MAX_REQUEST_BYTES
from afterhours.record_crate import RecordCrate
from test_agent_scenarios import ScenarioSession
from test_cue_planner import fixture
from test_dj_agent import decision, settled
import test_vdj_discovery
from afterhours.vdj_session import DJSession


class StandingSettingsTests(unittest.TestCase):
    def make(self, path=None, evaluator=None):
        session = ScenarioSession()
        session.plan(['a', 'b', 'c'])
        provider = evaluator or Mock(return_value=(decision(), {'responseId':'fixture'}))
        return session, DJAgent(session, path, provider), provider

    def change(self, agent, kind, value, request_id='edit', expected=None):
        return agent.settings_action(request_id, {'kind':kind,'value':value},
                                     expected or agent.snapshot()['settings']['expected'])

    def test_additive_contract_contains_real_library_and_only_supported_standing_rule(self):
        session, agent, provider = self.make()
        result = agent.snapshot()['settings']
        self.assertEqual(result['style'], dj_settings.DEFAULT_STYLE)
        self.assertTrue(result['discover'])
        self.assertTrue({'style','discover','librarySummary'} <= set(result['capabilities']))
        self.assertEqual([r['id'] for r in result['houseRules']], ['reorder'])
        self.assertEqual(result['library']['count'], 4)
        self.assertEqual(result['library']['durationKnownCount'], 0)
        self.assertEqual([r['id'] for r in result['library']['sampleArt']], ['a','b','c','d'])
        self.assertFalse(any(r['group']=='learned' for r in result['houseRules']))
        provider.assert_not_called()

    def test_settings_mutations_are_revision_bound_idempotent_and_do_not_touch_order(self):
        session, agent, provider = self.make()
        before = session.snapshot()
        expected = agent.snapshot()['settings']['expected']
        first = self.change(agent,'style',{'id':'energy','stop':4},expected=expected)
        again = self.change(agent,'style',{'id':'energy','stop':4},expected=expected)
        self.assertEqual(first['settings'],again['settings'])
        self.assertEqual(first['settings']['receipt']['status'],'confirmed')
        self.assertEqual(first['settings']['style']['energy'],4)
        with self.assertRaisesRegex(ValueError,'Settings changed'):
            self.change(agent,'discover',False,'stale',expected)
        self.assertTrue(agent.settings.state['discover'])
        self.assertEqual(session.snapshot(),before)
        self.assertEqual(agent.remaining,0)
        provider.assert_not_called()

    def test_invalid_values_reject_before_mutating_preferences(self):
        _, agent, provider = self.make()
        before = copy.deepcopy(agent.settings.state)
        for value in ({'id':'unknown','stop':2}, {'id':'energy','stop':True},
                      {'id':'energy','stop':-1}, {'id':'energy','stop':5},
                      {'id':['energy'],'stop':2}, {'id':'energy','stop':2.0},
                      {'id':'energy','stop':2,'extra':True}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.change(agent,'style',value)
        for value in ('off',0,1,None,[]):
            with self.assertRaises(ValueError):self.change(agent,'discover',value)
        self.assertEqual(agent.settings.state,before)
        provider.assert_not_called()

    def test_old_duplicate_returns_its_receipt_without_reverting_newer_preferences(self):
        session,agent,provider=self.make()
        expected=agent.snapshot()['settings']['expected']
        first=self.change(agent,'style',{'id':'energy','stop':4},'A',expected)
        self.change(agent,'discover',False,'B')
        latest=self.change(agent,'style',{'id':'energy','stop':0},'C')
        before=copy.deepcopy(agent.settings.state)
        session_before=session.snapshot()
        replay=self.change(agent,'style',{'id':'energy','stop':4},'A',expected)
        self.assertEqual(replay['settings']['receipt'],first['settings']['receipt'])
        self.assertEqual(replay['settings']['style']['energy'],0)
        self.assertFalse(replay['settings']['discover'])
        self.assertEqual(replay['settings']['expected'],latest['settings']['expected'])
        self.assertEqual(agent.snapshot()['settings']['receipt']['id'],'C')
        self.assertEqual(agent.settings.state,before)
        self.assertEqual(session.snapshot(),session_before)
        self.assertEqual(agent.remaining,0)
        provider.assert_not_called()

    def test_preferences_survive_restart_and_new_set_without_replaying_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'agent.json'
            _, agent, provider=self.make(path)
            for name,stop in {'energy':4,'adv':0,'vocals':1,'blend':0}.items():
                self.change(agent,'style',{'id':name,'stop':stop},name)
            self.change(agent,'discover',False,'discover')
            session, restored, _=self.make(path)
            with patch.object(session,'send') as send,patch.object(session,'start') as start:
                restored.restore_settings()
                self.assertEqual(restored.snapshot()['settings']['style'],agent.settings.state['style'])
                self.assertFalse(restored.snapshot()['settings']['discover'])
                self.assertEqual(session.standing_style,agent.settings.state['style'])
                session.new_set()
                self.assertEqual(session.standing_style,agent.settings.state['style'])
                self.assertFalse(restored.snapshot()['settings']['discover'])
                send.assert_not_called();start.assert_not_called()
            provider.assert_not_called()

    def test_whole_command_parser_does_not_treat_titles_questions_or_negations_as_preferences(self):
        for text in ('What do longer blends do?', 'Do not set energy to 4', 'More Vocals by Fixture',
                     'Play Discovery', 'Can you explain discovery?', 'set energy to 4 and play music',
                     'fewer vocals?', 'longer blends in the track called Energy', 'Never turn discovery off'):
            with self.subTest(text=text):
                self.assertIsNone(dj_settings.parse(text,surface='settings'))
        for text in ('longer blends','more vocals','more new music','more patient builds'):
            self.assertIsNone(dj_settings.parse(text))
            self.assertIsNotNone(dj_settings.parse(text,surface='settings'))
            self.assertIsNotNone(dj_settings.parse(text+' from now on'))
        self.assertEqual(dj_settings.parse('Please set energy to driving.'),
                         {'kind':'style','value':{'id':'energy','stop':4}})
        self.assertEqual(dj_settings.parse('turn discovery off'),{'kind':'discover','value':False})
        self.assertEqual(dj_settings.parse('longer blends',surface='settings',style={**dj_settings.DEFAULT_STYLE,'blend':4})['value']['stop'],4)

    def test_settings_text_and_voice_share_direct_receipts_without_provider_work(self):
        for source in ('typed','voice'):
            session, agent, provider=self.make()
            order=session.snapshot()['order']
            agent.message('more new music',source=source,message_id='nudge',surface='settings',approved=True)
            result=agent.snapshot()
            self.assertEqual(result['settings']['style']['adv'],2)
            self.assertEqual(result['settings']['receipt']['id'],'nudge')
            self.assertEqual(session.snapshot()['order'],order)
            self.assertEqual(result['allowance']['remaining'],0)
            provider.assert_not_called()

    def test_unsupported_settings_text_is_an_honest_local_fallback(self):
        session, agent, provider=self.make()
        before=agent.snapshot()['settings'];order=session.snapshot()['order']
        agent.message('What does more energy mean?',message_id='question',surface='settings',approved=True)
        result=agent.snapshot()
        self.assertEqual(result['settings'],before)
        self.assertIn('No setting changed',result['messages'][-1]['text'])
        self.assertEqual(session.snapshot()['order'],order)
        self.assertFalse(result['evaluating']);self.assertEqual(agent.remaining,0)
        provider.assert_not_called()

    def test_discovery_off_reaches_provider_schema_and_bounded_context(self):
        session, agent, provider=self.make()
        self.change(agent,'discover',False)
        self.change(agent,'style',{'id':'adv','stop':4},'adventure')
        agent.message('Build something around Daft Punk',message_id='set',approved=True)
        settled(agent)
        packet=provider.call_args.args[0]
        self.assertFalse(packet['searchPolicy']['canSearch'])
        self.assertEqual(packet['searchPolicy']['roundsRemaining'],0)
        self.assertEqual(packet['settings']['style']['stops']['adv'],4)
        self.assertFalse(packet['settings']['discover'])
        encoded,bounded=planning_request(packet)
        self.assertLessEqual(len(encoded),MAX_REQUEST_BYTES)
        self.assertEqual(json.loads(encoded)['text']['format']['schema']['properties']['search'],{'type':'null'})
        self.assertEqual(session.calls,[])

    def test_discovery_off_is_checked_again_when_applying_a_retained_proposal(self):
        session, agent, provider=self.make()
        self.change(agent,'discover',False)
        query={'query':'Daft Punk','artist':'','genre':'','bpmMin':40,'bpmMax':300}
        agent.state['proposal']={**decision(search=query),'revision':agent.state['revision'],
                                 'contextKey':context_key(session.snapshot())}
        with self.assertRaisesRegex(ValueError,'discovery is off'):
            agent.apply(agent.state['revision'])
        self.assertEqual(session.calls,[])
        provider.assert_not_called()

    def test_a_late_search_cannot_escape_a_new_discovery_preference(self):
        entered,gate=threading.Event(),threading.Event()
        query={'query':'Daft Punk','artist':'','genre':'','bpmMin':40,'bpmMax':300}
        def evaluate(_):
            entered.set();self.assertTrue(gate.wait(2))
            return decision(search=query),{'responseId':'late'}
        session,agent,_=self.make(evaluator=evaluate)
        agent.message('Build a Daft Punk set',message_id='set',approved=True)
        self.assertTrue(entered.wait(2))
        try:self.change(agent,'discover',False)
        finally:gate.set()
        result=settled(agent)
        self.assertIsNone(result['proposal']);self.assertEqual(session.calls,[])

    def test_discovery_off_allows_existing_crate_selection_and_explicit_import(self):
        provider=Mock(return_value=(decision(['a','d','b']),{'responseId':'known'}))
        session,agent,_=self.make(evaluator=provider)
        self.change(agent,'discover',False)
        agent.message('Put House D second',message_id='known',approved=True)
        settled(agent)
        self.assertEqual([t['id'] for t in session.snapshot()['order']],['a','d','b'])
        with patch.object(session,'discover_more') as search:
            agent.message('Can you add some Daft Punk to my catalog?',message_id='explicit')
        search.assert_called_once_with('Daft Punk',recheck_missing=True)
        self.assertFalse(agent.settings.state['discover'])

    def test_style_edits_keep_playing_cued_armed_and_accepted_queue_unchanged(self):
        session, original=test_vdj_discovery.QueueRevisionTests().prepared()
        session.standing_style={**dj_settings.DEFAULT_STYLE,'blend':0}
        session.revise_queue(['0','1','3','2'],reason='Accepted future queue')
        before=session.snapshot()
        agent=DJAgent(session,evaluator=Mock())
        self.change(agent,'style',{'id':'blend','stop':4})
        after=session.snapshot()
        for key in ('order','executionPlan','queuedUpdate','index','active','phase','timing'):
            self.assertEqual(after.get(key),before.get(key),key)
        self.assertEqual(after['queuedUpdate']['standingStyle']['blend'],0)
        self.assertEqual(session.standing_style['blend'],4)
        self.assertEqual(after['executionPlan'],original)

    def test_style_edit_preserves_accepted_preparation_and_playback_identity(self):
        provider=Mock(return_value=(decision(['a','b','c']),{'responseId':'selection'}))
        session,agent,_=self.make(evaluator=provider)
        agent.message('Build a house set',message_id='prepare',approved=True,approve_preparation=True)
        settled(agent)
        pipeline=copy.deepcopy(agent.pipeline)
        agent.playback_request={'id':'start','status':'preparing','revision':agent.state['revision'],
                                'sessionId':session.snapshot()['id'],'orderIds':['a','b','c']}
        self.change(agent,'style',{'id':'energy','stop':4})
        self.assertEqual(agent.pipeline['ids'],pipeline['ids'])
        self.assertEqual(agent.pipeline['reviewRemaining'],pipeline['reviewRemaining'])
        self.assertEqual(agent.pipeline['revision'],agent.state['revision'])
        self.assertEqual(agent.playback_request['status'],'preparing')
        self.assertEqual(agent.playback_request['orderIds'],['a','b','c'])


class StylePlanningTests(unittest.TestCase):
    def test_style_is_actual_cue_planner_input_with_original_budget_and_validation(self):
        _,report,response=fixture()
        provider=Mock(return_value=io.BytesIO(json.dumps({'status':'completed','output':[{'type':'message',
            'content':[{'type':'output_text','text':json.dumps(response)}]}]}).encode()))
        style={**dj_settings.DEFAULT_STYLE,'blend':0,'energy':4}
        with patch('afterhours.ai_mix.api_key',return_value='synthetic-test-key'):
            result=ai_mix.plan(report,30,approved=True,opener=provider,standing_style=style)
        body=json.loads(provider.call_args.args[0].data)
        self.assertEqual(json.loads(body['input'])['standingStyle']['preferredBlendBeats'],8)
        self.assertLess(len(provider.call_args.args[0].data),ai_mix.MAX_BODY)
        self.assertFalse(result['appliedToPlayback']);provider.assert_called_once()

    def test_accepted_cue_review_captures_preferences_before_background_worker_runs(self):
        _,report,response=fixture()
        session=DJSession(threading.Lock())
        session.update(candidates=report['tracks'],order=report['tracks'],analysis=report)
        before={**dj_settings.DEFAULT_STYLE,'blend':0}
        session.standing_style=copy.deepcopy(before)
        peer=Mock();peer.status.return_value={'automix':False,'decks':[{'playing':False},{'playing':False}]}
        with patch.object(session,'_launch') as launch,patch('afterhours.cue_planner.verify_receipts'),patch('afterhours.ai_mix.plan',return_value={'decision':'hold'}) as planner:
            session.review_mix(approved=True)
            session.standing_style={**before,'blend':4}
            launch.call_args.args[0](peer)
        self.assertEqual(planner.call_args.kwargs['standing_style'],before)

    def test_background_blend_preference_only_tightens_new_handoffs_and_never_protected_pair(self):
        session,original=test_vdj_discovery.QueueRevisionTests().prepared()
        snapshot=session.snapshot()
        for stop,wanted in ((0,8),(1,16),(4,16)):
            with self.subTest(stop=stop):
                prepared=cue_planner.complete_background_queue(snapshot,snapshot['order'],[],
                    'Future cue fit',2,standing_style={**dj_settings.DEFAULT_STYLE,'blend':stop})
                transitions=prepared['executionPlan']['transitions']
                self.assertEqual(transitions[0],original['transitions'][0])
                self.assertEqual(transitions[1]['fadeBeats'],wanted)
                self.assertEqual(transitions[2]['fadeBeats'],wanted)
                self.assertTrue(all(t['leadPolicy']=='single-lead-v1' and not t['tonalOverlap'] for t in transitions))


class LibrarySummaryTests(unittest.TestCase):
    def test_cached_actual_totals_samples_metadata_updates_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'crate.sqlite3'
            crate=RecordCrate(path)
            for i in range(140):
                crate.upsert({'id':str(i),'title':f'Record {i}','artist':'Real metadata',
                              'duration':120 if i%2==0 else None,'artworkUrl':f'https://example.test/{i}.jpg'})
            before=crate.library_summary()
            self.assertEqual(before['count'],140);self.assertEqual(before['durationKnownCount'],70)
            self.assertEqual(before['knownDurationSeconds'],8400)
            self.assertEqual([r['id'] for r in before['sampleArt']],['0','1','2','3','4'])
            crate.upsert({'id':'1','duration':180})
            crate.upsert({'id':'0','duration':None,'artworkUrl':'https://example.test/new.jpg'})
            after=crate.library_summary()
            self.assertEqual(after['count'],140);self.assertEqual(after['durationKnownCount'],71)
            self.assertEqual(after['knownDurationSeconds'],8580)
            self.assertEqual(after['sampleArt'][0]['artworkUrl'],'https://example.test/new.jpg')
            observed=[];crate.db.set_trace_callback(observed.append)
            for _ in range(100):crate.summary();crate.library_summary()
            self.assertEqual(observed,[])
            crate.close();restored=RecordCrate(path)
            self.assertEqual(restored.library_summary(),after)
            restored.close()

    def test_unknown_or_invalid_durations_are_not_fabricated_hours(self):
        crate=RecordCrate()
        for i,value in enumerate((None,False,True,'120',-30,0,9999999)):
            crate.upsert({'id':str(i),'duration':value})
        summary=crate.library_summary()
        self.assertEqual(summary['durationKnownCount'],0)
        self.assertEqual(summary['knownDurationSeconds'],0)
        self.assertEqual(summary['count'],7)
        self.assertEqual(len(summary['sampleArt']),5)
        self.assertTrue(all('artworkUrl' not in r for r in summary['sampleArt']))


if __name__=='__main__':unittest.main()
