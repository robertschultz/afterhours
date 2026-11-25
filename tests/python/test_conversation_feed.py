"""Public DJ turns use actual work and matching receipts; no provider or audio."""
import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

from afterhours import conversation_feed
from afterhours.dj_agent import DJAgent
from test_dj_agent import Session, decision, settled
from test_planner_journal import reviewed
import test_planner_journal


class ConversationFeedTests(unittest.TestCase):
    def test_header_off_stops_late_planner_posts_and_does_not_resume_them(self):
        started=threading.Event();release=threading.Event();session=Session()
        def evaluator(packet):
            started.set();release.wait(2);return reviewed(['b','a','c']),{}
        agent=DJAgent(session,evaluator=evaluator)
        agent.message('Choose B first',message_id='late',approved=True)
        self.assertTrue(started.wait(1));session.update(copilot={'mode':'off'})
        release.set();state=settled(agent);agent.tick(force=True)
        self.assertEqual(state['conversation']['turns'][-1]['status'],'superseded')
        self.assertEqual(len(state['messages']),1);self.assertEqual(session.calls,[])
        self.assertEqual(agent.snapshot()['allowance']['remaining'],0)

    def test_running_then_answer_is_one_linked_turn_without_private_reasoning(self):
        started=threading.Event();release=threading.Event();session=Session()
        def evaluator(packet):
            started.set();release.wait(2)
            return {**decision(),'reply':'The current order stays in place.'},{'responseId':'fixture','privateReasoning':'never publish this'}
        agent=DJAgent(session,evaluator=evaluator)
        agent.message('Why this track?',source='voice',message_id='voice-question',approved=True)
        self.assertTrue(started.wait(1))
        running=agent.snapshot()['conversation']['turns'][-1]
        self.assertFalse(running['done']);self.assertEqual(running['text'],'');self.assertIsNone(running['action'])
        self.assertIn('3 queued',running['steps'][0]['result'])
        release.set();state=settled(agent);turn=state['conversation']['turns'][-1]
        self.assertEqual(running['id'],turn['id']);self.assertTrue(turn['done'])
        self.assertEqual(turn['messageId'],state['messages'][-1]['id'])
        self.assertEqual(turn['requestId'],'voice-question');self.assertEqual(turn['text'],'The current order stays in place.')
        self.assertNotIn('never publish this',json.dumps(state['conversation']));self.assertEqual(session.calls,[])

    def test_selection_waits_for_applied_plan_before_showing_action(self):
        session=Session();agent=DJAgent(session,evaluator=lambda p:(reviewed(['b','a','c']),{}))
        agent.message('B first',message_id='select',approved=True)
        selected=settled(agent)['conversation']['turns'][-1]
        self.assertIsNone(selected['action']);self.assertEqual(selected['status'],'selected')
        self.assertIn('Preparing',selected['text'])
        session.update(executionPlan={'id':'new-plan','applied':True})
        applied=agent.snapshot()['conversation']['turns'][-1]
        self.assertEqual(applied['id'],selected['id'])
        self.assertEqual(applied['action']['verb'],'Moved');self.assertEqual(applied['action']['object'],'B')
        self.assertEqual(session.calls,[('plan',['b','a','c'])])

    def test_staged_swap_requires_exact_receipt_and_has_actual_titles(self):
        fixture=test_planner_journal.PlannerJournalTests()
        for correct in (False,True):
            session,journal,_=fixture.staged()
            staged=conversation_feed.merge([],journal.snapshot(session),125)[-1]
            self.assertIsNone(staged['action'])
            session.update(order=session['queuedUpdate']['tracks'],queuedUpdate=None,executionPlan={'id':'new','applied':True},
                lastQueueApplication={'queueRevision':'queue-4' if correct else 'wrong','trackIds':['a','b','d'],'planId':'new','appliedAt':125})
            after=conversation_feed.merge([staged],journal.snapshot(session),125)[-1]
            if correct:
                self.assertEqual(after['action']['verb'],'Swapped');self.assertEqual(after['action']['object'],'C → D')
            else:self.assertIsNone(after['action']);self.assertEqual(after['status'],'superseded')

    def test_failed_and_superseded_work_has_no_success_action(self):
        for error in ('Provider unavailable','A newer direction replaced this evaluation.'):
            session=Session();agent=DJAgent(session,evaluator=Mock(side_effect=ValueError(error)))
            agent.message('Find a record',message_id='bad',approved=True)
            turn=settled(agent)['conversation']['turns'][-1]
            self.assertTrue(turn['done']);self.assertIsNone(turn['action']);self.assertEqual(turn['text'],error)

    def test_restart_retains_answers_but_never_resumes_pending_work(self):
        for selection in (False,True):
            with tempfile.TemporaryDirectory() as temp:
                path=Path(temp)/'agent.json';session=Session()
                agent=DJAgent(session,path,evaluator=lambda p:(reviewed(['b','a','c']) if selection else decision(),{}))
                agent.message('Keep it warm',message_id='one',approved=True);original=settled(agent)['conversation']['turns'][-1]
                restored=DJAgent(session,path);state=restored.snapshot();turn=state['conversation']['turns'][-1]
                self.assertEqual(turn['id'],original['id']);self.assertTrue(turn['done'])
                self.assertEqual(turn['status'],'interrupted' if selection else 'unchanged')
                self.assertIsNone(turn['action']);self.assertEqual(state['allowance']['remaining'],0)

    def test_room_requests_are_actual_events_and_never_operator_commands(self):
        session=Session();agent=DJAgent(session)
        self.assertEqual(agent.snapshot()['conversation']['events'],[])
        agent.audience_request('play a little disco','a'*32)
        state=agent.snapshot();self.assertEqual(len(state['conversation']['events']),1)
        self.assertEqual(state['messages'],[]);self.assertEqual(state['conversation']['turns'],[]);self.assertEqual(session.calls,[])

    def test_control_confirmation_cannot_be_faked_by_assistant_text(self):
        messages=[dict(id='command-reply',text='Started the blend.',at=100)]
        for status in ('pending','blocked','confirmed'):
            receipt=dict(id='command',action='go',status=status,message='Observed engine result',completedAt=102)
            turn=conversation_feed.control([],receipt,messages,102)[0]
            self.assertEqual(turn['action'] is not None,status=='confirmed')
        self.assertEqual(conversation_feed.control([],dict(id='other',action='go',status='confirmed'),messages,102),[])

    def test_history_is_bounded_stable_and_fresh_clears_it(self):
        session=Session();agent=DJAgent(session,evaluator=lambda p:(decision(),{}))
        for i in range(30):
            agent.message('Keep it warm',message_id=str(i),approved=True);settled(agent)
        before=agent.snapshot()['conversation'];after=agent.snapshot()['conversation']
        self.assertEqual(before,after);self.assertLessEqual(len(after['turns']),24)
        self.assertLess(len(json.dumps(after)),65000)
        agent.fresh();self.assertEqual(agent.snapshot()['conversation']['turns'],[])

    def test_search_progress_changes_only_for_the_matching_real_query(self):
        q=dict(query='disco',artist='',genre='Disco',bpmMin=120,bpmMax=130)
        session=Session();agent=DJAgent(session,evaluator=lambda p:(decision(search=q),{}))
        agent.message('Find disco',message_id='search',approved=True);settled(agent)
        session.update(catalogSearch=dict(query='another search',examined=80,added=12))
        unmatched=agent.snapshot()['conversation']['turns'][-1]['steps']
        self.assertFalse(any('80 checked' in s['result'] for s in unmatched))
        session.update(catalogSearch=dict(query='disco',examined=7,added=2))
        matching=agent.snapshot()['conversation']['turns'][-1]['steps']
        self.assertTrue(any('7 checked · 2 found' in s['result'] for s in matching))
