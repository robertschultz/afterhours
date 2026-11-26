"""The planner's read-only lookup loop, with a fake provider; never live audio.

Issue #73: before it decides, the planner may look things up in the crate.
Lookups are asked in their own small requests. Each is a real engine
computation, is published as a step while the turn is still running, and
never changes the queue. The decision is then the same bounded request it has
always been, with a short digest of what the lookups showed.
"""
import copy,json,unittest
from unittest.mock import patch

from afterhours import agent_loop
from afterhours import dj_agent
from afterhours import record_crate
from afterhours import session_curation
from afterhours.dj_agent import (DJAgent,evaluate,music_context,planning_request,turn_steps,EvaluationSuperseded,LOOP_REQUESTS,
                      LOOP_SECONDS,LOOP_TURN_CEILING,TEXT_REQUESTS,TEXT_REQUEST_RESERVE,TEXT_TURN_CEILING,MAX_REQUEST_BYTES)
from afterhours.planner_journal import PlannerJournal
from test_dj_agent import Session,decision,settled


def track(i,bpm,camelot,genre='House',title=None,artist='Fixture'):
    return {'id':'t%02d'%i,'title':title or 'Record %d'%i,'artist':artist,'bpm':bpm,'camelot':camelot,'genre':genre}


def crate():
    rows=[track(1,125,'8A',title='Warm Start'),track(2,126,'8A',title='Deep Water',artist='Mira Kade'),
          track(3,127,'9A',title='Late Glass'),track(4,174,'8A','Drum & Bass','Fast One'),
          track(5,125,'2B',title='Far Key'),track(6,124,'8A','Techno','Steel Room')]
    # Each filler has its own title word, so a search can single one out.
    rows+=[track(i,125+(i%3),'8A',title='Filler '+'bcdfg'[i//10]*3+'klmnprstvz'[i%10]*3) for i in range(7,47)]
    return rows


def snapshot(order=('t01','t02')):
    rows=crate()
    return {'id':'fixture','phase':'stopped','active':False,'index':-1,'candidates':rows,
            'order':[r for r in rows if r['id'] in order],'setlist':{'allowReorder':True,'lockedIds':[]},
            'telemetry':{'decks':[]},'maxTempoShift':8}


def lookups(session=None):
    session=session or snapshot()
    return agent_loop.Lookups(session,music_context(session,full=True)['catalog'])


def real_session(queue=4,size=800,guests=0):
    """A crate with 74-character recording IDs, long names and a prepared queue, as the engine really holds."""
    session=snapshot()
    session['candidates']=[track(i,120+(i%12),'8A',title='Record '+'bcdfghjklm'[i%10]*4+'npqrstvwxz'[(i//10)%10]*4+' with a long title',
                                 artist='Fixture collaboration artist name') for i in range(1,size+1)]
    for row in session['candidates']:row['id']='virtualdj:'+row['id'].ljust(64,'0')
    session['order']=session['candidates'][:queue]
    cue={'maxBlendBeats':32,'entry':{'rmsDb':-9.0,'bassDb':-12.0,'highDb':-20.0},'exit':{'rmsDb':-9.0,'bassDb':-12.0,'highDb':-20.0}}
    session['executionPlan']={'applied':True,'selections':[{'trackId':t['id'],**cue} for t in session['order']],'transitions':[]}
    request=packet(session)
    request['requestCandidates']=[{'requestId':'g%d'%i,'trackId':session['order'][min(queue-1,i)]['id'],'asks':2} for i in range(guests)]
    return session,request


class Reply:
    def __init__(self,body):self.body=json.dumps(body).encode()
    def __enter__(self):return self
    def __exit__(self,*args):return False
    def read(self,limit=-1):return self.body


class Provider:
    """Answers each request from a script and keeps what it was sent."""
    def __init__(self,*script):self.script=list(script);self.requests=[]
    def __call__(self,request,timeout=None):
        body=json.loads(request.data);self.requests.append((body,len(request.data)))
        step=self.script.pop(0) if len(self.script)>1 else self.script[0]
        return Reply(step(body) if callable(step) else step)
    def packets(self):return [json.loads(body['input']) for body,_ in self.requests]
    def explores(self):return [body for body,_ in self.requests if 'tools' in body]
    def decisions(self):return [body for body,_ in self.requests if 'text' in body]


def call(name,arguments,call_id='c1'):
    return {'type':'function_call','name':name,'arguments':json.dumps(arguments),'call_id':call_id}


def asks(*calls,tokens=(100,20)):
    return {'id':'ask','status':'completed','output':list(calls),'usage':{'input_tokens':tokens[0],'output_tokens':tokens[1]}}


def ready(tokens=(50,5)):
    return {'id':'ready','status':'completed','usage':{'input_tokens':tokens[0],'output_tokens':tokens[1]},
            'output':[{'type':'message','content':[{'type':'output_text','text':'READY'}]}]}


def decides(ids=None,tokens=(200,40)):
    return {'id':'final','status':'completed','usage':{'input_tokens':tokens[0],'output_tokens':tokens[1]},
            'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps(full_decision(ids))}]}]}


def full_decision(ids=None):
    value=decision(ids)
    for key in dj_agent.SCHEMA['properties']:value.setdefault(key,None)
    return value


def packet(session=None):
    session=session or snapshot()
    return {'reason':'operator message','typedTurn':True,'messages':[],'direction':'Steady house.',
            'operatorRequest':{'id':'m1','role':'user','text':'something deeper'},
            'music':music_context(session),'searchPolicy':{'canSearch':False}}


SEARCH={'text':None,'genre':'house','bpmMin':124,'bpmMax':127,'limit':8}


class LookupTests(unittest.TestCase):
    def test_search_filters_the_whole_crate_and_orders_by_tempo_fit(self):
        book=lookups();result=book.run('search_catalog',{'text':None,'genre':'house','bpmMin':124,'bpmMax':126,'limit':3})
        self.assertEqual(result['total'],sum(1 for r in crate() if r['genre']=='House' and 124<=r['bpm']<=126))
        self.assertEqual(result['crateSize'],len(crate()));self.assertEqual(len(result['matches']),3)
        self.assertEqual(set(result['matches'][0]),{'id','title','artist','bpm','camelot','genre'})
        self.assertTrue(all(row['genre']=='House' and 124<=row['bpm']<=126 for row in result['matches']))
        shifts=[abs(book.rows[row['id']]['pitchAtSetTempo']) for row in result['matches']]
        self.assertEqual(shifts,sorted(shifts))

    def test_search_reaches_records_the_bounded_planning_view_left_out(self):
        session=snapshot();bounded={row['id'] for row in music_context(session)['catalog']}
        self.assertLess(len(bounded),len(session['candidates']))
        hidden=next(r for r in session['candidates'] if r['id'] not in bounded)
        result=lookups(session).run('search_catalog',{'text':hidden['title'],'genre':None,'bpmMin':None,'bpmMax':None,'limit':8})
        self.assertEqual(result['matches'][0]['id'],hidden['id'])

    def test_search_words_match_names_and_report_what_the_crate_does_not_carry(self):
        found=lookups().run('search_catalog',{'text':'mira deep','genre':None,'bpmMin':None,'bpmMax':None,'limit':8})
        self.assertEqual([row['id'] for row in found['matches']],['t02'])
        # The first live run searched for "deeper house" and found nothing.
        search={'text':'deeper house','genre':None,'bpmMin':124,'bpmMax':127,'limit':8}
        book=lookups();mood=book.run('search_catalog',search)
        self.assertGreater(mood['total'],0);self.assertEqual(mood['wordsNotInCrate'],['deeper'])
        self.assertTrue(book.describe('search_catalog',search,mood)[1].endswith(' · not catalogued: deeper'))
        nothing=lookups().run('search_catalog',{'text':'nothing like this','genre':None,'bpmMin':None,'bpmMax':None,'limit':8})
        self.assertEqual(nothing['total'],0);self.assertEqual(nothing['wordsNotInCrate'],['like','nothing','this'])
        self.assertEqual(set(lookups().run('search_catalog',{'text':'the','genre':None,'bpmMin':None,'bpmMax':None,'limit':8})),{'error'})

    def test_what_can_follow_is_ranked_by_key_then_tempo_and_stays_in_the_tempo_limit(self):
        book=lookups();result=book.run('suggest_next',{'afterId':'t01','limit':8})
        ids=[row['id'] for row in result['matches']]
        self.assertNotIn('t01',ids);self.assertNotIn('t04',ids)
        self.assertTrue(all(abs(row['tempoChangePercent'])<=8 for row in result['matches']))
        self.assertTrue(all(row['harmonicCompatible'] for row in result['matches']))
        changes=[abs(row['tempoChangePercent']) for row in result['matches']]
        self.assertEqual(changes,sorted(changes))
        self.assertEqual(result['playable'],sum(1 for r in crate() if r['id']!='t01' and abs((125/r['bpm']-1)*100)<=8))
        self.assertEqual(set(result['matches'][0])-{'relationships'},{'id','title','artist','bpm','camelot','genre','tempoChangePercent','harmonicCompatible','preparedCue','queuePosition'})
        self.assertTrue(any('sharedGenre' in row.get('relationships',[]) for row in result['matches']))
        text,detail=book.describe('suggest_next',{'afterId':'t01','limit':8},result)
        self.assertEqual(text,'Looked for what can follow Warm Start')
        self.assertEqual(detail,f" · {result['playable']} within the tempo limit · {result['compatibleKey']} in a compatible key")

    def test_transition_check_reports_the_engines_own_tempo_and_key_facts(self):
        close=lookups().run('check_transition',{'fromId':'t01','toId':'t02'})
        self.assertEqual(close['tempoChangePercent'],round((125/126-1)*100,2))
        self.assertTrue(close['pairWithinTempoLimit']);self.assertTrue(close['harmonicCompatible'])
        self.assertEqual(close['technique'],'House blend');self.assertIn(close['blendBeats'],(64,128))
        far=lookups().run('check_transition',{'fromId':'t01','toId':'t04'})
        self.assertFalse(far['pairWithinTempoLimit']);self.assertLess(far['tempoChangePercent'],-8)
        self.assertFalse(lookups().run('check_transition',{'fromId':'t01','toId':'t05'})['harmonicCompatible'])

    def test_an_unknown_key_is_reported_as_unknown_not_as_a_clash(self):
        session=snapshot();session['candidates'][2]['camelot']=None
        book=lookups(session);check={'fromId':'t01','toId':'t03'};result=book.run('check_transition',check)
        self.assertIsNone(result['harmonicCompatible'])
        self.assertIn(' · key unknown · ',book.describe('check_transition',check,result)[1])

    def test_the_blend_reported_is_the_one_this_set_would_use(self):
        session=snapshot();session['mixStyle']='quick'
        quick=lookups(session).run('check_transition',{'fromId':'t01','toId':'t02'})
        self.assertEqual((quick['technique'],quick['blendBeats']),('Quick crossfade',8))
        # Second live run: 30 seconds per track and no plan yet made every blend check fail.
        short=snapshot();short['secondsPerTrack']=30
        trial=lookups(short).run('check_transition',{'fromId':'t01','toId':'t02'})
        self.assertNotIn('error',trial);self.assertLessEqual(trial['blendBeats'],32)
        nothing=snapshot();nothing['secondsPerTrack']=30
        for row in nothing['candidates']:row['bpm']=None
        self.assertNotIn('error',agent_loop.Lookups(nothing,nothing['candidates']).run('check_transition',{'fromId':'t01','toId':'t02'}))

    def test_a_pair_already_planned_reports_the_transition_that_will_run(self):
        session=snapshot()
        session['executionPlan']={'applied':True,'orderIds':['t01','t02'],'selections':[],
                                  'transitions':[{'label':'Restrained bass swap','fadeBeats':16}]}
        planned=lookups(session).run('check_transition',{'fromId':'t01','toId':'t02'})
        self.assertEqual((planned['technique'],planned['blendBeats'],planned['basis']),('Restrained bass swap',16,'the applied mix plan for this pair'))
        other=lookups(session).run('check_transition',{'fromId':'t02','toId':'t01'})
        self.assertNotEqual(other['basis'],'the applied mix plan for this pair')
        session['executionPlan']['applied']=False
        self.assertNotEqual(lookups(session).run('check_transition',{'fromId':'t01','toId':'t02'})['blendBeats'],16)

    def test_transition_check_includes_a_prepared_cue_limit_only_when_one_exists(self):
        session=snapshot()
        self.assertNotIn('maxBlendBeats',lookups(session).run('check_transition',{'fromId':'t01','toId':'t02'}))
        session['executionPlan']={'selections':[{'trackId':'t01','maxBlendBeats':32},{'trackId':'t02','maxBlendBeats':16}]}
        self.assertEqual(lookups(session).run('check_transition',{'fromId':'t01','toId':'t02'})['maxBlendBeats'],16)

    def test_compare_sets_recordings_side_by_side(self):
        result=lookups().run('compare_tracks',{'trackIds':['t02','t03','t05']})
        self.assertEqual([row['id'] for row in result['tracks']],['t02','t03','t05'])
        self.assertEqual(result['tracks'][0]['queuePosition'],1);self.assertIsNone(result['tracks'][1]['queuePosition'])
        self.assertEqual((result['tracks'][2]['camelot'],result['tracks'][2]['bpm']),('2B',125))
        self.assertEqual(set(result['tracks'][0])-{'id','title','artist','bpm','camelot','genre'},
                         {'queuePosition','selectionEligible','withinSetTempoLimit','preparedCue','analysed'})

    def test_inspecting_audio_reports_only_what_was_measured(self):
        session=snapshot()
        book=lookups(session);blank=book.run('inspect_track',{'trackId':'t02'})
        self.assertEqual((blank['analysed'],set(blank)),(False,{'trackId','analysed','note'}))
        self.assertEqual(book.describe('inspect_track',{'trackId':'t02'},blank),('Read the audio of Deep Water',' · not analysed yet'))
        contour=[[t,4.0,-20.0 if t<48 else -8.0,-30.0 if t<48 else -9.0,-21.0] for t in range(0,384,4)]
        cue={'id':'cue-one','entrySeconds':60.47,'exitSeconds':90.7,'introEndSeconds':75.5,'maxBlendBeats':32,
             'entry':{'rmsDb':-11.5,'bassDb':-16.3},'exit':{'rmsDb':-7.7,'bassDb':-9.6}}
        session['analysis']={'tracks':[{'id':'t02','duration':384.0,'energyMap':{'contour':contour},
                                        'cueCandidates':[cue,{**cue,'id':'cue-two','entrySeconds':120.0,'exitSeconds':150.0},{'id':'broken'}]}]}
        session['executionPlan']={'selections':[{'trackId':'t02','id':'cue-two','maxBlendBeats':32}]}
        book=lookups(session);seen=book.run('inspect_track',{'trackId':'t02'})
        self.assertTrue(seen['analysed']);self.assertEqual(seen['durationSeconds'],384);self.assertEqual(len(seen['profile']),8)
        self.assertEqual(seen['profile'][0],{'fromSeconds':0,'toSeconds':48,'levelDb':-20.0,'bassDb':-30.0})
        self.assertEqual([w['id'] for w in seen['mixWindows']],['cue-one','cue-two'])
        self.assertEqual(seen['mixWindows'][0]['entrySeconds'],60.5);self.assertEqual(seen['mixWindows'][0]['exitBassDb'],-9.6)
        self.assertEqual(seen['plannedWindowId'],'cue-two');self.assertNotIn('vocalsDetected',seen)
        self.assertIn('not vocal',seen['basis'])
        self.assertEqual(book.describe('inspect_track',{'trackId':'t02'},seen),
                         ('Read the audio of Deep Water',' · 2 mix windows · quietest 0:00–0:48'))

    def test_a_recording_with_every_mix_window_the_engine_allows_still_gets_an_answer(self):
        session,_=real_session();tid=session['order'][0]['id']
        contour=[[t,4.0,-9.0,-12.0,-21.0] for t in range(0,384,4)]
        cues=[{'id':'cue%020d'%i,'entrySeconds':30.0*i,'exitSeconds':30.0*i+30,'maxBlendBeats':32,
               'entry':{'rmsDb':-11.55,'bassDb':-16.31},'exit':{'rmsDb':-7.76,'bassDb':-9.66}} for i in range(8)]
        session['analysis']={'tracks':[{'id':tid,'duration':384.0,'energyMap':{'contour':contour},'cueCandidates':cues}]}
        session['executionPlan']['selections'][0]['id']=cues[7]['id']
        seen=lookups(session).run('inspect_track',{'trackId':tid})
        self.assertNotIn('error',seen);self.assertEqual(seen['mixWindowCount'],8)
        self.assertLessEqual(len(json.dumps(seen)),agent_loop.MAX_RESULT_BYTES)
        self.assertIn(cues[7]['id'],[w['id'] for w in seen['mixWindows']])      # the planned window is the last to go

    def test_answers_fit_with_real_length_recording_ids_and_long_names(self):
        session,_=real_session();book=lookups(session);first=session['candidates'][0]['id']
        for name,arguments in (('search_catalog',{'text':None,'genre':None,'bpmMin':None,'bpmMax':None,'limit':8}),
                               ('suggest_next',{'afterId':first,'limit':8}),
                               ('compare_tracks',{'trackIds':[r['id'] for r in session['candidates'][:4]]}),
                               ('check_transition',{'fromId':first,'toId':session['candidates'][1]['id']})):
            result=book.run(name,arguments)
            self.assertNotIn('error',result,name);self.assertLessEqual(len(json.dumps(result)),agent_loop.MAX_RESULT_BYTES,name)
        self.assertLessEqual(len(book.surfaced),agent_loop.MAX_SURFACED)

    def test_bad_requests_are_answers_not_crashes(self):
        for name,arguments in (('search_catalog',{'limit':'many'}),('check_transition',{'fromId':'t01','toId':'missing'}),
                               ('compare_tracks',{'trackIds':['t01']}),('compare_tracks',{'trackIds':['t01','nope']}),
                               ('compare_tracks',{'trackIds':['t01','t02','t03','t05','t06']}),
                               ('compare_tracks',{'trackIds':[['t01'],'t02']}),('check_transition',{'fromId':['t01'],'toId':{}}),
                               ('inspect_track',{'trackId':['t01']}),('suggest_next',{'afterId':{'a':1},'limit':3}),
                               ('delete_everything',{}),('search_catalog','not an object')):
            result=lookups().run(name,arguments)
            self.assertEqual(set(result),{'error'},name);self.assertLessEqual(len(result['error']),160)
            self.assertNotIn('unhashable',result['error'])

    def test_asking_the_same_thing_twice_is_refused_without_repeating_the_work(self):
        book=lookups();ask={'trackIds':['t01','t02']}
        self.assertIn('tracks',book.run('compare_tracks',ask))
        again=book.run('compare_tracks',dict(ask))
        self.assertEqual(set(again),{'error'});self.assertIn('Already looked up',again['error'])
        self.assertIn('tracks',book.run('compare_tracks',{'trackIds':['t01','t03']}))

    def test_public_step_wording_states_what_was_looked_up(self):
        book=lookups()
        text,detail=book.describe('search_catalog',SEARCH,book.run('search_catalog',SEARCH))
        self.assertEqual(text,'Searched the crate · house · 124–127 BPM');self.assertRegex(detail,r'^ · \d+ of \d+ fit · showing \d+$')
        check={'fromId':'t01','toId':'t02'}
        self.assertEqual(book.describe('check_transition',check,book.run('check_transition',check)),
                         ('Checked the blend · Warm Start → Deep Water',' · -0.79% tempo · compatible key · House blend'))
        compare={'trackIds':['t02','t03']}
        self.assertEqual(book.describe('compare_tracks',compare,book.run('compare_tracks',compare)),
                         ('Compared 2 records',' · Deep Water · Late Glass'))
        self.assertEqual(book.describe('check_transition',{'fromId':'t01','toId':'zz'},{'error':'Unknown recording.'}),
                         ('A lookup could not be answered',' · Unknown recording.'))

    def test_the_planners_own_sentence_leads_and_the_engines_finding_follows(self):
        book=lookups();check={'why':'Seeing whether Deep Water can sit under Warm Start without a tempo push','fromId':'t01','toId':'t02'}
        result=book.run('check_transition',check)
        self.assertNotIn('error',result)
        self.assertEqual(book.narrate('check_transition',check,result),
                         ('Seeing whether Deep Water can sit under Warm Start without a tempo push.',
                          ' · Warm Start → Deep Water · -0.79% tempo · compatible key · House blend'))
        follow={'why':'What keeps this groove going next?','afterId':'t01','limit':8};found=book.run('suggest_next',follow)
        said,finding=book.narrate('suggest_next',follow,found)
        self.assertEqual(said,'What keeps this groove going next?')
        self.assertEqual(finding,f" · {found['playable']} within the tempo limit · {found['compatibleKey']} in a compatible key")
        # The digest for the decision stays the engine's factual line.
        self.assertEqual(book.describe('check_transition',check,result)[0],'Checked the blend · Warm Start → Deep Water')

    def test_an_unusable_sentence_falls_back_to_the_factual_line(self):
        book=lookups()
        for i,why in enumerate((None,'','ok',42,'Looking at virtualdj:'+'a'*64,'x'*40+' '+'ab12'*10,['list'])):
            check={'why':why,'fromId':'t01','toId':'t%02d'%(i+2)};result=book.run('check_transition',check)
            self.assertEqual(book.narrate('check_transition',check,result),book.describe('check_transition',check,result),repr(why))
        long={'why':'A very long thought '*20,'fromId':'t01','toId':'t20'}
        said,_=book.narrate('check_transition',long,book.run('check_transition',long))
        self.assertLessEqual(len(said),121);self.assertNotIn('\n',said)
        quoted={'why':'The operator said "play something deeper" so\nI am checking','fromId':'t01','toId':'t21'}
        said,_=book.narrate('check_transition',quoted,book.run('check_transition',quoted))
        self.assertNotIn('"',said);self.assertNotIn('\n',said)
        failed={'why':'Checking a record that is not there','fromId':'t01','toId':'zz'}
        self.assertEqual(book.narrate('check_transition',failed,book.run('check_transition',failed))[0],'A lookup could not be answered')

    def test_the_sentence_is_not_part_of_the_question(self):
        book=lookups()
        self.assertIn('tracks',book.run('compare_tracks',{'why':'First look','trackIds':['t01','t02']}))
        again=book.run('compare_tracks',{'why':'Looking again with other words','trackIds':['t01','t02']})
        self.assertIn('Already looked up',again['error'])

    def test_tool_definitions_are_strict_and_cover_exactly_the_lookups(self):
        self.assertEqual([tool['name'] for tool in agent_loop.TOOL_DEFS],
                         ['search_catalog','suggest_next','compare_tracks','check_transition','inspect_track'])
        for tool in agent_loop.TOOL_DEFS:
            self.assertEqual(tool['type'],'function');self.assertIs(tool['strict'],True)
            schema=tool['parameters']
            self.assertFalse(schema['additionalProperties']);self.assertEqual(set(schema['required']),set(schema['properties']))
            self.assertEqual(list(schema['properties'])[0],'why');self.assertEqual(schema['properties']['why']['type'],'string')


class DigestRoomTests(unittest.TestCase):
    """Third review of #86: what the lookups found must reach the decision."""
    def test_the_digest_is_kept_ahead_of_most_surfaced_recordings(self):
        for queue,size in ((6,48),(4,800),(2,48)):
            session,request=real_session(queue=queue,size=size)
            if not dj_agent.room_for_lookups(copy.deepcopy(request)):continue
            book=lookups(session);ids=[row['id'] for row in session['candidates']];transcript=[]
            for n in range(9):
                arguments={'afterId':ids[n],'limit':8};result=book.run('suggest_next',arguments)
                self.assertNotIn('error',result)
                transcript.append({'tool':'suggest_next','arguments':arguments,'result':result,'summary':' · '.join(book.describe('suggest_next',arguments,result))})
            encoded,sized=planning_request(copy.deepcopy(request),book,transcript)
            self.assertLessEqual(len(encoded),dj_agent.MAX_REQUEST_BYTES)
            self.assertEqual(len(sized.get('lookups',[])),9,(queue,size))
            self.assertIn(agent_loop.DIGEST_INSTRUCTIONS,json.loads(encoded)['instructions'])
            # The newest finds are still selectable.
            supplied={row['id'] for row in sized['music']['catalog']}
            self.assertTrue(set(book.surfaced[-4:])<=supplied,(queue,size))

    def test_room_for_lookups_counts_the_digest_as_well_as_the_rows(self):
        session,request=real_session(queue=4,size=800)
        self.assertTrue(dj_agent.room_for_lookups(copy.deepcopy(request)))
        with patch.object(dj_agent,'MAX_REQUEST_BYTES',len(planning_request(copy.deepcopy(request))[0])-200):
            pass    # shape only: the function must not raise for a tighter limit
        session,request=real_session(queue=12,size=800,guests=8)
        # Compacted instructions can leave this same fixture more room. Test
        # the digest reservation directly instead of depending on prose size.
        with patch.object(dj_agent,'DIGEST_RESERVE_BYTES',dj_agent.MAX_REQUEST_BYTES):
            self.assertFalse(dj_agent.room_for_lookups(copy.deepcopy(request)))

    def test_saying_how_many_are_shown_never_turns_a_fitting_answer_into_a_refusal(self):
        refused=0
        for width in range(20,80):
            session=snapshot()
            for n,row in enumerate(session['candidates']):row.update(title='W'*(width-4)+'%04d'%n,artist='M'*(width//2),id='virtualdj:'+row['id'].ljust(64,'0'))
            session['order']=session['candidates'][:2];book=lookups(session)
            result=book.run('suggest_next',{'afterId':session['candidates'][5]['id'],'limit':8})
            refused+='error' in result
            if 'error' not in result:
                self.assertLessEqual(len(json.dumps(result,ensure_ascii=False)),agent_loop.MAX_RESULT_BYTES)
                self.assertEqual(result.get('shown',len(result['matches'])),len(result['matches']))
        self.assertEqual(refused,0)

    def test_a_repeated_lookup_is_refused_without_claiming_its_answer_is_still_shown(self):
        book=lookups();book.run('check_transition',{'fromId':'t01','toId':'t02'})
        again=book.run('check_transition',{'fromId':'t01','toId':'t02'})
        self.assertIn('Already looked up',again['error']);self.assertNotIn('is in lookups',again['error'])

    def test_suggestions_with_the_longest_names_are_shortened_not_refused(self):
        session=snapshot()
        for n,row in enumerate(session['candidates']):row.update(title='W'*76+'%04d'%n,artist='M'*60,genre='G'*40,id='virtualdj:'+row['id'].ljust(64,'0'))
        session['order']=session['candidates'][:2]
        book=lookups(session);result=book.run('suggest_next',{'afterId':session['candidates'][5]['id'],'limit':8})
        self.assertNotIn('error',result)
        self.assertGreaterEqual(len(result['matches']),4)
        self.assertLessEqual(len(json.dumps(result,ensure_ascii=False)),agent_loop.MAX_RESULT_BYTES)
        self.assertEqual(result['shown'],len(result['matches']))
        self.assertLess(result['shown'],8)


class ExploreRequestTests(unittest.TestCase):
    def test_it_is_a_small_lookup_only_request_with_the_ask_and_the_queue(self):
        session=snapshot();book=lookups(session)
        body=json.loads(agent_loop.explore_request(packet(session),book,[],3,MAX_REQUEST_BYTES))
        self.assertEqual(body['tools'],agent_loop.TOOL_DEFS);self.assertNotIn('text',body);self.assertIs(body['store'],False)
        sent=json.loads(body['input'])
        self.assertEqual(sent['operatorRequest']['text'],'something deeper');self.assertEqual(sent['direction'],'Steady house.')
        self.assertEqual([row['id'] for row in sent['queue']],['t01','t02'])
        self.assertEqual(sent['queue'][0]['position'],0);self.assertEqual(sent['crateSize'],len(book.rows))
        self.assertEqual((sent['lookups'],sent['lookupsRemaining']),([],3))
        self.assertNotIn('catalog',json.dumps(sent))
        self.assertLess(len(json.dumps(body)),7000)

    def test_the_booth_voice_is_asked_for_and_follows_the_selected_dj(self):
        session=snapshot();request=packet(session);request['djProfile']={'name':'Nova','persona':'Dry, warm, a little cheeky. '*80}
        body=json.loads(agent_loop.explore_request(request,lookups(session),[],3,MAX_REQUEST_BYTES))
        self.assertIn('mutters to a friend in the booth',body['instructions']);self.assertIn('At most 14 words',body['instructions'])
        self.assertIn('never how you are checking it',body['instructions'])
        profile=json.loads(body['input'])['djProfile']
        self.assertIn('Nova',profile);self.assertLessEqual(len(profile),600)

    def test_it_carries_every_answer_until_the_oldest_must_give_way(self):
        session,request=real_session();book=lookups(session);transcript=[]
        for i in range(12):
            ask={'text':None,'genre':None,'bpmMin':120+i*.5,'bpmMax':132,'limit':8}
            transcript.append({'tool':'search_catalog','arguments':ask,'result':book.run('search_catalog',ask),'summary':'x'})
        encoded=agent_loop.explore_request(request,book,transcript,0,MAX_REQUEST_BYTES)
        self.assertLessEqual(len(encoded),MAX_REQUEST_BYTES)
        kept=json.loads(json.loads(encoded)['input'])['lookups']
        self.assertGreaterEqual(len(kept),6);self.assertEqual(kept[-1]['arguments'],transcript[-1]['arguments'])
        self.assertNotIn('summary',kept[0])


class LoopTests(unittest.TestCase):
    def run_loop(self,provider,session=None,steps=None,book='default',request=None,**options):
        session=session or snapshot();request=request or packet(session)
        book=lookups(session) if book=='default' else book
        with patch('afterhours.ai_mix.api_key',return_value='fixture-key'):
            return evaluate(request,provider,lookups=book,on_step=(steps.append if steps is not None else None),**options)

    def test_lookups_are_asked_first_and_the_decision_reads_their_digest(self):
        steps=[];provider=Provider(asks(call('search_catalog',SEARCH)),ready(),decides(['t01','t03']))
        result,receipt=self.run_loop(provider,steps=steps)
        self.assertEqual(result['trackIds'],['t01','t03'])
        self.assertEqual((len(provider.requests),receipt['requests'],receipt['lookups']),(3,3,1))
        self.assertEqual(len(provider.explores()),2);self.assertEqual(len(provider.decisions()),1)
        first,second,final=provider.packets()
        self.assertEqual(first['lookups'],[])
        self.assertEqual(second['lookups'][0]['tool'],'search_catalog');self.assertTrue(second['lookups'][0]['result']['matches'])
        self.assertEqual(final['lookups'],[steps[0][0]+steps[0][1]])
        self.assertEqual(steps[0][0],'Searched the crate · house · 124–127 BPM')
        self.assertIn('lookups lists engine facts',provider.decisions()[0]['instructions'])
        self.assertEqual(receipt['usage'],{'input_tokens':350,'output_tokens':65})

    def test_the_operator_reads_the_planners_reasoning_and_the_decision_reads_the_facts(self):
        steps=[];ask={'why':'Hunting for something a touch deeper around the same tempo','text':None,'genre':'house','bpmMin':124,'bpmMax':127,'limit':8}
        provider=Provider(asks(call('search_catalog',ask)),ready(),decides(['t01','t03']))
        self.run_loop(provider,steps=steps)
        self.assertEqual(steps[0][0],'Hunting for something a touch deeper around the same tempo.')
        self.assertRegex(steps[0][1],r'^ · house · 124–127 BPM · \d+ of \d+ fit · showing \d+$')
        _,second,final=provider.packets()
        self.assertNotIn('why',second['lookups'][0]['arguments'])
        self.assertTrue(final['lookups'][0].startswith('Searched the crate · house · 124–127 BPM · '))
        self.assertNotIn('Hunting',json.dumps(final))

    def test_nothing_to_look_up_costs_one_small_request_and_the_decision(self):
        provider=Provider(ready(),decides())
        result,receipt=self.run_loop(provider)
        self.assertEqual((len(provider.requests),receipt['requests'],receipt['lookups']),(2,2,0))
        self.assertLess(provider.requests[0][1],6000)
        self.assertNotIn('lookups',provider.packets()[1]);self.assertNotIn('lookups lists',provider.decisions()[0]['instructions'])

    def test_the_decision_request_is_the_one_it_always_was_when_nothing_was_looked_up(self):
        session=snapshot();request=packet(session);provider=Provider(ready(),decides())
        self.run_loop(provider,session=session,request=request)
        alone,_=planning_request(request)
        self.assertEqual(provider.decisions()[0],json.loads(alone))

    def test_a_model_that_keeps_asking_is_stopped_and_still_has_to_decide(self):
        provider=Provider(*[asks(call('check_transition',{'fromId':'t01','toId':'t%02d'%i})) for i in range(2,9)],decides(['t01','t02']))
        provider.script=provider.script[:LOOP_REQUESTS-1]+[decides(['t01','t02'])]
        _,receipt=self.run_loop(provider)
        self.assertEqual(receipt['requests'],LOOP_REQUESTS);self.assertEqual(len(provider.explores()),LOOP_REQUESTS-1)
        self.assertNotIn('tools',provider.requests[-1][0])
        self.assertEqual([p['lookupsRemaining'] for p in provider.packets()[:-1]],list(range(LOOP_REQUESTS-2,-1,-1)))

    def test_records_found_by_a_lookup_can_be_chosen(self):
        session=snapshot();bounded={row['id'] for row in music_context(session)['catalog']}
        hidden=next(r for r in session['candidates'] if r['id'] not in bounded)
        search={'text':hidden['title'],'genre':None,'bpmMin':None,'bpmMax':None,'limit':1}
        provider=Provider(asks(call('search_catalog',search)),ready(),decides(['t01',hidden['id']]))
        _,receipt=self.run_loop(provider,session=session)
        self.assertIn(hidden['id'],receipt['catalogIds'])
        final=provider.decisions()[0]
        self.assertIn(hidden['id'],json.dumps(final['text']['format']['schema']))
        self.assertIn(hidden['id'],[row['id'] for row in json.loads(final['input'])['music']['catalog']])

    def test_one_round_answers_a_bounded_number_of_lookups(self):
        steps=[];many=[call('check_transition',{'fromId':'t01','toId':'t0%d'%i},'c%d'%i) for i in range(2,7)]
        _,receipt=self.run_loop(Provider(asks(*many),ready(),decides(['t01','t02'])),steps=steps)
        self.assertEqual(receipt['lookups'],agent_loop.MAX_CALLS_PER_REQUEST);self.assertEqual(len(steps),agent_loop.MAX_CALLS_PER_REQUEST)

    def test_a_malformed_lookup_is_reported_back_instead_of_ending_the_turn(self):
        steps=[];broken={'type':'function_call','name':'search_catalog','arguments':'{not json','call_id':'c1'}
        provider=Provider(asks(broken),ready(),decides(['t01','t02']))
        result,_=self.run_loop(provider,steps=steps)
        self.assertEqual(result['trackIds'],['t01','t02'])
        self.assertEqual(set(provider.packets()[1]['lookups'][0]['result']),{'error'})
        self.assertEqual(steps[0][0],'A lookup could not be answered')

    def test_without_a_lookup_book_or_allowance_it_is_the_single_bounded_request(self):
        for options,book in (({},None),({'allowed':1},'default')):
            provider=Provider(decides())
            self.run_loop(provider,book=book,**options)
            self.assertEqual(len(provider.requests),1);self.assertNotIn('tools',provider.requests[0][0])

    def test_a_smaller_allowance_caps_the_requests_of_one_evaluation(self):
        provider=Provider(asks(call('search_catalog',SEARCH)),decides(['t01','t02']))
        _,receipt=self.run_loop(provider,allowed=2)
        self.assertEqual(receipt['requests'],2);self.assertNotIn('tools',provider.requests[-1][0])

    def test_lookups_never_cost_the_decision_any_engine_evidence(self):
        # Real-length IDs and a prepared queue: whatever the decision request
        # holds without lookups, it still holds after them.
        for queue,guests in ((2,0),(4,0),(6,0),(6,8)):
            with self.subTest(queue=queue,guests=guests):
                session,request=real_session(queue=queue,guests=guests);book=lookups(session)
                _,alone=planning_request(request)
                rounds=[asks(*[call('suggest_next',{'afterId':session['candidates'][r*3+i]['id'],'limit':8},'c%d%d'%(r,i)) for i in range(3)])
                        for r in range(LOOP_REQUESTS-1)]
                provider=Provider(*rounds,decides([t['id'] for t in session['order']]))
                _,receipt=self.run_loop(provider,session=session,request=request,book=book)
                self.assertTrue(all(size<=MAX_REQUEST_BYTES for _,size in provider.requests))
                final=json.loads(provider.decisions()[0]['input'])
                self.assertEqual(final['music']['preparedCues'],alone['music']['preparedCues'])
                self.assertEqual(final['music'].get('preparedCuesOmitted'),alone['music'].get('preparedCuesOmitted'))
                self.assertEqual(final.get('requestCandidates'),alone.get('requestCandidates'))
                self.assertEqual(provider.decisions()[0]['text']['format']['schema']['properties']['mixTransitions'],
                                 json.loads(planning_request(request)[0])['text']['format']['schema']['properties']['mixTransitions'])
                self.assertEqual(receipt['lookups'],12);self.assertEqual(len(final['lookups']),12)

    def test_when_the_decision_request_has_no_room_nothing_is_looked_up(self):
        session,request=real_session(queue=4)
        self.assertTrue(dj_agent.room_for_lookups(request))
        # A request so full that it keeps no ordinary catalog row could not carry what lookups add.
        with patch.object(dj_agent,'LOOKUP_ROOM_ROWS',10**6):
            self.assertFalse(dj_agent.room_for_lookups(request))
            provider=Provider(decides([t['id'] for t in session['order']]))
            _,receipt=self.run_loop(provider,session=session,request=request)
        self.assertEqual((receipt['requests'],receipt['lookups']),(1,0))
        self.assertEqual(provider.requests[0][0],json.loads(planning_request(request)[0]))

    def test_work_that_is_no_longer_wanted_stops_before_the_next_request(self):
        provider=Provider(asks(call('search_catalog',SEARCH)),ready(),decides(['t01','t02']));asked=[]
        def wanted():asked.append(len(provider.requests));return False
        with self.assertRaises(EvaluationSuperseded):self.run_loop(provider,should_continue=wanted)
        self.assertEqual(len(provider.requests),1);self.assertEqual(asked,[1])

    def test_lookups_stop_once_the_evaluation_has_run_long_and_the_decision_follows(self):
        now=[0.0]
        def slow(body):
            now[0]+=LOOP_SECONDS+1
            return asks(call('search_catalog',SEARCH))
        provider=Provider(slow,decides(['t01','t02']))
        _,receipt=self.run_loop(provider,clock=lambda:now[0])
        self.assertEqual(receipt['requests'],2);self.assertNotIn('tools',provider.requests[1][0])

    def test_a_provider_failure_mid_loop_changes_nothing_and_is_not_retried(self):
        def fails(body):raise OSError('network')
        provider=Provider(asks(call('search_catalog',SEARCH)),fails)
        with self.assertRaisesRegex(ValueError,'uncertain'):self.run_loop(provider)
        self.assertEqual(len(provider.requests),2)

    def test_a_decision_that_never_arrives_is_a_failure_not_a_guess(self):
        with self.assertRaisesRegex(ValueError,'did not complete'):self.run_loop(Provider(ready(),asks()))

    def test_usage_is_summed_including_its_nested_counters(self):
        detail=lambda n:{'input_tokens':n,'input_tokens_details':{'cached_tokens':n//2},'output_tokens':7,'total_tokens':n+7}
        first=ready();first['usage']=detail(100);last=decides(['t01','t02']);last['usage']=detail(300)
        _,receipt=self.run_loop(Provider(first,last))
        self.assertEqual(receipt['usage'],{'input_tokens':400,'input_tokens_details':{'cached_tokens':200},'output_tokens':14,'total_tokens':414})


class SpendTests(unittest.TestCase):
    def test_the_approved_ceiling_covers_the_worst_case_turn(self):
        self.assertEqual(LOOP_TURN_CEILING,.10);self.assertEqual(TEXT_REQUESTS*LOOP_REQUESTS,10)
        self.assertLessEqual(TEXT_REQUESTS*LOOP_REQUESTS*TEXT_REQUEST_RESERVE,LOOP_TURN_CEILING)
        # A direction from an external client keeps the earlier single-request ceiling.
        self.assertEqual(TEXT_TURN_CEILING,.02)

    def test_one_operator_turn_never_sends_more_than_ten_requests(self):
        budget=TEXT_REQUESTS*LOOP_REQUESTS;first=turn_steps(budget,True);second=turn_steps(budget-first,False)
        self.assertEqual((first,second),(5,5))
        # Talk may search twice before it chooses: three evaluations share the same ten.
        budget=TEXT_REQUESTS*LOOP_REQUESTS;used=[]
        for may_search in (True,True,False):
            used.append(turn_steps(budget,may_search));budget-=used[-1]
        self.assertEqual(used,[5,4,1]);self.assertEqual(sum(used),10)
        self.assertEqual(turn_steps(budget,False),0);self.assertEqual(turn_steps(1,True),0)

    def test_automatic_curation_stays_inside_the_ceiling_it_discloses(self):
        self.assertLessEqual(session_curation.DECISIONS*LOOP_REQUESTS*TEXT_REQUEST_RESERVE,session_curation.COST_CEILING_USD)
        self.assertEqual(session_curation.COST_CEILING_USD,2.90)


class CrateReachTests(unittest.TestCase):
    """The lookups read the whole persistent crate, not only the working set."""
    def test_stored_recordings_outside_the_working_set_become_lookup_rows(self):
        working=crate()[:4]
        stored=working+[track(60,128,'8A',title='Stored Gem'),track(61,126,'9A',title='Warm Start',artist='Fixture'),
                        track(62,150,'8A',title='Too Fast'),{'title':'No identity'},track(63,None,'8A',title='No Tempo')]
        rows=dj_agent.crate_rows(stored,working,125,8,blocked=lambda tid:{'code':'x'} if tid=='t62' else None)
        self.assertEqual([row['id'] for row in rows],['t60','t63'])   # not a working row, the same song again, or a blocked one
        gem=rows[0]
        self.assertEqual((gem['pitchAtSetTempo'],gem['withinTempoLimit'],gem['fromCrate']),(round((125/128-1)*100,2),True,True))
        self.assertEqual((rows[1]['pitchAtSetTempo'],rows[1]['withinTempoLimit']),(None,None))

    def test_a_search_reaches_the_crate_and_says_how_much_it_looked_through(self):
        session=snapshot();working=music_context(session,full=True)['catalog']
        stored=dj_agent.crate_rows([track(900+i,125,'8A',title='Crate Only '+'bcdfg'[i%5]*3+str(i)) for i in range(300)],session['candidates'],125,8)
        book=agent_loop.Lookups(session,working+stored)
        search={'text':'crate only','genre':None,'bpmMin':None,'bpmMax':None,'limit':8}
        result=book.run('search_catalog',search)
        self.assertEqual(result['total'],300);self.assertEqual(result['crateSize'],len(working)+300)
        self.assertIn(f" · 300 of {len(working)+300} fit · showing 8",book.describe('search_catalog',search,result)[1])
        self.assertGreater(book.run('suggest_next',{'afterId':'t01','limit':8})['playable'],300)

    def agent(self,provider,hydrate):
        session=Session();session.state['candidates']=crate()[:6];session.state['order']=crate()[:2];session.state['maxTempoShift']=8
        session.crate=record_crate.RecordCrate();session.crate.upsert(track(77,125,'8A',title='Stored Gem'),source='dig')
        session.hydrate_records=lambda ids,keep=():hydrate(session,ids,keep) if hydrate.__code__.co_argcount>2 else hydrate(session,ids)
        agent=DJAgent(session);agent.opener=provider
        return agent,session

    def test_a_chosen_crate_recording_joins_the_working_set_and_is_selected(self):
        recalled=[]
        def hydrate(session,ids):
            recalled.extend(ids);session.state['candidates']=session.state['candidates']+[track(77,125,'8A',title='Stored Gem')]
        search={'text':'stored gem','genre':None,'bpmMin':None,'bpmMax':None,'limit':8}
        provider=Provider(asks(call('search_catalog',search)),ready(),decides(['t01','t77']))
        agent,session=self.agent(provider,hydrate)
        with patch('afterhours.ai_mix.api_key',return_value='fixture-key'):
            agent.message('find me that stored gem',message_id='m1',approved=True);settled(agent);agent.tick(force=True);state=settled(agent)
        self.assertEqual(recalled,['t77']);self.assertEqual(state['proposal']['trackIds'],['t01','t77'])
        self.assertIn('t77',[t['id'] for t in session.snapshot()['candidates']])

    def test_recalling_a_chosen_recording_protects_everything_else_the_decision_chose(self):
        kept=[]
        def hydrate(session,ids,keep):
            kept.extend(keep);session.state['candidates']=session.state['candidates']+[track(77,125,'8A',title='Stored Gem')]
        search={'text':'stored gem','genre':None,'bpmMin':None,'bpmMax':None,'limit':8}
        provider=Provider(asks(call('search_catalog',search)),ready(),decides(['t01','t02','t03','t77']))
        agent,session=self.agent(provider,hydrate)
        with patch('afterhours.ai_mix.api_key',return_value='fixture-key'):
            agent.message('find me that stored gem',message_id='m1',approved=True);settled(agent);agent.tick(force=True);settled(agent)
        self.assertTrue({'t01','t02','t03','t77'}<=set(kept))

    def test_music_that_changed_during_the_decision_is_asked_again_even_for_a_crate_pick(self):
        recalled=[]
        search={'text':'stored gem','genre':None,'bpmMin':None,'bpmMax':None,'limit':8}
        def final(body):
            session.state['index']=0      # a handoff landed while the decision was being written
            return decides(['t01','t77'])
        provider=Provider(asks(call('search_catalog',search)),ready(),final)
        agent,session=self.agent(provider,lambda session,ids:recalled.extend(ids))
        with patch('afterhours.ai_mix.api_key',return_value='fixture-key'):
            agent.message('find me that stored gem',message_id='m1',approved=True);state=settled(agent)
        self.assertEqual(recalled,[])
        self.assertEqual(state['status'],'pending');self.assertNotIn('Discover music again',state['message'])
        self.assertEqual(state['reasoning']['rounds'][-1]['status'],'superseded')

    def test_a_decision_a_newer_direction_replaced_recalls_nothing(self):
        recalled=[]
        search={'text':'stored gem','genre':None,'bpmMin':None,'bpmMax':None,'limit':8}
        def final(body):
            agent.state['revision']+=1
            return decides(['t01','t77'])
        provider=Provider(asks(call('search_catalog',search)),ready(),final)
        agent,session=self.agent(provider,lambda session,ids:recalled.extend(ids))
        with patch('afterhours.ai_mix.api_key',return_value='fixture-key'):
            agent.message('find me that stored gem',message_id='m1',approved=True);settled(agent);agent.tick(force=True);settled(agent)
        self.assertEqual(recalled,[])

    def test_a_crate_recording_that_was_not_chosen_stays_out_of_the_working_set(self):
        recalled=[]
        search={'text':'stored gem','genre':None,'bpmMin':None,'bpmMax':None,'limit':8}
        provider=Provider(asks(call('search_catalog',search)),ready(),decides(['t01','t02']))
        agent,_=self.agent(provider,lambda session,ids:recalled.extend(ids))
        with patch('afterhours.ai_mix.api_key',return_value='fixture-key'):
            agent.message('look for that stored gem',message_id='m1',approved=True);settled(agent);agent.tick(force=True);settled(agent)
        self.assertEqual(recalled,[])


class JournalTests(unittest.TestCase):
    def test_lookups_appear_as_steps_while_the_round_is_still_evaluating(self):
        journal=PlannerJournal(lambda:100.0);session=snapshot()
        round_id=journal.begin(session,1,'operator message')
        journal.lookup(round_id,'Searched the crate · house',' · 12 fit · showing 8')
        row=journal.snapshot(session)['rounds'][-1]
        self.assertEqual(row['status'],'evaluating')
        self.assertEqual([(s['text'],s['result']) for s in row['steps']][-1],('Searched the crate · house','· 12 fit · showing 8'))

    def test_every_lookup_an_evaluation_can_make_is_shown_and_the_outcome_still_fits(self):
        journal=PlannerJournal(lambda:100.0);session=snapshot()
        round_id=journal.begin(session,1,'operator message')
        for i in range(30):journal.lookup(round_id,'Checked the blend · %d'%i,'')
        row=journal.get(round_id)
        self.assertEqual(sum(1 for s in row['steps'] if s['id'].startswith('lookup:')),PlannerJournal.MAX_LOOKUPS)
        self.assertEqual(PlannerJournal.MAX_LOOKUPS,(LOOP_REQUESTS-1)*agent_loop.MAX_CALLS_PER_REQUEST)
        journal.finish(round_id,{**full_decision(['t01','t02']),'search':None},session,{'responseId':'r'},None)
        journal.close(round_id,'failed','stopped')
        self.assertIn('delivery',[s['id'] for s in journal.get(round_id)['steps']])

    def test_a_finished_round_takes_no_more_lookups(self):
        journal=PlannerJournal(lambda:100.0);session=snapshot()
        round_id=journal.begin(session,1,'operator message');journal.close(round_id,'superseded','newer')
        before=copy.deepcopy(journal.get(round_id)['steps'])
        journal.lookup(round_id,'Searched the crate','')
        self.assertEqual(journal.get(round_id)['steps'],before)


class AgentIntegrationTests(unittest.TestCase):
    def agent(self,provider):
        session=Session();session.state['candidates']=crate();session.state['order']=crate()[:2];session.state['maxTempoShift']=8
        agent=DJAgent(session);agent.opener=provider
        return agent,session

    def test_a_typed_turn_looks_things_up_and_shows_each_lookup_in_the_journal(self):
        provider=Provider(asks(call('search_catalog',SEARCH),call('check_transition',{'fromId':'t01','toId':'t03'},'c2')),
                          ready(),decides(['t01','t03']))
        agent,_=self.agent(provider)
        with patch('afterhours.ai_mix.api_key',return_value='fixture-key'):
            agent.message('something deeper',message_id='m1',approved=True);settled(agent);agent.tick(force=True);state=settled(agent)
        self.assertEqual(len(provider.requests),3)
        steps=[s['text'] for s in state['reasoning']['rounds'][-1]['steps']]
        self.assertIn('Searched the crate · house · 124–127 BPM',steps)
        self.assertIn('Checked the blend · Warm Start → Late Glass',steps)
        self.assertEqual(state['proposal']['trackIds'],['t01','t03']);self.assertEqual(state['lastEvaluation']['requests'],3)
        self.assertEqual(agent.turn_budget,TEXT_REQUESTS*LOOP_REQUESTS-3)

    def test_the_operator_turn_reports_the_approved_loop_ceiling(self):
        agent,_=self.agent(Provider(ready(),decides()))
        with patch('afterhours.ai_mix.api_key',return_value='fixture-key'):
            agent.message('something deeper',message_id='m1',approved=True)
            self.assertEqual(agent.snapshot()['allowance']['turnCeilingUSD'],LOOP_TURN_CEILING)
            agent.tick(force=True);settled(agent)

    def test_an_injected_evaluator_is_called_exactly_as_before(self):
        calls=[]
        def evaluator(request):calls.append(request);return decision(['a','b']),{'responseId':'fixture'}
        session=Session();agent=DJAgent(session,evaluator=evaluator)
        agent.message('keep it steady',message_id='m1',approved=True);settled(agent);agent.tick(force=True);settled(agent)
        self.assertEqual(len(calls),1);self.assertNotIn('lookups',calls[0])

    def test_changed_music_stops_the_spend_instead_of_paying_for_a_discarded_decision(self):
        agent,session=self.agent(None)
        def advance(body):
            session.state['index']=1      # the set moved on while the planner was looking things up
            return asks(call('search_catalog',SEARCH))
        provider=Provider(advance,ready(),decides(['t01','t03']));agent.opener=provider
        with patch('afterhours.ai_mix.api_key',return_value='fixture-key'):
            agent.message('something deeper',message_id='m1',approved=True);state=settled(agent)
        self.assertEqual(len(provider.requests),1)
        self.assertEqual(state['reasoning']['rounds'][-1]['status'],'superseded')

    def test_a_turn_interrupted_by_changed_music_is_asked_again_not_left_evaluating(self):
        for typed in (True,False):
            agent,session=self.agent(None);moved=[]
            def advance(body):
                if not moved:moved.append(1);session.state['index']=1;return asks(call('search_catalog',SEARCH))
                return ready() if 'tools' in body else decides(['t01','t02','t03'])
            provider=Provider(advance);agent.opener=provider
            session.state['order']=crate()[:3];session.update(active=True,phase='playing',index=0)
            with patch('afterhours.ai_mix.api_key',return_value='fixture-key'):
                if typed:agent.message('something deeper',message_id='m1',approved=True)
                else:agent.enable(True,1800,60);agent.message('something deeper',source='voice',message_id='m1')
                state=settled(agent)
                self.assertEqual(len(provider.requests),1)
                self.assertEqual(state['status'],'pending',typed)                       # not stranded as 'evaluating'
                self.assertEqual(agent.turn_budget,TEXT_REQUESTS*LOOP_REQUESTS-1)      # only what was sent is spent
                agent.last_request=0;agent.tick(force=True);state=settled(agent)
            self.assertGreater(len(provider.requests),1)
            self.assertNotEqual(state['status'],'evaluating')
            self.assertEqual(state['reasoning']['rounds'][-1]['status'],'selected',typed)

    def talk(self,step):
        """A Talk session whose first operator turn has been answered and applied."""
        provider=Provider(step);agent,session=self.agent(provider)
        session.state['order']=crate()[:3];session.update(active=True,phase='playing',index=0)
        agent.enable(True,1800,60);agent.message('something deeper',source='voice',message_id='m1');settled(agent)
        for _ in range(3):agent.last_request=0;agent.tick(force=True);settled(agent)
        return provider,agent,session

    def reasons(self,provider,since=0):
        return [json.loads(body['input']).get('reason') for body,_ in provider.requests[since:] if 'text' in body]

    def test_an_interrupted_automatic_refill_is_never_rerun_as_the_operators_message(self):
        moving=[False];holder={};count=[0]
        def step(body):
            count[0]+=1
            if moving[0] and 'tools' in body:
                holder['s'].state['index']+=1      # the set keeps moving while the refill looks things up
                return asks(call('check_transition',{'fromId':'t01','toId':'t%02d'%(3+count[0]%40)}))
            return ready() if 'tools' in body else decides(['t01','t02','t03'])
        with patch('afterhours.ai_mix.api_key',return_value='fixture-key'):
            provider,agent,session=self.talk(step);holder['s']=session
            base=len(provider.requests);budget=agent.turn_budget;replies=len([m for m in agent.snapshot()['messages'] if m['role']=='assistant'])
            moving[0]=True;session.state['index']=1
            for _ in range(12):agent.last_request=0;agent.tick();settled(agent)
        self.assertNotIn('operator message',self.reasons(provider,base))
        self.assertEqual(agent.turn_budget,budget)                       # the operator's turn is not charged for automatic work
        self.assertEqual(len([m for m in agent.snapshot()['messages'] if m['role']=='assistant']),replies)
        self.assertGreater(agent.remaining,0)                            # and Talk is not closed by it

    def test_switching_to_mixing_only_during_lookups_discards_the_work_without_asking_again(self):
        for automatic in (False,True):
            armed=[False];done=[False];holder={};count=[0]
            def step(body):
                count[0]+=1
                if armed[0] and not done[0] and 'tools' in body:
                    done[0]=True;mode=dict(holder['s'].state.get('copilot') or {})
                    mode.update(mode='mix',selectionEpoch=mode.get('selectionEpoch',0)+1);holder['s'].state['copilot']=mode
                    return asks(call('check_transition',{'fromId':'t01','toId':'t%02d'%(3+count[0]%40)}))
                return ready() if 'tools' in body else decides(['t01','t02','t03'])
            with patch('afterhours.ai_mix.api_key',return_value='fixture-key'):
                if automatic:
                    provider,agent,session=self.talk(step);holder['s']=session;session.state['index']=1
                else:
                    provider=Provider(step);agent,session=self.agent(provider);holder['s']=session
                    session.state['order']=crate()[:3];session.update(active=True,phase='playing',index=0)
                    agent.enable(True,1800,60)
                armed[0]=True;base=len(provider.requests)
                if not automatic:agent.message('something deeper',source='voice',message_id='m1');settled(agent)
                for _ in range(6):agent.last_request=0;agent.tick();settled(agent)
            self.assertEqual(len(provider.requests)-base,1,automatic)   # the interrupted request, and nothing after it
            self.assertNotEqual(agent.snapshot()['status'],'evaluating')

    def test_an_interrupted_choice_from_search_results_is_made_again_on_a_stopped_set(self):
        """Review of 71d8411: the opening set of a new request must not be dropped."""
        search={'query':'deep house','artist':'','genre':'House','bpmMin':120,'bpmMax':130}
        count=[0];move=[False];holder={}
        def step(body):
            count[0]+=1
            if move[0] and 'tools' in body:
                move[0]=False;rows=holder['s'].state['candidates']
                holder['s'].state['candidates']=rows+[dict(rows[0],id='zz%d'%count[0])]     # more results land mid-lookup
                return asks(call('check_transition',{'fromId':'t01','toId':'t05'}))
            if 'tools' in body:return ready()
            value=full_decision([]) if count[0]<=2 else full_decision(['t01','t02','t03'])
            if count[0]<=2:value['search']=search
            reply=decides();reply['output'][0]['content'][0]['text']=json.dumps(value);return reply
        provider=Provider(step);agent,session=self.agent(provider);holder['s']=session;session.state['order']=[]
        with patch('afterhours.ai_mix.api_key',return_value='fixture-key'):
            agent.enable(True,1800,60);agent.message('find me some deeper house',source='voice',message_id='m1');settled(agent)
            for _ in range(3):agent.last_request=0;agent.tick(force=True);settled(agent)
            self.assertTrue(agent.awaiting_catalog)
            session.state['catalogSearch']={'status':'complete','matched':6,'added':6,'query':'house'};session.state['phase']='catalog-ready'
            move[0]=True;base=len(provider.requests)
            for _ in range(8):agent.last_request=0;agent.tick();settled(agent)
        self.assertGreater(len(provider.requests)-base,1)                 # asked again after the interruption
        self.assertEqual(set(self.reasons(provider,base)),{'choose from catalog results'})
        self.assertIn(('plan',['t01','t02','t03']),session.calls)

    def test_automatic_work_that_keeps_being_overtaken_stops_asking(self):
        holder={}
        def step(body):
            if 'tools' in body:
                holder['s'].state['index']+=1
                return asks(call('check_transition',{'fromId':'t01','toId':'t%02d'%(3+len(holder['p'].requests)%40)}))
            return decides(['t01','t02','t03'])
        with patch('afterhours.ai_mix.api_key',return_value='fixture-key'):
            provider,agent,session=self.talk(lambda body:ready() if 'tools' in body else decides(['t01','t02','t03']))
            holder.update(s=session,p=provider);provider.script=[step];base=len(provider.requests)
            session.state['order']=crate()[:40];session.state['index']=1
            for _ in range(20):agent.last_request=0;agent.tick();settled(agent)
        self.assertLessEqual(len(provider.requests)-base,dj_agent.OVERTAKEN_LIMIT+1)

    def test_discovery_off_also_stops_the_engine_digging_on_its_own(self):
        agent,session=self.agent(Provider(ready(),decides()))
        agent.tick(force=True);self.assertTrue(session.dig_allowed)
        agent.settings.state['discover']=False
        agent.tick(force=True);self.assertFalse(session.dig_allowed)

    def test_an_unused_allowance_goes_back_only_to_the_turn_it_belongs_to(self):
        agent,_=self.agent(Provider(ready(),decides()))
        with patch('afterhours.ai_mix.api_key',return_value='fixture-key'):
            agent.message('something deeper',message_id='m1',approved=True);settled(agent);agent.tick(force=True);settled(agent)
        self.assertLessEqual(agent.turn_budget,TEXT_REQUESTS*LOOP_REQUESTS)
        self.assertEqual(agent.turn_budget,TEXT_REQUESTS*LOOP_REQUESTS-2)


if __name__=='__main__':unittest.main()
