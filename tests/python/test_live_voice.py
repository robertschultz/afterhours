"""GPT-Live boundary with fake HTTP. No paid requests, microphone or uploads."""
import json,threading,unittest
from unittest.mock import Mock,patch
from urllib.error import URLError
from afterhours.live_voice import LiveVoice
from afterhours.dj_agent import DJAgent
from test_dj_agent import Session

class Response:
    def __init__(self,data):self.data=json.dumps(data).encode()
    def __enter__(self):return self
    def __exit__(self,*args):pass
    def read(self,n):return self.data[:n]

class VoiceTests(unittest.TestCase):
    def setUp(self):
        self.key=patch('afterhours.ai_mix.api_key',return_value='sk-fixture-not-a-real-key');self.key.start()
        self.agent=DJAgent(Session(),evaluator=Mock(side_effect=AssertionError('No inference expected')))
        self.requests=[]
        def provider(request,**kwargs):
            self.requests.append(request)
            return Response({} if request.full_url.endswith('/hangup') else {'session':{'id':'voice_fixture'},'transport':{'sdp':'v=0\r\nanswer'}})
        self.voice=LiveVoice(self.agent,opener=provider)
    def tearDown(self):
        if self.voice.timer:self.voice.timer.cancel()
        self.key.stop()
    def start(self):return self.voice.create('v=0\r\noffer','request_123','nova',True)
    def test_no_approval_and_invalid_inputs_never_call_provider(self):
        for args in [('v=0','request_123','nova',False),('bad','request_123','nova',True),('v=0','short','nova',True),('v=0','request_123','unknown',True)]:
            with self.assertRaises(ValueError):self.voice.create(*args)
        self.assertEqual(self.requests,[])
    def test_configuration_and_response_hide_project_key(self):
        result=self.start();body=json.loads(self.requests[0].data)
        self.assertEqual(body['session']['model'],'gpt-live-1.1');self.assertEqual(body['session']['delegation'],{'type':'client'})
        self.assertFalse(body['session']['store']);self.assertEqual(body['session']['audio']['output']['voice'],'meridian')
        self.assertNotIn('sk-fixture',json.dumps(result));self.assertNotIn('sk-fixture',json.dumps(self.voice.status()))
        self.assertEqual(self.agent.snapshot()['allowance']['remaining'],6);self.assertEqual(self.voice.timer.interval,120)
    def test_one_session_and_duplicate_attempt_guard(self):
        self.start()
        with self.assertRaises(ValueError):self.voice.create('v=0','request_456','rivet',True)
        self.voice.end();self.assertEqual(self.agent.snapshot()['allowance']['remaining'],0)
        with self.assertRaises(ValueError):self.start()
        self.assertEqual(len(self.requests),2)
    def test_explicit_long_talk_has_matching_server_deadline_and_decision_allowance(self):
        result=self.voice.create('v=0\r\noffer','long_request_123','nova',True,1800)
        self.assertEqual(result['maxSeconds'],1800)
        self.assertAlmostEqual(result['expiresAt']-result['startedAt'],1800,places=2)
        self.assertEqual(self.voice.timer.interval,1800)
        self.assertEqual(self.agent.snapshot()['allowance']['remaining'],60)
        self.assertIn('30-minute session limit',json.loads(self.requests[0].data)['session']['instructions'])
        self.voice.end();self.assertEqual(self.agent.snapshot()['allowance']['remaining'],0)
    def test_unbounded_or_coerced_session_durations_are_rejected_before_provider(self):
        for seconds in (0,3600,1800.0,'1800',True,None):
            with self.assertRaises(ValueError):self.voice.create('v=0','bounded_request','nova',True,seconds)
        self.assertEqual(self.requests,[])
    def test_final_usage_is_cumulative_and_stops_allowance(self):
        self.start();self.voice.receipt('voice_fixture',12,False);self.voice.receipt('voice_fixture',10,False)
        self.assertEqual(self.voice.status()['usageSeconds'],12)
        self.voice.receipt('voice_fixture',15,True)
        self.assertTrue(self.voice.status()['finalized']);self.assertEqual(self.agent.snapshot()['allowance']['remaining'],0)
        with self.assertRaises(ValueError):self.voice.require('voice_fixture')

    def test_hangup_and_final_receipt_preserve_only_started_set_allowance(self):
        from test_playback_requests import PlaybackSession
        self.agent.session=PlaybackSession();s=self.agent.session
        s.plan(['a','b','c']);s.update(executionPlan={'applied':True,'id':'prepared'})
        self.start();deadline=self.agent.deadline
        self.agent.message('start playing',source='voice',message_id='explicit-start');self.agent.tick()
        self.voice.end();self.voice.receipt('voice_fixture',10,True)
        self.assertEqual(self.voice.status()['status'],'closed')
        self.assertTrue(self.agent.snapshot()['allowance']['background'])
        self.assertEqual(self.agent.remaining,6);self.assertEqual(self.agent.deadline,deadline)
        self.voice.create('v=0\r\noffer','second_request','nova',True)
        self.assertEqual(self.agent.remaining,6);self.assertFalse(self.agent.background_allowance)
    def test_uncertain_start_has_no_automatic_retry(self):
        self.voice.opener=Mock(side_effect=URLError('Synthetic network failure'))
        with self.assertRaisesRegex(ValueError,'No automatic retry'):self.start()
        self.assertEqual(self.voice.opener.call_count,1);self.assertEqual(self.agent.snapshot()['allowance']['remaining'],0)
    def test_hangup_failure_is_explicit_and_does_not_claim_final_usage(self):
        self.start();self.voice.opener=Mock(side_effect=URLError('Synthetic failure'))
        state=self.voice.end();self.assertEqual(state['status'],'close-unconfirmed');self.assertFalse(state['finalized'])
        with self.assertRaises(ValueError):self.voice.create('v=0','request_456','nova',True)
    def test_expired_sessions_cannot_delegate_even_if_timer_is_delayed(self):
        self.start();self.voice.state['expiresAt']=0
        with self.assertRaises(ValueError):self.voice.require('voice_fixture')
    def test_cancel_during_creation_closes_returned_session(self):
        original=self.voice.opener;started=threading.Event();release=threading.Event();errors=[]
        def delayed(req,**kwargs):
            if not req.full_url.endswith('/hangup'):started.set();release.wait(2)
            return original(req,**kwargs)
        self.voice.opener=delayed
        def run():
            try:self.start()
            except ValueError as e:errors.append(str(e))
        t=threading.Thread(target=run);t.start();self.assertTrue(started.wait(1));self.voice.end();release.set();t.join(2)
        self.assertFalse(t.is_alive());self.assertIn('cancelled',errors[0]);self.assertEqual(self.voice.status()['status'],'closed')
        self.assertEqual(self.agent.snapshot()['allowance']['remaining'],0)

if __name__=='__main__':unittest.main()
