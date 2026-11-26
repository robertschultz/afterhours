"""Fresh starts, insertion and planning deadlines against disposable sessions."""

import threading
import unittest
from unittest.mock import Mock, patch

from afterhours import dj_agent, queue_planning
from afterhours.agent_loop import Lookups
from afterhours.rolling_queue import RollingQueue
from afterhours.vdj_session import DJSession
from test_dj_agent import decision, settled


def track(identifier, key='8A', bpm=125):
    return {'id': identifier, 'title': identifier.upper(), 'bpm': bpm, 'camelot': key}


class QueuePlanningTests(unittest.TestCase):
    def setUp(self):
        self.session = DJSession(threading.Lock(), client_factory=Mock(side_effect=AssertionError('No native connection')))
        self.tracks = [track('a'), track('b', '4A'), track('c'), track('d'), track('x')]
        self.session.update(candidates=self.tracks)
        self.session.plan(['a', 'b', 'c', 'd'])

    def test_engine_inserts_between_compatible_neighbors_without_reordering(self):
        result = queue_planning.placement(self.session.snapshot(), 'x')
        self.assertEqual(result['ids'], ['a', 'b', 'c', 'x', 'd'])
        self.assertEqual((result['afterId'], result['beforeId']), ('c', 'd'))

    def test_locks_keep_absolute_positions_and_prepared_prefix_stays_in_place(self):
        self.session.update(index=0, active=True, phase='playing',
                            setlist={'allowReorder':True,'lockedIds':['d'],'sourceById':{}})
        result = queue_planning.placement(self.session.snapshot(), 'x')
        self.assertEqual(result['ids'], ['a', 'b', 'c', 'd', 'x'])

    def test_invalid_tempo_and_repeat_are_held(self):
        self.session.update(candidates=self.tracks + [track('far', bpm=200)])
        with self.assertRaisesRegex(ValueError, 'No safe slot'):
            queue_planning.placement(self.session.snapshot(), 'far')
        with self.assertRaisesRegex(ValueError, 'already'):
            queue_planning.placement(self.session.snapshot(), 'a')
        self.assertEqual([row['id'] for row in self.session.snapshot()['order']], ['a', 'b', 'c', 'd'])

    def test_quick_set_adopts_future_tracks_without_a_measured_plan(self):
        self.session.plan(['a', 'b'],mix_style='quick')
        self.session.update(index=0,active=True,phase='mixing')
        placement=queue_planning.placement(self.session.snapshot(),'x')
        staged=self.session.revise_queue(placement['ids'])
        self.assertEqual([t['id'] for t in staged['order']],['a','b'])
        self.assertEqual(staged['queuedUpdate']['status'],'ready')
        self.assertEqual(staged['queuedUpdate']['protectedIds'],['a','b'])
        with patch('afterhours.cue_planner.verify_receipts',side_effect=AssertionError('No measured plan')):
            adopted,plan=self.session.adopt_queue(1)
        self.assertEqual([t['id'] for t in adopted],['a','b','x'])
        self.assertIsNone(plan)
        self.assertIsNone(self.session.snapshot()['queuedUpdate'])

    def test_quick_live_insertion_enforces_the_running_fixed_tempo(self):
        self.session.update(candidates=self.tracks+[track('far',bpm=150)])
        self.session.plan(['a','b'],mix_style='quick')
        self.session.update(index=1,active=True,phase='playing')
        with self.assertRaisesRegex(ValueError,'No safe slot'):
            queue_planning.placement(self.session.snapshot(),'far')
        self.assertIsNone(self.session.snapshot()['queuedUpdate'])

    def test_rolling_quick_set_appends_as_each_track_starts(self):
        self.session.plan(['a','b'],mix_style='quick')
        self.session.update(index=0,active=True,phase='playing',rollingQueue={'status':'ready'})
        roller=RollingQueue()
        roller.tick(self.session, 12)
        staged=self.session.snapshot()['queuedUpdate']
        self.assertEqual(staged['protectedIds'],['a','b'])
        self.assertEqual([t['id'] for t in staged['tracks']],['a','b','c'])
        self.session.adopt_queue(1)
        self.assertEqual(self.session.snapshot()['setlist']['sourceById']['c'],'planner')
        self.session.update(index=1)
        roller.tick(self.session, 12)
        staged=self.session.snapshot()['queuedUpdate']
        self.assertEqual(staged['protectedIds'],['a','b','c'])
        self.assertEqual([t['id'] for t in staged['tracks']],['a','b','c','d'])

    def test_rolling_queue_search_is_bounded_and_only_for_auto_playback(self):
        rows=[{**track('a'),'artist':'One Artist'},{**track('b'),'artist':'One Artist'},track('far',bpm=200)]
        self.session.update(candidates=rows)
        self.session.plan(['a','b'],mix_style='quick')
        self.session.update(index=0,active=True,phase='playing',rollingQueue={'status':'ready'})
        roller=RollingQueue()
        roller.tick(self.session, 3)
        self.assertIsNone(self.session.catalog_job)
        roller.tick(self.session, 12)
        self.assertEqual(self.session.catalog_job.terms,['One Artist'])
        self.assertTrue(self.session.catalog_job.rolling)
        self.session.catalog_job.done=True
        roller.tick(self.session, 12)
        self.assertEqual(self.session.snapshot()['rollingQueue']['status'],'held')
        self.session.update(phase='takeover')
        self.session.update(candidates=rows+[track('c')])
        roller.tick(self.session, 20)
        self.assertIsNone(self.session.snapshot()['queuedUpdate'])

    def test_rolling_measured_set_uses_existing_background_verification(self):
        self.session.plan(['a','b'])
        self.session.update(index=0,active=True,phase='playing',rollingQueue={'status':'ready'},
                            executionPlan={'id':'plan','applied':True,'targetBpm':125})
        RollingQueue().tick(self.session, 22)
        staged=self.session.snapshot()['queuedUpdate']
        self.assertEqual(staged['protectedIds'],['a','b'])
        self.assertEqual(staged['status'],'needs-analysis')

    def test_agent_requests_insertion_and_executor_stages_existing_order(self):
        self.session.update(index=0, active=True, phase='playing',
                            executionPlan={'id':'plan','applied':True,'targetBpm':125})
        agent = dj_agent.DJAgent(self.session, evaluator=Mock(return_value=(
            {**decision(), 'trackInsertion': {'trackId':'x'}}, {})))
        agent.message('Please fit X into this set', message_id='insert', approved=True)
        settled(agent)
        state = self.session.snapshot()
        self.assertEqual([row['id'] for row in state['order']], ['a','b','c','d'])
        self.assertEqual([row['id'] for row in state['queuedUpdate']['tracks']], ['a','b','c','x','d'])
        self.assertEqual(state['queuedUpdate']['status'], 'needs-analysis')
        self.assertEqual(state['queuedUpdate']['protectedIds'], ['a','b'])

    def test_fresh_start_rotates_recent_openers_and_starts_new_order(self):
        old_session = self.session.snapshot()['id']
        played = []
        self.session.start = Mock(side_effect=lambda **_: played.append([t['id'] for t in self.session.snapshot()['order']]))
        agent = dj_agent.DJAgent(self.session, evaluator=Mock(return_value=(decision(['a','b','c','d']), {})))
        agent.message('Start a fresh queue', message_id='fresh', approved=True)
        settled(agent)
        self.assertNotEqual(self.session.snapshot()['id'], old_session)
        agent.tick(force=True)
        self.assertEqual(played, [['c','d','a','b']])
        self.assertEqual(agent.evaluator.call_args.args[0]['music']['recentOpeningIds'], ['a','b'])

    def test_fresh_start_requires_approval_and_preserves_playing_session(self):
        agent = dj_agent.DJAgent(self.session, evaluator=Mock())
        session_id = self.session.snapshot()['id']
        with self.assertRaisesRegex(ValueError, 'Approve'):
            agent.message('Start a fresh queue', message_id='fresh')
        self.session.update(active=True, phase='playing', index=0)
        with self.assertRaisesRegex(ValueError, 'Pause playback'):
            agent.message('Start a fresh queue', message_id='fresh', approved=True)
        self.assertEqual(self.session.snapshot()['id'], session_id)
        agent.evaluator.assert_not_called()

    def test_idle_voice_play_builds_new_queue_without_replenishing_talk_budget(self):
        from afterhours import playback_requests
        old=self.session.snapshot()['id']
        self.session.start=Mock()
        provider=Mock(return_value=({**decision(['c','d']), 'reply':'Opening pair selected.'}, {}))
        agent=dj_agent.DJAgent(self.session, evaluator=provider)
        agent.enable(True,seconds=120,requests=6)
        self.session.discover_more=Mock(side_effect=lambda _: self.session.update(
            phase='discovering',catalogSearch={'status':'searching'}))
        deadline=agent.deadline
        result=agent.message('Play Daft Punk',source='voice',message_id='voice-fresh',approved=True)
        self.session.discover_more.assert_called_once_with('daft punk')
        self.assertEqual(result['status'],'searching')
        provider.assert_not_called()
        self.session.update(phase='catalog-ready',catalogSearch={'status':'complete','matched':2})
        agent.tick(force=True)
        settled(agent)
        provider.assert_called_once()
        self.assertEqual(provider.call_args.args[0]['reason'],'choose from catalog results')
        self.assertNotEqual(self.session.snapshot()['id'],old)
        self.assertEqual(agent.state['messages'][0]['queueMode'],'fresh-start')
        self.assertTrue(agent.playback_request['freshQueue'])
        self.assertEqual(agent.deadline,deadline)
        self.assertLessEqual(agent.remaining,6)
        self.assertFalse(playback_requests.fresh_voice_request('Play the current queue',self.session.snapshot()))
        self.assertFalse(playback_requests.fresh_voice_request('Play Daft Punk',{'telemetry':{'decks':[{'playing':True}]}}))

    def test_runway_uses_fresh_clock_and_verified_cues(self):
        state = self.session.snapshot()
        state.update(index=0, active=True, phase='playing', timing={'mixAtBeat':32},
                     executionPlan={'applied':True,'targetBpm':120,'selections':[
                        {'trackId':row['id'],'entryBeat':0,'exitBeat':32,'replayVerified':True}
                        for row in state['order']
                     ]}, telemetry={'sampledAt':100,'decks':[
                        {'id':'a','playing':True,'beatPosition':12,'bpm':120}
                     ]})
        self.assertEqual(queue_planning.runway(state, 101), 57)
        self.assertIsNone(queue_planning.runway(state, 104))
        state['executionPlan']['selections'][1]['replayVerified'] = False
        self.assertIsNone(queue_planning.runway(state, 101))

    def test_next_track_prefers_a_verified_cue_between_key_matches(self):
        rows = [track('out'), track('close'), track('ready', bpm=126)]
        book = Lookups({'executionPlan':{'selections':[{'trackId':'ready','replayVerified':True}]}}, rows)
        request = {'afterId':'out','limit':2}
        result = book.suggest(request)
        baseline = book.suggest(request, prefer_prepared=False)
        self.assertEqual([row['id'] for row in result['matches']], ['ready','close'])
        self.assertEqual([row['id'] for row in baseline['matches']], ['close','ready'])

    def test_live_deadline_prevents_dispatch(self):
        times = iter([0, 41])
        with patch.object(dj_agent.ai_mix, 'api_key', return_value='fixture'), \
             patch.object(dj_agent, 'provider_call') as send, \
             patch.object(dj_agent, 'planning_request', return_value=(b'final', {})):
            with self.assertRaises(dj_agent.PlanningDeadline):
                dj_agent.evaluate({}, clock=lambda: next(times), time_budget=40)
        send.assert_not_called()

    def test_configured_model_cannot_exceed_existing_approval(self):
        env = {'AFTERHOURS_PLANNER_MODEL':'fixture-model',
               'AFTERHOURS_PLANNER_INPUT_USD_PER_MILLION':'0.25',
               'AFTERHOURS_PLANNER_OUTPUT_USD_PER_MILLION':'5'}
        with patch.dict('os.environ',env), patch.object(dj_agent.ai_mix,'api_key',return_value='fixture'), \
             patch.object(dj_agent,'provider_call') as provider:
            with self.assertRaisesRegex(ValueError,'pricing needs verification'):
                dj_agent.evaluate({'typedTurn':True})
        provider.assert_not_called()

    def test_profile_scoped_track_preference_appears_in_planning_and_can_be_cleared(self):
        evaluator = Mock(return_value=(decision(), {}))
        agent = dj_agent.DJAgent(self.session, evaluator=evaluator)
        agent.profile_context = lambda: {'id':'resident'}
        agent.record_track_feedback('x', 'avoid', 'Save that for the late set')
        agent.message('Recheck this queue', message_id='first', approved=True)
        settled(agent)
        row = next(row for row in evaluator.call_args.args[0]['music']['catalog'] if row['id']=='x')
        self.assertEqual(row['operatorPreference'], 'avoid')
        agent.profile_context = lambda: {'id':'guest'}
        agent.message('Recheck this queue', message_id='second', approved=True)
        settled(agent)
        row = next(row for row in evaluator.call_args.args[0]['music']['catalog'] if row['id']=='x')
        self.assertNotIn('operatorPreference', row)
        agent.profile_context = lambda: {'id':'resident'}
        self.assertEqual(agent.record_track_feedback('x', 'clear')['trackFeedback'], [])

    def test_fresh_start_keeps_the_operator_opening_choice_after_clarification(self):
        self.session.start = Mock()
        clarify = {**decision(), 'sessionScope':{'mode':'clarify','message':'Which opening track?'}}
        selected = {**decision(['a','b','c','d']), 'sessionScope':{'mode':'finite','message':'Opening with A.'}}
        agent = dj_agent.DJAgent(self.session, evaluator=Mock(side_effect=[(clarify,{}),(selected,{})]))
        agent.message('Start a fresh queue', message_id='fresh', approved=True)
        settled(agent)
        self.assertEqual(agent.playback_request['status'], 'clarifying')
        agent.message('Open with A', message_id='opener', approved=True)
        settled(agent)
        agent.tick(force=True)
        self.assertEqual([row['id'] for row in self.session.snapshot()['order']], ['a','b','c','d'])
        self.session.start.assert_called_once()
