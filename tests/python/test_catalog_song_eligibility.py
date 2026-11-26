"""Catalog identity filtering uses existing queue rules; no native/provider IO."""
import copy
import json
import unittest

from afterhours.dj_agent import SCHEMA, catalog_schema, music_context, planning_request, validate_decision
from afterhours.vdj_session import song_identity, validate_order
from test_dj_agent import Session, decision


class CatalogSongEligibilityTests(unittest.TestCase):
    def variant(self,track,identifier):
        return {**track,'id':identifier,'title':'  '+track['title'].upper()+'  ',
                'artist':'Different release credit','remix':'Remaster'}

    def test_protected_exact_recordings_survive_and_duplicate_releases_do_not(self):
        s=Session();s.update(active=True,phase='playing',index=0)
        a,b,c=s.state['candidates']
        s.state['candidates'] += [self.variant(a,'alternate-a'),self.variant(b,'alternate-b'),
                                  {**c,'id':'different','title':'Another song'}]
        original=copy.deepcopy(s.state)
        music=music_context(s.snapshot(),request='Different release credit')
        ids=[t['id'] for t in music['catalog']]
        self.assertEqual(ids[:3],['a','b','c']);self.assertIn('different',ids)
        self.assertNotIn('alternate-a',ids);self.assertNotIn('alternate-b',ids)
        self.assertEqual(music['protectedIds'],['a','b'])
        self.assertEqual(music['currentOrder'],['a','b','c']);self.assertEqual(s.state,original)

    def test_unplayed_variants_have_one_representative_and_distinct_titles_remain(self):
        s=Session();s.update(order=[],index=-1)
        a=s.state['candidates'][0]
        alternate=self.variant(a,'alternate-a')
        s.state['candidates'].insert(0,alternate)
        music=music_context(s.snapshot(),request='Fixture')
        ids=[t['id'] for t in music['catalog']]
        self.assertIn('a',ids);self.assertNotIn('alternate-a',ids)
        self.assertEqual(len({song_identity(t)[0] for t in music['catalog']}),len(ids))
        schema=catalog_schema(SCHEMA,music['catalog'])
        self.assertEqual(schema['$defs']['recordingId']['enum'],ids)
        self.assertEqual([t['id'] for t in validate_order(['a','b'],s.state['candidates'])],['a','b'])
        with self.assertRaisesRegex(ValueError,'repeats a song title'):
            validate_order(['a','alternate-a'],s.state['candidates'])

    def test_pinned_and_deferred_alternates_remain_evidence_but_cannot_be_selected(self):
        s=Session();s.update(active=True,phase='playing',index=0,setPins=['pinned-a'])
        a,b,c=s.state['candidates']
        s.state['candidates'] += [self.variant(a,'pinned-a'),self.variant(b,'held-b'),self.variant(a,'unrequested-a')]
        held=[{'trackId':'held-b','title':b['title'],'reason':'Requested alternate release.'}]
        music=music_context(s.snapshot(),deferred=held)
        by_id={t['id']:t for t in music['catalog']}
        self.assertNotIn('unrequested-a',by_id)
        self.assertFalse(by_id['pinned-a']['selectionEligible']);self.assertFalse(by_id['held-b']['selectionEligible'])
        encoded,sent=planning_request({'music':music,'crowdPolicy':{'canOffer':True}})
        schema=json.loads(encoded)['text']['format']['schema']
        self.assertEqual(schema['$defs']['selectableId']['enum'],['a','b','c'])
        self.assertEqual(schema['$defs']['recordingId']['enum'],[t['id'] for t in sent['music']['catalog']])
        self.assertEqual(schema['properties']['trackIds']['items'],{'$ref':'#/$defs/selectableId'})
        self.assertEqual(schema['properties']['requestVerdicts'],{'type':'null'})
        self.assertEqual(schema['properties']['deferredTracks']['anyOf'][1]['items']['properties']['trackId'],
                         {'$ref':'#/$defs/recordingId'})
        encoded,_=planning_request({'music':music,'requestCandidates':[{'requestId':'guest-1','trackId':'held-b','asks':1}]})
        with_guest=json.loads(encoded)['text']['format']['schema']['properties']['requestVerdicts']
        self.assertEqual(with_guest['anyOf'][1]['items']['properties']['trackId'],{'$ref':'#/$defs/recordingId'})
        self.assertEqual(schema['properties']['selectionReview']['anyOf'][1]['properties']['options']['items']['properties']['trackId'],
                         {'$ref':'#/$defs/selectableId'})
        result=validate_decision({**decision(['a','b','c']),
            'deferredTracks':[{'trackId':'pinned-a','reason':'This song is already in the protected queue.'}]},s.snapshot())
        self.assertEqual(result['trackIds'],['a','b','c'])

    def test_only_heard_song_evidence_keeps_valid_deferral_schema_and_no_selection(self):
        s=Session();heard=s.state['candidates'][0]
        alternate=self.variant(heard,'pin')
        s.update(order=[],candidates=[alternate],setPins=['pin'])
        music=music_context(s.snapshot(),heard_tracks=[heard])
        self.assertEqual(music['catalog'][0]['id'],'pin')
        self.assertIn('already heard',music['catalog'][0]['selectionReason'])
        schema=catalog_schema(SCHEMA,music['catalog'])
        self.assertEqual(schema['properties']['trackIds']['maxItems'],0)
        self.assertNotIn('selectableId',schema['$defs'])
        self.assertEqual(schema['$defs']['recordingId']['enum'],['pin'])
        for field in ('mixTransitions','tempoTargets','selectionReview','crowdChoice'):
            self.assertEqual(schema['properties'][field],{'type':'null'})

    def test_unavailable_pinned_and_held_records_remain_deferrable_not_selectable(self):
        s=Session();s.update(active=True,phase='playing',index=0,setPins=['pin'])
        a=s.state['candidates'][0]
        s.state['candidates'] += [{**a,'id':tid,'title':tid} for tid in ('pin','held','unrequested')]
        s.state['recordingAvailability']=[{'trackId':tid,'reason':'Provider preview only.'}
                                         for tid in ('pin','held','unrequested')]
        held=[{'trackId':'held','reason':'Waiting for full recording access.'}]
        music=music_context(s.snapshot(),deferred=held)
        by_id={t['id']:t for t in music['catalog']}
        self.assertNotIn('unrequested',by_id)
        for tid in ('pin','held'):
            self.assertFalse(by_id[tid]['selectionEligible'])
            self.assertIn('unavailable',by_id[tid]['selectionReason'])
        schema=catalog_schema(SCHEMA,music['catalog'])
        self.assertEqual(schema['$defs']['selectableId']['enum'],['a','b','c'])
        self.assertEqual(schema['$defs']['recordingId']['enum'],['a','b','c','pin','held'])
        answer={**decision(['a','b','c']),'deferredTracks':held+[{'trackId':'pin','reason':'Provider preview only.'}]}
        self.assertEqual(validate_decision(answer,s.snapshot())['trackIds'],['a','b','c'])
        with self.assertRaisesRegex(ValueError,'currently unavailable'):
            validate_decision(decision(['a','b','pin']),s.snapshot())


if __name__=='__main__':unittest.main()
