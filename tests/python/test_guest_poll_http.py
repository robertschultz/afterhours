"""Disposable HTTP plus actual guarded agent adoption; no native/provider IO."""
import copy
import json
import unittest
from unittest.mock import Mock

from afterhours import crowd_choice
from afterhours.guest_protocol import GuestProtocol
from afterhours.guest_polls import GuestPolls
from afterhours.room_requests import RoomRequests
import test_crowd_choice
import test_guest_http
from test_dj_agent import decision,settled
from test_provider_eligibility import restrict


class GuestPollHTTPTests(unittest.TestCase):
    call=test_guest_http.GuestHTTPTests.call
    bootstrap=test_guest_http.GuestHTTPTests.bootstrap

    def setUp(self):
        self.engine=test_crowd_choice.CrowdChoiceTests();self.engine.setUp();self.addCleanup(self.engine.doCleanups)
        e=self.engine;self.now=e.now;self.support=[True];self.receive=Mock()
        self.api=GuestProtocol(e.ledger,e.s.crate,e.s.availability,e.guest_worker.context,e.guest_worker.night,
            clock=lambda:self.now[0],polls=e.polls,poll_supported=lambda:self.support[0] and e.worker.supported)
        e.worker.scope_generation=lambda:(self.api.scope or {}).get('generation')
        self.room=RoomRequests(self.receive,resolve_host=lambda:'127.0.0.1',protocol=self.api);self.addCleanup(self.room.close)
        self.assertFalse(self.room.snapshot()['enabled']);self.url=self.room.configure(True,port=0)['url']

    def start(self):
        boot=self.bootstrap();self.token=boot['guestToken'];self.engine.open()
        status,state=self.call('/state',token=self.token);self.assertEqual(status,200)
        self.poll=state['poll'];return boot,state

    def body(self,command='vote-http-01',index=1):
        return {'requestId':command,'pollId':self.poll['id'],'trackId':self.engine.options[index]}

    def test_actual_http_guarded_vote_contract_and_immutable_receipt(self):
        fixture=self.contract_fixture()
        self.assertEqual(fixture['finalState']['poll']['status'],'applied')
        self.assertEqual(fixture['accepted']['receipt'],fixture['oldReceipt']['receipt'])
        self.assertNotEqual(fixture['oldReceipt']['receipt']['change']['trackId'],fixture['oldReceipt']['poll']['myChoiceId'])
        self.assertEqual(fixture['interruptedState']['poll']['status'],'interrupted')
        self.receive.assert_not_called();self.engine.no_audio()
        public=json.dumps(fixture)
        for private in ('contextHash','agentGeneration','baseOrder','selection_id','claimId'):
            self.assertNotIn(private,public)

    def contract_fixture(self):
        boot,opened=self.start();command=self.body()
        status,accepted=self.call('/votes',command,self.token);self.assertEqual(status,200)
        changed=self.body('vote-http-02',2)
        status,moved=self.call('/votes',changed,self.token);self.assertEqual(status,200)
        _,old=self.call('/vote-receipts/'+command['requestId'],token=self.token)
        _,replayed=self.call('/votes',command,self.token)
        status,conflict=self.call('/votes',{**command,'trackId':changed['trackId']},self.token);self.assertEqual(status,409)
        status,nonoption=self.call('/votes',{**command,'requestId':'non-option','trackId':self.engine.ids[3]},self.token)
        self.assertEqual(status,409)
        status,unknown=self.call('/vote-receipts/never-submitted',token=self.token);self.assertEqual(status,404)
        self.engine.close()
        _,final=self.call('/state',token=self.token)
        _,after_close=self.call('/vote-receipts/'+changed['requestId'],token=self.token)
        status,closed=self.call('/votes',self.body('late-new-vote',0),self.token);self.assertEqual(status,409)
        _,after_deadline_replay=self.call('/votes',command,self.token)
        # A second explicit synthetic authorized round supplies the next poll;
        # it is never inferred by the public API or a selectionReview.
        base=crowd_choice.order_ids(self.engine.s.snapshot())
        self.engine.answer={**decision(base),'crowdChoice':{'slot':2,
            'trackIds':[base[2],self.engine.ids[4],self.engine.ids[2]]}}
        self.engine.agent.state['status']='pending'
        self.engine.agent.tick(force=True);settled(self.engine.agent);self.engine.guest_worker.step()
        _,second=self.call('/state',token=self.token);self.assertEqual(second['poll']['status'],'open')
        self.assertNotEqual(second['poll']['id'],final['poll']['id'])
        # Constructor recovery is the same durable interruption code used on
        # restart. The installed process and all native audio are untouched.
        reopened=GuestPolls(self.engine.ledger);self.api.polls=reopened
        _,interrupted=self.call('/state',token=self.token)
        return {'version':1,'basis':'Actual disposable loopback HTTP; real DJAgent/DJSession queue validation with synthetic measured tracks, fake evaluator and clock. Constructor recovery tested on the same disposable ledger. Zero native/provider calls.',
            'invitationURL':self.url,'scope':boot['scope'],
            'actions':{'pollId':self.poll['id'],'incumbentTrackId':self.engine.options[0],
                       'firstChoiceTrackId':command['trackId'],'movedChoiceTrackId':changed['trackId']},
            'bootstrap':boot,'open':opened,'voteBody':command,'accepted':accepted,'movedBody':changed,
            'moved':moved,'oldReceipt':old,'replayed':replayed,'conflictError':conflict,
            'nonOptionError':nonoption,'unknownReceipt':unknown,'finalState':final,
            'finalReceipt':after_close,'closedError':closed,'replayedAfterDeadline':after_deadline_replay,
            'secondOpen':second,'interruptedState':interrupted}

    def test_disabled_capability_hides_poll_and_routes_even_with_valid_guest(self):
        self.support[0]=False;boot=self.bootstrap();token=boot['guestToken']
        self.assertFalse(boot['capabilities']['poll']);self.assertNotIn('poll',boot)
        self.assertEqual(self.call('/votes',{'requestId':'a','pollId':'a','trackId':self.engine.ids[0]},token)[0],404)
        self.assertEqual(self.call('/vote-receipts/a',token=token)[0],404)
        self.engine.evaluator.assert_not_called();self.engine.no_audio()

    def test_shared_ip_votes_are_unique_per_guest_and_independent_of_three_ask_slots(self):
        _,state=self.start();other=self.bootstrap('bootstrap-other');other_token=other['guestToken']
        for index in range(12):
            status,_=self.call('/votes',self.body('http-vote-'+str(index),1+(index%2)),self.token)
            self.assertEqual(status,200)
        status,limited=self.call('/votes',self.body('vote-over-limit'),self.token)
        self.assertEqual((status,limited['code']),(429,'rate-limited'))
        status,result=self.call('/votes',self.body('other-guest'),other_token);self.assertEqual(status,200)
        self.assertEqual(result['poll']['totalVotes'],2)
        _,current=self.call('/state',token=self.token);self.assertEqual(current['guest']['openCount'],0)
        _,search=self.call('/search?q=Selected',token=self.token)
        for index in range(3):
            status,_=self.call('/requests',{'requestId':'http-ask-'+str(index),'searchId':search['searchId'],
                'trackId':search['records'][index]['id'],'follow':False},self.token)
            self.assertEqual(status,200)
        _,current=self.call('/state',token=self.token)
        self.assertEqual(current['guest']['openCount'],3);self.assertEqual(current['poll']['totalVotes'],2)
        self.assertEqual(self.engine.evaluator.call_count,1);self.engine.no_audio()

    def test_receipts_are_authenticated_scoped_and_do_not_disclose_other_guest_choice(self):
        self.start();command=self.body();self.assertEqual(self.call('/votes',command,self.token)[0],200)
        other=self.bootstrap('bootstrap-other');token=other['guestToken']
        self.assertEqual(self.call('/vote-receipts/'+command['requestId'],token=token)[0],404)
        self.assertEqual(self.call('/votes',command)[0],401)
        self.assertEqual(self.call('/vote-receipts/'+command['requestId'])[0],401)
        _,state=self.call('/state',token=token);self.assertNotIn('myChoiceId',state['poll'])
        self.assertEqual(state['poll']['totalVotes'],1)
        status,value=self.call('/votes',command,self.token,headers={'Origin':'https://other.test'})
        self.assertEqual(status,403);self.engine.no_audio()

    def test_new_known_restriction_rejects_new_vote_but_preserves_original_receipt(self):
        self.start();command=self.body();_,first=self.call('/votes',command,self.token)
        restrict(self.engine.s.availability,command['trackId'])
        status,blocked=self.call('/votes',{**command,'requestId':'new-vote-same-option'},self.token)
        self.assertEqual((status,blocked['code']),(409,'option-unavailable'))
        _,duplicate=self.call('/votes',command,self.token);self.assertEqual(duplicate['receipt'],first['receipt'])
        self.engine.guest_worker.step();_,state=self.call('/state',token=self.token)
        self.assertEqual(state['poll']['status'],'cancelled');self.assertNotIn('chosenTrackId',state['poll'])
        self.assertEqual(crowd_choice.order_ids(self.engine.s.snapshot()),self.engine.base);self.engine.no_audio()

    def test_close_reopen_rotates_scope_and_cancels_old_slot_without_replaying_queue(self):
        boot,_=self.start();self.call('/votes',self.body(),self.token)
        before=copy.deepcopy(self.engine.s.state['queuedUpdate'])
        self.room.close();self.url=self.room.configure(True,port=0)['url']
        self.assertNotEqual(self.api.scope['generation'],boot['scope']['generation'])
        self.engine.guest_worker.step()
        _,state=self.call('/state',token=self.token)
        self.assertEqual(state['poll']['status'],'cancelled');self.assertNotIn('chosenTrackId',state['poll'])
        self.assertEqual(self.engine.s.state['queuedUpdate'],before);self.engine.no_audio()

    def test_late_first_write_unknown_receipt_and_malformed_commands_do_not_adopt(self):
        self.start();before=copy.deepcopy(self.engine.s.state['queuedUpdate'])
        for body in ({**self.body(),'approve':True},{**self.body(),'trackId':'title:Selected 4'},
                     {**self.body(),'requestId':'x'*129}):
            self.assertEqual(self.call('/votes',body,self.token)[0],400)
        self.now[0]=self.poll['closesAt']
        status,late=self.call('/votes',self.body(),self.token)
        self.assertEqual((status,late['code']),(409,'poll-closed'))
        self.assertEqual(self.call('/vote-receipts/'+self.body()['requestId'],token=self.token)[0],404)
        self.assertEqual(self.engine.s.state['queuedUpdate'],before);self.engine.no_audio()


if __name__=='__main__':unittest.main()
