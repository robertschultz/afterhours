"""Pure metadata worker tests; no HTTP, native/provider or audio actions."""
import copy
import unittest
from unittest.mock import Mock

from afterhours.guest_requests import GuestLedger
from afterhours.guest_polls import GuestPolls
from afterhours.guest_poll_coordinator import PollCoordinator


def track(n):return {'id':'virtualdj:'+format(n,'064x'),'title':f'Worker fixture {n}','artist':'Fixture Artist'}


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.now=[1000.];self.night=['night-fixture'];self.enabled=[True];self.valid=[True]
        self.ledger=GuestLedger(clock=lambda:self.now[0]);self.addCleanup(self.ledger.close)
        self.ledger.ensure_night(self.night[0]);self.polls=GuestPolls(self.ledger)
        self.guest,_=self.ledger.create_guest(self.night[0]);self.scope={'origin':'http://127.0.0.1:19012','roomId':self.ledger.room_id,'nightId':self.night[0],'generation':'fixture'}
        self.binding={'sessionId':self.night[0],'agentRevision':1,'agentGeneration':1,'contextHash':'a'*64}
        self.order=[track(n)['id'] for n in (1,2,3,4)];self.tracks=[track(3),track(5),track(6)]
        self.verify=Mock(side_effect=lambda _:self.valid[0]);self.adopt=Mock(side_effect=self.accept)
        self.worker=PollCoordinator(self.polls,lambda:self.night[0],lambda:self.enabled[0],self.verify,self.adopt)
    def accept(self,claim):
        order=claim['baseOrder'][:];order[claim['slot']]=claim['choiceId']
        return {'status':'kept' if claim['choiceId']==claim['incumbentId'] else 'applied','sessionId':claim['nightId'],
                'trackId':claim['choiceId'],'orderIds':order}
    def submit(self,name='offer-fixture'):
        return self.worker.submit(self.night[0],name,self.tracks,self.order,2,self.binding)
    def view(self):return self.polls.public(self.night[0],self.guest['id'])
    def choose(self,n=5):
        poll=self.view();self.polls.vote(self.night[0],self.guest['id'],{'requestId':'vote-fixture','pollId':poll['id'],'trackId':track(n)['id']},self.scope)
    def due(self):self.now[0]=self.view()['closesAt'];self.worker.step()

    def test_offer_enqueue_has_no_db_or_engine_callbacks(self):
        self.assertTrue(self.submit());self.verify.assert_not_called();self.adopt.assert_not_called()
        self.assertIsNone(self.view());self.worker.step();self.assertEqual(self.view()['status'],'open')
        self.assertEqual(self.view()['totalVotes'],0);self.adopt.assert_not_called()

    def test_capability_requires_both_real_guard_and_adoption_callbacks(self):
        worker=PollCoordinator(self.polls,lambda:self.night[0],lambda:True)
        self.assertFalse(worker.supported);self.assertFalse(worker.submit(self.night[0],'offer-fixture',self.tracks,self.order,2,self.binding))
        worker.step();self.assertIsNone(self.view())

    def test_event_bound_and_stale_offers_are_discarded_without_replacement(self):
        for n in range(self.worker.MAX_EVENTS):self.assertTrue(self.submit('offer-fixture-'+str(n)))
        self.assertFalse(self.submit('offer-overflow'))
        self.valid[0]=False;self.worker.step();self.assertIsNone(self.view());self.adopt.assert_not_called()
        self.assertEqual(len(self.worker.events),0)

    def test_two_new_offers_keep_first_poll_id_and_deadline(self):
        self.submit('first-offer');self.submit('second-offer');self.worker.step();first=self.view()
        self.now[0]+=5;self.submit('third-offer');self.worker.step()
        self.assertEqual(self.view(),first);self.adopt.assert_not_called()

    def test_real_deadline_adopts_once_and_receipt_is_exact_full_order(self):
        self.submit();self.worker.step();self.choose();self.worker.step();self.adopt.assert_not_called()
        self.due();self.assertEqual(self.view()['status'],'applied');self.assertEqual(self.view()['chosenTrackId'],track(5)['id'])
        self.worker.step();self.worker.step();self.assertEqual(self.adopt.call_count,1)
        self.assertEqual(self.ledger.own(self.night[0],self.guest['id']),([],0))

    def test_changed_guard_or_disabled_listener_cancels_without_adoption(self):
        for field in (self.valid,self.enabled):
            self.submit('offer-fixture-'+str(int(self.now[0])));self.worker.step();self.choose();field[0]=False
            self.worker.step();self.assertEqual(self.view()['status'],'cancelled');self.assertNotIn('chosenTrackId',self.view())
            self.adopt.assert_not_called();field[0]=True;self.now[0]+=1
            # A distinct exact vote command is required for the next poll.
            self.guest,_=self.ledger.create_guest(self.night[0])

    def test_new_night_drops_queued_old_offer_and_cancels_old_open_slot(self):
        self.submit();self.worker.step();old=self.view()['id'];self.submit('late-old-offer')
        self.night[0]='new-night';self.ledger.ensure_night('new-night');self.worker.step()
        row=self.ledger.db.execute('SELECT status,choice FROM guest_polls WHERE id=?',(old,)).fetchone()
        self.assertEqual(row['status'],'cancelled');self.assertIsNone(row['choice']);self.adopt.assert_not_called()
        self.assertEqual(self.ledger.db.execute('SELECT count(*) FROM guest_polls').fetchone()[0],1)

    def test_unknown_adoption_is_final_uncertainty_with_no_second_attempt(self):
        self.submit();self.worker.step();self.choose();self.adopt.side_effect=OSError('Private diagnostic must not escape')
        self.due();self.assertEqual(self.view()['status'],'cancelled');self.assertNotIn('chosenTrackId',self.view())
        self.assertNotIn('Private',str(self.view()));self.worker.step();self.assertEqual(self.adopt.call_count,1)

    def test_changed_or_partial_engine_receipt_cannot_claim_a_win(self):
        for change in ({'trackId':track(6)['id']},{'orderIds':self.order},{'sessionId':'different-night'},{'status':'kept'}):
            self.submit('offer-'+str(int(self.now[0])));self.worker.step();self.choose()
            self.adopt.side_effect=lambda claim:{**self.accept(claim),**change}
            self.due();self.assertEqual(self.view()['status'],'cancelled');self.assertNotIn('chosenTrackId',self.view())
            self.guest,_=self.ledger.create_guest(self.night[0]);self.now[0]+=1

    def test_failed_final_persistence_retries_only_ledger_completion(self):
        self.submit();self.worker.step();self.choose();original=self.polls.finish
        self.polls.finish=Mock(side_effect=OSError('Disk temporarily busy'))
        self.due();self.assertEqual(self.adopt.call_count,1);self.assertIsNotNone(self.worker.finalizing)
        self.assertEqual(self.view()['status'],'open');self.assertNotIn('chosenTrackId',self.view())
        self.worker.step();self.assertEqual(self.adopt.call_count,1)
        self.polls.finish=original;self.worker.step()
        self.assertEqual(self.view()['status'],'applied');self.assertIsNone(self.worker.finalizing);self.assertIsNone(self.worker.error)
        self.assertEqual(self.adopt.call_count,1)

    def test_restart_after_unknown_final_persistence_never_reinvokes_engine(self):
        self.submit();self.worker.step();self.choose();self.polls.finish=Mock(side_effect=OSError('Disk'))
        self.due();self.assertEqual(self.adopt.call_count,1)
        reopened=GuestPolls(self.ledger);second=PollCoordinator(reopened,lambda:self.night[0],lambda:True,self.verify,self.adopt)
        second.step();self.assertEqual(reopened.public(self.night[0],self.guest['id'])['status'],'interrupted')
        self.assertEqual(self.adopt.call_count,1)

    def test_guard_failure_is_optional_service_error_and_never_calls_engine_adoption(self):
        self.submit();self.verify.side_effect=ValueError('Private state mismatch');self.worker.step()
        self.assertIsNone(self.view());self.assertEqual(self.worker.error,'Voting is temporarily unavailable.');self.adopt.assert_not_called()


if __name__=='__main__':unittest.main()
