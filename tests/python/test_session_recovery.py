"""Durable queue recovery with native transport and provider calls forbidden."""
import copy,json,stat,tempfile,threading,unittest
from pathlib import Path
from unittest.mock import patch
from afterhours.dj_bridge import BridgeError
from afterhours.session_recovery import SessionRecovery,recovery_state
from afterhours.vdj_catalog import identity
from afterhours.vdj_session import DJSession


def session():
    s=DJSession(threading.Lock(),client_factory=lambda *a,**k:(_ for _ in ()).throw(AssertionError('No transport')))
    tracks=[{'id':'virtualdj:'+identity(str(i)),'title':str(i),'artist':'Fixture','bpm':125,
             'engine':{'selection_id':identity(str(i)),'folder_id':identity('origin-'+str(i))}} for i in range(4)]
    s.restore({'candidates':tracks,'order':tracks,'catalogFolder':{'id':identity('current')}})
    return s


class RecoveryTests(unittest.TestCase):
    def long_session(self, index=24):
        source=session()
        tracks=[{'id':'virtualdj:'+identity('long-'+str(i)),'title':f'House record {i}','artist':'Fixture','bpm':125,
                 'engine':{'selection_id':identity('long-'+str(i)),'folder_id':identity('long-folder')}} for i in range(32)]
        source.update(candidates=tracks,order=tracks[:28],active=True,phase='playing',index=index,
                      telemetry={'decks':[{'playing':True}]},executionPlan={'applied':True})
        return source

    def test_checkpoint_keeps_provider_punctuation_out_of_native_search(self):
        source = session()
        for bucket in ('candidates', 'order'):
            source.state[bucket][0]['artist'] = 'Nat "King" Cole'
            source.state[bucket][1]['artist'] = 'AC/DC'
            source.state[bucket][2].update(artist='[]', title='Fallback title')
        with tempfile.TemporaryDirectory() as directory:
            store = SessionRecovery(Path(directory) / 'queue.json')
            store.save(source)
            target = session()
            receipt = store.restore(target)
        self.assertTrue(receipt['restored'])
        self.assertEqual(target.state['order'], source.state['order'])
        self.assertEqual([target.positions[t['id']]['query'] for t in target.state['order'][:3]],
                         ['Nat King Cole', 'AC DC', 'Fallback title'])
        self.assertFalse(target.state['active'])

    def test_long_checkpoint_restores_remaining_queue_and_auditable_history_without_authority(self):
        source=self.long_session();original=copy.deepcopy(source.state['order'])
        with tempfile.TemporaryDirectory() as directory:
            store=SessionRecovery(Path(directory)/'queue.json');store.save(source)
            stored=json.loads(store.path.read_text())['session']
            self.assertEqual(stored['order'],original)
            self.assertEqual(stored['recoveryWindow']['heardIds'],[t['id'] for t in original[:24]])
            target=session();receipt=store.restore(target);restored=target.snapshot()
            self.assertEqual(receipt['status'],'needs-analysis');self.assertEqual(receipt['playedHistoryCount'],24)
            self.assertEqual(restored['order'],original[24:])
            self.assertEqual([t['id'] for t in restored['recoveryHistory']]+[t['id'] for t in restored['order']],
                             [t['id'] for t in original])
            self.assertFalse(restored['active']);self.assertEqual(restored['phase'],'stopped')
            self.assertEqual(restored['index'],-1);self.assertIsNone(restored['telemetry']);self.assertIsNone(restored['executionPlan'])
            self.assertIsNone(restored['analysis']);self.assertIsNone(restored['aiReview'])
            from afterhours.dj_agent import DJAgent
            agent=DJAgent(target);self.assertIsNone(agent.snapshot()['curation']);self.assertEqual(agent.remaining,0)
            # A subsequent checkpoint preserves the audit metadata too.
            store.save(target);again=session();store.restore(again)
            self.assertEqual(again.state['recoveryHistory'],restored['recoveryHistory'])
            self.assertEqual(again.state['order'],original[24:])

    def test_final_remaining_record_survives_in_catalog_without_replaying_history(self):
        source=self.long_session(index=27)
        with tempfile.TemporaryDirectory() as directory:
            store=SessionRecovery(Path(directory)/'queue.json');store.save(source)
            target=session();receipt=store.restore(target)
            self.assertEqual(receipt['status'],'catalog-only');self.assertEqual(target.state['order'],[])
            self.assertEqual(target.state['recoveryRemainder'][0]['id'],source.state['order'][-1]['id'])
            self.assertIn(source.state['order'][-1]['id'],[t['id'] for t in target.state['candidates']])
            self.assertFalse(target.state['active']);self.assertIn('final remaining',target.state['message'])
            store.save(target);again=session();self.assertEqual(store.restore(again)['status'],'catalog-only')
            self.assertIn('final remaining',again.state['message'])

    def test_new_pair_supersedes_final_single_recovery_notice(self):
        source=self.long_session(index=27)
        with tempfile.TemporaryDirectory() as directory:
            store=SessionRecovery(Path(directory)/'queue.json');store.save(source)
            target=session();store.restore(target)
            old_remainder=copy.deepcopy(target.state['recoveryRemainder'])
            pair=[source.state['order'][-1]['id'],source.state['candidates'][-1]['id']]
            target.plan(pair,mix_style='quick')
            self.assertEqual(target.state['recoveryRemainder'],[])
            store.save(target);restored=session();receipt=store.restore(restored)
            self.assertEqual(receipt['status'],'restored');self.assertEqual(receipt['tracks'],2)
            self.assertEqual([t['id'] for t in restored.state['order']],pair)
            self.assertNotIn('final remaining',restored.state['message'])
            self.assertEqual(restored.state['recoveryRemainder'],[])
            # Older metadata cannot override an actual selected queue either.
            stale=json.loads(store.path.read_text());stale['session']['recoveryRemainder']=old_remainder
            store.path.write_text(json.dumps(stale));again=session()
            self.assertEqual(store.restore(again)['status'],'restored')
            self.assertEqual(again.state['recoveryRemainder'],[])

    def test_forged_long_history_and_oversized_fresh_queue_are_rejected_transactionally(self):
        source=self.long_session();saved=recovery_state(source)
        for change in ('missing-boundary','wrong-prefix','wrong-tail','duplicate-prefix','unknown-history','fresh-invalid'):
            bad=copy.deepcopy(saved)
            if change=='missing-boundary':bad.pop('recoveryWindow')
            elif change=='wrong-prefix':bad['recoveryWindow']['heardIds'][0]=bad['order'][-1]['id']
            elif change=='wrong-tail':bad['recoveryWindow']['remainingIds'].reverse()
            elif change=='duplicate-prefix':bad['recoveryWindow']['heardIds'][1]=bad['recoveryWindow']['heardIds'][0]
            elif change=='unknown-history':
                bad['order'][0]['id']='virtualdj:'+identity('forged-history')
                bad['recoveryWindow']['heardIds'][0]=bad['order'][0]['id']
            else:bad['recoveryWindow']={'heardIds':[],'remainingIds':[t['id'] for t in bad['order']]}
            with self.subTest(change=change),tempfile.TemporaryDirectory() as directory:
                path=Path(directory)/'queue.json';path.write_text(json.dumps({'version':1,'session':bad}))
                target=session();before=target.snapshot()
                with self.assertRaises(ValueError):SessionRecovery(path).restore(target)
                self.assertEqual(target.snapshot(),before)
        source.update(active=False,phase='ready',index=-1)
        with self.assertRaisesRegex(ValueError,'history boundary'):recovery_state(source)

    def test_short_prepared_checkpoint_preserves_verified_plan_identity(self):
        from test_cue_planner import fixture
        from afterhours import ai_mix, cue_planner
        _,report,answer=fixture(3)
        ids={t['id']:'virtualdj:'+identity('short-prepared-'+t['id']) for t in report['tracks']}
        for track in report['tracks']:
            track['id']=ids[track['id']]
            track['engine']={'selection_id':track['id'].removeprefix('virtualdj:'),'folder_id':identity('prepared-folder')}
        for row in answer['selections']:row['trackId']=ids[row['trackId']]
        for row in answer['transitions']:row['fromId']=ids[row['fromId']];row['toId']=ids[row['toId']]
        report['fingerprint']=cue_planner.fingerprint(report['tracks']);review=ai_mix.validate_plan(answer,report,30)
        plan=cue_planner.compile_plan(review,report,report['tracks'],30)
        source=session();source.update(candidates=report['tracks'],order=report['tracks'],analysis=report,aiReview=review,
                                       executionPlan={**plan,'applied':True},mixStyle='phrase',secondsPerTrack=30)
        source.state['setlist']['lockedIds']=[];source.state['setlist']['sourceById']={}
        with tempfile.TemporaryDirectory() as directory,patch('afterhours.cue_planner.verify_receipts') as verify:
            store=SessionRecovery(Path(directory)/'queue.json');store.save(source);target=session();receipt=store.restore(target)
            self.assertTrue(receipt['analysisPreserved']);verify.assert_called_once_with(report)
            rebuilt=cue_planner.compile_plan(target.state['aiReview'],target.state['analysis'],target.state['order'],30)
            self.assertEqual(rebuilt['id'],plan['id']);self.assertIsNone(target.state['executionPlan'])
            self.assertFalse(target.state['active']);self.assertFalse(target.state['aiReview']['appliedToPlayback'])

    def test_playing_set_recovers_queue_without_live_authority(self):
        s=session();s.update(active=True,phase='mixing',index=2,
            telemetry={'decks':[{'playing':True}]},startup={'status':'ready'},
            copilot={'mode':'leading','held':True,'command':{'status':'pending'}},
            executionPlan={'applied':True},mixerBaselines={'1':{'level':0}},
            transitions=[{'private':'runtime only'}])
        with tempfile.TemporaryDirectory() as directory:
            store=SessionRecovery(Path(directory)/'queue.json');self.assertTrue(store.save(s))
            self.assertFalse(store.save(s))
            self.assertEqual(stat.S_IMODE(store.path.stat().st_mode),0o600)
            value=json.loads(store.path.read_text())['session']
            for key in ('active','telemetry','startup','copilot','executionPlan','transitions','mixerBaselines','playbackRequest','allowance'):
                self.assertNotIn(key,value)
            recovered=session();result=store.restore(recovered)
            self.assertTrue(result['restored']);self.assertFalse(recovered.active)
            self.assertEqual(recovered.state['phase'],'stopped');self.assertIsNone(recovered.state['executionPlan'])
            self.assertEqual(recovered.state['order'],s.state['order'])

    def test_unprepared_tail_is_saved_with_its_full_order(self):
        s=session();order=copy.deepcopy(s.state['order'])
        s.update(order=order[:2],analysis={'stale':True},aiReview={'stale':True},
                 queuedUpdate={'tracks':order,'status':'analyzing','sourceById':{t['id']:'planner' for t in order}})
        saved=recovery_state(s)
        self.assertEqual(saved['order'],order);self.assertIsNone(saved['analysis']);self.assertIsNone(saved['aiReview'])
        self.assertEqual(len(saved['setlist']['sourceById']),4)

    def test_fresh_set_does_not_resurrect_the_previous_queue(self):
        s=session()
        with tempfile.TemporaryDirectory() as directory:
            store=SessionRecovery(Path(directory)/'queue.json');store.save(s)
            s.new_set();store.save(s)
            saved=json.loads(store.path.read_text())['session']
            self.assertEqual(saved['order'],[])
            self.assertEqual(saved['candidates'],s.state['candidates'])
            blank=DJSession(threading.Lock());self.assertTrue(store.restore(blank)['restored'])
            self.assertEqual(blank.state['order'],[])
            self.assertEqual(blank.state['candidates'],s.state['candidates'])
            self.assertFalse(blank.state['active'])

    def test_stale_analysis_recovers_only_the_valid_queue(self):
        s=session();saved=recovery_state(s);saved.update(analysis={'invalid':True},aiReview={'decision':'propose'})
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'queue.json';path.write_text(json.dumps({'version':1,'session':saved}))
            recovered=session();result=SessionRecovery(path).restore(recovered)
            self.assertEqual(result['status'],'needs-analysis');self.assertEqual(recovered.state['order'],s.state['order'])
            self.assertIsNone(recovered.state['analysis']);self.assertFalse(recovered.active)

    def test_invalid_identity_and_symlink_do_not_change_a_live_session(self):
        s=session();before=s.snapshot();saved=recovery_state(s);saved['candidates'][0]['engine']['selection_id']='wrong'
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'queue.json';path.write_text(json.dumps({'version':1,'session':saved}))
            with self.assertRaises(ValueError):SessionRecovery(path).restore(s)
            self.assertEqual(s.snapshot(),before)
            link=Path(directory)/'link';link.symlink_to(path)
            with self.assertRaises(ValueError):SessionRecovery(link).save(s)
            with self.assertRaises(ValueError):SessionRecovery(link).restore(s)
            self.assertEqual(json.loads(path.read_text())['session'],saved)

    def test_checkpoint_changes_only_for_recovery_metadata(self):
        s=session()
        with tempfile.TemporaryDirectory() as directory:
            store=SessionRecovery(Path(directory)/'queue.json');store.save(s)
            s.update(telemetry={'sampledAt':123},events=[{'text':'runtime'}],index=2)
            self.assertFalse(store.save(s))
            s.state['setlist']['allowReorder']=False
            self.assertTrue(store.save(s))

    def test_interrupted_write_preserves_previous_checkpoint(self):
        s=session()
        with tempfile.TemporaryDirectory() as directory:
            store=SessionRecovery(Path(directory)/'queue.json');store.save(s)
            before=store.path.read_bytes();s.state['setlist']['allowReorder']=False
            with patch('afterhours.session_recovery.os.replace',side_effect=OSError('disk failure')):
                with self.assertRaises(OSError):store.save(s)
            self.assertEqual(store.path.read_bytes(),before)
            self.assertEqual(list(Path(directory).glob('*.tmp')),[])

    def test_rejected_checkpoint_is_retained_before_a_fresh_one(self):
        with tempfile.TemporaryDirectory() as directory:
            store=SessionRecovery(Path(directory)/'queue.json');store.path.write_text('{invalid')
            with self.assertRaises(ValueError):store.restore(session())
            backup=store.retain_invalid();store.save(session())
            self.assertEqual(backup.read_text(),'{invalid')
            self.assertEqual(json.loads(store.path.read_text())['version'],1)

if __name__=='__main__':unittest.main()
