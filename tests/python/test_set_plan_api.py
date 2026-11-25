"""Actual Set HTTP routing, authentication and exact receipt behavior."""
import copy
import http.client
from http.server import ThreadingHTTPServer
import json
import threading
import unittest
from unittest.mock import Mock,patch

from afterhours import server
from afterhours.dj_agent import DJAgent
from afterhours.vdj_session import DJSession
from test_recording_availability import records
from test_set_plan import plan


class SetPlanHTTPTests(unittest.TestCase):
    def test_local_changes_are_authenticated_context_checked_and_set_surface_routes_without_music(self):
        blocked=Mock(side_effect=AssertionError('Unexpected native or provider work'))
        session=DJSession(threading.Lock(),client_factory=blocked);tracks=records()
        session.update(candidates=tracks,order=tracks[:3],phase='ready')
        agent=DJAgent(session,evaluator=blocked,clock=lambda:1000)
        agent.settings_action('plan',{'kind':'plan','value':plan()},agent.snapshot()['settings']['expected'])
        httpd=ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        thread=threading.Thread(target=httpd.serve_forever,daemon=True);thread.start()
        def call(body,token=True,origin=None):
            client=http.client.HTTPConnection('127.0.0.1',httpd.server_port,timeout=3)
            headers={'Content-Type':'application/json'}
            if token:headers['X-DJ-Token']=server.TOKEN
            if origin:headers['Origin']=origin
            client.request('POST','/api/dj-agent',body=json.dumps(body),headers=headers)
            response=client.getresponse();result=json.loads(response.read());status=response.status;client.close()
            return status,result
        try:
            with patch.object(server,'DJ_AGENT',agent):
                expected=agent.snapshot()['settings']['expected']
                warm={'action':'settings','requestId':'warm','expected':expected,'change':{'kind':'warmLength','value':3600}}
                self.assertEqual(call(warm,token=False)[0],403)
                self.assertEqual(call(warm,origin='https://example.test')[0],403)
                self.assertNotIn('warmLen',agent.settings.state['plan'])
                status,result=call(warm);self.assertEqual(status,200)
                self.assertEqual(result['setPlan']['schedule']['warmLen'],3600)
                self.assertEqual(result['settings']['receipt']['change'],warm['change'])
                self.assertEqual(result['settings']['expected'],result['setPlan']['expected'])
                self.assertEqual(call({**warm,'requestId':'stale','change':{'kind':'peakShift','value':900}})[0],409)
                self.assertEqual(agent.settings.state['plan']['peakAt'],9800)
                before_order=copy.deepcopy(session.snapshot()['order'])
                for source in ('typed','voice'):
                    voice=Mock();voice.status.return_value={'status':'idle'}
                    with patch.object(server,'LIVE_VOICE',voice):
                        status,result=call({'action':'message','source':source,'sessionId':'isolated-voice',
                            'messageId':'peak-'+source,'text':'peak later','surface':'set'})
                        self.assertEqual(status,200)
                        self.assertEqual(result['settings']['receipt']['change'],{'kind':'peakShift','value':900})
                        if source=='voice':voice.require.assert_called_once_with('isolated-voice')
                self.assertEqual(agent.settings.state['plan']['peakAt'],11600)
                self.assertEqual(session.snapshot()['order'],before_order)
                self.assertEqual(agent.remaining,0);blocked.assert_not_called()
        finally:
            httpd.shutdown();httpd.server_close();thread.join(2)


if __name__=='__main__':unittest.main()
