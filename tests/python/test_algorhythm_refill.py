"""Preserve queue ownership across asynchronous discovery hints."""

from datetime import datetime, timedelta, timezone
from threading import Event
import unittest
from unittest.mock import Mock, patch

from afterhours.algorhythm_refill import AlgorhythmRefill, linked_apple_recording
from afterhours.rolling_queue import RollingQueue, discovery_style


class AlgorhythmRefillTests(unittest.TestCase):
    def test_activity_reports_each_current_lookup_once(self):
        order = [{'id':'one','title':'Song','artist':'Artist'}]
        state = {'id':'set','startedAt':1}
        activity = Mock()
        worker = AlgorhythmRefill(lambda *_, **kwargs: ({'artist':'Other','title':'Lead'},), enabled=True)
        self.assertFalse(worker.hints(state, order, 'house', (), activity=activity))
        worker.future.result(timeout=2)
        for _ in range(3):
            self.assertTrue(worker.hints(state, order, 'house', (), activity=activity))
        self.assertEqual([call.kwargs['stage'] for call in activity.call_args_list], ['searching','leads'])
        self.assertIn('lead for Afterhours to verify', activity.call_args.args[0])

    def test_apple_reference_requires_the_same_id_on_the_public_track_link(self):
        ref = ('itunes-store','1771106780')
        self.assertEqual(linked_apple_recording(ref, 'https://music.apple.com/us/album/hero/1771106656?i=1771106780&uo=4'),
                         ('apple-music','1771106780'))
        self.assertIsNone(linked_apple_recording(ref, 'https://music.apple.com/us/album/hero/1771106656?i=1770891033'))
        self.assertIsNone(linked_apple_recording(ref, 'https://music.apple.com.invalid/us/song/1771106780'))

    def test_stale_result_is_dropped_when_direction_changes(self):
        started, finish = Event(), Event()
        seen = []

        def lookup(order, style, neighbors, **kwargs):
            seen.append((tuple(row['id'] for row in order), style, neighbors))
            started.set()
            if style == 'house':
                if not finish.wait(3):
                    raise TimeoutError
            return ({'artist':'Artist', 'title':style},)

        order = [{'id':str(i), 'artist':'Artist', 'title':str(i)} for i in range(10)]
        state = {'id':'session', 'startedAt':1}
        worker = AlgorhythmRefill(lookup, enabled=True)
        self.addCleanup(finish.set)
        self.assertFalse(worker.hints(state, order, 'house', ('Related',)))
        self.assertTrue(started.wait(2))
        stale = worker.future
        self.assertFalse(worker.hints(state, order, 'gospel', ()))
        self.assertIs(worker.future, stale)
        finish.set()
        stale.result(timeout=2)
        self.assertFalse(worker.hints(state, order, 'gospel', ()))
        worker.future.result(timeout=2)
        self.assertEqual(worker.hints(state, order, 'gospel', ()), ({'artist':'Artist', 'title':'gospel'},))
        self.assertEqual(seen[0], (tuple(str(i) for i in range(10)), 'house', ('Related',)))

    def test_consumed_leads_rotate_without_forgetting_catalog_history(self):
        now = [0.]
        seen = []

        def lookup(order, style, neighbors, *, memory, excluded):
            seen.append((memory, excluded))
            if excluded:
                self.assertIn('already-searched', memory.searched_artists)
                return ({'artist':'Other','title':'Fresh track'},)
            memory.searched_artists.add('already-searched')
            return ({'artist':'Other','title':'First lead'},)

        worker = AlgorhythmRefill(lookup, enabled=True, clock=lambda: now[0])
        state = {'id':'set','startedAt':1}
        order = [{'id':'opener','artist':'Artist','title':'Opener'}]
        worker.hints(state, order, 'house', ())
        worker.future.result(timeout=2)
        excluded = ('other first lead',)
        self.assertFalse(worker.hints(state, order, 'house', (), searched=excluded))
        self.assertEqual(len(seen), 1)
        now[0] = 21
        self.assertFalse(worker.hints(state, order, 'house', (), searched=excluded))
        worker.future.result(timeout=2)
        self.assertIs(seen[0][0], seen[1][0])
        self.assertEqual(worker.hints(state, order, 'house', (), searched=excluded)[0]['title'], 'Fresh track')

    def test_native_search_resolves_algorhythm_metadata_without_revising_queue(self):
        queue = RollingQueue()
        queue.algorhythm = Mock()
        queue.algorhythm.hints.return_value = ({
            'artist':'Artist Two', 'title':'New Track', 'version':'Extended Mix',
            'public_reference':('itunes-store','123'),
            'source_url':'https://music.apple.com/us/song/123',
        },)
        session = Mock()
        order = [{'id':'opener', 'artist':'Artist One', 'title':'Opener', 'bpm':124, 'genre':'house'}]
        with patch('afterhours.rolling_queue.DISCOVERY') as discovery:
            discovery.hints.return_value = []
            self.assertTrue(queue.search(session, {}, order, 'house', True))
        session.discover_more.assert_called_once_with('Artist Two New Track', bpm_min=124*.90, bpm_max=124*1.11)
        call = session.catalog_job.set_rolling_seed.call_args
        self.assertEqual(call.args, ('Artist Two','New Track'))
        self.assertEqual(call.kwargs['version'], 'Extended Mix')
        self.assertTrue(call.kwargs['exact_credits'])
        self.assertNotIn('catalog_reference', call.kwargs)
        self.assertEqual(call.kwargs['linked_provider_reference'], ('apple-music','123'))
        session.revise_queue.assert_not_called()

    def test_catalog_discovery_keeps_the_opening_genre_unless_explicitly_redirected(self):
        order = [{'genre': 'Christian & Gospel'}, {'genre': 'Deep House'}]
        self.assertEqual(discovery_style({'agentDirection':'play Forrest Frank'}, order), 'christian gospel')
        self.assertEqual(discovery_style({'agentDirection':'move into deep house'}, order), 'deep house')

    def test_genre_lookup_continues_after_a_page_with_no_eligible_leads(self):
        now = [0.0]
        pages = []

        def lookup(order, style, neighbors, *, memory, excluded):
            page = memory.genre_pages.get(style, 0)
            pages.append(page)
            memory.genre_pages[style] = page + 1
            return ({'artist': 'Neighbor', 'title': 'Fresh track'},) if page else ()

        worker = AlgorhythmRefill(lookup, enabled=True, clock=lambda: now[0])
        state = {'id': 'set', 'startedAt': 1}
        order = [{'id': 'opener', 'artist': 'Artist', 'title': 'Opener'}]
        self.assertFalse(worker.hints(state, order, 'house', ()))
        worker.future.result(timeout=2)
        self.assertFalse(worker.hints(state, order, 'house', ()))
        self.assertEqual(pages, [0])
        now[0] = 21
        self.assertFalse(worker.hints(state, order, 'house', ()))
        worker.future.result(timeout=2)
        self.assertEqual(worker.hints(state, order, 'house', ()), ({'artist': 'Neighbor', 'title': 'Fresh track'},))
        self.assertEqual(pages, [0, 1])

    def test_genre_refresh_wakes_a_parked_lookup_without_a_queue_change(self):
        now = [0.0]
        origin = datetime(2026, 11, 28, tzinfo=timezone.utc)
        refresh_at = origin + timedelta(minutes=2)
        calls = []

        def lookup(order, style, neighbors, *, memory, excluded):
            calls.append(now[0])
            if memory.scheduled_lookup_due(origin + timedelta(seconds=now[0])):
                return ({'artist': 'Neighbor', 'title': 'Fresh catalog recording'},)
            memory.genre_pages[style] = None
            memory.genre_refresh_after[style] = refresh_at
            return ()

        worker = AlgorhythmRefill(lookup, enabled=True, clock=lambda: now[0],
                                 wall_clock=lambda: origin + timedelta(seconds=now[0]))
        state = {'id': 'set', 'startedAt': 1}
        order = [{'id': 'opener', 'artist': 'Artist', 'title': 'Opener'}]
        worker.hints(state, order, 'house', ())
        worker.future.result(timeout=2)
        now[0] = 21
        worker.hints(state, order, 'house', ())
        worker.future.result(timeout=2)
        now[0] = 119
        self.assertFalse(worker.hints(state, order, 'house', ()))
        self.assertEqual(calls, [0, 21])
        now[0] = 120
        worker.hints(state, order, 'house', ())
        worker.future.result(timeout=2)
        self.assertEqual(worker.hints(state, order, 'house', ())[0]['title'], 'Fresh catalog recording')
        self.assertEqual(calls, [0, 21, 120])

    def test_provider_error_retries_when_its_backoff_expires(self):
        now = [0.0]
        origin = datetime(2026, 11, 28, tzinfo=timezone.utc)
        calls = []

        def lookup(order, style, neighbors, *, memory, excluded):
            calls.append(now[0])
            if not memory.lookup_retry_after:
                memory.lookup_retry_after['genre:house'] = origin + timedelta(seconds=30)
                raise RuntimeError('Catalog provider unavailable')
            memory.lookup_retry_after.clear()
            return ({'artist': 'Neighbor', 'title': 'Recovered lead'},)

        worker = AlgorhythmRefill(lookup, enabled=True, clock=lambda: now[0],
                                 wall_clock=lambda: origin + timedelta(seconds=now[0]))
        state = {'id': 'set', 'startedAt': 1}
        order = [{'id': 'opener', 'artist': 'Artist', 'title': 'Opener'}]
        worker.hints(state, order, 'house', ())
        with self.assertRaises(RuntimeError):
            worker.future.result(timeout=2)
        now[0] = 29
        self.assertFalse(worker.hints(state, order, 'house', ()))
        self.assertEqual(calls, [0])
        now[0] = 30
        worker.hints(state, order, 'house', ())
        worker.future.result(timeout=2)
        self.assertEqual(worker.hints(state, order, 'house', ())[0]['title'], 'Recovered lead')
        self.assertEqual(calls, [0, 30])
