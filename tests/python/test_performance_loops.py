import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

from afterhours.performance import Performance


class SavedLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        (self.root/'web/media/character-loops').mkdir(parents=True)
        (self.root/'web/media/character-loops/dj-one.mp4').write_bytes(b'fixture')
        (self.root/'.runtime').mkdir()
        self.profile={'id':'fixture-dj','name':'Fixture DJ','poster':'/agent-media/reference.jpg','prompt':'Fixture motion'}
        self.entry={'referencePoster':self.profile['poster'],'clip':'/media/character-loops/dj-one.mp4','duration':10,'loopBeats':24}
        self.save(self.entry)
        self.root_patch=patch('afterhours.performance.ROOT',self.root);self.root_patch.start()
        self.visual_patch=patch('afterhours.agent_profiles.AgentProfiles.visual',side_effect=lambda _:dict(self.profile));self.visual_patch.start()
        self.provider=Mock();self.performance=Performance(self.provider,1)

    def tearDown(self):
        self.visual_patch.stop();self.root_patch.stop();self.tmp.cleanup()

    def save(self,entry):
        (self.root/'.runtime/performer-loops.json').write_text(json.dumps({'version':1,'profiles':{'fixture-dj':entry}}))

    def select(self):
        return self.performance.command({'action':'profile','profileId':'fixture-dj'})

    def test_bundled_demo_rehearses_without_a_local_copy(self):
        self.assertFalse((self.root / 'web/media/nova-demo.mp4').exists())
        state = self.performance.command({'action': 'character', 'character': 'nova'})
        self.assertEqual(state['mode'], 'playing')
        self.assertEqual(state['segments'][0]['url'], '/media/nova-demo.mp4')
        self.provider.generate.assert_not_called()

    def test_selection_starts_only_the_saved_loop_and_preserves_display_settings(self):
        self.performance.command({'action':'settings','look':'mono','fit':'contain','beatSync':True})
        state=self.select()
        self.assertEqual(state['mode'],'playing')
        self.assertEqual(state['segments'][0]['url'],self.entry['clip'])
        self.assertEqual(state['clipDuration'],10)
        self.assertEqual(state['generation'],'rehearsal')
        self.assertEqual(state['look'],'mono');self.assertTrue(state['beatSync'])
        self.assertEqual(state['submissions'],0);self.provider.generate.assert_not_called()

    def test_updated_reference_clears_old_video_and_falls_back_to_its_still(self):
        self.select();self.profile['poster']='/agent-media/new-reference.jpg'
        state=self.select()
        self.assertEqual(state['mode'],'paused');self.assertEqual(state['segments'],[])
        self.assertEqual(state['poster'],self.profile['poster'])

    def test_invalid_or_missing_media_never_keeps_the_previous_clip(self):
        for change in ({'clip':'https://example.com/video.mp4'}, {'clip':'/media/character-loops/../other.mp4'},
                       {'clip':'/media/character-loops/missing.mp4'}, {'duration':float('nan')},
                       {'duration':True}, {'duration':31}, {'loopBeats':True}, {'referencePoster':'other'}):
            with self.subTest(change=change):
                self.save(self.entry);self.select();self.save({**self.entry,**change})
                self.assertEqual(self.select()['segments'],[])
        (self.root/'.runtime/performer-loops.json').write_text('{broken')
        self.assertEqual(self.select()['segments'],[])
        self.provider.generate.assert_not_called()

    def test_live_lead_prepares_the_same_fallback_without_a_provider_call(self):
        state=self.performance.assign_live_lead(self.profile)
        self.assertEqual(state['character'],self.profile['id'])
        self.assertEqual(state['segments'][0]['url'],self.entry['clip'])
        self.assertEqual(state['submissions'],0);self.provider.generate.assert_not_called()

    def test_live_lead_cannot_resume_a_paused_continuation_worker(self):
        before=self.performance.snapshot()
        self.performance.worker=Mock();self.performance.worker.is_alive.return_value=True
        with self.assertRaisesRegex(ValueError,'Stop generation'):
            self.performance.assign_live_lead(self.profile)
        self.assertEqual(self.performance.snapshot()['session'],before['session'])
        self.assertEqual(self.performance.snapshot()['mode'],'paused')
        self.provider.generate.assert_not_called()

    def test_director_reference_uses_selected_custom_and_revised_builtin_images(self):
        from afterhours.performance_server import reference_file
        base=self.root/'web/agent-media';base.mkdir()
        for character,marker in [('fixture-dj','a'),('nova','b')]:
            path=base/(marker*32+'.jpg');path.write_bytes(character.encode())
            profile={**self.profile,'id':character,'poster':'/agent-media/'+path.name}
            self.performance.assign_live_lead(profile)
            with patch('afterhours.performance_server.ROOT',self.root),patch('afterhours.performance_server.WEB',self.root/'web'):
                self.assertEqual(reference_file(self.performance.snapshot()['poster']).read_bytes(),character.encode())
                with self.assertRaises(ValueError):reference_file('/assets/../secret.jpg')

    def test_loop_cannot_escape_the_media_directory_via_symlink(self):
        (self.root/'outside.mp4').write_bytes(b'fixture')
        (self.root/'web/media/character-loops/escape.mp4').symlink_to(self.root/'outside.mp4')
        self.save({**self.entry,'clip':'/media/character-loops/escape.mp4'})
        self.assertEqual(self.select()['segments'],[])


if __name__=='__main__':unittest.main()
