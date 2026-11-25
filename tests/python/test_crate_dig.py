"""Autonomous crate digging with a fake browser; never loads or plays audio.

The digger walks the catalog on its own while a set plays: it searches for the
artists, labels and genres already in the crate, reads every row it is shown,
and stores each recording in the persistent crate. What it finds gives it the
next names to follow.
"""
import hashlib
import threading
import unittest
from unittest.mock import patch

from afterhours.record_crate import RecordCrate, WORKING_LIMIT
from afterhours.vdj_discovery import CrateDig
from afterhours.vdj_session import DJSession

FOLDER = {'id': 'a' * 64}


def track(i, **extra):
    digest = hashlib.sha256(f'dig:{i}'.encode()).hexdigest()
    return {'id': 'virtualdj:' + digest, 'title': f'Record {i}', 'artist': f'Artist {i % 7}', 'label': f'Label {i % 3}',
            'bpm': 124 + i % 4, 'camelot': '8A', 'genre': 'House' if i % 2 else 'Tech House',
            'engine': {'name': 'virtualdj', 'selection_id': digest, 'folder_id': 'a' * 64}, **extra}


def row(i, **extra):
    record = track(i, **extra)
    return {'id': record['engine']['selection_id'], 'artist': record['artist'], 'metadata': record}


class Browser:
    """A catalog whose search results depend on the term last searched."""
    def __init__(self, results):
        self.results = results; self.term = None; self.position = 0; self.sent = []; self.slow = False

    def send(self, client, script):
        self.sent.append(script)
        if script.startswith('search '):
            self.term = script[len('search '):].strip("'\"").replace('/', ' '); self.position = 0
        elif script.endswith("browser_scroll 'top'"):
            self.position = 0
        elif script == 'browser_scroll +1':
            self.position += 1

    def selection(self, client, details=False):
        rows = self.results.get(self.term, [])
        self.reads = getattr(self, 'reads', 0) + 1
        # Like the real browser: nothing is selected at the top of a fresh list.
        if self.slow and self.position == 0:
            return None
        index = self.position - (1 if self.slow else 0)
        return rows[min(index, len(rows) - 1)] if rows else None


class CrateTermTests(unittest.TestCase):
    def test_names_to_follow_come_from_the_crate_most_common_first(self):
        crate = RecordCrate()
        for i in range(21):
            crate.upsert(track(i), source='discovery')
        terms = crate.dig_candidates(limit=40)
        self.assertEqual(terms[0], 'Artist 0')           # three records, more than any other artist
        self.assertIn('Label 0', terms); self.assertIn('Tech House', terms)
        self.assertEqual(len(terms), len(set(terms)))
        self.assertLess(terms.index('Artist 6'), terms.index('Label 0'))

    def test_collaborations_are_followed_as_separate_artists(self):
        crate = RecordCrate()
        crate.upsert(track(1, artist='Miss Kittin, Antonelli'), source='discovery')
        crate.upsert(track(2, artist='Oxia & Miss Kittin'), source='discovery')
        terms = crate.dig_candidates(limit=10)
        self.assertEqual(terms[0], 'Miss Kittin')
        self.assertIn('Antonelli', terms); self.assertIn('Oxia', terms)

    def test_a_name_already_dug_is_not_repeated_until_it_is_stale(self):
        now = [1000.0]; crate = RecordCrate(clock=lambda: now[0])
        for i in range(8):
            crate.upsert(track(i), source='discovery')
        first = crate.dig_candidates(limit=3)
        for term in first:
            crate.mark_dug(term, examined=100, added=40)
        again = crate.dig_candidates(limit=40)
        self.assertFalse(set(first) & set(again))
        now[0] += RecordCrate.DIG_AGAIN_SECONDS + 1
        self.assertTrue(set(first) <= set(crate.dig_candidates(limit=40)))
        self.assertEqual(crate.dig_summary()['termsDug'], 3)

    def test_unusable_names_are_never_proposed(self):
        crate = RecordCrate()
        crate.upsert(track(1, artist='AC/DC "Live"', label='x' * 200, genre=''), source='discovery')
        crate.upsert(track(2, artist='A', label=None), source='discovery')
        for term in crate.dig_candidates(limit=20):
            self.assertTrue(2 <= len(term) <= 80, term)
            self.assertFalse(any(c in term for c in '"`/\\*?[]{}#'), term)


class CrateDigTests(unittest.TestCase):
    def session(self):
        s = DJSession(threading.Lock())
        s.program = lambda: {'connected': False}
        s.folder = dict(FOLDER)
        return s

    def run_dig(self, s, browser, steps, now=None):
        now = now or [0.0]
        job = CrateDig(s, clock=lambda: now[0])
        with patch('afterhours.vdj_discovery.folder_state', return_value=dict(FOLDER)), \
             patch('afterhours.vdj_discovery.selection', side_effect=browser.selection), patch.object(s, 'send', side_effect=browser.send):
            for _ in range(steps):
                if job.done:
                    break
                job.step(None); now[0] += 0.2
        return job

    def test_every_row_is_stored_in_the_crate_without_touching_the_working_set(self):
        s = self.session(); seeds = [track(i) for i in range(4)]
        s.update(candidates=seeds, order=seeds[:2], index=0)
        for record in seeds:
            s.crate.upsert(record, source='discovery')
        results = {'Artist 0': [row(i) for i in range(100, 160)]}
        browser = Browser(results)
        job = self.run_dig(s, browser, steps=80)
        self.assertEqual(s.crate.summary()['count'], 4 + 60)
        state = s.snapshot()
        self.assertEqual([t['id'] for t in state['candidates']], [t['id'] for t in seeds])
        self.assertEqual([t['id'] for t in state['order']], [t['id'] for t in seeds[:2]])
        self.assertTrue(all(script.startswith(('search ', 'browser_')) for script in browser.sent))
        self.assertGreaterEqual(job.added, 60)

    def test_what_it_finds_gives_it_the_next_names_to_follow(self):
        s = self.session(); seed = track(1, artist='Seed Artist', label='Seed Label', genre='House')
        s.update(candidates=[seed], order=[seed], index=0); s.crate.upsert(seed, source='discovery')
        results = {'Seed Artist': [row(i, artist='New Friend', label='Seed Label') for i in range(200, 206)],
                   'New Friend': [row(i, artist='Further Out', label='Deep Label') for i in range(300, 305)],
                   'Further Out': [row(i, artist='Further Out', label='Deep Label') for i in range(400, 403)]}
        browser = Browser(results)
        self.run_dig(s, browser, steps=400)
        searched = [script for script in browser.sent if script.startswith('search ')]
        order = [next(i for i, script in enumerate(searched) if name.replace(' ', '/') in script or name in script)
                 for name in ('Seed Artist', 'New Friend', 'Further Out')]
        self.assertEqual(order, sorted(order))
        self.assertEqual(s.crate.summary()['count'], 1 + 6 + 5 + 3)

    def test_a_term_ends_at_its_row_limit_or_when_the_list_stops_moving(self):
        s = self.session(); seed = track(1, artist='Big Artist')
        s.update(candidates=[seed], order=[seed], index=0); s.crate.upsert(seed, source='discovery')
        browser = Browser({'Big Artist': [row(i, artist='Big Artist') for i in range(500, 800)]})
        job = self.run_dig(s, browser, steps=CrateDig.MAX_ROWS + 12)
        self.assertEqual(s.crate.summary()['count'], 1 + CrateDig.MAX_ROWS)
        self.assertEqual(s.crate.dig_summary()['termsDug'], 1)
        short = self.session(); short.update(candidates=[seed], order=[seed], index=0); short.crate.upsert(seed, source='discovery')
        self.run_dig(short, Browser({'Big Artist': [row(i, artist='Big Artist') for i in range(900, 903)]}), steps=30)
        self.assertEqual(short.crate.summary()['count'], 1 + 3)
        self.assertGreaterEqual(short.crate.dig_summary()['termsDug'], 1)

    def test_an_unselected_first_read_does_not_end_the_name(self):
        # First live run: every name was abandoned after one read that returned nothing.
        s = self.session(); seed = track(1, artist='Seed Artist'); s.update(candidates=[seed], order=[seed], index=0)
        s.crate.upsert(seed, source='discovery')
        browser = Browser({'Seed Artist': [row(i, artist='Seed Artist') for i in range(600, 620)]}); browser.slow = True
        self.run_dig(s, browser, steps=45)
        self.assertEqual(s.crate.summary()['count'], 1 + 20)

    def test_a_name_that_showed_nothing_is_tried_again_within_the_hour_not_the_week(self):
        now = [1000.0]; s = self.session(); s.crate.clock = lambda: now[0]
        seed = track(1, artist='Quiet Artist', label=None, genre=None); s.update(candidates=[seed], order=[seed], index=0)
        s.crate.upsert(seed, source='discovery')
        self.run_dig(s, Browser({}), steps=CrateDig.EMPTY_READS + 12)
        self.assertEqual(s.crate.dig_summary()['termsDug'], 0)
        self.assertNotIn('Quiet Artist', s.crate.dig_candidates(limit=10))
        now[0] += RecordCrate.DIG_EMPTY_AGAIN_SECONDS + 1
        self.assertIn('Quiet Artist', s.crate.dig_candidates(limit=10))

    def test_it_stops_quietly_when_the_browser_leaves_the_bound_folder(self):
        s = self.session(); seed = track(1); s.update(candidates=[seed], order=[seed], index=0); s.crate.upsert(seed, source='discovery')
        job = CrateDig(s, clock=lambda: 0.0)
        with patch('afterhours.vdj_discovery.folder_state', return_value={'id': 'b' * 64}), patch.object(s, 'send') as send:
            for _ in range(5):
                job.step(None)
        self.assertTrue(job.done); send.assert_not_called()
        self.assertEqual(s.snapshot()['crateDig']['status'], 'paused')
        self.assertEqual(s.crate.summary()['count'], 1)

    def test_without_a_bound_folder_or_any_name_it_does_nothing(self):
        empty = self.session(); job = CrateDig(empty, clock=lambda: 0.0)
        with patch.object(empty, 'send') as send:
            job.step(None)
        self.assertTrue(job.done); send.assert_not_called()
        unbound = self.session(); unbound.folder = None; unbound.crate.upsert(track(1), source='discovery')
        job = CrateDig(unbound, clock=lambda: 0.0)
        with patch.object(unbound, 'send') as send:
            job.step(None)
        self.assertTrue(job.done); send.assert_not_called()

    def test_unavailable_recordings_are_not_stored_as_new_finds(self):
        s = self.session(); seed = track(1, artist='Seed Artist'); s.update(candidates=[seed], order=[seed], index=0)
        s.crate.upsert(seed, source='discovery')
        blocked = row(700, artist='Seed Artist')
        with patch.object(s.availability, 'blocked', side_effect=lambda tid: {'code': 'x', 'reason': 'no'} if tid == blocked['metadata']['id'] else None):
            self.run_dig(s, Browser({'Seed Artist': [blocked, row(701, artist='Seed Artist')]}), steps=20)
        self.assertIsNone(s.crate.get(blocked['metadata']['id']))
        self.assertIsNotNone(s.crate.get(track(701)['id']))

    def test_progress_is_published_for_the_app(self):
        s = self.session(); seed = track(1, artist='Seed Artist'); s.update(candidates=[seed], order=[seed], index=0)
        s.crate.upsert(seed, source='discovery')
        self.run_dig(s, Browser({'Seed Artist': [row(i, artist='Seed Artist') for i in range(800, 830)]}), steps=25)
        dig = s.snapshot()['crateDig']
        self.assertEqual(dig['status'], 'digging'); self.assertEqual(dig['term'], 'Seed Artist')
        self.assertGreater(dig['added'], 0); self.assertGreater(dig['examined'], 0)
        self.assertTrue(1 < dig['crateCount'] <= s.crate.summary()['count'])


class SessionDigTests(unittest.TestCase):
    def session(self):
        s = DJSession(threading.Lock()); s.program = lambda: {'connected': False}; s.folder = dict(FOLDER); s.dig_allowed = True
        seed = track(1, artist='Seed Artist'); s.update(candidates=[seed], order=[seed], index=0); s.crate.upsert(seed, source='discovery')
        # Each look at the clock is 0.2 s later, like one pass of the playing loop.
        ticks = iter(range(10**9)); s.dig_clock = lambda: next(ticks) * 0.2
        return s

    def test_digging_runs_only_in_a_safe_gap_and_never_beside_a_search_or_analysis(self):
        s = self.session(); browser = Browser({'Seed Artist': [row(i, artist='Seed Artist') for i in range(10)]})
        with patch('afterhours.vdj_discovery.folder_state', return_value=dict(FOLDER)), \
             patch('afterhours.vdj_discovery.selection', side_effect=browser.selection), patch.object(s, 'send', side_effect=browser.send):
            s.service_dig(None, 8)
            self.assertEqual(browser.sent, [])                       # too close to a handoff
            s.background_job = object(); s.service_dig(None, 60)
            self.assertEqual(browser.sent, [])                       # a spare deck is analysing
            s.background_job = None
            class Busy:
                done = False
            s.catalog_job = Busy(); s.service_dig(None, 60)
            self.assertEqual(browser.sent, [])                       # the planner's own search has the browser
            s.catalog_job = None
            for _ in range(40):
                s.service_dig(None, 60)
        self.assertTrue(browser.sent[0].startswith('search '))
        self.assertGreater(s.crate.summary()['count'], 1)

    def test_after_another_job_used_the_browser_the_dig_searches_its_term_again(self):
        s = self.session(); browser = Browser({'Seed Artist': [row(i, artist='Seed Artist') for i in range(40)]})
        with patch('afterhours.vdj_discovery.folder_state', return_value=dict(FOLDER)), \
             patch('afterhours.vdj_discovery.selection', side_effect=browser.selection), patch.object(s, 'send', side_effect=browser.send):
            for _ in range(30):
                s.service_dig(None, 60)
            searches = sum(1 for script in browser.sent if script.startswith('search '))
            s.dig_interrupted()
            for _ in range(4):
                s.service_dig(None, 60)
        self.assertEqual(sum(1 for script in browser.sent if script.startswith('search ')), searches + 1)

    def test_working_set_stays_bounded_while_the_crate_grows(self):
        s = self.session(); browser = Browser({'Seed Artist': [row(i, artist='Seed Artist') for i in range(1000, 1090)]})
        with patch('afterhours.vdj_discovery.folder_state', return_value=dict(FOLDER)), \
             patch('afterhours.vdj_discovery.selection', side_effect=browser.selection), patch.object(s, 'send', side_effect=browser.send):
            for _ in range(200):
                s.service_dig(None, 60)
        self.assertGreaterEqual(s.crate.summary()['count'], 80)
        self.assertLessEqual(len(s.snapshot()['candidates']), WORKING_LIMIT)
        self.assertEqual(len(s.snapshot()['candidates']), 1)


class DigSafetyTests(unittest.TestCase):
    """Third review of #86: nothing the digger does may reach the playing set."""
    def session(self):
        return SessionDigTests.session(self)

    def patched(self, s, browser):
        return (patch('afterhours.vdj_discovery.folder_state', return_value=dict(FOLDER)),
                patch('afterhours.vdj_discovery.selection', side_effect=browser.selection), patch.object(s, 'send', side_effect=browser.send))

    def serve(self, s, browser, passes=40):
        a, b, c = self.patched(s, browser)
        with a, b, c:
            for _ in range(passes):
                s.service_dig(None, 60)

    def test_a_storage_fault_while_storing_a_row_ends_the_dig_not_the_set(self):
        import sqlite3
        s = self.session(); browser = Browser({'Seed Artist': [row(i, artist='Seed Artist') for i in range(10)]})
        with patch.object(s.crate, 'upsert', side_effect=sqlite3.OperationalError('database or disk is full')):
            self.serve(s, browser)
        self.assertEqual(s.snapshot()['crateDig']['status'], 'paused')
        self.assertNotIn('disk', s.snapshot()['crateDig']['message'])      # never interpreter text

    def test_a_storage_fault_while_choosing_names_ends_the_dig_not_the_set(self):
        import sqlite3
        s = self.session(); browser = Browser({})
        with patch.object(s.crate, 'dig_candidates', side_effect=sqlite3.OperationalError('locked')):
            self.serve(s, browser, passes=3)
        self.assertEqual(s.snapshot()['crateDig']['status'], 'paused')
        self.assertEqual(browser.sent, [])

    def test_a_malformed_row_ends_the_dig_not_the_set(self):
        s = self.session(); browser = Browser({'Seed Artist': [{'id': 'x', 'metadata': {'title': 'No identity'}}]})
        self.serve(s, browser)
        self.assertEqual(s.snapshot()['crateDig']['status'], 'paused')

    def test_a_name_whose_row_cannot_be_stored_is_not_searched_again_every_minute(self):
        s = self.session(); browser = Browser({'Seed Artist': [row(i, artist='Seed Artist') for i in range(10)]})
        with patch.object(s.crate, 'upsert', side_effect=ValueError('Metadata is too large.')):
            self.serve(s, browser, passes=2000)                              # 400 s of playing-loop passes
        self.assertEqual(sum(1 for script in browser.sent if script.startswith('search ') and 'Seed' in script), 1)

    def test_one_bridge_timeout_does_not_shelve_a_name_for_a_week(self):
        from afterhours.dj_bridge import BridgeError
        now = [1000.0]; s = self.session(); s.crate.clock = lambda: now[0]
        browser = Browser({'Seed Artist': [row(i, artist='Seed Artist') for i in range(30)]}); reads = [0]
        def flaky(client, details=False):
            reads[0] += 1
            if reads[0] == 4:
                raise OSError('timed out')
            return browser.selection(client, details)
        a, b, c = self.patched(s, browser)
        with a, patch('afterhours.vdj_discovery.selection', side_effect=flaky), c:
            for _ in range(20):
                s.service_dig(None, 60)
            self.assertEqual(s.snapshot()['crateDig']['status'], 'paused')
            now[0] += RecordCrate.DIG_EMPTY_AGAIN_SECONDS + 1
            self.assertIn('Seed Artist', s.crate.dig_candidates(limit=40))     # back within the hour, not the week
            self.assertEqual(s.crate.dig_summary()['termsDug'], 0)

    def test_a_session_without_a_planner_does_not_dig_until_told_it_may(self):
        s = DJSession(threading.Lock())
        self.assertFalse(s.dig_allowed)

    def test_recalled_recordings_come_before_the_ones_merely_kept(self):
        s = DJSession(threading.Lock()); rows = [track(i) for i in range(WORKING_LIMIT)]
        s.update(candidates=rows, order=rows[:40], index=0)
        gems = [track(i) for i in range(2000, 2004)]
        for gem in gems:
            s.crate.upsert(gem, source='dig', query='Artist', position=1)
        s.hydrate_records([gem['id'] for gem in gems], keep=[r['id'] for r in rows[40:48]])
        held = {t['id'] for t in s.snapshot()['candidates']}
        self.assertTrue({gem['id'] for gem in gems} <= held)

    def test_digging_stops_when_the_operator_turned_discovery_off(self):
        s = self.session(); browser = Browser({'Seed Artist': [row(i, artist='Seed Artist') for i in range(10)]})
        s.dig_allowed = False
        self.serve(s, browser)
        self.assertEqual(browser.sent, []); self.assertEqual(s.crate.summary()['count'], 1)
        s.dig_allowed = True
        self.serve(s, browser)
        self.assertGreater(s.crate.summary()['count'], 1)

    def test_turning_discovery_off_stops_a_dig_that_is_running(self):
        s = self.session(); browser = Browser({'Seed Artist': [row(i, artist='Seed Artist') for i in range(60)]})
        self.serve(s, browser, passes=15); sent = len(browser.sent)
        s.dig_allowed = False
        self.serve(s, browser, passes=15)
        self.assertEqual(len(browser.sent), sent)
        self.assertEqual(s.snapshot()['crateDig']['status'], 'off')

    def test_the_crate_stops_growing_at_its_limit(self):
        s = self.session(); browser = Browser({'Seed Artist': [row(i, artist='Seed Artist') for i in range(1000, 1060)]})
        with patch.object(CrateDig, 'MAX_CRATE', 12):
            self.serve(s, browser, passes=200)
        self.assertEqual(s.crate.summary()['count'], 12)
        self.assertEqual(s.snapshot()['crateDig']['status'], 'idle')
        self.assertIn('full', s.snapshot()['crateDig']['message'])

    def test_a_dig_never_replaces_where_a_recording_was_found(self):
        crate = RecordCrate(); record = track(5)
        crate.upsert(record, source='discovery', query='Seed Artist exact title', position=2)
        crate.upsert(record, source='dig', query='House', position=95)
        self.assertEqual(crate.locator(record['id']), {'query': 'Seed Artist exact title', 'position': 2})
        fresh = track(6); crate.upsert(fresh, source='dig', query='House', position=7)
        self.assertEqual(crate.locator(fresh['id']), {'query': 'House', 'position': 7})

    def test_seeing_a_known_recording_again_does_not_count_as_a_crate_change(self):
        crate = RecordCrate(); record = track(5)
        crate.upsert(record, source='discovery', query='first', position=2)
        before = crate.summary()['revision']
        for term in ('House', 'Artist 5', 'Label 2'):
            crate.upsert(record, source='dig', query=term, position=40)
        self.assertEqual(crate.summary()['revision'], before)

    def test_recalling_a_recording_never_evicts_one_the_caller_still_needs(self):
        s = DJSession(threading.Lock()); rows = [track(i) for i in range(WORKING_LIMIT)]
        s.update(candidates=rows, order=rows[:2], index=0)
        gems = [track(i) for i in range(2000, 2003)]
        for gem in gems:
            s.crate.upsert(gem, source='dig', query='Artist', position=1)
        needed = [rows[2]['id'], rows[3]['id'], rows[4]['id']]
        s.hydrate_records([gem['id'] for gem in gems], keep=needed)
        held = {t['id'] for t in s.snapshot()['candidates']}
        self.assertTrue(set(needed) <= held); self.assertTrue({gem['id'] for gem in gems} <= held)
        self.assertLessEqual(len(held), WORKING_LIMIT)

    def test_a_new_playing_loop_searches_its_name_again(self):
        s = self.session(); browser = Browser({'Seed Artist': [row(i, artist='Seed Artist') for i in range(40)]})
        self.serve(s, browser, passes=30)
        searches = sum(1 for script in browser.sent if script.startswith('search '))
        s.dig_restart()                                                      # stop, takeover or a new run
        self.serve(s, browser, passes=4)
        self.assertEqual(sum(1 for script in browser.sent if script.startswith('search ')), searches + 1)


if __name__ == '__main__':
    unittest.main()
