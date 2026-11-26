"""Run permission/scope and rolling queues with fake providers, no live audio."""
import copy
import threading
import unittest
from unittest.mock import Mock, patch

from afterhours.dj_agent import DJAgent, MAX_REQUEST_BYTES, planning_request, validate_decision
from afterhours.session_curation import planning_view, expand_decision
from test_dj_agent import Session, decision, settled
from afterhours.vdj_session import validate_order, DJSession


class RunSession(Session):
    def __init__(self):
        super().__init__()
        self.state['candidates']=[{'id':f't{i:02}', 'title':f'House recording {i}', 'artist':'Fixture',
            'bpm':125,'camelot':'8A','genre':'House'} for i in range(80)]
        self.state['order']=copy.deepcopy(self.state['candidates'][:3])
        self.state.update(secondsPerTrack=30,maxTempoShift=8,startedAt=None,copilot={'mode':'leading'})
        self.prepared()
    def prepared(self):
        self.state['executionPlan']={'applied':True,'id':'prepared','orderIds':[t['id'] for t in self.state['order']],'targetBpm':125,
            'selections':[{'trackId':t['id']} for t in self.state['order']],
            'tempoSchedule':[{'trackId':t['id'],'fromBpm':125,'toBpm':125,'durationBeats':0,'startOffsetBeats':0,'maxPitchPercent':8} for t in self.state['order']]}
    def revise_queue(self, ids, **kwargs):
        super().revise_queue(ids,**kwargs);self.prepared()
    def start(self, initialize_mixer=False, immediate=False, rolling_queue=False):
        assert initialize_mixer and immediate
        self.calls.append(('start',))
        self.update(active=True,phase='playing',index=0,startedAt=1234,
                    telemetry={'decks':[{'playing':True,'id':self.state['order'][0]['id']}]})
    def stop(self,pause=True):
        self.calls.append(('stop',));self.update(active=False,phase='stopped',telemetry={'decks':[]})


def scoped(mode='ongoing', ids=None, start_id=None):
    return {**decision(ids), 'sessionScope':{'mode':mode,'message':
        'A continuing house set.' if mode=='ongoing' else 'A few selected records.' if mode=='finite' else 'A continuing set or just a few records?'},
        'playbackAction':{'requestId':start_id,'action':'start-now'} if start_id else None}


class SessionCurationTests(unittest.TestCase):
    def test_two_hour_night_keeps_same_bounded_curation_budget(self):
        s=RunSession();clock=[1000.]
        e=Mock(return_value=(scoped('ongoing',[t['id'] for t in s.state['order']],start_id='two-hours'),{}))
        a=DJAgent(s,evaluator=e,clock=lambda:clock[0])
        a.settings.state['plan']={'startAt':1000.,'endAt':8200.,'peakAt':6400.,'warm':True,'onePeak':True,'darkLate':False}
        a.message('Start a set based on house',message_id='two-hours',approved=True,
                  approve_preparation=True,approve_session_curation=True);settled(a)
        self.assertEqual(e.call_args.args[0]['sessionIntent']['curationLimit'], {'seconds':7200,'decisions':60})
        self.playing(a)
        grant=a.snapshot()['curation']
        self.assertEqual(grant['expiresAt'],8200.)
        self.assertEqual(grant['remaining'],60)
        self.assertEqual(grant['costCeilingUSD'],2.90)
        clock[0]=8000.
        a.curation.reconcile(s.snapshot(),a.playback_request,'all')
        self.assertTrue(a.curation.available())
        self.assertIsNotNone(a.curation.reserve())
        self.assertEqual(a.curation.grant['remaining'],59)
        clock[0]=8200.
        a.curation.reconcile(s.snapshot(),a.playback_request,'all')
        self.assertFalse(a.curation.available())
        self.assertEqual(a.curation.grant['status'],'expired')
        self.assertTrue(s.state['active'], 'budget expiry does not interrupt prepared audio')

    def test_night_extension_is_bounded_and_future_plan_cannot_extend_today(self):
        from afterhours.dj_agent import night_end
        from afterhours.session_curation import RunCuration
        c=RunCuration(lambda:1000.)
        self.assertEqual(c.expiry(1000.+7200),8200.)
        self.assertEqual(c.expiry(1000.+86400),22600.)
        for value in (None,True,float('nan'),float('inf')):
            self.assertEqual(c.expiry(value),2800.)
        self.assertIsNone(night_end({'startAt':90000.,'endAt':97200.},1000.))
        self.assertIsNone(night_end({'startAt':1.,'endAt':900.},1000.))

    def make(self, mode='ongoing', source='typed', **approval):
        s=RunSession();clock=[1000.]
        e=Mock(return_value=(scoped(mode,[t['id'] for t in s.state['order']],start_id='start'),{}))
        a=DJAgent(s,evaluator=e,clock=lambda:clock[0])
        if source=='voice':a.enable(True,1800,60)
        flags={'approved':True,'approve_preparation':True,'approve_session_curation':True,**approval}
        a.message('Start a set based on house',source=source,message_id='start',**flags);settled(a)
        return s,a,e,clock
    def playing(self,a):
        for _ in range(2):a.tick(force=True);settled(a)
        self.assertEqual(a.playback_request['status'],'playing')
    def test_three_track_typed_start_keeps_curating_after_turn_is_spent(self):
        s,a,e,clock=self.make();self.assertEqual(a.remaining,0)
        self.assertEqual(a.snapshot()['curation']['status'],'preparing');self.assertNotIn(('start',),s.calls)
        self.playing(a)
        e.return_value=(decision(['t00','t01','t02','t03']),{})
        a.tick(force=True);settled(a)
        self.assertEqual([r['id'] for r in s.state['order']],['t00','t01','t02','t03'])
        grant=a.snapshot()['curation'];self.assertEqual(grant['remaining'],59)
        self.assertEqual(grant['preparedUpcoming'],3);self.assertEqual(grant['costCeilingUSD'],2.90)
        self.assertEqual(a.remaining,0);self.assertFalse(any(c[0]=='review' for c in s.calls))
    def test_finite_model_scope_does_not_admit_or_refill_for_typed_or_voice(self):
        for source in ('typed','voice'):
            with self.subTest(source=source):
                s,a,e,c=self.make('finite',source);self.playing(a)
                calls=e.call_count
                for i in range(3):a.tick(force=True);settled(a)
                self.assertIsNone(a.snapshot()['curation']);self.assertEqual(e.call_count,calls)
    def test_old_clients_missing_approval_questions_and_voice_never_get_new_grant(self):
        for flags in ({'approve_session_curation':False},{'approve_session_curation':'true'},
                      {'approve_preparation':False},{'approved':False}):
            s,a,e,c=self.make(**flags);self.assertIsNone(a.snapshot()['curation'])
        s,a,e,c=self.make(source='voice');self.assertIsNone(a.snapshot()['curation'])
        s=RunSession();e=Mock(return_value=(scoped('ongoing'),{}));a=DJAgent(s,evaluator=e)
        a.message('What tempo is the current song?',message_id='question',approved=True,
                  approve_preparation=True,approve_session_curation=True);settled(a)
        self.assertIsNone(a.snapshot()['curation']);self.assertNotIn(('start',),s.calls)
    def test_repeat_start_and_steer_do_not_renew_grant(self):
        s,a,e,c=self.make();self.playing(a);before=copy.deepcopy(a.snapshot()['curation'])
        a.message('Start a set based on house',message_id='start',approved=True,approve_preparation=True,approve_session_curation=True)
        e.return_value=(decision(),{})
        a.message('What is the current tempo?',message_id='question',approved=True,approve_preparation=True,approve_session_curation=True);settled(a)
        after=a.snapshot()['curation']
        self.assertEqual((after['remaining'],after['expiresAt']),(before['remaining'],before['expiresAt']))
        self.assertEqual(s.calls.count(('start',)),1)
    def test_ready_go_uses_only_accepted_exact_prepared_scope(self):
        for known in (False,True):
            s=RunSession();s.update(phase='ready');a=DJAgent(s,evaluator=Mock())
            if known:a.session_scope={'mode':'ongoing','message':'Continue the set.','sessionId':s.state['id'],
                                      'revision':a.state['revision'],'orderIds':[t['id'] for t in s.state['order']]}
            a.message('Go',message_id='go',approved=True,approve_preparation=True,approve_session_curation=True)
            self.assertEqual(bool(a.snapshot()['curation']),known);a.evaluator.assert_not_called()
            self.assertEqual(s.calls.count(('start',)),1)
    def test_clarification_retains_start_then_resolves_without_second_play_typed_and_voice(self):
        for source in ('typed','voice'):
            s=RunSession();e=Mock(return_value=(scoped('clarify',start_id='origin'),{}));a=DJAgent(s,evaluator=e)
            if source=='voice':a.enable(True,1800,60)
            a.message('Start a set based on house',source=source,message_id='origin',approved=True,
                      approve_preparation=True,approve_session_curation=True);settled(a)
            question=a.snapshot()['clarification'];self.assertEqual(question['requestId'],'origin')
            self.assertEqual(a.playback_request['status'],'clarifying');self.assertEqual(s.calls,[])
            e.return_value=(scoped('ongoing',['t00','t01','t02']),{})
            a.message('A continuing set, please',source=source,message_id='answer',approved=True,
                      approve_preparation=True,approve_session_curation=True);settled(a);self.playing(a)
            self.assertIsNone(a.snapshot()['clarification']);self.assertEqual(s.calls.count(('start',)),1)
            self.assertEqual(bool(a.snapshot()['curation']),source=='typed')
            self.assertTrue(e.call_args.args[0]['sessionIntent']['awaitingClarification'])
    def test_unrelated_answer_and_expired_clarification_never_autostart(self):
        s=RunSession();clock=[1000.];e=Mock(return_value=(scoped('clarify',start_id='origin'),{}));a=DJAgent(s,evaluator=e,clock=lambda:clock[0])
        a.message('Start a set based on house',message_id='origin',approved=True,approve_preparation=True,approve_session_curation=True);settled(a)
        deadline=a.snapshot()['clarification']['expiresAt'];e.return_value=(decision(),{})
        a.message('What is your name?',message_id='unrelated',approved=True);settled(a)
        self.assertEqual(a.snapshot()['clarification']['expiresAt'],deadline);self.assertEqual(s.calls,[])
        clock[0]=deadline+1;e.return_value=(scoped('ongoing',['t00','t01','t02']),{})
        a.message('A full set please',message_id='late',approved=True,approve_preparation=True,approve_session_curation=True);settled(a)
        for _ in range(3):a.tick(force=True);settled(a)
        self.assertNotIn(('start',),s.calls);self.assertIsNone(a.snapshot()['curation'])
    def test_stop_takeover_expiry_and_restart_revoke_without_stopping_accepted_audio(self):
        for cause,status in (('takeover','revoked'),('error','blocked'),('stopped','revoked'),('expired','expired'),('new-run','revoked')):
            s,a,e,clock=self.make();self.playing(a)
            if cause=='expired':clock[0]+=1800
            elif cause=='new-run':s.update(startedAt=2222)
            else:s.update(phase=cause,active=cause=='error')
            before=len(s.calls);a.tick(force=True);settled(a)
            self.assertEqual(a.snapshot()['curation']['status'],status,cause);self.assertEqual(len(s.calls),before)
        s,a,e,c=self.make();self.playing(a);b=DJAgent(s,evaluator=Mock())
        self.assertIsNone(b.snapshot()['curation'])
    def test_stop_command_and_copilot_off_revoke(self):
        s,a,e,c=self.make();self.playing(a)
        a.message('Stop the music',message_id='stop')
        self.assertEqual(a.snapshot()['curation']['status'],'revoked')
        s,a,e,c=self.make();self.playing(a);s.update(copilot={'mode':'off'})
        a.tick(force=True);self.assertEqual(a.snapshot()['curation']['status'],'revoked')
    def test_question_provider_failure_does_not_revoke_the_existing_run(self):
        s,a,e,c=self.make();self.playing(a);remaining=a.snapshot()['curation']['remaining']
        e.side_effect=ValueError('Question failed.')
        a.message('What is your name?',message_id='q',approved=True);settled(a)
        self.assertEqual(a.snapshot()['curation']['status'],'active')
        self.assertEqual(a.snapshot()['curation']['remaining'],remaining)
    def test_background_error_is_terminal_and_never_retries(self):
        s,a,e,c=self.make();self.playing(a);e.side_effect=ValueError('Provider outcome uncertain.')
        a.tick(force=True);settled(a)
        self.assertEqual(a.snapshot()['curation']['status'],'blocked');calls=e.call_count
        for _ in range(4):a.tick(force=True);settled(a)
        self.assertEqual(e.call_count,calls);self.assertTrue(s.state['active'])
    def test_late_evaluation_cannot_apply_after_revocation(self):
        s,a,e,c=self.make();self.playing(a);entered=threading.Event();release=threading.Event()
        def wait(packet):
            entered.set();release.wait(2);return decision(['t00','t01','t02','t03']),{}
        e.side_effect=wait;a.tick(force=True);self.assertTrue(entered.wait(1))
        s.update(phase='takeover',active=False);a.snapshot();release.set();settled(a)
        self.assertEqual(len(s.state['order']),3);self.assertEqual(a.snapshot()['curation']['status'],'revoked')
    def test_sixty_decisions_no_sixty_first_or_deadline_renewal(self):
        s,a,e,c=self.make();self.playing(a);grant=a.curation;expiry=grant.grant['expiresAt']
        for _ in range(60):self.assertIsNotNone(grant.reserve())
        self.assertIsNone(grant.reserve());self.assertEqual(grant.grant['status'],'exhausted')
        self.assertEqual(grant.grant['expiresAt'],expiry)
        calls=e.call_count;a.tick(force=True);settled(a);self.assertEqual(e.call_count,calls)
    def test_twenty_five_record_run_keeps_immutable_history_and_bounded_requests(self):
        s,a,e,c=self.make();self.playing(a);packets=[]
        def refill(packet):
            encoded,trimmed=planning_request(packet);self.assertLessEqual(len(encoded),MAX_REQUEST_BYTES)
            packets.append(packet)
            chosen=list(packet['music']['currentOrder'])
            next_id=next(t['id'] for t in packet['music']['catalog'] if t['id'] not in chosen)
            return decision(chosen+[next_id]),{'catalogIds':[t['id'] for t in trimmed['music']['catalog']]}
        e.side_effect=refill
        for index in range(24):
            s.update(index=index);prefix=[t['id'] for t in s.state['order'][:index+2]]
            c[0]+=30;a.tick(force=True);settled(a)
            self.assertEqual([t['id'] for t in s.state['order'][:index+2]],prefix)
            self.assertGreaterEqual(len(s.state['order'])-index,4)
            self.assertEqual(a.snapshot()['curation']['status'],'active')
        self.assertGreater(len(s.state['order']),25)
        self.assertLessEqual(max(len(p['music']['currentOrder']) for p in packets),12)
        self.assertTrue(any(p['music'].get('queueWindow') for p in packets))
        self.assertEqual(s.calls.count(('start',)),1)
    def test_discovery_followup_charges_run_budget_only_and_preserves_scope(self):
        s,a,e,c=self.make();self.playing(a)
        q={'query':'House next','artist':'','genre':'House','bpmMin':120,'bpmMax':130}
        e.return_value=(decision(search=q),{})
        a.tick(force=True);settled(a)
        self.assertEqual(a.snapshot()['curation']['remaining'],59)
        s.update(phase='playing',catalogSearch={'status':'complete','matched':4})
        e.return_value=(decision(['t00','t01','t02','t03']),{})
        a.tick(force=True);settled(a)
        self.assertEqual(a.snapshot()['curation']['remaining'],58)
        self.assertEqual(a.remaining,0);self.assertEqual(len(s.state['order']),4)
        self.assertEqual(a.snapshot()['curation']['status'],'active')
    def test_rolling_refill_excludes_variants_of_omitted_heard_history(self):
        s,a,e,c=self.make();self.playing(a)
        s.update(order=copy.deepcopy(s.state['candidates'][:25]),index=23)
        s.prepared();prefix=[t['id'] for t in s.state['order'][:23]]
        for index in (0,22):
            old=s.state['candidates'][index]
            s.state['candidates'].append({**old,'id':f'remaster-{index}','title':' '+old['title'].upper()+' ','remix':'Remaster'})
        def refill(packet):
            self.assertEqual(packet['music']['queueWindow']['playedCount'],23)
            ids=[t['id'] for t in packet['music']['catalog']]
            self.assertNotIn('remaster-0',ids);self.assertNotIn('remaster-22',ids)
            self.assertTrue({'t23','t24','t25'}<=set(ids))
            wire,sent=planning_request(packet);self.assertLessEqual(len(wire),MAX_REQUEST_BYTES)
            return decision(['t23','t24','t25']),{'catalogIds':[t['id'] for t in sent['music']['catalog']]}
        e.side_effect=refill;a.tick(force=True);settled(a)
        self.assertEqual([t['id'] for t in s.state['order']],prefix+['t23','t24','t25'])
        self.assertEqual(a.snapshot()['curation']['status'],'active')
    def test_opening_search_evidence_survives_scope_answer_and_first_refill(self):
        s=RunSession();c=[1000.];e=Mock(return_value=(scoped('clarify',start_id='start'),{}))
        a=DJAgent(s,evaluator=e,clock=lambda:c[0])
        a.message('Start a set based on Daft Punk',message_id='start',approved=True,
                  approve_preparation=True,approve_session_curation=True);settled(a)
        for track in s.state['candidates'][75:]:track.update(artist='Daft Punk')
        q={'query':'Daft Punk','artist':'','genre':'','bpmMin':40,'bpmMax':300}
        search={**scoped('ongoing'), 'direction':'A continuing Daft Punk set.', 'search':q}
        e.return_value=(search,{})
        a.message('A full ongoing set please',message_id='scope-answer',approved=True,
                  approve_preparation=True,approve_session_curation=True);settled(a)
        s.update(phase='catalog-ready',catalogSearch={'status':'complete','query':'Daft Punk',
            'filters':{k:q[k] for k in ('artist','genre','bpmMin','bpmMax')},
            'examined':5,'matched':5,'added':5,'foundIds':[f't{i}' for i in range(75,80)]},
            recordingAvailability=[{'trackId':'t79','reason':'Unavailable recording','code':'recording-unavailable'}])
        e.return_value=({**decision(['t75','t76','t77']), 'direction':'A continuing Daft Punk set.'},{})
        a.tick(force=True);settled(a)
        self.assertNotEqual(a.playback_request['status'],'failed')
        self.playing(a)
        searches=len([call for call in s.calls if call[0]=='discover'])
        def from_crate(packet):
            self.assertEqual(packet['operatorRequest']['text'],'A full ongoing set please')
            self.assertEqual(packet['searchPolicy']['previousQueries'],['daft punk'])
            self.assertEqual(packet['searchPolicy']['lastResult']['matched'],5)
            self.assertEqual(packet['searchPolicy']['lastResult']['query'],'Daft Punk')
            catalog={t['id']:t for t in packet['music']['catalog']}
            self.assertTrue(catalog['t78']['searchMatch']);self.assertNotIn('t79',catalog)
            encoded,sent=planning_request(packet);self.assertLessEqual(len(encoded),MAX_REQUEST_BYTES)
            self.assertIn('t78',[t['id'] for t in sent['music']['catalog']])
            return decision(['t75','t76','t77','t78']),{}
        e.side_effect=from_crate;a.tick(force=True);settled(a)
        self.assertEqual([t['id'] for t in s.state['order']],['t75','t76','t77','t78'])
        self.assertEqual(len([call for call in s.calls if call[0]=='discover']),searches)
        self.assertEqual(a.snapshot()['curation']['status'],'active')
        self.assertEqual(a.snapshot()['curation']['remaining'],59)
    def test_duplicate_refill_search_remains_rejected_once_without_retry(self):
        s,a,e,c=self.make();self.playing(a)
        a.searches.add('house next')
        q={'query':'  HOUSE   next  ','artist':'','genre':'','bpmMin':40,'bpmMax':300}
        e.return_value=(decision(search=q),{})
        before=copy.deepcopy(s.state['order']);a.tick(force=True);settled(a)
        calls=e.call_count
        self.assertEqual(a.snapshot()['curation']['status'],'blocked')
        self.assertIn('already been tried',a.snapshot()['curation']['message'])
        self.assertEqual(s.state['order'],before);self.assertTrue(s.state['active'])
        self.assertFalse(any(call[0]=='discover' for call in s.calls))
        for _ in range(3):c[0]+=30;a.tick(force=True);settled(a)
        self.assertEqual(e.call_count,calls);self.assertEqual(a.snapshot()['curation']['remaining'],59)
    def test_terminal_run_budget_never_borrows_talk_allowance_for_background_work(self):
        for status in ('exhausted','expired','blocked','revoked'):
            with self.subTest(status=status):
                s,a,e,c=self.make();self.playing(a)
                a.enable(True,seconds=3600,requests=6)
                if status=='exhausted':
                    for _ in range(60):a.curation.reserve()
                elif status=='expired':c[0]+=1801
                else:a.curation.end(status,'Run curation ended.')
                s.update(index=1);calls=e.call_count
                a.tick(force=True);settled(a)
                self.assertEqual(e.call_count,calls);self.assertEqual(a.remaining,6)
                self.assertEqual(a.snapshot()['curation']['status'],status)
                # A new explicit voice turn still uses its own Talk allowance.
                e.return_value=(decision(),{})
                a.message('What is your name?',source='voice',message_id='voice-question');settled(a)
                self.assertEqual(e.call_count,calls+1);self.assertEqual(a.remaining,5)
                a.tick(force=True);settled(a)
                self.assertEqual(e.call_count,calls+1);self.assertEqual(a.remaining,5)
    def test_run_catalog_failure_preserves_concurrent_talk_allowance(self):
        s,a,e,c=self.make();self.playing(a);a.enable(True,seconds=1800,requests=6)
        q={'query':'House next','artist':'','genre':'House','bpmMin':120,'bpmMax':130}
        e.return_value=(decision(search=q),{})
        a.tick(force=True);settled(a);self.assertEqual(a.remaining,6)
        s.update(phase='playing',catalogSearch={'status':'error','message':'Catalog disconnected.'})
        a.tick(force=True);settled(a)
        self.assertEqual(a.remaining,6);self.assertEqual(a.snapshot()['curation']['status'],'blocked')
        calls=e.call_count;a.tick(force=True);settled(a);self.assertEqual(e.call_count,calls)
    def test_explicit_turn_search_followup_keeps_its_budget_after_run_is_blocked(self):
        s,a,e,c=self.make();self.playing(a);a.curation.end('blocked','Run curation ended.')
        a.enable(True,seconds=1800,requests=6)
        q={'query':'Specific record','artist':'','genre':'House','bpmMin':120,'bpmMax':130}
        e.return_value=(decision(search=q),{})
        a.message('Find this specific record',source='voice',message_id='voice-search');settled(a)
        s.update(phase='playing',catalogSearch={'status':'complete','matched':4})
        e.return_value=(decision(['t00','t01','t02','t03']),{})
        a.tick(force=True);settled(a)
        self.assertEqual(a.remaining,4);self.assertEqual(len(s.state['order']),4)
        self.assertEqual(a.snapshot()['curation']['status'],'blocked')
    def test_closing_talk_preserves_the_run_grant_without_renewing_it(self):
        s,a,e,c=self.make();self.playing(a);before=copy.deepcopy(a.snapshot()['curation'])
        a.disable(preserve_playback=True)
        after=a.snapshot()['curation']
        self.assertEqual((after['remaining'],after['expiresAt']),(before['remaining'],before['expiresAt']))
        self.assertEqual(after['status'],'active');self.assertTrue(s.state['active'])
    def test_grant_does_not_leak_into_saved_conversation(self):
        s,a,e,c=self.make();self.playing(a)
        self.assertNotIn('curation',a.state);self.assertNotIn('clarification',a.state)
        self.assertNotIn('sessionScope',a.state)
    def test_scope_question_rejects_simultaneous_selection(self):
        with self.assertRaisesRegex(ValueError,'scope question'):
            validate_decision(scoped('clarify',['t00','t01']),RunSession().snapshot())
    def test_long_names_and_large_history_stay_inside_full_request_byte_bound(self):
        s,a,e,c=self.make();self.playing(a)
        for row in s.state['candidates']:
            row.update(title=row['title']+' Extended original mix'*12,artist='A long artist name '*8)
        s.update(order=copy.deepcopy(s.state['candidates'][:40]),index=30);s.prepared()
        e.side_effect=lambda packet:(decision(packet['music']['currentOrder']),{'requestBytes':len(planning_request(packet)[0])})
        c[0]+=30;a.tick(force=True);settled(a)
        self.assertNotEqual(a.state['status'],'error',a.state['message'])
        packet=e.call_args.args[0];encoded,trimmed=planning_request(packet)
        self.assertLessEqual(len(encoded),MAX_REQUEST_BYTES)
        self.assertEqual(trimmed['music']['currentOrder'],[f't{i:02}' for i in range(30,40)])
        self.assertEqual(trimmed['music']['protectedIds'],['t30','t31'])
        self.assertNotIn('t00',[r['id'] for r in trimmed['music']['catalog']])


class RollingQueueTests(unittest.TestCase):
    def test_short_queue_keeps_absolute_contract_and_model_window_stays_bounded(self):
        s=RunSession();s.update(active=True,phase='playing',index=1)
        view,prefix=planning_view(s.snapshot())
        self.assertEqual(prefix,[]);self.assertEqual(view['index'],1)
        self.assertEqual(view['order'],s.state['order'])
        provider=Mock(return_value=(decision([t['id'] for t in s.state['candidates'][:13]]),{}))
        agent=DJAgent(s,evaluator=provider)
        agent.message('Extend this queue',message_id='too-many',approved=True);settled(agent)
        self.assertEqual(agent.state['status'],'error');self.assertIn('at most 12',agent.state['message'])
        self.assertEqual(len(s.state['order']),3);self.assertEqual(s.calls,[])
    def test_final_record_resume_validates_exact_retained_plan_before_dispatch(self):
        from test_copilot_commands import running
        for paused in (False, True):
            with self.subTest(paused=paused):
                session=running();last=len(session.state['order'])-1
                session.update(phase='takeover',active=False,index=last)
                session.state['telemetry']['decks'][0].update(id=session.state['order'][last]['id'],playing=not paused)
                with patch('afterhours.cue_planner.verify_receipts'),patch.object(session,'_launch') as launch:
                    session.start(resume=True,resume_paused=paused)
                launch.assert_called_once();self.assertEqual(launch.call_args.args[1],'loading')
                # The exception is only for the exact retained applied plan.
                session.state['executionPlan']['orderIds']=list(reversed(session.state['executionPlan']['orderIds']))
                with patch.object(session,'_launch') as launch:
                    with self.assertRaises(ValueError):session.start(resume=True,resume_paused=paused)
                launch.assert_not_called()
        tracks=RunSession().state['candidates'][:3];ids=[t['id'] for t in tracks]
        with self.assertRaises(ValueError):validate_order(ids,tracks,history_prefix=ids[:2])
    def test_unchanged_history_required_and_ordinary_queue_still_max_twelve(self):
        s=RunSession();tracks=s.state['candidates'][:24];ids=[t['id'] for t in tracks]
        with self.assertRaises(ValueError):validate_order(ids,tracks)
        self.assertEqual(len(validate_order(ids,tracks,history_prefix=ids[:20])),24)
        with self.assertRaisesRegex(ValueError,'history'):validate_order(ids[::-1],tracks,history_prefix=ids[:20])
        with self.assertRaises(ValueError):validate_order(ids+['new'],tracks,history_prefix=ids[:10])
    def test_real_session_revision_keeps_absolute_current_cued_and_locks(self):
        s=DJSession(threading.Lock(),client_factory=Mock(side_effect=AssertionError('No native calls')))
        tracks=RunSession().state['candidates'][:30];s.update(candidates=tracks,order=tracks[:23],active=True,phase='playing',index=20,
            executionPlan={'applied':True,'id':'plan','targetBpm':125,'tempoSchedule':[
                {'trackId':t['id'],'fromBpm':125,'toBpm':125,'durationBeats':0,'startOffsetBeats':0,'maxPitchPercent':8} for t in tracks[:23]]})
        ids=[t['id'] for t in tracks[:24]];s.revise_queue(ids,reason='Keep the set supplied.')
        q=s.snapshot()['queuedUpdate'];self.assertEqual([t['id'] for t in q['tracks']],ids)
        self.assertEqual(q['status'],'needs-analysis');self.assertEqual(q['protectedIds'],ids[:22])
        changed=ids[:];changed[20],changed[22]=changed[22],changed[20]
        with self.assertRaisesRegex(ValueError,'protected'):s.revise_queue(changed,reason='Invalid replacement')
    def test_projected_decision_rejects_hidden_history_and_preserves_tempo(self):
        s=RunSession();s.update(order=s.state['candidates'][:24],index=20,active=True,phase='playing');s.prepared()
        view,prefix=planning_view(s.snapshot())
        self.assertEqual(view['index'],0);self.assertEqual(view['executionPlan']['tempoSchedule'][0]['trackId'],'t20')
        raw=decision(['t20','t21','t22','t23']);result=expand_decision(raw,prefix)
        self.assertEqual(len(validate_decision(result,s.snapshot())['trackIds']),24)
        raw['trackIds'][0]='t00'
        with self.assertRaises(ValueError):validate_decision(expand_decision(raw,prefix),s.snapshot())
        raw=decision(['t20','t21','t22','t23']);raw['tempoTargets']=[{'trackId':'t20','bpm':140}]
        with self.assertRaises(ValueError):validate_decision(expand_decision(raw,prefix),s.snapshot())
