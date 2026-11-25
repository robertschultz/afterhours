"""Local reference selection and scene composition. No image provider calls."""
import copy
import unittest

from afterhours.agent_profiles import base_profile
from afterhours.character_board import board_prompt, reference_inputs
import test_agent_profiles as fixtures


class ReferenceRoleTests(unittest.TestCase):
    setUp=fixtures.ProfilesTests.setUp
    tearDown=fixtures.ProfilesTests.tearDown
    upload=fixtures.ProfilesTests.upload
    def test_master_and_body_roles_are_local_explicit_and_revision_bound(self):
        body = self.upload()
        master = self.upload()
        p = self.profiles.get(self.p['id'])
        saved = self.profiles.mutate('reference-roles', {
            'id': p['id'], 'revision': p['revision'],
            'masterPhotoId': master['id'], 'bodyPhotoIds': [body['id']]})
        self.assertEqual(saved['referenceRoles'], {'master': master['id'], 'body': [body['id']]})
        self.assertEqual(reference_inputs(saved, 'board')[0]['id'], master['id'])
        self.assertIsNone(saved['approvedBoard'])
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.profiles.mutate('reference-roles', {'id': p['id'], 'revision': p['revision'],
                'masterPhotoId': body['id'], 'bodyPhotoIds': []})
        self.profiles.mutate('remove-photo', {'id': p['id'], 'photoId': body['id']})
        self.assertEqual(self.profiles.get(p['id'])['referenceRoles']['body'], [])
        self.profiles.mutate('remove-photo', {'id': p['id'], 'photoId': master['id']})
        self.assertEqual(self.profiles.get(p['id'])['referenceRoles'], {'master': None, 'body': []})

    def test_roles_cannot_point_to_another_profile_or_relabel_master_as_body(self):
        master = self.upload()
        p = self.profiles.get(self.p['id'])
        for values in [dict(masterPhotoId='missing', bodyPhotoIds=[]),
                       dict(masterPhotoId=master['id'], bodyPhotoIds=[master['id']]),
                       dict(masterPhotoId=master['id'], bodyPhotoIds=['missing']),
                       dict(masterPhotoId=None, bodyPhotoIds=[master['id']])]:
            with self.assertRaises(ValueError):
                self.profiles.mutate('reference-roles', {'id': p['id'], 'revision': p['revision'], **values})
        self.assertNotIn('referenceRoles', self.profiles.get(p['id']))

    def test_scene_receipt_preserves_floating_deck_when_profile_has_legacy_booth(self):
        frame=self.profiles.add_image(self.p['id'],fixtures.photo(),'Floating frame','frame',
            receipt={'assetRole':'performance-scene','sceneStyle':'white-floating'})
        self.profiles.mutate('approve',{'id':self.p['id'],'kind':'frame','assetId':frame['id']})
        p=self.profiles.get(self.p['id']);p['booth']='A large table in a dark club.'
        self.profiles.mutate('save',{'id':p['id'],'revision':p['revision'],'profile':p})
        prompt=self.profiles.visual(p['id'])['prompt']
        self.assertIn('thin floating DJ controller',prompt)
        self.assertNotIn('A large table',prompt)
        self.assertIn('No gradual body widening',prompt)


class CharacterCompositionTests(unittest.TestCase):
    def profile(self):
        p = base_profile('test', 'Test DJ')
        p['photos'] = [{'id': 'side'}, {'id': 'master'}, {'id': 'front'}]
        p['referenceRoles'] = {'master': 'master', 'body': ['front', 'side']}
        p['boards'] = [{'id': 'clean-board'}]
        p['approvedBoard'] = 'clean-board'
        p['booth'] = 'Old profile says a large table and dark booth.'
        return p

    def test_board_has_only_character_views_and_no_performance_setting(self):
        p = self.profile()
        prompt = board_prompt(p, 'board')
        self.assertIn('six equal photographic panels', prompt)
        self.assertIn('FULL-LENGTH FRONT', prompt)
        self.assertIn('No DJ equipment, controller, table, booth', prompt)
        self.assertNotIn(p['booth'], prompt)
        self.assertNotIn('Listening at the mixer', prompt)
        self.assertIn('Image 1 is the MASTER FACE', prompt)
        self.assertIn('Image 2 is BODY PROPORTIONS ONLY', prompt)
        self.assertIn('Image 3 is BODY PROPORTIONS ONLY', prompt)

    def test_frame_adds_floating_controller_without_contaminating_identity(self):
        p = self.profile()
        prompt = board_prompt(p, 'frame')
        self.assertEqual([a['id'] for a in reference_inputs(p, 'frame')],
                         ['master', 'side', 'front', 'clean-board'])
        self.assertIn('thin floating DJ controller', prompt)
        self.assertIn('no table, tabletop, stand, cabinet or booth', prompt)
        self.assertIn('Image 4 is a DERIVED CHARACTER BOARD', prompt)
        self.assertIn('no collage', prompt)
        self.assertNotIn(p['booth'], prompt)
        self.assertEqual(p['photos'][0]['id'], 'side')  # No mutation of persisted order.

    def test_explicit_custom_scene_changes_only_scene_not_identity_board(self):
        p = self.profile()
        p['sceneStyle'] = 'custom'
        self.assertIn(p['booth'], board_prompt(p, 'frame'))
        self.assertNotIn(p['booth'], board_prompt(p, 'board'))

    def test_new_profiles_default_to_white_floating_scene(self):
        p = base_profile('test', 'Test DJ')
        self.assertEqual(p['sceneStyle'], 'white-floating')
        self.assertIn('white', p['booth'])

    def test_dangling_reference_roles_are_rejected_before_generation(self):
        p = self.profile()
        for roles in [{'master':'missing','body':[]}, {'master':'master','body':['missing']}]:
            bad = copy.deepcopy(p)
            bad['referenceRoles'] = roles
            with self.assertRaises(ValueError):
                reference_inputs(bad, 'frame')

