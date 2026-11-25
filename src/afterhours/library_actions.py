"""Operator crate actions using the same catalog identities and guarded engine."""
import copy
from afterhours.dj_bridge import BridgeError


def library_action(session,data):
    action=data.get('action')
    if action=='search':return session.discover_more(data.get('query'),bpm_min=data.get('bpmMin',40),bpm_max=data.get('bpmMax',300),recheck_missing=True)
    with session.lock:
        s=session.snapshot();track=next((t for t in s['candidates'] if t['id']==data.get('trackId')),None)
        if not track:raise ValueError('This recording is no longer in the current catalog. Search it again.')
        if action=='queue':
            order=(s.get('queuedUpdate') or {}).get('tracks') or s['order'];ids=[t['id'] for t in order]
            if track['id'] in ids:return s
            if len(ids)>=12:raise ValueError('The upcoming set holds twelve tracks. Remove a future track in Setlist first.')
            ids.append(track['id'])
            if len(ids)==1:
                if s['active'] or any(d.get('playing') for d in (s.get('telemetry') or {}).get('decks',[])):raise ValueError('Stop manual playback before building a new set.')
                session.state.update(order=[copy.deepcopy(track)],phase='catalog-ready',analysis=None,aiReview=None,executionPlan=None,
                                     message='Opening track selected. Queue another recording or give the DJ a direction.')
                session.state['setlist']['sourceById'][track['id']]='operator'
            else:
                session.revise_queue(ids,reason=f"Operator queued {track['title']} from Library.")
                session.state['setlist']['sourceById'][track['id']]='operator'
            session.event(f"Queued {track['title']} from Library. Playback is unchanged.",'library')
            return session.snapshot()
        if action=='load':
            deck=data.get('deck',2)
            if type(deck) is not int or deck not in (1,2):raise ValueError('Choose Deck A or B.')
            if s['active']:raise ValueError('Take over before replacing a deck. The scheduled and cued tracks are protected.')
            if not session.folder or track['id'] not in session.positions:raise ValueError('Search this recording again before loading it.')
            def load(client):
                before=client.status()
                if before.get('automix') or before['decks'][deck-1].get('playing'):raise BridgeError('The target deck is playing or Automix is on. No load was sent.')
                session.load(client,track,deck)
                session.update(phase='takeover',executionPlan=None,message=f"{track['title']} loaded into Deck {'A' if deck==1 else 'B'}. Playback remains under manual control.")
                session.event(f"Loaded {track['title']} into Deck {'A' if deck==1 else 'B'}; no Play command sent.",'library')
            session._launch(load,'loading')
            return session.snapshot()
        raise ValueError('Unknown library action.')
