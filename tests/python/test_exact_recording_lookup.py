"""Bounded catalog relocation without native calls or alternate recordings."""
import threading
import unittest
from unittest.mock import Mock, patch

from afterhours.background_analysis import BackgroundAnalysis
from afterhours.dj_bridge import BridgeError
from afterhours.vdj_catalog import identity, selected_recording_id
from afterhours.vdj_session import DJSession,RecordingLookupIncomplete,RecordingUnavailable


TRACK={'id':'virtualdj:'+identity('saved-house-dawgs'),'title':'House Dawgs','artist':'CamelPhat','bpm':126,
       'engine':{'selection_id':identity('saved-house-dawgs'),'folder_id':identity('original-discovery-folder')}}


class Browser:
    def __init__(self, results):
        self.results=results;self.query='';self.row=0;self.pending=None;self.commands=[];self.reads=0
        self.folder={'id':identity('current-provider-folder')}
        self.count_error=False

    def send(self, client, script):
        self.commands.append(script)
        if script.startswith('search '):self.query=script[8:-1];self.row=0
        elif 'top' in script:self.pending=0
        elif script.startswith('browser_scroll +'):self.pending=self.row+int(script.rsplit('+',1)[1])
        else:raise AssertionError(script)

    def wait(self, seconds):
        if self.pending is not None:
            self.row=min(self.pending,max(0,len(self.results.get(self.query,[]))-1));self.pending=None

    def selected(self, client):
        self.reads+=1
        rows=self.results.get(self.query,[])
        return rows[self.row] if rows else None

    def count(self, client, script, kind=None):
        if script!='file_count':raise AssertionError(script)
        if self.count_error:raise BridgeError('Count unavailable')
        return len(self.results.get(self.query,[]))


class RecordingLookupTests(unittest.TestCase):
    def setup_browser(self, results, position=0):
        session=DJSession(threading.Lock());browser=Browser(results);session.folder=browser.folder.copy()
        session.positions={TRACK['id']:{'query':'CamelPhat','position':position}}
        return session,browser

    def run_lookup(self,session,browser,method='select'):
        with patch('afterhours.vdj_session.folder_state',side_effect=lambda c:browser.folder.copy()), \
             patch('afterhours.vdj_session.selected_recording_id',side_effect=browser.selected), \
             patch.object(session,'send',side_effect=browser.send), \
             patch.object(session,'q',side_effect=browser.count), \
             patch.object(session,'sleep',side_effect=browser.wait), \
             patch.object(session,'wait_observed',side_effect=lambda c,s:browser.wait(s)), \
             patch.object(session,'service_telemetry'), \
             patch('afterhours.vdj_session.paused_load_target',return_value={}), \
             patch.object(session.recordings,'find',return_value=None), \
             patch('afterhours.vdj_session.load_selected',return_value={'verified':True}) as load:
            getattr(session,method)(None,TRACK,*([2] if method=='load' else []))
            return load

    def test_relocation_reads_identity_without_catalog_metadata(self):
        client = Mock()
        client.query.return_value = 'opaque-locator'
        self.assertEqual(selected_recording_id(client), identity('opaque-locator'))
        client.query.assert_called_once_with('get_browsed_filepath', 'text')

    def test_provider_capped_artist_results_use_exact_title_before_long_walk(self):
        wrong_version=identity('another-house-dawgs-mix')
        s,b=self.setup_browser({'CamelPhat':[identity(str(i)) for i in range(100)],
                               'House Dawgs':[wrong_version,TRACK['engine']['selection_id']]})
        load=self.run_lookup(s,b,'load')
        self.assertEqual([c for c in b.commands if c.startswith('search ')],['search "CamelPhat"','search "House Dawgs"'])
        self.assertLess(b.reads,6)
        self.assertEqual(s.positions[TRACK['id']],{'query':'House Dawgs','position':1})
        load.assert_called_once()
        self.assertEqual(load.call_args.args[1:5],(2,TRACK['engine']['selection_id'],s.folder['id'],True))

    def test_saved_cursor_still_loads_directly_without_additional_search(self):
        s,b=self.setup_browser({'CamelPhat':[identity('other'),TRACK['engine']['selection_id']]},position=1)
        self.run_lookup(s,b,'load')
        self.assertEqual([c for c in b.commands if c.startswith('search ')],['search "CamelPhat"'])

    def test_title_matches_cannot_replace_an_exact_recording_or_poison_availability(self):
        s,b=self.setup_browser({'CamelPhat':[identity('different-recording')],
                               'House Dawgs':[identity('different-house-dawgs-mix')]})
        with self.assertRaises(RecordingLookupIncomplete) as caught:self.run_lookup(s,b,'load')
        self.assertNotIsInstance(caught.exception,RecordingUnavailable)
        self.assertIn('House Dawgs — CamelPhat',str(caught.exception))
        self.assertIn('availability is unconfirmed',str(caught.exception))
        job=BackgroundAnalysis(s,'rev',[TRACK])
        with patch.object(DJSession,'analyze_deck',side_effect=caught.exception):job.run()
        self.assertEqual(job.status,'blocked');self.assertIsNone(s.availability.blocked(TRACK['id']))
        self.assertFalse(any(' load' in c or ' play' in c for c in b.commands))

    def test_search_fallbacks_remain_bounded_when_provider_count_is_unavailable(self):
        s,b=self.setup_browser({'CamelPhat':[identity('other')],'House Dawgs':[identity('version')]});b.count_error=True
        with self.assertRaises(RecordingLookupIncomplete):self.run_lookup(s,b)
        self.assertLessEqual(b.reads,134)
        self.assertEqual(len([c for c in b.commands if c.startswith('search ')]),3)

    def test_title_search_can_fall_back_to_original_exact_identity_after_ranking_change(self):
        s,b=self.setup_browser({'CamelPhat':[identity('other'),TRACK['engine']['selection_id']],
                               'House Dawgs':[]})
        self.run_lookup(s,b)
        self.assertEqual(s.positions[TRACK['id']],{'query':'CamelPhat','position':1})
        self.assertEqual([c for c in b.commands if c.startswith('search ')],
                         ['search "CamelPhat"','search "House Dawgs"','search "CamelPhat"'])

    def test_changed_folder_aborts_before_new_search_or_load(self):
        s,b=self.setup_browser({'CamelPhat':[identity('other')]});selected=b.selected
        def moved(client):
            result=selected(client);b.folder={'id':identity('operator-changed-folder')};return result
        b.selected=moved
        with self.assertRaisesRegex(BridgeError,'folder changed'):self.run_lookup(s,b,'load')
        self.assertEqual([c for c in b.commands if c.startswith('search ')],['search "CamelPhat"'])


if __name__=='__main__':unittest.main()
