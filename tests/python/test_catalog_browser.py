"""Catalog actions against the real session validator; no OSC/provider calls."""
import copy
import threading
import unittest
from unittest.mock import Mock, patch
from afterhours.catalog_browser import CatalogBrowser, context, search, intent, browse_request, import_query
from afterhours.vdj_session import DJSession
from afterhours.dj_agent import DJAgent


class CatalogBrowserTests(unittest.TestCase):
    def test_browsing_never_swallows_queue_or_play_directions(self):
        for text in ('add daft punk into the queue','play daft punk on b','queue some Daft Punk',
                     'Find Daft Punk and add it to the queue','Find Daft Punk then play it','mix in Daft Punk'):
            self.assertFalse(browse_request(text),text)
        for text in ('Find Daft Punk','Please show me Daft Punk','Can you find Another Love','Search for house'):
            self.assertTrue(browse_request(text),text)
        self.assertEqual(import_query('Can you add some Daft Punk to my catalog?'),'Daft Punk')
        self.assertIsNone(import_query('Add Daft Punk into the queue'))

    def test_catalog_add_and_play_use_the_same_agent_as_control_for_text_and_voice(self):
        from test_dj_agent import Session,decision,settled
        for source in ('typed','voice'):
            s=Session();e=Mock(return_value=(decision(['a','c','b']),{'responseId':'test'}))
            a=DJAgent(s,evaluator=e)
            a.catalog_browser.open('Find Artist','prior-search',s.snapshot())
            a.message('add daft punk into the queue',source,'queue-'+source,True,surface='catalog')
            state=settled(a)
            self.assertEqual(e.call_count,1)
            self.assertIsNone(state['catalogBrowser']['search'])
            self.assertIn(('plan',['a','c','b']),s.calls)
            self.assertEqual(state['proposal']['status'],'applied')

    def test_explicit_catalog_import_discovers_without_selecting_or_spending_model_budget(self):
        from test_dj_agent import Session
        s=Session();e=Mock(side_effect=AssertionError('Library import does not use a model'))
        a=DJAgent(s,evaluator=e)
        before=s.snapshot()['order']
        a.message('Add Daft Punk to my catalog',message_id='import',approved=True,surface='catalog')
        self.assertEqual(s.calls,[('discover',('Daft Punk',))])
        self.assertEqual(s.snapshot()['order'],before)
        self.assertEqual(a.snapshot()['allowance']['remaining'],0)
        e.assert_not_called()

    def setUp(self):
        self.now = 100
        self.browser = CatalogBrowser(lambda:self.now)
        self.session = DJSession(threading.Lock())
        self.tracks = [dict(id=c,title='Record '+c,artist='Test Artist',bpm=125,camelot='8A') for c in 'abcdefgh']
        self.session.update(candidates=self.tracks,order=self.tracks[:6],index=0,phase='stopped')
        self.session.program = lambda:dict(current='a',connected=True)

    def expected(self):
        return context(self.session.snapshot(),'a')

    def test_play_next_protects_current_and_next_caps_five_and_never_plays(self):
        with patch.object(self.session,'send') as send:
            receipt=self.browser.play_next(self.session,'g','pick-1',self.expected())
            self.assertEqual(receipt['orderIds'],list('abgcde'))
            self.assertEqual(receipt['note'],'You asked')
            send.assert_not_called()
        self.assertEqual([t['id'] for t in self.session.snapshot()['order']],list('abgcde'))

    def test_new_set_preserves_the_catalog_but_clears_its_order(self):
        self.session.folder={'id':'folder'}
        self.session.positions={'a':{'query':'Record a','position':0}}
        before=self.session.snapshot()['candidates']
        with patch.object(self.session,'send') as send:
            after=self.session.new_set()
        self.assertEqual(after['candidates'],before)
        self.assertEqual(after['order'],[])
        self.assertEqual(self.session.positions,{'a':{'query':'Record a','position':0}})
        self.assertEqual(self.session.folder,{'id':'folder'})
        send.assert_not_called()

    def test_retry_is_idempotent_even_after_context_changes(self):
        expected=self.expected()
        first=self.browser.play_next(self.session,'g','pick-1',expected)
        self.assertEqual(self.browser.play_next(self.session,'g','pick-1',expected),first)
        with self.assertRaises(ValueError):self.browser.play_next(self.session,'h','pick-1',expected)
        with self.assertRaises(ValueError):self.browser.play_next(self.session,'h','pick-2',expected)

    def test_already_queued_and_locked_positions_are_preserved(self):
        before=copy.deepcopy(self.session.snapshot()['order'])
        receipt=self.browser.play_next(self.session,'d','pick-1',self.expected())
        self.assertEqual(receipt['status'],'already')
        self.assertEqual(self.session.snapshot()['order'],before)
        self.session.state['setlist']['lockedIds']=['c']
        with self.assertRaises(ValueError):self.browser.play_next(self.session,'g','pick-2',self.expected())
        self.assertEqual(self.session.snapshot()['order'],before)

    def test_paused_prepared_plan_is_retained_and_change_is_staged(self):
        plan={'id':'measured','applied':True}
        self.session.update(executionPlan=plan)
        receipt=self.browser.play_next(self.session,'g','pick-1',self.expected())
        s=self.session.snapshot()
        self.assertEqual(s['executionPlan'],plan)
        self.assertEqual([t['id'] for t in s['order']],list('abcdef'))
        self.assertEqual(s['queuedUpdate']['protectedIds'],list('ab'))
        self.assertEqual(s['queuedUpdate']['status'],'needs-analysis')
        self.assertEqual(receipt['status'],'staged')

    def test_search_is_local_bounded_expiring_and_old_putback_cannot_close_new(self):
        before=self.session.snapshot()
        self.browser.open('Find Test Artist','search-1',before)
        result=self.browser.snapshot(self.session,None)
        self.assertEqual(result['search']['ids'],list('abc'))
        self.assertIsNone(result['search']['best'])
        self.assertEqual(result['scope'],'available-crate')
        self.browser.open('Find Record h','search-2',before)
        self.browser.close('search-1')
        self.assertTrue(self.browser.active())
        self.now+=16
        self.assertIsNone(self.browser.snapshot(self.session,None)['search'])
        self.assertEqual(self.session.snapshot()['order'],before['order'])

    def test_search_and_short_commands_do_not_consume_titles_with_command_words(self):
        self.assertEqual(intent('skip'),'skip')
        self.assertEqual(intent('darker'),'darker')
        self.assertEqual(intent('Find Another Love'),'search')
        self.assertEqual(search('Find Another Love',[dict(id='x',title='Another Love',artist='Artist')]),['x'])

    def test_native_voice_and_text_search_take_same_path_without_provider_budget(self):
        evaluator=Mock(side_effect=AssertionError('No provider call for crate browsing'))
        agent=DJAgent(self.session,evaluator=evaluator,clock=lambda:self.now)
        for source in ('typed','voice'):
            result=agent.message('Find Test Artist',source,'find-'+source,True,surface='catalog')
            self.assertEqual(result['catalogBrowser']['search']['ids'],list('abc'))
            self.assertEqual(result['allowance']['remaining'],0)
            self.assertTrue(result['messages'][-1]['text'].startswith('3 matching records'))
        agent.tick(force=True);evaluator.assert_not_called()
        agent.catalog_action('catalog-put-back',search_id='find-voice')
        self.assertIsNone(agent.snapshot()['catalogBrowser']['search'])


if __name__=='__main__':unittest.main()
