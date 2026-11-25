"""Isolated ledger/closure behavior, without HTTP or engine integration."""
import copy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest

from afterhours.guest_requests import GuestError,GuestLedger
from afterhours.guest_polls import GuestPolls,MAX_POLLS,MAX_RECEIPTS


def track(number):
    return {'id':'virtualdj:'+format(number,'064x'),'title':f'Poll fixture {number}','artist':'Fixture Artist'}


class PollTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.now=[1000.];self.path=Path(self.tmp.name)/'ledger.sqlite'
        self.ledger=GuestLedger(self.path,clock=lambda:self.now[0]);self.addCleanup(lambda:self.ledger.close())
        self.ledger.ensure_night('fixture-night');self.polls=GuestPolls(self.ledger)
        self.guest,self.token=self.ledger.create_guest('fixture-night')
        self.scope={'origin':'http://127.0.0.1:19010','roomId':self.ledger.room_id,'nightId':'fixture-night','generation':'fixture-generation'}
        self.binding={'sessionId':'fixture-night','agentRevision':7,'agentGeneration':3,'contextHash':'a'*64}
        self.order=[track(i)['id'] for i in range(1,5)];self.options=[track(3),track(5),track(6)]
    def open(self,offer='offer-fixture',**change):
        return self.polls.open('fixture-night',offer,change.get('tracks',self.options),change.get('order',self.order),
            change.get('slot',2),change.get('binding',self.binding))
    def send(self,poll,number,command='vote-fixture-1',guest=None):
        payload={'requestId':command,'pollId':poll['id'],'trackId':track(number)['id']}
        return self.polls.vote('fixture-night',(guest or self.guest)['id'],payload,self.scope)
    def finish(self,poll,status='kept'):
        self.now[0]=poll['closesAt'];claim=self.polls.claim_due('fixture-night')
        self.assertIsNotNone(claim);self.assertTrue(self.polls.finish(claim,status));return claim
    def reopen(self):
        self.ledger.close();self.ledger=GuestLedger(self.path,clock=lambda:self.now[0]);self.polls=GuestPolls(self.ledger)
    def error(self,code,callback):
        with self.assertRaises(GuestError) as caught:callback()
        self.assertEqual(caught.exception.code,code)

    def test_real_window_stable_metadata_and_no_private_binding_in_projection(self):
        options=copy.deepcopy(self.options);options[0]['engine']={'filename':'private://never-public'}
        poll=self.open(tracks=options)
        self.assertEqual(poll['openedAt'],1000);self.assertEqual(poll['closesAt'],1060)
        self.assertNotIn('slotAt',poll);self.assertNotIn('chosenTrackId',poll)
        self.assertNotIn('binding',poll);self.assertNotIn('engine',poll['options'][0]['track'])
        self.assertEqual(poll['totalVotes'],0);self.assertEqual(poll['status'],'open')
        options[0]['title']='Caller changed title'
        self.assertEqual(self.polls.public('fixture-night',self.guest['id'])['options'][0]['track']['title'],'Poll fixture 3')

    def test_only_exact_distinct_options_with_original_incumbent_are_valid(self):
        for kwargs in [{'tracks':[track(3)]},{'tracks':[track(3),track(5),track(6),track(7)]},
                       {'tracks':[track(3),track(3)]},{'tracks':[track(5),track(6)]},
                       {'tracks':[track(3),track(2)]},{'slot':True},{'slot':20},
                       {'order':self.order+[self.order[0]]},{'binding':{**self.binding,'sessionId':'another-night'}},
                       {'binding':{**self.binding,'agentRevision':True}},{'binding':{**self.binding,'contextHash':'unknown'}}]:
            with self.assertRaises((ValueError,GuestError)):self.open(**kwargs)
        self.assertIsNone(self.polls.public('fixture-night',self.guest['id']))

    def test_repeated_offer_does_not_rearm_deadline_or_accept_changed_options(self):
        poll=self.open();revision=self.ledger.revision('fixture-night');self.now[0]+=10
        self.assertEqual(self.open(),poll);self.assertEqual(self.ledger.revision('fixture-night'),revision)
        with self.assertRaises(ValueError):self.open(tracks=[track(3),track(7)])
        with self.assertRaises(ValueError):self.open(offer='another-offer')
        self.finish(poll);self.assertEqual(self.open()['status'],'kept')

    def test_unique_guests_count_once_and_choice_moves_in_one_transaction(self):
        poll=self.open();other,_=self.ledger.create_guest('fixture-night')
        first=self.send(poll,5);same=self.send(poll,5,'vote-fixture-2')
        self.assertFalse(first['receipt']['duplicate']);self.assertTrue(same['receipt']['duplicate'])
        self.assertEqual(same['poll']['totalVotes'],1)
        moved=self.send(poll,6,'vote-fixture-3')
        self.assertFalse(moved['receipt']['duplicate']);self.assertEqual(moved['poll']['totalVotes'],1)
        self.send(poll,5,'other-vote-01',other)
        view=self.polls.public('fixture-night',self.guest['id'])
        self.assertEqual({r['track']['id']:r['votes'] for r in view['options']},
                         {track(3)['id']:0,track(5)['id']:1,track(6)['id']:1})
        self.assertEqual(view['totalVotes'],2);self.assertEqual(view['myChoiceId'],track(6)['id'])
        self.assertEqual(self.polls.public('fixture-night',other['id'])['myChoiceId'],track(5)['id'])
        self.assertEqual(self.ledger.own('fixture-night',self.guest['id']),([],0))

    def test_replayed_receipt_freezes_accepted_change_and_revision_after_later_vote(self):
        poll=self.open();first=self.send(poll,5);self.send(poll,6,'vote-fixture-2')
        replay=self.send(poll,5);lookup=self.polls.receipt('fixture-night',self.guest['id'],'vote-fixture-1',self.scope)
        self.assertEqual(replay['receipt'],first['receipt']);self.assertEqual(lookup['receipt'],first['receipt'])
        self.assertEqual(replay['poll']['myChoiceId'],track(6)['id']);self.assertEqual(replay['poll']['totalVotes'],1)
        self.assertGreater(replay['poll']['revision'],replay['receipt']['pollRevision'])
        self.error('command-conflict',lambda:self.send(poll,6))

    def test_duplicate_command_after_close_returns_receipt_without_another_vote(self):
        poll=self.open();first=self.send(poll,5);self.finish(poll,'applied')
        replay=self.send(poll,5);self.assertEqual(replay['receipt'],first['receipt'])
        self.assertEqual(replay['poll']['status'],'applied');self.assertEqual(replay['poll']['totalVotes'],1)
        self.error('poll-closed',lambda:self.send(poll,6,'new-after-close'))

    def test_tie_and_zero_votes_keep_incumbent_without_claiming_audio_play(self):
        poll=self.open();self.send(poll,5)
        other,_=self.ledger.create_guest('fixture-night');self.send(poll,6,'other-guest-vote',other)
        claim=self.finish(poll);self.assertEqual(claim['choiceId'],track(3)['id'])
        view=self.polls.public('fixture-night',self.guest['id']);self.assertEqual(view['chosenTrackId'],track(3)['id'])
        self.assertNotIn('playedAt',view);self.assertNotIn('playing',view)
        second=self.open('zero-votes-offer');claim=self.finish(second);self.assertEqual(claim['choiceId'],track(3)['id'])

    def test_unique_highest_vote_reserved_once_with_stable_private_context(self):
        poll=self.open();self.send(poll,6);self.assertIsNone(self.polls.claim_due('fixture-night'))
        self.now[0]=1060;claim=self.polls.claim_due('fixture-night')
        self.assertEqual(claim['choiceId'],track(6)['id']);self.assertEqual(claim['baseOrder'],self.order)
        self.assertEqual(claim['binding'],self.binding);self.assertIsNone(self.polls.claim_due('fixture-night'))
        closing=self.polls.public('fixture-night',self.guest['id'])
        self.assertEqual(closing['status'],'open');self.assertNotIn('chosenTrackId',closing)
        self.error('poll-closed',lambda:self.send(poll,5,'during-closure'))
        with self.assertRaises(ValueError):self.polls.finish(claim,'kept')
        self.assertFalse(self.polls.finish({**claim,'claimId':'wrong-claim'},'applied'))
        self.assertTrue(self.polls.finish(claim,'applied'));revision=self.ledger.revision('fixture-night')
        self.assertFalse(self.polls.finish(claim,'applied'));self.assertEqual(self.ledger.revision('fixture-night'),revision)

    def test_concurrent_votes_preserve_one_per_guest_and_exact_sum(self):
        poll=self.open();guests=[self.ledger.create_guest('fixture-night')[0] for _ in range(20)]
        def vote(index):
            guest=guests[index%20];self.send(poll,5 if index%3 else 6,'parallel-'+str(index).zfill(4),guest)
        with ThreadPoolExecutor(max_workers=8) as executor:list(executor.map(vote,range(80)))
        view=self.polls.public('fixture-night',self.guest['id'])
        self.assertEqual(view['totalVotes'],20);self.assertEqual(sum(r['votes'] for r in view['options']),20)
        self.assertNotIn('myChoiceId',view)

    def test_competing_deadline_claims_return_one_owner(self):
        poll=self.open();self.send(poll,5);self.now[0]=poll['closesAt']
        with ThreadPoolExecutor(max_workers=8) as executor:
            claims=list(executor.map(lambda _:self.polls.claim_due('fixture-night'),range(20)))
        self.assertEqual(sum(c is not None for c in claims),1)

    def test_deadline_rejection_does_not_create_vote_or_receipt(self):
        poll=self.open();self.now[0]=poll['closesAt']
        self.error('poll-closed',lambda:self.send(poll,5))
        self.assertIsNone(self.polls.receipt('fixture-night',self.guest['id'],'vote-fixture-1',self.scope))
        self.assertEqual(self.polls.public('fixture-night',self.guest['id'])['totalVotes'],0)

    def test_wrong_option_guest_and_night_cannot_vote_or_read_receipts(self):
        poll=self.open();self.error('option-unavailable',lambda:self.send(poll,99))
        self.error('guest-expired',lambda:self.send(poll,5,guest={'id':'nonexistent-guest'}))
        self.send(poll,5);other,_=self.ledger.create_guest('fixture-night')
        self.assertIsNone(self.polls.receipt('fixture-night',other['id'],'vote-fixture-1',self.scope))
        self.ledger.ensure_night('next-night')
        self.error('night-closed',lambda:self.send(poll,6,'old-night-vote'))
        self.error('night-closed',lambda:self.polls.claim_due('fixture-night'))

    def test_restart_interrupts_open_or_claimed_poll_and_never_replays_adoption(self):
        for close in (False,True):
            poll=self.open('restart-offer-'+str(close));self.send(poll,5,'restart-vote-'+str(close))
            if close:self.now[0]=poll['closesAt'];claim=self.polls.claim_due('fixture-night')
            self.reopen();view=self.polls.public('fixture-night',self.guest['id'])
            self.assertEqual(view['status'],'interrupted');self.assertNotIn('chosenTrackId',view)
            self.assertIsNone(self.polls.claim_due('fixture-night'));self.assertEqual(self.open('restart-offer-'+str(close))['status'],'interrupted')
            if close:self.assertFalse(self.polls.finish(claim,'applied'))
            self.now[0]+=61

    def test_finished_poll_and_receipt_survive_restart_and_later_poll(self):
        poll=self.open();first=self.send(poll,5);self.finish(poll,'applied');self.reopen()
        next_poll=self.open('later-offer-fixture')
        self.assertEqual(self.polls.public('fixture-night',self.guest['id'])['id'],next_poll['id'])
        receipt=self.polls.receipt('fixture-night',self.guest['id'],'vote-fixture-1',self.scope)
        self.assertEqual(receipt['receipt'],first['receipt']);self.assertEqual(receipt['poll']['id'],poll['id'])
        self.assertEqual(receipt['poll']['status'],'applied')

    def test_cancel_and_uncertain_adoption_have_no_chosen_record_and_no_retry(self):
        poll=self.open();self.assertTrue(self.polls.cancel_open('fixture-night','changed'))
        self.assertFalse(self.polls.cancel_open('fixture-night'))
        view=self.polls.public('fixture-night',self.guest['id']);self.assertEqual(view['status'],'cancelled');self.assertNotIn('chosenTrackId',view)
        poll=self.open('uncertain-offer');self.send(poll,5);self.now[0]=poll['closesAt'];claim=self.polls.claim_due('fixture-night')
        self.assertFalse(self.polls.cancel_open('fixture-night')) # The single claim owner retains responsibility.
        self.assertTrue(self.polls.finish(claim,'cancelled','unconfirmed'));self.assertIsNone(self.polls.claim_due('fixture-night'))
        view=self.polls.public('fixture-night',self.guest['id']);self.assertNotIn('chosenTrackId',view)
        self.assertIn('will not be replayed',view['reason'])

    def test_receipt_capacity_preserves_prior_replay_and_vote_totals(self):
        poll=self.open()
        for index in range(MAX_RECEIPTS):self.send(poll,5 if index%2 else 6,'capacity-'+str(index).zfill(4))
        self.error('capacity',lambda:self.send(poll,5,'too-many-votes'))
        self.assertEqual(self.send(poll,6,'capacity-0000')['receipt']['change']['trackId'],track(6)['id'])
        self.assertEqual(self.polls.public('fixture-night',self.guest['id'])['totalVotes'],1)

    def test_poll_capacity_prevents_unbounded_night_history(self):
        for index in range(MAX_POLLS):
            poll=self.open('capacity-offer-'+str(index));self.finish(poll);self.now[0]+=1
        self.error('capacity',lambda:self.open('excess-offer'))
        self.assertEqual(self.polls.open('fixture-night','capacity-offer-0',self.options,self.order,2,self.binding)['status'],'kept')

    def test_payload_unknown_keys_and_raw_locators_rejected_without_partial_write(self):
        poll=self.open();body={'requestId':'valid-command','pollId':poll['id'],'trackId':track(5)['id']}
        for payload in [{**body,'approve':True},{**body,'trackId':'netsearch://private'},
                        {**body,'requestId':'bad space'}, {**body,'pollId':'../private'}]:
            self.error('invalid-request',lambda:self.polls.vote('fixture-night',self.guest['id'],payload,self.scope))
        self.assertEqual(self.polls.public('fixture-night',self.guest['id'])['totalVotes'],0)


if __name__=='__main__':unittest.main()
