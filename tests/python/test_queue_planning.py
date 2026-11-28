"""Fresh starts, insertion and planning deadlines against disposable sessions."""

import copy
import threading
import unittest
from unittest.mock import Mock, patch

from afterhours import ai_mix, cue_planner, dj_agent, dj_discovery, queue_planning, tempo_planner
from afterhours.agent_loop import Lookups
from afterhours.rolling_queue import RollingQueue
from afterhours.vdj_session import DJSession
from test_cue_planner import fixture
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

    def test_rolling_avoids_repeating_the_same_song_in_a_different_mix(self):
        rows = [
            {**track('a'), 'title': 'Can’t Stop It (Aviators Remix)', 'artist': 'Cabin Crew'},
            track('b'),
            {**track('alternate'), 'title': 'Can’t Stop It (Club Mix)', 'artist': 'Cabin Crew'},
            track('new'),
        ]
        self.session.update(candidates=rows)
        self.session.plan(['a', 'b'], mix_style='quick')
        self.session.update(index=0, active=True, phase='playing', rollingQueue={'status':'ready'})
        RollingQueue().tick(self.session, 25)
        self.assertEqual(self.session.snapshot()['queuedUpdate']['tracks'][-1]['id'], 'new')

    def test_rolling_avoids_repeating_the_same_song_with_a_different_feature_credit(self):
        for suffix in ('feat. Sam Smith','with Sam Smith'):
            with self.subTest(suffix=suffix):
                rows = [
                    {**track('a'), 'title':'Latch', 'artist':'Disclosure, Sam Smith'},
                    track('b'),
                    {**track('alternate'), 'title':f'Latch ({suffix})', 'artist':'Disclosure'},
                    track('new'),
                ]
                self.session.update(candidates=rows,active=False)
                self.session.plan(['a','b'],mix_style='quick')
                self.session.update(index=0,active=True,phase='playing',rollingQueue={'status':'ready'})
                RollingQueue().tick(self.session,25)
                self.assertEqual(self.session.snapshot()['queuedUpdate']['tracks'][-1]['id'],'new')

    def test_rolling_avoids_a_catalog_title_variant_with_a_missing_article(self):
        rows = [
            {**track('a'), 'title':'Take Me to the King (feat. Kirk Franklin)', 'artist':'Tamela Mann'},
            track('b'),
            {**track('alternate'), 'title':'Take Me to King', 'artist':'Tamela Mann'},
            track('new'),
        ]
        self.session.update(candidates=rows)
        self.session.plan(['a','b'],mix_style='quick')
        self.session.update(index=0,active=True,phase='playing',rollingQueue={'status':'ready'})
        RollingQueue().tick(self.session,25)
        self.assertEqual(self.session.snapshot()['queuedUpdate']['tracks'][-1]['id'],'new')

    def test_rolling_genre_follows_the_requested_direction(self):
        rows = [
            {**track('a'), 'genre':'Classic Rock'},
            {**track('b'), 'genre':'Classic Rock'},
            {**track('house'), 'genre':'House'},
            {**track('rock'), 'genre':'Rock'},
        ]
        self.session.update(candidates=rows)
        self.session.plan(['a', 'b'], mix_style='quick')
        self.session.update(index=0, active=True, phase='playing', rollingQueue={'status':'ready'})
        roller = RollingQueue()
        roller.tick(self.session, 25)
        queued = self.session.snapshot()['queuedUpdate']
        self.assertEqual(queued['tracks'][-1]['id'], 'rock')

        self.session.update(queuedUpdate=None, agentDirection='Switch to house')
        roller.tick(self.session, 25)
        queued = self.session.snapshot()['queuedUpdate']
        self.assertEqual(queued['tracks'][-1]['id'], 'house')

    @patch('afterhours.rolling_queue.DISCOVERY.neighbors', return_value=())
    def test_named_artist_refill_rejects_electronic_cache_but_can_broaden_to_related_music(self, _):
        seeds = [
            {**track('opener', '7A', 118), 'artist': 'Avery Stone & The Chorus', 'genre': 'Christian'},
            {**track('current', '6B', 120), 'artist': 'Avery Stone', 'genre': 'Christian'},
        ]
        other = {**track('other', '6B', 120), 'artist': 'Unrelated Producer', 'genre': 'Electronic'}
        unrelated_soul = {**track('soul', '6B', 120), 'artist': 'Unrelated Singer', 'genre': 'R&B/Soul'}
        related = {**track('related', '6B', 120), 'artist': 'Another Singer', 'genre': 'Christian R&B/Soul'}
        self.session.update(candidates=seeds + [other, unrelated_soul])
        self.session.plan([row['id'] for row in seeds])
        self.session.update(index=1, active=True, phase='playing', rollingQueue={'status':'ready'},
                            agentDirection='Play Avery Stone',
                            executionPlan={'id':'plan','applied':True,'targetBpm':120})
        roller = RollingQueue()
        roller.advisor.enabled = False
        roller.search = Mock(return_value=False)
        roller.tick(self.session, 25)
        self.assertIsNone(self.session.snapshot()['queuedUpdate'])
        self.assertEqual(roller.search.call_args.args[3], 'gospel')

        self.session.update(candidates=seeds + [other, unrelated_soul, related])
        roller.tick(self.session, 25)
        staged = self.session.snapshot()['queuedUpdate']
        self.assertEqual([row['id'] for row in staged['tracks']], ['opener', 'current', 'related'])

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

    def test_rolling_search_can_use_related_house_with_no_explicit_style(self):
        discovery = dj_discovery.Discovery(clock=lambda: 1)
        discovery.feeds['deep house'] = {'observedAt': 1, 'source': 'Test chart', 'rows': [
            {'title': 'Related Recording', 'artist': 'Related Artist', 'percentile': 90},
        ]}
        order = [{**track('a'), 'artist': 'Opening Artist', 'genre': 'House'}]
        with patch('afterhours.rolling_queue.DISCOVERY', discovery):
            for direction, expected in (('', 'Related Artist Related Recording'), ('House only', 'House')):
                with self.subTest(direction=direction):
                    roller = RollingQueue()
                    roller.searched.add('opening artist')
                    session = Mock()
                    self.assertTrue(roller.search(session, {'agentDirection':direction}, order, 'house', True))
                    self.assertEqual(session.discover_more.call_args.args, (expected,))

    def test_rolling_breaks_a_repeated_artist_run_without_risking_the_next_track(self):
        for alternate_bpm, selected in [(125,'x'),(190,'c')]:
            with self.subTest(alternate_bpm=alternate_bpm):
                seeds=[{**track(identifier),'artist':artist,'genre':'House'} for identifier,artist in
                       [('a','Resident DJ'),('b','Resident DJ & Guest'),('c','Resident DJ')]]
                seeds.append({**track('x',bpm=alternate_bpm),'artist':'Related Artist','genre':'House'})
                self.session.update(active=False,candidates=seeds)
                self.session.plan(['a','b'])
                self.session.update(index=1,active=True,phase='playing',rollingQueue={'status':'ready'},
                                    entryCadence=[60,30],executionPlan={'applied':True,'targetBpm':125})
                roller=RollingQueue()
                roller.advisor.advise=Mock(return_value=(None,False))
                roller.live_matches.add('c')
                roller.tick(self.session,28)
                offered=roller.advisor.advise.call_args.args[2]
                self.assertEqual([row['id'] for row in offered],['x'])
                queued=self.session.snapshot()['queuedUpdate']
                self.assertEqual([row['id'] for row in queued['tracks']],['a','b',selected])

    def test_rolling_measured_set_uses_existing_background_verification(self):
        self.session.plan(['a','b'])
        self.session.update(index=0,active=True,phase='playing',rollingQueue={'status':'ready'},
                            executionPlan={'id':'plan','applied':True,'targetBpm':125})
        RollingQueue().tick(self.session, 22)
        staged=self.session.snapshot()['queuedUpdate']
        self.assertEqual(staged['protectedIds'],['a','b'])
        self.assertEqual(staged['status'],'needs-analysis')

    def test_rolling_search_tries_related_artists_before_extending_an_artist_streak(self):
        order=[{**track('a'),'artist':'Resident DJ','genre':'House'},
               {**track('b'),'artist':'Resident DJ & Guest','genre':'House'}]
        roller=RollingQueue()
        session=Mock()
        with patch('afterhours.rolling_queue.research_hints',return_value=[]), \
                patch('afterhours.rolling_queue.DISCOVERY') as facts:
            facts.hints.return_value=[]
            facts.neighbors.return_value=['Related Artist']
            self.assertTrue(roller.search(session, {}, order, 'house', True))
        self.assertEqual(session.discover_more.call_args.args, ('Related Artist',))

    def test_rolling_exact_title_miss_broadens_once_to_the_same_credited_artist(self):
        order = [{**track('a'), 'artist':'Opening Artist', 'genre':'House'}]
        hints = [{'artist':'Related Artist', 'title':'Catalog Version'},
                 {'artist':'Another Artist', 'title':'Second Candidate'}]
        for seen, direction, expected, scope in (
            (set(), 'house late set', 'Related Artist', ('Related Artist','')),
            ({'found'}, 'house late set', 'Another Artist Second Candidate', ('Another Artist','Second Candidate')),
            (set(), 'house early set', 'Another Artist Second Candidate', ('Another Artist','Second Candidate')),
        ):
            with self.subTest(seen=seen, direction=direction):
                roller = RollingQueue()
                session = Mock()
                searches = []

                def discover(term, **_):
                    searches.append(term)
                    session.catalog_job = Mock(done=False, seen=set())

                session.discover_more.side_effect = discover
                def research(_, __, searched):
                    return [row for row in hints
                            if f"{row['artist']} {row['title']}".lower() not in searched]

                with patch('afterhours.rolling_queue.research_hints', side_effect=research), \
                        patch('afterhours.rolling_queue.DISCOVERY') as discovery:
                    discovery.hints.return_value = []
                    discovery.neighbors.return_value = []
                    self.assertTrue(roller.search(session, {'agentDirection':'house late set'}, order, 'house', True))
                    session.catalog_job.set_rolling_seed.assert_called_once_with('Related Artist','Catalog Version')
                    session.catalog_job.done = True
                    session.catalog_job.seen = seen
                    self.assertTrue(roller.search(session, {'agentDirection':direction}, order, 'house', True))
                    self.assertEqual(searches, ['Related Artist Catalog Version', expected])
                    session.catalog_job.set_rolling_seed.assert_called_once_with(*scope)

    def test_rolling_search_recovers_from_exhausted_recent_collaboration_queries(self):
        order = [{**track('a'), 'artist':'Earlier Artist'},
                 {**track('b'), 'artist':'Recent Duo & Guest Artist', 'genre':'Pop'},
                 {**track('c'), 'artist':'Later Artist', 'genre':'Pop'},
                 {**track('d'), 'artist':'Last Artist', 'genre':'Pop'}]
        roller = RollingQueue()
        roller.searched.update(('last artist','later artist','recent duo guest artist','pop'))
        session = Mock()
        with patch('afterhours.rolling_queue.DISCOVERY') as facts:
            facts.hints.return_value=[]
            facts.neighbors.return_value=[]
            self.assertTrue(roller.search(session, {}, order, 'pop', True))
        self.assertEqual(session.discover_more.call_args.args, ('guest artist',))

    def test_measured_rolling_set_refills_while_three_followups_remain(self):
        self.session.update(index=0,active=True,phase='playing',rollingQueue={'status':'ready'},
                            executionPlan={'id':'plan','applied':True,'targetBpm':125})
        RollingQueue().tick(self.session,22)
        staged=self.session.snapshot()['queuedUpdate']
        self.assertEqual(staged['protectedIds'],['a','b'])
        self.assertEqual(staged['rollingTrackId'],'x')

    def test_open_ended_refill_replaces_a_conclusive_scan_or_fit_failure(self):
        self.session.update(candidates=[{**track(tid), 'genre': 'Christmas: Pop'}
                                        for tid in ('a', 'b', 'c', 'd')])
        self.session.plan(['a', 'b'])
        self.session.update(index=0, active=True, phase='playing', rollingQueue={'status': 'ready'},
                            executionPlan={'id': 'plan', 'applied': True, 'targetBpm': 125},
                            agentDirection='Christmas favorites')
        roller = RollingQueue()
        roller.tick(self.session, 22)
        pending = self.session.state['queuedUpdate']
        rejected = pending['rollingTrackId']
        pending.update(status='blocked', analysisMissId=rejected)
        current = {'position': 45, 'controls': {'durationMs': 180000}}
        self.assertGreater(self.session.final_preparation_runway({'id': 'b'}, 1, current), 16)
        roller.tick(self.session, 22)
        self.assertIn(rejected, roller.missed)
        self.assertNotEqual(self.session.state['queuedUpdate']['rollingTrackId'], rejected)
    def test_rolling_replaces_only_its_failed_catalog_lookup_after_cleanup(self):
        self.session.plan(['a','b'])
        self.session.update(index=1,active=True,phase='playing',rollingQueue={'status':'ready'},
                            executionPlan={'id':'plan','applied':True,'targetBpm':125})
        roller=RollingQueue()
        roller.tick(self.session,30)
        pending=self.session.state['queuedUpdate']
        miss=pending['rollingTrackId']
        pending.update(status='blocked',error='Native Load acknowledgement unknown')
        roller.tick(self.session,30)
        self.assertEqual(self.session.snapshot()['queuedUpdate']['rollingTrackId'],miss)
        self.assertEqual(self.session.snapshot()['rollingQueue']['status'],'held')
        pending.update(catalogMissId=miss,error='Recording was not found before Load')
        self.session.update(rollingQueue={'status':'preparing'})
        roller.tick(self.session,30)
        replacement=self.session.snapshot()['queuedUpdate']
        self.assertNotEqual(replacement['rollingTrackId'],miss)
        self.assertEqual(replacement['protectedIds'],['a','b'])
        self.assertNotIn(miss,[item['id'] for item in replacement['tracks']])

    def test_failed_model_candidate_releases_refill_but_operator_pick_is_retained(self):
        for source in ('planner','operator'):
            with self.subTest(source=source):
                self.session.update(active=False,phase='stopped',setPins=[])
                self.session.plan(['a','b'])
                self.session.update(index=1,active=True,phase='playing',rollingQueue={'status':'ready'},
                                    executionPlan={'id':'plan','applied':True,'targetBpm':125})
                self.session.revise_queue(['a','b','c','d'],source=source)
                pending=self.session.state['queuedUpdate']
                pending['status']='analyzing'
                job=Mock(revision=pending['id'],done=True,error='Track analysis did not settle.',
                         analysis_miss='c',catalog_miss=None,cache={},osc_reply_timeout=False)
                job.snapshot.return_value={'message':job.error}
                self.session.background_job=job
                self.session.service_background(Mock(),24)
                job.close.assert_called_once()
                roller=RollingQueue()
                roller.tick(self.session,24)
                state=self.session.snapshot()
                self.assertEqual([t['id'] for t in state['order']],['a','b'])
                if source=='planner':
                    self.assertEqual(roller.missed,{'c'})
                    self.assertNotIn('c',[t['id'] for t in state['queuedUpdate']['tracks']])
                else:
                    self.assertEqual(state['queuedUpdate']['id'],pending['id'])
                    self.assertEqual(state['queuedUpdate']['status'],'blocked')

    def test_timed_refill_starts_when_nine_upcoming_slots_remain(self):
        tracks = [track(chr(97+i)) for i in range(13)]
        self.session.update(candidates=tracks)
        self.session.plan([row['id'] for row in tracks[:11]])
        self.session.update(index=0, active=True, phase='playing', rollingQueue={'status':'ready'},
                            entryCadence=[60,30], executionPlan={'applied':True,'targetBpm':125})
        roller = RollingQueue()
        roller.live_matches.update(row['id'] for row in tracks)
        roller.tick(self.session, 35)
        self.assertIsNone(self.session.snapshot()['queuedUpdate'])
        self.session.update(index=1)
        roller.tick(self.session, 35)
        update = self.session.snapshot()['queuedUpdate']
        self.assertEqual(update['rollingTrackIds'], ['l', 'm'])
        self.assertEqual(update['protectedIds'], ['a', 'b', 'c'])

    def test_long_last_slot_can_stage_two_without_waiting_to_pick_again(self):
        self.session.plan(['a', 'b'])
        self.session.update(index=1, active=True, phase='playing', rollingQueue={'status':'ready'},
                            entryCadence=[60,30], executionPlan={'applied':True,'targetBpm':125})
        roller = RollingQueue()
        roller.advisor.enabled = False
        roller.tick(self.session, 43)
        queued = self.session.snapshot()['queuedUpdate']
        self.assertEqual(len(queued['rollingTrackIds']), 2)
        self.assertEqual(queued['protectedIds'], ['a', 'b'])

    def test_timed_refill_discovers_successors_while_the_current_batch_is_measured(self):
        self.session.plan(['a', 'b'])
        self.session.update(index=1, active=True, phase='playing', rollingQueue={'status':'ready'},
                            entryCadence=[60,30], executionPlan={'applied':True,'targetBpm':125})
        roller = RollingQueue()
        roller.advisor.enabled = False
        roller.tick(self.session, 43)
        pending = self.session.state['queuedUpdate']
        pending['status'] = 'analyzing'
        self.session.background_job = Mock(revision=pending['id'])
        self.session.background_job.navigation_idle.return_value = False
        roller.search = Mock()
        roller.tick(self.session, 32)
        roller.search.assert_not_called()
        self.session.background_job.navigation_idle.return_value = True
        roller.tick(self.session, 32)
        roller.search.assert_called_once_with(self.session, unittest.mock.ANY, pending['tracks'], '', True,
                                              lookahead=True)
        self.assertEqual(self.session.state['queuedUpdate']['id'], pending['id'])
        self.assertEqual(self.session.state['rollingQueue']['status'], 'preparing')

    def test_timed_refill_skips_provider_continuous_mix_cuts(self):
        self.session.plan(['a', 'b'])
        self.session.update(index=0, active=True, phase='playing', rollingQueue={'status':'ready'},
                            entryCadence=[60,30], executionPlan={'applied':True, 'targetBpm':125})
        for row, suffix in zip(self.session.state['candidates'][2:4], [' [Mixed]', ' (Mixed)']):
            row['title'] += suffix
        roller = RollingQueue()
        roller.live_matches.update(row['id'] for row in self.tracks)
        roller.tick(self.session, 35)
        added = self.session.snapshot()['queuedUpdate']['tracks'][2:]
        self.assertTrue(added)
        self.assertTrue(all(not row['title'].endswith((' [Mixed]', ' (Mixed)')) for row in added))

    def test_timed_queue_recovers_a_rejected_measured_fit_but_preserves_uncertain_load(self):
        self.session.plan(['a','b'])
        self.session.update(index=0, active=True, phase='playing', rollingQueue={'status':'ready'},
                            entryCadence=[60,30], executionPlan={'id':'plan','applied':True,'targetBpm':125})
        roller = RollingQueue()
        roller.tick(self.session, 20)
        pending = self.session.state['queuedUpdate']
        miss = pending['rollingTrackId']
        pending.update(status='blocked', error='Native load remains unconfirmed.')
        roller.tick(self.session, 20)
        self.assertEqual(self.session.state['queuedUpdate']['rollingTrackId'], miss)
        pending['measuredFitMissId'] = miss
        roller.tick(self.session, 20)
        self.assertNotEqual(self.session.state['queuedUpdate']['rollingTrackId'], miss)
        self.assertEqual(self.session.state['queuedUpdate']['protectedIds'], ['a','b'])

    def test_timed_queue_replaces_exact_failed_scan_in_parallel_batch(self):
        self.session.plan(['a','b'])
        self.session.update(index=0,active=True,phase='playing',rollingQueue={'status':'ready'},
                            entryCadence=[60,30],executionPlan={'applied':True,'targetBpm':125})
        roller=RollingQueue()
        roller.live_matches.update(t['id'] for t in self.tracks)
        roller.tick(self.session,35)
        pending=self.session.state['queuedUpdate']
        good,miss=pending['rollingTrackIds']
        pending.update(status='blocked',analysisMissId=miss,error='Exact recording grid did not settle')
        roller.tick(self.session,35)
        replacement=self.session.state['queuedUpdate']
        ids=[track['id'] for track in replacement['tracks']]
        self.assertEqual(ids[:2],['a','b'])
        self.assertIn(good,ids)
        self.assertNotIn(miss,ids)
        self.assertNotIn('analysisMissId',replacement)

    def test_timed_queue_prepares_explicit_opening_tail_in_order(self):
        self.session.plan(['a','b'])
        self.session.update(index=0, active=True, phase='playing', rollingQueue={'status':'ready'},
                            timedContinuation=[self.tracks[-1], self.tracks[-2]],
                            entryCadence=[60,30], executionPlan={'id':'plan','applied':True,'targetBpm':125})
        RollingQueue().tick(self.session, 20)
        state = self.session.snapshot()
        self.assertEqual([row['id'] for row in state['queuedUpdate']['tracks']], ['a','b','x'])
        self.assertEqual([row['id'] for row in state['timedContinuation']], ['d'])

    def test_timed_queue_commits_the_measured_prefix_and_keeps_its_preparation_owner(self):
        self.session.plan(['a','b'])
        original = self.session.snapshot()['order']
        self.session.update(index=0, active=True, phase='playing', rollingQueue={'status':'ready'},
                            entryCadence=[60,30], executionPlan={'id':'plan','applied':True,'targetBpm':125})
        roller = RollingQueue()
        roller.live_matches.update(t['id'] for t in self.tracks)
        roller.tick(self.session, 30)
        queued = self.session.state['queuedUpdate']
        first, second = queued['tracks'][-2:]
        queued['status'] = 'analyzing'
        job = Mock(revision=queued['id'], result=[first])
        self.session.background_job = job
        prepared = {'analysis': {}, 'executionPlan': {'id':'verified-plan','transitions':[]}}
        with patch.object(self.session, 'fit_background_tracks', return_value=(original+[first], prepared)), \
                patch('afterhours.cue_planner.verify_receipts'):
            self.session.publish_prepared_prefix(job)
            order, plan = self.session.adopt_queue(0)
        self.assertEqual([t['id'] for t in order], ['a','b',first['id']])
        self.assertTrue(plan['applied'])
        self.assertIs(self.session.background_job, job)
        pending = self.session.state['queuedUpdate']
        self.assertEqual(pending['id'], queued['id'])
        self.assertEqual(pending['status'], 'analyzing')
        self.assertEqual(pending['tracks'][-1]['id'], second['id'])
        self.assertEqual(pending['rollingTrackId'], second['id'])
        self.assertNotIn('rollingTrackIds', pending)
        job.done = True
        job.error = None
        job.result = [first, second]
        job.cache = {}
        job.snapshot.return_value = {'message':'Both tracks have measured cues.'}
        with patch.object(self.session, 'fit_background_tracks', side_effect=ValueError('No protected tempo route')):
            self.session.service_background(Mock(), 24)
        self.assertEqual(self.session.state['queuedUpdate']['measuredFitMissId'], second['id'])
        self.assertEqual([track['id'] for track in self.session.state['order']], ['a','b',first['id']])

    def staged_measured_batch(self, missed, *, pin=None):
        _, report, response = fixture()
        tracks = [{key: t[key] for key in ('id', 'title', 'bpm', 'duration', 'camelot', 'genre', 'artist')}
                  for t in report['tracks']]
        measured = copy.deepcopy(report['tracks'][2:])
        # Catalog estimates admitted both candidates. Native measurement
        # proves one cannot join this actually frozen 155 BPM route.
        measured[int(missed) - 2]['bpm'] = 143 if missed == '2' else 141
        seed = {**report, 'tracks': report['tracks'][:2]}
        seed['fingerprint'] = cue_planner.fingerprint(seed['tracks'])
        review = ai_mix.validate_plan({**response, 'selections': response['selections'][:2],
                                     'transitions': response['transitions'][:1]}, seed, 30)
        plan = cue_planner.compile_plan(review, seed, tracks[:2], 30)
        plan['applied'] = True
        self.session.update(candidates=tracks, active=False)
        self.session.plan(['0','1'])
        self.session.update(index=0, active=True, phase='playing', rollingQueue={'status':'preparing'},
                            entryCadence=[60,30], executionPlan=plan, analysis=seed, aiReview=review,
                            setPins=[pin] if pin else [])
        self.session.revise_queue(['0','1','2','3'], source='planner', reason='Timed rolling pair.')
        pending = self.session.state['queuedUpdate']
        pending.update(status='analyzing', rollingTrackId='2', rollingTrackIds=['2','3'])
        self.session.background_job = Mock(revision=pending['id'], result=measured, done=True, error=None, cache={})
        self.session.background_job.snapshot.return_value = {'message': 'Both recordings measured.'}
        return plan, pending['id'], measured

    def test_typed_two_record_fit_recovers_the_other_recording_on_verified_cues(self):
        for missed, survivor in (('2', '3'), ('3', '2')):
            with self.subTest(missed=missed):
                plan, revision, measured = self.staged_measured_batch(missed)
                state = self.session.snapshot()
                with self.assertRaises(tempo_planner.TempoRouteError) as error:
                    self.session.fit_background_tracks(state, state['queuedUpdate'],
                                                       state['queuedUpdate']['tracks'], measured, 2)
                self.assertEqual(error.exception.track['id'], missed)
                client = Mock()
                with patch('afterhours.cue_planner.verify_receipts') as verify:
                    self.session.service_background(client, 24)
                    pending = self.session.snapshot()['queuedUpdate']
                    self.assertEqual(pending['protectedIds'], ['0','1'])
                    self.assertEqual(pending['id'], revision)
                    self.assertEqual(pending['status'], 'ready')
                    self.assertEqual(pending['rollingTrackId'], survivor)
                    self.assertEqual([t['id'] for t in pending['tracks']], ['0','1',survivor])
                    verify.assert_called_once()
                    self.session.update(index=1)
                    self.assertGreater(self.session.final_preparation_runway({'id':'1'}, 1, {'position':60}), 16)
                    order, applied = self.session.adopt_queue(1)
                self.assertEqual([t['id'] for t in order], ['0','1',survivor])
                self.assertEqual(applied['selections'][:2], plan['selections'])
                self.assertEqual(applied['transitions'][0], plan['transitions'][0])
                self.assertIsNone(self.session.state['queuedUpdate'])
                client.send.assert_not_called()

    def test_failed_survivor_verification_leaves_existing_plan_and_refill_runway(self):
        original, revision, _ = self.staged_measured_batch('2')
        with patch('afterhours.cue_planner.verify_receipts', side_effect=ValueError('Survivor digest drift')):
            self.session.service_background(Mock(), 24)
        state = self.session.snapshot()
        self.assertEqual(state['executionPlan'], original)
        self.assertEqual([track['id'] for track in state['order']], ['0','1'])
        pending = state['queuedUpdate']
        self.assertEqual(pending['id'], revision)
        self.assertEqual(pending['status'], 'blocked')
        self.assertEqual(pending['measuredFitMissId'], '2')
        self.assertGreater(self.session.final_preparation_runway({'id':'1'}, 1, {'position':60}), 16)
        self.session.update(setPins=['3'])
        RollingQueue().tick(self.session, 24)
        self.assertEqual(self.session.state['queuedUpdate']['id'], revision)
        self.assertEqual(self.session.final_preparation_runway({'id':'1'}, 1, {'position':60}), 0)

    def test_operator_owned_sibling_is_not_removed_with_the_failed_revision(self):
        _, revision, _ = self.staged_measured_batch('2')
        self.session.state['queuedUpdate']['sourceById']['3'] = 'operator'
        self.session.service_background(Mock(), 24)
        pending = self.session.snapshot()['queuedUpdate']
        self.assertEqual(pending['status'], 'blocked')
        self.assertNotIn('measuredFitMissId', pending)
        RollingQueue().tick(self.session, 24)
        self.assertEqual(self.session.state['queuedUpdate']['id'], revision)

    def test_pending_pins_and_unattributed_fit_failures_remain_held(self):
        for pin in ('2', '3', None):
            with self.subTest(pin=pin):
                original, revision, _ = self.staged_measured_batch('2', pin=pin)
                if pin:
                    self.session.service_background(Mock(), 24)
                else:
                    with patch.object(self.session, 'fit_background_tracks', side_effect=ValueError('Unknown proof error')):
                        self.session.service_background(Mock(), 24)
                state = self.session.snapshot()
                self.assertEqual(state['executionPlan'], original)
                self.assertEqual(state['queuedUpdate']['status'], 'blocked')
                self.assertNotIn('measuredFitMissId', state['queuedUpdate'])
                RollingQueue().tick(self.session, 24)
                self.assertEqual(self.session.state['queuedUpdate']['id'], revision)
                self.assertEqual([track['id'] for track in self.session.state['queuedUpdate']['tracks']], ['0','1','2','3'])

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
        self.session.discover_more=Mock(side_effect=lambda _, **__: self.session.update(
            phase='discovering',catalogSearch={'status':'searching'}))
        deadline=agent.deadline
        result=agent.message('Play Daft Punk',source='voice',message_id='voice-fresh',approved=True)
        self.session.discover_more.assert_called_once_with('daft punk',opening_query='daft punk')
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

    def test_voice_artist_start_rolls_the_queue_unless_the_request_is_finite(self):
        agent=dj_agent.DJAgent(self.session, evaluator=Mock())
        self.session.start=Mock()
        state=self.session.snapshot()
        request={'status':'preparing','source':'voice','sessionId':state['id'],
                 'revision':agent.state['revision'],'orderIds':[t['id'] for t in state['order']],
                 'expiresAt':agent.clock()+60,'fastOpeningQuery':'daft punk','freshQueue':True}
        for scope in (None,'finite'):
            with self.subTest(scope=scope):
                agent.playback_request={**request,'scope':scope}
                self.session.start.reset_mock()
                self.assertTrue(agent.advance_playback())
                self.assertEqual(self.session.start.call_args.kwargs.get('rolling_queue',False),scope is None)

    def test_beat_grid_opening_receipt_survives_a_rolling_refill(self):
        agent=dj_agent.DJAgent(self.session, evaluator=Mock())
        self.session.update(phase='playing',active=True,startedAt=123,index=0,
                            telemetry={'decks':[{'id':'a','playing':True}]},
                            startup={'mode':'native-beat-grid','status':'ready',
                                     'requestedIds':['a','b'],'playingAt':123})
        request={'status':'starting','source':'voice','sessionId':self.session.snapshot()['id'],
                 'revision':agent.state['revision'],'orderIds':['a','b']}
        agent.playback_request={**request}
        self.assertTrue(agent.advance_playback())
        self.assertEqual(agent.playback_request['status'],'playing')

        self.session.update(startedAt=124)
        agent.playback_request={**request}
        self.assertTrue(agent.advance_playback())
        self.assertEqual(agent.playback_request['status'],'failed')

    def test_voice_title_by_artist_starts_that_recording_with_a_distinct_followup(self):
        self.session.start=Mock()
        self.session.discover_more=Mock(side_effect=lambda _, **__:self.session.update(
            phase='discovering',catalogSearch={'status':'searching'}))
        evaluator=Mock(side_effect=AssertionError('Exact live matches should start without a planning call'))
        agent=dj_agent.DJAgent(self.session,evaluator=evaluator)
        agent.enable(True,seconds=120,requests=5)
        agent.message('Play Around the World by Daft Punk',source='voice',message_id='title-by-artist',approved=True)
        search='around the world by daft punk; daft punk'
        self.session.discover_more.assert_called_once_with(search,opening_query='around the world by daft punk')
        candidates=[{**track('cover'),'title':'Around the World','artist':'Cover Artist'},
                    {**track('requested'),'title':'Around the World','artist':'Daft Punk'},
                    {**track('remix',bpm=125.2),'title':'Around the World','artist':'Daft Punk'},
                    {**track('next',bpm=126),'title':'Digital Love','artist':'Daft Punk'}]
        self.session.update(candidates=candidates,phase='catalog-ready',catalogSearch={
            'status':'complete','query':search,'foundIds':[item['id'] for item in candidates]})
        agent.tick(force=True)
        self.assertEqual([item['id'] for item in self.session.snapshot()['order']],['requested','next'])
        self.session.start.assert_called_once_with(initialize_mixer=True,immediate=True,rolling_queue=True,allow_grid_opening=True,cue_led_opening=True)
        evaluator.assert_not_called()

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
