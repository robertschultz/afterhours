"""Decision provenance and delivery under real coordinator validation, no network."""
import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock,patch

from afterhours.dj_agent import DJAgent, validate_decision, evaluate, MAX_REQUEST_BYTES
from afterhours.planner_journal import PlannerJournal, comparison
from test_dj_agent import Session, decision, settled


def reviewed(order,slot=0,options=('a','b')):
    options=sorted(options,key=lambda tid:tid!=order[slot])
    return {**decision(order),'selectionReview':{'slot':slot,'options':[
        {'trackId':tid,'fit':90-i*8,'reason':f'{tid.upper()} matches the requested tempo and key.'}
        for i,tid in enumerate(options)]}}


class PlannerJournalTests(unittest.TestCase):
    def test_public_scores_require_top_winner_and_strict_incumbent_improvement(self):
        state=Session().snapshot()
        result=reviewed(['b','a','c'])
        self.assertEqual(comparison(result,state)['winnerId'],'b')
        for score in (81,82):
            result['selectionReview']['options'][0]['fit']=score
            self.assertIsNone(comparison(result,state))
            # The public annotation is not a second music-selection authority.
            checked=validate_decision(result,state)
            self.assertEqual(checked['trackIds'],['b','a','c'])
            self.assertIsNone(checked['selectionReview'])
        held=reviewed(['a','b','c'])
        for option in held['selectionReview']['options']:option['fit']=90
        self.assertEqual(comparison(held,state)['winnerId'],'a')

    def test_selected_order_and_applied_cues_are_distinct_and_snapshot_does_not_mutate(self):
        session=Session();agent=DJAgent(session,evaluator=lambda p:(reviewed(['b','a','c']),{'responseId':'offline'}))
        agent.message('Start with B',message_id='select',approved=True);state=settled(agent)
        journal=state['reasoning'];row=journal['rounds'][0]
        self.assertEqual(row['status'],'selected');self.assertIsNone(row['appliedAt'])
        self.assertEqual(row['comparison']['winnerId'],'b');self.assertEqual(row['comparison']['incumbentId'],'a')
        self.assertEqual([e['status'] for e in journal['events']],['evaluating','decided','selected'])
        self.assertEqual(state['proposal']['roundId'],row['id'])
        journal['rounds'][0]['selectedIds'].clear()
        session.update(executionPlan={'id':'prepared-plan','applied':True})
        applied=agent.snapshot()['reasoning']['rounds'][0]
        self.assertEqual(applied['status'],'applied');self.assertEqual(applied['after']['planId'],'prepared-plan')
        self.assertEqual(applied['selectedIds'],['b','a','c'])
        self.assertEqual(session.calls,[('plan',['b','a','c'])])

    def test_invalid_or_unprovided_comparison_is_omitted_without_blocking_music(self):
        session=Session();good=reviewed(['b','a','c']);bad=[]
        for key,value in [('slot',True),('slot',2),('slot',-1),('options',[]),('options',[{'trackId':'x','fit':90,'reason':'No source'}])]:
            item=copy.deepcopy(good);item['selectionReview'][key]=value;bad.append(item)
        for field,value in [('fit',True),('fit',101),('fit',float('nan')),('reason',''),('reason','a'*321)]:
            item=copy.deepcopy(good);item['selectionReview']['options'][0][field]=value;bad.append(item)
        for item in bad:
            with self.subTest(item=item):
                self.assertIsNone(comparison(item,session.snapshot()))
                self.assertEqual(validate_decision(item,session.snapshot())['trackIds'],['b','a','c'])
        self.assertIsNone(comparison(good,session.snapshot(),['a']))
        session.state['candidates'].append({'id':'d','title':'D','bpm':125})
        good=reviewed(['b','a','c'],options=('a','b','d'))
        evaluator=Mock(return_value=(good,{'responseId':'trimmed','catalogIds':['a','b','c']}))
        agent=DJAgent(session,evaluator=evaluator)
        agent.message('Choose B',message_id='trimmed',approved=True)
        self.assertIsNone(settled(agent)['reasoning']['rounds'][0]['comparison'])
        self.assertEqual(session.calls,[('plan',['b','a','c'])])

    def test_model_cannot_choose_a_recording_removed_by_request_compaction(self):
        s=Session();a=DJAgent(s,evaluator=lambda p:(decision(['b','a','c']),{'catalogIds':['a','c']}))
        a.message('Choose B',message_id='one',approved=True);state=settled(a)
        self.assertEqual(state['status'],'error');self.assertEqual(s.calls,[])
        self.assertEqual(state['reasoning']['rounds'][0]['status'],'failed')

    def test_receipt_names_the_exact_bounded_wire_catalog_with_public_comparison(self):
        packet={'typedTurn':True,'music':{'catalog':[{'id':str(i),'title':'Long title '*40,'bpm':125} for i in range(32)],
                                          'currentOrder':['0','1'],'protectedIds':['0','1'],'requestedIds':['31']},
                'operatorRequest':{'role':'user','text':'Add the newly found recording.'},
                'messages':[{'role':'user','text':'Add the newly found recording.'},{'role':'assistant','text':'Prior search. '*1000}]}
        captured=[]
        def opener(request,timeout):
            captured.append(request.data)
            response=Mock();response.read.return_value=json.dumps({'status':'completed','id':'offline',
                'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(decision())}]}]}).encode()
            context=Mock();context.__enter__=Mock(return_value=response);context.__exit__=Mock(return_value=False)
            return context
        with patch('afterhours.ai_mix.api_key',return_value='local-test-placeholder'):
            _,receipt=evaluate(packet,opener=opener)
        self.assertLessEqual(len(captured[0]),MAX_REQUEST_BYTES)
        body=json.loads(captured[0]);sent=json.loads(body['input'])['music']['catalog']
        self.assertEqual(receipt['catalogIds'],[t['id'] for t in sent])
        self.assertLess(len(sent),32);self.assertTrue({'0','1','31'}<=set(receipt['catalogIds']))
        self.assertEqual(packet['operatorRequest'],json.loads(body['input'])['operatorRequest'])
        self.assertNotIn(packet['operatorRequest'],json.loads(body['input'])['messages'])
        self.assertIn('selectionReview',body['text']['format']['schema']['properties'])

    def test_recheck_never_rates_protected_records_and_requires_incumbent_and_winner(self):
        session=Session();session.state['candidates'].append({'id':'d','title':'D','bpm':125})
        session.update(active=True,phase='playing',index=0,startedAt=10)
        valid=reviewed(['a','b','d'],slot=2,options=('c','d'))
        self.assertEqual(comparison(valid,session.snapshot())['incumbentId'],'c')
        for options in [('a','d'),('d',),('c',),('c','d','d')]:
            self.assertIsNone(comparison(reviewed(['a','b','d'],2,options),session.snapshot()))

    def staged(self):
        s=Session().snapshot();s.update(active=True,phase='playing',index=0,startedAt=100,executionPlan={'id':'old','applied':True})
        s['candidates'].append({'id':'d','title':'D','bpm':125})
        result=reviewed(['a','b','d'],2,('c','d'));j=PlannerJournal(lambda:123)
        rid=j.begin(s,4,'refill upcoming queue');j.finish(rid,result,s,{'responseId':'offline'},['a','b','c','d'])
        s['queuedUpdate']={'id':'queue-4','tracks':[next(t for t in s['candidates'] if t['id']==tid) for tid in result['trackIds']],'status':'needs-analysis'}
        j.accepted(rid,s)
        return s,j,rid

    def test_staged_queue_needs_exact_application_receipt_not_matching_track_ids(self):
        for receipt in [None,{'queueRevision':'different','trackIds':['a','b','d'],'planId':'new','appliedAt':125}]:
            s,j,rid=self.staged();self.assertEqual(j.get(rid)['status'],'staged')
            s.update(order=s['queuedUpdate']['tracks'],queuedUpdate=None,executionPlan={'id':'new','applied':True},lastQueueApplication=receipt)
            row=j.snapshot(s)['rounds'][0]
            self.assertEqual(row['status'],'superseded')
        s,j,rid=self.staged();s['queuedUpdate'].update(status='blocked',error='Decode is incomplete')
        row=j.snapshot(s)['rounds'][0]
        self.assertEqual(row['status'],'staged');self.assertEqual(row['preparation'],'blocked')
        s.update(order=s['queuedUpdate']['tracks'],queuedUpdate=None,executionPlan={'id':'new','applied':True},
                 lastQueueApplication={'queueRevision':'queue-4','trackIds':['a','b','d'],'planId':'new','appliedAt':125})
        row=j.snapshot(s)['rounds'][0]
        self.assertEqual(row['status'],'applied');self.assertEqual(row['appliedAt'],125)

    def test_changed_session_plan_queue_and_newer_message_discard_late_result(self):
        for kind in ['session','plan','queue','message','disable']:
            with self.subTest(kind=kind):
                s=Session();started=threading.Event();release=threading.Event()
                def slow(packet):
                    started.set();release.wait(2);return reviewed(['b','a','c']),{'responseId':'late'}
                agent=DJAgent(s,evaluator=slow);agent.message('B first',message_id='one',approved=True)
                self.assertTrue(started.wait(1))
                if kind=='session':s.update(id='new-session')
                elif kind=='plan':s.update(executionPlan={'id':'different'})
                elif kind=='queue':s.update(queuedUpdate={'id':'replacement','tracks':s.state['order']})
                elif kind=='message':agent.message('Keep the groove',message_id='two')
                else:agent.disable()
                release.set();state=settled(agent)
                self.assertEqual(state['reasoning']['rounds'][0]['status'],'superseded')
                self.assertIsNone(state['reasoning']['rounds'][0]['comparison']);self.assertEqual(s.calls,[])

    def test_history_is_bounded_and_no_restart_replay_or_new_allowance(self):
        s=Session().snapshot();j=PlannerJournal(lambda:100)
        for n in range(200):
            rid=j.begin(s,n,'operator message');j.finish(rid,reviewed(['b','a','c']),s,{},['a','b','c']);j.close(rid,'held','Not committed')
        snapshot=j.snapshot(s)
        self.assertLessEqual(len(snapshot['rounds']),24);self.assertLessEqual(len(snapshot['events']),96)
        sequences=[e['sequence'] for e in snapshot['events']]
        self.assertEqual(sequences,sorted(set(sequences)))
        self.assertLess(len(json.dumps(snapshot)),65000)
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'agent.json';session=Session()
            a=DJAgent(session,path,evaluator=lambda p:(reviewed(['b','a','c']),{}))
            a.message('B first',message_id='one',approved=True);settled(a)
            b=DJAgent(session,path)
            self.assertEqual(b.snapshot()['reasoning']['rounds'],[])
            self.assertNotEqual(a.planner.epoch,b.planner.epoch)
            self.assertEqual(b.snapshot()['allowance']['remaining'],0)

    def test_failure_and_no_selection_do_not_create_a_winner(self):
        for result in [ValueError('Provider unavailable'),decision()]:
            s=Session();e=Mock(side_effect=result) if isinstance(result,Exception) else Mock(return_value=(result,{}))
            a=DJAgent(s,evaluator=e);a.message('House',message_id='one',approved=True);row=settled(a)['reasoning']['rounds'][0]
            self.assertEqual(row['status'],'failed' if isinstance(result,Exception) else 'unchanged')
            self.assertIsNone(row['comparison']);self.assertEqual(s.calls,[]);e.assert_called_once()

    def test_full_future_queue_is_rechecked_once_per_meaningful_change_within_existing_allowance(self):
        s=Session();s.state['candidates'] += [{'id':c,'title':c.upper(),'bpm':125,'camelot':'8A'} for c in 'def']
        s.update(order=copy.deepcopy(s.state['candidates']),active=True,phase='playing',index=0,
                 executionPlan={'id':'prepared','applied':True})
        e=Mock(return_value=(reviewed(list('abcdef'),3,('d','e')),{'responseId':'recheck'}))
        agent=DJAgent(s,evaluator=e);agent.state['direction']='Keep a steady groove';agent.enable(True,120,2)
        agent.tick(force=True);e.assert_not_called()
        s.update(index=1);agent.tick(force=True);state=settled(agent)
        self.assertEqual(e.call_count,1);self.assertIn('recheck',e.call_args.args[0]['reason'])
        self.assertEqual(state['reasoning']['rounds'][0]['status'],'kept')
        for _ in range(10):agent.tick(force=True)
        self.assertEqual(e.call_count,1)
        s.update(telemetry={'decks':[{'position':99}]});agent.tick(force=True)
        self.assertEqual(e.call_count,1)
        e.return_value=(reviewed(list('abcdef'),4,('e','f')),{'responseId':'recheck-two'})
        s.update(index=2);agent.tick(force=True);settled(agent)
        self.assertEqual(e.call_count,2);self.assertEqual(agent.snapshot()['allowance']['remaining'],0)
        s.update(index=3);agent.tick(force=True);self.assertEqual(e.call_count,2)
        self.assertEqual(s.calls,[])
