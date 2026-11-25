"""Settings use real coordinator/session validation, with no paid or audio I/O."""
import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from afterhours import catalog_browser
from afterhours import copilot_commands as cc
from afterhours import dj_settings
from afterhours.dj_agent import DJAgent, context_key
from test_agent_scenarios import ScenarioSession
from test_copilot_commands import running
from test_dj_agent import decision, settled
from test_playback_requests import PlaybackSession


class SettingsTests(unittest.TestCase):
    def make(self, session=None, evaluator=None, path=None):
        session = session or ScenarioSession()
        if not session.snapshot()['order']:
            session.plan(['a', 'b', 'c'])
        evaluator = evaluator or Mock(return_value=(decision(), {'responseId': 'fixture'}))
        return session, DJAgent(session, path, evaluator), evaluator

    def change(self, agent, kind, value, request_id='edit', expected=None):
        return agent.settings_action(request_id, {'kind': kind, 'value': value},
            expected if expected is not None else agent.snapshot()['settings']['expected'])

    def test_mode_and_brief_are_revision_bound_idempotent_and_provider_free(self):
        s, a, provider = self.make()
        before = copy.deepcopy(s.snapshot()['order'])
        expected = a.snapshot()['settings']['expected']
        first = self.change(a, 'lead', 'mix', expected=expected)
        epoch = dj_settings.selection_epoch(s.snapshot())
        again = self.change(a, 'lead', 'mix', expected=expected)
        self.assertEqual(first['settings'], again['settings'])
        self.assertEqual(epoch, dj_settings.selection_epoch(s.snapshot()))
        with self.assertRaisesRegex(ValueError, 'different settings'):
            self.change(a, 'lead', 'off', expected=expected)
        with self.assertRaisesRegex(ValueError, 'Settings changed'):
            self.change(a, 'brief', 'House at 128 BPM', 'stale', expected)
        reply = self.change(a, 'brief', '  House at 128 BPM  ', 'brief')
        self.assertEqual(reply['settings']['brief'], 'House at 128 BPM')
        self.assertEqual(reply['settings']['lead'], 'mix')
        self.assertEqual(s.snapshot()['agentDirection'], 'House at 128 BPM')
        self.assertEqual(s.snapshot()['order'], before)
        self.assertEqual(reply['allowance']['remaining'], 0)
        self.assertEqual(s.calls, [])
        provider.assert_not_called()

    def test_invalid_changes_cannot_mutate_the_engine(self):
        s, a, provider = self.make()
        before = s.snapshot()
        for change in ({'kind':'lead','value':'maybe'}, {'kind':'lead','value':False},
                       {'kind':'brief','value':''}, {'kind':'brief','value':'a'*1601},
                       {'kind':'lead','value':'mix','extra':True}, {'kind':'unknown','value':True}):
            with self.assertRaises(ValueError):
                a.settings_action('invalid', change, a.snapshot()['settings']['expected'])
        self.assertEqual(s.snapshot(), before)
        provider.assert_not_called()

    def test_mode_parser_recognizes_only_complete_imperatives(self):
        for text in ('Mixing only.', 'Please switch to mixing only', 'I pick the tracks, you mix'):
            self.assertEqual(dj_settings.parse(text), {'kind':'lead','value':'mix'})
        for text in ('What is mixing only?', 'Do not switch to mixing only', 'Mixing Only by Test Artist',
                     'mixing only tomorrow', 'tracks and mixing?', None):
            self.assertIsNone(dj_settings.parse(text), text)

    def test_typed_voice_and_header_share_mode_without_an_allowance(self):
        for source in ('typed', 'voice'):
            s, a, provider = self.make()
            a.message('Mixing only', source=source, message_id='spoken', approved=True)
            reply = a.snapshot()
            self.assertEqual(reply['settings']['lead'], 'mix')
            self.assertEqual(reply['copilotControl']['status'], 'confirmed')
            self.assertEqual(s.snapshot()['copilot']['leadMode'], 'mix')
            a.copilot_action('on', 'header', cc.context(s.snapshot()))
            self.assertEqual(a.snapshot()['settings']['lead'], 'all')
            self.assertEqual(a.snapshot()['allowance']['remaining'], 0)
            provider.assert_not_called()

    def test_late_choice_is_discarded_even_after_all_mix_all(self):
        for direct_session in (False, True):
            gate = threading.Event()
            entered = threading.Event()
            def evaluate(_):
                entered.set()
                self.assertTrue(gate.wait(2))
                return decision(['b','a','c']), {'responseId':'late'}
            s, a, _ = self.make(evaluator=evaluate)
            before = copy.deepcopy(s.snapshot()['order'])
            a.message('Put B first', message_id='choice', approved=True)
            self.assertTrue(entered.wait(2))
            try:
                target = s.copilot_command if direct_session else a.copilot_action
                target('mix', 'mix', cc.context(s.snapshot()))
                target('on', 'all', cc.context(s.snapshot()))
            finally:
                gate.set()
            state = settled(a)
            self.assertEqual(s.snapshot()['order'], before)
            self.assertIsNone(state['proposal'])
            self.assertNotEqual(state['status'], 'pending')
            self.assertEqual(s.calls, [])
            self.assertEqual(state['allowance']['remaining'], 0)

    def test_mixing_only_never_refills_or_rechecks_but_can_answer(self):
        s, a, provider = self.make()
        self.change(a, 'lead', 'mix')
        a.state['direction'] = 'House at 125 BPM'
        a.enable(True)
        s.update(active=True, phase='playing', index=0)
        for index in (0, 1, 2):
            s.update(index=index)
            a.tick(force=True)
        provider.assert_not_called()
        self.assertEqual(a.remaining, 6)
        s.update(active=False, phase='ready')
        a.message('What is the tempo of House B?', message_id='question')
        state = settled(a)
        self.assertIn('125.5 BPM', state['messages'][-1]['text'])
        self.assertEqual(provider.call_args.args[0]['leadMode'], 'mix')
        self.assertFalse(provider.call_args.args[0]['searchPolicy']['canSearch'])
        self.assertEqual(s.calls, [])

    def test_late_reply_cannot_replace_a_new_saved_brief(self):
        gate=threading.Event();entered=threading.Event()
        def evaluate(_):
            entered.set();self.assertTrue(gate.wait(2))
            return decision(['b','a','c']),{'responseId':'late-brief'}
        s,a,_=self.make(evaluator=evaluate)
        a.message('House please',message_id='old',approved=True)
        self.assertTrue(entered.wait(2))
        try:self.change(a,'brief','Darker techno at 132 BPM')
        finally:gate.set()
        result=settled(a)
        self.assertEqual(result['settings']['brief'],'Darker techno at 132 BPM')
        self.assertIsNone(result['proposal']);self.assertEqual(s.calls,[])

    def test_mixing_only_rejects_provider_search_and_selection(self):
        for result in (decision(['b','a','c']), decision(search={'query':'house','artist':'','genre':'House','bpmMin':120,'bpmMax':130})):
            provider = Mock(return_value=(result, {'responseId':'unauthorized-choice'}))
            s, a, _ = self.make(evaluator=provider)
            before = copy.deepcopy(s.snapshot()['order'])
            self.change(a, 'lead', 'mix')
            a.message('Change the records', message_id='direction', approved=True)
            state = settled(a)
            self.assertEqual(state['proposal']['trackIds'], [])
            self.assertIsNone(state['proposal']['search'])
            self.assertIn('Mixing only is on', state['messages'][-1]['text'])
            self.assertEqual(s.snapshot()['order'], before)
            self.assertEqual(s.calls, [])

    def test_applying_a_saved_proposal_checks_current_mode(self):
        s, a, _ = self.make()
        a.state['proposal'] = {**decision(['b','a','c']), 'revision':0, 'contextKey':context_key(s.snapshot())}
        s.copilot_command('mix', 'external', cc.context(s.snapshot()))
        with self.assertRaisesRegex(ValueError, 'Track selection is disabled'):
            a.apply(0)
        self.assertEqual(s.calls, [])

    def test_switching_to_mix_keeps_accepted_preparation_and_reserved_review(self):
        s, a, provider = self.make(evaluator=Mock(return_value=(decision(['a','b','c']), {'responseId':'selection'})))
        a.message('Build a house set', message_id='prepare', approved=True, approve_preparation=True)
        settled(a)
        self.assertIsNotNone(a.pipeline)
        self.change(a, 'lead', 'mix')
        for _ in range(8):
            a.tick(force=True)
        self.assertEqual(s.calls, [('analyze',),('review',),('apply',)])
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(a.snapshot()['settings']['lead'], 'mix')
        self.assertIsNone(a.pipeline)

    def test_brief_edit_keeps_accepted_preparation_and_start_identity(self):
        s, a, _ = self.make(evaluator=Mock(return_value=(decision(['a','b','c']), {'responseId':'selection'})))
        a.message('Build a house set', message_id='prepare', approved=True, approve_preparation=True)
        settled(a)
        old_revision = a.state['revision']
        a.playback_request = {'id':'start','status':'preparing','revision':old_revision,
                              'sessionId':s.snapshot()['id'],'orderIds':['a','b','c']}
        self.change(a, 'brief', 'Darker house later')
        self.assertEqual(a.pipeline['revision'], a.state['revision'])
        self.assertEqual(a.playback_request['revision'], a.state['revision'])
        self.assertEqual(a.playback_request['status'], 'preparing')
        self.assertEqual(a.playback_request['orderIds'], ['a','b','c'])
        self.assertEqual(a.pipeline['reviewRemaining'], 1)

    def test_mix_mode_can_start_an_explicit_prepared_queue_without_model_work(self):
        s = PlaybackSession()
        s.plan(['a','b','c'])
        s.update(executionPlan={'id':'prepared','applied':True})
        s, a, provider = self.make(s)
        self.change(a, 'lead', 'mix')
        a.message('start playing', message_id='start')
        for _ in range(3):
            a.tick(force=True)
        self.assertEqual(s.calls, [('start',)])
        self.assertEqual(a.snapshot()['playbackRequest']['status'], 'playing')
        self.assertEqual(a.snapshot()['settings']['lead'], 'mix')
        provider.assert_not_called()

    def test_mix_mode_starts_chosen_music_without_waiting_for_an_unrelated_answer(self):
        gate=threading.Event();entered=threading.Event()
        def evaluate(_):
            entered.set();self.assertTrue(gate.wait(2))
            return decision(),{'responseId':'question'}
        s=PlaybackSession();s.plan(['a','b','c'])
        s.update(executionPlan={'id':'prepared','applied':True})
        s,a,_=self.make(s,evaluate)
        self.change(a,'lead','mix')
        a.message('Explain the rhythm',message_id='question',approved=True)
        self.assertTrue(entered.wait(2))
        try:
            a.message('start playing',message_id='start')
            self.assertEqual(s.calls,[('start',)])
            self.assertEqual(a.playback_request['status'],'starting')
        finally:gate.set();settled(a)

    def test_mix_keeps_hold_and_go_and_requires_an_explicit_catalog_pick(self):
        s, a, provider = self.make(running())
        before = copy.deepcopy(s.snapshot()['order'])
        with patch('time.time',return_value=100), patch('afterhours.cue_planner.verify_receipts'), patch.object(s,'send') as send:
            self.change(a, 'lead', 'mix')
            a.message('hold', message_id='hold')
            self.assertTrue(s.snapshot()['copilot']['held'])
            a.message('go', message_id='go')
            change = s.service_copilot(None,s.state['telemetry']['decks'][0],2,Mock(),155,s.state['timing'])
            self.assertEqual(change['window']['mixAtBeat'],128)
            with self.assertRaisesRegex(ValueError,'Mixing only keeps'):
                a.copilot_action('skip','skip',cc.context(s.snapshot()))
            send.assert_not_called()
        self.assertEqual(s.snapshot()['order'], before)
        provider.assert_not_called()

    def test_explicit_catalog_edit_is_still_allowed_in_mix_mode(self):
        s, a, provider = self.make()
        self.change(a, 'lead', 'mix')
        s.program = lambda: {'current':None,'connected':True}
        result = a.catalog_action('catalog-play-next','pick','d',catalog_browser.context(s.snapshot(),None))
        self.assertIn('d',[t['id'] for t in s.snapshot()['order']])
        self.assertEqual(result['settings']['lead'],'mix')
        provider.assert_not_called()

    def test_off_cancels_unaccepted_work_and_retains_current_audio(self):
        s, a, provider = self.make(running())
        before = copy.deepcopy(s.snapshot()['order'])
        a.enable(True)
        with patch.object(s,'send') as send:
            result = self.change(a,'lead','off')
            self.assertEqual(result['settings']['lead'],'off')
            self.assertEqual(result['allowance']['remaining'],0)
            send.assert_not_called()
        self.assertEqual(s.snapshot()['order'],before)
        self.assertTrue(s.snapshot()['telemetry']['decks'][0]['playing'])
        provider.assert_not_called()

    def test_restart_restores_preferences_but_never_transport_or_allowance(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'conversation.json'
            s, a, provider = self.make(path=path)
            self.change(a,'lead','mix')
            self.change(a,'brief','House at 128 BPM','brief')
            saved = json.loads(path.read_text())
            self.assertEqual(saved['settings']['lead'],'mix')
            next_session = ScenarioSession()
            with patch.object(next_session,'start') as start, patch.object(next_session,'send') as send:
                restored = DJAgent(next_session,path,provider)
                restored.restore_settings()
                state = restored.snapshot()
                self.assertEqual(state['settings']['lead'],'mix')
                self.assertEqual(state['settings']['brief'],'House at 128 BPM')
                self.assertEqual(state['allowance']['remaining'],0)
                self.assertIsNone(state['playbackRequest'])
                self.assertIsNone(state['preparation'])
                self.assertIsNone(next_session.snapshot()['copilot']['command'])
                start.assert_not_called();send.assert_not_called()
            provider.assert_not_called()

    def test_saved_preferences_cannot_override_live_audio(self):
        s, a, _ = self.make(running())
        a.settings.state['lead']='mix'
        with self.assertRaisesRegex(ValueError,'running session'):
            a.restore_settings()
        self.assertEqual(s.snapshot()['copilot']['mode'],'leading')

    def test_pending_receipts_expire_on_restore_without_replay(self):
        saved=dj_settings.Settings()
        saved.confirmed('pending',{'kind':'lead','value':'mix'},{}, {'copilot':{'mode':'mix'}},0,100,'Pending','pending')
        restored=dj_settings.Settings(saved.state)
        self.assertEqual(restored.state['lead'],'mix')
        self.assertEqual(restored.state['receipts'][0]['status'],'expired')

    def test_pending_mode_receipt_follows_actual_engine_completion(self):
        s,a,_=self.make(running())
        result=self.change(a,'lead','off')
        self.assertEqual(result['settings']['receipt']['status'],'pending')
        s._copilot_result('unconfirmed','Cleanup did not confirm; inspect the mixer.')
        result=a.snapshot()
        self.assertEqual(result['settings']['receipt']['status'],'unconfirmed')
        self.assertIn('Cleanup did not confirm',result['settings']['receipt']['message'])

    def test_manual_takeover_invalidates_selection_and_is_saved_by_the_coordinator(self):
        s,a,_=self.make()
        before=context_key(s.snapshot())
        s.stop(pause=False)
        self.assertNotEqual(context_key(s.snapshot()),before)
        self.assertEqual(dj_settings.selection_epoch(s.snapshot()),1)
        a.tick(force=True)
        self.assertEqual(a.settings.state['lead'],'off')

    def test_fresh_set_keeps_actual_mode_after_manual_takeover(self):
        s,a,_=self.make()
        s.stop(pause=False)
        self.assertEqual(a.settings.state['lead'],'all')
        a.fresh()
        self.assertEqual(a.snapshot()['settings']['lead'],'off')


if __name__ == '__main__':
    unittest.main()
