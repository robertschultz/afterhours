"""Operator intent to verified start, with fake providers and no native audio."""
import copy
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from afterhours.dj_agent import DJAgent
from afterhours.playback_requests import intent
from test_agent_scenarios import ScenarioSession
from test_dj_agent import decision,settled


class PlaybackSession(ScenarioSession):
    def start(self,initialize_mixer=False,immediate=False,rolling_queue=False,allow_grid_opening=False):
        assert immediate is True
        assert initialize_mixer is True
        self.calls.append(('start',))
        self.update(active=True,phase='playing',index=0,message='Playing House A',
                    telemetry={'decks':[{'playing':True,'id':self.state['order'][0]['id']}]})
    def stop(self,pause=True):
        self.calls.append(('stop',));self.update(active=False,phase='stopped',telemetry={'decks':[]})


class PlaybackRequestTests(unittest.TestCase):
    def test_casual_start_uses_existing_queue_without_model_work(self):
        for text in ('go play some music', 'Can you go play some music?', 'go ahead and start the music'):
            with self.subTest(text=text):
                self.assertEqual(intent(text), 'start')
                s,a,e=self.make(True)
                a.message(text, message_id='casual-start')
                self.drain(a)
                self.assertEqual(s.calls.count(('start',)),1)
                self.assertEqual(a.playback_request['status'],'playing')
                e.assert_not_called()
                self.assertIsNone(a.snapshot()['curation'])

    def test_casual_prefix_preserves_named_music_and_deferred_intent(self):
        self.assertEqual(intent('go play Daft Punk'), 'select-and-start')
        for text in ('go play some music later', 'go play some music if the room is ready',
                     'go play some music in two hours', 'please do not go play some music'):
            self.assertIsNone(intent(text), text)

    def test_failed_native_start_settles_with_copilot_off_without_replaying(self):
        session, agent, evaluator = self.make(True)
        session.update(copilot={'mode': 'off'})
        error = 'Catalog folder changed. Discover music again before starting.'

        def launch(**_):
            session.update(active=True, phase='loading')

        with patch.object(session, 'start', side_effect=launch) as start:
            agent.message('Play the queue', message_id='initial')
            session.update(phase='error', lastError=error)
            agent.tick()
            self.assertEqual(agent.snapshot()['playbackRequest']['status'], 'starting')
            session.update(active=False)
            agent.tick()
            receipt = agent.snapshot()['playbackRequest']
            self.assertEqual((receipt['id'], receipt['status'], receipt['message']),
                             ('initial', 'failed', error))
            agent.tick()
            start.assert_called_once()
            evaluator.assert_not_called()

    def test_explicit_retry_can_clear_a_terminal_start_before_the_next_tick(self):
        session, agent, evaluator = self.make()
        session.update(copilot={'mode': 'off'})
        with patch.object(session, 'start', side_effect=lambda **_: session.update(active=True, phase='loading')) as start:
            agent.message('Play the queue', message_id='initial')
            session.update(active=False, phase='error', lastError='Native startup refused.')
            agent.message('Play the queue', message_id='fresh-retry')
            self.assertEqual(agent.snapshot()['playbackRequest']['id'], 'fresh-retry')
            self.assertEqual(start.call_count, 2)
            evaluator.assert_not_called()

    def test_stopping_native_start_cancels_receipt_without_a_second_start(self):
        for phase in ('stopped', 'takeover'):
            with self.subTest(phase=phase):
                session, agent, evaluator = self.make(True)
                session.update(copilot={'mode': 'off'})
                with patch.object(session, 'start', side_effect=lambda **_: session.update(active=True, phase='loading')) as start:
                    agent.message('Play the queue', message_id='initial')
                    session.update(active=False, phase=phase, message='Operator stopped the startup.')
                    agent.tick()
                    receipt = agent.snapshot()['playbackRequest']
                    self.assertEqual((receipt['status'], receipt['message']), ('cancelled', 'Operator stopped the startup.'))
                    self.assertEqual(agent.snapshot()['status'], 'held' if phase == 'takeover' else 'idle')
                    self.assertEqual(agent.snapshot()['message'], receipt['message'])
                    start.assert_called_once()
                    evaluator.assert_not_called()

    def make(self,prepared=False):
        s=PlaybackSession();s.plan(['a','b','c'])
        if prepared:s.update(executionPlan={'applied':True,'id':'prepared'})
        e=Mock(return_value=(decision(['a','b','c']),{'responseId':'offline'}))
        return s,DJAgent(s,evaluator=e),e
    def drain(self,a):
        for _ in range(12):a.tick(force=True);settled(a)
    def model_start(self,request_id='natural',ids=None,scope='ongoing'):
        return {**decision(ids or ['a','b','c']),
                'playbackAction':{'requestId':request_id,'action':'start-now'},
                'sessionScope':{'mode':scope,'message':'A continuing set.'}}
    def test_model_classified_natural_start_uses_same_guarded_path_typed_and_voice(self):
        text='Start an ongoing Daft Punk-inspired house set, with a new track roughly every 30 seconds. Keep discovering and preparing compatible tracks as you go.'
        self.assertIsNone(intent(text),'Exercise the semantic fallback, not a new phrase regex.')
        for source in ('typed','voice'):
            s,a,e=self.make(True)
            if source=='voice':a.enable(True,1800,6)
            e.return_value=(self.model_start(),{})
            a.message(text,source=source,message_id='natural',approved=source=='typed',
                      approve_preparation=source=='typed',approve_session_curation=source=='typed');settled(a)
            self.assertTrue(e.call_args.args[0]['canRequestStart'])
            self.assertEqual(a.playback_request['id'],'natural')
            for _ in range(2):a.tick(force=True);settled(a)
            self.assertEqual(s.calls,[('start',)])
            self.assertEqual(a.playback_request['status'],'playing');self.assertEqual(e.call_count,1)
            self.assertEqual(bool(a.snapshot()['curation']),source=='typed')
            self.assertEqual(a.remaining,0 if source=='typed' else 5)
    def test_model_nonstart_questions_and_deferred_requests_never_create_transport(self):
        for text in ('Would a Daft Punk set work?', 'Discuss starting a set.', 'I prefer Daft Punk.',
                     'Start an ongoing set later.', 'Start an ongoing set if the room is ready.',
                     'What music would you play?', 'Tell me about your jacket.'):
            s,a,e=self.make(True)
            e.return_value=({**decision(), 'sessionScope':{'mode':'ongoing','message':'A possible ongoing set.'}}, {})
            a.message(text,message_id='question',approved=True,approve_preparation=True,approve_session_curation=True);settled(a)
            self.drain(a);self.assertIsNone(a.playback_request);self.assertIsNone(a.snapshot()['curation'])
            self.assertEqual(s.calls,[]);self.assertEqual(e.call_count,1)
    def test_model_start_rejects_wrong_id_expired_and_missing_source_permission(self):
        for cause in ('wrong-id','expired','typed-unapproved','preparation-unapproved','voice-inactive','catalog'):
            s,a,e=self.make(True);now=[1000.];a.clock=lambda:now[0]
            if cause=='typed-unapproved':a.enable(True,1800,6)
            def answer(packet):
                if cause=='expired':now[0]+=181
                return self.model_start('history' if cause=='wrong-id' else 'natural'),{}
            e.side_effect=answer
            a.message('Begin an ongoing house set.',message_id='natural',
                      source='voice' if cause=='voice-inactive' else 'typed',approved=cause!='typed-unapproved',
                      approve_preparation=cause!='preparation-unapproved',surface='catalog' if cause=='catalog' else None);settled(a)
            self.drain(a);self.assertIsNone(a.playback_request);self.assertEqual(s.calls,[])
    def test_catalog_followup_cannot_mint_start_but_retains_original_accepted_intent(self):
        for accepted,forged in ((True,False),(False,False),(False,True)):
            s,a,e=self.make(True)
            opening=self.model_start()
            opening.update(trackIds=[],search={'query':'Daft Punk','artist':'','genre':'','bpmMin':40,'bpmMax':300})
            if not accepted:opening['playbackAction']=None
            e.return_value=(opening,{})
            a.message('Begin an ongoing house set.',message_id='natural',approved=True,
                      approve_preparation=True,approve_session_curation=True);settled(a)
            self.assertEqual(bool(a.playback_request),accepted)
            s.finish_search()
            e.return_value=(self.model_start() if forged else decision(['a','b','c']),{})
            a.tick(force=True);settled(a)
            self.assertNotIn('canRequestStart',e.call_args.args[0])
            for _ in range(2):a.tick(force=True);settled(a)
            self.assertEqual(('start',) in s.calls,accepted)
            if not accepted:self.assertIsNone(a.playback_request)
    def test_model_start_keeps_clarification_binding_without_second_play(self):
        s,a,e=self.make(True)
        reply=self.model_start();reply.update(trackIds=[],sessionScope={'mode':'clarify','message':'A full set or just a few records?'})
        e.return_value=(reply,{})
        a.message('Begin an ongoing house set.',message_id='natural',approved=True,approve_preparation=True,approve_session_curation=True);settled(a)
        self.assertEqual(a.playback_request['status'],'clarifying');self.assertEqual(s.calls,[])
        e.return_value=({**decision(['a','b','c']),'sessionScope':{'mode':'ongoing','message':'A full set.'}}, {})
        a.message('A full set please.',message_id='answer',approved=True,approve_preparation=True,approve_session_curation=True);settled(a)
        for _ in range(2):a.tick(force=True);settled(a)
        self.assertEqual(a.playback_request['id'],'natural');self.assertEqual(s.calls,[('start',)])
    def test_late_model_start_cannot_follow_new_message_or_disabled_conversation(self):
        for cause in ('new-message','disabled'):
            s,a,e=self.make(True);entered=threading.Event();release=threading.Event()
            def answer(packet):
                entered.set();release.wait(2);return self.model_start(),{}
            e.side_effect=answer
            a.message('Begin an ongoing house set.',message_id='natural',approved=True,approve_preparation=True)
            self.assertTrue(entered.wait(1))
            if cause=='new-message':a.message('What is playing?',message_id='status')
            else:a.disable()
            release.set();settled(a);self.drain(a)
            self.assertIsNone(a.playback_request);self.assertEqual(s.calls,[])
    def test_explicit_commands_exclude_negation_future_and_conversation(self):
        for text in ['start playing','Can you start the music?','please play music','Play the set.']:
            self.assertEqual(intent(text),'start')
        self.assertEqual(intent('Can you play CamelPhat?'),'select-and-start')
        for text in ['do not start','play this later','can you DJ Christian music','I like playing house','start analyzing','search for house','play something when I return']:
            self.assertIsNone(intent(text),text)
    def test_artist_seeded_session_phrases_authorize_selection_then_start(self):
        for text in ['can you start a session based on daft punk',
                     'lets start a new session based on daft punk',
                     "Let's start a new set with Daft Punk.",
                     'Could we start a DJ session inspired by Daft Punk?',
                     'start a mix featuring Daft Punk']:
            self.assertEqual(intent(text),'select-and-start',text)
        for text in ['do not start a session based on daft punk',
                     'can you start a session based on daft punk later',
                     'how do I start a session based on Daft Punk',
                     'I want to discuss starting a new session based on Daft Punk']:
            self.assertIsNone(intent(text),text)
    def test_complete_current_queue_commands_and_continuation_are_distinct(self):
        for qualifier in ('current','retained','ready','loaded','existing','prepared','selected','saved'):
            for text in (f'Start the {qualifier} queue.',f'Can you play the {qualifier} queue?',
                         f'Please go ahead and start playing the {qualifier} queue now.'):
                self.assertEqual(intent(text),'start',text)
            for text in (f'Continue the {qualifier} queue.',f'Resume the {qualifier} queue.',
                         f'Keep the {qualifier} queue going.',f'Keep playing the {qualifier} queue.'):
                self.assertEqual(intent(text),'continue',text)
        for text in ('Keep going.','Could you keep the music going?','Continue playing.'):
            self.assertEqual(intent(text),'continue',text)
    def test_current_queue_aliases_do_not_grant_start_for_questions_names_or_future(self):
        for text in ('Do not start the current queue.','Never play the ready queue.',
                     'Start the current queue later.','Start the current queue when I return.',
                     'Start the loaded queue in five minutes.','Start the existing queue if it is ready.',
                     'Play the loaded queue in five minutes.','Play the current queue unless I object.',
                     'Play the current queue once everyone arrives.',
                     'Should we start the current queue?','How do I start the current queue?',
                     'Can you explain how to start the current queue?', 'Is the current queue ready?',
                     'Do you want to keep going?', 'Keep going after I return.',
                     'Start the current queue with Daft Punk.','Resume the current queue with Daft Punk.'):
            self.assertIsNone(intent(text),text)
        for text in ('Play Daft Punk.','Play Current Queue by Daft Punk.',
                     'Start playing The Current Queue by Daft Punk.'):
            self.assertEqual(intent(text),'select-and-start',text)
    def test_current_queue_starts_locally_once_with_identical_queue_and_no_model_allowance(self):
        for source in ('typed','voice'):
            for text in ('Start the current queue.','Play the ready queue.','Keep the current queue going.'):
                with self.subTest(source=source,text=text):
                    s,a,e=self.make(True);before=copy.deepcopy(s.snapshot()['order'])
                    a.message(text,source=source,message_id='queue-start',approved=True,approve_preparation=True)
                    self.drain(a)
                    a.message(text,source=source,message_id='queue-start',approved=True,approve_preparation=True)
                    self.assertEqual(s.calls,[('start',)]);e.assert_not_called()
                    self.assertEqual(s.snapshot()['order'],before)
                    self.assertEqual(a.remaining,0)
                    self.assertEqual(a.playback_request['orderIds'],[t['id'] for t in before])
                    self.assertEqual(a.playback_request['status'],'playing')
    def test_continuation_uses_guarded_handback_or_go_without_starting_the_opening_again(self):
        for phase,active,held,command in (('takeover',False,False,'resume'),
                                          ('error',False,False,'resume'),
                                          ('playing',True,True,'go')):
            s,a,e=self.make(True);s.update(phase=phase,active=active)
            s.state['copilot']['held']=held
            before=copy.deepcopy(s.snapshot()['order'])
            reply={**s.snapshot(),'copilot':{**s.snapshot()['copilot'],
                   'command':{'id':'continue','action':command,'status':'confirmed','message':'Guarded continuation.'}}}
            with patch.object(a,'copilot_action',return_value=reply) as action:
                a.message('Keep going.',message_id='continue',approved=True,approve_preparation=True)
            self.assertEqual(action.call_args.args[0],command)
            self.assertEqual(s.calls,[]);e.assert_not_called()
            self.assertEqual(s.snapshot()['order'],before)
            self.assertEqual(a.remaining,0)
    def test_current_queue_start_does_not_override_manual_playback(self):
        s,a,e=self.make(True);s.update(phase='takeover',telemetry={'decks':[{'playing':True,'id':'b'}]})
        with self.assertRaisesRegex(ValueError,'already playing manually'):
            a.message('Start the current queue.',message_id='start')
        self.assertEqual(s.calls,[]);e.assert_not_called()
    def ready_opening(self):
        s,a,e=self.make(True)
        s.state['executionPlan'].update(orderIds=[t['id'] for t in s.state['order']],secondsBetweenEntries=30)
        return s,a,e
    def test_go_starts_exact_prepared_opening_once_without_reanalysis_or_model(self):
        for source in ('typed','voice'):
            s,a,e=self.ready_opening();before=copy.deepcopy(s.snapshot())
            a.playback_request={'id':'historical','revision':0,'sessionId':s.state['id'],'status':'failed'}
            a.message('Go',source=source,message_id='go-start',approved=True,approve_preparation=True)
            self.drain(a)
            a.message('Go',source=source,message_id='go-start',approved=True,approve_preparation=True)
            self.assertEqual(s.calls,[('start',)]);e.assert_not_called()
            self.assertEqual(s.snapshot()['order'],before['order'])
            self.assertEqual(s.snapshot()['executionPlan'],before['executionPlan'])
            self.assertEqual(a.playback_request['status'],'playing')
            self.assertEqual(a.remaining,0)
    def test_go_cannot_restart_runs_or_borrow_unapplied_or_different_queue_plan(self):
        changes=[{'phase':p} for p in ('playing','mixing','takeover','error','stopped','completed')]
        changes += [{'active':True},{'index':0},{'startedAt':100},{'endedAt':100},
                    {'resumeBlocked':True},{'queuedUpdate':{'tracks':[{'id':'x'}]}},
                    {'executionPlan':{'id':'bad','applied':False,'orderIds':['a','b','c']}},
                    {'executionPlan':{'id':'bad','applied':True,'orderIds':['b','a','c']}},
                    {'executionPlan':None},{'copilot':{'mode':'off'}}]
        for change in changes:
            with self.subTest(change=change):
                s,a,e=self.ready_opening();s.update(**change)
                reply={**s.snapshot(),'copilot':{'command':{'id':'go','action':'go','status':'blocked','message':'Guarded handoff.'}}}
                with patch.object(a,'copilot_action',return_value=reply) as action:
                    a.message('Go',message_id='go',approved=True,approve_preparation=True)
                self.assertEqual(action.call_args.args[0],'go')
                self.assertEqual(s.calls,[]);e.assert_not_called();self.assertIsNone(a.playback_request)
    def test_go_ready_opening_keeps_manual_playback_and_native_failure_guards(self):
        s,a,e=self.ready_opening();s.update(telemetry={'decks':[{'playing':True}]})
        with self.assertRaisesRegex(ValueError,'already playing manually'):a.message('Go',message_id='go')
        self.assertEqual(s.calls,[]);e.assert_not_called()
        s,a,e=self.ready_opening();s.start=Mock(side_effect=ValueError('Native cue readback changed.'))
        a.message('Go',message_id='go');self.drain(a)
        a.message('Go',message_id='go');self.drain(a)
        s.start.assert_called_once();e.assert_not_called()
        self.assertEqual(a.playback_request['status'],'failed')
    def test_go_rechecks_opening_under_session_lock_before_dispatch(self):
        from afterhours import playback_requests
        s,a,e=self.ready_opening();original=playback_requests.ready_opening
        def takeover(snapshot):
            accepted=original(snapshot)
            if accepted:s.update(phase='takeover')
            return accepted
        with patch.object(playback_requests,'ready_opening',side_effect=takeover):
            with self.assertRaisesRegex(ValueError,'opening changed'):
                a.message('Go',message_id='go')
        self.assertEqual(s.calls,[]);e.assert_not_called()
    def test_go_during_unfinished_selection_cannot_authorize_its_later_queue(self):
        for pending in ('evaluation','catalog'):
            with self.subTest(pending=pending):
                s,a,e=self.ready_opening();started=threading.Event();release=threading.Event()
                def slow(packet):
                    started.set();release.wait(2)
                    return decision(['b','a','c']),{'responseId':'later-queue'}
                if pending=='evaluation':
                    a.evaluator=slow
                    a.message('Try B first',message_id='select',approved=True)
                    self.assertTrue(started.wait(1))
                else:a.awaiting_catalog=True
                allowance=a.remaining
                try:
                    with self.assertRaisesRegex(ValueError,'No start was queued'):
                        a.message('Go',message_id='go',approved=True,approve_preparation=True)
                    self.assertEqual(a.remaining,allowance)
                    self.assertIsNone(a.playback_request)
                    self.assertNotIn(('start',),s.calls)
                finally:release.set();settled(a)
                if pending=='catalog':a.awaiting_catalog=False
                self.drain(a)
                self.assertIsNone(a.playback_request)
                self.assertNotIn(('start',),s.calls)
    def test_continuation_preserves_unconfirmed_cleanup_and_unknown_failure_latches(self):
        from test_copilot_commands import running
        for phase,blocked,error in (('takeover',True,None),('error',False,'Incoming cue moved.')):
            s=running();s.update(active=False,phase=phase,resumeBlocked=blocked,lastError=error)
            a=DJAgent(s,evaluator=Mock())
            with patch.object(s,'start') as start,patch.object(s,'factory') as factory:
                reply=a.message('Continue the current queue.',message_id='continue')
            self.assertEqual(reply['copilotControl']['status'],'blocked')
            start.assert_not_called();factory.assert_not_called();a.evaluator.assert_not_called()
            self.assertEqual(s.snapshot()['resumeBlocked'],blocked)
    def test_continuation_never_replays_a_stopped_or_completed_run_from_the_opening(self):
        for phase,index in (('stopped',1),('completed',2),('complete',2)):
            s,a,e=self.make(True);s.update(phase=phase,index=index,active=False,startedAt=100,endedAt=150)
            before=copy.deepcopy(s.snapshot())
            reply=a.message('Continue the current queue.',message_id='continue',approved=True,approve_preparation=True)
            self.drain(a)
            self.assertEqual(s.calls,[]);e.assert_not_called()
            self.assertEqual(s.snapshot(),before)
            self.assertEqual(reply['status'],'held')
            self.assertIn('No recording was restarted'.casefold(),reply['message'].casefold())
            self.assertIsNone(a.playback_request)
    def test_failed_selection_exits_choosing_and_fresh_request_can_recover(self):
        for source in ('typed','voice'):
            with self.subTest(source=source):
                s,a,e=self.make();s.update(order=[],candidates=[])
                if source=='voice':a.enable(True,120,3)
                e.side_effect=ValueError('Conversation context is too large for this bounded request.')
                a.message('start a session based on 90s house',source=source,
                          message_id='failed',approved=True,approve_preparation=True)
                settled(a)
                state=a.snapshot()
                self.assertEqual(state['status'],'error')
                self.assertEqual(state['playbackRequest']['status'],'failed')
                self.assertIn('context is too large',state['playbackRequest']['message'])
                self.assertFalse(state['evaluating']);self.assertEqual(a.remaining,0)
                self.drain(a);self.assertEqual(e.call_count,1)
                self.assertNotIn(('start',),s.calls)
                e.side_effect=None
                e.return_value=({**decision(search={'query':'Daft Punk','artist':'','genre':'','bpmMin':40,'bpmMax':300}),
                    'playbackAction':{'requestId':'fresh','action':'start-now'} if source=='typed' else None},{})
                if source=='voice':a.enable(True,120,3)
                a.message('start a session based on Daft Punk',source=source,
                          message_id='fresh',approved=True,approve_preparation=True)
                settled(a)
                self.assertEqual(a.playback_request['status'],'choosing')
                self.assertEqual(e.call_count,2)

    def test_artist_session_request_starts_selected_music_without_another_confirmation(self):
        for source in ('typed','voice'):
            with self.subTest(source=source):
                s,a,e=self.make();s.update(order=[],candidates=[])
                if source=='voice':a.enable(True,120,3)
                e.return_value=({**decision(search={'query':'Daft Punk','artist':'','genre':'','bpmMin':40,'bpmMax':300}),
                    'playbackAction':{'requestId':'artist-session','action':'start-now'} if source=='typed' else None},{})
                a.message('lets start a new session based on daft punk',source=source,
                          message_id='artist-session',approved=source=='typed',approve_preparation=source=='typed')
                settled(a)
                self.assertEqual(a.playback_request['status'],'choosing')
                self.assertNotIn(('start',),s.calls)
                s.finish_search();e.return_value=(decision(['a','b','c']),{})
                for _ in range(12):
                    a.tick(force=True);settled(a)
                    if (a.playback_request or {}).get('status')=='playing':break
                self.assertEqual([x[0] for x in s.calls],['search','start'])
                self.assertEqual(a.playback_request['status'],'playing')
                self.assertEqual(e.call_count,2)
    def test_prepared_queue_starts_once_without_a_model_call(self):
        s,a,e=self.make(True)
        a.message('start playing',message_id='start');self.drain(a)
        self.assertEqual(s.calls,[('start',)]);e.assert_not_called()
        self.assertEqual(a.snapshot()['playbackRequest']['status'],'playing')
        a.message('start playing',message_id='start');self.drain(a)
        a.message('start playing',message_id='another');self.drain(a)
        self.assertEqual(s.calls,[('start',)])
    def test_typed_start_does_not_wait_for_paused_full_set_analysis(self):
        s,a,e=self.make()
        a.message('start playing',message_id='start',approved=True,approve_preparation=True);self.drain(a)
        self.assertEqual(s.calls,[('start',)])
        self.assertEqual(a.snapshot()['playbackRequest']['status'],'playing');e.assert_not_called()
    def test_only_receipted_opening_subset_can_continue_a_start(self):
        for receipt in (True,False):
            s,a,e=self.make();s.start=Mock()
            a.message('start playing',message_id='start')
            s.update(order=[s.state['order'][0],s.state['order'][2]],active=True,phase='playing',
                     telemetry={'decks':[{'playing':True,'id':'a'}]})
            if receipt:s.update(startup={'requestedIds':['a','b','c'],'status':'ready',
                'skipped':[{'trackId':'b','code':'limited-decoded-audio','reason':'Only 30 seconds.'}]})
            a.advance_playback()
            self.assertEqual(a.playback_request['status'],'playing' if receipt else 'failed')

    def test_voice_start_needs_no_paid_review_and_keeps_existing_allowance(self):
        s,a,e=self.make();a.enable(True,120,3)
        a.message('start playing',source='voice',message_id='voice-start')
        self.assertEqual(a.remaining,3)
        a.disable(preserve_playback=True);self.drain(a)
        self.assertEqual(a.remaining,3)
        self.assertTrue(a.snapshot()['allowance']['background'])
        self.assertEqual(s.calls,[('start',)])

    def test_after_talk_closes_refill_uses_only_the_existing_count_and_deadline(self):
        s,a,e=self.make(True);now=[1000.0];a.clock=lambda:now[0]
        a.enable(True,120,3);a.state['direction']='House'
        a.message('start playing',source='voice',message_id='voice-start')
        original=a.deadline;a.disable(preserve_playback=True)
        e.return_value=(decision(['a','b','c','d']),{'responseId':'offline-refill'})
        self.drain(a)
        self.assertEqual(e.call_count,1)
        self.assertEqual(a.remaining,2);self.assertEqual(a.deadline,original)
        self.assertEqual([t['id'] for t in s.snapshot()['queuedUpdate']['tracks']],['a','b','c','d'])
        self.assertEqual(e.call_args.args[0]['reason'],'refill upcoming queue')
        self.assertEqual(s.calls,[('start',)])
        now[0]=original+1;s.update(queuedUpdate=None,index=1)
        self.drain(a)
        self.assertEqual(e.call_count,1);self.assertEqual(a.remaining,0)
        self.assertTrue(s.snapshot()['active']);self.assertFalse(a.snapshot()['allowance']['background'])

    def test_stop_takeover_and_completion_end_background_selection_and_update_start_receipt(self):
        for phase,receipt in [('stopped','stopped'),('takeover','manual'),('complete','completed'),('error','failed')]:
            s,a,e=self.make(True);a.enable(True,120,3)
            a.message('start playing',source='voice',message_id='start');self.drain(a)
            a.disable(preserve_playback=True)
            s.update(active=False,phase=phase,telemetry={'decks':[]});self.drain(a)
            self.assertEqual(a.playback_request['status'],receipt)
            self.assertEqual(a.remaining,0);self.assertFalse(a.background_allowance)
            e.assert_not_called()

    def test_new_voice_allowance_replaces_background_count_instead_of_adding_to_it(self):
        s,a,e=self.make(True);a.enable(True,120,3)
        a.message('start playing',source='voice',message_id='start');self.drain(a)
        a.disable(preserve_playback=True);a.enable(True,120,6)
        self.assertEqual(a.remaining,6);self.assertFalse(a.background_allowance)

    def test_closed_talk_without_explicit_play_never_keeps_background_permission(self):
        s,a,e=self.make(True);a.enable(True,120,3)
        s.update(active=True,phase='playing',index=0)
        a.disable(preserve_playback=True)
        self.assertEqual(a.remaining,0);self.assertFalse(a.background_allowance)
    def test_explicit_start_of_selected_music_spends_no_provider_allowance(self):
        s,a,e=self.make()
        a.message('start playing',source='voice',message_id='local');self.drain(a)
        self.assertEqual(s.calls,[('start',)]);e.assert_not_called()
        self.assertEqual(a.remaining,0)
    def test_status_question_does_not_cancel_a_pending_start_or_spend_another_call(self):
        s,a,e=self.make();a.message('play some music',message_id='start',approved=True,approve_preparation=True)
        revision=a.state['revision'];request=copy.deepcopy(a.playback_request)
        a.message('Why are you still analyzing?',source='voice',message_id='status')
        self.assertEqual(a.state['revision'],revision);self.assertEqual(a.playback_request,request)
        self.drain(a);self.assertIn(('start',),s.calls);e.assert_not_called()
    def test_play_artist_search_selects_and_prepares_before_starting(self):
        s,a,e=self.make();s.update(order=[],candidates=[])
        e.return_value=({**decision(search={'query':'CamelPhat','artist':'','genre':'','bpmMin':40,'bpmMax':300}),
            'playbackAction':{'requestId':'artist','action':'start-now'}},{})
        a.message('play CamelPhat',message_id='artist',approved=True,approve_preparation=True);settled(a)
        self.assertEqual(a.playback_request['status'],'choosing')
        self.assertNotIn(('start',),s.calls)
        s.finish_search();e.return_value=(decision(['a','b','c']),{})
        self.drain(a)
        self.assertEqual([x[0] for x in s.calls],['search','start'])
        self.assertEqual(e.call_count,2)
    def test_selection_without_play_intent_never_starts(self):
        s,a,e=self.make()
        a.message('Choose some house music',message_id='select',approved=True,approve_preparation=True);settled(a);self.drain(a)
        self.assertEqual(s.calls,[('analyze',),('review',),('apply',)])
        self.assertIsNone(a.playback_request)
    def test_manual_play_changed_queue_new_direction_and_expiry_cancel_pending_start(self):
        for change in ['manual','order','direction','expired']:
            s,a,e=self.make();s.update(active=True,phase='preparing')
            stop=s.stop;s.stop=Mock()
            a.message('start playing',message_id='start',approved=True,approve_preparation=True)
            s.update(active=False,phase='stopped')
            if change=='manual':s.update(telemetry={'decks':[{'playing':True}]})
            elif change=='order':s.update(order=s.state['order'][:2])
            elif change=='direction':a.message('Keep the mood steady',message_id='new');settled(a)
            else:a.playback_request['expiresAt']=0
            self.drain(a);self.assertNotIn(('start',),s.calls,change)
    def test_uncertain_start_is_not_retried_and_stop_cancels_it(self):
        s,a,e=self.make(True);s.start=Mock(side_effect=OSError('Start acknowledgement unavailable'))
        a.message('start playing',message_id='start');self.drain(a)
        a.message('start playing',message_id='start');self.drain(a)
        s.start.assert_called_once();self.assertEqual(a.playback_request['status'],'failed')
        a.message('stop music',message_id='stop');self.assertEqual(s.calls,[('stop',)])
    def test_restart_never_restores_playback_permission(self):
        with tempfile.TemporaryDirectory() as directory:
            s,a,e=self.make();s.update(active=True,phase='preparing');s.stop=Mock();a.path=Path(directory)/'agent.json'
            a.message('start playing',message_id='start',approved=True,approve_preparation=True)
            restored=DJAgent(s,a.path,evaluator=e);self.drain(restored)
            self.assertIsNone(restored.playback_request);self.assertNotIn(('start',),s.calls)

class PauseWordTests(unittest.TestCase):
    """Live use, 11 Nov 2026: a typed "pause" went to the planner, which said "Pause requested" and paused nothing."""
    def test_plain_pause_and_stop_phrasings_stop_the_music(self):
        from afterhours import playback_requests
        for text in ('pause','Pause.','can you pause?','pause it','pause please','pause playback','pause the set','pause the track',
                     'stop it','stop the track','stop playback','hold the music','Stop the music now.', '. Stop the music now',
                     'Hey Dot, could you stop the music right now?', 'Dot, pause playback, please.'):
            self.assertEqual(playback_requests.intent(text),'stop',text)

    def test_pause_inside_a_longer_request_is_not_a_transport_command(self):
        from afterhours import playback_requests
        for text in ('pause after this track','do not pause','add a pause before the drop','something with a long pause in it'):
            self.assertIsNone(playback_requests.intent(text),text)


if __name__=='__main__':unittest.main()
