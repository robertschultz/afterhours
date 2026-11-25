"""Engine-owned guest stages using exact fake readbacks and fake model calls."""
import copy
import json
import threading
import unittest
from unittest.mock import Mock

from afterhours.guest_requests import GuestLedger
from afterhours.guest_coordinator import GuestCoordinator,public_facts,verdicts
from afterhours.dj_agent import DJAgent,planning_request,MAX_REQUEST_BYTES
from test_dj_agent import decision,settled
from test_preview_continuation import measured_session
from test_provider_eligibility import restrict


class GuestCoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.now=[1000.];self.s,self.tracks=measured_session();self.ids=[r['id'] for r in self.tracks]
        self.s.update(queuedUpdate=None,startedAt=900,copilot={**self.s.state['copilot'],'mode':'leading'},
            telemetry={'sampledAt':1000.,'crossfader':0.,'decks':[
                {'deck':1,'id':self.ids[0],'loaded':True,'playing':True,'sampledAt':1000.,'position':20.,
                 'controls':{'sampledAt':1000.,'volume':1.,'durationMs':300000}},
                {'deck':2,'id':self.ids[1],'loaded':True,'playing':False,'sampledAt':1000.,
                 'controls':{'sampledAt':1000.,'volume':1.}}]})
        self.ledger=GuestLedger(clock=lambda:self.now[0]);self.addCleanup(self.ledger.close)
        self.coordinator=GuestCoordinator(self.ledger,self.s,lambda:{'id':'nova','name':'Nova'},clock=lambda:self.now[0])
        self.coordinator.step();self.g,_=self.ledger.create_guest(self.s.state['id'])
        self.scope={'origin':'http://127.0.0.1:12345','roomId':self.ledger.room_id,'nightId':self.s.state['id'],'generation':'fixture'}
        self.s.send=Mock(side_effect=AssertionError('No audio command'));self.s.q=Mock(side_effect=AssertionError('No native query'))
    def ask(self,index=3):
        value={'requestId':f'ask-record-{index}','searchId':'fixture-search','trackId':self.ids[index],'follow':False}
        result=self.ledger.ask(self.s.state['id'],self.g['id'],value,self.tracks[index],self.scope)
        self.coordinator.step();return result['request']
    def row(self,identifier):return next(r for r in self.ledger.own(self.s.state['id'],self.g['id'])[0] if r['id']==identifier)
    def agent(self,answer):
        evaluator=Mock(return_value=(answer,{'responseId':'fixture-model'}))
        agent=DJAgent(self.s,evaluator=evaluator,clock=lambda:self.now[0]);agent.guest_requests=self.coordinator
        agent.state['direction']='Keep the selected style and protect the playing pair.'
        return agent,evaluator
    def authorize_background(self,agent):
        agent.enable(True,120,3);agent.background_allowance=True
        agent.playback_request={'id':'existing-operator-start','sessionId':self.s.state['id'],'status':'playing',
            'revision':agent.state['revision'],'orderIds':self.ids[:2],'expiresAt':1120,'source':'typed'}
    def test_guest_never_grants_allowance_playback_or_operator_message(self):
        requested=self.ask();agent,evaluator=self.agent(decision())
        for _ in range(3):agent.tick(force=True);self.coordinator.step()
        evaluator.assert_not_called();self.assertEqual(agent.remaining,0);self.assertEqual(agent.deadline,0)
        self.assertIsNone(agent.playback_request);self.assertEqual(agent.state['messages'],[])
        self.assertEqual(self.row(requested['id'])['stage'],'sent')
        self.s.factory.assert_not_called();self.s.q.assert_not_called();self.s.send.assert_not_called()
    def test_existing_authorized_round_selects_exact_future_record_without_touching_pair_or_transport(self):
        requested=self.ask();old=copy.deepcopy(self.s.state['order'])
        agent,evaluator=self.agent(decision(self.ids[:2]+[self.ids[3],self.ids[4]]));self.authorize_background(agent)
        agent.tick(force=True);settled(agent);self.coordinator.step()
        self.assertEqual(evaluator.call_count,1);self.assertEqual(agent.remaining,2)
        packet=evaluator.call_args.args[0]
        self.assertEqual(packet['requestCandidates'][0]['requestId'],requested['id'])
        self.assertEqual(packet['requestCandidates'][0]['trackId'],self.ids[3])
        self.assertIn(self.ids[3],[r['id'] for r in packet['music']['catalog']])
        self.assertEqual(self.s.state['order'],old)
        self.assertEqual([r['id'] for r in self.s.state['queuedUpdate']['tracks']],self.ids[:2]+[self.ids[3],self.ids[4]])
        self.assertEqual(self.row(requested['id'])['stage'],'coming');self.assertNotIn('at',self.row(requested['id']))
        self.s.factory.assert_not_called();self.s.q.assert_not_called();self.s.send.assert_not_called()
    def test_listening_has_authorized_round_and_stale_result_cannot_decline_or_select(self):
        requested=self.ask();agent,_=self.agent(decision());self.authorize_background(agent)
        began=threading.Event();finish=threading.Event()
        def evaluator(packet):
            began.set();finish.wait(2)
            return {**decision(),'requestVerdicts':[{'requestId':requested['id'],'trackId':self.ids[3],'verdict':'decline'}]}, {'responseId':'old-round'}
        agent.evaluator=evaluator;agent.tick(force=True);self.assertTrue(began.wait(1));self.coordinator.step()
        self.assertEqual(self.row(requested['id'])['stage'],'listening')
        agent.generation+=1;finish.set();settled(agent);self.coordinator.step()
        self.assertEqual(self.row(requested['id'])['stage'],'sent');self.assertIsNone(self.s.state['queuedUpdate'])
    def test_wait_decision_is_not_timer_retried_and_decline_must_match_supplied_exact_request(self):
        requested=self.ask();answer={**decision(),'requestVerdicts':[{'requestId':requested['id'],'trackId':self.ids[3],'verdict':'wait'}]}
        agent,evaluator=self.agent(answer);self.authorize_background(agent)
        agent.tick(force=True);settled(agent);self.coordinator.step();self.now[0]+=20
        agent.tick(force=True);settled(agent);self.assertEqual(evaluator.call_count,1)
        self.assertEqual(self.row(requested['id'])['stage'],'sent')
        with self.assertRaises(ValueError):verdicts([{'requestId':requested['id'],'trackId':self.ids[4],'verdict':'decline'}],
            [{'requestId':requested['id'],'trackId':self.ids[3]}],[])
        with self.assertRaises(ValueError):verdicts(answer['requestVerdicts'],
            [{'requestId':requested['id'],'trackId':self.ids[3]}],[self.ids[3]])
    def test_requested_alternate_release_gets_one_consideration_without_repeat_spend(self):
        self.tracks[3]['title']='  '+self.tracks[0]['title'].upper()+'  '
        self.tracks[3]['remix']='Alternate release'
        self.s.update(candidates=self.tracks)
        requested=self.ask()
        answer={**decision(),'requestVerdicts':[{'requestId':requested['id'],'trackId':self.ids[3],'verdict':'wait'}]}
        agent,evaluator=self.agent(answer);self.authorize_background(agent)
        agent.tick(force=True);settled(agent);self.coordinator.step()
        packet=evaluator.call_args.args[0]
        self.assertEqual(packet['requestCandidates'][0]['trackId'],self.ids[3])
        row=next(t for t in packet['music']['catalog'] if t['id']==self.ids[3])
        self.assertFalse(row['selectionEligible'])
        schema=json.loads(planning_request(packet)[0])['text']['format']['schema']
        self.assertIn(self.ids[3],schema['$defs']['recordingId']['enum'])
        self.assertNotIn(self.ids[3],schema['$defs']['selectableId']['enum'])
        self.assertEqual(agent.guest_considered[requested['id']],1)
        for _ in range(3):
            self.now[0]+=20;agent.tick(force=True);settled(agent);self.coordinator.step()
        self.assertEqual(evaluator.call_count,1);self.assertEqual(agent.remaining,2)
        self.assertEqual(self.row(requested['id'])['stage'],'sent')
        self.assertIsNone(self.s.state['queuedUpdate'])
        self.s.factory.assert_not_called();self.s.q.assert_not_called();self.s.send.assert_not_called()
    def test_new_operator_queue_wins_over_late_decline_event(self):
        requested=self.ask();event={'requestId':requested['id'],'trackId':self.ids[3]}
        self.coordinator.submit('begin',self.s.state['id'],'round-a',[event]);self.coordinator.step()
        self.s.revise_queue(self.ids[:2]+[self.ids[3]])
        self.coordinator.submit('finish',self.s.state['id'],'round-a',[{**event,'verdict':'decline'}]);self.coordinator.step()
        self.assertEqual(self.row(requested['id'])['stage'],'coming')
    def test_preparation_block_and_takeover_clear_coming_without_guessing_a_decline(self):
        requested=self.ask();self.s.revise_queue(self.ids[:2]+[self.ids[3]]);self.coordinator.step()
        self.assertEqual(self.row(requested['id'])['stage'],'coming')
        self.s.state['queuedUpdate'].update(status='blocked',error='Private engine details')
        self.coordinator.step();row=self.row(requested['id'])
        self.assertEqual(row['stage'],'sent');self.assertNotIn('Private',str(row));self.assertNotIn('at',row)
        self.s.state['queuedUpdate']['status']='needs-analysis';self.s.update(phase='takeover',active=False)
        self.coordinator.step();self.assertEqual(self.row(requested['id'])['stage'],'sent')
        restrict(self.s.availability,self.ids[3]);self.coordinator.step()
        self.assertEqual(self.row(requested['id'])['stage'],'declined')
        self.assertEqual(self.row(requested['id'])['reason'],'This recording is not available for the set.')
    def test_only_fresh_exact_audible_ab_can_fulfill_request_never_muted_spare_or_paused(self):
        requested=self.ask();deck={'deck':3,'id':self.ids[3],'loaded':True,'playing':True,'sampledAt':1000.,
            'controls':{'sampledAt':1000.,'volume':1.}}
        self.s.state['telemetry']['decks'].append(deck);self.coordinator.step()
        self.assertEqual(self.row(requested['id'])['stage'],'sent')
        deck.update(deck=2,playing=False);self.s.state['telemetry']['decks']=[self.s.state['telemetry']['decks'][0],deck]
        self.s.state['telemetry']['crossfader']=1.;self.coordinator.step()
        self.assertEqual(self.row(requested['id'])['stage'],'sent')
        deck.update(playing=True);deck['controls']['volume']=0.;self.coordinator.step()
        self.assertEqual(self.row(requested['id'])['stage'],'sent')
        deck['controls']['volume']=1.;deck['sampledAt']=900.;self.coordinator.step()
        self.assertEqual(self.row(requested['id'])['stage'],'sent')
        deck['sampledAt']=1000.;self.coordinator.step();played=self.row(requested['id'])
        self.assertEqual(played['stage'],'playing');self.assertTrue(played['playingNow']);self.assertEqual(played['playedAt'],1000.)
        self.now[0]+=5;self.coordinator.step();ended=self.row(requested['id'])
        self.assertEqual(ended['stage'],'playing');self.assertFalse(ended['playingNow']);self.assertEqual(ended['playedAt'],1000.)
    def test_modes_off_and_mixing_only_do_not_consider_guest_queue_changes(self):
        for mode in ('mix','off'):
            requested=self.ask();agent,evaluator=self.agent(decision(self.ids[:2]+[self.ids[3]]))
            self.authorize_background(agent);self.s.state['copilot']['mode']=mode
            agent.tick(force=True);settled(agent);self.coordinator.step();evaluator.assert_not_called()
            self.assertEqual(self.row(requested['id'])['stage'],'sent')
    def test_packet_bound_retains_the_supplied_exact_guest_rows(self):
        from test_dj_request import populated_packet
        packet=populated_packet();catalog=packet['music']['catalog']
        packet['requestCandidates']=[{'requestId':f'request-{i}','trackId':r['id'],'title':r['title'],'artist':r['artist'],'asks':1}
            for i,r in enumerate(catalog[5:13])]
        raw,sent=planning_request(packet)
        self.assertLessEqual(len(raw),MAX_REQUEST_BYTES)
        self.assertGreater(len(sent['requestCandidates']),0)
        self.assertEqual(sent['requestCandidates'],packet['requestCandidates'][:len(sent['requestCandidates'])])
        self.assertTrue({r['trackId'] for r in sent['requestCandidates']}<={r['id'] for r in sent['music']['catalog']})
        packed_again,_=planning_request(sent)
        self.assertEqual(json.loads(raw)['text']['format']['schema'],json.loads(packed_again)['text']['format']['schema'])


if __name__=='__main__':unittest.main()
