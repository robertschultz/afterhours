"""MCP policy/receipt regressions: fake session and provider, no SDK or live I/O."""
import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from afterhours import copilot_commands
from afterhours.dj_agent import DJAgent
from afterhours.mcp_auth import ControlError, client_config, local_url, write_credentials
from afterhours.mcp_control import MCPControl
from afterhours.mcp_receipts import ReceiptStore, ExternalGrant
from test_dj_agent import Session, decision, settled
from test_cue_planner import fixture as cue_fixture
from afterhours.vdj_session import DJSession
from afterhours.vdj_discovery import CatalogSearch


class FakeSession(Session):
    def __init__(self):
        super().__init__();self.lock=threading.RLock();self.review_callbacks=None
        self.state.update(copilot={'mode':'leading','selectionEpoch':0,'held':False})

    def copilot_command(self,action,identifier,expected):
        copilot_commands.check_context(self.state,expected)
        self.calls.append(('control',action))
        old=self.state['copilot']
        self.state['copilot']={**old,'mode':'off' if action=='off' else 'leading',
            'selectionEpoch':old['selectionEpoch']+1,
            'command':{'id':identifier,'action':action,'status':'confirmed','message':'Fixture control confirmed.'}}
        return self.snapshot()

    def stop(self,pause=True):
        self.calls.append(('stop',pause));self.update(phase='stopped',active=False)
        return self.snapshot()

    def review_mix(self,approved=False,authorize=None,confirm_authorization=None):
        self.review_callbacks=(authorize,confirm_authorization)
        if authorize:authorize()
        self.calls.append(('review',approved))
        if confirm_authorization:confirm_authorization()
        self.update(aiReview={'decision':'propose'})


class MCPTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.now=1000.0
        self.store=ReceiptStore(Path(self.temp.name)/'mcp'/'receipts.sqlite3',clock=lambda:self.now)
        self.addCleanup(lambda:self.store.close())
        self.session=FakeSession()
        self.provider=Mock(return_value=(decision(),{'responseId':'synthetic-provider'}))
        self.agent=DJAgent(self.session,evaluator=self.provider,clock=lambda:self.now)
        self.control=MCPControl(self.agent,self.store,clock=lambda:self.now)
        self.local=self.store.pair('Local client')
        self.music=self.store.pair('Music client',allowance=self.allowance())

    def allowance(self,**changes):
        return {'maxCostUSD':1,'maxProviderCalls':30,'expiresAt':self.now+600,'prepare':True,**changes}

    def payload(self,client=None,text='Find a house groove',request_id='direction-1',**extra):
        client=client or self.music
        return {'requestId':request_id,'text':text,'context':self.control.state(client['credential'])['context'],**extra}

    def direct(self,client=None,**kwargs):
        client=client or self.music
        return self.control.direct(client['credential'],self.payload(client,**kwargs))

    def assertCode(self,code,fn):
        with self.assertRaises(ControlError) as raised:fn()
        self.assertEqual(raised.exception.code,code)

    def claim(self,client=None,identifier='reserved',**changes):
        client=client or self.music
        row,_=self.store.claim(self.store.authenticate(client['credential']),identifier,{'fixture':identifier},
            {'sessionId':'fixture','revision':0},music=True,selection_cost=changes.get('cost',.02),
            preparation_cost=.02,selection_calls=2)
        return row

    def test_openai_setup_failure_has_a_safe_reason_in_the_receipt(self):
        self.provider.side_effect=ValueError('OpenAI key is not configured.')
        reply=self.direct()
        settled(self.agent)
        receipt=self.control.receipt(self.music['credential'],reply['requestId'])
        self.assertEqual(receipt['status'],'blocked')
        self.assertEqual(receipt['reasonCode'],'openai-key-missing')

    def test_named_play_without_accepted_tracks_returns_selection_hold(self):
        reply=self.direct(text='Play Christmas music')
        self.session.update(phase='catalog-ready',catalogSearch={'status':'complete','foundIds':[]})
        self.agent.tick(force=True)
        settled(self.agent)
        receipt=self.control.receipt(self.music['credential'],reply['requestId'])
        self.assertEqual((receipt['status'],receipt['reasonCode']),('blocked','selection-held'))

    def test_idle_external_named_play_replaces_finished_queue_before_planning(self):
        self.session.update(phase='completed', index=19, startedAt=self.now - 900)
        self.session.state['order'] *= 7
        for row in self.session.state['candidates']:
            row['engine'] = {'folder_id': 'beatport' if row['id'] == 'a' else 'apple'}
        self.session.new_set = Mock(side_effect=lambda: self.session.update(
            id='fresh-fixture', phase='idle', index=-1, order=[], queuedUpdate=None,
            startedAt=None, executionPlan=None))
        self.session.start = Mock(side_effect=lambda **kwargs: self.session.update(
            active=True, phase='loading'))
        self.provider.return_value = ({**decision(['b', 'c']),
                                       'sessionScope': {'mode': 'ongoing', 'message': 'Keep playing.'}}, {})

        receipt = self.direct(text='Play Christmas music')
        self.provider.assert_not_called()
        self.assertEqual(self.session.calls[-1][:2], ('discover', ('christmas; christmas music',)))
        self.session.update(phase='catalog-ready', catalogFolder={'id': 'apple'},
                            catalogSearch={'status': 'complete', 'foundIds': ['b', 'c']})
        self.agent.tick(force=True)
        settled(self.agent)
        self.agent.tick()

        self.session.new_set.assert_called_once_with()
        self.session.start.assert_called_once_with(initialize_mixer=True, immediate=True, rolling_queue=True,
                                                   allow_opening_catalog_fallback=True)
        packet = self.provider.call_args.args[0]
        self.assertEqual({row['id'] for row in packet['music']['catalog']}, {'b', 'c'})
        request = self.agent.playback_request
        self.assertEqual((request['sessionId'], request['orderIds']), ('fresh-fixture', ['b', 'c']))
        self.assertEqual(request['externalRequestId'], receipt['commandId'])
        self.assertEqual(self.control.receipt(self.music['credential'], receipt['requestId'])['status'], 'pending')
        self.session.update(phase='playing', telemetry={'decks': [{'id': 'b', 'playing': True}]})
        self.agent.tick()
        self.assertEqual(self.control.receipt(self.music['credential'], receipt['requestId'])['status'], 'confirmed')

    def test_live_named_direction_cannot_restart_set_when_the_original_ends(self):
        self.session.update(phase='playing', active=True, index=1,
                            telemetry={'decks': [{'id': 'b', 'playing': True}]})
        self.session.new_set = Mock()
        self.session.start = Mock()

        def planner(*args, **kwargs):
            self.session.update(phase='completed', active=False, index=2,
                                telemetry={'decks': [{'id': 'c', 'playing': False}]})
            return decision(), {}

        self.provider.side_effect = planner
        self.direct(text='Play Christmas music')
        settled(self.agent)
        self.agent.tick()
        self.session.new_set.assert_not_called()
        self.session.start.assert_not_called()
        self.assertIsNone(self.agent.playback_request)

    def test_open_ended_request_can_choose_live_tempo_fit_without_reasking_planner(self):
        self.session.new_set = Mock(side_effect=lambda: self.session.update(
            id='fresh-fixture', phase='idle', index=-1, order=[]))
        for row, bpm in zip(self.session.state['candidates'], [124, 150, 125]):
            row.update(bpm=bpm, engine={'folder_id': 'apple'})
        self.session.start = Mock(side_effect=lambda **kwargs: self.session.update(
            active=True, phase='loading'))
        self.provider.return_value = ({**decision(['a', 'b']),
                                       'sessionScope': {'mode': 'finite', 'message': 'Use the available small batch.'}}, {})

        self.direct(text='Play Christmas music')
        self.session.update(phase='catalog-ready', catalogFolder={'id': 'apple'},
                            catalogSearch={'status': 'complete', 'foundIds': ['a', 'b', 'c']})
        self.agent.tick(force=True)
        settled(self.agent)
        self.agent.tick()

        self.assertEqual(self.agent.playback_request['orderIds'], ['a', 'c'])
        self.assertEqual(self.agent.playback_request['scope'], 'ongoing')
        self.provider.assert_called_once()
        self.session.start.assert_called_once()

    def test_manual_takeover_returns_specific_hold_and_leaves_deck_alone(self):
        self.session.update(phase='takeover',active=False,
                            telemetry={'decks':[{'deck':1,'id':'a','playing':True}]})
        self.provider.return_value=(decision(['a','b','c']),{})
        reply=self.direct(text='Play Christmas music')
        settled(self.agent)
        receipt=self.control.receipt(self.music['credential'],reply['requestId'])
        self.assertEqual((receipt['status'],receipt['reasonCode']),('blocked','manual-playback'))
        self.assertEqual(self.session.calls,[])

    def test_default_local_does_not_borrow_active_operator_allowance(self):
        self.agent.enable(True,180,6)
        before=copy.copy(self.agent._operator_allowance)
        self.assertCode('allowance-required',lambda:self.direct(self.local))
        self.assertEqual(vars(before),vars(self.agent._operator_allowance))
        self.assertIsNone(self.agent._external_allowance)
        self.provider.assert_not_called();self.assertEqual(self.session.calls,[])

    def test_two_local_clients_resume_without_borrowing_or_renewing_talk(self):
        self.agent.enable(True,120,6)
        before=copy.copy(self.agent._operator_allowance)
        other=self.store.pair('Second local')
        for index,client in enumerate((self.local,other)):
            self.direct(client,text='copilot on',request_id=f'local-{index}')
            self.agent.tick(force=True)
            self.assertEqual(self.agent.remaining,0)
            self.assertEqual(vars(before),vars(self.agent._operator_allowance))
        self.provider.assert_not_called()

    def test_external_grant_never_funds_a_later_unapproved_typed_or_voice_turn(self):
        for source in ('typed','voice'):
            with self.subTest(source=source):
                self.direct(request_id=source);settled(self.agent)
                before=self.provider.call_count
                self.agent.message('Explain the key',source,source+'-operator')
                settled(self.agent)
                self.assertEqual(self.provider.call_count,before)
                self.assertIsNone(self.agent._external_allowance)
                self.assertEqual(self.agent._operator_allowance.remaining,0)

    def test_read_only_client_can_read_but_cannot_even_send_local_control(self):
        client=self.store.pair('Read only',read_only=True)
        self.assertEqual(self.control.state(client['credential'])['client']['scopes'],['state:read'])
        self.assertCode('forbidden',lambda:self.direct(client,text='copilot on'))
        self.assertEqual(self.session.calls,[])

    def test_context_rejects_session_revision_current_cued_ownership_and_client_changes(self):
        original=copy.deepcopy(self.session.state)
        mutations=[lambda:self.session.state.update(id='changed'),
                   lambda:self.session.state.update(index=1),
                   lambda:self.session.state['order'].reverse(),
                   lambda:self.session.state['copilot'].update(mode='off'),
                   lambda:self.session.state['copilot'].update(selectionEpoch=3),
                   lambda:self.session.state.update(telemetry={'decks':[{'deck':1,'id':'b','playing':True}]})]
        for mutate in mutations:
            self.session.state=copy.deepcopy(original)
            payload=self.payload(text='copilot on');before=self.agent.generation
            mutate()
            self.assertCode('stale-context',lambda:self.control.direct(self.music['credential'],payload))
            self.assertEqual(self.agent.generation,before);self.assertEqual(self.session.calls,[])
        self.session.state=original
        payload=self.payload(text='copilot on')
        self.assertCode('stale-context',lambda:self.control.direct(self.local['credential'],payload))
        self.agent.state['revision']+=1
        self.assertCode('stale-context',lambda:self.control.direct(self.music['credential'],payload))

    def test_expired_context_and_invalid_payload_do_not_replace_pending_operation(self):
        self.agent.pipeline={'revision':0,'ids':['a','b','c'],'stage':'wait-analysis','deadline':self.now+500}
        before=copy.deepcopy(self.agent.pipeline)
        payload=self.payload(text='copilot on');self.now+=61
        self.assertCode('stale-context',lambda:self.control.direct(self.music['credential'],payload))
        self.assertEqual(self.agent.pipeline,before)
        for extra in ({'source':'typed'},{'approved':True},{'sourceIdentity':{'name':'You'}},{'requestId':[]}):
            self.assertCode('invalid-request',lambda:self.control.direct(self.music['credential'],{**self.payload(),**extra}))
        self.assertEqual(self.agent.pipeline,before)

    def test_rejected_device_direction_does_not_reassign_or_cancel_an_existing_grant(self):
        row=self.claim();grant=ExternalGrant(self.store,row,self.store.authenticate(self.music['credential']))
        self.agent._use_allowance(grant)
        before=self.agent.generation
        self.agent.roster_command=lambda _: {'kind':'handoff','id':'fixture'}
        self.assertCode('unsupported-operation',lambda:self.direct(self.local,text='Switch to Fixture'))
        other=self.store.pair('Other music',allowance=self.allowance())
        rejected=ExternalGrant(self.store,self.claim(other,'rejected'),self.store.authenticate(other['credential']))
        with self.assertRaises(ValueError):self.agent.message('Switch to Fixture','mcp',rejected.id,external=rejected)
        self.assertIs(self.agent._external_allowance,grant)
        self.assertEqual(self.agent.generation,before);grant.check()

    def test_transport_status_does_not_change_an_external_owner(self):
        row=self.claim();grant=ExternalGrant(self.store,row,self.store.authenticate(self.music['credential']))
        self.agent._use_allowance(grant);before=self.agent.generation
        self.agent.message('status','typed','operator-status')
        self.assertIs(self.agent._external_allowance,grant);self.assertEqual(self.agent.generation,before)

    def test_unsupported_settings_and_set_receipts_do_not_replace_the_current_owner(self):
        row=self.claim();grant=ExternalGrant(self.store,row,self.store.authenticate(self.music['credential']))
        self.agent._use_allowance(grant)
        generation=self.agent.generation;settings=copy.deepcopy(self.agent.settings.state)
        for index,(surface,text) in enumerate((('settings','make the interface purple'),
                ('set','make the interface purple'),('set','pin missing recording'),
                ('set','make the warm-up longer'))):
            with self.subTest(surface=surface,text=text):
                payload=self.payload(self.local,text=text,request_id=f'unsupported-{index}',surface=surface)
                result=self.control.direct(self.local['credential'],payload)
                self.assertEqual(result['status'],'blocked')
                self.assertEqual(result['reasonCode'],'engine-guard')
                self.assertEqual(self.control.direct(self.local['credential'],payload),result)
                self.assertIs(self.agent._external_allowance,grant)
                self.assertEqual(self.agent.generation,generation);grant.check()
                self.assertEqual(self.agent.settings.state,settings)
                self.assertEqual(self.agent.state['messages'],[])
        self.provider.assert_not_called();self.assertEqual(self.session.calls,[])

    def test_supported_local_settings_and_plan_changes_still_confirm(self):
        result=self.direct(self.local,text='turn discovery off',surface='settings')
        self.assertEqual(result['status'],'confirmed');self.assertFalse(self.agent.settings.state['discover'])
        self.agent.settings.state['plan']={'startAt':1000,'endAt':15400,'peakAt':10000,
            'warm':True,'onePeak':True,'darkLate':False}
        result=self.direct(self.local,text='clear the night plan',surface='set',request_id='clear-plan')
        self.assertEqual(result['status'],'confirmed');self.assertIsNone(self.agent.settings.state['plan'])
        result=self.direct(self.local,text='stop music',request_id='stop')
        self.assertEqual(result['status'],'confirmed');self.assertIn(('stop',True),self.session.calls)
        self.provider.assert_not_called()

    def test_typed_unsupported_surface_messages_keep_the_existing_explanation(self):
        for surface in ('settings','set'):
            self.agent.message('make the interface purple',message_id='typed-'+surface,surface=surface)
            self.assertEqual(self.agent.state['status'],'ready')
            self.assertIn('No setting changed.' if surface=='settings' else 'No plan changed.',
                          self.agent.state['messages'][-1]['text'])
        self.provider.assert_not_called();self.assertEqual(self.session.calls,[])

    def test_mcp_status_is_terminal_without_changing_current_owner(self):
        row=self.claim();grant=ExternalGrant(self.store,row,self.store.authenticate(self.music['credential']))
        self.agent._use_allowance(grant);before=self.agent.generation
        result=self.direct(self.local,text='status',request_id='external-status')
        self.assertEqual(result['status'],'confirmed')
        self.assertEqual(self.control.receipt(self.local['credential'],'external-status')['status'],'confirmed')
        self.assertIs(self.agent._external_allowance,grant);self.assertEqual(self.agent.generation,before)

    def test_copilot_acknowledgement_never_confirms_pending_failed_or_unknown_execution(self):
        original=self.session.copilot_command
        def pending(action,identifier,expected):
            original(action,identifier,expected)
            self.session.state['copilot']['command']['status']='pending'
            return self.session.snapshot()
        self.session.copilot_command=pending
        for index,outcome in enumerate(('confirmed','failed','unconfirmed')):
            with self.subTest(outcome=outcome):
                result=self.direct(self.local,text='copilot on',request_id=f'pending-{index}')
                self.assertEqual(result['status'],'pending')
                self.assertEqual(self.control.receipt(self.local['credential'],f'pending-{index}')['status'],'pending')
                self.session.state['copilot']['command']['status']=outcome
                expected={'confirmed':'confirmed','failed':'blocked','unconfirmed':'uncertain'}[outcome]
                self.assertEqual(self.control.receipt(self.local['credential'],f'pending-{index}')['status'],expected)

    def test_delayed_receipt_poll_uses_original_control_history(self):
        original=self.session.copilot_command
        self.session.copilot_requests={}
        def pending(action,identifier,expected):
            original(action,identifier,expected)
            command=self.session.state['copilot']['command'];command['status']='pending'
            self.session.copilot_requests[identifier]=command
            return self.session.snapshot()
        self.session.copilot_command=pending
        first=self.direct(self.local,text='copilot on',request_id='first')
        self.session.copilot_requests[first['commandId']]['status']='confirmed'
        self.session.state['copilot']['command']={'id':'new-native-control','status':'pending'}
        result=self.control.receipt(self.local['credential'],'first')
        self.assertEqual(result['status'],'confirmed')

    def test_catalog_import_acknowledgement_stays_pending_and_never_starts_paid_selection(self):
        def discover(*args,**kwargs):
            self.session.update(catalogSearch={'status':'queued','externalRequestId':kwargs['external_request_id']})
        self.session.discover_more=Mock(side_effect=discover)
        result=self.direct(text='Import house to my catalog')
        self.assertEqual(result['status'],'pending')
        self.assertEqual(self.agent.remaining,0)
        self.assertCode('busy',lambda:self.direct(self.local,text='copilot on',request_id='other'))
        self.session.state['catalogSearch']['status']='complete'
        self.agent.tick(force=True)
        self.provider.assert_not_called()
        self.assertEqual(self.control.receipt(self.music['credential'],'direction-1')['status'],'confirmed')

    def test_catalog_worker_checks_bound_grant_and_retains_terminal_receipt(self):
        row=self.claim();grant=ExternalGrant(self.store,row,self.store.authenticate(self.music['credential']))
        session=DJSession(threading.Lock());self.addCleanup(session.crate.close)
        job=CatalogSearch(session,['house'],'','',40,300,external_request_id=grant.id,
                          authorize=grant.check,finish=grant.finish)
        job.publish('queued','Fixture queued')
        self.store.revoke(self.music['clientId'])
        with patch('afterhours.vdj_discovery.folder_state') as native:
            self.assertCode('client-revoked',lambda:job.step(Mock()))
            native.assert_not_called()
        job.publish('error','PRIVATE_PROVIDER_BODY')
        self.assertEqual(self.store.lookup(self.music['clientId'],row['id'])['status'],'blocked')
        self.assertNotIn('PRIVATE_PROVIDER_BODY',self.store.public(self.store.lookup(self.music['clientId'],row['id']))['message'])

    def model_search(self):
        search={'query':'house','artist':'','genre':'','bpmMin':120,'bpmMax':130}
        self.provider.return_value=(decision(search=search),{'responseId':'synthetic-provider'})
        jobs=[]
        def discover(query,artist='',genre='',bpm_min=40,bpm_max=300,**kwargs):
            job=CatalogSearch(self.session,[query],artist,genre,bpm_min,bpm_max,**kwargs)
            jobs.append(job);job.publish('queued','Synthetic search queued')
        self.session.discover_more=discover
        self.session.check=Mock();self.session.folder=None;self.session.send=Mock()
        self.direct();settled(self.agent)
        self.assertEqual(len(jobs),1)
        self.assertEqual(self.session.state['catalogSearch']['externalRequestId'],
                         self.store.lookup(self.music['clientId'],'direction-1')['engine_id'])
        return jobs[0]

    def test_model_selected_catalog_search_rechecks_revocation_before_every_step(self):
        job=self.model_search()
        with patch('afterhours.vdj_discovery.folder_state',return_value={'id':'synthetic-folder'}) as native:
            job.step(Mock());self.assertEqual(self.session.send.call_count,1)
            self.store.revoke(self.music['clientId']);native.reset_mock()
            self.assertCode('client-revoked',lambda:job.step(Mock()))
            native.assert_not_called();self.assertEqual(self.session.send.call_count,1)
        self.agent.tick(force=True)
        self.assertEqual(self.store.lookup(self.music['clientId'],'direction-1')['status'],'blocked')
        self.assertEqual(self.provider.call_count,1)

    def test_model_selected_catalog_search_cannot_dispatch_after_expiry(self):
        job=self.model_search();self.now+=601
        with patch('afterhours.vdj_discovery.folder_state') as native:
            self.assertCode('grant-expired',lambda:job.step(Mock()))
            native.assert_not_called();self.session.send.assert_not_called()

    def test_model_search_completion_keeps_the_grant_for_the_followup_selection(self):
        job=self.model_search();job.matched=3
        job.publish('complete','Synthetic search complete')
        self.assertEqual(self.control.receipt(self.music['credential'],'direction-1')['status'],'pending')
        self.agent._external_allowance.check()
        self.session.update(executionPlan={'applied':True})
        self.provider.return_value=(decision(['a','b','c']),{'responseId':'synthetic-followup'})
        self.agent.tick(force=True);settled(self.agent)
        result=self.control.receipt(self.music['credential'],'direction-1')
        self.assertEqual(result['status'],'confirmed');self.assertEqual(self.provider.call_count,2)
        self.assertEqual(self.provider.call_args.args[0]['reason'],'choose from catalog results')
        self.assertEqual([row['id'] for row in self.session.state['order']],['a','b','c'])

    def test_completed_mcp_setting_does_not_fund_or_block_operator_run_curation(self):
        from test_session_curation import SessionCurationTests
        fixture=SessionCurationTests();session,agent,provider,clock=fixture.make();fixture.playing(agent)
        store=ReceiptStore(Path(self.temp.name)/'curation'/'receipts.sqlite3',clock=lambda:clock[0])
        self.addCleanup(store.close)
        control=MCPControl(agent,store,clock=lambda:clock[0]);client=store.pair('Synthetic settings')
        result=control.direct(client['credential'],{'requestId':'setting','text':'turn discovery off',
            'surface':'settings','context':control.state(client['credential'])['context']})
        self.assertEqual(result['status'],'confirmed')
        before=store.lookup(client['clientId'],'setting');calls=provider.call_count
        entered=threading.Event();release=threading.Event()
        def refill(_):
            entered.set();self.assertTrue(release.wait(2));return decision(['t00','t01','t02','t03']),{}
        provider.side_effect=refill
        agent.tick(force=True);self.assertTrue(entered.wait(1))
        try:
            agent.tick(force=True)  # A second tick during the provider call must retain run ownership.
            self.assertIsNone(agent._external_allowance)
            self.assertEqual(agent.snapshot()['curation']['status'],'active')
        finally:release.set();settled(agent)
        self.assertEqual(provider.call_count,calls+1)
        self.assertEqual([row['id'] for row in session.state['order']],['t00','t01','t02','t03'])
        self.assertEqual(agent.snapshot()['curation']['remaining'],59)
        self.assertEqual(agent.snapshot()['curation']['status'],'active')
        after=store.lookup(client['clientId'],'setting')
        self.assertEqual((after['status'],after['consumed']),(before['status'],before['consumed']))
        self.assertNotIn('externalRequestId',agent.state['proposal'])

    def test_duplicate_survives_chat_eviction_and_changed_payload_conflicts(self):
        payload=self.payload(self.local,text='copilot on')
        first=self.control.direct(self.local['credential'],payload)
        self.agent.state['messages']=[]
        second=self.control.direct(self.local['credential'],payload)
        self.assertEqual(first,second);self.assertEqual(self.session.calls,[('control','on')])
        self.assertCode('request-conflict',lambda:self.control.direct(self.local['credential'],{**payload,'text':'copilot off'}))

    def test_concurrent_duplicates_claim_exactly_once(self):
        payload=self.payload(self.local,text='copilot on')
        with ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda _:self.control.direct(self.local['credential'],payload),range(8)))
        self.assertEqual(len({r['commandId'] for r in results}),1)
        self.assertEqual(self.session.calls,[('control','on')])

    def test_concurrent_same_id_changed_payload_never_dispatches_both(self):
        payload=self.payload(self.local,text='copilot on')
        barrier=threading.Barrier(8)
        def send(index):
            barrier.wait()
            try:return self.control.direct(self.local['credential'],{**payload,'text':'copilot on' if index%2 else 'copilot off'})
            except ControlError as error:return error.code
        with ThreadPoolExecutor(max_workers=8) as pool:results=list(pool.map(send,range(8)))
        self.assertEqual(sum(result=='request-conflict' for result in results),4)
        self.assertEqual(len(self.session.calls),1)
        self.assertEqual(len({result['commandId'] for result in results if isinstance(result,dict)}),1)

    def test_management_connections_do_not_recover_or_cancel_active_dispatch(self):
        row=self.claim()
        second=ReceiptStore(self.store.path,clock=lambda:self.now)
        try:
            paired=second.pair('Later client');second.clients();second.revoke(paired['clientId'])
            self.assertEqual(self.store.lookup(self.music['clientId'],row['id'])['status'],'dispatching')
            self.store.check(row['engine_id'])
            self.assertCode('dispatch-owner-active',second.acquire_dispatch_owner)
        finally:second.close()

    def test_actual_owner_restart_retains_uncertain_claim_and_never_replays(self):
        payload=self.payload(self.local,text='copilot on')
        row,_=self.store.claim(self.store.authenticate(self.local['credential']),'crashed',
            {**payload,'requestId':'crashed'},self.control._context(),music=False,selection_cost=.02,preparation_cost=.02,selection_calls=2)
        self.store.close()
        self.store=ReceiptStore(self.store.path,clock=lambda:self.now)
        self.control=MCPControl(self.agent,self.store,clock=lambda:self.now)
        result=self.control.direct(self.local['credential'],{**payload,'requestId':'crashed'})
        self.assertEqual(result['status'],'uncertain');self.assertFalse(result['replayable'])
        self.assertEqual(self.session.calls,[])

    def test_crash_after_dispatch_keeps_uncertain_receipt_and_never_repeats_action(self):
        payload=self.payload(self.local,text='copilot on',request_id='after-dispatch')
        def dispatch_then_crash(*args,**kwargs):
            self.session.calls.append(('control','on'))
            raise SystemExit('Simulated engine death after native dispatch, before outcome persistence')
        with patch.object(self.agent,'message',side_effect=dispatch_then_crash):
            with self.assertRaises(SystemExit):self.control.direct(self.local['credential'],payload)
        row=self.store.lookup(self.local['clientId'],'after-dispatch')
        self.store.update(row['engine_id'],'pending','Fixture acknowledgement before crash')
        self.store.close()
        self.store=ReceiptStore(self.store.path,clock=lambda:self.now)
        self.control=MCPControl(self.agent,self.store,clock=lambda:self.now)
        receipt=self.control.direct(self.local['credential'],payload)
        self.assertEqual(receipt['status'],'uncertain');self.assertEqual(receipt['reasonCode'],'outcome-unconfirmed')
        self.assertFalse(receipt['replayable']);self.assertEqual(self.session.calls,[('control','on')])

    def test_cross_client_receipts_and_revocation_fail_closed(self):
        self.direct(self.local,text='copilot on')
        self.assertCode('unknown-request',lambda:self.control.receipt(self.music['credential'],'direction-1'))
        self.store.revoke(self.local['clientId'])
        self.assertCode('unauthorized',lambda:self.control.receipt(self.local['credential'],'direction-1'))
        self.assertCode('unauthorized',lambda:self.control.state(self.local['credential']))

    def test_budget_reservations_are_atomic_and_nondivisible_ceiling_rounds_up(self):
        client=self.store.pair('One turn',allowance=self.allowance(maxCostUSD=.04,maxProviderCalls=3))
        def claim(i):
            try:return self.claim(client,str(i))
            except ControlError:return None
        with ThreadPoolExecutor(max_workers=8) as pool:rows=list(pool.map(claim,range(8)))
        self.assertEqual(sum(r is not None for r in rows),1)
        row=self.claim(identifier='round-up',cost=.000003)
        allocation=json.loads(row['allocation'])
        self.assertEqual(allocation['costs']['selection'],2)
        self.assertEqual(row['reserved_cost'],20004)

    def test_trusted_music_has_no_client_allowance_and_retains_receipts(self):
        client=self.store.pair('Trusted DJ',unmetered_music=True)
        for index in range(3):
            self.now+=86400
            receipt=self.direct(client,request_id=f'trusted-{index}')
            settled(self.agent)
            result=self.control.receipt(client['credential'],receipt['requestId'])
            self.assertEqual(result['providerCallsDispatched'],1)
        state=self.control.state(client['credential'])
        self.assertTrue(state['allowance']['unmetered'])
        self.assertIsNone(state['allowance']['configured'])
        self.assertEqual(state['allowance']['committedProviderCalls'],3)
        self.assertEqual(self.agent._operator_allowance.remaining,0)
        self.assertCode('allowance-required',lambda:self.direct(self.local))
        row=self.claim(client,identifier='trusted-revocation')
        self.store.revoke(client['clientId'])
        self.assertCode('client-revoked',lambda:self.store.consume(row['engine_id'],'selection'))
        self.assertCode('unauthorized',lambda:self.direct(client))

    def test_trusted_music_requires_its_own_explicit_pairing(self):
        for fields in ({'read_only':True,'unmetered_music':True},
                       {'allowance':self.allowance(),'unmetered_music':True},
                       {'unmetered_music':'true'}):
            with self.subTest(fields=fields):
                self.assertCode('invalid-allowance',lambda:self.store.pair('Conflicting client',**fields))

    def test_dispatched_calls_keep_ceiling_and_only_unused_reservation_is_released(self):
        row=self.claim();self.store.consume(row['engine_id'],'selection')
        self.store.update(row['engine_id'],'blocked','Fixture')
        public=self.store.public(self.store.lookup(self.music['clientId'],row['id']))
        self.assertEqual(public['reservedCeilingUSD'],.01);self.assertEqual(public['providerCallsDispatched'],1)
        self.assertCode('grant-inactive',lambda:self.store.consume(row['engine_id'],'review'))

    def test_revocation_during_selection_blocks_apply_and_preserves_playing_audio(self):
        entered=threading.Event();release=threading.Event()
        def evaluate(_):
            entered.set();self.assertTrue(release.wait(2));return decision(['a','b','c']),{}
        self.agent.evaluator=evaluate
        self.direct();self.assertTrue(entered.wait(1))
        self.session.update(active=True,phase='playing',index=0,telemetry={'decks':[{'id':'a','playing':True}]})
        self.store.revoke(self.music['clientId']);release.set();settled(self.agent)
        self.assertEqual(self.session.calls,[]);self.assertTrue(self.session.state['active'])
        self.assertEqual(self.store.lookup(self.music['clientId'],'direction-1')['status'],'blocked')

    def test_expiry_before_async_review_dispatch_blocks_new_work(self):
        self.provider.return_value=(decision(['a','b','c']),{})
        self.direct();settled(self.agent)
        self.agent.tick(force=True)
        self.assertEqual(self.agent.pipeline['stage'],'wait-analysis')
        self.now+=601
        self.agent.tick(force=True)
        self.assertNotIn(('review',True),self.session.calls)
        self.assertNotIn(('apply',),self.session.calls)

    def test_native_review_worker_checks_revocation_before_dispatch_and_after_provider_result(self):
        _,report,_=cue_fixture()
        for boundary in ('before-dispatch','after-provider'):
            with self.subTest(boundary=boundary):
                client=self.store.pair(boundary,allowance=self.allowance())
                row=self.claim(client,boundary)
                grant=ExternalGrant(self.store,row,self.store.authenticate(client['credential']))
                session=DJSession(threading.Lock());self.addCleanup(session.crate.close)
                session.update(candidates=report['tracks'],order=report['tracks'],analysis=report)
                peer=Mock();peer.status.return_value={'automix':False,'decks':[{'playing':False},{'playing':False}]}
                def provider(*args,**kwargs):
                    self.store.revoke(client['clientId'])
                    return {'decision':'propose','private':'PRIVATE_PROVIDER_BODY'}
                with patch.object(session,'_launch') as launch,patch('afterhours.cue_planner.verify_receipts'),patch('afterhours.ai_mix.plan',side_effect=provider) as plan:
                    session.review_mix(approved=True,authorize=lambda:grant.consume('review'),confirm_authorization=grant.check)
                    if boundary=='before-dispatch':self.store.revoke(client['clientId'])
                    self.assertCode('client-revoked',lambda:launch.call_args.args[0](peer))
                self.assertEqual(plan.call_count,0 if boundary=='before-dispatch' else 1)
                self.assertIsNone(session.state['aiReview'])
                self.assertIsNone(session.state['executionPlan'])
                self.assertFalse(any(event.get('kind')=='ai-review' for event in session.state['events']))
                consumed=json.loads(self.store.lookup(client['clientId'],boundary)['consumed'])
                self.assertEqual(consumed['review'],0 if boundary=='before-dispatch' else 1)

    def test_native_review_worker_rejects_expired_provider_result(self):
        _,report,_=cue_fixture()
        row=self.claim();grant=ExternalGrant(self.store,row,self.store.authenticate(self.music['credential']))
        session=DJSession(threading.Lock());self.addCleanup(session.crate.close)
        session.update(candidates=report['tracks'],order=report['tracks'],analysis=report)
        peer=Mock();peer.status.return_value={'automix':False,'decks':[{'playing':False},{'playing':False}]}
        def provider(*args,**kwargs):self.now+=601;return {'decision':'propose'}
        with patch.object(session,'_launch') as launch,patch('afterhours.cue_planner.verify_receipts'),patch('afterhours.ai_mix.plan',side_effect=provider) as plan:
            session.review_mix(approved=True,authorize=lambda:grant.consume('review'),confirm_authorization=grant.check)
            self.assertCode('grant-expired',lambda:launch.call_args.args[0](peer))
        plan.assert_called_once();self.assertIsNone(session.state['aiReview'])
        self.assertIsNone(session.state['executionPlan'])

    def test_typed_factual_turn_preserves_external_preparation_and_separates_review_budget(self):
        self.provider.return_value=(decision(['a','b','c']),{})
        self.direct();settled(self.agent)
        external=self.agent._external_allowance
        self.assertIsNotNone(self.agent.pipeline)
        self.provider.return_value=(decision(),{})
        self.agent.message('Explain the original BPM without changing music','typed','question',approved=True)
        settled(self.agent)
        self.assertIsNone(self.agent._external_allowance)
        self.assertEqual(self.agent.pipeline['externalRequestId'],external.id)
        external.check()
        self.agent.tick(force=True);self.agent.tick(force=True)
        row=self.store.lookup(self.music['clientId'],'direction-1')
        self.assertEqual(json.loads(row['consumed'])['selection'],1)
        self.assertEqual(json.loads(row['consumed'])['review'],1)
        self.assertEqual(self.agent._operator_allowance.remaining,0)

    def test_external_pipeline_without_prepare_never_consumes_restored_operator_voice_allowance(self):
        no_prepare=self.store.pair('Selection only',allowance=self.allowance(prepare=False))
        self.agent.enable(True,180,6)
        self.provider.return_value=(decision(['a','b','c']),{})
        self.direct(no_prepare);settled(self.agent)
        self.agent._use_allowance()  # A subsequent local operator turn owns its original voice budget.
        before=copy.copy(self.agent._operator_allowance)
        self.agent.tick(force=True);self.agent.tick(force=True)
        self.assertEqual(vars(self.agent._operator_allowance),vars(before))
        row=self.store.lookup(no_prepare['clientId'],'direction-1')
        self.assertEqual(json.loads(row['consumed'])['review'],0)
        self.assertNotIn(('review',True),self.session.calls)

    def test_nested_secrets_are_not_returned_in_state_receipts_or_provider_failure(self):
        sentinels=['PRIVATE_PROVIDER_BODY','/private/SECRET_MEDIA_PATH','PRIVATE_OPERATOR_CHAT','PRIVATE_RUNTIME_TOKEN']
        self.session.state.update(private={'body':sentinels[0]},token=sentinels[3])
        self.session.state['candidates'][0].update(path=sentinels[1],provider={'body':sentinels[0]})
        self.session.state['order'][0].update(path=sentinels[1],provider={'token':sentinels[3]})
        self.agent.state['messages']=[{'id':'private','role':'user','source':'typed','text':sentinels[2],'at':self.now}]
        self.agent.evaluator=Mock(side_effect=ValueError(' '.join(sentinels)))
        state=self.control.state(self.music['credential'])
        self.direct();settled(self.agent)
        receipt=self.control.receipt(self.music['credential'],'direction-1')
        encoded=json.dumps([state,receipt])
        for sentinel in sentinels:self.assertNotIn(sentinel,encoded)
        self.assertEqual(receipt['status'],'blocked')

    def test_bounded_musical_metadata_rejects_nonfinite_or_arbitrary_values(self):
        self.session.state['order'][0].update(bpm=124,duration=240,camelot='8A')
        self.session.state['order'][1].update(bpm=float('nan'),duration=float('inf'),camelot='PRIVATE_PROVIDER_BODY')
        records=self.control.state(self.local['credential'])['queue']['records']
        self.assertEqual({key:records[0][key] for key in ('bpm','durationSeconds','key')},
                         {'bpm':124,'durationSeconds':240,'key':'8A'})
        for key in ('bpm','durationSeconds','key'):self.assertNotIn(key,records[1])

    def test_applied_order_current_index_and_deferred_queue_are_distinct(self):
        self.session.state.update(index=1,active=True,phase='playing',
            queuedUpdate={'status':'needs-analysis','tracks':list(reversed(self.session.state['order']))})
        state=self.control.state(self.local['credential'],offset=1,limit=1)
        self.assertEqual(state['queue']['currentIndex'],1)
        self.assertEqual(state['queue']['records'][0]['id'],'b')
        self.assertEqual(state['current']['id'],'b');self.assertEqual(state['cued']['id'],'c')
        self.assertFalse(state['pendingQueue']['applied'])
        self.assertEqual(state['pendingQueue']['status'],'needs-analysis')
        self.assertEqual(state['pendingQueue']['total'],3);self.assertEqual(state['pendingQueue']['nextOffset'],2)
        state=self.control.state(self.local['credential'],offset=0,limit=1)
        self.assertEqual(state['queue']['records'][0]['id'],'a')
        self.assertEqual(state['pendingQueue']['records'][0]['id'],'c')

    def test_identity_is_registered_name_and_mcp_cannot_inject_source(self):
        self.direct(self.local,text='copilot on')
        row=next(m for m in self.agent.state['messages'] if m['role']=='user')
        self.assertEqual(row['sourceIdentity'],{'id':self.local['clientId'],'name':'Local client'})
        row['sourceIdentity']={'id':'forged','name':'Forged'}
        snapshot=self.agent.snapshot()
        self.assertEqual(snapshot['externalReceipt']['sourceIdentity']['name'],'Local client')
        self.assertEqual(next(m for m in snapshot['messages'] if m['role']=='user')['sourceIdentity']['name'],'Local client')

    def test_historical_external_labels_refresh_from_ledger_after_an_operator_turn(self):
        self.direct(self.local,text='copilot on')
        row=next(m for m in self.agent.state['messages'] if m['role']=='user')
        row['sourceIdentity']={'id':'forged','name':'You'}
        self.agent.state['messages'].append({'id':'unknown-mcp-row','role':'user','source':'mcp',
            'sourceIdentity':{'id':'forged','name':'You'},'text':'Unbound persisted row','at':self.now})
        self.agent.message('status','typed','operator-status')
        snapshot=self.agent.snapshot()
        self.assertIsNone(snapshot['externalReceipt'])
        historical=next(m for m in snapshot['messages'] if m['id']==row['id'])
        self.assertEqual(historical['sourceIdentity'],{'id':self.local['clientId'],'name':'Local client'})
        unknown=next(m for m in snapshot['messages'] if m['id']=='unknown-mcp-row')
        self.assertNotIn('sourceIdentity',unknown)

    def test_corrupt_database_and_unsafe_credential_file_fail_closed(self):
        path=Path(self.temp.name)/'bad';path.mkdir(mode=0o700)
        (path/'db').write_bytes(b'not sqlite');(path/'db').chmod(0o600)
        with self.assertRaises(Exception):ReceiptStore(path/'db')
        credentials=Path(self.temp.name)/'client.json'
        write_credentials(credentials,{'version':1,'url':'http://127.0.0.1:43210',
                                      'clientId':self.local['clientId'],'credential':self.local['credential']})
        self.assertEqual(client_config(credentials)['clientId'],self.local['clientId'])
        credentials.chmod(0o644)
        with self.assertRaises(ControlError):client_config(credentials)
        credentials.chmod(0o600)
        link=Path(self.temp.name)/'link';link.symlink_to(credentials)
        with self.assertRaises(OSError):client_config(link)
        for url in ('http://localhost:43210','https://127.0.0.1:43210','http://127.0.0.1:43210/api/config','http://x@127.0.0.1:43210'):
            with self.assertRaises(ControlError):local_url(url)


if __name__=='__main__':unittest.main()
