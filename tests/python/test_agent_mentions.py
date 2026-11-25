"""Roster resolution and simulated planner handoffs; no paid/provider calls."""
import copy
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock
from afterhours.agent_mentions import resolve_mentions, validate_handoff
from afterhours.dj_agent import DJAgent
from test_dj_agent import Session, decision, settled

ROSTER=[{'id':'nova','name':'Nova','approvedFrame':'a'},
        {'id':'rivet','name':'Rivet','approvedFrame':'b'},
        {'id':'custom-3','name':'Jane Doe','approvedFrame':'c'}]
CONTEXT={'ready':True,'runId':'a'*32,'ownerId':'tab-1','character':'nova',
         'referenceMode':True,'castIds':['nova','rivet']}

class MentionTests(unittest.TestCase):
    def test_names_ids_spaces_case_and_email(self):
        self.assertEqual([m['id'] for m in resolve_mentions('Have @RIVET take over from @Nova. Email rivet@example.com. @rivet',ROSTER)],['rivet','nova'])
        self.assertEqual(resolve_mentions('@"Jane Doe"',ROSTER)[0]['id'],'custom-3')
        self.assertEqual(resolve_mentions('@Jane-Doe',ROSTER)[0]['id'],'custom-3')
        self.assertEqual(resolve_mentions('rivet@example.com',ROSTER),[])
    def test_unknown_ambiguous_and_invalid_handoff(self):
        with self.assertRaises(ValueError):resolve_mentions('@missing',ROSTER)
        with self.assertRaises(ValueError):resolve_mentions('@Rivet',ROSTER+[{'id':'duplicate','name':'Rivet'}])
        mentions=resolve_mentions('@Rivet',ROSTER)
        for source,target in [('rivet','rivet'),('nova','unknown'),('nova',[])]:
            with self.assertRaises(ValueError):validate_handoff({'fromAgentId':source,'toAgentId':target,'reason':'Asked'},mentions,ROSTER[0])
        self.assertIsNone(validate_handoff(None,mentions,ROSTER[0]))
    def make(self,context,change=True):
        s=Session();d=decision();d.update(direction='',handoff={'fromAgentId':'nova','toAgentId':'rivet','reason':'The operator requested Rivet.'} if change else None,
            performance={'prompt':'Rivet walks into the booth and takes over from Nova. Nova steps aside.','reason':'Requested handoff.'} if change else None)
        evaluator=Mock(return_value=(d,{'responseId':'simulated'}))
        a=DJAgent(s,evaluator=evaluator);a.profile_context=lambda:ROSTER[0];a.resolve_mentions=lambda text:resolve_mentions(text,ROSTER);a.handoff_commit=Mock()
        a.state['direction']='Warm house';a.message('Have @Rivet take over from @Nova',message_id='handoff-1',approved=True,performance_context=context);settled(a)
        return s,a,evaluator
    def test_offline_assignment_keeps_music_and_does_not_queue_video(self):
        s,a,e=self.make({'ready':False})
        self.assertEqual(a.state['handoff']['status'],'assigned');self.assertEqual(s.calls,[])
        self.assertEqual(a.state['direction'],'Warm house');self.assertIsNone(a.state['performanceCommand'])
        a.handoff_commit.assert_called_once();self.assertEqual(len(e.call_args.args[0]['mentionedAgents']),2)
    def test_solo_video_cannot_fake_second_identity(self):
        s,a,e=self.make({**CONTEXT,'referenceMode':False,'castIds':[]})
        self.assertEqual(a.state['handoff']['status'],'needs-cast');self.assertIsNone(a.state['performanceCommand'])
        a.handoff_commit.assert_not_called();self.assertEqual(s.calls,[])
    def test_named_handoff_assigns_once_only_after_matching_ack(self):
        s,a,e=self.make(CONTEXT);c=a.state['performanceCommand'];args=(c['id'],c['ownerId'],c['runId'])
        a.handoff_commit.assert_not_called();a.performance_delivery(*args,'dispatching');a.performance_delivery(*args,'sent')
        a.handoff_commit.assert_not_called();a.performance_delivery(*args,'applied');a.handoff_commit.assert_called_once()
        self.assertEqual(a.state['handoff']['status'],'assigned');self.assertEqual(s.calls,[])
        with self.assertRaises(ValueError):a.performance_delivery(*args,'applied')
    def test_rejection_no_assignment_and_question_no_handoff(self):
        s,a,e=self.make(CONTEXT);c=a.state['performanceCommand'];args=(c['id'],c['ownerId'],c['runId'])
        a.performance_delivery(*args,'dispatching');a.performance_delivery(*args,'rejected');a.handoff_commit.assert_not_called()
        s,a,e=self.make(CONTEXT,change=False);self.assertIsNone(a.state['handoff']);a.handoff_commit.assert_not_called()
    def test_unknown_name_rejected_before_provider(self):
        a=DJAgent(Session(),evaluator=Mock());a.resolve_mentions=lambda text:resolve_mentions(text,ROSTER)
        with self.assertRaises(ValueError):a.message('@Missing take over',message_id='new',approved=True)
        a.evaluator.assert_not_called()
    def test_pending_handoff_expires_and_is_not_restored_after_restart(self):
        s,a,e=self.make(CONTEXT)
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'conversation.json';a.path=path;a.save()
            restored=DJAgent(s,path=path,evaluator=Mock())
            self.assertEqual(restored.state['handoff']['status'],'expired')
            self.assertEqual(restored.state['performanceCommand']['status'],'expired')
            restored.evaluator.assert_not_called()
        a.path=None;a.clock=lambda:a.state['performanceCommand']['expiresAt']+1
        self.assertEqual(a.snapshot()['handoff']['status'],'expired')
        a.handoff_commit.assert_not_called()

if __name__=='__main__':unittest.main()
