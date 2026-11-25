"""Recap preference evidence, explicit adoption, exact receipts and precedence."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from afterhours.dj_agent import DJAgent,recap_played_reply,recap_tracklist_request
from afterhours import dj_settings
from afterhours import learned_preferences
from test_run_archive import Clock,setup,emit
from afterhours.run_archive import RunArchive


class RecapPreferencesTests(unittest.TestCase):
    def make(self,path=None,archive_path=None):
        clock=Clock();store=RunArchive(archive_path,clock=clock.wall,monotonic=clock.monotonic)
        self.addCleanup(store.close);session,native=setup(clock,store)
        provider=Mock(side_effect=AssertionError('No provider calls'))
        agent=DJAgent(session,path,evaluator=provider,clock=clock.wall)
        return clock,store,session,agent,native,provider

    def change(self,agent,kind,value,id='change',expected=None):
        return agent.settings_action(id,{'kind':kind,'value':value},expected or agent.snapshot()['settings']['expected'])

    def recorded(self,clock,store,session,agent,effects=None):
        emit(session,clock)
        for i,effect in enumerate(effects or [{'kind':'style','value':{'id':'energy','stop':4}}]):
            self.change(agent,effect['kind'],effect['value'],'during-'+str(i));clock.advance(1);emit(session,clock)
        emit(session,clock,a=False,phase='stopped',active=False);self.assertTrue(store.flush())
        return agent.recap(store.latest()['id'])

    def lesson_value(self,recap,keep=True,index=0):
        return {'archiveId':recap['id'],'archiveRevision':recap['revision'],'lessonId':recap['lessons'][index]['id'],'keep':keep}

    def test_only_explicit_supported_preferences_during_observed_run_create_unkept_lessons(self):
        c,store,s,a,n,p=self.make();self.change(a,'discover',False,'before')
        emit(s,c);self.change(a,'brief','A house set','brief');self.change(a,'pin',{'trackId':s.snapshot()['order'][0]['id'],'on':True},'pin')
        c.advance(1);emit(s,c)
        self.change(a,'style',{'id':'blend','stop':4},'blend');self.change(a,'discover',True,'discovery')
        c.advance(1);emit(s,c,a=False,phase='completed',active=True)
        self.assertTrue(store.flush());recap=a.recap(store.latest()['id'])
        self.assertEqual(len(recap['lessons']),2);self.assertTrue(all(not r['keep'] for r in recap['lessons']))
        self.assertEqual([r['effect']['kind'] for r in recap['lessons']],['style','discover'])
        self.assertFalse(any(r['group']=='learned' for r in a.snapshot()['settings']['houseRules']))
        p.assert_not_called();n.assert_not_called()

    def test_keep_drop_receipt_and_manual_precedence_use_real_supported_consumers(self):
        c,store,s,a,n,p=self.make();recap=self.recorded(c,store,s,a)
        self.change(a,'style',{'id':'energy','stop':1},'manual-one')
        baseline=copy.deepcopy(s.snapshot()['order']);expected=a.snapshot()['settings']['expected'];value=self.lesson_value(recap)
        keep=self.change(a,'lesson',value,'keep',expected)
        self.assertEqual(keep['settings']['style']['energy'],4);self.assertEqual(s.standing_style['energy'],4)
        self.assertEqual(keep['settings']['receipt']['change'],{'kind':'lesson','value':value})
        self.assertEqual(keep['settings']['receipt']['expected'],expected)
        self.assertEqual(keep['settings']['receipt']['current'],keep['settings']['expected'])
        rule=next(r for r in keep['settings']['houseRules'] if r['group']=='learned')
        self.assertEqual(rule['id'],recap['lessons'][0]['ruleId']);self.assertTrue(rule['on'])
        self.assertTrue(a.recap(recap['id'])['lessons'][0]['keep'])
        drop=self.change(a,'lesson',self.lesson_value(recap,False),'drop')
        self.assertEqual(drop['settings']['style']['energy'],1)
        self.change(a,'lesson',value,'keep-again')
        manual=self.change(a,'style',{'id':'energy','stop':3},'manual-three')
        self.assertFalse(next(r for r in manual['settings']['houseRules'] if r['id']==rule['id'])['on'])
        self.assertFalse(a.recap(recap['id'])['lessons'][0]['keep'])
        self.change(a,'lesson',self.lesson_value(recap,False),'drop-old')
        self.assertEqual(s.standing_style['energy'],3);self.assertEqual(s.snapshot()['order'],baseline)
        p.assert_not_called();n.assert_not_called()

    def test_discovery_and_reorder_adoption_feed_planner_and_keep_protected_audio(self):
        c,store,s,a,n,p=self.make()
        effects=[{'kind':'discover','value':False},{'kind':'rule','value':{'id':'reorder','on':False}}]
        recap=self.recorded(c,store,s,a,effects)
        self.change(a,'discover',True,'discovery-on');self.change(a,'rule',{'id':'reorder','on':True},'reorder-on')
        before=copy.deepcopy(s.snapshot()['order']);s.state['active']=True;s.state['phase']='playing'
        for i in range(2):self.change(a,'lesson',self.lesson_value(recap,index=i),'keep'+str(i))
        context=a.settings.planner_context(s.snapshot(),c.now)
        self.assertFalse(context['discover']);self.assertFalse(context['allowReorder'])
        self.assertEqual(s.snapshot()['order'],before);self.assertTrue(s.snapshot()['active']);n.assert_not_called();p.assert_not_called()

    def test_lesson_commands_are_stale_checked_idempotent_and_cannot_change_archived_effect(self):
        c,store,s,a,n,p=self.make();recap=self.recorded(c,store,s,a);value=self.lesson_value(recap)
        expected=a.snapshot()['settings']['expected'];first=self.change(a,'lesson',value,'keep',expected)
        self.change(a,'style',{'id':'energy','stop':1},'manual')
        replay=self.change(a,'lesson',value,'keep',expected)
        self.assertEqual(replay['settings']['receipt'],first['settings']['receipt']);self.assertEqual(replay['settings']['style']['energy'],1)
        before=copy.deepcopy(a.settings.state)
        for bad in ({**value,'archiveRevision':2},{**value,'lessonId':'e'*24},{**value,'archiveId':'e'*32},
                    {**value,'keep':1},{**value,'effect':{'kind':'discover','value':False}}):
            with self.subTest(bad=bad),self.assertRaises(ValueError):self.change(a,'lesson',bad,'bad')
            self.assertEqual(a.settings.state,before)
        with self.assertRaisesRegex(ValueError,'Settings changed'):self.change(a,'lesson',value,'stale',expected)
        with self.assertRaisesRegex(ValueError,'different settings'):self.change(a,'lesson',{**value,'keep':False},'keep',expected)
        p.assert_not_called();n.assert_not_called()

    def test_adoption_restart_restores_effect_without_mutating_archive_or_replaying_commands(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);c,store,s,a,n,p=self.make(root/'agent.json',root/'recaps')
            recap=self.recorded(c,store,s,a);original=(root/'recaps'/(recap['id']+'.json')).read_bytes()
            self.change(a,'style',{'id':'energy','stop':0},'manual-zero');self.change(a,'lesson',self.lesson_value(recap),'keep')
            s.state['active']=False;s.state['telemetry']=None
            restored=DJAgent(s,root/'agent.json',evaluator=p,clock=c.wall);restored.restore_settings()
            self.assertEqual(restored.settings.state['style']['energy'],4);self.assertTrue(restored.recap(recap['id'])['lessons'][0]['keep'])
            self.change(restored,'lesson',self.lesson_value(recap,False),'drop')
            self.assertEqual(restored.settings.state['style']['energy'],0)
            self.assertEqual((root/'recaps'/(recap['id']+'.json')).read_bytes(),original);p.assert_not_called();n.assert_not_called()

    def test_recap_questions_and_ambiguous_keywords_cannot_trigger_music_media_or_preference_mutation(self):
        for source in ('typed','voice'):
            c,store,s,a,n,p=self.make();recap=self.recorded(c,store,s,a)
            context={'archiveId':recap['id'],'archiveRevision':recap['revision']};settings=copy.deepcopy(a.settings.state)
            before=copy.deepcopy(s.snapshot()['order'])
            for i,text in enumerate(['Start a set','What did I learn?','Should I keep lesson '+recap['lessons'][0]['title']+'?',
                    'Can you export the recording?','play the best stretch','what did the crowd like?','generate clips of @albert']):
                result=a.message(text,source=source,message_id=str(i),approved=True,surface='recap',recap_context=context)
                self.assertEqual([r['role'] for r in result['messages'][-2:]],['user','assistant'])
                self.assertEqual(result['messages'][-1]['id'],str(i)+'-reply')
                self.assertEqual(a.settings.state,settings)
            self.assertEqual(s.snapshot()['order'],before);self.assertEqual(a.remaining,0);n.assert_not_called();p.assert_not_called()

    def test_recap_typed_and_voice_explicit_keep_are_exact_and_bound_to_selected_run(self):
        c,store,s,a,n,p=self.make();recap=self.recorded(c,store,s,a)
        context={'archiveId':recap['id'],'archiveRevision':recap['revision']}
        result=a.message('keep lesson '+recap['lessons'][0]['title'],message_id='spoken',source='voice',surface='recap',recap_context=context)
        self.assertTrue(result['settings']['receipt']['change']['value']['keep'])
        self.assertEqual(result['messages'][-1]['id'],'spoken-reply')
        again=a.message('keep lesson '+recap['lessons'][0]['title'],message_id='spoken',source='voice',surface='recap',recap_context=context)
        self.assertEqual(again['settings']['revision'],result['settings']['revision'])
        for invalid in (None,{}, {'archiveId':recap['id'],'archiveRevision':2}, {**context,'extra':True}):
            with self.assertRaises(ValueError):a.message('keep lesson '+recap['lessons'][0]['title'],message_id='bad',surface='recap',recap_context=invalid)
        with self.assertRaisesRegex(ValueError,'different conversation'):a.message('start music',message_id='spoken',surface='recap',recap_context=context)
        p.assert_not_called();n.assert_not_called()

    def test_natural_tracklist_questions_name_only_actual_played_records_in_observed_order(self):
        c,store,s,a,n,p=self.make()
        emit(s,c);c.advance(1);emit(s,c,a=False,b=True,cross=1)
        c.advance(1);emit(s,c,a=False,phase='stopped',active=False)
        self.assertTrue(store.flush());recap=a.recap(store.latest()['id'])
        context={'archiveId':recap['id'],'archiveRevision':recap['revision']}
        before=copy.deepcopy(s.snapshot());settings=copy.deepcopy(a.settings.state)
        for i,text in enumerate(('What records actually played?','Which songs did we play?',
                                 'Show tracklist.','Could you show me the track list?')):
            for source in ('typed','voice'):
                reply=a.message(text,source=source,message_id=f'tracklist-{i}-{source}',surface='recap',recap_context=context)['messages'][-1]['text']
                self.assertTrue(reply.startswith('Track a — Fixture artist; Track b — Fixture artist.'))
                self.assertNotIn('Track c',reply);self.assertNotIn('exact recordings',reply);self.assertNotIn('interval',reply)
        self.assertEqual(s.snapshot(),before);self.assertEqual(a.settings.state,settings)
        self.assertIsNone(a.playback_request);self.assertEqual(a.remaining,0);n.assert_not_called();p.assert_not_called()

    def test_tracklist_interruption_keeps_names_and_does_not_claim_confirmed_end(self):
        c,store,s,a,n,p=self.make();emit(s,c);c.advance(1);emit(s,c,phase='error',active=False)
        self.assertTrue(store.flush());recap=a.recap(store.latest()['id'])
        reply=a.message('What did we play?',message_id='interrupted-list',surface='recap',
            recap_context={'archiveId':recap['id'],'archiveRevision':recap['revision']})['messages'][-1]['text']
        self.assertTrue(reply.startswith('Track a — Fixture artist.'))
        self.assertIn('before the interruption',reply);self.assertIn('ending wasn’t confirmed',reply)
        n.assert_not_called();p.assert_not_called()

    def test_empty_and_repeated_playback_stretches_do_not_invent_music_or_restarts(self):
        self.assertEqual(recap_played_reply({'played':[],'order':[{'title':'Never played'}]}),
                         'No played records were captured for this run.')
        def row(tid,start,end):return {'id':tid,'title':'Song '+tid,'artist':'Artist '+tid,'start':start,'end':end}
        reply=recap_played_reply({'status':'stopped','played':[
            row('A',0,5),row('A',4,8),row('B',8,10),row('A',12,14),row('A',15,16)]})
        self.assertEqual(reply.count('Song A — Artist A'),3)
        self.assertTrue(reply.startswith('Song A — Artist A; Song B — Artist B; Song A — Artist A; Song A — Artist A.'))
        self.assertIn('separate captured stretches',reply);self.assertIn('not a confirmed restart',reply)

    def test_large_tracklist_is_bounded_and_read_only_recognizer_does_not_match_mutations(self):
        rows=[{'id':str(i),'title':f'Long title {i} '+('é'*280),'artist':'Artist '+('x'*280),
               'start':i*10,'end':i*10+9} for i in range(128)]
        reply=recap_played_reply({'status':'interrupted','played':rows})
        self.assertLessEqual(len(reply),1600);self.assertTrue(reply.startswith('Long title 0'))
        self.assertIn('more entries in the full tracklist',reply);self.assertNotIn('Long title 127',reply)
        self.assertIn('ending wasn’t confirmed',reply)
        for text in ('start playing the tracklist','show tracklist and play it','keep lesson tracklist?',
                     'what did the crowd like','export the recording'):
            self.assertFalse(recap_tracklist_request(text))

    def test_restore_rejects_malformed_learned_effects_and_limit_can_reclaim_dropped_row(self):
        state=dj_settings.Settings().state
        effect={'kind':'discover','value':False}
        lesson={'id':'a'*24,'title':'Discover off','detail':'Explicit preference','effect':effect}
        for i in range(32):learned_preferences.adopt(state,{'id':format(i,'032x')},lesson,True,1000)
        with self.assertRaises(ValueError):learned_preferences.adopt(state,{'id':'f'*32},lesson,True,1001)
        learned_preferences.adopt(state,{'id':format(0,'032x')},lesson,False,1002)
        learned_preferences.adopt(state,{'id':'f'*32},lesson,True,1003)
        self.assertEqual(len(state['learned']),32)
        saved=copy.deepcopy(state);saved['learned'][0]['id']='forged';saved['learned'][1]['changedAt']=float('nan')
        saved['learned'][2]['effect']={'kind':'load','value':'private locator'}
        restored=dj_settings.Settings(saved).state;self.assertEqual(len(restored['learned']),29)
        self.assertTrue(all(learned_preferences.valid_effect(row['effect']) for row in restored['learned']))


if __name__=='__main__':unittest.main()
