"""Bound the real provider request with populated session evidence; no network."""
import copy
import json
import unittest
from unittest.mock import Mock, patch

from afterhours.dj_agent import MAX_REQUEST_BYTES, SCHEMA, HANDOFF_INSTRUCTIONS, PERFORMANCE_INSTRUCTIONS, evaluate, music_context, planning_request


def populated_packet():
    tracks=[{'id':'virtualdj:'+format(i,'064x'),
             'title':f'Recording {i} (Extended Mix)',
             'artist':'Daft Punk' if i>=41 else 'Fixture Artist',
             'bpm':126,'camelot':'8A','genre':'House','duration':360,
             'year':None,'remix':None} for i in range(45)]
    cue={'rmsDb':-14.64,'bassDb':-16.71,'highDb':-28.25,
         'crestDb':12.6,'levelVariationDb':0.41,'silentFraction':0.0}
    session={'id':'fixture','phase':'ready','active':False,'index':0,
             'candidates':tracks,'order':tracks[:5],
             'setlist':{'allowReorder':True,'lockedIds':[tracks[1]['id']]},
             'telemetry':{'sampledAt':100,'decks':[
                 {'deck':1,'id':tracks[0]['id'],'playing':True,'bpm':126},
                 {'deck':2,'id':tracks[1]['id'],'playing':False,'bpm':126}]},
             'executionPlan':{'targetBpm':126,'applied':True,'selections':[
                 {'trackId':t['id'],'maxBlendBeats':32,'entry':cue,'exit':cue} for t in tracks[:5]]}}
    request={'id':'latest','role':'user','source':'typed','at':100,
             'text':'lets start a new session based on daft punk'}
    held=[{'trackId':tracks[10]['id'],'title':tracks[10]['title'],'bpm':126,'reason':'Hold for later.'}]
    packet={'operatorRequest':request,'mentionedAgents':[],
            'djProfile':{'id':'fixture-dj','name':'Fixture DJ','genres':['House'],'rules':'Keep vocals clear.'},
            'direction':'A previous house direction. '*12,
            'messages':[{'role':'assistant','text':'Previous analysis. '*100} for _ in range(12)]+[request],
            'audienceRequests':[{'text':'Optional room request. '*20,'at':100}],
            'pastSets':[{'direction':'An older set. '*300}],
            'queueMode':'steer','typedTurn':True,'leadMode':'all',
            'settings':{'nightPlan':None,'allowReorder':True},
            'performanceContext':{'ready':False},
            'reason':'operator message',
            'searchPolicy':{'canSearch':True,'roundsRemaining':2,'openingTarget':3},
            'preparationPolicy':{'automatic':True,'playback':'requested'},
            'music':music_context(session,held,now=100,request=request['text'])}
    return packet


class PlanningRequestTests(unittest.TestCase):
    def test_populated_artist_start_fits_without_losing_current_or_requested_music(self):
        packet=populated_packet();original=copy.deepcopy(packet)
        encoded,sent=planning_request(packet)
        body=json.loads(encoded)
        self.assertLessEqual(len(encoded),MAX_REQUEST_BYTES)
        self.assertEqual(json.loads(body['input']),sent)
        self.assertEqual(packet,original,'Packing must not mutate durable conversation or session data.')
        self.assertEqual(sent['operatorRequest'],original['operatorRequest'])
        self.assertNotIn(sent['operatorRequest'],sent['messages'],'The request already has its own field.')
        for key in ('currentOrder','protectedIds','requestedIds','queueFacts','deckFacts',
                    'tempoPolicy','deferredTracks','lockedIds','allowReorder'):
            self.assertEqual(sent['music'][key],original['music'][key],key)
        self.assertEqual(sent['direction'],original['direction'])
        self.assertEqual(sent['djProfile']['rules'],original['djProfile']['rules'])
        required=set(original['music']['currentOrder']+original['music']['requestedIds']+
                     [t['trackId'] for t in original['music']['deferredTracks']])
        supplied={t['id'] for t in sent['music']['catalog']}
        self.assertTrue(required<=supplied)
        self.assertTrue(supplied<={t['id'] for t in original['music']['catalog']})
        # The compact instructions leave room for useful cues in this envelope.
        self.assertEqual(sent['music']['preparedCues'],original['music']['preparedCues'])
        self.assertNotEqual(SCHEMA['properties']['mixTransitions'],{'type':'null'},'Do not change the shared schema.')

    def test_long_lived_full_queue_and_long_names_fit_without_changing_identity(self):
        for text in ('Start a session based on 90s house.', 'Play Daft Punk.',
                     'Go ahead and transition to the next track.'):
            with self.subTest(text=text):
                p=populated_packet();m=p['music']
                records=copy.deepcopy(m['catalog'][:12])
                for i,row in enumerate(records):
                    row['title']=f'Recording {i} '+('Extended café mix — '*12)
                    row['artist']='Fixture collaboration '+('Artist name, '*5)
                    if i>=8:row['searchMatch']=True
                m['catalog']=records;m['currentOrder']=[t['id'] for t in records]
                m['protectedIds']=m['currentOrder'][:10];m['requestedIds']=m['currentOrder'][-2:]
                m['deferredTracks']=[]
                m['queueFacts']={'current':[{'id':t['id'],'title':t['title']} for t in records],
                                 'staged':[{'id':t['id'],'title':t['title']} for t in records]}
                p['operatorRequest']['text']=text
                p['canRequestStart']=True
                p['djProfile']['rules']='Keep vocals clear; preserve the requested era.'
                p['settings'].update(style={'known':True,'vocal':'instrumental'},discover=True)
                p['searchPolicy'].update(previousQueries=['daft punk'],lastResult={'query':'Daft Punk',
                    'filters':{'artist':'','genre':'','bpmMin':40,'bpmMax':300},
                    'status':'complete','examined':7,'matched':4,'added':4})
                original=copy.deepcopy(p)
                encoded,sent=planning_request(p);music=sent['music']
                self.assertLessEqual(len(encoded),MAX_REQUEST_BYTES)
                self.assertEqual(sent['operatorRequest'],original['operatorRequest'])
                self.assertEqual(sent['djProfile'],original['djProfile'])
                self.assertEqual(sent['settings'],original['settings'])
                self.assertEqual(sent['searchPolicy'],original['searchPolicy'])
                self.assertEqual(json.loads(encoded)['text']['format']['schema']['$defs']['recordingId']['enum'],
                                 [t['id'] for t in music['catalog']])
                self.assertEqual(music['currentOrder'],m['currentOrder'])
                self.assertEqual(music['protectedIds'],m['protectedIds'])
                self.assertEqual(music['requestedIds'],m['requestedIds'])
                restored_catalog=copy.deepcopy(music['catalog'])
                for row in restored_catalog:
                    if 'artistRef' in row:row['artist']=music['catalogArtists'][row.pop('artistRef')]
                self.assertEqual(restored_catalog,[{k:v for k,v in t.items() if v is not None} for t in records])
                self.assertTrue(sent['canRequestStart'])
                action=json.loads(encoded)['text']['format']['schema']['properties']['playbackAction']['anyOf'][1]
                self.assertEqual(action['properties']['requestId'],{'type':'string','enum':[sent['operatorRequest']['id']]})
                names={t['id']:t['title'] for t in music['catalog']}
                for kind in ('current','staged'):
                    rows=music['queueFacts'][kind]
                    if isinstance(rows,dict):
                        self.assertEqual(rows,{'orderRef':'currentOrder'})
                        rows=[{'id':tid} for tid in music['currentOrder']]
                    restored=[{'id':t['id'],'title':t.get('title',names[t['id']])} for t in rows]
                    self.assertEqual(restored,m['queueFacts'][kind])
                self.assertTrue(music['preparedCuesOmitted'])
                self.assertEqual(json.loads(encoded)['text']['format']['schema']['properties']['mixTransitions'],{'type':'null'})
                self.assertEqual(p,original)

    def test_small_request_retains_cues_history_and_false_or_zero_readings(self):
        request={'role':'user','text':'Keep this order — café set.'}
        packet={'operatorRequest':request,'messages':[{'role':'user','text':'No vocals.'},request],
                'searchPolicy':{'canSearch':False},
                'music':{'catalog':[{'id':'a','year':None,'withinTempoLimit':False,'pitchAtSetTempo':0}],
                         'preparedCues':[{'trackId':'a','maxBlendBeats':16}]}}
        encoded,sent=planning_request(packet);body=json.loads(encoded)
        self.assertEqual(sent['operatorRequest'],request)
        self.assertEqual(sent['messages'],[{'role':'user','text':'No vocals.'}])
        self.assertEqual(sent['music']['catalog'],[{'id':'a','withinTempoLimit':False,'pitchAtSetTempo':0}])
        self.assertEqual(sent['music']['preparedCues'],packet['music']['preparedCues'])
        self.assertEqual(body['text']['format']['schema']['properties']['search'],{'type':'null'})
        transition=body['text']['format']['schema']['properties']['mixTransitions']['anyOf'][1]['items']['properties']
        self.assertEqual(transition['fromId'],{'$ref':'#/$defs/recordingId'})
        self.assertEqual(transition['toId'],{'$ref':'#/$defs/recordingId'})
        self.assertEqual(body['text']['format']['schema']['$defs']['recordingId']['enum'],['a'])

    def test_all_recording_fields_share_one_exact_catalog_enum(self):
        original=copy.deepcopy(SCHEMA)
        ids=['virtualdj:'+format(i,'064x') for i in range(3)]
        encoded,sent=planning_request({'music':{'catalog':[{'id':tid} for tid in ids]},
                                      'crowdPolicy':{'canOffer':True}})
        schema=json.loads(encoded)['text']['format']['schema'];references=[]
        def visit(node):
            if isinstance(node,dict):
                for key,value in node.get('properties',{}).items():
                    if key in ('trackId','fromId','toId'):references.append(value)
                    elif key=='trackIds':references.append(value['items'])
                for value in node.values():visit(value)
            elif isinstance(node,list):
                for value in node:visit(value)
        visit(schema)
        self.assertEqual(len(references),8)
        for node in references:
            self.assertEqual(node,{'$ref':'#/$defs/recordingId'})
            resolved=schema
            for key in node['$ref'][2:].split('/'):resolved=resolved[key]
            self.assertEqual(resolved,{'type':'string','enum':ids})
            self.assertNotIn('fabricated-recording',resolved['enum'])
        for tid in ids:self.assertEqual(json.dumps(schema).count(json.dumps(tid)),1)
        self.assertEqual(schema['properties']['trackIds']['maxItems'],12)
        self.assertEqual(schema['properties']['crowdChoice']['anyOf'][1]['properties']['trackIds']['maxItems'],3)
        self.assertEqual(schema['required'],SCHEMA['required']);self.assertFalse(schema['additionalProperties'])
        self.assertEqual(SCHEMA,original)

    def test_start_action_is_bound_to_one_current_eligible_operator_id(self):
        original=copy.deepcopy(SCHEMA)
        for identifier,allowed,reason in (('current-operator-uuid',True,'operator message'),
                ('x'*100,True,'operator message'),('current',False,'operator message'),
                (None,True,'operator message'),('',True,'operator message'),('x'*101,True,'operator message'),
                ('current',True,'choose from catalog results')):
            packet={'operatorRequest':{'id':identifier,'role':'user','text':'Begin a house set.'},
                    'canRequestStart':allowed,'reason':reason,'music':{'catalog':[{'id':'a'},{'id':'b'}]}}
            encoded,sent=planning_request(packet)
            action=json.loads(encoded)['text']['format']['schema']['properties']['playbackAction']
            if allowed and identifier and len(identifier)<=100 and reason=='operator message':
                self.assertEqual(action['anyOf'][1]['properties']['requestId'],{'type':'string','enum':[sent['operatorRequest']['id']]})
                self.assertEqual(action['anyOf'][1]['properties']['action'],{'type':'string','enum':['start-now']})
            else:self.assertEqual(action,{'type':'null'})
        self.assertEqual(SCHEMA,original)

    def test_empty_catalog_has_valid_empty_choices_without_an_empty_enum(self):
        encoded,sent=planning_request({'music':{'catalog':[]},'searchPolicy':{'canSearch':True}})
        schema=json.loads(encoded)['text']['format']['schema']
        self.assertNotIn('$defs',schema);self.assertNotIn('$ref',json.dumps(schema))
        self.assertEqual(schema['properties']['trackIds'],{'type':'array','items':{'type':'string'},'maxItems':0})
        for key in ('selectionReview','crowdChoice','deferredTracks','requestVerdicts','tempoTargets','mixTransitions'):
            self.assertEqual(schema['properties'][key],{'type':'null'})
        self.assertEqual(schema['properties']['search'],SCHEMA['properties']['search'])

    def test_capability_gating_keeps_enabled_character_instructions_and_guards(self):
        for reason,mentions,ready in (('operator message',True,True),('operator message',True,False),
                                       ('operator message',False,True),('operator message',False,False),
                                       ('refill upcoming queue',True,True)):
            p={'reason':reason,'mentionedAgents':[{'id':'albert'}] if mentions else [],
               'performanceContext':{'ready':ready,'referenceMode':True,'castIds':['nova','albert']},
               'music':{'catalog':[{'id':'a'}]}}
            encoded,_=planning_request(p);body=json.loads(encoded);schema=body['text']['format']['schema']
            handoff=reason=='operator message' and mentions;performance=reason=='operator message' and ready
            self.assertEqual(schema['properties']['handoff'],SCHEMA['properties']['handoff'] if handoff else {'type':'null'})
            self.assertEqual(schema['properties']['performance'],SCHEMA['properties']['performance'] if performance else {'type':'null'})
            self.assertEqual(HANDOFF_INSTRUCTIONS in body['instructions'],handoff)
            self.assertEqual(PERFORMANCE_INSTRUCTIONS in body['instructions'],performance)
            if not ready:self.assertIn('nothing was saved or executed',body['instructions'])

    def test_provider_receipt_names_exactly_the_bounded_catalog(self):
        packet=populated_packet()
        packet['djProfile']['rules']='Preserve the requested musical style. '*70
        response=Mock()
        response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        response.read.return_value=json.dumps({'id':'fixture-response','status':'completed',
            'output':[{'type':'message','content':[{'type':'output_text','text':'{}'}]}]}).encode()
        opener=Mock(return_value=response)
        with patch('afterhours.ai_mix.api_key',return_value='fixture-key'):
            _,receipt=evaluate(packet,opener)
        self.assertEqual(opener.call_count,1)
        wire=opener.call_args.args[0].data
        self.assertLessEqual(len(wire),MAX_REQUEST_BYTES)
        sent=json.loads(json.loads(wire)['input'])
        self.assertEqual(receipt['catalogIds'],[t['id'] for t in sent['music']['catalog']])
        schema=json.loads(wire)['text']['format']['schema']
        self.assertEqual(schema['$defs']['recordingId']['enum'],receipt['catalogIds'])
        omitted={t['id'] for t in packet['music']['catalog']}-set(receipt['catalogIds'])
        self.assertTrue(omitted,'Exercise catalog pruning, not just history trimming.')
        self.assertFalse(omitted & set(schema['$defs']['recordingId']['enum']))
        self.assertEqual(receipt['requestBytes'],len(wire))

    def test_oversized_mandatory_context_still_fails_before_any_provider_call(self):
        opener=Mock(side_effect=AssertionError('No provider call expected.'))
        request={'role':'user','text':'x'*MAX_REQUEST_BYTES}
        with patch('afterhours.ai_mix.api_key',return_value='fixture-key'):
            with self.assertRaisesRegex(ValueError,'context is too large'):
                evaluate({'operatorRequest':request,'messages':[request]},opener)
        opener.assert_not_called()


if __name__=='__main__':unittest.main()
