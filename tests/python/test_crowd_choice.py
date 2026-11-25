"""Actual agent/session queue guards, synthetic measured audio; no native IO."""
import copy
import json
import unittest
from unittest.mock import Mock

from afterhours import crowd_choice
from afterhours.dj_agent import DJAgent,MAX_REQUEST_BYTES,SCHEMA,planning_request,validate_decision
from afterhours.guest_requests import GuestLedger
from afterhours.guest_polls import GuestPolls
from afterhours.guest_poll_coordinator import PollCoordinator
from afterhours.guest_coordinator import GuestCoordinator
from test_dj_agent import decision,settled
from test_dj_request import populated_packet
from test_preview_continuation import measured_session
from test_provider_eligibility import restrict


class CrowdChoiceTests(unittest.TestCase):
    def setUp(self):
        self.now=[1000.];self.enabled=[True];self.scope_epoch=['fixture']
        self.s,self.tracks=measured_session();self.addCleanup(self.s.crate.close)
        self.ids=[t['id'] for t in self.tracks];self.base=self.ids[:4]
        self.options=[self.ids[2],self.ids[4],self.ids[5]]
        self.s.update(queuedUpdate=None,startedAt=900,copilot={**self.s.state['copilot'],'mode':'leading'},
            telemetry={'sampledAt':1000.,'crossfader':0.,'decks':[
                {'deck':1,'id':self.ids[0],'loaded':True,'playing':True,'sampledAt':1000.,'position':20.,
                 'controls':{'sampledAt':1000.,'volume':1.,'durationMs':300000}},
                {'deck':2,'id':self.ids[1],'loaded':True,'playing':False,'sampledAt':1000.,
                 'controls':{'sampledAt':1000.,'volume':1.}}]})
        self.s.send=Mock(side_effect=AssertionError('No audio commands'));self.s.q=Mock(side_effect=AssertionError('No native query'))
        self.ledger=GuestLedger(clock=lambda:self.now[0]);self.addCleanup(self.ledger.close)
        self.guest_worker=GuestCoordinator(self.ledger,self.s,clock=lambda:self.now[0]);self.guest_worker.step()
        self.guest,_=self.ledger.create_guest(self.s.state['id'])
        self.polls=GuestPolls(self.ledger)
        self.answer={**decision(self.base),'crowdChoice':{'slot':2,'trackIds':self.options}}
        self.evaluator=Mock(side_effect=lambda packet:(copy.deepcopy(self.answer),{'responseId':'fixture-model'}))
        self.agent=DJAgent(self.s,evaluator=self.evaluator,clock=lambda:self.now[0])
        self.agent.state['direction']='Keep this house set and protect the playing pair.'
        self.agent.guest_requests=self.guest_worker
        self.worker=PollCoordinator(self.polls,self.guest_worker.night,lambda:self.enabled[0],
            self.agent.valid_crowd_offer,self.agent.adopt_crowd_choice,lambda:self.scope_epoch[0])
        self.guest_worker.polls=self.worker;self.agent.crowd_polls=self.worker
        self.scope={'origin':'http://127.0.0.1:19012','roomId':self.ledger.room_id,
            'nightId':self.s.state['id'],'generation':'fixture'}

    def no_audio(self):
        self.s.factory.assert_not_called();self.s.send.assert_not_called();self.s.q.assert_not_called()

    def view(self):return self.polls.public(self.s.state['id'],self.guest['id'])

    def authorize(self):
        self.agent.enable(True,120,3);self.agent.background_allowance=True
        self.agent.playback_request={'id':'already-authorized-start','sessionId':self.s.state['id'],'status':'playing',
            'revision':self.agent.state['revision'],'orderIds':self.ids[:2],'expiresAt':1120,'source':'typed'}

    def open(self):
        self.authorize();self.agent.tick(force=True);settled(self.agent);self.guest_worker.step()
        self.assertIsNotNone(self.view(),self.agent.snapshot()['message'])
        return self.view()

    def fresh(self):
        telemetry=self.s.state['telemetry'];telemetry['sampledAt']=self.now[0]
        for deck in telemetry['decks']:
            deck['sampledAt']=self.now[0];deck['controls']['sampledAt']=self.now[0]

    def vote(self,index=1):
        poll=self.view()
        return self.polls.vote(self.s.state['id'],self.guest['id'],{'requestId':'vote-fixture',
            'pollId':poll['id'],'trackId':self.options[index]},self.scope)

    def close(self):
        self.now[0]=self.view()['closesAt'];self.fresh();self.guest_worker.step()

    def test_authorized_planner_opens_real_future_choice_and_adopts_one_exact_winner(self):
        old=copy.deepcopy(self.s.state['order']);plan=copy.deepcopy(self.s.state['executionPlan'])
        poll=self.open();self.assertEqual(poll['closesAt']-poll['openedAt'],60)
        self.assertEqual(poll['status'],'open');self.assertNotIn('slotAt',poll)
        self.assertTrue(self.evaluator.call_args.args[0]['crowdPolicy']['canOffer'])
        self.assertEqual(self.agent.remaining,2);self.assertEqual(self.s.state['order'],old)
        self.assertEqual([r['id'] for r in self.s.state['queuedUpdate']['tracks']],self.base)
        self.vote();revision=self.agent.state['revision'];generation=self.agent.generation
        mutate=Mock(wraps=self.s.revise_queue);self.s.revise_queue=mutate
        self.close();self.guest_worker.step();self.guest_worker.step()
        self.assertEqual(mutate.call_count,1)
        expected=[self.ids[0],self.ids[1],self.ids[4],self.ids[3]]
        self.assertEqual(crowd_choice.order_ids(self.s.snapshot()),expected)
        self.assertEqual(self.view()['status'],'applied');self.assertEqual(self.view()['chosenTrackId'],self.ids[4])
        self.assertEqual(self.s.state['order'],old);self.assertEqual(self.s.state['executionPlan'],plan)
        self.assertEqual(self.agent.remaining,2);self.assertEqual(self.evaluator.call_count,1)
        self.assertEqual(self.agent.state['revision'],revision);self.assertEqual(self.agent.generation,generation)
        self.assertEqual(self.ledger.own(self.s.state['id'],self.guest['id']),([],0))
        messages=[m for m in self.agent.state['messages'] if m.get('source')=='crowd-poll']
        self.assertEqual(len(messages),1);self.assertIn(self.tracks[4]['title'],messages[0]['text'])
        self.no_audio()

    def test_incumbent_or_no_votes_keeps_preparation_without_queue_rewrite(self):
        self.open();before=copy.deepcopy(self.s.snapshot());self.vote(0)
        self.s.revise_queue=Mock(side_effect=AssertionError('Incumbent does not reprepare'))
        self.close();self.assertEqual(self.view()['status'],'kept')
        self.assertEqual(self.view()['chosenTrackId'],self.ids[2])
        self.assertEqual(self.s.state['queuedUpdate'],before['queuedUpdate']);self.s.revise_queue.assert_not_called();self.no_audio()

    def test_guest_support_and_offer_never_grant_planner_allowance(self):
        for _ in range(3):self.agent.tick(force=True);self.guest_worker.step()
        self.evaluator.assert_not_called();self.assertEqual(self.agent.remaining,0)
        self.assertIsNone(self.agent.playback_request);self.assertIsNone(self.view());self.no_audio()

    def test_selection_review_cannot_implicitly_open_a_poll(self):
        self.answer['crowdChoice']=None
        self.answer['selectionReview']={'slot':2,'options':[
            {'trackId':tid,'fit':80,'reason':'Metadata comparison only.'} for tid in self.options]}
        self.authorize();self.agent.tick(force=True);settled(self.agent);self.guest_worker.step()
        self.assertEqual(self.evaluator.call_count,1);self.assertIsNone(self.view());self.no_audio()

    def test_unsupported_client_policy_nulls_offer_without_changing_valid_selection(self):
        self.enabled[0]=False;self.authorize();self.agent.tick(force=True);settled(self.agent);self.guest_worker.step()
        self.assertFalse(self.evaluator.call_args.args[0]['crowdPolicy']['canOffer'])
        self.assertIsNone(self.agent.state['proposal']['crowdChoice']);self.assertIsNone(self.view())
        self.assertEqual(crowd_choice.order_ids(self.s.snapshot()),self.base);self.no_audio()

    def test_provider_packing_receipt_cannot_reenable_a_budget_omitted_offer(self):
        self.evaluator.side_effect=lambda packet:(copy.deepcopy(self.answer),{'responseId':'fixture','crowdCanOffer':False})
        self.authorize();self.agent.tick(force=True);settled(self.agent);self.guest_worker.step()
        self.assertIsNone(self.agent.state['proposal']['crowdChoice']);self.assertIsNone(self.view())
        self.assertEqual(crowd_choice.order_ids(self.s.snapshot()),self.base);self.no_audio()

    def test_every_variant_needs_available_measured_fit_and_unreserved_future_position(self):
        self.s.revise_queue(self.base)
        snapshot=self.s.snapshot()
        changes={
            'current':lambda s:(0,[self.ids[0],self.ids[4]]),
            'cued':lambda s:(1,[self.ids[1],self.ids[4]]),
            'no incumbent':lambda s:(2,self.ids[4:6]),
            'duplicate position':lambda s:(2,[self.ids[2],self.ids[3]]),
            'pin':lambda s:(s.update(setPins=[self.ids[4]]) or (2,self.options)),
            'lock':lambda s:(s['setlist'].update(lockedIds=[self.ids[2]]) or (2,self.options)),
            'reorder off':lambda s:(s['setlist'].update(allowReorder=False) or (2,self.options)),
            'explicit future tempo':lambda s:(s['queuedUpdate'].update(tempoTargets=[{'trackId':self.ids[2],'bpm':125}]) or (2,self.options)),
            'mixing only':lambda s:(s['copilot'].update(mode='mix') or (2,self.options)),
            'unavailable':lambda s:(s.update(recordingAvailability=[{'trackId':self.ids[5],'reason':'Unavailable exact variant.'}]) or (2,self.options)),
            'bad tempo':lambda s:(s['candidates'][5].update(bpm=290) or (2,self.options)),
            'unknown':lambda s:(2,[self.ids[2],'virtualdj:'+'f'*64]),
        }
        for label,change in changes.items():
            with self.subTest(label=label):
                state=copy.deepcopy(snapshot);slot,options=change(state)
                with self.assertRaises(ValueError):crowd_choice.validate_variants(state,self.base,slot,options,validate_decision,self.now[0])
        self.assertEqual(self.s.snapshot(),snapshot);self.no_audio()

    def test_context_changes_cancel_without_moving_or_reopening_the_slot(self):
        poll=self.open();self.vote();self.agent.state['revision']+=1
        before=copy.deepcopy(self.s.snapshot());self.guest_worker.step()
        self.assertEqual(self.view()['status'],'cancelled');self.assertNotIn('chosenTrackId',self.view())
        self.now[0]=poll['closesAt'];self.fresh();self.guest_worker.step()
        self.assertEqual(self.s.state['queuedUpdate'],before['queuedUpdate']);self.assertEqual(self.evaluator.call_count,1);self.no_audio()

    def test_final_atomic_guard_rechecks_takeover_after_deadline_claim(self):
        self.open();self.vote();original=self.worker.adopt
        def takeover(claim):
            self.s.update(active=False,phase='takeover');return original(claim)
        self.worker.adopt=Mock(side_effect=takeover)
        before=copy.deepcopy(self.s.state['queuedUpdate']);self.close();self.guest_worker.step()
        self.assertEqual(self.view()['status'],'cancelled');self.assertEqual(self.s.state['queuedUpdate'],before)
        self.assertEqual(self.worker.adopt.call_count,1);self.no_audio()

    def test_actual_cued_identity_change_cancels_without_moving_the_newly_loaded_record(self):
        self.open();self.vote();offer=self.polls.pending(self.s.state['id'])
        before=copy.deepcopy(self.s.state['queuedUpdate'])
        self.s.state['telemetry']['decks'][1]['id']=self.ids[4]
        self.assertFalse(self.agent.valid_crowd_offer(offer))
        self.close();self.assertEqual(self.view()['status'],'cancelled')
        self.assertNotIn('chosenTrackId',self.view());self.assertEqual(self.s.state['queuedUpdate'],before)
        self.assertEqual(self.s.state['telemetry']['decks'][1]['id'],self.ids[4]);self.no_audio()

    def test_final_atomic_guard_rechecks_actual_cue_after_deadline_claim(self):
        self.open();self.vote();original=self.worker.adopt
        def new_cue(claim):
            self.s.state['telemetry']['decks'][1]['id']=self.ids[4]
            return original(claim)
        self.worker.adopt=Mock(side_effect=new_cue)
        before=copy.deepcopy(self.s.state['queuedUpdate']);self.close();self.guest_worker.step()
        self.assertEqual(self.view()['status'],'cancelled');self.assertEqual(self.s.state['queuedUpdate'],before)
        self.assertEqual(self.worker.adopt.call_count,1);self.no_audio()

    def test_opening_offer_requires_both_fresh_exact_scheduled_ab_identities(self):
        before=copy.deepcopy(self.s.snapshot())
        for label,change in (
                ('different cued recording',lambda d:d.update(id=self.ids[4])),
                ('duplicate current recording',lambda d:d.update(id=self.ids[0])),
                ('empty cued deck',lambda d:d.update(loaded=False)),
                ('started cued deck',lambda d:d.update(playing=True)),
                ('stale cued reading',lambda d:d.update(sampledAt=900)),
                ('stale cued controls',lambda d:d['controls'].update(sampledAt=900)),
                ('unavailable cued record',lambda d:d.update(unavailable=True))):
            state=copy.deepcopy(before);change(state['telemetry']['decks'][1])
            self.assertFalse(crowd_choice.eligible(state,self.now[0],require_future=False),label)
        state=copy.deepcopy(before);state['telemetry']['decks'].pop()
        self.assertFalse(crowd_choice.eligible(state,self.now[0],require_future=False))
        state=copy.deepcopy(before);state['telemetry']['decks'].append(copy.deepcopy(state['telemetry']['decks'][1]))
        self.assertFalse(crowd_choice.eligible(state,self.now[0],require_future=False))
        self.no_audio()

    def test_ab_assignment_is_bound_while_fresh_ticks_and_cd_analysis_are_independent(self):
        self.open();offer=self.polls.pending(self.s.state['id'])
        original_hash=crowd_choice.context_hash(self.s.snapshot())
        self.now[0]+=1;self.fresh()
        self.s.state['telemetry']['decks'].extend([
            {'deck':3,'id':self.ids[4],'loaded':True,'playing':False,'sampledAt':900},
            {'deck':4,'id':self.ids[5],'loaded':True,'playing':False,'sampledAt':900}])
        self.assertEqual(crowd_choice.context_hash(self.s.snapshot()),original_hash)
        self.assertTrue(self.agent.valid_crowd_offer(offer))
        self.s.state['telemetry']['decks'][0]['deck']=2
        self.s.state['telemetry']['decks'][1]['deck']=1
        self.s.state['telemetry']['crossfader']=1.
        # This is a coherent pair, but a different physical ownership context.
        self.assertTrue(crowd_choice.eligible(self.s.snapshot(),self.now[0]))
        self.assertNotEqual(crowd_choice.context_hash(self.s.snapshot()),original_hash)
        self.assertFalse(self.agent.valid_crowd_offer(offer));self.no_audio()

    def test_poll_can_start_with_the_current_record_on_b_and_cue_on_a(self):
        self.s.state['telemetry']['decks'][0]['deck']=2
        self.s.state['telemetry']['decks'][1]['deck']=1
        self.s.state['telemetry']['crossfader']=1.
        self.open();self.vote();self.close()
        self.assertEqual(self.view()['status'],'applied')
        self.assertEqual(self.view()['chosenTrackId'],self.ids[4]);self.no_audio()

    def test_rapid_invitation_close_reopen_invalidates_old_offer_even_between_worker_steps(self):
        self.open();self.vote();before=copy.deepcopy(self.s.state['queuedUpdate'])
        self.enabled[0]=False;self.scope_epoch[0]='new-fixture';self.enabled[0]=True
        self.close();self.assertEqual(self.view()['status'],'cancelled')
        self.assertEqual(self.s.state['queuedUpdate'],before);self.no_audio()

    def test_stale_telemetry_changed_queue_or_new_preview_invalidates_bound_offer(self):
        self.open();offer=self.polls.pending(self.s.state['id']);before=copy.deepcopy(self.s.state)
        self.assertTrue(self.agent.valid_crowd_offer(offer))
        for change in (
                lambda:self.s.state['telemetry'].update(sampledAt=900),
                lambda:self.s.state['queuedUpdate']['tracks'].reverse(),
                lambda:self.s.state.update(setPins=[self.ids[4]]),
                lambda:self.s.state['copilot'].update(held=True),
                lambda:self.s.state['copilot'].update(mode='mix'),
                lambda:self.s.state.update(resumeBlocked=True),
                lambda:self.s.state.update(phase='mixing'),
                lambda:self.s.state.update(active=False)):
            self.s.state=copy.deepcopy(before);change();self.assertFalse(self.agent.valid_crowd_offer(offer))
        self.s.state=copy.deepcopy(before);restrict(self.s.availability,self.ids[4])
        self.assertFalse(self.agent.valid_crowd_offer(offer));self.no_audio()

    def test_unconfirmed_queue_write_is_not_replayed_or_claimed_successful(self):
        self.open();self.vote();original=self.s.revise_queue
        def unknown(*args,**kwargs):
            original(*args,**kwargs);raise OSError('Synthetic lost acknowledgement')
        self.s.revise_queue=Mock(side_effect=unknown);self.close();self.guest_worker.step()
        self.assertEqual(self.s.revise_queue.call_count,1);self.assertEqual(self.view()['status'],'cancelled')
        self.assertNotIn('chosenTrackId',self.view());self.no_audio()

    def test_new_agent_generation_cancels_and_question_without_order_never_offers(self):
        self.open();offer=self.polls.pending(self.s.state['id']);self.agent.generation+=1
        self.assertFalse(self.agent.valid_crowd_offer(offer));self.guest_worker.step()
        self.assertEqual(self.view()['status'],'cancelled')
        with self.assertRaisesRegex(ValueError,'accepted full future'):
            validate_decision({**decision(),'crowdChoice':{'slot':2,'trackIds':self.options}},self.s.snapshot())
        self.no_audio()

    def test_optional_poll_instructions_do_not_break_bounded_artist_request(self):
        packet=populated_packet();packet['crowdPolicy']={'canOffer':True}
        wire,sent=planning_request(packet)
        self.assertLessEqual(len(wire),MAX_REQUEST_BYTES);self.assertEqual(sent['operatorRequest'],packet['operatorRequest'])
        self.assertEqual(sent['music']['currentOrder'],packet['music']['currentOrder'])
        self.assertEqual(sent['music']['protectedIds'],packet['music']['protectedIds'])
        schema=json.loads(wire)['text']['format']['schema']
        expected=copy.deepcopy(SCHEMA['properties']['crowdChoice'])
        choices=expected['anyOf'][1]['properties']['trackIds']
        choices['items']={'$ref':'#/$defs/recordingId'}
        self.assertEqual(schema['properties']['crowdChoice'],expected if sent['crowdPolicy']['canOffer'] else {'type':'null'})
        self.assertEqual(schema['$defs']['recordingId']['enum'],[t['id'] for t in sent['music']['catalog']])
        if sent['crowdPolicy']['canOffer']:
            actual=schema['properties']['crowdChoice']['anyOf'][1]['properties']['trackIds']
            self.assertEqual((actual['minItems'],actual['maxItems']),(2,3))


if __name__=='__main__':unittest.main()
