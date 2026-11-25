import copy
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from afterhours.guest_requests import GuestLedger,GuestError,track_ref
from afterhours.guest_protocol import GuestProtocol
from afterhours.record_crate import RecordCrate
from afterhours.recording_availability import RecordingAvailability
from test_provider_eligibility import restrict


def records(count=8):
    return [{'id':'virtualdj:'+format(i+1,'064x'),'title':f'Fixture record {i}',
             'artist':'Fixture artist','artworkUrl':'https://art.example.test/record.png',
             'engine':{'selection_id':format(i+1,'064x'),'private':'never public'}} for i in range(count)]


class GuestLedgerTests(unittest.TestCase):
    def setUp(self):
        self.now=[1000.];self.ledger=GuestLedger(clock=lambda:self.now[0]);self.addCleanup(self.ledger.close)
        self.ledger.ensure_night('night-a');self.g,self.token=self.ledger.create_guest('night-a')
        self.scope={'origin':'http://127.0.0.1:8769','roomId':self.ledger.room_id,'nightId':'night-a','generation':'invite-a'}
    def ask(self,index,command=None,guest=None):
        track=records()[index]
        payload={'requestId':command or 'command-'+str(index),'searchId':'search-fixture','trackId':track['id'],'follow':False}
        return self.ledger.ask('night-a',(guest or self.g)['id'],payload,track,self.scope)
    def test_same_guest_and_command_dedupe_and_other_guest_increments_once(self):
        first=self.ask(0);again=self.ask(0)
        self.assertEqual(first,again);self.now[0]+=10
        duplicate=self.ask(0,'another-command')
        self.assertTrue(duplicate['receipt']['duplicate']);self.assertEqual(duplicate['request']['asks'],1)
        self.assertEqual(duplicate['request']['firstAt'],first['request']['firstAt'])
        other,_=self.ledger.create_guest('night-a');result=self.ask(0,'other-command',other)
        self.assertEqual(result['request']['asks'],2);self.assertEqual(result['request']['id'],first['request']['id'])
        self.assertEqual(self.ledger.own('night-a',self.g['id'])[1],1)
    def test_simultaneous_fourth_request_and_same_record_are_atomic(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            result=list(pool.map(lambda _:self.ask(0,'same-command'),range(8)))
        self.assertTrue(all(r==result[0] for r in result))
        def send(i):
            try:return self.ask(i)
            except GuestError as exc:return exc.code
        with ThreadPoolExecutor(max_workers=4) as pool:result=list(pool.map(send,range(1,5)))
        self.assertEqual(sum(isinstance(row,dict) for row in result),2)
        self.assertEqual(result.count('open-limit'),2)
        self.assertEqual(self.ledger.own('night-a',self.g['id'])[1],3)
    def test_receipt_conflict_and_cross_guest_lookup_never_expose_another_request(self):
        self.ask(0,'same-command')
        with self.assertRaisesRegex(GuestError,'different choice'):self.ask(1,'same-command')
        other,_=self.ledger.create_guest('night-a')
        self.assertIsNone(self.ledger.receipt('night-a',other['id'],'same-command',self.scope))
    def test_restart_preserves_hashed_credentials_receipt_and_resets_only_uncertain_listening(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'guests.sqlite';ledger=GuestLedger(path,clock=lambda:self.now[0]);ledger.ensure_night('night-a')
            guest,token=ledger.create_guest('night-a');payload={'requestId':'first-command','searchId':'a','trackId':records()[0]['id'],'follow':False}
            result=ledger.ask('night-a',guest['id'],payload,records()[0],self.scope)
            ledger.stage('night-a',result['request']['id'],'listening',round_id='round-a');ledger.close()
            self.assertNotIn(token.encode(),path.read_bytes());self.assertEqual(path.stat().st_mode&0o777,0o600)
            ledger=GuestLedger(path,clock=lambda:self.now[0])
            try:
                self.assertEqual(ledger.authenticate('night-a',token),guest)
                loaded=ledger.receipt('night-a',guest['id'],'first-command',self.scope)
                self.assertEqual(loaded['receipt'],result['receipt']);self.assertEqual(loaded['request']['stage'],'sent')
            finally:ledger.close()
    def test_terminal_result_releases_quota_and_repeated_ask_does_not_reset_verdict(self):
        first=self.ask(0)
        self.ask(1);self.ask(2)
        self.ledger.stage('night-a',first['request']['id'],'declined',reason='not-tonight')
        self.ask(3)
        repeated=self.ask(0,'repeat-terminal')
        self.assertEqual(repeated['request']['stage'],'declined');self.assertEqual(repeated['request']['asks'],1)
        self.assertEqual(self.ledger.own('night-a',self.g['id'])[1],3)
    def test_new_night_closes_old_requests_and_old_token_cannot_cross(self):
        self.ask(0);self.ledger.ensure_night('night-b')
        old=self.ledger.operator_page('night-a')['requests'][0]
        self.assertEqual(old['stage'],'declined');self.assertEqual(old['reason'],'This night has ended.')
        with self.assertRaises(GuestError):self.ledger.authenticate('night-b',self.token)
        with self.assertRaises(GuestError):self.ledger.ensure_night('night-a')
        # A delayed old-night worker cannot close the new night.
        self.ledger.create_guest('night-b')
    def test_public_tracks_strip_private_fields_and_bad_artwork(self):
        track=records()[0];track['artworkUrl']='file:///private/token';track['artist']=''
        self.assertEqual(track_ref(track),{'id':track['id'],'title':track['title'],'artist':'Unknown artist'})


class GuestProtocolTests(unittest.TestCase):
    def setUp(self):
        self.now=[1000.];self.night=['night-a'];self.ledger=GuestLedger(clock=lambda:self.now[0]);self.addCleanup(self.ledger.close)
        self.crate=RecordCrate();self.addCleanup(self.crate.close);self.crate.remember(records())
        self.availability=RecordingAvailability(clock=lambda:self.now[0]);self.wake=Mock()
        self.api=GuestProtocol(self.ledger,self.crate,self.availability,lambda:{'nightId':self.night[0]},
            lambda:self.night[0],clock=lambda:self.now[0],on_request=self.wake)
        self.api.activate('http://127.0.0.1:8769','generation-a',self.night[0])
        self.session=self.api.handle('POST','/session',{}, {'requestId':'bootstrap-a'},None,'peer')
        self.token=self.session['guestToken']
    def request(self,method,route,query=None,payload=None,token=None):
        return self.api.handle(method,route,query or {},payload,token or self.token,'peer')
    def search(self):return self.request('GET','/search',{'q':['Fixture']})
    def send(self,search,index=0,command='request-0001',follow=False):
        payload={'requestId':command,'searchId':search['searchId'],'trackId':search['records'][index]['id'],'follow':follow}
        return self.request('POST','/requests',payload=payload)
    def test_exact_search_bound_and_receipt_survives_search_expiry_without_another_ask(self):
        search=self.search();self.assertEqual(len(search['records']),5)
        self.assertEqual(search['expiresAt']-self.now[0],120)
        first=self.send(search,follow=True);self.now[0]+=121
        again=self.send(search,follow=True)
        self.assertEqual(first['receipt'],again['receipt']);self.wake.assert_called_once()
        self.assertTrue(again['request']['followed']);self.assertEqual(again['follow']['status'],'configuration-required')
        with self.assertRaises(GuestError) as error:self.send(search,index=1,command='request-0002')
        self.assertEqual(error.exception.code,'selection-expired')
    def test_current_exact_restriction_rechecked_and_no_other_variant_selected(self):
        search=self.search();restrict(self.availability,search['records'][0]['id'])
        with self.assertRaises(GuestError) as error:self.send(search)
        self.assertEqual(error.exception.code,'recording-unavailable');self.wake.assert_not_called()
        self.assertNotIn(search['records'][0]['id'],[r['id'] for r in self.search()['records']])
        self.assertEqual(self.request('GET','/state')['requests'],[])
    def test_every_request_view_uses_fresh_exact_program_without_rewriting_receipt(self):
        search=self.search();accepted=self.send(search);request_id=accepted['request']['id']
        self.ledger.stage('night-a',request_id,'playing',playing=True)
        facts={'nightId':'night-a','program':{'track':search['records'][0],'playing':True,'sampledAt':1000.}}
        self.api.context=lambda:copy.deepcopy(facts)
        for label,now,changes,expected in [
                ('fresh',1000.,{},True),('stale',1005.,{},False),
                ('other recording',1000.,{'track':search['records'][1]},False),
                ('paused',1000.,{'playing':False},False),('future',990.,{},False)]:
            with self.subTest(label=label):
                self.now[0]=now
                facts['program']={'track':search['records'][0],'playing':True,'sampledAt':1000.,**changes}
                state=self.request('GET','/state')
                lookup=self.request('GET','/receipts/request-0001');duplicate=self.send(search)
                for view in (state['requests'][0],lookup['request'],duplicate['request']):
                    self.assertEqual(view['playingNow'],expected);self.assertEqual(view['stage'],'playing')
                    self.assertEqual(view['playedAt'],1000.)
                self.assertEqual(lookup['receipt'],accepted['receipt']);self.assertEqual(duplicate['receipt'],accepted['receipt'])
        self.now[0]=1005.;facts['program']={'track':search['records'][0],'playing':True,'sampledAt':1000.}
        # A new command for the same terminal request also projects its current
        # state, while retaining the new command's immutable acceptance receipt.
        added=self.send(search,command='same-track-new-command')
        self.assertFalse(added['request']['playingNow']);self.assertEqual(added['request']['stage'],'playing')
        bootstrap=self.api.handle('POST','/session',{}, {'requestId':'bootstrap-a'},None,'peer')
        self.assertEqual(bootstrap['guestToken'],self.token);self.assertIsNone(bootstrap['program'])
        self.assertFalse(bootstrap['requests'][0]['playingNow'])
    def test_request_time_requires_current_matching_program_promise_on_all_views(self):
        search=self.search();accepted=self.send(search);rid=accepted['request']['id']
        self.ledger.stage('night-a',rid,'coming',at=1020.)
        facts={'nightId':'night-a','program':{'track':search['records'][1],'playing':True,'sampledAt':1000.},
            'next':{'track':search['records'][0],'estimatedAt':1020.}}
        self.api.context=lambda:copy.deepcopy(facts)
        self.assertEqual(self.request('GET','/receipts/request-0001')['request']['at'],1020.)
        self.now[0]=1005.
        for response in (self.request('GET','/state')['requests'][0],self.send(search)['request'],
                         self.request('GET','/receipts/request-0001')['request']):
            self.assertNotIn('at',response);self.assertEqual(response['stage'],'coming')
        self.now[0]=1000.;facts.pop('next')  # held preparation has no next promise
        self.assertNotIn('at',self.request('GET','/receipts/request-0001')['request'])
        facts['nightId']='other-night'
        self.assertNotIn('at',self.send(search)['request'])
    def test_selection_binding_cannot_cross_guests_generation_or_exact_ids(self):
        search=self.search()
        other=self.api.handle('POST','/session',{}, {'requestId':'bootstrap-b'},None,'peer')
        payload={'requestId':'request-other','searchId':search['searchId'],'trackId':search['records'][0]['id'],'follow':False}
        with self.assertRaises(GuestError):self.request('POST','/requests',payload=payload,token=other['guestToken'])
        self.api.activate('http://127.0.0.1:8769','generation-b','night-a')
        with self.assertRaises(GuestError) as error:self.send(search)
        self.assertEqual(error.exception.code,'selection-expired')
        resumed=self.request('POST','/session',payload={'requestId':'resume-guest'})
        self.assertNotIn('guestToken',resumed);self.assertEqual(resumed['guest']['id'],self.session['guest']['id'])
    def test_two_shared_ip_guests_can_each_send_three_without_legacy_cooldown(self):
        for token in [self.token,self.api.handle('POST','/session',{}, {'requestId':'bootstrap-b'},None,'peer')['guestToken']]:
            search=self.request('GET','/search',{'q':['Fixture']},token=token)
            for i in range(3):
                payload={'requestId':f'request-{i:04}','searchId':search['searchId'],'trackId':search['records'][i]['id'],'follow':False}
                self.request('POST','/requests',payload=payload,token=token)
        rows=self.ledger.operator_page('night-a')['requests']
        self.assertEqual([r['asks'] for r in rows],[2,2,2])
    def test_invalid_bearer_cannot_bootstrap_new_guest_and_old_night_fails(self):
        with self.assertRaises(GuestError):self.request('POST','/session',payload={'requestId':'bad-bearer'},token='invalid')
        self.night[0]='night-b'
        with self.assertRaises(GuestError) as error:self.search()
        self.assertEqual(error.exception.code,'invitation-closed')
    def test_no_private_fields_or_unsupported_poll_claims(self):
        self.send(self.search());state=self.request('GET','/state');raw=json.dumps(state)
        self.assertFalse(state['capabilities']['poll']);self.assertFalse(state['capabilities']['liveActivity'])
        self.assertNotIn('poll',state);self.assertIsNone(state['program'])
        for private in ('selection_id','never public','X-DJ-Token','engineChecked','netsearch://'):self.assertNotIn(private,raw)


if __name__=='__main__':unittest.main()
