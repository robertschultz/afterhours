"""Local persistence and character generation contracts; provider is simulated."""
import base64,copy,io,json,tempfile,threading,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from PIL import Image
from afterhours.agent_profiles import AgentProfiles,clean_profile
from afterhours.character_board import CharacterBoards,board_prompt
from afterhours.director import DirectorGate
from afterhours.performance import Performance,CHARACTERS

def photo():
    out=io.BytesIO();Image.new('RGB',(400,300),(80,90,100)).save(out,'PNG');return out.getvalue()

class ProfilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);(self.root/'web').mkdir();self.profiles=AgentProfiles(self.root)
        self.p=self.profiles.mutate('create',{})
    def tearDown(self):self.tmp.cleanup()
    def upload(self,role='photo'):
        return self.profiles.upload(self.p['id'],base64.b64encode(photo()).decode(),'original.png',role)
    def test_originals_are_byte_identical_and_public_copy_is_decoded_jpeg(self):
        a=self.upload();original=self.root/'.runtime/agent-originals'/a['original']
        self.assertEqual(original.read_bytes(),photo());self.assertEqual(original.stat().st_mode&0o777,0o600)
        content=self.profiles.image_bytes(a['url']);self.assertEqual(Image.open(io.BytesIO(content)).format,'JPEG')
        self.assertFalse(Image.open(io.BytesIO(content)).getexif());self.assertIsNone(self.profiles.visual(self.p['id'])['poster'])
        self.assertEqual(AgentProfiles(self.root).get(self.p['id'])['photos'][0]['id'],a['id'])
    def test_upload_limits_and_path_traversal_are_rejected(self):
        for _ in range(4):self.upload()
        with self.assertRaisesRegex(ValueError,'four'):self.upload()
        for url in ['/../.env.local','/agent-media/../.env.local','https://example.com/img.jpg']:
            with self.assertRaises(ValueError):self.profiles.image_bytes(url)
        for raw in [b'not an image',b'<svg></svg>']:
            with self.assertRaises(ValueError):self.profiles.add_image(self.p['id'],raw,'bad')
    def test_iphone_mpo_uses_primary_image_and_retains_original(self):
        out=io.BytesIO()
        Image.new('RGB',(400,300),(80,90,100)).save(out,'MPO',save_all=True,append_images=[Image.new('RGB',(200,150),'white')])
        original=out.getvalue()
        item=self.profiles.add_image(self.p['id'],original,'iPhone body photo.jpg',original=True)
        public=Image.open(io.BytesIO(self.profiles.image_bytes(item['url'])))
        self.assertEqual(public.format,'JPEG');self.assertEqual(public.size,(400,300))
        self.assertEqual((self.root/'.runtime/agent-originals'/item['original']).read_bytes(),original)
    def test_edit_conflicts_cannot_overwrite_new_assets(self):
        stale=copy.deepcopy(self.p);self.upload()
        with self.assertRaisesRegex(ValueError,'changed'):self.profiles.mutate('save',{'id':self.p['id'],'revision':stale['revision'],'profile':{**stale,'name':'Lost update'}})
        p=self.profiles.get(self.p['id']);p['name']='DJ Sol';p['genres']=['Deep house'];p['bpmMin']=121
        self.profiles.mutate('save',{'id':p['id'],'revision':p['revision'],'profile':p})
        self.assertEqual(self.profiles.get(p['id'])['name'],'DJ Sol')
        self.assertEqual(len(self.profiles.get(p['id'])['photos']),1)
    def test_approval_is_explicit_versions_remain_and_context_contains_no_photos(self):
        a=self.upload('frame');b=self.upload('frame');self.assertIsNone(self.profiles.visual(self.p['id'])['poster'])
        self.profiles.mutate('approve',{'id':self.p['id'],'kind':'frame','assetId':a['id']})
        self.assertEqual(self.profiles.visual(self.p['id'])['poster'],a['url'])
        self.profiles.mutate('assign',{'id':self.p['id']});context=self.profiles.context()
        self.assertEqual(context['id'],self.p['id']);self.assertNotIn('photos',context);self.assertNotIn('poster',context)
        clone=self.profiles.mutate('duplicate',{'id':self.p['id']})
        self.assertNotEqual(clone['id'],self.p['id']);self.assertEqual(len(clone['frames']),2);self.assertEqual(clone['approvedFrame'],a['id'])
        self.assertEqual(clone['generationAttempts'],[])
    def test_new_agent_is_blank_even_when_selection_id_is_sent(self):
        self.upload('frame')
        p=self.profiles.mutate('create',{'id':self.p['id']})
        self.assertEqual(p['name'],'New DJ');self.assertEqual(p['photos'],[]);self.assertEqual(p['frames'],[]);self.assertIsNone(p['approvedFrame'])
    def test_invalid_taste_settings_do_not_persist(self):
        for changes in [{'bpmMin':150,'bpmMax':120},{'voice':'cloned-voice'},{'hype':11},{'genres':['House']*11}]:
            with self.assertRaises(ValueError):clean_profile({**self.p,**changes})
    def test_assigned_profile_uses_reviewed_image_without_starting_video(self):
        a=self.upload('frame');self.profiles.mutate('approve',{'id':self.p['id'],'kind':'frame','assetId':a['id']})
        visual=self.profiles.visual(self.p['id']);performance=Performance()
        with patch('afterhours.agent_profiles.AgentProfiles',return_value=self.profiles):
            result=performance.command({'action':'profile','profile':visual})
            self.assertEqual(result['character'],self.p['id']);self.assertEqual(result['poster'],a['url']);self.assertEqual(result['mode'],'paused');self.assertEqual(result['segments'],[])
            with self.assertRaisesRegex(ValueError,'still reference'):performance.command({'action':'rehearse'})
        CHARACTERS.pop(self.p['id'],None)
    def test_generation_requires_consent_photos_key_and_board_for_frame(self):
        gate=DirectorGate();boards=CharacterBoards(self.profiles,gate)
        args={'id':self.p['id'],'kind':'board','requestId':'a-request-id-12345','revision':self.p['revision'],'consent':True}
        with patch.object(boards,'run') as run:
            for data in [{**args,'consent':False},args]:
                with self.assertRaises(ValueError):boards.start(data)
            gate.set_key('test-not-a-real-key')
            with self.assertRaisesRegex(ValueError,'photo'):boards.start(args)
            self.upload();args['revision']=self.profiles.get(self.p['id'])['revision']
            with self.assertRaisesRegex(ValueError,'board'):boards.start({**args,'kind':'frame'})
            run.assert_not_called()

    def test_profile_id_resolves_current_approved_frame_without_prompt_version_coupling(self):
        frame=self.upload('frame')
        self.profiles.mutate('approve',{'id':self.p['id'],'kind':'frame','assetId':frame['id']})
        old=self.profiles.visual(self.p['id']);old['prompt']='Prompt from an earlier music service.'
        performance=Performance()
        try:
            with patch('afterhours.agent_profiles.AgentProfiles',return_value=self.profiles):
                with self.assertRaisesRegex(ValueError,'profile changed'):
                    performance.command({'action':'profile','profile':old})
                result=performance.command({'action':'profile','profileId':self.p['id']})
                self.assertEqual(result['poster'],frame['url'])
                self.assertEqual(result['prompt'],self.profiles.visual(self.p['id'])['prompt'])
                self.assertEqual(result['mode'],'paused');self.assertEqual(result['submissions'],0)
                with self.assertRaises(ValueError):
                    performance.command({'action':'profile','profileId':'missing'})
                with self.assertRaises(ValueError):
                    performance.command({'action':'profile','profileId':self.p['id'],'profile':old})
        finally:CHARACTERS.pop(self.p['id'],None)
    def test_one_submit_then_read_result_and_explicit_review(self):
        self.upload();p=self.profiles.get(self.p['id']);gate=DirectorGate('test-not-a-real-key');boards=CharacterBoards(self.profiles,gate)
        data=photo();response=Mock();response.__enter__=Mock(return_value=response);response.__exit__=Mock();response.read.return_value=data;gate.opener=Mock();gate.opener.open.return_value=response
        replies=[{'request_id':'provider-123','status_url':'https://queue.fal.run/model/requests/123/status','response_url':'https://queue.fal.run/model/requests/123'}, {'status':'COMPLETED'}, {'images':[{'url':'https://v3.fal.media/files/image.jpg'}]}]
        args={'id':p['id'],'kind':'board','requestId':'a-request-id-12345','revision':p['revision'],'consent':True}
        with patch.object(boards,'read',side_effect=replies) as read:
            boards.start(args)
            for worker in list(boards.workers.values()):worker.join(2)
            self.assertEqual(read.call_count,3);payload=read.call_args_list[0].args[2]
            self.assertEqual(payload['num_images'],1);self.assertEqual(payload['aspect_ratio'],'3:2');self.assertTrue(payload['image_urls'][0].startswith('data:image/jpeg;'))
            self.assertEqual(self.profiles.get(p['id'])['generation']['status'],'review');self.assertIsNone(self.profiles.get(p['id'])['approvedBoard'])
            with self.assertRaisesRegex(ValueError,'already submitted'):boards.start(args)
        self.assertEqual(len(self.profiles.get(p['id'])['boards']),1)
    def test_uncertain_submission_never_retries_and_restart_marks_interrupted(self):
        self.upload();p=self.profiles.get(self.p['id']);boards=CharacterBoards(self.profiles,DirectorGate('test-not-a-real-key'))
        args={'id':p['id'],'kind':'board','requestId':'a-request-id-12345','revision':p['revision'],'consent':True}
        with patch.object(boards,'read',side_effect=TimeoutError()) as read:
            boards.start(args)
            for worker in list(boards.workers.values()):worker.join(2)
            self.assertEqual(read.call_count,1);self.assertEqual(self.profiles.get(p['id'])['generation']['status'],'error')
        with self.profiles.transaction(True) as db:
            next(t for t in db['profiles'] if t['id']==p['id'])['generation']['status']='queued'
        CharacterBoards(self.profiles,DirectorGate());self.assertEqual(self.profiles.get(p['id'])['generation']['status'],'interrupted')
    def test_same_identity_board_and_single_scene_are_distinct(self):
        self.assertIn('six equal photographic panels',board_prompt(self.p,'board'))
        self.assertIn('no collage',board_prompt(self.p,'frame'))

if __name__=='__main__':unittest.main()

class ProfileContextTests(unittest.TestCase):
    def test_assigned_taste_is_sent_with_next_decision(self):
        from afterhours.dj_agent import DJAgent
        from test_dj_agent import Session,decision,settled
        evaluator=Mock(return_value=(decision(),{'responseId':'profile-fixture'}));agent=DJAgent(Session(),evaluator=evaluator)
        agent.profile_context=lambda:{'id':'sample','name':'Sample DJ','genres':['Disco'],'bpmMin':118,'bpmMax':126}
        agent.message('Something warm',message_id='profile-test',approved=True);settled(agent)
        self.assertEqual(evaluator.call_args.args[0]['djProfile']['name'],'Sample DJ')
        self.assertIn('protected tracks',evaluator.call_args.args[0]['profilePolicy'])
