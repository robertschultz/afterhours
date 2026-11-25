"""Actual local HTTP routes with an isolated agent and voice service; no cloud calls."""
import http.client,json,threading,unittest
from http.server import ThreadingHTTPServer
from unittest.mock import Mock,patch
from afterhours import server
from afterhours.dj_agent import DJAgent
from afterhours.live_voice import LiveVoice
from test_dj_agent import Session,decision,settled

class ConversationHTTPTests(unittest.TestCase):
    def setUp(self):
        self.provider=Mock(side_effect=AssertionError('Unexpected voice provider call'))
        self.evaluator=Mock(return_value=(decision(),{'responseId':'http-fixture'}))
        self.agent=DJAgent(Session(),evaluator=self.evaluator);self.voice=LiveVoice(self.agent,opener=self.provider)
        self.patches=[patch.object(server,'DJ_AGENT',self.agent),patch.object(server,'LIVE_VOICE',self.voice)]
        for p in self.patches:p.start()
        self.httpd=ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        self.thread=threading.Thread(target=self.httpd.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):
        self.httpd.shutdown();self.httpd.server_close();self.thread.join(2)
        for p in self.patches:p.stop()
    def call(self,path,data=None,token=True,origin=None):
        c=http.client.HTTPConnection('127.0.0.1',self.httpd.server_port,timeout=3)
        headers={'Content-Type':'application/json'}
        if token:headers['X-DJ-Token']=server.TOKEN
        if origin:headers['Origin']=origin
        c.request('POST' if data is not None else 'GET',path,body=None if data is None else json.dumps(data),headers=headers)
        r=c.getresponse();body=json.loads(r.read());status=r.status;c.close();return status,body
    def test_endpoints_exist_and_require_token(self):
        for path in ('/api/dj-agent','/api/voice'):
            self.assertEqual(self.call(path,token=False)[0],403)
            self.assertEqual(self.call(path)[0],200)
        self.provider.assert_not_called();self.evaluator.assert_not_called()
    def test_catalog_search_and_put_back_share_authenticated_boundary_without_ai(self):
        self.agent.session.lock=threading.RLock()
        payload={'action':'message','surface':'catalog','text':'Find Artist','messageId':'catalog-http','approveRequest':True}
        self.assertEqual(self.call('/api/dj-agent',payload,token=False)[0],403)
        status,reply=self.call('/api/dj-agent',payload)
        self.assertEqual(status,200);self.assertEqual(reply['catalogBrowser']['search']['id'],'catalog-http')
        self.assertEqual(reply['allowance']['remaining'],0)
        self.assertEqual(self.call('/api/dj-agent',{'action':'catalog-put-back','searchId':'catalog-http'})[0],200)
        self.assertIsNone(self.call('/api/dj-agent')[1]['catalogBrowser']['search'])
        self.assertEqual(self.call('/api/dj-agent',{'action':'catalog-play-next','requestId':'bad','trackId':'a','expected':{}})[0],409)
        self.provider.assert_not_called();self.evaluator.assert_not_called()
    def test_foreign_origin_and_unapproved_voice_do_not_call_provider(self):
        self.assertEqual(self.call('/api/voice',{'action':'start'},origin='https://example.com')[0],403)
        self.assertEqual(self.call('/api/voice',{'action':'start','sdp':'v=0','requestId':'fixture-request'})[0],409)
        self.provider.assert_not_called()
    def test_settings_http_auth_retry_stale_revision_and_header_sync(self):
        from test_agent_scenarios import ScenarioSession
        local=ScenarioSession();local.plan(['a','b','c'])
        agent=DJAgent(local,evaluator=self.evaluator)
        with patch.object(server,'DJ_AGENT',agent),patch.object(server,'DJ_SESSION',local):
            before=local.snapshot()['order']
            expected=self.call('/api/dj-agent')[1]['settings']['expected']
            body={'action':'settings','requestId':'settings-http','change':{'kind':'lead','value':'mix'},'expected':expected}
            self.assertEqual(self.call('/api/dj-agent',body,token=False)[0],403)
            self.assertEqual(self.call('/api/dj-agent',body,origin='https://example.com')[0],403)
            status,result=self.call('/api/dj-agent',body)
            self.assertEqual(status,200);self.assertEqual(result['settings']['lead'],'mix')
            self.assertEqual(self.call('/api/dj-agent',body)[1]['settings'],result['settings'])
            self.assertEqual(self.call('/api/dj-agent',{**body,'requestId':'stale'})[0],409)
            self.assertEqual(self.call('/api/set-session',{'action':'copilot','command':'on','commandId':'header',
                'expected':local.snapshot()['copilotContext']})[0],200)
            self.assertEqual(self.call('/api/dj-agent')[1]['settings']['lead'],'all')
            expected=self.call('/api/dj-agent')[1]['settings']['expected']
            brief={**body,'requestId':'brief','change':{'kind':'brief','value':'Darker house at 128 BPM'},'expected':expected}
            self.assertEqual(self.call('/api/dj-agent',brief)[1]['settings']['brief'],'Darker house at 128 BPM')
            self.assertEqual(local.snapshot()['order'],before)
            self.assertEqual(self.call('/api/dj-agent')[1]['allowance']['remaining'],0)
        self.provider.assert_not_called();self.evaluator.assert_not_called()

    def test_spoken_settings_need_the_same_active_voice_identity(self):
        from test_agent_scenarios import ScenarioSession
        local=ScenarioSession();agent=DJAgent(local,evaluator=self.evaluator)
        body={'action':'message','text':'Mixing only','messageId':'voice-settings','source':'voice','sessionId':'voice-session'}
        with patch.object(server,'DJ_AGENT',agent):
            self.assertEqual(self.call('/api/dj-agent',body)[0],409)
            self.assertEqual(agent.snapshot()['settings']['lead'],'all')
            with patch.object(self.voice,'require') as require:
                status,result=self.call('/api/dj-agent',body)
                self.assertEqual(status,200);self.assertEqual(result['settings']['lead'],'mix')
                require.assert_called_once_with('voice-session')
        self.provider.assert_not_called();self.evaluator.assert_not_called()

    def test_standing_settings_http_revision_receipt_and_voice_surface(self):
        from test_agent_scenarios import ScenarioSession
        local=ScenarioSession();local.plan(['a','b','c'])
        agent=DJAgent(local,evaluator=self.evaluator)
        with patch.object(server,'DJ_AGENT',agent),patch.object(server,'DJ_SESSION',local):
            expected=self.call('/api/dj-agent')[1]['settings']['expected']
            body={'action':'settings','requestId':'style-http','expected':expected,
                  'change':{'kind':'style','value':{'id':'energy','stop':4}}}
            self.assertEqual(self.call('/api/dj-agent',body,token=False)[0],403)
            status,result=self.call('/api/dj-agent',body)
            self.assertEqual(status,200);self.assertEqual(result['settings']['style']['energy'],4)
            self.assertEqual(self.call('/api/dj-agent',body)[1]['settings'],result['settings'])
            stale={**body,'requestId':'stale-http','change':{'kind':'discover','value':False}}
            status,error=self.call('/api/dj-agent',stale)
            self.assertEqual(status,409);self.assertIn('Settings changed',error['error'])
            self.assertTrue(self.call('/api/dj-agent')[1]['settings']['discover'])
            voice={'action':'message','surface':'settings','text':'fewer vocals','source':'voice',
                   'sessionId':'voice-session','messageId':'style-voice','approveRequest':True}
            self.assertEqual(self.call('/api/dj-agent',voice)[0],409)
            with patch.object(self.voice,'require') as require:
                status,result=self.call('/api/dj-agent',voice)
                self.assertEqual(status,200);require.assert_called_once_with('voice-session')
            self.assertEqual(result['settings']['style']['vocals'],2)
            self.assertEqual(result['settings']['receipt']['id'],'style-voice')
            self.assertEqual(result['allowance']['remaining'],0)
            self.assertEqual(local.calls,[])
        self.provider.assert_not_called();self.evaluator.assert_not_called()
    def test_native_device_delivery_requires_token_voice_identity_owner_and_claim(self):
        context={'ownerId':'native-http','sessionId':self.agent.session.snapshot()['id']}
        payload={'action':'message','text':'camera off','messageId':'native-http','source':'voice','sessionId':'voice-session','nativeSettingsContext':context}
        self.assertEqual(self.call('/api/dj-agent',payload)[0],409)
        with patch.object(self.voice,'require') as require:
            status,reply=self.call('/api/dj-agent',payload)
            self.assertEqual(status,200);require.assert_called_once_with('voice-session')
        self.assertEqual(reply['nativeSettingsCommand']['status'],'pending')
        delivery={'action':'native-settings-delivery','commandId':'native-http',**context,'status':'dispatching'}
        self.assertEqual(self.call('/api/dj-agent',delivery,token=False)[0],403)
        self.assertEqual(self.call('/api/dj-agent',delivery,origin='https://example.com')[0],403)
        self.assertEqual(self.call('/api/dj-agent',{**delivery,'ownerId':'wrong'})[0],409)
        self.assertEqual(self.call('/api/dj-agent',{**delivery,'status':'confirmed','message':'Premature success'})[0],409)
        self.assertEqual(self.call('/api/dj-agent',delivery)[0],200)
        self.assertEqual(self.call('/api/dj-agent',delivery)[0],409)
        status,reply=self.call('/api/dj-agent',{**delivery,'status':'confirmed','message':'Camera input is off.'})
        self.assertEqual(status,200);self.assertEqual(reply['messages'][-1]['text'],'Camera input is off.')
        self.assertEqual(reply['allowance']['remaining'],0)
        self.provider.assert_not_called();self.evaluator.assert_not_called()
    def test_text_submission_roundtrip_and_duplicate_is_not_replayed(self):
        data={'action':'message','text':'House, please','messageId':'http-message','approveRequest':True}
        self.assertEqual(self.call('/api/dj-agent',data)[0],200);settled(self.agent)
        self.assertEqual(self.call('/api/dj-agent',data)[0],200)
        status,state=self.call('/api/dj-agent');self.assertEqual(status,200)
        self.assertEqual(state['lastEvaluation']['responseId'],'http-fixture');self.assertEqual(self.evaluator.call_count,1)
    def test_voice_cannot_delegate_without_active_session(self):
        data={'action':'message','source':'voice','sessionId':'invented','text':'Play house','messageId':'no-session'}
        self.assertEqual(self.call('/api/dj-agent',data)[0],409);self.evaluator.assert_not_called()
    def test_send_prepares_automatically_with_one_disclosed_grant_and_no_transport(self):
        self.evaluator.return_value=(decision(['a','b','c']),{'responseId':'auto-http'})
        data={'action':'message','text':'Build this house set','messageId':'auto-http','approveRequest':True,'approvePreparation':True}
        self.assertEqual(self.call('/api/dj-agent',data)[0],200);settled(self.agent)
        for _ in range(8):self.agent.tick(force=True)
        self.assertEqual(self.call('/api/dj-agent',data)[0],200)
        status,state=self.call('/api/dj-agent');self.assertEqual(status,200)
        self.assertEqual(state['status'],'ready');self.assertIsNone(state['preparation'])
        self.assertEqual(state['allowance']['turnCeilingUSD'],.04)
        self.assertEqual(self.agent.session.calls,[('analyze',),('review',True),('apply',)])
        self.evaluator.assert_called_once();self.provider.assert_not_called()
    def test_string_consent_cannot_enable_an_additional_mix_review(self):
        self.evaluator.return_value=(decision(['a','b','c']),{'responseId':'old-client'})
        data={'action':'message','text':'House','messageId':'boolean-only','approveRequest':True,'approvePreparation':'true'}
        self.assertEqual(self.call('/api/dj-agent',data)[0],200);settled(self.agent)
        for _ in range(8):self.agent.tick(force=True)
        self.assertEqual(self.agent.session.calls,[('analyze',)])
    def test_background_retry_only_resets_a_blocked_running_job(self):
        from afterhours.vdj_session import DJSession
        local=DJSession(threading.Lock())
        with patch.object(server,'DJ_SESSION',local):
            self.assertEqual(self.call('/api/set-session',{'action':'retry-background'})[0],409)
            local.update(active=True,phase='playing',queuedUpdate={'id':'blocked','status':'blocked','error':'Needs four decks'})
            status,body=self.call('/api/set-session',{'action':'retry-background'})
            self.assertEqual(status,202);self.assertEqual(body['queuedUpdate']['status'],'needs-analysis')
            self.assertNotEqual(body['queuedUpdate']['id'],'blocked')
            self.assertEqual(self.call('/api/set-session',{'action':'retry-background'})[0],409)
        self.provider.assert_not_called();self.evaluator.assert_not_called()
    def test_prepare_set_is_explicit_idempotent_and_does_not_start_audio(self):
        data={'action':'prepare-set','requestId':'prepare-http'}
        self.assertEqual(self.call('/api/dj-agent',data)[0],409)
        self.assertEqual(self.agent.session.calls,[])
        data['approveRequest']=True
        self.assertEqual(self.call('/api/dj-agent',data)[0],200)
        for _ in range(6):self.agent.tick(force=True)
        self.assertEqual(self.call('/api/dj-agent',data)[0],200)
        self.assertEqual(self.agent.session.calls,[('analyze',),('review',True),('apply',)])
        self.evaluator.assert_not_called();self.provider.assert_not_called()
    def test_replace_is_atomic_with_message_and_preserves_reference_history(self):
        self.agent.state['direction']='Old dark house'
        data={'action':'message','text':'Dom Dolla','messageId':'fresh-http','approveRequest':True,'queueMode':'replace'}
        self.assertEqual(self.call('/api/dj-agent',data)[0],200);settled(self.agent)
        packet=self.evaluator.call_args.args[0]
        self.assertEqual(packet['direction'],'');self.assertEqual(packet['music']['currentOrder'],[])
        self.assertEqual(packet['messages'][-1]['text'],'Dom Dolla')
        self.assertEqual(self.call('/api/dj-agent',data)[0],200);self.assertEqual(self.evaluator.call_count,1)
    def test_replace_during_talk_is_rejected_before_mutating_or_spending(self):
        original=self.agent.session.snapshot()
        with patch.object(self.voice,'status',return_value={'status':'active'}):
            status,body=self.call('/api/dj-agent',{'action':'message','text':'Dom Dolla','messageId':'blocked','approveRequest':True,'queueMode':'replace'})
        self.assertEqual(status,409);self.assertIn('Stop Talk',body['error'])
        self.assertEqual(self.agent.session.snapshot(),original);self.evaluator.assert_not_called()

if __name__=='__main__':unittest.main()
