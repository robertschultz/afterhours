"""Authenticated Recap routing and real disposable HTTP contract fixture."""
import copy
import http.client
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock,patch

from afterhours import server
from afterhours.dj_agent import DJAgent
from afterhours.run_archive import RunArchive
from test_run_archive import Clock,setup,emit


class RecapHTTPTests(unittest.TestCase):
    def test_auth_context_receipts_and_selected_run_queries_have_no_music_side_effects(self):
        clock=Clock();store=RunArchive(clock=clock.wall,monotonic=clock.monotonic)
        session,native=setup(clock,store);provider=Mock(side_effect=AssertionError('No provider access'))
        agent=DJAgent(session,evaluator=provider,clock=clock.wall)
        httpd=ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        thread=threading.Thread(target=httpd.serve_forever,daemon=True);thread.start()
        def call(method,path,body=None,token=True,origin=None,host=None):
            client=http.client.HTTPConnection('127.0.0.1',httpd.server_port,timeout=3)
            headers={'Content-Type':'application/json'}
            if token:headers['X-DJ-Token']=server.TOKEN
            if origin:headers['Origin']=origin
            if host:headers['Host']=host
            client.request(method,path,body=json.dumps(body) if body is not None else None,headers=headers)
            response=client.getresponse();data=json.loads(response.read());status=response.status;client.close()
            return status,data
        def get(path):
            status,data=call('GET',path);self.assertEqual(status,200);return data
        def post(body):
            status,data=call('POST','/api/dj-agent',body);self.assertEqual(status,200);return data
        fixture={'evidence':'Disposable HTTP service with injected exact A/B playback observations; no native, provider or audible acceptance.'}
        try:
            with patch.object(server,'DJ_AGENT',agent),patch.object(server,'DJ_SESSION',session):
                self.assertNotIn('lastSet',get('/api/dj-agent'))
                for status in ('completed','stopped','interrupted'):
                    emit(session,clock)
                    change={'action':'settings','requestId':'during-'+status,'expected':agent.snapshot()['settings']['expected'],
                            'change':{'kind':'style','value':{'id':'blend','stop':4}}}
                    post(change);clock.advance(2);emit(session,clock)
                    clock.advance(1)
                    emit(session,clock,a=status=='interrupted',phase='error' if status=='interrupted' else status,active=False)
                    self.assertTrue(store.flush());snapshot=get('/api/dj-agent');identifier=snapshot['lastSet']['id']
                    detail=get('/api/recap?archiveId='+identifier)
                    self.assertEqual(detail['status'],status)
                    self.assertEqual(detail['endedAt'] is None,status=='interrupted')
                    self.assertEqual(detail['settingsExpected'],snapshot['settings']['expected'])
                    fixture[status]={'agent':snapshot,'session':session.snapshot(),'detail':detail}
                    clock.advance(1)
                identifier=fixture['stopped']['detail']['id'];detail=get('/api/recap?archiveId='+identifier)
                query='/api/recap?archiveId='+identifier
                self.assertEqual(call('GET',query,token=False)[0],403)
                self.assertEqual(call('GET',query,host='evil.example')[0],403)
                for invalid in ('/api/recap','/api/recap?archiveId=','/api/recap?archiveId=../active',query+'&archiveId='+identifier,query+'&extra=1'):
                    self.assertEqual(call('GET',invalid)[0],400)
                fixture['before']={'agent':get('/api/dj-agent'),'session':session.snapshot(),'detail':detail}
                baseline=copy.deepcopy(session.snapshot()['order']);lesson=detail['lessons'][0]
                request={'action':'settings','requestId':'keep','expected':detail['settingsExpected'],
                    'change':{'kind':'lesson','value':{'archiveId':identifier,'archiveRevision':detail['revision'],'lessonId':lesson['id'],'keep':True}}}
                self.assertEqual(call('POST','/api/dj-agent',request,token=False)[0],403)
                self.assertEqual(call('POST','/api/dj-agent',request,origin='https://evil.example')[0],403)
                kept=post(request);after=get(query)
                self.assertTrue(after['lessons'][0]['keep']);self.assertEqual(kept['settings']['receipt']['expected'],request['expected'])
                self.assertTrue(next(r for r in kept['settings']['houseRules'] if r['id']==lesson['ruleId'])['on'])
                fixture['keep']={'request':request,'response':kept,'session':session.snapshot(),'detail':after}
                bad=copy.deepcopy(request);bad['requestId']='stale';bad['change']['value']['keep']=False
                self.assertEqual(call('POST','/api/dj-agent',bad)[0],409);self.assertTrue(get(query)['lessons'][0]['keep'])
                dropped_request=copy.deepcopy(bad);dropped_request['requestId']='drop';dropped_request['expected']=kept['settings']['expected']
                dropped=post(dropped_request);after=get(query)
                self.assertFalse(after['lessons'][0]['keep'])
                fixture['drop']={'request':dropped_request,'response':dropped,'session':session.snapshot(),'detail':after}
                old=post(request);self.assertEqual(old['settings']['receipt'],kept['settings']['receipt'])
                self.assertEqual(old['settings']['revision'],dropped['settings']['revision']);self.assertFalse(get(query)['lessons'][0]['keep'])
                context={'archiveId':identifier,'archiveRevision':detail['revision']}
                settings=copy.deepcopy(agent.settings.state)
                # Native Recap sends no queueMode or music approval keys. Their
                # omission must route a factual question without music intent.
                native_question={'action':'message','source':'typed','messageId':'native-recap-question',
                    'text':'What records actually played?','surface':'recap','recapContext':context}
                native_answer=post(native_question)
                self.assertEqual(native_answer['messages'][-1]['id'],'native-recap-question-reply')
                self.assertEqual(native_answer['messages'][-1]['source'],'engine')
                self.assertIn('Track a — Fixture artist',native_answer['messages'][-1]['text'])
                self.assertNotIn('exact recordings',native_answer['messages'][-1]['text'])
                self.assertEqual(agent.remaining,0);self.assertIsNone(agent.playback_request)
                for source in ('typed','voice'):
                    voice=Mock();voice.status.return_value={'status':'idle'}
                    with patch.object(server,'LIVE_VOICE',voice):
                        question={'action':'message','source':source,'sessionId':'fixture-voice','messageId':'question-'+source,
                                  'text':'Should I start music and export clips of the crowd?','surface':'recap','recapContext':context,'approveRequest':True}
                        answer=post(question)
                        self.assertEqual(answer['messages'][-1]['id'],question['messageId']+'-reply')
                        self.assertIn('no run-bound mix recording',answer['messages'][-1]['text'])
                        if source=='voice':voice.require.assert_called_once_with('fixture-voice')
                self.assertEqual(agent.settings.state,settings)
                for context in (None,{'archiveId':identifier,'archiveRevision':999}):
                    self.assertEqual(call('POST','/api/dj-agent',{'action':'message','messageId':'bad-context','text':'start music',
                        'surface':'recap','recapContext':context,'approveRequest':True})[0],409)
                self.assertEqual(session.snapshot()['order'],baseline);self.assertEqual(agent.remaining,0)
                native.assert_not_called();provider.assert_not_called()
                destination=os.environ.get('AFTERHOURS_RECAP_FIXTURE')
                if destination:Path(destination).write_text(json.dumps(fixture,indent=2)+'\n')
        finally:
            httpd.shutdown();httpd.server_close();thread.join(2);store.close()


if __name__=='__main__':unittest.main()
