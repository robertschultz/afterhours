"""Exercise authenticated queue endpoints without transport or provider calls."""

from http.server import ThreadingHTTPServer
import json
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.request import Request, urlopen

from afterhours import server
from afterhours.vdj_session import DJSession
from test_queue_planning import track


class QueuePlanningHTTPTests(unittest.TestCase):
    def setUp(self):
        self.session = DJSession(threading.Lock(), client_factory=Mock(side_effect=AssertionError('No native connection')))
        self.addCleanup(self.session.crate.close)
        self.session.update(candidates=[track('a'), track('b'), track('x')])
        self.session.plan(['a','b'])
        session_patch = patch.object(server, 'DJ_SESSION', self.session)
        session_patch.start()
        self.addCleanup(session_patch.stop)
        self.httpd = ThreadingHTTPServer(('127.0.0.1',0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever,daemon=True)
        self.thread.start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.httpd.shutdown)

    def post(self, route, data):
        request = Request(f'http://127.0.0.1:{self.httpd.server_port}{route}', data=json.dumps(data).encode(),
                          headers={'Content-Type':'application/json','X-DJ-Token':server.TOKEN})
        with urlopen(request,timeout=3) as response:
            return json.load(response)

    def test_preview_keeps_state_intact_and_insert_applies_same_slot(self):
        before=self.session.snapshot()
        preview=self.post('/api/set-session', {'action':'preview-insertion','trackId':'x'})
        self.assertEqual(self.session.snapshot(),before)
        committed=self.post('/api/set-session', {'action':'insert-track','trackId':'x'})
        self.assertEqual(preview['insertion'],committed['insertion'])
        self.assertEqual([track['id'] for track in committed['order']],['a','x','b'])

    def test_explicit_fresh_start_routes_to_the_authorized_agent_action(self):
        with patch.object(server.DJ_AGENT,'message',return_value={'status':'pending'}) as planner:
            result=self.post('/api/dj-agent', {'action':'start-new-queue','messageId':'fresh','approveRequest':True})
        self.assertEqual(result['status'],'pending')
        self.assertEqual(planner.call_args.args[:5],('Start a fresh queue.','typed','fresh',True,'fresh-start'))
