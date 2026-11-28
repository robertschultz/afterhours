"""Owned spare-deck recovery with synthetic native replies, never live audio."""
import copy
import json
from pathlib import Path
import re
import tempfile
import threading
import unittest
from contextlib import nullcontext
from unittest.mock import Mock,patch

from afterhours.background_analysis import BackgroundAnalysis,cleanup_analysis_deck,cleanup_descriptor,merge_cleanup_pending
from afterhours.preparation_pool import PreparationPool
from afterhours.dj_bridge import BridgeError, OSCReplyTimeout
from afterhours.session_recovery import SessionRecovery
from afterhours.vdj_catalog import identity
from afterhours.vdj_mixing import NativeHandoff,TransitionMixer
from afterhours.vdj_session import DJSession
from test_background_analysis import Peer


class CleanupPeer(Peer):
    def __init__(self):
        super().__init__()
        self.values.update({k.replace('deck 3','deck 4'):v for k,v in list(self.values.items()) if k.startswith('deck 3')})
        self.values.update({'deck 4 get_deck':4,'deck 1 play':False,'deck 2 play':False})
        self.values.update({'deck 3 deck_has_error':False,'deck 4 deck_has_error':False})
        self.ignore_unload=set();self.fail_query=None;self.fail_after_restore=False;self.fail_ramp=False

    def query(self,script,kind=None):
        if self.fail_query==script:
            self.fail_query=None
            raise BridgeError('Injected cleanup read timeout')
        return super().query(script,kind)

    def send(self,script):
        self.commands.append(script)
        if 'repeat_stop ' in script:
            if self.fail_ramp:raise BridgeError('Injected ramp cancellation failure')
            return
        guard=re.match(r'deck ([34]) play \? nothing : ',script)
        if guard and self.values[f'deck {guard[1]} play']:return
        for deck in (1,2,3,4):
            if f'deck {deck} pause' in script:self.values[f'deck {deck} play']=False
            if f'deck {deck} unload' in script and deck not in self.ignore_unload:
                self.values[f'deck {deck} loaded']=False
                self.values[f'deck {deck} deck_has_error']=False
            found=re.search(rf'deck {deck} volume ([0-9.]+)%',script)
            if found:
                self.values[f'deck {deck} volume']=float(found[1])/100
                if self.fail_after_restore and float(found[1])>0:
                    self.fail_after_restore=False;self.fail_query='get_decks'
            found=re.search(rf'deck {deck} pfl (on|off)',script)
            if found:self.values[f'deck {deck} pfl']=found[1]=='on'
            found=re.search(rf'deck {deck} effect_active 1 (on|off)',script)
            if found:self.values[f'deck {deck} effect_active 1']=found[1]=='on'
            found=re.search(rf'deck {deck} effect_select 1 "([^"]+)"',script)
            if found:self.values[f'deck {deck} get_effect_name 1']=found[1]


class AnalysisCleanupTests(unittest.TestCase):
    def owned(self,deck=3,peer=None,session=None):
        p=peer or CleanupPeer();s=session or DJSession(threading.Lock())
        j=BackgroundAnalysis(s,'rev',[{'id':'test','title':'Test'}],deck=deck)
        j.acquire(p);j.loaded=identity('test');p.values[f'deck {deck} loaded']=True
        p.commands.clear()
        return s,p,j

    def latch(self,s,p,j):
        p.fail_query='get_decks'
        with self.assertRaises(BridgeError):j.close(p)
        s.remember_cleanup_failure('background',j)

    def test_native_auto_headphone_cue_after_unload_allows_safe_restoration(self):
        for during_retry in (False, True):
            with self.subTest(during_retry=during_retry):
                s, peer, job = self.owned()
                descriptor = job.cleanup_descriptors()[0]
                native_send = peer.send
                def send(command):
                    native_send(command)
                    if command.endswith('deck 3 unload'):
                        peer.values['deck 3 pfl'] = True
                peer.send = send
                if during_retry:
                    descriptor['unloadRequested'] = True
                    peer.values['deck 3 loaded'] = False
                    peer.values['deck 3 pfl'] = True
                cleanup_analysis_deck(peer, descriptor)
                self.assertFalse(peer.values['deck 3 pfl'])
                self.assertFalse(peer.values['deck 3 loaded'])
                self.assertFalse(peer.values['deck 3 deck_has_error'])
                self.assertEqual(peer.values['deck 3 volume'], descriptor['baseline']['volume'])
                self.assertTrue(descriptor['restoreRequested'])

    def test_one_lost_cleanup_reply_does_not_repeat_the_unload(self):
        _, peer, job = self.owned(deck=4)
        native_query = peer.query
        lost = []

        def query(script, kind=None):
            if not lost and script == 'deck 4 get_deck' and any(
                    command.endswith('deck 4 unload') for command in peer.commands):
                lost.append(script)
                raise OSCReplyTimeout('Missed one native reply.')
            return native_query(script, kind)

        peer.query = query
        job.close(peer)
        self.assertEqual(lost, ['deck 4 get_deck'])
        self.assertEqual(sum(command.endswith('deck 4 unload') for command in peer.commands), 1)
        self.assertFalse(job.owned)

    def test_failed_close_keeps_ownership_until_explicit_cleanup_verifies_it(self):
        s,p,j=self.owned();self.latch(s,p,j)
        self.assertTrue(j.owned);self.assertEqual(j.cleanup_descriptors()[0]['selectionId'],identity('test'))
        with self.assertRaises(BridgeError):j.close(p)
        self.assertEqual(p.commands,[])  # Ordinary teardown cannot retry it.
        s.retry_paused_cleanup(p)
        self.assertFalse(s.state['resumeBlocked']);self.assertIsNone(s.state['cleanupPending'])
        self.assertFalse(p.values['deck 3 loaded']);self.assertEqual(p.values['deck 3 volume'],1)
        self.assertTrue(all('deck 3' in c and 'deck 1' not in c and 'deck 2' not in c for c in p.commands))

    def test_failed_owned_load_can_clear_native_headphone_cue(self):
        for error in (False, True):
            with self.subTest(error=error):
                _, peer, job = self.owned()
                job.requested_load = job.loaded
                job.loaded = None
                peer.values.update({'deck 3 loaded':False,'deck 3 pfl':True,'deck 3 deck_has_error':error})
                descriptor = job.cleanup_descriptors()[0]
                if not error:
                    with self.assertRaises(BridgeError):
                        cleanup_analysis_deck(peer,descriptor)
                    self.assertEqual(peer.commands,[])
                    continue
                cleanup_analysis_deck(peer,descriptor)
                self.assertFalse(descriptor['loadPending'])
                self.assertFalse(peer.values['deck 3 pfl'])
                self.assertFalse(peer.values['deck 3 deck_has_error'])
                self.assertEqual(peer.values['deck 3 volume'],1)
                self.assertTrue(all('deck 3' in command for command in peer.commands))
                self.assertEqual(sum(command.count('deck 3 unload') for command in peer.commands),1)

    def test_failed_owned_load_keeps_spare_when_disk_error_does_not_clear(self):
        _, peer, job = self.owned()
        job.requested_load = job.loaded
        job.loaded = None
        peer.values.update({'deck 3 loaded':False,'deck 3 deck_has_error':True})
        peer.ignore_unload.add(3)
        descriptor = job.cleanup_descriptors()[0]
        with self.assertRaisesRegex(BridgeError,'still reports a load error'):
            cleanup_analysis_deck(peer,descriptor)
        self.assertTrue(descriptor['unloadRequested'])
        self.assertEqual(peer.values['deck 3 volume'],0)

    def test_unconfirmed_unload_is_an_error_with_a_recoverable_descriptor(self):
        s,p,j=self.owned();p.ignore_unload.add(3)
        with self.assertRaisesRegex(BridgeError,'unload was not confirmed'):j.close(p)
        self.assertTrue(j.owned);self.assertTrue(j.cleanup_descriptors()[0]['unloadRequested'])
        self.assertTrue(p.values['deck 3 loaded']);self.assertEqual(p.values['deck 3 volume'],0)
        s.remember_cleanup_failure('background',j);p.ignore_unload.clear();s.retry_paused_cleanup(p)
        self.assertFalse(s.state['resumeBlocked']);self.assertFalse(p.values['deck 3 loaded'])

    def test_missing_final_ack_verifies_completed_restore_without_repeating_writes(self):
        s,p,j=self.owned();p.fail_after_restore=True
        with self.assertRaises(BridgeError):j.close(p)
        self.assertTrue(j.owned);self.assertTrue(j.cleanup_descriptors()[0]['restoreRequested'])
        self.assertFalse(p.values['deck 3 loaded']);self.assertEqual(p.values['deck 3 volume'],1)
        s.remember_cleanup_failure('background',j);p.commands.clear();s.retry_paused_cleanup(p)
        self.assertEqual(p.commands,[]);self.assertFalse(s.state['resumeBlocked'])

    def test_operator_changes_and_aliases_prevent_all_spare_deck_mutations(self):
        for change in ({'deck 3 play':True},{'deck 3 volume':.7},{'deck 3 pfl':True},
                       {'deck 3 get_filepath':'operator'},{'deck 3 get_effect_name 1':'Echo'},
                       {'deck 3 get_deck':1}):
            with self.subTest(change=change):
                s,p,j=self.owned();self.latch(s,p,j);p.values.update(change)
                with self.assertRaises(BridgeError):s.retry_paused_cleanup(p)
                self.assertEqual(p.commands,[]);self.assertTrue(s.state['resumeBlocked'])
                self.assertEqual(len(s.state['cleanupPending']['analysis']),1)

    def test_known_cleanup_never_clears_an_independent_old_unknown_latch(self):
        s,p,j=self.owned();s.update(resumeBlocked=True,cleanupPending={'ramps':[],'unknown':True})
        self.latch(s,p,j);s.retry_paused_cleanup(p)
        self.assertFalse(p.values['deck 3 loaded']);self.assertTrue(s.state['resumeBlocked'])
        self.assertEqual(s.state['cleanupPending'],{'ramps':[],'unknown':True})

    def test_effect_change_during_restoration_is_not_followed_by_unload(self):
        s,p,j=self.owned();from afterhours.audio_analysis import PLUGIN
        p.values['deck 3 get_effect_name 1']=PLUGIN
        def changed(*args):p.values['deck 3 get_effect_name 1']='Operator Echo'
        with patch('afterhours.background_analysis.restore_effect',side_effect=changed):
            with self.assertRaisesRegex(BridgeError,'effect cleanup'):j.close(p)
        self.assertTrue(j.owned);self.assertEqual(p.commands,[]);self.assertTrue(p.values['deck 3 loaded'])

    def test_cancelled_load_keeps_dispatched_exact_identity_without_replaying_load(self):
        s,p,j=self.owned();j.loaded=None;j.requested_load=identity('test')
        self.latch(s,p,j);s.retry_paused_cleanup(p)
        self.assertFalse(s.state['resumeBlocked'])
        self.assertFalse(any(re.search(r'\b(load|play_sync|pause)\b',c) for c in p.commands))

    def test_native_load_error_releases_the_isolated_empty_deck(self):
        s,p,j=self.owned();j.loaded=None;j.requested_load=identity('test')
        p.values.update({'deck 3 loaded':False,'deck 3 deck_has_error':True})
        s.remember_cleanup_failure('background',j)
        s.retry_paused_cleanup(p)
        self.assertFalse(s.state['resumeBlocked'])
        self.assertEqual(p.values['deck 3 volume'],1)
        self.assertFalse(any(re.search(r'\b(load|play_sync|pause)\b',c) for c in p.commands))

    def test_empty_readback_cannot_release_an_unconfirmed_stream_load(self):
        s,p,j=self.owned();j.loaded=None;j.requested_load=identity('test')
        p.values['deck 3 loaded']=False
        with self.assertRaisesRegex(BridgeError,'stream load is still unconfirmed'):j.close(p)
        self.assertTrue(j.owned);self.assertTrue(j.cleanup_descriptors()[0]['loadPending'])
        self.assertEqual(p.values['deck 3 volume'],0);self.assertEqual(p.commands,[])
        s.remember_cleanup_failure('background',j)
        with self.assertRaises(BridgeError):s.retry_paused_cleanup(p)
        self.assertTrue(s.state['resumeBlocked']);self.assertEqual(p.commands,[])
        # The original Load may finish later. Its exact identity can be cleaned
        # by a subsequent explicit Stop, without ever issuing another Load.
        p.values['deck 3 loaded']=True;s.retry_paused_cleanup(p)
        self.assertFalse(s.state['resumeBlocked']);self.assertFalse(p.values['deck 3 loaded'])
        self.assertEqual(p.values['deck 3 volume'],1)
        self.assertFalse(any(re.search(r'\b(load|play_sync|pause)\b',c) for c in p.commands))

    def test_pool_keeps_only_failed_children_and_reports_the_actual_deck(self):
        s,p,c=self.owned();_,_,d=self.owned(4,p,s)
        pool=PreparationPool(s,'rev',[]);pool.jobs=[c,d]
        c.done=True;d.done=True
        p.values['deck 4 play']=True
        with self.assertRaisesRegex(BridgeError,'Deck 4'):pool.close(p)
        self.assertFalse(c.owned);self.assertTrue(d.owned)
        self.assertEqual([r['deck'] for r in pool.cleanup_descriptors()],[4])
        self.assertEqual([r['deck'] for r in pool.snapshot()['decks']],[4])
        self.assertFalse(any('deck 4' in cmd for cmd in p.commands))

    def test_normal_completion_failure_quarantines_only_the_unconfirmed_spare(self):
        s,p,j=self.owned();j.done=True;p.ignore_unload.add(3)
        s.background_job=j
        s.update(queuedUpdate={'id':'rev','status':'analyzing','tracks':[],'protectedIds':[],'reason':'fixture'})
        s.service_background(p,20)
        self.assertIsNone(s.background_job);self.assertTrue(s.state['resumeBlocked'])
        self.assertEqual(s.preparation_decks(),(4,))
        self.assertFalse(s.state['cleanupPending']['unknown'])
        self.assertEqual(s.state['cleanupPending']['analysis'][0]['deck'],3)
        self.assertEqual(s.state['queuedUpdate']['status'],'blocked')

    def test_cancelled_revision_failure_retains_ownership_before_worker_teardown(self):
        s,p,j=self.owned();s.background_job=j;p.values['deck 3 play']=True
        with self.assertRaises(BridgeError):s.service_background(p,20)
        self.assertIs(s.background_job,j);self.assertTrue(s.state['resumeBlocked'])
        self.assertEqual(p.commands,[])

    def test_worker_failure_retains_exact_ownership_and_the_native_error(self):
        s,p,j=self.owned();p.values['deck 3 play']=True;s.background_job=j
        s.factory=lambda *a,**kw:nullcontext(p);s.control_lock.acquire()
        def fail(client):raise BridgeError('Fixture transition timeout')
        s._work(fail)
        self.assertIsNone(s.background_job);self.assertTrue(s.state['resumeBlocked'])
        self.assertFalse(s.state['cleanupPending']['unknown'])
        self.assertTrue(any('deck 3 is no longer paused and isolated' in event['text'] for event in s.state['events']))
        self.assertEqual(p.commands,[])

    def test_descriptors_survive_empty_and_nonempty_checkpoints_without_engine_calls(self):
        from test_session_recovery import session
        for queued in (False,True):
            with self.subTest(queued=queued),tempfile.TemporaryDirectory() as directory:
                source=session() if queued else DJSession(threading.Lock())
                _,p,j=self.owned(session=source);self.latch(source,p,j)
                store=SessionRecovery(Path(directory)/'queue.json');store.save(source)
                recovered=DJSession(threading.Lock())
                with patch.object(recovered,'factory') as native:store.restore(recovered)
                native.assert_not_called();self.assertEqual(source.state['cleanupPending'],recovered.state['cleanupPending'])
                recovered.retry_paused_cleanup(p)
                self.assertFalse(recovered.state['resumeBlocked'])

    def test_legacy_unknown_cleanup_survives_invalid_queue_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'queue.json'
            path.write_text(json.dumps({'version':1,'session':{'order':'invalid',
                'resumeBlocked':True,'cleanupPending':{'ramps':[],'unknown':True}}}))
            recovered=DJSession(threading.Lock())
            with patch.object(recovered,'factory') as native:
                with self.assertRaises(ValueError):SessionRecovery(path).restore(recovered)
            native.assert_not_called();self.assertTrue(recovered.state['resumeBlocked'])
            self.assertEqual(recovered.state['cleanupPending'],{'ramps':[],'unknown':True})

    def test_malformed_or_conflicting_recovered_descriptors_cannot_mutate_engine(self):
        s,p,j=self.owned();valid=j.cleanup_descriptors()[0]
        rows=[{**valid,'deck':1},{**valid,'selectionId':'untrusted'},
              {**valid,'baseline':{**valid['baseline'],'effect':'Echo" & deck 1 play'}},
              {**valid,'baseline':{**valid['baseline'],'volume':float('nan')}}]
        for row in rows:
            with self.subTest(row=row):
                s.update(resumeBlocked=True,cleanupPending={'ramps':[],'analysis':[row],'unknown':False})
                s.retry_paused_cleanup(p)
                self.assertTrue(s.state['resumeBlocked']);self.assertEqual(p.commands,[])
        conflict={**valid,'ownerId':'a'*32,'selectionId':identity('other')}
        merged=merge_cleanup_pending({'analysis':[valid,conflict]})
        self.assertTrue(merged['unknown']);self.assertNotIn('analysis',merged)

    def test_final_main_deck_read_failure_retains_completed_cleanup_stage(self):
        s,p,j=self.owned();self.latch(s,p,j);p.fail_query='deck 1 play'
        with self.assertRaises(BridgeError):s.retry_paused_cleanup(p)
        self.assertTrue(s.state['resumeBlocked'])
        self.assertTrue(s.state['cleanupPending']['analysis'][0]['restoreRequested'])
        p.commands.clear();s.retry_paused_cleanup(p)
        self.assertFalse(s.state['resumeBlocked']);self.assertEqual(p.commands,[])

    def test_crossfade_cancellation_failure_has_a_fixed_owned_stop_path(self):
        s=DJSession(threading.Lock());p=CleanupPeer()
        handoff=NativeHandoff(Mock(),Mock(),Mock());handoff.ramps['crossfade']=1;s.handoff=handoff
        p.fail_ramp=True;s.factory=lambda *a,**kw:nullcontext(p);s.control_lock.acquire()
        s._work(lambda client:None)
        self.assertEqual(s.state['cleanupPending'],{'ramps':['1:afterhours_crossfade'],'unknown':False})
        self.assertIsNone(s.handoff)
        p.fail_ramp=False;s.retry_paused_cleanup(p)
        self.assertFalse(s.state['resumeBlocked'])
        self.assertEqual(p.commands,["deck 1 repeat_stop 'afterhours_crossfade'"]*2)

    def test_owned_audible_ramps_are_stopped_before_slow_analysis_cleanup(self):
        s,p,j=self.owned();s.background_job=j
        handoff=NativeHandoff(Mock(),Mock(),Mock());handoff.ramps['crossfade']=1;s.handoff=handoff
        mixer=TransitionMixer(Mock(),Mock());mixer.ramps['out_low']=1;s.mixer=mixer
        close=j.close
        def cleanup(client):
            self.assertEqual(handoff.ramps,{});self.assertEqual(mixer.ramps,{})
            self.assertEqual(p.commands,["deck 1 repeat_stop 'afterhours_crossfade'","deck 1 repeat_stop 'afterhours_out_low'"])
            close(client)
        j.close=cleanup;s.factory=lambda *a,**kw:nullcontext(p);s.control_lock.acquire()
        s._work(lambda client:None)
        self.assertFalse(s.state.get('resumeBlocked'));self.assertFalse(j.owned)

    def test_explicit_stop_does_not_pause_a_spare_that_started_outside_its_worker(self):
        s,p,j=self.owned();self.latch(s,p,j);p.values['deck 3 play']=True
        s.factory=lambda *a,**kw:nullcontext(p)
        class InlineThread:
            def __init__(self,target,args,**kw):self.target,self.args=target,args
            def start(self):self.target(*self.args)
            def is_alive(self):return False
        with patch('afterhours.vdj_session.threading.Thread',InlineThread):s.stop(pause=True)
        self.assertEqual(p.commands,['deck 1 pause & deck 2 pause'])
        self.assertTrue(p.values['deck 3 play']);self.assertTrue(s.state['resumeBlocked'])
        p.values['deck 3 play']=False;p.commands.clear()
        with patch('afterhours.vdj_session.threading.Thread',InlineThread):s.stop(pause=True)
        self.assertFalse(s.state['resumeBlocked']);self.assertFalse(p.values['deck 3 loaded'])
        self.assertFalse(any('deck 3 pause' in cmd for cmd in p.commands))


if __name__=='__main__':unittest.main()


class DeferredSpareCleanupTests(unittest.TestCase):
    def setUp(self):
        self.session = DJSession(threading.Lock())
        self.peer = CleanupPeer()
        self.job = BackgroundAnalysis(self.session, 'revision', [{'id':'test','title':'Test'}], deck=4)
        self.job.acquire(self.peer)
        self.job.loaded = identity('test')
        self.peer.values['deck 4 loaded'] = True
        self.peer.commands.clear()
        s, peer, job = self.session, self.peer, self.job
        tracks = [{'id':'current','title':'Current'}, {'id':'test','title':'Test'}]
        self.c = {'version':1, 'ownerId':'a'*32, 'deck':3, 'selectionId':'b'*64,
                  'baseline':{'volume':1, 'pfl':False, 'effect':'Flanger'}, 'loadPending':True}
        s.update(active=True, phase='playing', resumeBlocked=True,
                 cleanupPending=merge_cleanup_pending({'analysis':[self.c]}), order=tracks[:1],
                 rollingQueue={'status':'preparing'},
                 queuedUpdate={'id':job.revision, 'tracks':tracks, 'status':'analyzing',
                               'rollingTrackId':'test', 'protectedIds':['current']})
        peer.values['deck 3 volume'] = 0
        peer.values['deck 1 play'] = True
        pool = PreparationPool(s, job.revision, tracks)
        pool.jobs[:] = [job]
        pool.pending.clear()
        job.done = True
        s.background_job = pool

    def lose_post_unload_reads(self, loss=OSCReplyTimeout, count=2):
        native_query = self.peer.query
        lost = []

        def query(script, kind=None):
            if len(lost) < count and script == 'deck 4 get_deck' and any(
                    command.endswith('deck 4 unload') for command in self.peer.commands):
                lost.append(script)
                raise loss('private raw timeout detail')
            return native_query(script, kind)

        self.peer.query = query
        return lost

    def test_verified_deferred_cleanup_reopens_refill_after_one_restore(self):
        lost = self.lose_post_unload_reads()
        s, peer = self.session, self.peer
        with patch('afterhours.vdj_session.time.monotonic', return_value=100) as clock:
            s.service_background(peer, 25)
            state = s.snapshot()
            self.assertEqual(state['backgroundAnalysis']['spareCleanup']['status'], 'waiting')
            self.assertEqual(state['queuedUpdate']['status'], 'blocked')
            self.assertNotIn('private raw timeout detail', json.dumps(state))
            self.assertEqual(s.preparation_decks(), ())
            clock.return_value = 101
            s.service_background(peer, 15)
            self.assertEqual(len(peer.commands), 1)
            s.service_background(peer, 25)
        state = s.snapshot()
        self.assertEqual(len(lost), 2)
        self.assertEqual(state['backgroundAnalysis']['spareCleanup']['status'], 'verified')
        self.assertEqual(len(peer.commands), 2)
        self.assertTrue(peer.commands[0].endswith('deck 4 unload'))
        self.assertIn('deck 4 volume 100.000000%', peer.commands[1])
        self.assertTrue(all(command.startswith('deck 4 ') for command in peer.commands))
        self.assertEqual(state['cleanupPending']['analysis'], [cleanup_descriptor(self.c)])
        self.assertTrue(state['resumeBlocked'])
        self.assertEqual(s.preparation_decks(), (4,))
        self.assertEqual(state['queuedUpdate']['status'], 'needs-analysis')
        self.assertEqual(state['queuedUpdate']['id'], self.job.revision)
        self.assertTrue(peer.values['deck 1 play'])

    def test_unconfirmed_restore_is_not_replayed(self):
        self.lose_post_unload_reads()
        s, peer = self.session, self.peer
        with patch('afterhours.vdj_session.time.monotonic', return_value=100) as clock:
            s.service_background(peer, 25)
            peer.fail_after_restore = True
            clock.return_value = 101
            s.service_background(peer, 25)
            clock.return_value = 102
            s.service_background(peer, 25)
        state = s.snapshot()
        d = next(row for row in state['cleanupPending']['analysis'] if row['deck'] == 4)
        self.assertEqual(state['backgroundAnalysis']['spareCleanup']['status'], 'unconfirmed')
        self.assertTrue(d['restoreRequested'])
        self.assertEqual(state['queuedUpdate']['status'], 'blocked')
        self.assertEqual(len(peer.commands), 2)

    def test_changed_or_stale_spare_is_not_restored(self):
        for reason in ('loaded', 'revision', 'ownership', 'expired', 'unknown', 'deck-c-changed'):
            with self.subTest(reason=reason):
                self.setUp()
                self.lose_post_unload_reads()
                s, peer = self.session, self.peer
                with patch('afterhours.vdj_session.time.monotonic', return_value=100) as clock:
                    s.service_background(peer, 25)
                    clock.return_value = 101
                    if reason == 'loaded':
                        peer.values.update({'deck 4 loaded':True, 'deck 4 get_filepath':'operator track'})
                    elif reason == 'revision':
                        s.state['queuedUpdate']['id'] = 'new revision'
                    elif reason == 'ownership':
                        s.state['cleanupPending']['analysis'][-1]['ownerId'] = 'd'*32
                    elif reason == 'unknown':
                        s.state['cleanupPending']['unknown'] = True
                    elif reason == 'deck-c-changed':
                        peer.values['deck 3 volume'] = .5
                    else:
                        clock.return_value = 106
                    s.service_background(peer, 25)
                self.assertIsNone(s.deferred_spare_cleanup)
                self.assertEqual(len(peer.commands), 1)
                self.assertEqual(s.state['queuedUpdate']['status'], 'blocked')
                self.assertTrue(s.state['resumeBlocked'])

    def test_untyped_cleanup_error_does_not_authorize_deferred_recovery(self):
        self.lose_post_unload_reads(BridgeError, count=1)
        self.session.service_background(self.peer, 25)
        self.assertIsNone(self.session.deferred_spare_cleanup)
        self.assertEqual(len(self.peer.commands), 1)
