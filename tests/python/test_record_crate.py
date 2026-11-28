"""Persistent crate/working-pool integration with no provider or native audio."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from afterhours.catalog_browser import CatalogBrowser, context
from afterhours.dj_agent import DJAgent, music_context
from afterhours.dj_bridge import BridgeError
from afterhours.record_crate import RecordCrate, page_parameters, WORKING_LIMIT
from afterhours.vdj_discovery import CatalogSearch
from afterhours.vdj_session import DJSession


def track(i, **extra):
    digest = hashlib.sha256(f'fixture:{i}'.encode()).hexdigest()
    return {'id': 'virtualdj:' + digest, 'title': f'Record {i}', 'artist': f'Artist {i}',
            'bpm': 125, 'camelot': '8A', 'genre': 'House',
            'engine': {'name': 'virtualdj', 'selection_id': digest, 'folder_id': 'a' * 64}, **extra}


class RecordCrateTests(unittest.TestCase):
    def test_persistent_paging_beyond_112_and_exact_id_dedup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'crate.sqlite3'
            crate = RecordCrate(path)
            rows = [track(i) for i in range(180)]
            for row in rows:
                crate.upsert(row, source='discovery', query='House')
            before = crate.summary()
            for row in rows:
                crate.upsert(row, source='discovery', query='House')
            self.assertEqual(crate.summary(), before)
            # Same name does not collapse two distinct engine recordings.
            variant = track(181, title=rows[0]['title'], artist=rows[0]['artist'])
            crate.upsert(variant)
            self.assertEqual(crate.summary()['count'], 181)
            selected = crate.page(5, 28, [rows[0]['id'], rows[179]['id'], rows[0]['id'], 'missing'])
            self.assertEqual((selected['page'], selected['pages'], selected['count']), (5, 7, 181))
            self.assertEqual([r['crateSlot'] for r in selected['records'][:28]], list(range(140, 168)))
            self.assertEqual({r['crateSlot'] for r in selected['records'][28:]}, {0, 179})
            self.assertEqual(len({r['id'] for r in selected['records']}), len(selected['records']))
            revision = crate.summary()['revision']
            crate.close()
            reopened = RecordCrate(path)
            self.assertEqual(reopened.summary(), {'count': 181, 'revision': revision})
            self.assertEqual(reopened.get(rows[0]['id'])['crateSlot'], 0)
            self.assertEqual(reopened.page(999)['page'], 6)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            reopened.close()

    def test_sparse_rediscovery_retains_native_metadata_artwork_and_analysis(self):
        crate = RecordCrate(clock=lambda: 123)
        original = track(1, bpm=125.321, engineChecked=True, album='Actual release',
                         artworkUrl='https://example.com/cover.jpg',
                         artwork={'source': 'virtualdj.database'}, provenance={'bpm': {'source': 'native'}})
        first, inserted = crate.upsert(original, source='analysis', analysis='analyzed')
        sparse = track(1, bpm=124, camelot=None, album=None, artworkUrl=None)
        updated, inserted_again = crate.upsert(sparse, source='discovery', query='Artist 1', position=4)
        self.assertTrue(inserted);self.assertFalse(inserted_again)
        self.assertEqual(updated['crateSlot'], first['crateSlot'])
        self.assertEqual((updated['bpm'], updated['camelot'], updated['album']), (125.321, '8A', 'Actual release'))
        self.assertEqual(updated['artworkUrl'], original['artworkUrl'])
        self.assertEqual(updated['crate']['analysisStatus'], 'analyzed')
        self.assertEqual(updated['crate']['analyzedAt'], 123)
        self.assertEqual(crate.locator(original['id']), {'query': 'Artist 1', 'position': 4})
        self.assertEqual(crate.summary()['count'], 1)

    def test_queries_are_bounded_and_return_exact_archive_matches(self):
        crate = RecordCrate()
        for i in range(240):crate.upsert(track(i))
        special = track(250, title='Around the World', artist='Daft Punk')
        crate.upsert(special)
        self.assertEqual([r['id'] for r in crate.search('Find Daft Punk')], [special['id']])
        self.assertLessEqual(len(crate.search('House')), 3)
        with self.assertRaises(ValueError):crate.page(0, 65)
        with self.assertRaises(ValueError):crate.page(-1)
        with self.assertRaises(ValueError):crate.page(0, 28, ['a'] * 65)
        with self.assertRaises(ValueError):crate.search('x', 1000)
        self.assertEqual(page_parameters({'page': ['2'], 'pageSize': ['28'], 'include': ['a,b', 'c']}), (2, 28, ['a', 'b', 'c']))
        self.assertEqual(RecordCrate().page()['pages'], 1)

    def test_later_artwork_updates_known_records_and_revision_only_once(self):
        crate = RecordCrate();row = track(1);crate.upsert(row)
        art = {'url': 'https://geo-media.beatport.com/image_size/500x500/abcdef.jpg', 'provider': 'beatport',
               'source': 'virtualdj.beatport-cache', 'trackId': '123'}
        index = {row['id']: art, 'unconsidered': art}
        before = crate.summary()['revision']
        self.assertEqual(crate.refresh_artwork(index), 1)
        self.assertEqual(crate.summary(), {'count': 1, 'revision': before + 1})
        self.assertEqual(crate.get(row['id'])['artworkUrl'], art['url'])
        self.assertEqual(crate.refresh_artwork(index), 0)
        self.assertEqual(crate.summary()['revision'], before + 1)
        self.assertIsNone(crate.get('unconsidered'))

    def test_same_artwork_callback_keeps_provider_database_read_only(self):
        from afterhours.track_artwork import TrackArtwork, recording_id
        from test_track_artwork import XML_COVER
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            body = '<VirtualDJ_Database><Song FilePath="netsearch://bp123"><Link Cover="' + XML_COVER + '"/></Song></VirtualDJ_Database>'
            path = root / 'database.xml';path.write_text(body)
            crate = RecordCrate();crate.upsert({'id': recording_id(123), 'title': 'Known'})
            art = TrackArtwork(root, on_refresh=crate.refresh_artwork)
            art.refresh()
            self.assertEqual(crate.get(recording_id(123))['artworkUrl'], XML_COVER)
            self.assertEqual(path.read_text(), body)

    def test_stale_checked_order_cannot_overwrite_new_native_analysis(self):
        now = [100]
        crate = RecordCrate(clock=lambda: now[0])
        s = DJSession(threading.Lock(), crate=crate)
        old = track(1, bpm=125, key='A minor', camelot='8A', engineChecked=True)
        s.update(candidates=[old], order=[old])
        now[0] = 200
        measured = {**old, 'bpm': 125.4, 'key': 'E minor', 'camelot': '9A'}
        s.remember_record(measured, source='analysis', analysis='analyzed')
        s.update(order=[old])
        archive = crate.get(old['id'])
        self.assertEqual((archive['bpm'], archive['key'], archive['camelot']), (125.4, 'E minor', '9A'))
        self.assertEqual(archive['crate']['analyzedAt'], 200)
        # A real subsequent measurement may refine the values again.
        now[0] = 300
        s.remember_record({**measured, 'bpm': 125.6, 'key': None, 'camelot': None}, source='analysis', analysis='analyzed')
        self.assertEqual(crate.get(old['id'])['bpm'], 125.6)
        self.assertIsNone(crate.get(old['id'])['key'])
        self.assertIsNone(crate.get(old['id'])['camelot'])
        self.assertEqual(crate.get(old['id'])['crate']['analyzedAt'], 300)


class CrateSessionTests(unittest.TestCase):
    def session(self):
        s = DJSession(threading.Lock())
        s.program = lambda: {'connected': False}
        return s

    def test_growth_keeps_queue_and_current_bounded_pool_and_prompt(self):
        s = self.session();rows = [track(i) for i in range(200)]
        s.update(candidates=rows[:8], order=rows[:4], queuedUpdate={'tracks': rows[4:8]}, index=0)
        for i, row in enumerate(rows):s.remember_record(row, query='House', position=i)
        state = s.snapshot()
        self.assertEqual(state['crateSummary']['count'], 200)
        self.assertEqual(len(state['candidates']), WORKING_LIMIT)
        self.assertTrue({r['id'] for r in rows[:8]} <= {r['id'] for r in state['candidates']})
        self.assertEqual(state['order'], rows[:4])
        self.assertEqual(state['queuedUpdate']['tracks'], rows[4:8])
        self.assertLessEqual(len(s.positions), WORKING_LIMIT)
        prompt = music_context(state, request='House')
        self.assertLessEqual(len(prompt['catalog']), 32)
        self.assertLess(len(json.dumps(prompt)), 24000)
        self.assertNotIn('records', state['crateSummary'])

    def test_discovery_after_48_keeps_every_new_find_and_no_transport(self):
        s = self.session();s.update(candidates=[track(i) for i in range(48)], order=[track(0), track(1)])
        now = [0.0]
        job = CatalogSearch(s, ['Fresh artist'], '', '', 40, 300, clock=lambda: now[0])
        rows = iter({'id': str(i), 'artist': f'Artist {i}', 'metadata': track(i)} for i in range(48, 80))
        with patch('afterhours.vdj_discovery.folder_state', return_value={'id': 'a' * 64}), \
             patch('afterhours.vdj_discovery.selection', side_effect=lambda *args: next(rows)), patch.object(s, 'send') as send:
            for _ in range(30):
                job.step(None);now[0] += 1
                if job.done:break
        self.assertTrue(job.done)
        self.assertEqual((job.added, job.matched), (6, 6))
        self.assertEqual(s.crate.summary()['count'], 54)
        self.assertEqual(len(s.snapshot()['candidates']), 48)
        self.assertEqual([r['id'] for r in s.snapshot()['order']], [track(0)['id'], track(1)['id']])
        self.assertTrue(all(c.args[1].startswith(('search ', 'browser_')) for c in send.call_args_list))

    def test_expansion_digs_past_known_rows_while_opening_reuses_them_quickly(self):
        known = [track(i, artist='Daft Punk') for i in range(6)]
        for opening, explicit_import, expected in ((False, False, (6, 12)), (True, True, (6, 12)), (True, False, (0, 3))):
            with self.subTest(opening=opening, explicit_import=explicit_import):
                s = self.session();s.update(candidates=known, order=[] if opening else known[:2])
                now = [0.0]
                job = CatalogSearch(s, ['Daft Punk'], '', '', 40, 300, clock=lambda: now[0], recheck_missing=explicit_import)
                rows = iter({'id': str(i), 'artist': 'Daft Punk', 'metadata': track(i, artist='Daft Punk')} for i in range(12))
                with patch('afterhours.vdj_discovery.folder_state', return_value={'id': 'a' * 64}), \
                     patch('afterhours.vdj_discovery.selection', side_effect=lambda *args: next(rows)), patch.object(s, 'send'):
                    for _ in range(30):
                        job.step(None);now[0] += 1
                        if job.done:break
                self.assertTrue(job.done)
                self.assertEqual((job.added, job.examined), expected)
                self.assertEqual(s.crate.summary()['count'], 6 + expected[0])

    def test_archive_search_pick_and_new_set_preserve_library(self):
        s = self.session();rows = [track(i) for i in range(150)]
        s.update(candidates=rows[:2], order=rows[:2], index=-1)
        for row in rows:s.remember_record(row, query=row['artist'])
        requested = track(20, artist='Daft Punk', title='One More Time')
        s.crate.upsert(requested, source='discovery', query='Daft Punk')
        self.assertNotIn(requested['id'], {r['id'] for r in s.snapshot()['candidates']})
        browser = CatalogBrowser(lambda: 100)
        browser.open('Find Daft Punk', 'find', s.snapshot())
        view = browser.snapshot(s, None)
        self.assertEqual(view['availableCount'], 150)
        self.assertEqual(view['search']['ids'], [requested['id']])
        expected = context(s.snapshot(), None)
        with patch.object(s, 'send') as send:
            receipt = browser.play_next(s, requested['id'], 'pick', expected)
            send.assert_not_called()
        self.assertIn(requested['id'], receipt['orderIds'])
        self.assertEqual(s.positions[requested['id']]['query'], 'Daft Punk')
        self.assertEqual(s.crate.summary()['count'], 150)
        s.new_set()
        self.assertEqual(s.crate.summary()['count'], 150)
        self.assertEqual(s.snapshot()['order'], [])

    def test_fresh_set_preserves_recorded_provider_restrictions_in_crate_reads(self):
        session = self.session()
        self.addCleanup(session.crate.close)
        record = track(3, playback_verified=True)
        track_id = record['id']
        session.crate.upsert(record)
        session.availability.record_preview_only(track_id, 'Fixture Music', 30,
            {'source':'operator-confirmed', 'reference':'preview-proof', 'sha256':'a'*64})
        self.assertFalse(session.crate.get(track_id)['playback_verified'])
        first = session.snapshot()['id']
        for _ in range(2):
            fresh = session.new_set()
            self.assertNotEqual(fresh['id'], first)
            self.assertIs(session.crate.availability, session.availability)
            self.assertFalse(session.crate.get(track_id)['playback_verified'])
            self.assertEqual(fresh['recordingAvailability'][0]['code'], 'provider-preview-only')
            first = fresh['id']

    def test_restore_seeds_archive_then_restart_keeps_unselected_records(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'crate.sqlite3'
            first = DJSession(threading.Lock(), crate=RecordCrate(path))
            rows = [track(i) for i in range(60)]
            saved = {'candidates': rows, 'order': rows[:2], 'catalogFolder': {'id': 'a' * 64}}
            first.restore(saved)
            self.assertEqual(first.crate.summary()['count'], 60)
            self.assertEqual(len(first.snapshot()['candidates']), 48)
            first.crate.close()
            second = DJSession(threading.Lock(), crate=RecordCrate(path))
            self.assertEqual(second.snapshot()['crateSummary']['count'], 60)
            self.assertEqual(second.snapshot()['order'], [])
            recalled = second.recall_records('Artist 3')
            self.assertLessEqual(len(recalled), 12)
            self.assertIn(track(3)['id'], {r['id'] for r in recalled})
            second.crate.close()

    def test_failed_and_completed_analysis_both_remain_in_crate(self):
        s = self.session();row = track(1)
        with patch.object(s, 'q', return_value=False), patch.object(s, 'load', side_effect=BridgeError('Fixture failed load')):
            with self.assertRaises(BridgeError):s.analyze_deck(None, row, 3)
        self.assertEqual(s.crate.get(row['id'])['crate']['analysisStatus'], 'attempted')
        def query(client, script, kind=None):
            if script.endswith('loaded'):return False
            if script.endswith(' play'):return False
            if script.endswith('get_bpm absolute'):return 125.125
            if script.endswith('get_key'):return '8A'
            if script.endswith('get_firstbeat'):return -2.494
            if script.endswith('get_time total absolute'):return 264509
            if script.endswith('get_filepath'):return 'fixture:1'
            raise AssertionError(script)
        with patch.object(s, 'q', side_effect=query), patch.object(s, 'load'), patch.object(s, 'send'), \
             patch.object(s, 'wait_observed'), patch.object(s, 'sleep'):
            s.analyze_deck(None, row, 3)
        archived = s.crate.get(row['id'])
        self.assertEqual(archived['crate']['analysisStatus'], 'analyzed')
        self.assertEqual(archived['bpm'], 125.125)

    def test_spare_deck_helper_persists_attempts_to_shared_crate(self):
        from afterhours.background_analysis import BackgroundAnalysis
        s = self.session();row = track(9)
        job = BackgroundAnalysis(s, 'revision', [row], deck=3)
        # Stop at the first mailbox request. The helper has already archived
        # this attempted recording in the owning session's persistent crate.
        with patch.object(job, 'request', side_effect=BridgeError('Fixture mailbox unavailable')):
            job.run()
        self.assertTrue(job.done)
        self.assertEqual(s.crate.get(row['id'])['crate']['analysisStatus'], 'attempted')

    def test_repeated_catalog_snapshot_reuses_results_until_crate_changes(self):
        s = self.session();s.update(candidates=[track(1)])
        browser = CatalogBrowser(lambda: 100);browser.open('Find House', 'query', s.snapshot())
        with patch.object(s.crate, 'search', wraps=s.crate.search) as search:
            for _ in range(20):browser.snapshot(s, None)
            self.assertEqual(search.call_count, 1)
            s.remember_record(track(2))
            browser.snapshot(s, None)
            self.assertEqual(search.call_count, 2)

    def test_conversational_direction_recalls_archive_before_bounded_planning(self):
        s = self.session();s.update(candidates=[track(i) for i in range(48)])
        special = track(200, artist='Daft Punk', title='Aerodynamic')
        s.crate.upsert(special)
        agent = DJAgent(s)
        with patch.object(agent, 'tick'), patch.object(s, 'send') as send:
            agent.message('Add Daft Punk into the queue', message_id='request')
            send.assert_not_called()
        self.assertIn(special['id'], {r['id'] for r in s.snapshot()['candidates']})
        self.assertEqual(s.snapshot()['order'], [])
        self.assertIn(special['id'], music_context(s.snapshot(), request='Daft Punk')['requestedIds'])

    def test_batch_recall_retains_best_existing_match_and_protected_queue(self):
        s = self.session();rows = [track(i) for i in range(48)]
        rows[2] = track(2, artist='Daft Punk', title='Aerodynamic')
        staged = rows[:2] + rows[10:14]
        s.update(candidates=rows, order=rows[:2], queuedUpdate={'tracks': staged}, index=0)
        for i in range(48, 59):s.crate.upsert(track(i, artist='Daft Punk'))
        recalled = s.recall_records('Daft Punk Aerodynamic')
        self.assertEqual(recalled[0]['id'], rows[2]['id'])
        state = s.snapshot();candidates = {r['id'] for r in state['candidates']}
        self.assertEqual(len(candidates), 48)
        self.assertTrue({r['id'] for r in recalled} <= candidates)
        self.assertTrue({r['id'] for r in staged} <= candidates)
        self.assertEqual(state['order'], rows[:2])
        self.assertEqual(state['queuedUpdate']['tracks'], staged)
        supplied = music_context(state, request='Daft Punk Aerodynamic')['catalog']
        self.assertIn(rows[2]['id'], {r['id'] for r in supplied})

    def test_telemetry_and_snapshot_do_not_read_or_write_archive(self):
        s = self.session();s.update(candidates=[track(1)])
        with patch.object(s.crate, 'upsert', side_effect=AssertionError('No archive write')), \
             patch.object(s.crate, 'page', side_effect=AssertionError('No archive page')):
            for _ in range(100):
                s.update(telemetry={'decks': [], 'sampledAt': 10})
                self.assertEqual(s.snapshot()['crateSummary']['count'], 1)
                self.assertNotIn('crateSummary', s.control_state())


class CatalogCrateRouteTests(unittest.TestCase):
    def test_get_requires_local_token_and_bounds_page_with_artwork(self):
        from afterhours import server
        s = DJSession(threading.Lock());rows = [track(i) for i in range(70)]
        s.update(candidates=rows)
        handler = object.__new__(server.Handler)
        handler.headers = {'Host': '127.0.0.1'}
        handler.path = '/api/catalog-crate?page=2&pageSize=28&include=' + rows[0]['id']
        handler.reply = Mock()
        with patch.object(server, 'DJ_SESSION', s):
            handler.do_GET();self.assertEqual(handler.reply.call_args.args[1], 403)
            handler.headers['X-DJ-Token'] = server.TOKEN
            handler.do_GET();response = handler.reply.call_args.args[0]
            self.assertEqual((response['count'], response['page'], response['pages']), (70, 2, 3))
            self.assertEqual(len(response['records']), 15)
            self.assertEqual(response['records'][-1]['id'], rows[0]['id'])
            handler.path = '/api/catalog-crate?pageSize=100000'
            handler.do_GET();self.assertEqual(handler.reply.call_args.args[1], 400)


if __name__ == '__main__':
    unittest.main()
