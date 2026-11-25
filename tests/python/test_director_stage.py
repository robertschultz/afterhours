"""Local reviewed-frame admission. Synthetic JPEGs, no provider/native I/O."""
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock,patch
from urllib.error import HTTPError
from urllib.request import Request,urlopen

from PIL import Image

from afterhours.agent_profiles import AgentProfiles
from afterhours.director import DirectorGate
from afterhours.director_stage import DirectorStages
from afterhours.performance_server import make_server
from afterhours import performance_server


class StageFixture:
    def fixture(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.profiles=AgentProfiles(self.root)
        self.db=self.profiles.seed();self.profiles.path.parent.mkdir(parents=True)
        self.profiles.path.write_text(json.dumps(self.db))
        self.stages=DirectorStages(self.profiles,self.root)
        self.entries={}
        self.gate=DirectorGate('synthetic-stage-test-key',1,60,now=lambda:100)
        self.uploads=[]
        def upload(key,content):
            self.uploads.append((key,hashlib.sha256(content).hexdigest()))
            self.gate.references[key]='https://fixture.fal.media/'+key+'.jpg'
        self.gate.prepare_reference=Mock(side_effect=upload)
        self.gate.upstream=Mock(side_effect=AssertionError('No provider calls'))
        self.asset('light');self.asset('dark')

    def asset(self,appearance):
        data=io.BytesIO();Image.new('RGB',(128,128),'white' if appearance=='light' else (24,24,27)).save(data,format='JPEG')
        content=data.getvalue();digest=hashlib.sha256(content).hexdigest()
        poster='/director-stage/'+digest+'.jpg';path=self.root/'web'/poster.lstrip('/')
        path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(content)
        self.entries[appearance]={'profileRevision':1,'sourceSha256':digest,'poster':poster,
            'width':128,'height':128,'aspectRatio':'1:1','background':'#FFFFFF' if appearance=='light' else '#18181B',
            'scene':'Fixed full head, torso, arms and hands above a thin floating controller. No table.','reviewed':True}
        self.write_manifest()

    def write_manifest(self):
        self.stages.path.write_text(json.dumps({'version':1,'profiles':{'nova':self.entries}}))

    def reference(self,appearance='light'):
        return self.stages.resolve('nova',appearance)[0]


class StageTests(StageFixture,unittest.TestCase):
    def setUp(self):self.fixture()

    def test_preflight_validates_without_upload_admission_or_image_mutation(self):
        before={p:p.read_bytes() for p in (self.root/'web/director-stage').iterdir()}
        result=self.stages.preflight('nova','light')
        self.assertTrue(result['ready']);reference=result['stageReference']
        self.assertRegex(reference['referenceKey'],r'^stage-[a-f0-9]{64}$')
        self.assertEqual(reference['appearance'],'light');self.assertEqual(reference['aspectRatio'],'1:1')
        self.assertEqual(before,{p:p.read_bytes() for p in before})
        self.gate.prepare_reference.assert_not_called();self.assertEqual(self.gate.used,0)

    def test_missing_unreviewed_or_portrait_frame_never_silently_falls_back(self):
        original=copy.deepcopy(self.entries)
        for change in (None,{'reviewed':False},{'width':1086,'height':1448,'aspectRatio':'16:9'}):
            self.entries=copy.deepcopy(original)
            if change is None:del self.entries['light']
            else:self.entries['light'].update(change)
            self.write_manifest();result=self.stages.preflight('nova','light')
            self.assertFalse(result['ready']);self.assertIsNone(result['stageReference'])
            self.assertTrue(result['message'])
        self.gate.prepare_reference.assert_not_called();self.assertEqual(self.gate.used,0)

    def test_reviewed_stages_survive_selection_and_default_changes(self):
        # Both DJs have a previously approved still and independently reviewed
        # stage metadata. Selecting either must not edit their identities.
        with self.profiles.transaction(True) as db:
            for profile in db['profiles']:
                profile['frames']=[{'id':'fixture-frame','url':self.entries['light']['poster']}]
                profile['approvedFrame']='fixture-frame'
        self.stages.path.write_text(json.dumps({'version':1,'profiles':{
            identifier:copy.deepcopy(self.entries) for identifier in ('nova','rivet')}}))
        originals={identifier:self.profiles.get(identifier) for identifier in ('nova','rivet')}
        references={(identifier,appearance):self.stages.resolve(identifier,appearance)[0]
                    for identifier in originals for appearance in ('light','dark')}
        reference=references['nova','light']
        self.stages.prepare(self.gate,'nova','light',reference['referenceKey'])
        for identifier in ('rivet','nova','nova'):
            for action in ('assign','default'):
                with self.subTest(identifier=identifier,action=action):
                    self.assertEqual(self.profiles.mutate(action,{'id':identifier}),originals[identifier])
                    roster=AgentProfiles(self.root).snapshot()
                    self.assertEqual(roster['assigned' if action=='assign' else 'default'],identifier)
                    self.assertEqual(roster['assignedRevision'],originals[roster['assigned']]['revision'])
                    for (character,appearance),expected in references.items():
                        self.assertEqual(self.stages.preflight(character,appearance),
                                         {'ready':True,'stageReference':expected})
        self.assertEqual(self.gate.start('nova',stage_reference=reference)['stageReference'],reference)
        self.assertEqual(self.gate.prepare_reference.call_count,1);self.gate.upstream.assert_not_called()

    def test_real_profile_edit_still_invalidates_reviewed_stage(self):
        profile=self.profiles.get('nova');profile['wardrobe']='A different jacket.'
        updated=self.profiles.mutate('save',{'id':'nova','revision':profile['revision'],'profile':profile})
        self.assertEqual(updated['revision'],profile['revision']+1)
        for appearance in ('light','dark'):
            result=self.stages.preflight('nova',appearance)
            self.assertFalse(result['ready']);self.assertIsNone(result['stageReference'])
            self.assertIn('DJ changed',result['message'])
            with self.assertRaisesRegex(ValueError,'DJ changed'):
                self.stages.prepare(self.gate,'nova',appearance,'stage-'+'0'*64)
        self.gate.prepare_reference.assert_not_called();self.assertEqual(self.gate.used,0)

    def test_stale_profile_bytes_dimensions_and_unapproved_paths_fail_before_upload(self):
        original=copy.deepcopy(self.entries)
        patches=[{'profileRevision':2},{'width':256,'height':256},{'aspectRatio':'3:4'},
                 {'background':'#18181B'},{'poster':'/agent-media/private.jpg'},
                 {'sourceSha256':'0'*64,'poster':'/director-stage/'+'0'*64+'.jpg'},
                 {'scene':'x'*1001},{'width':True},{'profileRevision':True}]
        for change in patches:
            with self.subTest(change=change):
                self.entries=copy.deepcopy(original);self.entries['light'].update(change);self.write_manifest()
                with self.assertRaises(ValueError):self.stages.prepare(self.gate,'nova','light','stage-'+'0'*64)
        self.entries=original;self.write_manifest()
        path=self.root/'web'/self.entries['light']['poster'].lstrip('/');path.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'changed after review'):self.reference()
        self.gate.prepare_reference.assert_not_called();self.assertEqual(self.gate.used,0)

    def test_symlink_outside_stage_directory_is_not_read_or_uploaded(self):
        path=self.root/'web'/self.entries['light']['poster'].lstrip('/')
        outside=self.root/'private.jpg';path.rename(outside);path.symlink_to(outside)
        with self.assertRaisesRegex(ValueError,'path is invalid'):self.reference()
        self.gate.prepare_reference.assert_not_called()

    def test_hash_matching_truncated_jpeg_is_rejected_before_upload(self):
        old=self.root/'web'/self.entries['light']['poster'].lstrip('/')
        content=old.read_bytes()[:-8];digest=hashlib.sha256(content).hexdigest()
        poster='/director-stage/'+digest+'.jpg';(self.root/'web'/poster.lstrip('/')).write_bytes(content)
        self.entries['light'].update(sourceSha256=digest,poster=poster);self.write_manifest()
        with self.assertRaisesRegex(ValueError,'could not be read'):
            self.stages.prepare(self.gate,'nova','light','stage-'+'0'*64)
        self.gate.prepare_reference.assert_not_called()

    def test_light_and_dark_cache_and_admission_are_bound_to_exact_metadata(self):
        refs={appearance:self.reference(appearance) for appearance in ('light','dark')}
        self.assertNotEqual(refs['light']['referenceKey'],refs['dark']['referenceKey'])
        self.gate.references['nova']='https://fixture.fal.media/legacy.jpg'
        with self.assertRaises(ValueError):self.gate.start('nova',mode='live',consent=True,minutes=2,stage_reference=refs['light'])
        self.assertEqual(self.gate.used,0)
        for appearance,reference in refs.items():
            self.stages.prepare(self.gate,'nova',appearance,reference['referenceKey'])
        admission=self.gate.start('nova',mode='live',consent=True,minutes=2,stage_reference=refs['dark'])
        self.assertEqual(admission['stageReference'],refs['dark'])
        self.assertEqual(admission['imageUrl'],self.gate.references[refs['dark']['referenceKey']])
        self.assertEqual(admission['generatedLimitSeconds'],105);self.assertEqual(self.gate.used,1)
        self.assertEqual(self.gate.snapshot()['stageReference'],refs['dark'])
        admission['stageReference']['scene']='caller mutation'
        self.assertEqual(self.gate.snapshot()['stageReference']['scene'],refs['dark']['scene'])
        self.gate.upstream.assert_not_called();self.gate.stop()
        self.assertIsNone(self.gate.snapshot()['stageReference'])

    def test_stale_preview_and_profile_changed_during_upload_cannot_be_prepared(self):
        reference=self.reference()
        with self.assertRaisesRegex(ValueError,'since preview'):
            self.stages.prepare(self.gate,'nova','light',self.reference('dark')['referenceKey'])
        self.gate.prepare_reference.assert_not_called()
        original_upload=self.gate.prepare_reference.side_effect
        def changed(key,content):
            original_upload(key,content)
            self.db['profiles'][0]['revision']=2;self.profiles.path.write_text(json.dumps(self.db))
        self.gate.prepare_reference.side_effect=changed
        with self.assertRaisesRegex(ValueError,'DJ changed'):
            self.stages.prepare(self.gate,'nova','light',reference['referenceKey'])
        self.assertEqual(self.gate.stage_references,{});self.assertEqual(self.gate.used,0)

    def test_legacy_single_reference_is_explicitly_unbound_and_cast_stays_separate(self):
        self.gate.references['rivet']='https://fixture.fal.media/rivet.jpg'
        admission=self.gate.start('rivet')
        self.assertIsNone(admission['stageReference']);self.assertIn('imageUrl',admission)
        self.gate.stop();reference=self.reference()
        self.stages.prepare(self.gate,'nova','light',reference['referenceKey'])
        with self.assertRaisesRegex(ValueError,'exact reviewed'):
            self.gate.start('nova',mode='live',consent=True,minutes=2,cast=[{},{}],stage_reference=reference)
        self.assertEqual(self.gate.used,1)


class StageHTTPTests(StageFixture,unittest.TestCase):
    def setUp(self):
        self.fixture()
        web_patch=patch.object(performance_server,'WEB',self.root/'web');web_patch.start();self.addCleanup(web_patch.stop)
        self.server=make_server(0,director=self.gate,profiles=self.profiles,stages=self.stages)
        self.server.performance.state['character']='nova'
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.base=f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join()

    def post(self,path,body):
        with urlopen(Request(self.base+path,method='POST',data=json.dumps(body).encode(),headers={
                'Content-Type':'application/json','X-Performance-Token':self.server.token}),timeout=2) as response:
            return json.load(response)

    def test_full_preflight_prepare_start_flow_and_stale_theme_are_guarded(self):
        with urlopen(self.base+'/api/director/stage?character=nova&appearance=light',timeout=2) as response:
            reference=json.load(response)['stageReference']
        with urlopen(self.base+reference['poster'],timeout=2) as response:
            self.assertEqual(response.headers['Content-Type'],'image/jpeg')
            self.assertEqual(hashlib.sha256(response.read()).hexdigest(),reference['sourceSha256'])
        payload={'character':'nova','appearance':'light','expectedReferenceKey':reference['referenceKey'],'consent':True}
        start={**payload,'expectedCharacter':'nova','profile':'director-live-v1','minutes':2}
        with self.assertRaises(HTTPError):self.post('/api/director/start',start)
        self.assertEqual(self.gate.used,0);self.gate.prepare_reference.assert_not_called()
        result=self.post('/api/director/reference',payload);self.assertEqual(result['stageReference'],reference)
        with self.assertRaises(HTTPError):self.post('/api/director/start',{**start,'appearance':'dark'})
        self.assertEqual(self.gate.used,0)
        admitted=self.post('/api/director/start',start)
        self.assertEqual(admitted['stageReference'],reference);self.assertEqual(self.gate.used,1)
        with self.assertRaises(HTTPError):self.post('/api/director/start',start)
        self.assertEqual(self.gate.used,1);self.gate.upstream.assert_not_called()

    def test_missing_stage_or_changed_selection_never_uploads_or_uses_admission(self):
        reference=self.reference();del self.entries['dark'];self.write_manifest()
        with urlopen(self.base+'/api/director/stage?character=nova&appearance=dark',timeout=2) as response:
            result=json.load(response)
        self.assertFalse(result['ready']);self.assertIn('review',result['message'])
        payload={'character':'nova','appearance':'light','expectedReferenceKey':reference['referenceKey'],'consent':True}
        self.server.performance.state['character']='rivet'
        with urlopen(self.base+'/api/director/stage?character=nova&appearance=light',timeout=2) as response:
            self.assertFalse(json.load(response)['ready'])
        with self.assertRaises(HTTPError):self.post('/api/director/reference',payload)
        with self.assertRaises(HTTPError):self.post('/api/director/start',{**payload,'profile':'director-live-v1','minutes':2,'expectedCharacter':'nova'})
        self.gate.prepare_reference.assert_not_called();self.assertEqual(self.gate.used,0)

    def test_file_changed_after_preparation_cannot_start_using_cached_url(self):
        reference=self.reference();payload={'character':'nova','appearance':'light','expectedReferenceKey':reference['referenceKey'],'consent':True}
        self.post('/api/director/reference',payload)
        (self.root/'web'/reference['poster'].lstrip('/')).write_bytes(b'changed after upload')
        with self.assertRaises(HTTPError):self.post('/api/director/start',{**payload,'profile':'director-live-v1','minutes':2,'expectedCharacter':'nova'})
        self.assertEqual(self.gate.used,0);self.assertIsNone(self.gate.lease)
        self.assertEqual(self.gate.prepare_reference.call_count,1);self.gate.upstream.assert_not_called()


if __name__=='__main__':unittest.main()
