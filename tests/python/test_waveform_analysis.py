"""Synthetic decoded-envelope boundary tests; not a native playback test."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import threading
from unittest.mock import patch,MagicMock
from afterhours.audio_analysis import scan_track
from afterhours.waveform_analysis import read_waveform,scan_waveform,validate_waveform,verified_waveform
from test_audio_analysis import Peer,TRACK,SID,report
from afterhours.dj_bridge import BridgeError
from afterhours.vdj_session import DJSession


def wave_report():
    data=report();data['version']=1
    data['envelope']={'framesPerBin':882,'peak':[8000]*500,'rms':[3277]*500}
    return data


class WavePeer(Peer):
    def send(self,q):
        if q!='deck 1 effect_button 1 2':return super().send(q)
        self.commands.append(q);self.scans+=1
        data=wave_report();data['scanStartedAtMs']+=self.scans
        (self.root/(SID+'.waveform.json')).write_text(json.dumps(data))


class WaveformTests(unittest.TestCase):
    def source(self,root):
        peer=Peer(root);scan_track(peer,TRACK,9.8,root=root,wait=peer.wait,clock=peer.clock)
        raw=(root/(SID+'.json')).read_bytes()
        return {'duration':10,'points':[{'label':'realEnd','seconds':9.8}],
                'energyMap':{'reportDigest':hashlib.sha256(raw).hexdigest()}},raw

    def test_full_waveform_scan_preserves_approved_analysis_and_publishes_only_numeric_envelope(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);evidence,original=self.source(root);receipt=(root/(SID+'.verified.json')).read_bytes();peer=WavePeer(root)
            result=scan_waveform(peer,TRACK,evidence,root=root,wait=peer.wait,clock=peer.clock)
            self.assertEqual(result['passes'],2);self.assertGreaterEqual(result['stableForSeconds'],1)
            self.assertEqual((root/(SID+'.json')).read_bytes(),original);self.assertEqual((root/(SID+'.verified.json')).read_bytes(),receipt)
            value=verified_waveform(SID,10,evidence['energyMap']['reportDigest'],root)
            self.assertEqual(value['resolutionSeconds'],.02);self.assertEqual(len(value['peak']),500)
            self.assertFalse(value['audioIncluded']);self.assertEqual(peer.effect,'Flanger');self.assertFalse(peer.active)
            self.assertTrue(all('effect_' in q for q in peer.commands))

    def test_malformed_or_missing_detail_and_mismatched_source_are_rejected(self):
        data=wave_report();feature=hashlib.sha256(json.dumps(data['features'],sort_keys=True).encode()).hexdigest()
        validate_waveform(data,9.8,feature)
        for mutation in ('short','spacing','amplitude','boolean','source','partial'):
            bad=copy.deepcopy(data);digest=feature
            if mutation=='short':bad['envelope']['peak'].pop()
            if mutation=='spacing':bad['envelope']['framesPerBin']=True
            if mutation=='amplitude':bad['envelope']['rms'][0]=40000
            if mutation=='boolean':bad['envelope']['peak'][0]=False
            if mutation=='source':digest='0'*64
            if mutation=='partial':bad['bufferReadComplete']=False
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):validate_waveform(bad,9.8,digest)

    def test_receipt_is_bound_to_source_and_waveform_and_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);evidence,_=self.source(root);peer=WavePeer(root)
            scan_waveform(peer,TRACK,evidence,root=root,wait=peer.wait,clock=peer.clock)
            self.assertIsNone(verified_waveform(SID,10,'0'*64,root))
            path=root/(SID+'.waveform.json');data=json.loads(path.read_text());data['envelope']['peak'][0]=9000;path.write_text(json.dumps(data))
            self.assertIsNone(verified_waveform(SID,10,evidence['energyMap']['reportDigest'],root))
            renamed=root/'source';path.rename(renamed);path.symlink_to(renamed);self.assertIsNone(read_waveform(SID,root))

    def test_playback_identity_and_active_effect_prevent_scan_commands(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);evidence,_=self.source(root)
            for flag in ('playing','changed','active'):
                peer=WavePeer(root);setattr(peer,flag,True)
                with self.subTest(flag=flag),self.assertRaises(BridgeError):
                    scan_waveform(peer,TRACK,evidence,root=root,wait=peer.wait,clock=peer.clock)
                self.assertEqual(peer.commands,[])

    def test_old_bridge_fails_with_actionable_message_and_restores_effect(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);evidence,_=self.source(root);peer=WavePeer(root);original=peer.send
            peer.send=lambda q:None if q=='deck 1 effect_button 1 2' else original(q)
            with self.assertRaisesRegex(BridgeError,'0.1.2'):
                scan_waveform(peer,TRACK,evidence,root=root,wait=peer.wait,clock=peer.clock)
            self.assertFalse(peer.active);self.assertEqual(peer.effect,'Flanger')
            self.assertFalse((root/(SID+'.waveform.verified.json')).exists())

    def test_session_waveform_lookup_cannot_read_unrelated_recordings(self):
        session=DJSession(threading.Lock());session.state['analysis']={'tracks':[{'id':'virtualdj:'+SID,'duration':10,'energyMap':{'reportDigest':'known'}}]}
        with patch('afterhours.vdj_session.verified_waveform',return_value={'ok':True}) as read:
            self.assertIsNone(session.waveform('virtualdj:'+'f'*64));self.assertIsNone(session.waveform('../arbitrary'))
            read.assert_not_called();self.assertEqual(session.waveform('virtualdj:'+SID),{'ok':True})
            read.assert_called_once_with(SID,10,'known')

    def test_new_set_preparation_refreshes_stale_features_before_waveforms_and_cues(self):
        for cached in (False,True):
            with self.subTest(cached=cached):
                session=DJSession(threading.Lock())
                order=[{'id':'virtualdj:'+c*64,'title':c,'bpm':125,'engine':{'selection_id':c*64}} for c in ('a','b')]
                session.state.update(order=order,candidates=order,mixStyle='phrase')
                refreshed=set();sequence=[]
                def report_for(items):
                    return {'tracks':[{'id':t['id'],'duration':10,'points':[{'label':'realEnd','seconds':9.8}],
                        'energyMap':{'reportDigest':'fresh' if t['id'] in refreshed else 'old'},'cueCandidates':[]} for t in items]}
                def rescan(client,t,*args,**kwargs):
                    refreshed.add(t['id']);sequence.append('features')
                def waveform(client,t,evidence,**kwargs):
                    self.assertEqual(evidence['energyMap']['reportDigest'],'fresh');sequence.append('waveform')
                def cues(client,t,evidence,*args,**kwargs):
                    self.assertEqual(evidence['energyMap']['reportDigest'],'old' if cached else 'fresh');sequence.append('cues');return []
                client=MagicMock();client.status.return_value={'automix':False,'decks':[{'playing':False}]}
                with patch('afterhours.vdj_session.validate_order',return_value=order), \
                     patch('afterhours.vdj_session.track_analysis',side_effect=report_for), \
                     patch('afterhours.vdj_session.verified_waveform',return_value={'verified':True} if cached else None), \
                     patch('afterhours.vdj_session.scan_track',side_effect=rescan), \
                     patch('afterhours.vdj_session.scan_waveform',side_effect=waveform), \
                     patch('afterhours.vdj_session.cue_planner.attach_candidates',side_effect=lambda report,*args:report), \
                     patch.object(session,'analyze_deck',side_effect=lambda c,t,d:t), \
                     patch.object(session,'measure_cues',side_effect=cues), \
                     patch.object(session,'prepare_deck'),patch.object(session,'send'),patch.object(session,'telemetry'), \
                     patch.object(session,'_launch',side_effect=lambda fn,phase:fn(client)):
                    session.prepare_analysis()
                self.assertEqual(sequence,['cues']*2 if cached else ['features','waveform','cues']*2)
                self.assertEqual(session.state['phase'],'ready')

    def test_waveform_worker_preserves_existing_ai_plan_and_rechecks_paused_state(self):
        for playing in (False,True):
            session=DJSession(threading.Lock());order=[{'id':'virtualdj:'+c*64,'bpm':125,'title':c,'engine':{'selection_id':c*64}} for c in ('a','b')]
            report={'tracks':[{'id':t['id'],'duration':10,'energyMap':{'reportDigest':t['id']}} for t in order]}
            review={'approval':'existing-only'};plan={'targetBpm':125,'selections':[{},{}],'transitions':[]}
            session.state.update(order=order,analysis=copy.deepcopy(report),aiReview=copy.deepcopy(review))
            client=MagicMock();client.status.return_value={'automix':False,'decks':[{'playing':playing},{'playing':False}]}
            with patch('afterhours.vdj_session.cue_planner.verify_receipts'),patch('afterhours.vdj_session.cue_planner.compile_plan',return_value=plan),patch('afterhours.vdj_session.scan_waveform') as scan,patch.object(session,'waveform',return_value=None),patch.object(session,'analyze_deck',side_effect=lambda c,t,d:t),patch.object(session,'prepare_deck') as prepare,patch.object(session,'send') as send,patch.object(session,'telemetry'),patch.object(session,'_launch',side_effect=lambda fn,phase:fn(client)):
                if playing:
                    with self.assertRaisesRegex(BridgeError,'Pause both'):session.prepare_waveforms()
                    scan.assert_not_called();prepare.assert_not_called();send.assert_not_called()
                else:
                    session.prepare_waveforms();self.assertEqual(scan.call_count,2);self.assertEqual(prepare.call_count,2)
                    send.assert_called_once_with(client,'crossfader 0%');self.assertTrue(session.state['executionPlan']['applied'])
                self.assertEqual(session.state['analysis'],report)
                self.assertEqual(session.state['aiReview'],review if playing else {**review,'appliedToPlayback':True})


if __name__=='__main__':unittest.main()
