"""Conversation decisions with a fake provider and fake session; never live audio."""
import copy,json,tempfile,threading,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from afterhours.dj_agent import DJAgent,context_key,music_context,validate_decision,evaluate,TEXT_REQUESTS,TEXT_REQUEST_RESERVE,TEXT_TURN_CEILING

class Session:
    def __init__(self):
        self.state={'id':'fixture','phase':'stopped','active':False,'index':-1,
            'candidates':[{'id':c,'title':c.upper(),'artist':'Fixture','bpm':125,'camelot':'8A'} for c in 'abc'],
            'order':[],'setlist':{'allowReorder':True,'lockedIds':[]},'telemetry':{'decks':[]}}
        self.state['order']=copy.deepcopy(self.state['candidates']);self.calls=[]
    def snapshot(self):return copy.deepcopy(self.state)
    def update(self,**values):self.state.update(values)
    def plan(self,ids,*args):self.calls.append(('plan',ids));self.state['order']=[t for i in ids for t in self.state['candidates'] if t['id']==i]
    def discover(self,*args):self.calls.append(('discover',args));self.state['phase']='discovering'
    def discover_more(self,*args,**kwargs):self.discover(*args)
    def revise_queue(self,ids,**kwargs):
        self.plan(ids);self.update(message='Queue accepted.')
    def prepare_analysis(self):
        self.calls.append(('analyze',));self.update(analysis={'tracks':copy.deepcopy(self.state['order'])})
    def review_mix(self,approved=False):
        self.calls.append(('review',approved));self.update(aiReview={'decision':'propose'})
    def apply_plan(self):
        self.calls.append(('apply',));self.update(executionPlan={'applied':True})
    def new_set(self):self.state.update(id='fresh-fixture',phase='idle',candidates=[],order=[],telemetry=None)

def decision(ids=None,search=None):
    return {'reply':'I would keep a steady house groove.','direction':'Early 2000s house at 125 BPM.','search':search,
            'trackIds':ids or [],'reason':'Compatible tempo and the requested musical direction.','mixTransitions':None}

def settled(a):
    if a.worker:a.worker.join(2)
    assert not (a.worker and a.worker.is_alive())
    return a.snapshot()

class StandingBriefTests(unittest.TestCase):
    """Owner direction, 11 Nov 2026: a requested set starts at once and is not capped at four records."""
    def test_asking_for_a_set_to_be_built_is_a_request_to_hear_it(self):
        from afterhours import dj_agent
        self.assertIn('built, made, put together, started or played',dj_agent.START_INSTRUCTIONS)
        self.assertIn('prepare only, wait or not start yet',dj_agent.START_INSTRUCTIONS)

    def test_a_new_set_opens_deeper_than_four_records(self):
        from afterhours import dj_agent
        import inspect
        self.assertIn("'openingTarget':6",inspect.getsource(dj_agent.DJAgent.tick))
        self.assertIn('6-8 fitting recordings',dj_agent.INSTRUCTIONS)
        self.assertNotIn('useful small opening',dj_agent.INSTRUCTIONS)


class AgentTests(unittest.TestCase):
    def test_planner_distinguishes_track_cadence_seconds_from_blend_beats(self):
        s=Session();s.update(secondsPerTrack=30,executionPlan={'secondsBetweenEntries':30,'transitions':[{'fadeBeats':16},{'fadeBeats':32}]})
        timing=music_context(s.snapshot())['timing']
        self.assertEqual(timing['secondsBetweenEntries'],30)
        self.assertEqual(timing['appliedSecondsBetweenEntries'],30)
        self.assertEqual(timing['appliedBlendBeats'],[16,32])
        s.update(executionPlan=None)
        timing=music_context(s.snapshot())['timing']
        self.assertIsNone(timing['appliedSecondsBetweenEntries']);self.assertEqual(timing['appliedBlendBeats'],[])
    def test_appended_artist_matches_survive_catalog_limit_and_avoid_unavailable_recordings(self):
        s=Session()
        s.state['candidates'] += [dict(id=f'old-{i}',title=f'Unrelated {i}',artist='Another artist',bpm=125) for i in range(45)]
        s.state['candidates'] += [dict(id='requested',title='New song',artist='Requested Trio',bpm=126),
                                  dict(id='absent',title='Older song',artist='Requested Trio',bpm=125)]
        s.state['recordingAvailability']=[dict(trackId='absent',reason='Not found')]
        music=music_context(s.snapshot(),request='add Requested Trio into the queue')
        ids=[t['id'] for t in music['catalog']]
        self.assertEqual(ids[:4],['a','b','c','requested'])
        self.assertEqual(music['requestedIds'],['requested'])
        self.assertNotIn('absent',ids);self.assertEqual(len(ids),32)

    def test_search_followup_keeps_latest_user_request_when_context_is_trimmed(self):
        s,a,e=self.make(decision())
        a.enable(True,120,3)
        user={'id':'artist-add','role':'user','text':'add daft punk into the queue','source':'typed','at':a.clock()}
        a.state['messages']=[user,{'role':'assistant','text':'Old chosen title is unavailable. '*1000}]
        a.state['direction']='An earlier model plan restricted the title.'
        a.state['status']='searching';a.awaiting_catalog=True
        s.update(phase='catalog-ready',catalogSearch={'status':'complete','added':6})
        a.tick(force=True);settled(a)
        packet=e.call_args.args[0]
        self.assertEqual(packet['operatorRequest'],user)
        self.assertIn(user,packet['messages'])
        self.assertEqual(packet['reason'],'choose from catalog results')

    def test_selected_track_retires_its_held_entry_without_losing_other_requests(self):
        s=Session();s.state['candidates'].append(dict(id='d',title='D',artist='Fixture',bpm=125))
        proposal=decision(['a','b','c'])
        proposal['deferredTracks']=[{'trackId':'c','reason':'Previously held.'},{'trackId':'d','reason':'For later.'}]
        result=validate_decision(proposal,s.snapshot())
        self.assertEqual(result['trackIds'],['a','b','c'])
        self.assertEqual(result['deferredTracks'],[{'trackId':'d','reason':'For later.'}])
        proposal['deferredTracks'].append({'trackId':'missing','reason':'Unknown identity.'})
        with self.assertRaises(ValueError):validate_decision(proposal,s.snapshot())

    def test_general_explanation_receives_fresh_deck_facts(self):
        s,a,e=self.make(decision())
        a.clock=lambda:101
        s.update(telemetry={'sampledAt':100,'decks':[{'deck':2,'id':'b','bpm':128,'playing':False}]},
                 executionPlan={'targetBpm':123})
        a.message('Explain the difference between the original BPM and the current deck tempo. Keep everything unchanged.',
                  message_id='explain',approved=True)
        settled(a)
        music=e.call_args.args[0]['music']
        self.assertEqual(music['deckFacts']['decks'][0]['currentBpm'],128)
        self.assertEqual(music['deckFacts']['decks'][0]['originalBpm'],125)
        self.assertEqual(music['tempoPolicy']['targetBpm'],123)
        self.assertEqual(s.calls,[])

    def test_factual_typed_and_voice_answers_use_queue_receipts_not_provider_claims(self):
        for source in ('typed','voice'):
            result=decision();result['reply']='House Dawgs is queued after Camel Toe.'
            s,a,e=self.make(result)
            camel={'id':'camel','title':'Camel Toe','artist':'Fixture','bpm':123}
            dawgs={'id':'dawgs','title':'House Dawgs','artist':'Fixture','bpm':126}
            s.update(order=[camel],candidates=[camel,dawgs])
            a.state['direction']='Keep the current musical direction.'
            held=[{'trackId':'dawgs','title':'House Dawgs','bpm':126,'reason':'For a later tempo direction.'}]
            a.state['deferredTracks']=copy.deepcopy(held)
            result['deferredTracks']=[]
            a.message('What is the tempo of Camel Toe, and what is queued after it? Keep playback paused.',
                      source=source,message_id='question',approved=True)
            state=settled(a)
            self.assertEqual(state['messages'][-1]['text'],
                             'Camel Toe is 123 BPM. Nothing follows it in the current queue.')
            self.assertEqual(state['direction'],'Keep the current musical direction.')
            self.assertEqual(state['deferredTracks'],held)
            self.assertEqual(s.calls,[]);self.assertEqual(s.state['order'],[camel])
            self.assertEqual(e.call_args.args[0]['music']['queueFacts']['current'],[{'id':'camel','title':'Camel Toe'}])
            self.assertEqual(state['proposal']['trackIds'],[])

    def make(self,result=None):
        s=Session();evaluate=Mock(return_value=(result or decision(),{'responseId':'fixture-response'}));a=DJAgent(s,evaluator=evaluate);return s,a,evaluate
    def test_without_approval_saves_direction_without_request(self):
        s,a,e=self.make();a.message('House, please',message_id='one');settled(a);e.assert_not_called()
        self.assertEqual(a.snapshot()['messages'][0]['text'],'House, please');self.assertEqual(s.calls,[])
    def test_one_approved_message_and_duplicate_do_not_repeat_request(self):
        s,a,e=self.make(decision(['a','b','c']));s.update(executionPlan={'applied':True})
        a.message('Keep this order',message_id='one',approved=True);settled(a)
        a.message('Keep this order',message_id='one',approved=True);a.tick(force=True)
        self.assertEqual(e.call_count,1);self.assertEqual(a.snapshot()['allowance']['remaining'],0)
        self.assertEqual(s.calls,[]);self.assertTrue(s.state['executionPlan']['applied'])
    def test_new_paused_order_enters_existing_pipeline(self):
        s,a,e=self.make(decision(['b','a','c']));a.message('Try B first',message_id='one',approved=True);settled(a)
        self.assertEqual(s.calls,[('plan',['b','a','c'])]);self.assertEqual(a.snapshot()['proposal']['status'],'applied')
    def test_playing_decks_protect_opening_selection(self):
        for phase,active,playing in [('playing',True,True),('takeover',False,True),('stopped',False,True)]:
            s,a,e=self.make(decision(['b','a','c']));s.update(phase=phase,active=active,telemetry={'decks':[{'playing':playing}]})
            a.message('Try B first',message_id='one',approved=True);settled(a)
            self.assertEqual(s.calls,[])
            self.assertEqual(a.snapshot()['status'],'error')
    def test_paused_takeover_can_choose_a_new_set_without_starting_audio(self):
        s,a,e=self.make(decision(['b','a','c']));s.update(phase='takeover',active=False)
        a.message('Try B first',message_id='one',approved=True);settled(a)
        self.assertEqual(s.calls,[('plan',['b','a','c'])])
        self.assertEqual(a.snapshot()['proposal']['status'],'applied')
    def test_invalid_and_locked_ids_are_rejected(self):
        s=Session()
        for ids in [['x','a'],['a','a'],['a'],['a','c','b']]:
            s.state['setlist']['lockedIds']=['b']
            with self.assertRaises(ValueError):validate_decision(decision(ids),s.snapshot())
        s.state['setlist']={'allowReorder':False,'lockedIds':[]}
        with self.assertRaises(ValueError):validate_decision(decision(['c','b','a']),s.snapshot())
    def test_context_change_or_new_message_drops_stale_result(self):
        for kind in ('context','message','disable'):
            s=Session();started=threading.Event();release=threading.Event()
            def slow(packet):started.set();release.wait(2);return decision(['b','a','c']),{'responseId':'old'}
            a=DJAgent(s,evaluator=slow);a.message('First request',message_id='one',approved=True);self.assertTrue(started.wait(1))
            if kind=='context':s.update(index=1)
            elif kind=='message':a.message('Actually warmer',message_id='two')
            else:a.disable()
            release.set();state=settled(a)
            self.assertIsNone(state['lastEvaluation']);self.assertEqual(s.calls,[])
    def test_allowance_does_not_survive_restart_and_direction_does(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'conversation.json';s=Session();a=DJAgent(s,path,evaluator=lambda p:(decision(),{'responseId':'saved'}))
            a.message('House',message_id='one',approved=True);settled(a)
            b=DJAgent(s,path);self.assertEqual(b.snapshot()['direction'],'Early 2000s house at 125 BPM.')
            self.assertEqual(b.snapshot()['allowance']['remaining'],0);self.assertEqual(path.stat().st_mode&0o777,0o600)
    def test_ordinary_state_changes_do_not_restart_search_or_selection(self):
        s,a,e=self.make();a.enable(True,120,2);a.message('House',message_id='one');settled(a)
        key=context_key(s.snapshot());s.update(telemetry={'sampledAt':123,'decks':[{'position':32}]})
        self.assertEqual(key,context_key(s.snapshot()));a.tick(force=True);self.assertEqual(e.call_count,1)
        s.update(index=1,phase='ready');a.tick(force=True);settled(a);self.assertEqual(e.call_count,1)
        s.update(index=2);a.tick(force=True);self.assertEqual(e.call_count,1)

    def test_search_results_commit_a_starter_queue_without_searching_again(self):
        q={'query':'House reference','artist':'','genre':'House','bpmMin':120,'bpmMax':130}
        s,a,e=self.make(decision(search=q));s.update(order=[])
        a.enable(True,1800,60);a.message('A few house tracks',message_id='seed');settled(a)
        self.assertEqual(s.calls[0][0],'discover')
        e.return_value=(decision(['a','b','c']),{'responseId':'selected'})
        s.update(phase='catalog-ready');a.tick(force=True);settled(a)
        self.assertFalse(e.call_args.args[0]['searchPolicy']['canSearch'])
        self.assertEqual(s.calls[-1],('plan',['a','b','c']))
        s.update(phase='ready');a.tick(force=True);settled(a)
        self.assertEqual(e.call_count,2)

    def test_repeat_search_and_incompatible_search_after_success_are_held(self):
        q={'query':'House reference','artist':'','genre':'House','bpmMin':120,'bpmMax':130}
        s,a,e=self.make(decision(search=q));a.enable(True,1800,60)
        a.message('House',message_id='seed');settled(a);s.update(phase='catalog-ready')
        a.tick(force=True);settled(a);a.tick(force=True)
        self.assertEqual(e.call_count,2);self.assertEqual(len(s.calls),1)
        self.assertEqual(a.snapshot()['status'],'error')

    def test_low_queue_refill_is_bounded_and_new_steer_is_immediate(self):
        s,a,e=self.make(decision(['a','b','c']));s.update(active=True,phase='playing',index=0)
        a.enable(True,1800,60);a.state['direction']='House';a.tick(force=True);settled(a)
        self.assertEqual(e.call_count,1);a.tick(force=True);settled(a);self.assertEqual(e.call_count,1)
        a.message('Make the next part deeper',message_id='steer');settled(a)
        self.assertEqual(e.call_count,2)

    def test_opening_preparation_uses_existing_pipeline_and_one_more_budgeted_decision(self):
        s,a,e=self.make(decision(['b','a','c']));a.enable(True,1800,4)
        a.message('A few tracks to begin',message_id='seed');settled(a)
        for _ in range(4):a.tick(force=True)
        self.assertEqual(s.calls,[('plan',['b','a','c']),('analyze',),('review',True),('apply',)])
        self.assertEqual(a.snapshot()['allowance']['remaining'],2)
        self.assertIn('prepared',a.snapshot()['message']);self.assertTrue(s.state['executionPlan']['applied'])
        for _ in range(4):a.tick(force=True)
        self.assertEqual(e.call_count,1)

    def test_one_text_request_never_grants_an_extra_paid_mix_plan(self):
        s,a,e=self.make(decision(['b','a','c']));a.message('A few tracks',message_id='seed',approved=True);settled(a)
        for _ in range(4):a.tick(force=True)
        self.assertEqual(s.calls,[('plan',['b','a','c']),('analyze',)])
        self.assertEqual(a.snapshot()['allowance']['remaining'],0)

    def test_typed_search_finishes_selection_without_talk_or_an_extra_message(self):
        q={'query':'Dom Dolla','artist':'Dom Dolla','genre':'','bpmMin':40,'bpmMax':300}
        s,a,e=self.make(decision(search=q));s.update(order=[])
        a.message('dom dolala',message_id='seed',approved=True);settled(a)
        self.assertEqual(a.snapshot()['allowance']['remaining'],1)
        self.assertTrue(e.call_args.args[0]['typedTurn'])
        e.return_value=(decision(['a','b','c']),{'responseId':'selection'})
        s.update(phase='catalog-ready',catalogSearch={'status':'complete','added':3})
        a.tick();settled(a)
        self.assertFalse(e.call_args.args[0]['searchPolicy']['canSearch'])
        for _ in range(6):a.tick(force=True)
        self.assertEqual(e.call_count,2)
        self.assertEqual(s.calls,[('discover',('Dom Dolla','Dom Dolla','',40,300)),('plan',['a','b','c']),('analyze',)])
        self.assertEqual(a.snapshot()['allowance']['remaining'],0)
        self.assertLessEqual(TEXT_REQUESTS*TEXT_REQUEST_RESERVE,TEXT_TURN_CEILING)

    def test_fresh_message_resets_old_constraints_and_duplicate_cannot_clear_again(self):
        s,a,e=self.make();a.state['direction']='Dark deep house, 120-126 BPM'
        a.state['messages']=[{'id':'old','role':'user','text':'Old deep house'}]
        a.message('dom dolla',message_id='new',approved=True,queue_mode='replace');settled(a)
        packet=e.call_args.args[0]
        self.assertEqual(packet['queueMode'],'replace');self.assertEqual(packet['direction'],'')
        self.assertEqual(packet['music']['currentOrder'],[]);self.assertEqual(packet['music']['catalog'],[])
        self.assertEqual(packet['pastSets'],[])
        self.assertEqual([m['text'] for m in packet['messages']],['dom dolla'])
        self.assertEqual(len(a.snapshot()['pastSets']),1)
        s.state['order']=[{'id':'new-selected'}]
        a.message('dom dolla',message_id='new',approved=True,queue_mode='replace');settled(a)
        self.assertEqual(s.state['order'],[{'id':'new-selected'}]);self.assertEqual(e.call_count,1)

    def test_fresh_rejection_keeps_original_queue_conversation_and_allowance(self):
        for kwargs in ({'queue_mode':'invalid','approved':True},{'queue_mode':'replace','approved':False}):
            s,a,e=self.make();before=s.snapshot();old=a.snapshot()
            with self.assertRaises(ValueError):a.message('Dom Dolla',message_id='no',**kwargs)
            self.assertEqual(s.snapshot(),before);self.assertEqual(a.snapshot(),old);e.assert_not_called()
        s,a,e=self.make();s.new_set=Mock(side_effect=ValueError('Pause playback first.'));before=a.snapshot()
        with self.assertRaises(ValueError):a.message('Dom Dolla',message_id='no',approved=True,queue_mode='replace')
        self.assertEqual(a.snapshot(),before);e.assert_not_called()

    def test_native_playback_blocks_fresh_even_if_client_claims_it_is_stopped(self):
        from afterhours.vdj_session import DJSession
        for active,playing in [(True,False),(False,True)]:
            session=DJSession(threading.Lock())
            session.update(active=active,telemetry={'decks':[{'playing':playing}]})
            evaluator=Mock();agent=DJAgent(session,evaluator=evaluator)
            before=session.snapshot()
            with self.assertRaisesRegex(ValueError,'Pause playback'):
                agent.message('Dom Dolla',message_id='playing',approved=True,queue_mode='replace')
            self.assertEqual(session.snapshot(),before);evaluator.assert_not_called()

    def test_search_can_echo_only_the_unchanged_queue(self):
        q={'query':'Dom Dolla','artist':'','genre':'','bpmMin':40,'bpmMax':300}
        s=Session();raw=decision(['a','b','c'],q)
        self.assertEqual(validate_decision(raw,s.snapshot())['trackIds'],[])
        self.assertEqual(raw['trackIds'],['a','b','c'])
        for ids in (['b','a','c'],['a','b']):
            with self.assertRaises(ValueError):validate_decision(decision(ids,q),s.snapshot())

    def test_zero_results_are_reported_without_a_search_loop(self):
        q={'query':'Unknown artist','artist':'','genre':'','bpmMin':40,'bpmMax':300}
        s,a,e=self.make(decision(search=q));a.message('Unknown artist',message_id='zero',approved=True);settled(a)
        s.update(phase='stopped',catalogSearch={'status':'complete','added':0})
        reply=decision();reply['reply']='No matching recordings found. Try another artist.'
        e.return_value=(reply,{'responseId':'empty'})
        a.tick(force=True);settled(a)
        for _ in range(5):a.tick(force=True)
        self.assertEqual(e.call_count,2);self.assertFalse(e.call_args.args[0]['searchPolicy']['canSearch'])
        self.assertEqual(a.snapshot()['message'],reply['reply']);self.assertEqual(len(s.calls),1)

    def test_catalog_error_does_not_spend_the_followup_request(self):
        q={'query':'Dom Dolla','artist':'','genre':'','bpmMin':40,'bpmMax':300}
        s,a,e=self.make(decision(search=q));a.message('Dom Dolla',message_id='search',approved=True);settled(a)
        s.update(phase='stopped',catalogSearch={'status':'error','message':'Catalog disconnected.'})
        a.tick(force=True);a.tick(force=True)
        self.assertEqual(e.call_count,1);self.assertEqual(a.snapshot()['status'],'error')
        self.assertIn('Catalog disconnected',a.snapshot()['message'])

    def test_text_budget_requires_the_verified_model_before_provider_call(self):
        opener=Mock(side_effect=AssertionError('No provider call expected'))
        with patch('afterhours.ai_mix.api_key',return_value='test-key'),patch('afterhours.ai_mix.MODEL','different-model'):
            with self.assertRaisesRegex(ValueError,'pricing needs verification'):evaluate({'typedTurn':True},opener)
        opener.assert_not_called()

    def test_resuming_same_selected_queue_reuses_analysis_instead_of_researching(self):
        s,a,e=self.make(decision(['a','b','c']));s.update(analysis={'tracks':copy.deepcopy(s.state['order'])})
        a.enable(True,1800,4);a.message('Prepare these',message_id='resume');settled(a)
        for _ in range(4):a.tick(force=True)
        self.assertEqual(s.calls,[('review',True),('apply',)])
        self.assertEqual(e.call_count,1)

    def test_old_unrelated_crate_does_not_prevent_one_refined_search(self):
        q={'query':'New reference','artist':'','genre':'House','bpmMin':120,'bpmMax':130}
        s,a,e=self.make(decision(search=q));a.enable(True,1800,4);a.message('New music',message_id='seed');settled(a)
        s.update(phase='catalog-ready',catalogSearch={'status':'complete','added':0})
        e.return_value=(decision(search={**q,'query':'Different reference'}),{'responseId':'refined'})
        a.tick(force=True);settled(a)
        self.assertTrue(e.call_args.args[0]['searchPolicy']['canSearch'])
        self.assertEqual(len(s.calls),2)
        s.update(phase='catalog-ready',catalogSearch={'status':'complete','added':0})
        e.return_value=(decision(),{'responseId':'held'})
        a.tick(force=True);settled(a)
        self.assertFalse(e.call_args.args[0]['searchPolicy']['canSearch'])

    def test_ending_talk_stops_further_automatic_preparation(self):
        s,a,e=self.make(decision(['b','a','c']));a.enable(True,1800,4)
        a.message('A few tracks',message_id='seed');settled(a);a.tick(force=True)
        a.disable();a.tick(force=True)
        self.assertEqual(s.calls,[('plan',['b','a','c']),('analyze',)])
    def test_search_uses_catalog_pipeline_but_cannot_be_combined_with_ids(self):
        q={'query':'Groove Armada; Roger Sanchez','artist':'','genre':'House','bpmMin':120,'bpmMax':130}
        s,a,e=self.make(decision(search=q));a.message('Different house',message_id='one',approved=True);settled(a)
        self.assertEqual(s.calls[0][0],'discover')
        with self.assertRaises(ValueError):validate_decision(decision(['a','b'],q),Session().snapshot())
    def test_expired_unused_allowance_can_be_reapproved(self):
        s,a,e=self.make();a.enable(True,1,3);a.deadline=0
        a.message('House',message_id='one',approved=True);settled(a);self.assertEqual(e.call_count,1)
    def test_provider_failure_is_not_retried(self):
        s,a,e=self.make();e.side_effect=ValueError('Synthetic provider failure')
        a.message('House',message_id='one',approved=True);settled(a);a.tick(force=True)
        self.assertEqual(e.call_count,1);self.assertEqual(a.snapshot()['status'],'error');self.assertEqual(s.calls,[])

    def test_fresh_set_remembers_context_without_seeding_or_reusing_old_ids(self):
        s,a,e=self.make();a.state['direction']='House around 125 BPM';a.state['messages']=[{'id':'old','text':'Old direction','role':'user'}]
        fresh=a.fresh();self.assertEqual(fresh['direction'],'');self.assertEqual(fresh['messages'],[])
        self.assertEqual(s.state['candidates'],[]);self.assertEqual(s.state['order'],[]);e.assert_not_called()
        remembered=a.past_context('that house set');self.assertEqual(remembered[0]['direction'],'House around 125 BPM')
        self.assertEqual(len(remembered[0]['tracks']),3);self.assertNotIn('id',remembered[0]['tracks'][0])
        a.message('What did we choose before?',message_id='recall',approved=True);settled(a)
        packet=e.call_args.args[0];self.assertEqual(packet['music']['catalog'],[]);self.assertTrue(packet['pastSets'])

if __name__=='__main__':unittest.main()
