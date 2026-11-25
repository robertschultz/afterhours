"""Library routes use protected queue/load paths; synthetic deck only."""
import threading,unittest
from unittest.mock import Mock,patch
from afterhours.library_actions import library_action
from afterhours.vdj_session import DJSession
from afterhours.dj_bridge import BridgeError

class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.s=DJSession(threading.Lock());self.tracks=[{'id':c,'title':c.upper(),'artist':'Fixture','bpm':125,'camelot':'8A'} for c in 'abcd']
        self.s.update(candidates=self.tracks)
    def test_starter_queue_is_idempotent_and_uses_existing_validation(self):
        self.assertEqual(len(library_action(self.s,{'action':'queue','trackId':'a'})['order']),1)
        self.assertEqual(len(library_action(self.s,{'action':'queue','trackId':'a'})['order']),1)
        with patch.object(self.s,'revise_queue',wraps=self.s.revise_queue) as revise:
            s=library_action(self.s,{'action':'queue','trackId':'b'});revise.assert_called_once()
        self.assertEqual([t['id'] for t in s['order']],['a','b']);self.assertEqual(s['setlist']['sourceById']['b'],'operator')
    def test_invalid_track_and_manual_playback_cannot_replace_set(self):
        with self.assertRaises(ValueError):library_action(self.s,{'action':'queue','trackId':'wrong'})
        self.s.update(telemetry={'decks':[{'playing':True}]})
        with self.assertRaises(ValueError):library_action(self.s,{'action':'queue','trackId':'a'})
        self.assertEqual(self.s.snapshot()['order'],[])
    def test_load_rechecks_actual_deck_and_does_not_play(self):
        self.s.folder='test';self.s.positions={'a':0}
        client=Mock();client.status.return_value={'automix':False,'decks':[{'playing':False},{'playing':True}]}
        with patch.object(self.s,'_launch',side_effect=lambda f,p:f(client)),patch.object(self.s,'load') as load:
            with self.assertRaises(BridgeError):library_action(self.s,{'action':'load','trackId':'a','deck':2})
            load.assert_not_called();client.status.return_value['decks'][1]['playing']=False
            library_action(self.s,{'action':'load','trackId':'a','deck':2});load.assert_called_once_with(client,self.tracks[0],2)
            client.send.assert_not_called()
    def test_scheduled_load_protected_and_search_uses_incremental_catalog(self):
        self.s.update(active=True)
        with patch.object(self.s,'_launch') as launch:
            with self.assertRaises(ValueError):library_action(self.s,{'action':'load','trackId':'a','deck':2})
            launch.assert_not_called()
        with patch.object(self.s,'discover_more',return_value={}) as discover:
            library_action(self.s,{'action':'search','query':'House'});discover.assert_called_once_with('House',bpm_min=40,bpm_max=300,recheck_missing=True)

if __name__=='__main__':unittest.main()
