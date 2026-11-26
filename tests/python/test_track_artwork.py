import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.request import urlopen
from unittest.mock import Mock, patch

from afterhours.track_artwork import TrackArtwork, beatport_cover, recording_id
from afterhours.vdj_catalog import identity


XML_COVER='https://geo-media.beatport.com/image_size/1400x1400/11111111-aaaa-aaaa-aaaa-111111111111.jpg'
CACHE_COVER='https://geo-media.beatport.com/image_size/500x500/22222222-bbbb-bbbb-bbbb-222222222222.jpg'
APPLE_COVER='https://is1-ssl.mzstatic.com/image/thumb/Music125/v4/11/22/33/fixture.jpg/800x800bb.jpg'


class TrackArtworkTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.art=TrackArtwork(self.root);self.addCleanup(self.art.close)
    def xml(self,rows):
        body=''.join(f'<Song FilePath="{path}"><Tags Title="Same title"/><Link Cover="{url}"/></Song>' for path,url in rows)
        (self.root/'database.xml').write_text('<VirtualDJ_Database>'+body+'</VirtualDJ_Database>')
    def cache(self,rows):
        path=self.root/'Cache/Beatport/123/sqlite3.db';path.parent.mkdir(parents=True,exist_ok=True)
        db=sqlite3.connect(path)
        try:
            db.execute('CREATE TABLE tracks (id INTEGER, album_artwork TEXT)')
            db.executemany('INSERT INTO tracks VALUES (?,?)',rows);db.commit()
        finally:db.close()
        return path
    def test_cover_uses_exact_recording_identity_not_title(self):
        self.xml([('netsearch://bp123',XML_COVER),('netsearch://va123',CACHE_COVER),('/local/song.mp3',CACHE_COVER)])
        self.assertEqual(self.art.refresh(),1)
        sid='virtualdj:'+identity('netsearch://bp123')
        self.assertEqual(recording_id(123),sid)
        self.assertEqual(self.art.lookup(sid)['url'],XML_COVER)
        self.assertIsNone(self.art.lookup(recording_id(124)))
        self.assertIsNone(self.art.lookup('virtualdj:'+identity('netsearch://va123')))
    def test_apple_and_beatport_art_share_an_opaque_url_contract(self):
        self.xml([('netsearch://ap123', APPLE_COVER), ('netsearch://bp123', XML_COVER),
                  ('netsearch://bp124', APPLE_COVER), ('netsearch://ap124', XML_COVER)])
        self.assertEqual(self.art.refresh(), 2)
        apple = 'virtualdj:' + identity('netsearch://ap123')
        beatport = recording_id(123)
        self.assertNotEqual(apple, beatport)
        result = self.art.decorate({'library': [{'id': apple}], 'nextTrack': {'id': beatport}})
        self.assertEqual(self.art.lookup(apple)['provider'], 'apple-music')
        self.assertEqual(result['library'][0]['artworkPath'], '/api/track-artwork/'+identity('netsearch://ap123'))
        self.assertEqual(result['nextTrack']['artworkPath'], '/api/track-artwork/'+identity('netsearch://bp123'))

    def test_fetch_only_serves_bounded_image_payloads_for_an_exact_id(self):
        self.xml([('netsearch://ap123', APPLE_COVER)])
        self.art.refresh()
        opener = Mock()
        response = Mock()
        response.headers.get_content_type.return_value = 'image/jpeg'
        response.read.return_value = b'\xff\xd8\xffvalid'
        from contextlib import nullcontext
        opener.open.return_value = nullcontext(response)
        apple = 'virtualdj:' + identity('netsearch://ap123')
        self.assertEqual(self.art.fetch(apple, opener), ('image/jpeg', b'\xff\xd8\xffvalid'))
        self.assertEqual(opener.open.call_args.args[0].full_url, APPLE_COVER)
        response.read.return_value = b'0' * 2_000_001
        self.assertIsNone(self.art.fetch(apple, opener))
        response.headers.get_content_type.return_value = 'text/html'
        self.assertIsNone(self.art.fetch(apple, opener))
        opener.reset_mock()
        self.assertIsNone(self.art.fetch(recording_id(123), opener))
        opener.open.assert_not_called()

    def test_provider_cache_preferred_and_read_only(self):
        self.xml([('netsearch://bp123',XML_COVER)])
        path=self.cache([(123,CACHE_COVER),(124,None),(125,'https://unrelated.example/cover.jpg')])
        before=path.read_bytes();self.assertEqual(self.art.refresh(),1)
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual(self.art.lookup(recording_id(123)),{'url':CACHE_COVER,'provider':'beatport','trackId':'123','source':'virtualdj.beatport-cache'})
        # Connections must be closed; the next refresh can see replacement data.
        path.unlink();self.assertEqual(self.art.refresh(),1)
        self.assertEqual(self.art.lookup(recording_id(123))['url'],XML_COVER)
    def test_invalid_art_leaves_no_artwork_and_no_file_fallback(self):
        values=[None,'','file:///private/secret','http://geo-media.beatport.com/image_size/1x1/abc.jpg',
                'https://geo-media.beatport.com.evil.example/image_size/1x1/abc.jpg',
                'https://user:secret@geo-media.beatport.com/image_size/1x1/abc.jpg',
                'https://geo-media.beatport.com:444/image_size/1x1/abc.jpg',XML_COVER+'?token=secret',
                XML_COVER+'#fragment','https://geo-media.beatport.com/../secret.jpg','x'*2049]
        for value in values:
            with self.subTest(value=value):self.assertIsNone(beatport_cover(value))
        self.assertEqual(beatport_cover(XML_COVER),XML_COVER)
    def test_missing_malformed_or_locked_metadata_does_not_break_snapshot(self):
        self.assertEqual(self.art.refresh(),0)
        (self.root/'database.xml').write_text('<broken')
        path=self.cache([(123,CACHE_COVER)]);path.write_bytes(b'not sqlite')
        self.assertEqual(self.art.refresh(),0)
        state={'order':[{'id':recording_id(123),'title':'Title'}]}
        self.assertEqual(self.art.decorate(state),state)
    def test_decorates_all_visible_roles_without_io_or_mutation(self):
        self.cache([(123,CACHE_COVER)]);self.art.refresh();sid=recording_id(123)
        state={'order':[{'id':sid}], 'candidates':[{'id':sid}],
               'telemetry':{'decks':[{'id':sid,'deck':1}]},
               'backgroundAnalysis':{'decks':[{'trackId':sid,'deck':3}]}}
        before=copy.deepcopy(state)
        with patch('afterhours.track_artwork.sqlite3.connect',side_effect=AssertionError('disk access on snapshot')), \
                patch('afterhours.track_artwork.ET.iterparse',side_effect=AssertionError('disk access on snapshot')):
            result=self.art.decorate(state)
        self.assertEqual(state,before)
        for row in [result['order'][0],result['candidates'][0],result['telemetry']['decks'][0],result['backgroundAnalysis']['decks'][0]]:
            self.assertEqual(row['artworkUrl'],CACHE_COVER);self.assertEqual(row['artwork']['trackId'],'123')
        result['order'][0]['artwork']['url']='modified'
        self.assertEqual(self.art.lookup(sid)['url'],CACHE_COVER)
    def test_removal_does_not_leave_stale_art(self):
        self.xml([('netsearch://bp123',XML_COVER)]);self.art.refresh()
        self.xml([]);self.art.refresh();self.assertIsNone(self.art.lookup(recording_id(123)))
    def test_refresh_runs_off_caller_and_duplicate_start_is_idempotent(self):
        entered=threading.Event();release=threading.Event();thread_ids=[]
        def refresh():
            thread_ids.append(threading.get_ident());entered.set();release.wait(2)
        with patch.object(self.art,'refresh',side_effect=refresh):
            try:
                self.art.start();self.art.start();self.assertTrue(entered.wait(1))
                self.assertEqual(self.art.decorate({'order':[]}),{'order':[]})
                self.assertNotEqual(thread_ids,[threading.get_ident()]);self.assertEqual(len(thread_ids),1)
            finally:release.set();self.art.close()
        self.assertFalse(self.art.worker.is_alive())
    def test_http_read_supplies_art_without_playback_calls(self):
        from afterhours import server
        self.xml([('netsearch://bp123',XML_COVER)]);self.art.refresh()
        state={'order':[{'id':recording_id(123)}],'phase':'idle','active':False}
        httpd=ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        worker=threading.Thread(target=httpd.serve_forever,daemon=True);worker.start()
        try:
            with patch.object(server,'TRACK_ARTWORK',self.art),patch.object(server.DJ_SESSION,'snapshot',return_value=state), \
                    patch.object(server,'vdj_request',side_effect=AssertionError('audio command on artwork read')):
                with urlopen(f'http://127.0.0.1:{httpd.server_port}/api/set-session',timeout=2) as response:
                    result=json.load(response)
                with urlopen(f'http://127.0.0.1:{httpd.server_port}/api/set-session?view=live',timeout=2) as response:
                    compact=json.load(response)
                self.assertEqual(result['order'][0]['artworkPath'], compact['order'][0]['artworkPath'])
                with patch.object(self.art,'fetch',return_value=('image/jpeg',b'fixture')) as fetch:
                    with urlopen(f'http://127.0.0.1:{httpd.server_port}'+result['order'][0]['artworkPath'],timeout=2) as response:
                        self.assertEqual(response.headers.get_content_type(),'image/jpeg')
                        self.assertEqual(response.read(), b'fixture')
                    fetch.assert_called_once_with(recording_id(123))
                self.assertEqual(result['order'][0]['artworkUrl'],XML_COVER)
                self.assertFalse(result['active']);self.assertEqual(state['order'],[{'id':recording_id(123)}])
        finally:httpd.shutdown();httpd.server_close();worker.join(2)


if __name__=='__main__':unittest.main()
