"""Isolated loopback command boundary, no real browser or VirtualDJ execution."""
import http.client
import json
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from afterhours import server

class SetPlaybackHTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd=ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        cls.thread=threading.Thread(target=cls.httpd.serve_forever,daemon=True)
        cls.thread.start()
    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown();cls.httpd.server_close();cls.thread.join(2)
    def setUp(self):
        server.STATE.clear();server.STATE.update(sessionId='test-session',audioEnabled=True,capabilities=['set-run-v1'],library=[{'id':'a'},{'id':'b'}])
        server.OWNER.update(id='test-owner',seen=time.time());server.COMMANDS.clear();server.SEQ=0
        self.command=dict(action='play-draft',requestId='request-first',sessionId='test-session',trackIds=['b','a'])
    def call(self,data,origin=None):
        connection=http.client.HTTPConnection('127.0.0.1',self.httpd.server_port,timeout=3)
        headers={'Content-Type':'application/json','X-DJ-Token':server.TOKEN}
        if origin:headers['Origin']=origin
        connection.request('POST','/api/command',json.dumps(data),headers)
        response=connection.getresponse();body=json.loads(response.read());connection.close()
        return response.status,body
    def test_valid_order_is_queued_not_claimed_executed(self):
        status,body=self.call(self.command)
        self.assertEqual(status,202);self.assertTrue(body['accepted']);self.assertNotIn('executed',body)
        self.assertEqual(server.COMMANDS[0]['trackIds'],['b','a'])
    def test_invalid_or_catalog_ids_never_enter_queue(self):
        for ids in ([],['a','a'],['catalog-only'],[{'id':'a'}],list(map(str,range(14)))):
            status,_=self.call(dict(self.command,trackIds=ids))
            self.assertIn(status,(400,409));self.assertEqual(server.COMMANDS,[])
    def test_owner_reload_and_disabled_audio_block_execution(self):
        self.assertEqual(self.call(dict(self.command,sessionId='old-session'))[0],409)
        server.STATE['audioEnabled']=False
        self.assertEqual(self.call(self.command)[0],409)
        self.assertEqual(server.COMMANDS,[])
    def test_duplicate_submission_does_not_restart_set(self):
        self.assertEqual(self.call(self.command)[0],202)
        self.assertEqual(self.call(self.command)[0],409)
        self.assertEqual(len(server.COMMANDS),1)
    def test_takeover_is_bound_to_the_current_audio_owner(self):
        data=dict(action='takeover',sessionId='test-session',requestId='takeover-123')
        self.assertEqual(self.call(dict(data,sessionId='old-session'))[0],409)
        self.assertEqual(self.call(data)[0],202)
        self.assertEqual(server.COMMANDS[0]['action'],'takeover')
    def test_foreign_origin_and_expired_owner_cannot_queue(self):
        self.assertEqual(self.call(self.command,'https://example.invalid')[0],403)
        server.OWNER['seen']=time.time()-10
        self.assertEqual(self.call(self.command)[0],409)
        self.assertEqual(server.COMMANDS,[])

if __name__=='__main__':unittest.main()
