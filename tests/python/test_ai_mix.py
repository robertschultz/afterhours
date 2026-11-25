"""AI and native-analysis boundaries. Fixtures only; no paid calls or playback."""
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from afterhours import ai_mix
from afterhours.native_analysis import track_analysis
from afterhours.vdj_catalog import identity


def evidence():
    return {'fingerprint':'fixture','tracks':[{'id':x,'title':x,'points':[{'id':x+'-p','seconds':10}],
            'energyMap':None} for x in ('a','b')]}


def answer():
    return {'summary':'Needs a cue audition.','transitions':[{'fromId':'a','toId':'b','decision':'hold',
            'outgoingPointId':'a-p','incomingPointId':'b-p','technique':'restrained-bass-swap',
            'blendBeats':16,'confidence':'low','reason':'No vocal or section map.',
            'missingEvidence':['Audition the cue and verify the bar grid.']}]}


def dense_cue_report(count):
    """Native-shaped horizon: eight measured alternatives for every recording."""
    from afterhours import cue_planner
    from test_cue_planner import fixture
    rows,report,_=fixture(count,60)
    for index,track in enumerate(report['tracks']):
        track.update(id='virtualdj:'+identity('synthetic-recording-'+str(index)),
                     title='Synthetic extended recording '+str(index),artist='Fixture artist',
                     limitations=['Numerical energy does not identify vocals or musical sections.'])
        candidates=[]
        for option in range(8):
            offset=64+option*32
            native={'anchorBeat':0,'offsetBeats':offset,'entryBeat':offset,
                    'entrySeconds':offset*60/155,'introEndSeconds':(offset+64)*60/155,
                    'exitBeat':offset+128,'exitSeconds':(offset+128)*60/155,
                    'endBeat':offset+192,'endSeconds':(offset+192)*60/155}
            cue=cue_planner.candidate(track,rows,native,{'spanBeats':128,'blendBeats':64,'medianRmsDb':-14})
            cue['replayVerified']=True
            candidates.append(cue)
        track['cueCandidates']=candidates
    report=cue_planner.attach_candidates(report,{t['id']:t['cueCandidates'] for t in report['tracks']},60,155)
    response={'summary':'Synthetic measured window, not a listening assessment.','decision':'propose','missingEvidence':[],
              'selections':[{'trackId':t['id'],'candidateId':t['cueCandidates'][-1]['id'],'reason':'Measured alternative.'}
                            for t in report['tracks']],
              'transitions':[{'fromId':a['id'],'toId':b['id'],'technique':'restrained-bass-swap',
                              'blendBeats':64,'reason':'Unknown vocal activity.'}
                             for a,b in zip(report['tracks'],report['tracks'][1:])]}
    return report,response


class AnalysisTests(unittest.TestCase):
    def test_exact_identity_match_and_no_locator_disclosure(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'db.xml';p.write_text('<VirtualDJ_Database><Song FilePath="netsearch://private-recording"><Tags Title="X"/><Infos SongLength="120"/><Scan BeatGrid="opaque"/><Poi Pos="25" Type="automix" Point="cutStart"/><Poi Pos="nan" Type="remix" Name="Bad"/><Poi Pos="130" Type="cue" Name="Outside"/></Song></VirtualDJ_Database>')
            tracks=[{'id':'x','title':'X','bpm':120,'engine':{'selection_id':identity('netsearch://private-recording')}},
                    {'id':'y','title':'X','bpm':120,'engine':{'selection_id':identity('different-recording')}}]
            result=track_analysis(tracks,p)
            self.assertEqual(len(result['tracks'][0]['points']),1)
            self.assertTrue(result['tracks'][0]['variableTempo'])
            self.assertEqual(result['tracks'][1]['points'],[])
            self.assertNotIn('netsearch',json.dumps(result))
            self.assertIsNone(result['tracks'][0]['energyMap'])
            self.assertFalse(result['audioIncluded'])

    def test_corrupt_database_fails_without_fabricating_points(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'db.xml';p.write_text('<broken')
            with self.assertRaises(ValueError):track_analysis([],p)


class AIReviewTests(unittest.TestCase):
    def test_provider_failure_classes_are_safe_and_not_guessed(self):
        cases=[('insufficient_quota','quota is exhausted'),
               ('credit_balance_exhausted','credit balance is exhausted'),
               ('organization_spend_limit_exceeded','organization spending limit reached'),
               ('project_spend_limit_exceeded','project spending limit reached'),
               ('organization_usage_limit_exceeded','organization usage limit reached'),
               ('rate_limit_exceeded','temporarily rate-limited'),
               ('slow_down','slower request rate'),('unknown','HTTP 429')]
        for code,label in cases:
            with self.subTest(code=code):
                body=json.dumps({'error':{'code':code,'message':'SECRET provider payload'}}).encode()
                exc=HTTPError('https://api.openai.com/v1/responses',429,'',{},io.BytesIO(body))
                result=ai_mix.provider_error(exc)
                self.assertIn(label,result)
                self.assertNotIn('SECRET',result)
                self.assertIn('No automatic retry',result)

    def test_provider_type_fallback_and_specific_code_precedence(self):
        cases=[({'type':'insufficient_quota'},'quota is exhausted'),
               ({'type':'rate_limit_error'},'temporarily rate-limited'),
               ({'code':'project_spend_limit_exceeded','type':'insufficient_quota'},'project spending limit reached'),
               ({'code':'slow_down','type':'insufficient_quota'},'slower request rate')]
        for error,label in cases:
            with self.subTest(error=error):
                exc=HTTPError('private',429,'',{},io.BytesIO(json.dumps({'error':error}).encode()))
                self.assertIn(label,ai_mix.provider_error(exc))

    def test_provider_malformed_or_oversized_body_never_leaks_or_guesses(self):
        bodies=[b'not json SECRET',b'null',b'[]',b'"SECRET"',b'{"error":[]}',
                b'{"error":{"code":[],"type":{}}}',
                json.dumps({'error':{'code':'insufficient_quota','message':'SECRET'*2000}}).encode()]
        for body in bodies:
            with self.subTest(body_size=len(body)):
                exc=HTTPError('private',429,'SECRET error',{},io.BytesIO(body))
                result=ai_mix.provider_error(exc)
                self.assertIn('HTTP 429',result)
                self.assertNotIn('SECRET',result)
                self.assertNotIn('quota is exhausted',result)

    def test_billing_code_cannot_override_other_http_status(self):
        for status in (401,403,404,500):
            with self.subTest(status=status):
                exc=HTTPError('private',status,'',{},io.BytesIO(b'{"error":{"code":"insufficient_quota"}}'))
                result=ai_mix.provider_error(exc)
                self.assertNotIn('quota is exhausted',result)
                self.assertIn('No automatic retry',result)

    def test_approval_required_before_reading_key_or_network(self):
        with patch('afterhours.ai_mix.api_key') as key:
            with self.assertRaises(ValueError):ai_mix.review(evidence(),30)
            key.assert_not_called()

    def test_validator_rejects_invented_cues_wrong_order_and_unearned_confidence(self):
        for field,value in [('outgoingPointId','invented'),('toId','another'),('confidence','high'),('blendBeats',24)]:
            bad=answer();bad['transitions'][0][field]=value
            with self.subTest(field=field),self.assertRaises(ValueError):ai_mix.validate(bad,evidence())
        self.assertEqual(ai_mix.validate(answer(),evidence()),answer())

    def test_one_request_bounded_output_no_audio_no_transport(self):
        payload={'status':'completed','id':'fixture','output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(answer())}]}],
                 'usage':{'input_tokens':10,'output_tokens':10,'total_tokens':20}}
        opener=Mock(return_value=io.BytesIO(json.dumps(payload).encode()))
        with patch('afterhours.ai_mix.api_key',return_value='sk-fixture-not-a-real-key'):
            result=ai_mix.review(evidence(),30,approved=True,opener=opener)
        opener.assert_called_once()
        request=opener.call_args.args[0];body=json.loads(request.data)
        self.assertFalse(body['store']);self.assertEqual(body['max_output_tokens'],6000)
        self.assertEqual(body['text']['format']['type'],'json_schema')
        self.assertFalse(result['appliedToPlayback'])
        self.assertNotIn('sk-fixture',json.dumps(result))
        self.assertFalse(json.loads(body['input'])['audioIncluded'])

    def test_http_failure_is_sanitized_and_not_retried(self):
        opener=Mock(side_effect=HTTPError('private',429,'secret error',{},None))
        with patch('afterhours.ai_mix.api_key',return_value='sk-fixture-not-a-real-key'),self.assertRaisesRegex(ValueError,'HTTP 429') as ctx:
            ai_mix.review(evidence(),30,approved=True,opener=opener)
        opener.assert_called_once();self.assertNotIn('secret',str(ctx.exception))

    def test_incomplete_or_invalid_output_never_becomes_a_plan(self):
        for payload in ({'status':'incomplete'},{'status':'completed','output':[]}):
            with patch('afterhours.ai_mix.api_key',return_value='sk-fixture-not-a-real-key'),self.assertRaises(ValueError):
                ai_mix.review(evidence(),30,approved=True,opener=lambda *a,**k:io.BytesIO(json.dumps(payload).encode()))


class CuePlanBudgetTests(unittest.TestCase):
    def test_dense_seven_and_twelve_recording_horizons_fit_one_unchanged_request_budget(self):
        for count in (7,12):
            with self.subTest(count=count):
                report,response=dense_cue_report(count);before=copy.deepcopy(report)
                payload={'status':'completed','output':[{'type':'message','content':[
                    {'type':'output_text','text':json.dumps(response)}]}]}
                opener=Mock(return_value=io.BytesIO(json.dumps(payload).encode()))
                with patch('afterhours.ai_mix.api_key',return_value='sk-fixture-not-a-real-key'):
                    result=ai_mix.plan(report,60,approved=True,opener=opener,direction='Synthetic direction. '*80)
                opener.assert_called_once()
                wire=opener.call_args.args[0].data;body=json.loads(wire);packet=json.loads(body['input'])
                self.assertEqual(ai_mix.MAX_BODY,50000);self.assertLessEqual(len(wire),ai_mix.MAX_BODY)
                self.assertEqual(body['max_output_tokens'],6000)
                self.assertEqual(result['selections'],response['selections'])
                self.assertFalse(result['appliedToPlayback']);self.assertEqual(report,before)
                self.assertEqual([t['id'] for t in packet['tracks']],[t['id'] for t in report['tracks']])
                for source,sent in zip(report['tracks'],packet['tracks']):
                    self.assertEqual(len(sent['cueCandidates']),8)
                    for cue,row in zip(source['cueCandidates'],sent['cueCandidates']):
                        decoded=dict(zip(packet['cueColumns'],row))
                        for field in ('entry','exit','body'):
                            decoded[field]=dict(zip(packet['energyColumns'],decoded[field]))
                        self.assertEqual(decoded,{field:cue[field] for field in decoded})
                        self.assertEqual(decoded['id'],cue['id'])
                        self.assertTrue(decoded['replayVerified'])
                # This fixture crosses the former cap without reducing the
                # queue or its eight choices to make the new request fit.
                legacy=copy.deepcopy(packet)
                for source,sent in zip(report['tracks'],legacy['tracks']):sent['cueCandidates']=source['cueCandidates']
                body['input']=json.dumps(legacy,ensure_ascii=True,separators=(',',':'))
                self.assertGreater(len(json.dumps(body,ensure_ascii=True,separators=(',',':')).encode()),ai_mix.MAX_BODY)

    def test_model_packet_keeps_uncertainty_and_leaves_replay_evidence_local(self):
        report,_=dense_cue_report(2);cue=report['tracks'][0]['cueCandidates'][0]
        cue.update(replayVerified=False,phraseConfidence='estimated',vocals='unknown',
                   privateReceipt={'locator':'synthetic-local-only'},entry=None)
        before=copy.deepcopy(report);packet=ai_mix.plan_packet(report,60)
        decoded=dict(zip(packet['cueColumns'],packet['tracks'][0]['cueCandidates'][0]))
        self.assertFalse(decoded['replayVerified']);self.assertIsNone(decoded['entry'])
        self.assertEqual(decoded['phraseConfidence'],'estimated');self.assertEqual(decoded['vocals'],'unknown')
        self.assertNotIn('synthetic-local-only',json.dumps(packet));self.assertEqual(report,before)

    def test_dense_plan_still_needs_approval_and_oversized_facts_never_send_or_retry(self):
        report,_=dense_cue_report(7);opener=Mock()
        with patch('afterhours.ai_mix.api_key') as key:
            with self.assertRaises(ValueError):ai_mix.plan(report,60,opener=opener)
            key.assert_not_called();opener.assert_not_called()
        report['tracks'][0]['limitations']=['x'*ai_mix.MAX_BODY]
        before=copy.deepcopy(report)
        with patch('afterhours.ai_mix.api_key',return_value='sk-fixture-not-a-real-key'):
            with self.assertRaisesRegex(ValueError,'bounded AI request size'):
                ai_mix.plan(report,60,approved=True,opener=opener)
        opener.assert_not_called();self.assertEqual(report,before)

    def test_compact_input_cannot_bypass_native_replay_validation(self):
        report,response=dense_cue_report(2)
        report['tracks'][0]['cueCandidates'][-1]['replayVerified']=False
        payload={'status':'completed','output':[{'type':'message','content':[
            {'type':'output_text','text':json.dumps(response)}]}]}
        opener=Mock(return_value=io.BytesIO(json.dumps(payload).encode()))
        with patch('afterhours.ai_mix.api_key',return_value='sk-fixture-not-a-real-key'):
            with self.assertRaisesRegex(ValueError,'native replay verification'):
                ai_mix.plan(report,60,approved=True,opener=opener)
        opener.assert_called_once()

if __name__=='__main__':unittest.main()
