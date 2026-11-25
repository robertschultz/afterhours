"""Local character/default commands. No model, live provider, or audio writes."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from afterhours.agent_mentions import resolve_mentions, roster_command
from afterhours.agent_profiles import AgentProfiles
from afterhours.dj_agent import DJAgent
from test_dj_agent import Session
from test_agent_profiles import photo

ROSTER=[{'id':'nova','name':'Nova','approvedFrame':'r'},
        {'id':'albert','name':'Albert','approvedFrame':'a'},
        {'id':'mark','name':'Mark','approvedFrame':'m'}]

class RosterCommandTests(unittest.TestCase):
    def test_typed_and_spoken_handoffs_resolve_names_and_direction(self):
        for text in ['have @albert now come in and take over the decks from @nova',
                     'Have Albert take over from Nova', '@Albert take over',
                     'Can you please switch the DJ to Albert?', 'Let @albert take over now']:
            with self.subTest(text=text):
                c=roster_command(text,ROSTER,'nova')
                self.assertEqual((c['kind'],c['fromAgentId'],c['toAgentId']),('handoff','nova','albert'))
        for text in ['do not have @albert take over','who is @albert?',
                     'Have @albert take over after two tracks','play a track by Mark',
                     'Could Albert take over later?', 'If I ask, have Albert take over']:
            self.assertIsNone(roster_command(text,ROSTER,'nova'),text)
        for text in ['Have @unknown take over','Have Albert take over from Mark']:
            with self.assertRaises(ValueError):roster_command(text,ROSTER,'nova')

    def make(self):
        s=Session();a=DJAgent(s,evaluator=Mock())
        a.profile_context=lambda:ROSTER[0]
        a.resolve_mentions=lambda text:resolve_mentions(text,ROSTER)
        a.roster_command=lambda text:roster_command(text,ROSTER,'nova')
        a.handoff_commit=Mock();a.default_profile=Mock()
        return s,a

    def test_handoff_is_local_idempotent_and_keeps_accepted_audio(self):
        for source,text in [('typed','Have @albert now come in and take over the decks from @nova'),
                            ('voice','Have Albert take over the decks from Nova')]:
            s,a=self.make();before=copy.deepcopy(s.state)
            a.playback_request={'id':'music','revision':0,'status':'playing','sessionId':s.state['id']}
            a.message(text,source=source,message_id='change',approved=True)
            a.message(text,source=source,message_id='change',approved=True)
            a.evaluator.assert_not_called();a.handoff_commit.assert_called_once()
            self.assertEqual(s.state,before);self.assertEqual(s.calls,[])
            self.assertEqual(a.playback_request['status'],'playing')
            self.assertEqual(a.state['handoff']['status'],'assigned')
            self.assertEqual(a.remaining,0)
            self.assertIsNone(a.state['performanceCommand'])

    def test_default_and_already_current_do_not_dispatch_handoff(self):
        s,a=self.make()
        a.message('Make @albert my default DJ',message_id='default',approved=True)
        a.default_profile.assert_called_once_with('albert');a.handoff_commit.assert_not_called()
        a.message('Have Nova take over',message_id='same',approved=True)
        a.handoff_commit.assert_not_called();a.evaluator.assert_not_called()
        self.assertIn('already',a.state['message'])

    def test_failed_video_assignment_does_not_report_handoff_success(self):
        s,a=self.make();a.handoff_commit.side_effect=ValueError('The video service is unavailable.')
        a.message('Have Albert take over',message_id='blocked',approved=True)
        self.assertEqual(a.state['handoff']['status'],'blocked')
        self.assertEqual(s.calls,[]);a.evaluator.assert_not_called()

    def test_default_persists_independently_of_current_assignment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'web').mkdir();p=AgentProfiles(root)
            target=p.mutate('create',{})
            frame=p.add_image(target['id'],photo(),'Fixture','frame')
            p.mutate('approve',{'id':target['id'],'kind':'frame','assetId':frame['id']})
            p.mutate('default',{'id':target['id']})
            self.assertEqual(p.snapshot()['assigned'],'nova')
            p.mutate('assign',{'id':'rivet'})
            self.assertEqual(AgentProfiles(root).snapshot()['default'],target['id'])
            self.assertEqual(AgentProfiles(root).snapshot()['assigned'],'rivet')
        s,a=self.make();a.reset_profile=Mock(return_value='Albert')
        a.fresh();a.reset_profile.assert_called_once()
        self.assertIn('Albert is at the decks',a.state['message'])

if __name__=='__main__':unittest.main()
