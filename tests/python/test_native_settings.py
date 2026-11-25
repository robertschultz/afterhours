"""Native command delivery with no real hardware, providers or playback."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from afterhours.dj_agent import DJAgent
from test_agent_scenarios import ScenarioSession
from afterhours import native_settings


class NativeSettingsTests(unittest.TestCase):
    def setUp(self):
        self.now=1000
        self.session=ScenarioSession();self.session.plan(['a','b','c'])
        self.provider=Mock(side_effect=AssertionError('No provider for device preferences'))
        self.agent=DJAgent(self.session,None,self.provider,clock=lambda:self.now)
        self.owner={'ownerId':'native-owner','sessionId':self.session.snapshot()['id']}

    def request(self,text='camera off',identity='device',source='typed'):
        return self.agent.message(text,source=source,message_id=identity,approved=True,native_context=self.owner)

    def deliver(self,status,message=None,**changes):
        args={'command_id':'device','owner_id':self.owner['ownerId'],'session_id':self.owner['sessionId'],'status':status,'message':message}
        return self.agent.native_settings_delivery(**{**args,**changes})

    def test_typed_and_voice_wait_for_the_native_result_without_music_side_effects(self):
        before=copy.deepcopy(self.session.snapshot())
        for index,source in enumerate(('typed','voice')):
            identity='device'+str(index)
            reply=self.request(identity=identity,source=source)
            self.assertEqual(reply['nativeSettingsCommand']['status'],'pending')
            self.assertEqual(reply['messages'][-1]['role'],'user')
            self.deliver('dispatching',command_id=identity)
            final=self.deliver('confirmed','Camera input is off.',command_id=identity)
            self.assertEqual(final['messages'][-1]['text'],'Camera input is off.')
            self.assertEqual(final['messages'][-2]['source'],source)
            self.assertEqual(final['allowance']['remaining'],0)
        self.assertEqual(self.session.snapshot(),before)
        self.assertEqual(self.session.calls,[]);self.provider.assert_not_called()

    def test_duplicate_message_and_claim_cannot_execute_twice(self):
        first=self.request();second=self.request()
        self.assertEqual(first['nativeSettingsCommand'],second['nativeSettingsCommand'])
        self.deliver('dispatching')
        with self.assertRaisesRegex(ValueError,'already been claimed'):self.deliver('dispatching')
        self.deliver('confirmed','Camera input is off.')
        self.deliver('confirmed','Camera input is off.')
        self.assertEqual(len(self.agent.snapshot()['messages']),2)
        with self.assertRaises(ValueError):self.deliver('unavailable','A conflicting result.')

    def test_only_current_owner_and_session_can_claim_or_finish(self):
        self.request()
        for changes in ({'owner_id':'another-owner'},{'session_id':'previous-set'},{'command_id':'other'}):
            with self.assertRaises(ValueError):self.deliver('dispatching',**changes)
        with self.assertRaises(ValueError):self.deliver('confirmed','Camera input is off.')
        self.assertEqual(self.agent.snapshot()['nativeSettingsCommand']['status'],'pending')
        self.owner['sessionId']='previous-set'
        with self.assertRaises(ValueError):self.request(identity='stale')

    def test_expiry_and_restart_do_not_replay(self):
        self.request();self.now+=31
        snapshot=self.agent.snapshot()
        self.assertEqual(snapshot['nativeSettingsCommand']['status'],'unavailable')
        self.assertIn('not confirmed',snapshot['messages'][-1]['text'])
        with self.assertRaises(ValueError):self.deliver('dispatching')
        self.now+=1
        with tempfile.TemporaryDirectory() as folder:
            self.agent.path=Path(folder)/'conversation.json'
            self.request(identity='next');self.deliver('dispatching',command_id='next')
            restored=DJAgent(self.session,self.agent.path,self.provider,clock=lambda:self.now)
            self.assertEqual(restored.snapshot()['nativeSettingsCommand']['status'],'unavailable')
            self.assertEqual(restored.snapshot()['allowance']['remaining'],0)

    def test_unknown_outcome_cannot_become_success_and_newer_turn_is_retained(self):
        self.request();self.deliver('dispatching')
        self.agent.state['messages'].append({'id':'newer','role':'user','text':'another direction','at':1001})
        self.agent.state['message']='Handling a newer direction.'
        final=self.deliver('unavailable','Camera access was denied. The camera is off.')
        self.assertEqual(final['message'],'Handling a newer direction.')
        self.assertEqual(final['nativeSettingsCommand']['status'],'unavailable')
        self.assertEqual([m['id'] for m in final['messages']],['device','device-reply','newer'])

    def test_only_exact_commands_execute_and_unsupported_controls_are_honest(self):
        for text in ('camera off?', 'do not turn the camera off', 'can the camera see me',
                     'camera off and play techno','open the room','turn it off', 'camera'):
            self.assertIsNone(native_settings.intent(text))
        for index,text in enumerate(('stop recording','set latency to low','start video recording','turn spoken commentary on')):
            reply=self.request(text,'unsupported'+str(index))
            self.assertIsNone(reply['nativeSettingsCommand'])
            self.assertEqual(reply['messages'][-1]['source'],'engine')
            self.assertEqual(reply['allowance']['remaining'],0)
        self.provider.assert_not_called()

    def test_inflight_is_not_overwritten_and_active_preparation_is_preserved(self):
        self.agent.pipeline={'stage':'analyzing','identity':'preserved'}
        self.request()
        with self.assertRaisesRegex(ValueError,'still being confirmed'):self.request('open catalog','second')
        self.assertEqual(self.agent.pipeline,{'stage':'analyzing','identity':'preserved'})
        self.assertEqual(self.agent.snapshot()['nativeSettingsCommand']['id'],'device')


if __name__=='__main__':unittest.main()
