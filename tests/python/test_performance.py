import threading
import unittest
from unittest.mock import patch
from afterhours.performance import Performance, checked_url, OVERLAP

class FixtureProvider:
    def __init__(self,fail=False):self.calls=[];self.fail=fail
    def generate(self,previous,prompt,cancel,on_job):
        self.calls.append((previous,prompt));on_job('fixture-job')
        if self.fail:raise TimeoutError('uncertain submit')
        return {'url':'/media/rivet-demo.mp4','provider_url':'https://example.fal.media/fixture.mp4','duration':8,'inPoint':OVERLAP}

class PerformanceTests(unittest.TestCase):
    def test_performer_switch_changes_every_segment_and_blocks_wrong_cloud_reference(self):
        p=Performance(FixtureProvider(),1)
        with patch('afterhours.performance.Path.is_file', return_value=True):
            result=p.command({'action':'character','character':'nova'})
        self.assertEqual(result['performer'],'Nova')
        self.assertTrue(all('nova-' in s['url'] for s in result['segments']))
        self.assertEqual(result['clipBeats'],24)
        self.assertEqual(p.submissions,0)
        with self.assertRaises(ValueError):p.command({'action':'generate'})
        p.command({'action':'rehearse'})
        self.assertEqual(p.snapshot()['character'],'nova')

    def test_unavailable_performer_does_not_replace_current_picture(self):
        p=Performance();before=p.snapshot()
        with patch('afterhours.performance.Path.is_file', return_value=False):
            with self.assertRaises(ValueError):p.command({'action':'character','character':'nova'})
        self.assertEqual(p.snapshot()['session'],before['session'])

    def test_hosted_locked_by_default(self):
        p=Performance()
        with self.assertRaises(ValueError):p.command({'action':'generate'})
        self.assertEqual(p.submissions,0)
    def test_rehearsal_has_no_provider_calls(self):
        provider=FixtureProvider();p=Performance(provider,3);p.command({'action':'rehearse'})
        self.assertEqual(provider.calls,[]);self.assertEqual(p.snapshot()['source'],'existing clip loop')
    def test_pause_resume_blackout_preserve_elapsed(self):
        clock=[100];p=Performance(now=lambda:clock[0]);p.command({'action':'rehearse'});clock[0]+=3
        p.command({'action':'pause'});clock[0]+=5;self.assertEqual(p.elapsed(),3)
        p.command({'action':'play'});clock[0]+=2;p.command({'action':'blackout'});clock[0]+=2;self.assertEqual(p.elapsed(),5)
    def test_direction_survives_start(self):
        p=Performance();p.command({'action':'settings','look':'mono','fit':'cover','caption':'Hello','prompt':'A subtle wave'})
        p.command({'action':'rehearse'});self.assertEqual(p.state['caption'],'Hello');self.assertEqual(p.state['prompt'],'A subtle wave')
    def test_music_timing_survives_rehearsal_and_older_settings(self):
        p=Performance();self.assertFalse(p.snapshot()['beatSync'])
        p.command({'action':'settings','look':'warm','fit':'contain','beatSync':True})
        p.command({'action':'rehearse'})
        p.command({'action':'settings','look':'mono','fit':'cover'})
        self.assertTrue(p.snapshot()['beatSync']);self.assertEqual(p.submissions,0)
        p.command({'action':'settings','look':'mono','fit':'cover','beatSync':False})
        self.assertFalse(p.snapshot()['beatSync'])
    def test_invalid_music_timing_does_not_change_settings(self):
        p=Performance();before=p.snapshot()
        for value in ('false',1,None,[]):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):p.command({'action':'settings','look':'mono','fit':'cover','beatSync':value})
                self.assertEqual(p.snapshot()['beatSync'],before['beatSync'])
                self.assertEqual(p.snapshot()['look'],before['look'])
                self.assertEqual(p.snapshot()['revision'],before['revision'])
    def test_limit_and_reference_chain(self):
        provider=FixtureProvider();p=Performance(provider,2);p.command({'action':'generate'});p.worker.join(2)
        self.assertFalse(p.worker.is_alive());self.assertEqual(len(provider.calls),2);self.assertEqual(p.state['generation'],'limit')
        self.assertEqual(provider.calls[1][0],'https://example.fal.media/fixture.mp4');self.assertEqual(p.state['segments'][1]['inPoint'],OVERLAP)
        with self.assertRaises(ValueError):p.command({'action':'generate'})
    def test_uncertain_submission_is_not_retried(self):
        provider=FixtureProvider(True);p=Performance(provider,10);p.command({'action':'generate'});p.worker.join(2)
        self.assertEqual(p.submissions,1);self.assertEqual(len(provider.calls),1);self.assertEqual(p.state['generation'],'error')
    def test_stop_during_job_discards_result(self):
        begun=threading.Event();release=threading.Event()
        class Slow(FixtureProvider):
            def generate(self,*args):begun.set();release.wait(2);return super().generate(*args)
        p=Performance(Slow(),3);p.command({'action':'generate'});self.assertTrue(begun.wait(2));p.command({'action':'stop-generation'});release.set();p.worker.join(2)
        self.assertEqual(p.state['generated'],0);self.assertEqual(p.state['mode'],'playing');self.assertEqual(p.submissions,1)
    def test_provider_urls_block_secrets_and_loopback(self):
        for url in ('http://foo.fal.media/a','https://127.0.0.1/a','https://fal.media.evil.test/a','https://user:pass@foo.fal.media/a'):
            with self.assertRaises(ValueError):checked_url(url)
        with self.assertRaises(ValueError):checked_url('https://evil.test/a',queue=True)
    def test_display_heartbeats_expire(self):
        now=[100];p=Performance(now=lambda:now[0]);p.heartbeat({'id':'display-123','state':'playing'});self.assertEqual(len(p.snapshot()['displays']),1)
        now[0]+=7;self.assertEqual(len(p.snapshot()['displays']),0)
    def test_snapshot_does_not_expose_mutable_state(self):
        p=Performance();s=p.snapshot();s['segments'].append({});self.assertEqual(p.state['segments'],[])

if __name__=='__main__':unittest.main()
