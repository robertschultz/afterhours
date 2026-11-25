"""Real local HTTP API, temporary roster and fake video handoff. No provider."""
import http.client,json,threading,unittest,tempfile,base64
from pathlib import Path
from unittest.mock import patch,Mock
from http.server import ThreadingHTTPServer
from afterhours import server
from afterhours.agent_profiles import AgentProfiles
from test_agent_profiles import photo
class AgentsHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();root=Path(self.temp.name);(root/'web').mkdir();self.profiles=AgentProfiles(root)
        self.voice=Mock();self.voice.status.return_value={'status':'idle'};self.agent=Mock();self.agent.snapshot.return_value={'evaluating':False};self.session=Mock()
        self.patches=[patch.object(server,'PROFILES',self.profiles),patch.object(server,'LIVE_VOICE',self.voice),patch.object(server,'DJ_AGENT',self.agent),patch.object(server,'DJ_SESSION',self.session),patch.object(server.Handler,'agent_video_request',return_value={'ok':True})]
        self.video=self.patches[-1].start()
        for p in self.patches[:-1]:p.start()
        self.httpd=ThreadingHTTPServer(('127.0.0.1',0),server.Handler);self.thread=threading.Thread(target=self.httpd.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):
        self.httpd.shutdown();self.httpd.server_close();self.thread.join(2)
        for p in self.patches:p.stop()
        self.temp.cleanup()
    def call(self,path,data=None,auth=True,origin=None):
        c=http.client.HTTPConnection('127.0.0.1',self.httpd.server_port,timeout=3);headers={'Content-Type':'application/json'}
        if auth:headers['X-DJ-Token']=server.TOKEN
        if origin:headers['Origin']=origin
        c.request('GET' if data is None else 'POST',path,body=json.dumps(data) if data is not None else None,headers=headers);r=c.getresponse();status=r.status;result=json.loads(r.read());c.close();return status,result
    def test_create_upload_review_assign_and_conflict(self):
        _,roster=self.call('/api/agents',{'action':'create','id':'nova'});p=next(p for p in roster['profiles'] if p['id']==roster['selected']);self.assertEqual(p['name'],'New DJ')
        self.assertEqual(self.call('/api/agents',{'action':'assign','id':p['id']})[0],409);self.video.assert_not_called()
        status,roster=self.call('/api/agents/photo',{'id':p['id'],'role':'frame','name':'test.png','content':base64.b64encode(photo()).decode()});self.assertEqual(status,200)
        saved=self.profiles.get(p['id']);self.assertIsNone(saved['approvedFrame'])
        self.assertEqual(self.call('/api/agents',{'action':'save','id':p['id'],'revision':p['revision'],'profile':p})[0],409)
        self.call('/api/agents',{'action':'approve','id':p['id'],'kind':'frame','assetId':saved['frames'][0]['id']})
        reviewed=self.profiles.get(p['id'])
        status,roster=self.call('/api/agents',{'action':'assign','id':p['id']});self.assertEqual(status,200);self.assertEqual(roster['assigned'],p['id'])
        self.video.assert_called_once();payload=self.video.call_args.args[1];self.assertEqual(payload['action'],'profile');self.assertEqual(payload['profileId'],p['id'])
        self.assertEqual(roster['assignedRevision'],next(t for t in roster['profiles'] if t['id']==p['id'])['revision'])
        self.assertEqual(self.profiles.get(p['id']),reviewed)
    def test_foreign_origin_missing_token_and_active_talk_block_assignment(self):
        self.assertEqual(self.call('/api/agents',auth=False)[0],403)
        self.assertEqual(self.call('/api/agents',{'action':'create'},origin='https://example.com')[0],403)
        self.voice.status.return_value={'status':'active'}
        self.assertEqual(self.call('/api/agents',{'action':'assign','id':'nova'})[0],409);self.video.assert_not_called()
