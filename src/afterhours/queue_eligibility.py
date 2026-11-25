"""Local omission of an explicitly restricted future recording, never a retry.

The crate and provider evidence stay intact. Only the queue's remaining exact
recordings survive, in the same order; later cue fitting still has to succeed.
"""
import copy

from afterhours.dj_bridge import BridgeError
from afterhours.recording_availability import PREVIEW_ONLY


class QueueEligibilityHeld(BridgeError):
    def __init__(self, failure):
        self.failure=copy.deepcopy(failure)
        super().__init__(failure['reason'])


def preview_failure(availability, tracks, reason=None):
    """Bind only an exact current restriction, not a spare-deck guess."""
    for track in tracks:
        row=availability.blocked(track['id'])
        if row and row['code']==PREVIEW_ONLY and (reason is None or row['reason']==reason):
            return {'trackId':track['id'],'title':track.get('title','Recording'),
                    'code':PREVIEW_ONLY,'reason':row['reason']}
    return None


def preview_tail(availability, snapshot, tracks, protected=2):
    """Return an exact filtered tail or a constraint-bound held receipt.

    A generic source='operator' tag is provenance, not a must-play lock. Exact
    standing pins, fixed positions, the protected prefix, and reorder-off are
    authoritative. Unknown/temporary lookup or decode failures never qualify.
    """
    if not isinstance(tracks,list) or not 2<=len(tracks)<=12:
        raise ValueError('A continuation needs a bounded selected queue.')
    ids=[track['id'] for track in tracks]
    if len(set(ids))!=len(ids) or type(protected) is not int or not 0<=protected<=len(tracks):
        raise ValueError('The continuation queue or protected prefix changed.')
    skipped=[]
    for track in tracks:
        failure=preview_failure(availability,[track])
        if failure:skipped.append(failure)
    if not skipped:return None
    policy=snapshot.get('setlist') or {}
    locked=set(policy.get('lockedIds',[]));pinned=set(snapshot.get('setPins',[]))
    removed={row['trackId'] for row in skipped}
    kept=[track for track in tracks if track['id'] not in removed]
    retained=[track['id'] for track in kept]
    def hold(failure,detail):
        raise QueueEligibilityHeld({**failure,'reason':failure['reason']+' '+detail})
    for failure in skipped:
        tid=failure['trackId']
        if tid in ids[:protected]:hold(failure,'The current or prepared opening pair stays in place.')
        if tid in pinned:hold(failure,'This exact recording is pinned; its request remains held.')
        if tid in locked:hold(failure,'This recording is locked in the queue; its request remains held.')
    if (snapshot.get('copilot') or {}).get('mode')=='mix':
        hold(skipped[0],'Mixing-only control keeps the chosen queue; the exact request remains held.')
    if not policy.get('allowReorder',True):hold(skipped[0],'Queue reordering is off; the selected order stays in place.')
    if any(index>=len(retained) or retained[index]!=tid for index,tid in enumerate(ids) if tid in locked):
        hold(skipped[0],'Omitting it would move a locked queue position; the selected order stays in place.')
    if len(kept)<2:hold(skipped[0],'Fewer than two eligible recordings remain; the selected order stays in place.')
    targets=snapshot.get('tempoTargets')
    return {'tracks':copy.deepcopy(kept),'skipped':skipped,'requestedIds':ids,'retainedIds':retained,
            'tempoTargets':[copy.deepcopy(row) for row in targets if row['trackId'] in retained] if targets else targets}


def skipped_message(rows):
    names=', '.join(row['title'] for row in rows)
    return f'{names} {"is" if len(rows)==1 else "are"} preview-only. Continuing with the remaining selected recordings in order.'
