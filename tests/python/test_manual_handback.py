"""Manual mixer handback through the real command and mixer paths; no audio."""
import copy
import unittest
from contextlib import nullcontext
from unittest.mock import Mock, patch

from afterhours import copilot_commands as cc
from afterhours.dj_agent import DJAgent
from afterhours.dj_bridge import BridgeError
from test_copilot_commands import running
from afterhours.vdj_mixing import MixerControlChanged


class ManualHandbackTests(unittest.TestCase):
    def setUp(self):
        clock=patch('time.time',return_value=100)
        clock.start();self.addCleanup(clock.stop)

    def test_typed_and_voice_handback_phrases_resume_once_without_a_provider(self):
        phrases={'go ahead and beat match into the next track':'go',
                 'go ahead and start transitioning to the next track':'go',
                 'transition next':'go',
                 'Dot, transition next':'go',
                 'That transition to the next track':'go',
                 'could you please transition to the next track?':'go',
                 'take back over':'resume','you take over':'resume','resume automatic mixing':'resume'}
        for source in ('typed','voice'):
            for text,action in phrases.items():
                with self.subTest(source=source,text=text):
                    s=running();s.update(phase='takeover',active=False)
                    s.state['copilot']['mode']='off'
                    provider=Mock();agent=DJAgent(s,evaluator=provider)
                    before=copy.deepcopy(s.state['telemetry'])
                    with patch.object(s,'start') as start:
                        first=agent.message(text,source=source,message_id='one',approved=True)
                        agent.message(text,source=source,message_id='one',approved=True)
                    start.assert_called_once_with(resume=True,adopt_mixer=True,**({'resume_paused':True} if action=='resume' else {}))
                    self.assertEqual(first['copilotControl']['action'],action)
                    self.assertEqual(first['copilotControl']['status'],'pending')
                    self.assertEqual(s.state['copilot']['mode'],'leading')
                    self.assertEqual(s.state['telemetry'],before)
                    self.assertEqual(first['allowance']['remaining'],0)
                    provider.assert_not_called()

    def test_handback_recovers_known_alignment_stop_with_playing_or_paused_pair(self):
        for paused in (False,True):
            s=running();s.update(active=False,phase='error',lastError=None,
                events=[{'kind':'error','text':'Deck tempos moved after alignment. No transition was sent.'}])
            s.state['telemetry']['crossfader']=.46
            s.state['telemetry']['decks'][0]['playing']=not paused
            provider=Mock();agent=DJAgent(s,evaluator=provider)
            with patch.object(s,'start') as start:
                result=agent.message('take back over',message_id='resume')
                agent.message('take back over',message_id='resume')
            start.assert_called_once_with(resume=True,adopt_mixer=True,resume_paused=True)
            self.assertEqual(result['copilotControl']['status'],'pending')
            provider.assert_not_called()

    def test_handback_never_confirms_unknown_failure_or_uncertain_cleanup_as_ready(self):
        for phase,error,cleanup in [('error','Incoming cue moved.',False),('takeover',None,True)]:
            s=running();s.update(active=False,phase=phase,lastError=error,resumeBlocked=cleanup)
            for deck in s.state['telemetry']['decks']:deck['playing']=False
            agent=DJAgent(s,evaluator=Mock())
            with patch.object(s,'start') as start:
                result=agent.message('take back over',message_id='resume')
            self.assertEqual(result['copilotControl']['status'],'blocked')
            self.assertNotIn('is ready',result['messages'][-1]['text'])
            start.assert_not_called()

    def test_handback_keeps_hold_until_worker_confirms_a_future_exit(self):
        s=running();s.state['copilot']['held']=True
        with patch.object(s,'start') as start:
            s.copilot_command('resume','resume',cc.context(s.snapshot()))
        self.assertTrue(s.state['copilot']['held'])
        self.assertEqual(s.state['copilot']['command']['status'],'pending')
        with patch('afterhours.cue_planner.verify_receipts'):
            change=s.service_copilot(None,s.state['telemetry']['decks'][0],2,Mock(),155,s.state['timing'])
        self.assertIsNotNone(change);self.assertFalse(s.state['copilot']['held'])
        self.assertEqual(s.state['copilot']['command']['status'],'confirmed');start.assert_not_called()

    def test_future_negated_and_explanatory_requests_are_not_direct_controls(self):
        for text in ('do not take back over','go ahead and transition later',
                     'how do I tell you to take back over?',
                     'could you explain how to beat match the next track?',
                     'take back over after this song','go ahead and do not beat match the next track'):
            self.assertIsNone(cc.intent(text),text)
        self.assertEqual(cc.intent('take over'),'off')

    def test_resume_go_still_rejects_unknown_errors_stale_decks_and_missed_exits(self):
        changes=[lambda s:s.update(phase='error',lastError='Incoming cue moved.'),
                 lambda s:s['telemetry'].update(sampledAt=96),
                 lambda s:s['telemetry'].update(crossfader=.5),
                 lambda s:s['telemetry']['decks'][1].update(playing=True),
                 lambda s:s['telemetry']['decks'][1].update(id='other'),
                 lambda s:s['telemetry']['decks'][0].update(beatPosition=10000)]
        for change in changes:
            s=running();s.update(phase='takeover',active=False);s.state['copilot']['mode']='off'
            change(s.state);provider=Mock();agent=DJAgent(s,evaluator=provider)
            with patch.object(s,'start') as start:
                result=agent.message('go ahead and beat match into the next track',message_id='go')
            self.assertEqual(result['copilotControl']['status'],'blocked')
            start.assert_not_called();provider.assert_not_called()

    def test_mixer_change_yields_without_reset_or_pause_and_uncertain_cleanup_stays_blocked(self):
        for cleanup_failed in (False,True):
            s=running();before=copy.deepcopy(s.state['telemetry']);client=Mock()
            s.factory=lambda *a,**kw:nullcontext(client)
            s.mixer=Mock()
            if cleanup_failed:s.mixer.cancel_ramps.side_effect=BridgeError('No acknowledgement')
            s.control_lock.acquire()
            def interrupted(c):raise MixerControlChanged('Playing deck mixer controls changed.')
            s._work(interrupted)
            result=s.snapshot()
            self.assertEqual(result['phase'],'error' if cleanup_failed else 'takeover')
            self.assertFalse(result['active']);self.assertEqual(result['telemetry'],before)
            client.send.assert_not_called()
            if not cleanup_failed:
                self.assertEqual(result['copilot']['mode'],'off')
                self.assertIsNone(result['lastError'])

    def test_original_playback_receipt_moves_between_manual_and_confirmed_resume(self):
        s=running();agent=DJAgent(s,evaluator=Mock())
        agent.playback_request={'id':'original','status':'playing','sessionId':s.state['id']}
        s.update(active=False,phase='takeover',message='You have control.')
        agent.reconcile_playback(s.snapshot())
        self.assertEqual(agent.playback_request['status'],'manual')
        s.update(active=True,phase='playing')
        s.state['copilot']['command']={'action':'go','status':'pending','message':'Checking.'}
        agent.reconcile_playback(s.snapshot())
        self.assertEqual(agent.playback_request['status'],'manual')
        s.state['copilot']['command'].update(status='confirmed',message='Resumed at a verified cue.')
        agent.reconcile_playback(s.snapshot())
        self.assertEqual(agent.playback_request['status'],'playing')


if __name__=='__main__':unittest.main()
