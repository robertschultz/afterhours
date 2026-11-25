"""Read-only EQ telemetry: no live engine or audio output."""
import threading
import unittest
from unittest.mock import patch
from afterhours.vdj_session import DJSession
from afterhours.dj_bridge import BridgeError

class EQPeer:
    def __init__(self):self.calls=[];self.eq=.5;self.failed=False;self.track='one'
    def query(self,script,kind=None):
        self.calls.append(script)
        if 'eq_' in script:
            if self.failed:raise BridgeError('Missing EQ')
            return self.eq
        if script=='crossfader':return 0
        readings={'loaded':1,'get_beatpos':32,'get_beat_bar':.25,'get_filepath':self.track,
                'get_title':'Track','get_artist':'Artist','play':0,'get_bpm':125,
                'get_time elapsed absolute':15000,'get_vu_meter':0,'is_sync':0}
        key=script.split(' ',2)[2]
        if key not in readings:raise BridgeError('Unsupported simulated reading')
        return readings[key]

class EQTests(unittest.TestCase):
    def test_actual_neutral_cut_and_changed_recording_are_observed(self):
        s=DJSession(threading.Lock());p=EQPeer()
        with patch('afterhours.vdj_session.time.time',return_value=100):a=s.telemetry(p,observing=True)
        self.assertEqual(a['decks'][0]['eq']['eq_low_freq'],.5)
        self.assertEqual(a['decks'][0]['eqSampledAt'],100)
        p.eq=0
        with patch('afterhours.vdj_session.time.time',return_value=101):b=s.telemetry(p,observing=True)
        self.assertEqual(b['decks'][1]['eq']['eq_mid_freq'],0)
        p.eq=.7;p.track='changed'
        with patch('afterhours.vdj_session.time.time',return_value=101.1):c=s.telemetry(p,observing=True)
        self.assertEqual(c['decks'][0]['eq']['eq_high_freq'],.7)

    def test_missing_and_expired_readings_are_not_invented(self):
        s=DJSession(threading.Lock());p=EQPeer();p.failed=True
        with patch('afterhours.vdj_session.time.time',return_value=100):a=s.telemetry(p,observing=True)
        self.assertNotIn('eq',a['decks'][0])
        p.failed=False
        with patch('afterhours.vdj_session.time.time',return_value=101):s.telemetry(p,observing=True)
        p.calls=[];s.update(phase='mixing')
        with patch('afterhours.vdj_session.time.time',return_value=105):b=s.telemetry(p)
        self.assertFalse(any('eq_' in call and '_freq' in call for call in p.calls))
        self.assertNotIn('eq',b['decks'][0])

if __name__=='__main__':unittest.main()
