"""Four-deck dispatch regressions; simulated VDJ peer, never real transport."""
import unittest

from afterhours.dj_bridge import BridgeError
from afterhours.vdj_mixing import NativeHandoff


class Peer:
    def __init__(self, outgoing=1, selected=3):
        self.outgoing=outgoing
        self.master=selected
        self.cross=0.0 if outgoing==1 else 1.0
        self.playing={deck:deck==outgoing for deck in (1,2,3,4)}
        self.bpm={outgoing:127,3-outgoing:127,3:111,4:90}
        self.sent=[]
        self.sleep=[]
        self.master_ack=True
        self.play_ack=True
        self.play_queries=0
        self.delay=0
        self.assignments=True
        self.move_cross=False
        self.fail_play=False
        self.fail_fade=False
        self.fail_cancel=False

    def query(self, client, script, kind=None):
        if script=='crossfader':return self.cross
        _,number,key=script.split(maxsplit=2);deck=int(number)
        if key.startswith('cross_assign'):return self.assignments
        if key=='masterdeck':return self.master_ack and self.master==deck
        if key=='play':
            if deck==3-self.outgoing and self.playing[deck]:
                self.play_queries+=1
                return self.play_ack and self.play_queries>self.delay
            return self.playing[deck]
        raise AssertionError(script)

    def send(self, script):
        self.sent.append(script)
        if script.endswith(' masterdeck on'):
            self.master=int(script.split()[1])
        elif script.endswith(' play_sync'):
            deck=int(script.split()[1]);self.playing[deck]=True
            self.bpm[deck]=self.bpm[self.master]
            if self.move_cross:self.cross=.5
            if self.fail_play:raise BridgeError('Transport acknowledgement lost')
        elif 'repeat_start_fade' in script:
            if self.fail_fade:raise BridgeError('Fade acknowledgement lost')
        elif script.startswith('repeat_stop'):
            if self.fail_cancel:raise BridgeError('Native stop unavailable')
        else:raise AssertionError(script)

    def handoff(self):
        return NativeHandoff(self.query,lambda c,s:self.send(s),self.sleep.append)


class NativeHandoffTests(unittest.TestCase):
    def test_selected_spare_and_sync_master_cannot_capture_either_direction(self):
        for outgoing in (1,2):
            for selected in (3,4):
                with self.subTest(outgoing=outgoing,selected=selected):
                    p=Peer(outgoing,selected);h=p.handoff();h.dispatch(p,outgoing,3-outgoing,16)
                    self.assertEqual(p.master,outgoing)
                    self.assertTrue(p.playing[1] and p.playing[2])
                    self.assertFalse(p.playing[3] or p.playing[4])
                    self.assertEqual([p.bpm[d] for d in (1,2,3,4)],[127,127,111,90])
                    self.assertEqual(sum('play_sync' in s for s in p.sent),1)
                    self.assertEqual(sum('repeat_start_fade' in s for s in p.sent),1)
                    self.assertTrue(p.sent[-1].startswith(f'deck {outgoing} repeat_start_fade'))
                    endpoint='1.000000' if outgoing==1 else '0.000000'
                    self.assertIn(f' {endpoint} 16bt & crossfader',p.sent[-1])
                    self.assertFalse(any('mix_now' in s or 'goto' in s or 'deck 3 ' in s or 'deck 4 ' in s for s in p.sent))
                    h.cancel_ramps(p)
                    self.assertEqual(p.sent[-1],"repeat_stop 'afterhours_crossfade'")
                    self.assertFalse(h.ramps)

    def test_delayed_start_is_read_again_but_never_replayed(self):
        p=Peer();p.delay=2;h=p.handoff();h.dispatch(p,1,2,8)
        self.assertEqual(p.sleep,[.05,.05])
        self.assertEqual(sum('play_sync' in s for s in p.sent),1)
        self.assertEqual(sum('repeat_start_fade' in s for s in p.sent),1)

    def test_missing_play_acknowledgement_does_not_fade_or_replay(self):
        p=Peer();p.play_ack=False;h=p.handoff()
        with self.assertRaisesRegex(BridgeError,'No fade or repeat Play'):h.dispatch(p,1,2,16)
        before=list(p.sent)
        with self.assertRaisesRegex(BridgeError,'already dispatched'):h.dispatch(p,1,2,16)
        self.assertEqual(p.sent,before)
        self.assertEqual(sum('play_sync' in s for s in p.sent),1)
        self.assertFalse(any('repeat_start_fade' in s for s in p.sent))
        self.assertLessEqual(sum(p.sleep),.151)

    def test_unconfirmed_master_or_changed_routing_never_starts_any_deck(self):
        for field in ('master_ack','assignments'):
            p=Peer();setattr(p,field,False);h=p.handoff()
            with self.subTest(field=field),self.assertRaises(BridgeError):h.dispatch(p,1,2,16)
            self.assertFalse(any('play_sync' in s or 'repeat_start_fade' in s for s in p.sent))

    def test_operator_movement_during_start_prevents_crossfade(self):
        p=Peer();p.move_cross=True;h=p.handoff()
        with self.assertRaisesRegex(BridgeError,'No crossfade'):h.dispatch(p,1,2,16)
        self.assertFalse(any('repeat_start_fade' in s for s in p.sent))
        self.assertTrue(p.playing[1] and p.playing[2])

    def test_uncertain_play_send_is_not_replayed(self):
        p=Peer();p.fail_play=True;h=p.handoff()
        with self.assertRaisesRegex(BridgeError,'acknowledgement lost'):h.dispatch(p,1,2,16)
        before=list(p.sent)
        with self.assertRaisesRegex(BridgeError,'already dispatched'):h.dispatch(p,1,2,16)
        self.assertEqual(p.sent,before)
        self.assertTrue(p.playing[2])
        self.assertFalse(h.ramps)

    def test_uncertain_fade_and_failed_cleanup_retain_exact_owned_ramp(self):
        p=Peer();p.fail_fade=True;h=p.handoff()
        with self.assertRaisesRegex(BridgeError,'Fade acknowledgement lost'):h.dispatch(p,1,2,16)
        self.assertEqual(h.ramps,{'crossfade'})
        p.fail_cancel=True
        with self.assertRaises(BridgeError):h.cancel_ramps(p)
        self.assertEqual(h.ramps,{'crossfade'})
        p.fail_cancel=False;h.cancel_ramps(p)
        self.assertFalse(h.ramps)


if __name__=='__main__':unittest.main()
