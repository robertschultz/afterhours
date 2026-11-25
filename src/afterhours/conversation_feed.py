"""Read-only public conversation projection. Never executes or invents DJ work.

The planner's exact queue receipts authorize action lines. Completed turns are
retained with the conversation; in-flight work becomes interrupted on restart.
Provider reasoning and raw response objects are deliberately not projected.
"""
import copy
import math
import json

from afterhours.planner_journal import line
from afterhours.queue_eligibility import skipped_message

LIMIT = 24


def bounded(rows):
    rows = rows[-LIMIT:]
    while len(rows) > 1 and len(json.dumps(rows)) > 64000:
        rows.pop(0)
    return rows


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def restore(rows, now):
    result = []
    for row in rows[-LIMIT:] if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not isinstance(row.get('id'), str) or not number(row.get('at')):
            continue
        item = copy.deepcopy(row)
        if not item.get('done') or item.get('status') in ('selected', 'staged', 'search'):
            item.update(done=True, status='interrupted', action=None,
                        text='The previous work stopped when the service restarted.', updatedAt=now)
        result.append(item)
    return bounded(result)


def action(row):
    if row['status'] not in ('applied', 'kept'):
        return None
    before = row['before']['orderIds']
    after = row.get('selectedIds', [])
    titles = row.get('titles', {})
    name = lambda tid: titles.get(tid, 'the recording')
    added = [i for i in after if i not in before]
    removed = [i for i in before if i not in after]
    if row['status'] == 'kept' or before == after:
        verb, obj, detail = 'Kept', 'the current order', f'{len(after)} recordings'
    elif len(added) == 1 and len(removed) == 1:
        verb, obj, detail = 'Swapped', name(removed[0]) + ' → ' + name(added[0]), 'in the prepared queue'
    elif len(added) == 1 and not removed:
        index = after.index(added[0])
        verb, obj = 'Queued', name(added[0])
        detail = 'after ' + name(after[index - 1]) if index else 'first in the set'
    elif not added and not removed:
        index = next(i for i, tid in enumerate(after) if before[i] != tid)
        verb, obj, detail = 'Moved', name(after[index]), f'to position {index + 1}'
    else:
        verb, obj, detail = 'Re-planned', 'the queue', f'{len(after)} recordings'
    return dict(verb=verb, object=obj, detail=detail, confirmed=True, receiptId=row['id'])


def merge(history, journal, now):
    rows = {row['id']: copy.deepcopy(row) for row in history if isinstance(row, dict) and isinstance(row.get('id'), str)}
    for row in journal.get('rounds', []):
        status = row['status']
        text = row.get('reply', '')
        if status == 'evaluating':
            text = ''
        elif status == 'selected':
            text = 'The recordings are selected. Preparing their audio and transitions.'
        elif status == 'staged':
            text = 'The queue update is staged for a verified handoff.'
            if row.get('preparation') == 'blocked':
                text = 'The queue update is held. ' + line(row.get('deliveryNote'))
        elif status in ('failed', 'held', 'superseded'):
            text = line(row.get('reason'))
        end = row.get('decidedAt', row.get('updatedAt', now))
        item = dict(id='planner:' + row['id'], at=row['openedAt'], updatedAt=row['updatedAt'],
                    status=status, done=status != 'evaluating', text=text,
                    messageId=row.get('messageId'), requestId=row.get('requestId'),
                    steps=copy.deepcopy(row.get('steps', [])), action=action(row),
                    seconds=max(0, round(end - row['openedAt'])) if status != 'evaluating' else None)
        rows[item['id']] = item
    return bounded(sorted(rows.values(), key=lambda r: (r['at'], r['id'])))


def control(history, receipt, messages, now):
    """Only the matching engine command can confirm a local Hold/Go/Skip turn."""
    rid = receipt.get('id')
    reply = next((m for m in messages if m.get('id') == str(rid) + '-reply'), None)
    if not rid or not reply:
        return history
    status = receipt.get('status')
    words = {'hold': ('Held', 'the next blend'), 'go': ('Started', 'the prepared blend'),
             'skip': ('Skipped', 'the next selection'), 'off': ('Stopped', 'Copilot'),
             'on': ('Enabled', 'Copilot')}
    pair = words.get(receipt.get('action'))
    if pair is None:
        return history
    confirmed = status == 'confirmed'
    item = dict(id='control:' + rid, at=reply['at'], updatedAt=now, messageId=reply['id'],
                requestId=rid, status=status, done=status != 'pending', text=line(receipt.get('message'), 1600),
                steps=[dict(id='delivery', text='Engine confirmed the command' if confirmed else 'Checking the engine command',
                            result='', at=receipt.get('completedAt') or reply['at'])],
                action=dict(verb=pair[0], object=pair[1], detail='', confirmed=True, receiptId=rid) if confirmed else None)
    existing = next((r for r in history if r['id'] == item['id']), None)
    if existing and existing.get('status') == status:
        return history
    return bounded(sorted([r for r in history if r['id'] != item['id']] + [item], key=lambda r: (r['at'], r['id'])))


def queue_repair(history, receipt):
    """One public confirmed omission turn, bound to the engine's exact receipt.

    This confirms only the queue edit; it never claims later tracks have been
    prepared or played. It leaves the live playing status/message untouched.
    """
    if not isinstance(receipt,dict) or receipt.get('basis')!='known-provider-preview-only':return history
    rid=receipt.get('id');revision=receipt.get('queueRevision');at=receipt.get('at')
    before=receipt.get('requestedIds');after=receipt.get('retainedIds');skipped=receipt.get('skipped')
    if (not isinstance(rid,str) or not 1<=len(rid)<=100 or not isinstance(revision,str) or not revision
            or not number(at) or not isinstance(before,list) or not 2<=len(before)<=12
            or not isinstance(after,list) or not 2<=len(after)<=12
            or any(not isinstance(tid,str) or not tid for tid in before+after)
            or len(set(before))!=len(before) or len(set(after))!=len(after)
            or not isinstance(skipped,list) or not 1<=len(skipped)<=10):return history
    for row in skipped:
        if (not isinstance(row,dict) or row.get('code')!='provider-preview-only'
                or not isinstance(row.get('trackId'),str) or row['trackId'] not in before
                or not isinstance(row.get('title'),str) or not 1<=len(row['title'])<=300
                or not isinstance(row.get('reason'),str) or not 1<=len(row['reason'])<=1600):return history
    removed={row['trackId'] for row in skipped}
    if len(removed)!=len(skipped) or after!=[tid for tid in before if tid not in removed]:return history
    identifier='queue-repair:'+rid
    if any(row.get('id')==identifier for row in history):return history
    item={'id':identifier,'at':at,'updatedAt':at,'status':'confirmed','done':True,
        'text':line(skipped_message(skipped),1600),'seconds':0,
        'steps':[{'id':'availability','text':'Confirmed provider preview restriction',
                  'result':line(skipped[0]['reason'],240),'at':at}],
        'action':{'verb':'Skipped','object':line(', '.join(row['title'] for row in skipped),300),
                  'detail':'from the selected future queue','confirmed':True,'receiptId':rid}}
    return bounded(sorted(history+[item],key=lambda row:(row['at'],row['id'])))
