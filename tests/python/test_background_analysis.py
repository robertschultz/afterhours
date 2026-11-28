"""Mailbox scheduling and deck isolation with simulated native replies only."""
import queue,threading,time,unittest
from unittest.mock import Mock,patch
from afterhours.background_analysis import BackgroundAnalysis,MailboxClient,AnalysisCancelled,SpareStreamRejected,guard_deck,guard_isolation,allowed
from afterhours.dj_bridge import BridgeError
from afterhours.vdj_catalog import identity
from afterhours.vdj_session import DJSession

class Peer:
    def __init__(self):
        self.commands=[];self.queries=[];self.values={'get_decks':4,'deck 3 get_deck':3,
            'deck 3 loaded':False,'deck 3 play':False,'deck 3 volume':1,'deck 3 pfl':False,'deck 3 deck_has_error':False,
            'deck 3 get_effect_name 1':'Flanger','deck 3 effect_active 1':False,'deck 3 get_filepath':'test'}
    def query(self,s,k=None):self.queries.append(s);return self.values[s]
    def send(self,s):
        self.commands.append(s)
        if 'deck 3 volume 0%' in s:self.values['deck 3 volume']=0
        if 'deck 3 pfl off' in s:self.values['deck 3 pfl']=False
        if 'unload' in s:self.values['deck 3 loaded']=False
        if 'volume 100.' in s:self.values['deck 3 volume']=1
        if s.startswith('deck 3 effect_select 1 '):self.values['deck 3 get_effect_name 1']=s.split('"')[1]
        if s=='deck 3 effect_active 1 off':self.values['deck 3 effect_active 1']=False

class BackgroundTests(unittest.TestCase):
    def job(self):return BackgroundAnalysis(DJSession(threading.Lock()),'revision',[{'id':'test','title':'Test'}])
    def test_two_deck_alias_and_in_use_deck_send_nothing(self):
        for values in ({'get_decks':2,'deck 3 get_deck':1},{'deck 3 loaded':True},{'deck 3 play':True},{'deck 3 effect_active 1':True}):
            peer=Peer();peer.values.update(values);job=self.job();job.start(peer)
            self.assertTrue(job.done);self.assertTrue(job.error);self.assertEqual(peer.commands,[])
    def test_acquire_and_cleanup_touch_only_isolated_deck(self):
        peer=Peer();job=self.job();job.acquire(peer);self.assertTrue(job.owned);guard_isolation(peer)
        job.loaded=identity('test');peer.values['deck 3 loaded']=True
        job.close(peer);self.assertFalse(job.owned)
        self.assertTrue(all('deck 3' in c and 'deck 1' not in c and 'deck 2' not in c for c in peer.commands))
        self.assertFalse(peer.values['deck 3 loaded']);self.assertEqual(peer.values['deck 3 volume'],1)
    def test_cold_start_empty_spare_slot_initializes_inactive_analyzer(self):
        from afterhours.audio_analysis import PLUGIN
        peer=Peer();peer.values['deck 3 get_effect_name 1']='';job=self.job()
        job.acquire(peer)
        self.assertTrue(job.owned);guard_isolation(peer)
        self.assertEqual(peer.values['deck 3 get_effect_name 1'],PLUGIN)
        self.assertFalse(peer.values['deck 3 effect_active 1'])
        job.close(peer)
        self.assertEqual(peer.values['deck 3 volume'],1)
        self.assertEqual(peer.values['deck 3 get_effect_name 1'],PLUGIN)
        self.assertFalse(peer.values['deck 3 effect_active 1'])
        self.assertFalse(any('deck 1' in c or 'deck 2' in c or 'effect_active 1 on' in c for c in peer.commands))
    def test_changed_recording_or_level_is_not_unloaded_or_overwritten(self):
        for values in ({'deck 3 loaded':True,'deck 3 get_filepath':'operator'},{'deck 3 volume':.7},{'deck 3 play':True}):
            peer=Peer();job=self.job();job.acquire(peer);job.loaded=identity('test');peer.values.update(values);peer.commands.clear()
            try:job.close(peer)
            except BridgeError:pass
            self.assertEqual(peer.commands,[])
    def test_auto_headphone_cue_is_cleared_only_for_our_confirmed_load(self):
        for changed in ({},{'deck 3 play':True},{'deck 3 volume':.7},{'deck 3 get_filepath':'operator'}):
            peer=Peer();job=self.job();job.acquire(peer);job.thread=Mock();job.loaded=identity('test')
            peer.values.update({'deck 3 pfl':True,'deck 3 loaded':True,**changed});peer.commands.clear()
            response=queue.Queue();job.pending.put(('isolate-loaded','',identity('test'),response))
            job.step(peer,40);result=response.get_nowait()
            if changed:
                self.assertIsInstance(result,BridgeError);self.assertFalse(peer.commands)
            else:
                self.assertIsNone(result);guard_isolation(peer)
                self.assertEqual(peer.commands,['deck 3 play ? nothing : deck 3 pfl off'])
    def test_no_background_rpc_is_serviced_near_a_mix(self):
        peer=Peer();job=self.job();job.thread=Mock();response=queue.Queue();job.pending.put(('query','get_decks',None,response))
        job.step(peer,11);self.assertTrue(response.empty());self.assertEqual(peer.queries,[])
        job.step(peer,40);self.assertEqual(response.get_nowait(),4)
    def test_socketless_client_unblocks_on_cancel(self):
        job=self.job();result=[]
        def run():
            try:MailboxClient(job).query('get_decks')
            except AnalysisCancelled:result.append('cancelled')
        thread=threading.Thread(target=run);thread.start();time.sleep(.02);job.cancelled.set();thread.join(.5)
        self.assertEqual(result,['cancelled']);self.assertFalse(thread.is_alive())
    def test_foreground_catalog_change_reselects_without_loading_the_wrong_row(self):
        peer=Peer();peer.values['deck 3 volume']=0;job=self.job();job.thread=Mock()
        response=queue.Queue();job.pending.put(('load','',{'track':{},'browserEpoch':0},response))
        job.invalidate_catalog();job.step(peer,40)
        self.assertIsInstance(response.get_nowait(),BridgeError);self.assertEqual(peer.commands,[])
        helper=Mock();helper.folder={'id':'current-scope'};track={'engine':{'selection_id':identity('test')}}
        helper.locate_for_load.side_effect=lambda *a:job.invalidate_catalog() if helper.locate_for_load.call_count==1 else None
        sent=[]
        def request(kind,script,arg):
            if kind=='isolate-loaded':return
            if arg['browserEpoch']!=job.browser_epoch:
                from afterhours.background_analysis import CatalogInterrupted
                raise CatalogInterrupted('Yielded')
            sent.append(arg)
        job.request=request;job.wait=lambda seconds:None;peer.values.update({'deck 3 loaded':True,'deck 3 deck_has_error':False})
        job.load(helper,peer,track,3)
        self.assertEqual(helper.locate_for_load.call_count,2);self.assertEqual(len(sent),1)
    def test_background_load_uses_verified_search_scope_instead_of_origin_folder(self):
        peer=Peer();job=self.job();job.acquire(peer);job.thread=Mock()
        track={'engine':{'selection_id':identity('test'),'folder_id':'old-origin'}}
        response=queue.Queue();job.pending.put(('load','',{'track':track,'browserEpoch':0,'folderId':'current-scope'},response))
        with patch('afterhours.background_analysis.require_selection') as guard:
            job.step(peer,40)
        self.assertIsNone(response.get_nowait())
        guard.assert_called_once_with(peer,identity('test'),'current-scope')
        self.assertIn('deck 3 play ? nothing : deck 3 load',peer.commands)
    def test_rpc_scope_rejects_transport_live_decks_and_unscoped_audio(self):
        for script in ('deck 1 load','deck 2 pause','deck 3 play','play','crossfader 50%','deck 3 goto 50% & deck 1 play'):
            self.assertFalse(allowed(script,True),script)
        for script in ('deck 3 effect_button 1 1','deck 3 goto 40%','search "Fixture Artist"'):
            self.assertTrue(allowed(script,True),script)
    def test_decoder_readback_timeout_does_not_repeat_the_load(self):
        job=self.job();job.wait=lambda seconds:None
        helper=Mock();helper.folder={'id':'current-scope'};helper.locate_for_load.return_value=None;sent=[];job.request=lambda *args:sent.append(args)
        track={'engine':{'selection_id':identity('test')}}
        class DecodingPeer:
            pending=True
            def query(self,script,kind=None):
                if self.pending:
                    self.pending=False;raise BridgeError('Decoder busy')
                return {'deck 3 deck_has_error':False,'deck 3 loaded':True,
                        'deck 3 get_filepath':'test'}[script]
        job.load(helper,DecodingPeer(),track,3)
        helper.locate_for_load.assert_called_once()
        self.assertEqual([call[0] for call in sent],['load','isolate-loaded'])
        self.assertEqual(sent[0][0],'load')
        self.assertEqual(job.loaded,track['engine']['selection_id'])
    def test_native_stream_rejection_is_exact_and_never_replays_load(self):
        expected = identity('test')
        for loaded, path, error in ((False,'',SpareStreamRejected), (True,'operator',BridgeError)):
            with self.subTest(loaded=loaded,path=path):
                peer = Peer()
                job = self.job()
                job.acquire(peer)
                peer.values.update({'deck 3 deck_has_error':True,'deck 3 loaded':loaded,
                                    'deck 3 get_filepath':path})
                helper = Mock()
                helper.folder = {'id':'current-scope'}
                helper.locate_for_load.return_value = None
                job.wait = Mock()
                job.requested_load = expected
                job.request = Mock()
                with self.assertRaises(error) as failed:
                    job.load(helper,peer,{'engine':{'selection_id':expected}},3)
                self.assertIs(type(failed.exception),error)
                job.request.assert_called_once()

    def test_empty_work_list_reuses_evidence_without_reserving_deck(self):
        peer=Peer();job=BackgroundAnalysis(DJSession(threading.Lock()),'rev',[]);job.step(peer,40)
        self.assertTrue(job.done);self.assertFalse(peer.commands);self.assertFalse(peer.queries)
    def test_finished_or_replaced_queue_cancels_analysis_before_another_command(self):
        s=DJSession(threading.Lock());job=self.job();job.close=Mock();s.background_job=job
        s.service_background(None,11);job.close.assert_not_called()
        s.service_background(None,20);job.close.assert_called_once();self.assertIsNone(s.background_job)

    def test_complete_two_scan_verification_runs_only_on_isolated_deck_while_ab_play(self):
        import tempfile
        from pathlib import Path
        from test_audio_analysis import Peer as AudioPeer,TRACK
        from afterhours.audio_analysis import scan_track,verified_energy
        class IsolatedPeer(AudioPeer):
            def query(self,script,kind=None):
                if script=='get_decks':return 4
                if script=='deck 3 get_deck':return 3
                if script=='deck 3 volume':return 0
                if script=='deck 3 pfl':return False
                if script=='deck 3 play':return False
                self.assert_scope(script)
                return super().query(script.replace('deck 3','deck 1'),kind)
            def assert_scope(self,script):
                if 'deck 1' in script or 'deck 2' in script:raise AssertionError('Protected decks addressed')
            def send(self,script):
                self.assert_scope(script);super().send(script.replace('deck 3','deck 1'))
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);peer=IsolatedPeer(root);peer.playing=True
            result=scan_track(peer,TRACK,9.8,root=root,clock=peer.clock,wait=peer.wait,deck=3)
            self.assertGreaterEqual(result['passes'],2)
            self.assertTrue(peer.playing);self.assertFalse(peer.active);self.assertEqual(peer.effect,'Flanger')
            self.assertEqual(verified_energy(TRACK['engine']['selection_id'],10,root)['coverage'],1)

    def test_background_missing_waveform_refreshes_source_before_measuring_cues(self):
        track={'id':'virtualdj:'+'a'*64,'title':'Test','bpm':125,'engine':{'selection_id':'a'*64}}
        old={'duration':10,'points':[{'label':'realEnd','seconds':9.8}],'energyMap':{'reportDigest':'old'}}
        fresh={**old,'energyMap':{'reportDigest':'fresh'}}
        job=BackgroundAnalysis(DJSession(threading.Lock()),'revision',[track]);sequence=[]
        def scan(client,t,end,**kwargs):
            self.assertEqual(kwargs['deck'],3);sequence.append('features')
        def waveform(client,t,evidence,**kwargs):
            self.assertEqual(evidence['energyMap']['reportDigest'],'fresh');self.assertEqual(kwargs['deck'],3);sequence.append('waveform')
        def cues(client,t,evidence,*args,**kwargs):
            self.assertEqual(evidence['energyMap']['reportDigest'],'fresh');self.assertEqual(kwargs['deck'],3);sequence.append('cues');return [{'verified':True}]
        with patch.object(DJSession,'analyze_deck',return_value=track), \
             patch.object(DJSession,'measure_cues',side_effect=cues), \
             patch('afterhours.background_analysis.track_analysis',side_effect=[{'tracks':[old]},{'tracks':[fresh]}]), \
             patch('afterhours.background_analysis.verified_waveform',return_value=None), \
             patch('afterhours.background_analysis.scan_track',side_effect=scan), \
             patch('afterhours.background_analysis.scan_waveform',side_effect=waveform):
            job.run()
        self.assertIsNone(job.error);self.assertEqual(job.status,'complete')
        self.assertEqual(sequence,['features','waveform','cues'])
        self.assertEqual(job.result[0]['energyMap']['reportDigest'],'fresh')

    def test_timed_refill_still_verifies_audio_and_cues_without_display_waveform(self):
        track = {'id':'virtualdj:'+'a'*64,'title':'Test','bpm':125,'engine':{'selection_id':'a'*64}}
        cold = {'duration':10,'points':[{'label':'realEnd','seconds':9.8}]}
        verified = {**cold,'energyMap':{'reportDigest':'fresh'}}
        session = DJSession(threading.Lock())
        session.update(entryCadence=[60,30])
        job = BackgroundAnalysis(session,'revision',[track])
        with patch.object(DJSession,'analyze_deck',return_value=track), \
             patch.object(DJSession,'measure_cues',return_value=[{'replayVerified':True}]) as cues, \
             patch('afterhours.background_analysis.track_analysis',side_effect=[{'tracks':[cold]},{'tracks':[verified]}]), \
             patch('afterhours.background_analysis.scan_track') as features, \
             patch('afterhours.background_analysis.scan_waveform') as waveform:
            job.run()
        features.assert_called_once()
        waveform.assert_not_called()
        self.assertEqual(cues.call_args.args[2]['energyMap'],verified['energyMap'])
        self.assertTrue(cues.call_args.kwargs['timed'])
        self.assertTrue(job.result[0]['timedPreparation'])

if __name__=='__main__':unittest.main()
