import unittest
import threading
import json
import tempfile
import io
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from afterhours.director import DirectorGate, APP
from afterhours.director_signaling import signaling_error
from afterhours.performance_server import make_server

OFFER={'app_id':APP,'sdp':'v=0\r\na=test-offer','type':'offer'}
SESSION='https://wma.fal.run/session'


def fixture_gate(*args, **kwargs):
    gate = DirectorGate(*args, **kwargs)
    gate.references['rivet'] = 'https://fixture.fal.media/rivet.jpg'
    return gate


class GateTests(unittest.TestCase):
    def setUp(self):
        self.clock=100.;self.gate=fixture_gate('fake-test-key',1,60,now=lambda:self.clock)
    def test_default_locked(self):
        gate=DirectorGate();self.assertEqual(gate.references,{})
        self.assertFalse(gate.snapshot()['ready'])
        with self.assertRaises(ValueError):gate.start()
    def test_only_one_admission(self):
        self.gate.start()
        with self.assertRaises(ValueError):self.gate.start()
        self.gate.stop()
        with self.assertRaises(ValueError):self.gate.start()
    def test_single_offer_including_uncertain_attempt(self):
        lease=self.gate.start()['lease'];self.gate.authorize(lease,SESSION,OFFER)
        with self.assertRaises(ValueError):self.gate.authorize(lease,SESSION,OFFER)
    def test_expiration_and_late_bind(self):
        lease=self.gate.start()['lease'];self.clock=161
        with self.assertRaises(ValueError):self.gate.authorize(lease,SESSION,OFFER)
        self.gate.bind(lease,b'{"session_id":"late"}');self.assertIsNone(self.gate.session_id)
    def test_allowlist_and_body(self):
        lease=self.gate.start()['lease']
        for url in ['http://wma.fal.run/session','https://wma.fal.run.evil/session','https://wma.fal.run/session?x=1','https://localhost/session','https://queue.fal.run/'+APP]:
            with self.assertRaises(ValueError):self.gate.authorize(lease,url,OFFER)
        with self.assertRaises(ValueError):self.gate.authorize(lease,SESSION,{**OFFER,'app_id':'other/app'})
        self.assertFalse(self.gate.submitted)
    def test_heartbeat_bound_to_session(self):
        lease=self.gate.start()['lease'];url='https://wma.fal.run/session/heartbeat'
        with self.assertRaises(ValueError):self.gate.authorize(lease,url,{'session_id':'forged'})
        self.gate.bind(lease,b'{"session_id":"own"}')
        self.assertEqual(self.gate.authorize(lease,url,{'session_id':'own'}),'heartbeat')
        with self.assertRaises(ValueError):self.gate.authorize(lease,url,{'session_id':'other'})
    def test_parallel_admission(self):
        results=[]
        def attempt():
            try:results.append(self.gate.start())
            except ValueError:pass
        ts=[threading.Thread(target=attempt) for _ in range(5)]
        for t in ts:t.start()
        for t in ts:t.join()
        self.assertEqual(len(results),1)

class HttpTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.server=make_server(0,captures=self.temporary.name,profiles=__import__('afterhours.agent_profiles', fromlist=['AgentProfiles']).AgentProfiles(self.temporary.name));self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start();self.base=f'http://127.0.0.1:{self.server.server_port}'
    def tearDown(self):self.server.shutdown();self.server.server_close();self.thread.join();self.temporary.cleanup()
    def post(self,path,body,headers=None):
        return urlopen(Request(self.base+path,method='POST',data=json.dumps(body).encode(),headers={'Content-Type':'application/json','X-Performance-Token':self.server.token,**(headers or {})}),timeout=2)
    def test_no_key_leaked_and_locked_no_network(self):
        with urlopen(self.base+'/api/performance/config') as r:data=json.load(r)
        self.assertFalse(data['director']['keyConfigured']);self.assertNotIn('key',data['director'])
        def forbidden(*a):raise AssertionError('Network called')
        self.server.director.upstream=forbidden
        with self.assertRaises(HTTPError) as ctx:self.post('/api/director/start',{'profile':'director-study-v1'})
        self.assertEqual(ctx.exception.code,400)
        with self.assertRaises(HTTPError) as ctx:self.post('/api/fal/proxy',OFFER,{'x-fal-target-url':SESSION})
        self.assertEqual(ctx.exception.code,403)
    def test_cross_origin_and_wrong_token(self):
        for headers in [{'Origin':'https://evil.invalid'},{'X-Performance-Token':'wrong'},{'Host':'evil.invalid'}]:
            with self.assertRaises(HTTPError) as ctx:self.post('/api/director/start',{},headers)
            self.assertEqual(ctx.exception.code,403)
    def test_video_byte_range(self):
        with urlopen(Request(self.base+'/media/rivet-demo.mp4',headers={'Range':'bytes=0-31'})) as r:
            self.assertEqual(r.status,206);self.assertEqual(len(r.read()),32);self.assertEqual(r.headers['Content-Type'],'video/mp4')
    def test_live_profile_requires_consent_and_does_not_call_provider(self):
        self.server.director=fixture_gate('fake',0)
        def forbidden(*a):raise AssertionError('Provider must not be called by admission')
        self.server.director.upstream=forbidden
        with self.assertRaises(HTTPError):self.post('/api/director/start',{'profile':'director-live-v1','minutes':5})
        with self.post('/api/director/start',{'profile':'director-live-v1','minutes':5,'consent':True}) as r:admission=json.load(r)
        self.assertEqual(admission['mode'],'live');self.assertIsNone(admission['sampleSeconds'])
        self.assertEqual(admission['generatedLimitSeconds'],285)
    def test_admitted_session_stop_and_rehearsal_guard(self):
        self.server.director=fixture_gate('fake',1,60)
        with self.post('/api/director/start',{'profile':'director-study-v1'}) as r:admission=json.load(r)
        self.assertIn('lease',admission)
        with self.assertRaises(HTTPError):self.post('/api/performance/command',{'action':'pause'})
        with self.post('/api/director/stop',{}) as r:self.assertTrue(json.load(r)['ok'])
        with self.post('/api/performance/command',{'action':'rehearse'}) as r:self.assertEqual(json.load(r)['mode'],'playing')
    def test_stale_character_cannot_consume_admission(self):
        self.server.director=fixture_gate('fake',1,60)
        self.server.director.references['nova']='https://v3.fal.media/nova.jpg'
        with self.post('/api/performance/command',{'action':'character','character':'nova'}):pass
        for profile in ('director-study-v1','director-live-v1'):
            with self.assertRaises(HTTPError) as ctx:
                self.post('/api/director/start',{'profile':profile,'expectedCharacter':'rivet','consent':True,'minutes':2})
            self.assertEqual(ctx.exception.code,400)
            self.assertIn('selected DJ changed',json.load(ctx.exception)['error'])
            self.assertEqual(self.server.director.used,0)
            self.assertFalse(self.server.director.snapshot()['active'])
        with self.post('/api/director/start',{'profile':'director-study-v1','expectedCharacter':'nova'}) as r:
            self.assertEqual(json.load(r)['character'],'nova')

    def test_character_switch_waiting_on_admission_is_rejected(self):
        self.server.director=fixture_gate('fake',1,60)
        entered=threading.Event();release=threading.Event();results=[]
        original=self.server.director.start
        def delayed(*args,**kwargs):
            entered.set();self.assertTrue(release.wait(1));return original(*args,**kwargs)
        self.server.director.start=delayed
        def start():
            with self.post('/api/director/start',{'profile':'director-study-v1','expectedCharacter':'rivet'}) as r:results.append(json.load(r)['character'])
        def switch():
            try:self.post('/api/performance/command',{'action':'character','character':'nova'})
            except HTTPError as error:results.append(error.code)
        starter=threading.Thread(target=start);starter.start();self.assertTrue(entered.wait(1))
        changer=threading.Thread(target=switch);changer.start();release.set()
        starter.join(2);changer.join(2)
        self.assertCountEqual(results,['rivet',400])
        self.assertEqual(self.server.performance.snapshot()['character'],'rivet')
    def test_provider_503_is_preserved_and_never_retried(self):
        self.server.director=fixture_gate('fake',1,60)
        a=self.server.director.start();calls=[]
        def upstream(target,data):
            calls.append(target)
            raise HTTPError(target,503,'Service unavailable',{},io.BytesIO(b'{"detail":"private runner diagnostic"}'))
        self.server.director.upstream=upstream
        headers={'X-Director-Lease':a['lease'],'x-fal-target-url':SESSION}
        with self.assertRaises(HTTPError) as ctx:self.post('/api/fal/proxy',OFFER,headers)
        self.assertEqual(ctx.exception.code,503)
        data=json.load(ctx.exception);self.assertIn('HTTP 503',data['error']);self.assertNotIn('private',str(data))
        with self.assertRaises(HTTPError) as ctx:self.post('/api/fal/proxy',OFFER,headers)
        self.assertEqual(ctx.exception.code,403);self.assertEqual(len(calls),1)

    def test_proxy_keeps_validation_status_and_safe_fields_without_retry(self):
        self.server.director=fixture_gate('fake',1,60)
        a=self.server.director.start();calls=[]
        def upstream(target,data):
            calls.append(target)
            body={'detail':[{'loc':['body','sdp'],'type':'value_error','msg':'Incompatible media section','input':'private SDP and credentials'}]}
            raise HTTPError(target,422,'Unprocessable Entity',{},io.BytesIO(json.dumps(body).encode()))
        self.server.director.upstream=upstream
        headers={'X-Director-Lease':a['lease'],'x-fal-target-url':SESSION}
        with self.assertRaises(HTTPError) as ctx:self.post('/api/fal/proxy',OFFER,headers)
        self.assertEqual(ctx.exception.code,422)
        data=json.load(ctx.exception);self.assertIn({'field':'body.sdp','issue':'value_error'},data['validation'])
        self.assertNotIn('private',json.dumps(data))
        with self.assertRaises(HTTPError) as ctx:self.post('/api/fal/proxy',OFFER,headers)
        self.assertEqual(ctx.exception.code,403);self.assertEqual(len(calls),1)

    def test_credit_verification_failure_reaches_browser_without_retry(self):
        self.server.director=fixture_gate('fake',1,60)
        a=self.server.director.start();calls=[]
        def upstream(target,data):
            calls.append(target)
            body={'detail':'Unable to verify session credit.','input':'private key and SDP'}
            raise HTTPError(target,503,'Service unavailable',{},io.BytesIO(json.dumps(body).encode()))
        self.server.director.upstream=upstream
        headers={'X-Director-Lease':a['lease'],'x-fal-target-url':SESSION}
        with self.assertRaises(HTTPError) as ctx:self.post('/api/fal/proxy',OFFER,headers)
        self.assertEqual(ctx.exception.code,503)
        result=json.load(ctx.exception)
        self.assertEqual(result['category'],'credit_verification_unavailable')
        self.assertNotIn('private',json.dumps(result))
        with self.assertRaises(HTTPError) as ctx:self.post('/api/fal/proxy',OFFER,headers)
        self.assertEqual(ctx.exception.code,403);self.assertEqual(len(calls),1)


class SignalingErrorTests(unittest.TestCase):
    def parse(self,body,code=422):
        with HTTPError('https://private.invalid/?token=secret',code,'secret',{},io.BytesIO(body)) as error:
            return signaling_error(error)
    def test_redacts_input_messages_and_unknown_fields(self):
        body={'detail':[{'loc':['body','private-secret','sdp'],'type':'private-secret','msg':'token=secret-key','input':{'sdp':'private-secret'}}]}
        result=self.parse(json.dumps(body).encode())
        self.assertEqual(result['validation'],[{'field':'body.sdp','issue':'invalid_value'}])
        self.assertNotIn('secret',json.dumps(result))
    def test_wrapped_negotiation_error_is_classified_without_raw_details(self):
        result=self.parse(json.dumps({'error':json.dumps({'detail':[{'loc':['body','sdp'],'type':'value_error','msg':'Incompatible media section: private=secret'}]})}).encode())
        self.assertIn({'field':'sdp','issue':'media_contract'},result['validation'])
        self.assertNotIn('secret',json.dumps(result))
    def test_malformed_oversized_or_other_status_bodies_remain_generic(self):
        for body in (b'not json secret',b'x'*65_537,b'{"detail":null}'):
            self.assertEqual(self.parse(body)['validation'],[])
        self.assertEqual(self.parse(b'{"error":"private"}',401)['validation'],[])
    def test_credit_classification_is_exact_bounded_and_not_an_empty_balance_claim(self):
        body={'detail':'Unable to verify session credit.','private':'do not copy'}
        for payload in (body,{'error':json.dumps(body)}):
            result=self.parse(json.dumps(payload).encode(),503)
            self.assertEqual(result['category'],'credit_verification_unavailable')
            self.assertEqual(result['validation'],[])
            self.assertNotIn('private',json.dumps(result))
            self.assertNotIn('insufficient',result['error'])
        for payload in ({'detail':'Unable to verify session credit. secret'}, {'detail':'Capacity unavailable'}, {'input':body}):
            self.assertNotIn('category',self.parse(json.dumps(payload).encode(),503))
        self.assertNotIn('category',self.parse(json.dumps(body).encode(),401))
        self.assertNotIn('category',self.parse(b'x'*65_537,503))


class NewSessionTests(unittest.TestCase):
    def test_key_stays_private_and_restart_does_not_renew_allowance(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger=Path(folder)/'admissions.json';gate=fixture_gate(approved_sessions=1,ledger=ledger)
            gate.set_key('private-test-only-key');self.assertNotIn('private-test',json.dumps(gate.snapshot()))
            gate.start();gate.stop();self.assertFalse(fixture_gate('new-key',1,ledger=ledger).snapshot()['ready'])
            self.assertNotIn('private-test',ledger.read_text())
    def test_study_projection_expires_with_owner_and_clock(self):
        clock=[100.];gate=fixture_gate('fake',1,now=lambda:clock[0]);a=gate.start()
        gate.music(a['lease'],{'positionSeconds':3});self.assertEqual(gate.program()['clock']['bpm'],124)
        clock[0]+=4;self.assertIsNone(gate.program())
        gate.music(a['lease'],{'positionSeconds':7});gate.stop();self.assertIsNone(gate.program())
        with self.assertRaises(ValueError):gate.music(a['lease'],{'positionSeconds':8})
    def test_missing_reference_does_not_consume_approval(self):
        gate=fixture_gate('fake',1)
        with self.assertRaises(ValueError):gate.start('nova')
        self.assertEqual(gate.used,0);gate.references['nova']='https://example.fal.media/nova.jpg'
        admission=gate.start('nova');self.assertEqual(admission['character'],'nova');self.assertEqual(admission['resolution'],'768p')
        self.assertNotEqual(admission['imageUrl'],gate.references['rivet'])
        gate.stop();self.assertTrue(gate.authorize_capture(admission['runId'],admission['captureToken']))
        self.assertFalse(gate.authorize_capture(admission['runId'],'wrong'))
    def test_explicit_cumulative_retry_allows_only_one_more_attempt(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger=Path(folder)/'admissions.json';gate=fixture_gate('fake',1,ledger=ledger)
            gate.start();gate.stop()
            self.assertFalse(fixture_gate('fake',1,ledger=ledger).snapshot()['ready'])
            approved=fixture_gate('fake',2,ledger=ledger);approved.start();approved.stop()
            self.assertEqual(approved.used,2)
            self.assertFalse(fixture_gate('fake',2,ledger=ledger).snapshot()['ready'])

class CaptureTests(HttpTests):
    def test_write_only_key_and_reference_consent(self):
        with self.post('/api/director/key',{'key':'test-key-no-provider-call'}) as response:self.assertTrue(json.load(response)['ok'])
        with urlopen(self.base+'/api/performance/config') as response:
            raw=response.read().decode();self.assertNotIn('test-key',raw);self.assertTrue(json.loads(raw)['director']['keyConfigured'])
        with self.assertRaises(HTTPError):self.post('/api/director/reference',{'character':'rivet','consent':False})
    def test_capture_scoped_to_admission_validated_and_no_overwrite(self):
        self.server.director=fixture_gate('fake',1)
        a=self.server.director.start();headers={'X-Director-Run':a['runId'],'X-Director-Capture':a['captureToken']}
        def write(body,mime='video/webm'):
            return urlopen(Request(self.base+'/api/director/capture',method='POST',data=body,headers={'X-Performance-Token':self.server.token,'Content-Type':mime,**headers}))
        with self.assertRaises(HTTPError):write(b'not a video')
        self.server.director.stop()
        with write(bytes.fromhex('1a45dfa3')+b'fixture') as response:
            result=json.load(response);self.assertEqual(result['bytes'],11)
        with urlopen(self.base+result['url']) as response:self.assertEqual(response.headers['Content-Type'],'video/webm')
        with self.assertRaises(HTTPError) as error:write(bytes.fromhex('1a45dfa3')+b'other')
        self.assertEqual(error.exception.code,409)
        with self.assertRaises(HTTPError):write(json.dumps({'runId':a['runId'],'key':'do-not-save'}).encode(),'application/json')
        with write(json.dumps({'runId':a['runId'],'frames':720,'events':[]}).encode(),'application/json') as response:self.assertEqual(response.status,200)


class ContinuousLiveTests(unittest.TestCase):
    def test_live_requires_explicit_bounded_consent_and_keeps_sample_locked(self):
        gate=fixture_gate('fake',0,now=lambda:100)
        for args in ({}, {'consent':True,'minutes':0},{'consent':True,'minutes':True},{'consent':True,'minutes':60}):
            with self.assertRaises(ValueError):gate.start(mode='live',**args)
        self.assertEqual(gate.used,0)
        a=gate.start(mode='live',consent=True,minutes=5)
        self.assertIsNone(a['sampleSeconds']);self.assertEqual(a['generatedLimitSeconds'],285);self.assertEqual(a['deadline'],520)
        with self.assertRaises(ValueError):gate.start(mode='live',consent=True,minutes=5)
        gate.stop()
        with self.assertRaises(ValueError):gate.start()
        # A later explicit user action can start a separate live session.
        b=gate.start(mode='live',consent=True,minutes=2);self.assertNotEqual(a['runId'],b['runId'])
        self.assertEqual(gate.used,2)
    def test_failed_live_attempt_remains_in_ledger(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'admission.json';gate=fixture_gate('fake',0,ledger=path)
            gate.start(mode='live',consent=True,minutes=2);gate.stop()
            saved=json.loads(path.read_text());self.assertEqual(saved['mode'],'live');self.assertEqual(saved['minutes'],2)
            restored=fixture_gate('fake',0,ledger=path);self.assertEqual(restored.used,1)
            self.assertFalse(restored.snapshot()['ready']);self.assertTrue(restored.snapshot()['liveReady'])

if __name__=='__main__':unittest.main()
