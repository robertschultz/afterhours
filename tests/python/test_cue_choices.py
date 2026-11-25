"""The planner choosing where a record comes in; fixtures only, never live audio.

Issue #88: the engine measures several mix windows per analysed recording and
used to pick one by formula. The planner may now name a window for a queued
recording. The engine uses it only if it is one of that recording's verified
windows, the recording is not already playing or cued, and the window can
carry the planned blend. Otherwise it keeps its own choice and says why.
"""
import copy,json,unittest
from unittest.mock import patch

from afterhours import agent_loop
from afterhours import cue_planner
from afterhours import dj_agent
import test_vdj_discovery
from afterhours.dj_agent import DJAgent,planning_request,validate_decision,music_context
from test_agent_loop import Provider,asks,call,ready,decides,full_decision,settled
from test_dj_agent import Session,decision


def prepared():
    """A playing four-record set, analysed and planned; records 2 and 3 have other windows."""
    session,_=test_vdj_discovery.QueueRevisionTests().prepared()
    s=session.snapshot();tracks=s['analysis']['tracks']
    def window(track,name,**changes):
        cue=copy.deepcopy(track['cueCandidates'][0]);cue.update(id=name,**changes);track['cueCandidates'].append(cue)
    window(tracks[2],'window-late-2',offsetBeats=128,entryBeat=128,entrySeconds=49.548387,introEndSeconds=61.935484,
           exitBeat=192,exitSeconds=74.322581,endBeat=224,endSeconds=86.709677)
    window(tracks[2],'window-unverified-2',replayVerified=False)
    window(tracks[3],'window-short-3',maxBlendBeats=8)
    return session,s


def ids(s):return [t['id'] for t in s['order']]
def pick(track,window,reason='Comes in on the breakdown, where the bass drops away.'):
    return {'trackId':track,'windowId':window,'reason':reason}
def chosen(plan):return {row['trackId']:row['id'] for row in plan['executionPlan']['selections']}


class RevisionTests(unittest.TestCase):
    def revise(self,s,choices):
        return cue_planner.revise_prepared_queue(s,s['order'],copy.deepcopy(s['aiReview']['transitions']),'Recheck',2,cue_choices=choices)

    def test_a_verified_window_for_a_future_record_is_used_with_the_planners_reason(self):
        _,s=prepared();order=ids(s);before=chosen({'executionPlan':s['executionPlan']})
        plan=self.revise(s,[pick(order[2],'window-late-2')])
        after=chosen(plan)
        self.assertEqual(after[order[2]],'window-late-2')
        self.assertEqual({k:v for k,v in after.items() if k!=order[2]},{k:v for k,v in before.items() if k!=order[2]})
        row=next(r for r in plan['executionPlan']['selections'] if r['trackId']==order[2])
        self.assertEqual((row['entryBeat'],row['exitBeat']),(128,192))                 # a different place in the record, not only a label
        self.assertEqual(row['reason'],'Comes in on the breakdown, where the bass drops away.')
        self.assertEqual(plan['cueChoices'],{'applied':[order[2]],'ignored':[]})

    def test_without_choices_the_plan_is_exactly_what_it_was(self):
        _,s=prepared()
        with_none=self.revise(s,None);with_empty=self.revise(s,[])
        self.assertEqual(with_none['executionPlan'],with_empty['executionPlan'])
        self.assertEqual(chosen(with_none),chosen({'executionPlan':s['executionPlan']}))
        self.assertNotIn('cueChoices',with_none)

    def test_a_playing_or_cued_record_keeps_its_window(self):
        _,s=prepared();order=ids(s)
        s['analysis']['tracks'][1]['cueCandidates'].append({**copy.deepcopy(s['analysis']['tracks'][1]['cueCandidates'][0]),'id':'window-other-1'})
        plan=self.revise(s,[pick(order[1],'window-other-1')])
        self.assertNotEqual(chosen(plan)[order[1]],'window-other-1')
        self.assertEqual(plan['cueChoices']['applied'],[])
        self.assertEqual(plan['cueChoices']['ignored'],[{'trackId':order[1],'reason':'That record is already playing or cued.'}])

    def test_a_window_the_engine_did_not_verify_is_not_used(self):
        _,s=prepared();order=ids(s)
        for window,reason in (('window-unverified-2','That window has not passed replay verification.'),
                              ('no-such-window','That is not one of this recording’s measured windows.')):
            plan=self.revise(s,[pick(order[2],window)])
            self.assertEqual(chosen(plan)[order[2]],chosen({'executionPlan':s['executionPlan']})[order[2]])
            self.assertEqual(plan['cueChoices'],{'applied':[],'ignored':[{'trackId':order[2],'reason':reason}]})

    def test_a_window_too_short_for_the_planned_blend_is_not_used(self):
        _,s=prepared();order=ids(s)
        plan=self.revise(s,[pick(order[3],'window-short-3')])
        self.assertNotEqual(chosen(plan)[order[3]],'window-short-3')
        self.assertEqual(plan['cueChoices']['ignored'],[{'trackId':order[3],'reason':'That window is too short for the planned blend.'}])

    def test_a_record_outside_the_queue_and_a_repeated_choice_are_ignored(self):
        _,s=prepared();order=ids(s)
        plan=self.revise(s,[pick('virtualdj:'+'f'*64,'x'),pick(order[2],'window-late-2'),pick(order[2],'no-such-window')])
        self.assertEqual(plan['cueChoices']['applied'],[order[2]])
        self.assertEqual([row['reason'] for row in plan['cueChoices']['ignored']],
                         ['That record is not in the queue.','A window was already chosen for that record.'])

    def test_a_choice_the_plan_cannot_be_built_with_falls_back_to_the_engines_window(self):
        _,s=prepared();order=ids(s);base=chosen({'executionPlan':s['executionPlan']})
        broken=copy.deepcopy(s['analysis']['tracks'][2]['cueCandidates'][0]);broken['id']='window-broken-2';del broken['exitBeat']
        s['analysis']['tracks'][2]['cueCandidates'].append(broken)
        for build in (lambda c:self.revise(s,c),
                      lambda c:cue_planner.complete_background_queue(s,s['order'],[],'Future cue fit',2,cue_choices=c)):
            plan=build([pick(order[2],'window-broken-2')])
            self.assertEqual(chosen(plan),base)
            self.assertEqual(plan['cueChoices'],{'applied':[],'ignored':[{'trackId':order[2],'reason':'That window does not fit the planned tempo or blend.'}]})

    def test_malformed_choices_are_ignored_not_raised(self):
        _,s=prepared();base=chosen({'executionPlan':s['executionPlan']})
        for bad in ('x',{'trackId':'a'},['x',7,None],[{'trackId':ids(s)[2],'windowId':['window-late-2']}]):
            self.assertEqual(chosen(self.revise(s,bad)),base)

    def test_a_window_is_named_by_its_exact_id_only(self):
        _,s=prepared();order=ids(s)
        plan=self.revise(s,[pick(order[2],'window-late')])
        self.assertEqual(plan['cueChoices']['applied'],[])

    def test_the_background_fit_honours_the_same_choice(self):
        _,s=prepared();order=ids(s)
        plan=cue_planner.complete_background_queue(s,s['order'],[],'Future cue fit',2,cue_choices=[pick(order[2],'window-late-2'),pick(order[0],'x')])
        self.assertEqual(chosen(plan)[order[2]],'window-late-2')
        self.assertEqual(plan['cueChoices']['applied'],[order[2]])
        self.assertEqual(plan['cueChoices']['ignored'][0]['reason'],'That record is already playing or cued.')
        plain=cue_planner.complete_background_queue(s,s['order'],[],'Future cue fit',2)
        self.assertEqual(chosen(plain),chosen({'executionPlan':s['executionPlan']}));self.assertNotIn('cueChoices',plain)


class SessionTests(unittest.TestCase):
    def test_a_queue_revision_carries_the_choice_into_the_prepared_change(self):
        session,s=prepared();session.update(analysis=s['analysis']);order=ids(s)
        session.revise_queue(order,reason='Recheck',mix_transitions=copy.deepcopy(s['aiReview']['transitions']),
                             cue_choices=[pick(order[2],'window-late-2')])
        queued=session.snapshot()['queuedUpdate']
        self.assertEqual(queued['status'],'ready')
        self.assertEqual(chosen(queued['prepared'])[order[2]],'window-late-2')
        self.assertEqual(queued['cueChoices'],[pick(order[2],'window-late-2')])
        self.assertIn('The DJ chose where 1 record comes in.',session.snapshot()['message'])

    def test_a_revision_without_a_choice_is_unchanged(self):
        session,s=prepared();session.update(analysis=s['analysis']);order=ids(s)
        session.revise_queue(order,reason='Recheck',mix_transitions=copy.deepcopy(s['aiReview']['transitions']))
        queued=session.snapshot()['queuedUpdate']
        self.assertEqual(chosen(queued['prepared']),chosen({'executionPlan':s['executionPlan']}))
        self.assertIsNone(queued['cueChoices'])


class DecisionTests(unittest.TestCase):
    def session(self):
        value={'id':'fixture','phase':'stopped','active':False,'index':-1,
               'candidates':[{'id':c,'title':c.upper(),'artist':'Fixture','bpm':125,'camelot':'8A'} for c in 'abcd'],
               'setlist':{'allowReorder':True,'lockedIds':[]},'telemetry':{'decks':[]}}
        value['order']=copy.deepcopy(value['candidates']);return value

    def playing(self):
        s=self.session();cue={'maxBlendBeats':32,'entry':{'rmsDb':-9.0,'bassDb':-12.0,'highDb':-20.0},'exit':{'rmsDb':-9.0,'bassDb':-12.0,'highDb':-20.0}}
        s.update(active=True,index=0,phase='playing',telemetry={'decks':[{'deck':1,'playing':True,'loaded':True,'id':'a'}]},
                 executionPlan={'applied':True,'selections':[{'trackId':t['id'],**cue} for t in s['order']],'transitions':[]})
        return s

    def test_a_decision_may_name_windows_and_malformed_choices_are_refused(self):
        s=self.session();good={**full_decision(['a','b','c','d']),'cueChoices':[pick('c','w1')]}
        self.assertEqual(validate_decision(good,s)['cueChoices'],[pick('c','w1')])
        self.assertIsNone(validate_decision(full_decision(['a','b']),s)['cueChoices'])
        self.assertIsNone(validate_decision(decision(['a','b']),s)['cueChoices'])      # older saved proposals
        for bad in ('x',[{'trackId':'c'}],[pick('c','w1')]*9,[pick('c','')],[pick(7,'w1')],[{**pick('c','w1'),'extra':1}],
                    [pick('c','w1','')],[pick('c','w'*81)],[pick('c','w1','r'*401)]):
            with self.assertRaisesRegex(ValueError,'mix window'):validate_decision({**good,'cueChoices':bad},s)

    def request(self,session,transcript=None):
        packet={'reason':'recheck upcoming queue after music changed','typedTurn':True,'messages':[],'direction':'House.',
                'music':music_context(session),'searchPolicy':{'canSearch':False}}
        book=agent_loop.Lookups(session,music_context(session,full=True)['catalog'])
        encoded,sized=planning_request(packet,book,transcript or [])
        return json.loads(encoded),sized

    def inspected(self,track,windows=('w1','w2')):
        result={'trackId':track,'analysed':True,'durationSeconds':380,'profile':[],'mixWindowCount':len(windows),'plannedWindowId':windows[0],
                'mixWindows':[{'id':w,'entrySeconds':30.0+i*60,'exitSeconds':60.0+i*60,'maxBlendBeats':32,'entryLevelDb':-9.0,
                               'entryBassDb':-12.0,'exitLevelDb':-9.0,'exitBassDb':-12.0} for i,w in enumerate(windows)],'basis':'x'}
        return {'tool':'inspect_track','arguments':{'trackId':track},'result':result,'summary':'Read the audio of '+track.upper()}

    def test_windows_are_offered_only_for_queued_records_the_planner_inspected(self):
        s=self.playing()
        body,sized=self.request(s)
        self.assertEqual(body['text']['format']['schema']['properties']['cueChoices'],{'type':'null'})
        self.assertNotIn('mixWindows',sized)
        # 'a' is playing and 'b' is cued: only future records can be given a window.
        body,sized=self.request(s,[self.inspected('a'),self.inspected('c'),self.inspected('d',('w9',))])
        self.assertEqual(sorted(sized['mixWindows']),['c','d'])
        self.assertEqual(sized['mixWindows']['c'],{'plannedWindowId':'w1','windows':[
            {'id':'w1','entrySeconds':30.0,'exitSeconds':60.0,'maxBlendBeats':32},{'id':'w2','entrySeconds':90.0,'exitSeconds':120.0,'maxBlendBeats':32}]})
        schema=body['text']['format']['schema']['properties']['cueChoices']
        self.assertEqual(schema['anyOf'][0],{'type':'null'})
        item=schema['anyOf'][1]['items']['properties']
        self.assertEqual(sorted(item['trackId']['enum']),['c','d']);self.assertEqual(sorted(item['windowId']['enum']),['w1','w2','w9'])
        self.assertIn('cueChoices',body['instructions'])

    def test_windows_are_not_offered_where_a_choice_could_not_be_applied(self):
        inspected=[self.inspected('c'),self.inspected('d')]
        stopped=self.session()                                           # not playing: the set is planned from scratch
        self.assertNotIn('mixWindows',self.request(stopped,inspected)[1])
        unplanned=self.playing();unplanned['executionPlan']=None       # playing without an applied mix plan
        self.assertNotIn('mixWindows',self.request(unplanned,inspected)[1])
        packet={'reason':'operator message','typedTurn':True,'messages':[],'direction':'House.','music':music_context(self.playing()),'searchPolicy':{'canSearch':False}}
        packet['music']['preparedCuesOmitted']=True                      # the rolling queue view cannot carry blends
        book=agent_loop.Lookups(self.playing(),music_context(self.playing(),full=True)['catalog'])
        body,sized=planning_request(packet,book,inspected)
        self.assertNotIn('mixWindows',sized);self.assertEqual(json.loads(body)['text']['format']['schema']['properties']['cueChoices'],{'type':'null'})
        self.assertIn('mixWindows',self.request(self.playing(),inspected)[1])

    def test_a_choice_without_a_selected_order_is_refused(self):
        s=self.session()
        with self.assertRaisesRegex(ValueError,'mix window'):
            validate_decision({**full_decision([]),'cueChoices':[pick('c','w1')]},s)

    def test_an_unanalysed_inspection_offers_nothing(self):
        s=self.playing();blank={'tool':'inspect_track','arguments':{'trackId':'c'},'result':{'trackId':'c','analysed':False,'note':'x'},'summary':'x'}
        body,sized=self.request(s,[blank])
        self.assertNotIn('mixWindows',sized);self.assertEqual(body['text']['format']['schema']['properties']['cueChoices'],{'type':'null'})


class EndToEndTests(unittest.TestCase):
    def test_an_inspection_in_the_lookup_stage_lets_the_decision_choose_the_window(self):
        """Explore: inspect a queued record. Decide: the windows are offered and the choice reaches the engine's plan."""
        session,s=prepared();order=ids(s)
        # Records outside the queue, so the request has room for lookups.
        s['candidates']=s['candidates']+[{**copy.deepcopy(s['candidates'][0]),'id':'virtualdj:'+('%x'%n)*64,'title':'Spare %d'%n} for n in range(10,16)]
        packet={'reason':'operator message','typedTurn':True,'messages':[],'direction':'House.',
                'operatorRequest':{'id':'m1','role':'user','text':'bring the third one in later'},
                'music':music_context(s),'searchPolicy':{'canSearch':False}}
        book=agent_loop.Lookups(s,music_context(s,full=True)['catalog'],s['analysis'])
        seen={}
        def decide(body):
            sized=json.loads(body['input']);seen['windows']=sized.get('mixWindows');seen['schema']=body['text']['format']['schema']['properties']['cueChoices']
            value=full_decision(order);value['cueChoices']=[pick(order[2],'window-late-2')]
            reply=decides();reply['output'][0]['content'][0]['text']=json.dumps(value);return reply
        provider=Provider(asks(call('inspect_track',{'why':'Where does this one breathe','trackId':order[2]})),ready(),decide)
        with patch('afterhours.ai_mix.api_key',return_value='fixture-key'):
            raw,receipt=dj_agent.evaluate(packet,provider,book,allowed=5)
        self.assertEqual(receipt['lookups'],1)
        offered=seen['windows'][order[2]]
        self.assertIn('window-late-2',[w['id'] for w in offered['windows']])
        self.assertNotIn('window-unverified-2',[w['id'] for w in offered['windows']])     # only windows the engine verified
        self.assertEqual(seen['schema']['anyOf'][1]['items']['properties']['trackId']['enum'],[order[2]])
        result=validate_decision(raw,s)
        plan=cue_planner.revise_prepared_queue(s,s['order'],copy.deepcopy(s['aiReview']['transitions']),'Recheck',2,cue_choices=result['cueChoices'])
        self.assertEqual(chosen(plan)[order[2]],'window-late-2')


class RealSessionApplyTests(unittest.TestCase):
    """The agent applying a choice-only decision to a real session (review of f5f409e)."""
    def agent(self,session,order,choice):
        value=full_decision(order);value['cueChoices']=choice
        agent=DJAgent(session,evaluator=lambda packet:(copy.deepcopy(value),{'responseId':'fixture'}))
        return agent

    def test_a_choice_on_a_playing_set_keeps_the_planned_blends_and_tempo(self):
        session,s=prepared();session.update(analysis=s['analysis']);order=ids(s)
        before=[(t['fromId'],t['toId'],t['blendBeats'],t['technique'],t['reason']) for t in s['aiReview']['transitions']]
        agent=self.agent(session,order,[pick(order[2],'window-late-2')])
        agent.message('bring the third one in later',message_id='m1',approved=True);settled(agent)
        if not session.snapshot().get('queuedUpdate'):agent.tick(force=True);settled(agent)
        queued=session.snapshot()['queuedUpdate']
        self.assertEqual(queued['status'],'ready')                        # no trip through background analysis
        self.assertEqual(chosen(queued['prepared'])[order[2]],'window-late-2')
        after=[(t['fromId'],t['toId'],t['blendBeats'],t['technique'],t['reason']) for t in queued['prepared']['aiReview']['transitions']]
        self.assertEqual(after,before)
        self.assertEqual(queued['tempoTargets'],s.get('tempoTargets'))

    def test_a_choice_while_a_change_is_staged_keeps_the_staged_blends(self):
        """Confirming review of 1279170: a staged planner blend must survive a later window choice."""
        session,s=prepared();session.update(analysis=s['analysis']);order=ids(s)
        blends=copy.deepcopy(s['aiReview']['transitions']);blends[2].update(blendBeats=16,reason='Short and tight into the last one.')
        session.revise_queue(order,reason='Tighter ending',mix_transitions=blends)
        self.assertEqual(session.snapshot()['queuedUpdate']['status'],'ready')
        agent=self.agent(session,order,[pick(order[2],'window-late-2')])
        agent.message('bring the third one in later',message_id='m1',approved=True);settled(agent)
        queued=session.snapshot()['queuedUpdate']
        self.assertEqual(queued['status'],'ready')
        self.assertEqual(chosen(queued['prepared'])[order[2]],'window-late-2')
        kept=queued['prepared']['aiReview']['transitions'][2]
        self.assertEqual((kept['blendBeats'],kept['reason']),(16,'Short and tight into the last one.'))

    def test_a_choice_that_cannot_apply_stages_nothing(self):
        session,s=prepared();session.update(analysis=s['analysis']);order=ids(s)
        agent=self.agent(session,order,[pick(order[1],'anything'),pick('virtualdj:'+'f'*64,'x')])   # a cued record, and one not queued
        agent.message('bring the next one in later',message_id='m1',approved=True);settled(agent);agent.tick(force=True);settled(agent)
        self.assertFalse(bool(session.snapshot().get('queuedUpdate')))

    def test_a_choice_on_a_stopped_set_leaves_the_prepared_plan_alone(self):
        session,s=prepared();session.update(analysis=s['analysis'],active=False,phase='stopped',index=-1,telemetry={'decks':[]});order=ids(s)
        plan=session.snapshot()['executionPlan']['id']
        agent=self.agent(session,order,[pick(order[2],'window-late-2')])
        agent.message('bring the third one in later',message_id='m1',approved=True);settled(agent);agent.tick(force=True);settled(agent)
        after=session.snapshot()
        self.assertEqual((after['executionPlan'] or {}).get('id'),plan)
        self.assertIsNotNone(after['analysis']);self.assertIsNone(after.get('queuedUpdate'))


class AgentTests(unittest.TestCase):
    def test_a_chosen_window_reaches_the_queue_even_when_the_order_is_kept(self):
        session=Session();calls=[]
        def revise(ids,**kwargs):calls.append((ids,kwargs));session.update(message='Queue accepted.')
        session.revise_queue=revise
        session.update(active=True,phase='playing',index=0,executionPlan={'applied':True},
                       telemetry={'decks':[{'deck':1,'playing':True,'loaded':True,'id':'a'}]})
        choice=[pick('c','w2','Lets the vocal breathe before the drop.')]
        def choose(body):
            value=full_decision(['a','b','c']);value['cueChoices']=choice
            reply=decides();reply['output'][0]['content'][0]['text']=json.dumps(value);return reply
        provider=Provider(choose);agent=DJAgent(session);agent.opener=provider
        with patch('afterhours.ai_mix.api_key',return_value='fixture-key'):
            agent.message('keep the order but bring C in later',message_id='m1',approved=True);state=settled(agent)
            if not calls:agent.tick(force=True);state=settled(agent)
        self.assertEqual(state['proposal']['cueChoices'],choice)
        self.assertEqual(len(calls),1);self.assertEqual(calls[0][0],['a','b','c'])
        self.assertEqual(calls[0][1]['cue_choices'],choice)

    def test_no_choice_and_an_unchanged_order_still_changes_nothing(self):
        session=Session();calls=[];session.revise_queue=lambda ids,**kwargs:calls.append(ids)
        provider=Provider(decides(['a','b','c']));agent=DJAgent(session);agent.opener=provider
        with patch('afterhours.ai_mix.api_key',return_value='fixture-key'):
            agent.message('keep it steady',message_id='m1',approved=True);settled(agent);agent.tick(force=True);state=settled(agent)
        self.assertGreaterEqual(len(provider.requests),1)
        self.assertEqual(calls,[])


if __name__=='__main__':unittest.main()
