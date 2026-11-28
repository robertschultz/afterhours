"""Native console contract against a simulated engine; never contacts VirtualDJ."""
import copy
import http.client
import json
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from afterhours.dj_bridge import BridgeError
from afterhours.vdj_catalog import identity
from afterhours.vdj_controls import perform_control, read_controls, validate_control, bounded_controls
from afterhours.vdj_session import DJSession
from afterhours import server


class Peer:
    def __init__(self):
        self.sent=[];self.calls=[];self.cross=.5;self.automix=False;self.reject=False
        self.decks={d:dict(path=f'track-{d}',play=False,pitch=0.,key_lock=True,masterdeck=d==1,
            eq_high=.5,eq_mid=.5,eq_low=.5,filter=.5,volume=1.,pfl=False,
            position=15000.,cue=0,cues={0:15000.,1:0.},loop=False,sync=False) for d in (1,2)}
    def __enter__(self):return self
    def __exit__(self,*_):pass
    def query(self,script,kind=None):
        self.calls.append(script)
        if self.reject and self.sent:raise BridgeError('Lost readback')
        if script=='automix':return self.automix
        if script=='crossfader':return self.cross
        _,deck,verb=script.split(' ',2);d=self.decks[int(deck)]
        if verb=='loaded':return d['path'] is not None
        if verb=='get_filepath':return d['path']
        if verb=='get_title':return 'Native '+d['path']
        if verb=='get_artist':return 'Test artist'
        if verb=='get_pitch':return d['pitch']*100
        if verb=='pitch_range':return .12
        if verb in ('get_bpm','get_bpm absolute'):return 125*(1+d['pitch']) if verb=='get_bpm' else 125
        if verb=='get_time total absolute':return 240000
        if verb=='get_time elapsed absolute':return d['position']
        if verb=='get_key "harmonic"':return '08A'
        if verb=='is_sync':return d['sync']
        if verb=='get_beatpos':return d['position']/480
        if verb=='get_beat_bar':return .25
        if verb=='get_vu_meter':return .2 if d['play'] else 0
        if verb=='get_cue':return d['cue']
        if verb=='get_loop':return 4
        if verb.endswith('_label') or verb=='filter_label "name"':return verb.split('_')[0].upper()
        if verb.startswith('has_cue '):return int(verb.split()[1]) in d['cues']
        if verb.startswith('cue_pos '):
            try:return d['cues'][int(verb.split()[1])]
            except KeyError:raise BridgeError('No cue')
        if verb.startswith('cue_name '):return 'Intro'
        if verb.endswith('_freq'):return d[verb.removesuffix('_freq')]
        if verb in d:return d[verb]
        raise BridgeError('Unsupported query '+verb)
    def send(self,script):
        self.sent.append(script)
        for part in script.split(' & '):
            if part.startswith('crossfader '):self.cross=float(part.split()[1].strip('%'))/100;continue
            _,deck,verb=part.split(' ',2);d=self.decks[int(deck)];bits=verb.split();name=bits[0]
            if name in ('play','pause'):d['play']=name=='play'
            elif name=='goto_cue':d['position']=d['cues'][d['cue']]
            elif name=='set_cue':d['cue']=int(bits[1]);d['cues'][d['cue']]=d['position']
            elif name=='hot_cue':d['cue']=int(bits[1]);d['position']=d['cues'][d['cue']];d['play']=True
            elif name=='loop_exit':d['loop']=False
            elif name=='sync':d['sync']=True
            elif name=='pitch':d['pitch']=float(bits[1].strip('%'))/100-1
            elif bits[-1] in ('on','off'):d[name]=bits[-1]=='on'
            else:d[name]=float(bits[-1].strip('%'))/100


def payload(peer,control,deck=1,**kwargs):
    path=peer.decks[deck]['path']
    return dict(action='control',control=control,deck=deck,
        expectedTrackId='virtualdj:'+identity(path) if path else None,observedAt=time.time(),**kwargs)


class ControlTests(unittest.TestCase):
    def test_physical_mixer_movement_is_not_held_for_the_metadata_cache(self):
        p=Peer();s=DJSession(threading.Lock(),client_factory=lambda *a,**k:p)
        self.assertTrue(s.observe_once())
        p.decks[1].update(eq_high=.12,eq_mid=.23,eq_low=.34,volume=.45,filter=.56)
        p.cross=.67
        self.assertTrue(s.observe_once())
        t=s.snapshot()['telemetry'];c=t['decks'][0]['controls']
        self.assertEqual([c[k] for k in ('high','mid','low','volume','filter')],[.12,.23,.34,.45,.56])
        self.assertEqual(t['crossfader'],.67)
        self.assertEqual(p.sent,[])

    def test_each_visible_control_dispatches_once_and_confirms_readback(self):
        cases=[('play',{}),('pause',{}),('cue',{}),('sync',{}),('key-lock',{'value':False}),
            ('pitch',{'value':-.06}),('high',{'value':0}),('mid',{'value':.3}),('low',{'value':.2}),
            ('filter',{'value':.7}),('crossfader',{'value':.8}),('hotcue',{'slot':1}),
            ('hotcue',{'slot':8}),('loop-exit',{})]
        for name,values in cases:
            for deck in (1,2):
                with self.subTest(name=name,deck=deck):
                    p=Peer();p.decks[deck]['loop']=True
                    result=perform_control(p,payload(p,name,deck,**values))
                    self.assertTrue(result['verified']);self.assertEqual(len(p.sent),1)
    def test_cue_returns_playing_deck_to_selected_cue_and_pauses(self):
        p=Peer();p.decks[1].update(play=True,cue=1,position=20000)
        self.assertTrue(perform_control(p,payload(p,'cue'))['verified'])
        self.assertEqual(p.decks[1]['position'],0);self.assertFalse(p.decks[1]['play'])
    def test_no_automatic_replay_when_acknowledgement_is_lost(self):
        p=Peer();p.reject=True
        r=perform_control(p,payload(p,'high',value=.2))
        self.assertTrue(r['sent']);self.assertFalse(r['verified']);self.assertEqual(len(p.sent),1)
    def test_changed_recording_stale_request_bad_values_and_automix_send_nothing(self):
        for case in ('changed','stale','future','nan','injection','deck','range','automix'):
            with self.subTest(case=case):
                p=Peer();data=payload(p,'pitch',value=.02)
                if case=='changed':p.decks[1]['path']='replacement'
                if case=='stale':data['observedAt']-=4
                if case=='future':data['observedAt']+=10
                if case=='nan':data['value']=float('nan')
                if case=='injection':data['control']='pitch & play'
                if case=='deck':data['deck']=True
                if case=='range':data['value']=.15
                if case=='automix':p.automix=True
                with self.assertRaises(ValueError):perform_control(p,data)
                self.assertEqual(p.sent,[])
    def test_empty_deck_allows_mixer_controls_but_not_transport(self):
        p=Peer();p.decks[2]['path']=None
        self.assertTrue(perform_control(p,payload(p,'low',2,value=.25))['verified'])
        for name in ('play','cue','sync'):
            with self.assertRaises(ValueError):perform_control(p,payload(p,name,2))
    def test_external_knob_pitch_cue_loop_and_key_changes_are_measured(self):
        p=Peer();p.decks[2].update(eq_high=.15,eq_mid=.25,eq_low=0,filter=.9,pitch=.04,loop=True,key_lock=False)
        p.decks[2]['cues'][8]=200000
        c=read_controls(p,2,True)
        self.assertEqual((c['high'],c['mid'],c['low'],c['filter'],c['pitch']),(.15,.25,0,.9,.04))
        self.assertFalse(c['keyLock']);self.assertTrue(c['loop']);self.assertEqual(c['durationMs'],240000)
        self.assertEqual(c['cues'][7]['seconds'],200);self.assertEqual(c['key'],'08A');self.assertEqual(p.sent,[])
    def test_unknown_optional_control_is_missing_not_neutral(self):
        p=Peer();query=p.query
        def fail(script,kind=None):
            if script.endswith(' filter'):raise BridgeError('Unsupported filter')
            return query(script,kind)
        p.query=fail;c=read_controls(p,1,True)
        self.assertNotIn('filter',c);self.assertIn('filter',c['unavailable']);self.assertEqual(c['high'],.5)
    def test_observe_tracks_with_empty_agent_queue_and_identity_change(self):
        p=Peer();s=DJSession(threading.Lock(),client_factory=lambda *a,**k:p)
        self.assertEqual(s.snapshot()['order'],[]);self.assertTrue(s.observe_once())
        d=s.snapshot()['telemetry']['decks'][0];self.assertEqual(d['title'],'Native track-1')
        p.decks[1]['path']='other';p.decks[1]['pitch']=.03
        self.assertTrue(s.observe_once());d=s.snapshot()['telemetry']['decks'][0]
        self.assertEqual(d['id'],'virtualdj:'+identity('other'));self.assertEqual(d['controls']['pitch'],.03)
    def test_paused_preroll_keeps_signed_position_and_fresh_deck_readings(self):
        p=Peer();p.decks[1]['position']=-35.;p.cross=.2
        s=DJSession(threading.Lock(),client_factory=lambda *a,**k:p)
        s.update(phase='stopped')
        self.assertTrue(s.observe_once())
        t=s.snapshot()['telemetry']
        self.assertEqual(t['decks'][0]['position'],-.035)
        self.assertEqual(t['decks'][1]['position'],15.)
        self.assertEqual(t['crossfader'],.2)
        self.assertTrue(s.program()['connected'])
        self.assertEqual(s.program()['positionSeconds'],-.035)
        self.assertFalse(s.program()['playing'])
        p.decks[1]['position']=0.;p.cross=.7
        self.assertTrue(s.observe_once())
        self.assertEqual(s.snapshot()['telemetry']['decks'][0]['position'],0.)
        self.assertEqual(s.snapshot()['telemetry']['crossfader'],.7)
        self.assertFalse(s.control_lock.locked());self.assertFalse(s.observing)
        self.assertEqual(p.sent,[])
    def test_invalid_elapsed_readings_do_not_publish_or_leak_observer_lock(self):
        p=Peer();s=DJSession(threading.Lock(),client_factory=lambda *a,**k:p)
        self.assertTrue(s.observe_once());previous=s.snapshot()['telemetry']
        query=p.query
        for invalid in [-3_600_001,3_600_001,float('nan'),float('inf'),float('-inf'),True,None,'','not a time']:
            with self.subTest(invalid=invalid):
                p.query=lambda script,kind=None: invalid if script=='deck 1 get_time elapsed absolute' else query(script,kind)
                self.assertFalse(s.observe_once())
                self.assertEqual(s.snapshot()['telemetry'],previous)
                self.assertFalse(s.control_lock.locked());self.assertFalse(s.observing)
        p.query=query
        self.assertTrue(s.observe_once());self.assertEqual(p.sent,[])
    def test_signed_elapsed_boundaries_do_not_relax_other_engine_ranges(self):
        p=Peer();s=DJSession(threading.Lock(),client_factory=lambda *a,**k:p)
        query=p.query
        for milliseconds in [-3_600_000,3_600_000]:
            with self.subTest(milliseconds=milliseconds):
                p.query=lambda script,kind=None: milliseconds if script=='deck 1 get_time elapsed absolute' else query(script,kind)
                self.assertTrue(s.observe_once())
                self.assertEqual(s.snapshot()['telemetry']['decks'][0]['position'],milliseconds/1000)
        previous=s.snapshot()['telemetry']
        p.query=lambda script,kind=None: -1 if script=='deck 1 get_bpm' else query(script,kind)
        self.assertFalse(s.observe_once());self.assertEqual(s.snapshot()['telemetry'],previous)
        self.assertFalse(s.control_lock.locked());self.assertFalse(s.observing)
        self.assertEqual(p.sent,[])
    def test_transition_clock_defers_only_optional_console_reads(self):
        p = Peer()
        session = DJSession(threading.Lock())
        ordinary = session.telemetry(p)
        with (patch('afterhours.vdj_controls.bounded_controls') as slow,
              patch('afterhours.vdj_controls.live_controls') as live):
            for phase, clock_only in (('playing', True), ('mixing', False)):
                session.update(phase=phase)
                result = session.telemetry(p, clock_only=clock_only)
                for before, after in zip(ordinary['decks'], result['decks']):
                    for field in ('id', 'beatPosition', 'playing', 'bpm', 'level'):
                        self.assertEqual(before[field], after[field])
            slow.assert_not_called()
            live.assert_not_called()

    def test_session_requires_takeover_and_releases_lock(self):
        p=Peer();s=DJSession(threading.Lock(),client_factory=lambda *a,**k:p)
        with self.assertRaisesRegex(ValueError,'Take over'):s.console_control(payload(p,'play'))
        self.assertFalse(s.control_lock.locked());self.assertEqual(p.sent,[])
        s.update(phase='takeover');r=s.console_control(payload(p,'high',value=.2))
        self.assertTrue(r['verified']);self.assertEqual(r['session']['telemetry']['decks'][0]['controls']['high'],.2)
        s.update(active=True)
        with self.assertRaises(ValueError):s.console_control(payload(p,'play'))
        self.assertFalse(s.control_lock.locked())
    def test_program_uses_external_native_track_and_ignores_empty_other_deck(self):
        p=Peer();p.decks[2]['path']=None;s=DJSession(threading.Lock(),client_factory=lambda *a,**k:p)
        s.observe_once();out=s.program()
        self.assertEqual(out['current'],'virtualdj:'+identity('track-1'))
        self.assertEqual(out['library'][0]['title'],'Native track-1');self.assertEqual(out['durationSeconds'],240)
        self.assertEqual(out['key'],'08A')
    def test_optional_read_budget_does_not_extend_parent_deadline(self):
        p=Peer();p.deadline=time.monotonic()+.01;deadline=p.deadline
        with patch('afterhours.vdj_controls.read_controls',return_value={}) as read:
            bounded_controls(p,1,True);read.assert_called_once()
        self.assertEqual(p.deadline,deadline)


class ControlHTTPTests(unittest.TestCase):
    def test_fast_poll_omits_program_but_default_route_stays_compatible(self):
        session=DJSession(threading.Lock())
        session.update(telemetry={'sampledAt':123,'crossfader':.75,'decks':[]})
        httpd=ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        thread=threading.Thread(target=httpd.serve_forever,daemon=True);thread.start()
        def read(path):
            connection=http.client.HTTPConnection('127.0.0.1',httpd.server_port,timeout=3)
            connection.request('GET',path)
            response=connection.getresponse();data=json.loads(response.read());connection.close()
            self.assertEqual(response.status,200)
            return data
        try:
            with patch.object(server,'DJ_SESSION',session),patch.object(session,'program',return_value={'playing':False}) as program:
                fast=read('/api/control-state?program=0')
                self.assertEqual(fast['telemetry']['crossfader'],.75)
                self.assertEqual(fast['sessionId'],session.state['id'])
                self.assertNotIn('program',fast);program.assert_not_called()
                legacy=read('/api/control-state')
                self.assertEqual(legacy['program'],{'playing':False})
                program.assert_called_once()
        finally:httpd.shutdown();httpd.server_close();thread.join(2)

    def test_authenticated_route_and_guards(self):
        p=Peer();session=DJSession(threading.Lock(),client_factory=lambda *a,**k:p)
        httpd=ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        thread=threading.Thread(target=httpd.serve_forever,daemon=True);thread.start()
        def call(token=True,origin=None):
            connection=http.client.HTTPConnection('127.0.0.1',httpd.server_port,timeout=3)
            headers={'Content-Type':'application/json'}
            if token:headers['X-DJ-Token']=server.TOKEN
            if origin:headers['Origin']=origin
            connection.request('POST','/api/vdj/control',json.dumps(payload(p,'low',value=.2)),headers)
            r=connection.getresponse();result=(r.status,json.loads(r.read()));connection.close();return result
        try:
            with patch.object(server,'DJ_SESSION',session):
                self.assertEqual(call(token=False)[0],403)
                self.assertEqual(call(origin='https://example.com')[0],403)
                self.assertEqual(call()[0],409);self.assertEqual(p.sent,[])
                session.update(phase='takeover');status,data=call()
                self.assertEqual(status,200);self.assertTrue(data['verified'])
                self.assertEqual(data['session']['telemetry']['decks'][0]['controls']['low'],.2)
        finally:httpd.shutdown();httpd.server_close();thread.join(2)


if __name__=='__main__':unittest.main()
