"""Observed answers to narrow recording/queue questions; no music decisions.

Only use this after the planner returns a no-action answer. Ambiguous subjects,
creative requests and mixed questions/commands stay on the normal agent path.
"""
import math
import re


def deck_facts(session, now):
    """Bounded observed deck facts for questions; planned tempos are separate."""
    telemetry = session.get('telemetry') or {}
    sampled = telemetry.get('sampledAt')
    fresh = (type(sampled) in (float, int) and math.isfinite(sampled)
             and -2 <= now - sampled < 3)
    if not fresh:
        return {'status': 'unavailable', 'decks': [],
                'meaning': 'Fresh deck readings are unavailable. A planned tempo is not an observed deck tempo.'}
    records = {t['id']: t for t in (session.get('candidates') or []) + (session.get('order') or []) if t.get('id')}
    decks = []
    for deck in telemetry.get('decks', [])[:4]:
        number = deck.get('deck')
        if type(number) is not int or number not in (1, 2, 3, 4) or deck.get('unavailable'):
            continue
        track = records.get(deck.get('id')) or {}
        current = float(_bpm(deck.get('bpm'))) if _bpm(deck.get('bpm')) else None
        original = float(_bpm(track.get('bpm'))) if _bpm(track.get('bpm')) else None
        decks.append({'deck': 'ABCD'[number - 1], 'trackId': deck.get('id'),
                      'title': track.get('title') or deck.get('title'),
                      'playing': deck.get('playing') if type(deck.get('playing')) is bool else None,
                      'currentBpm': current, 'originalBpm': original,
                      'bpmDifference': round(current - original, 2) if current and original else None,
                      'pitchPercent': round((current / original - 1) * 100, 2) if current and original else None})
    return {'status': 'observed', 'sampleAgeSeconds': round(max(0, now - sampled), 2), 'decks': decks,
            'meaning': 'CurrentBpm is the measured deck tempo even while paused. OriginalBpm is the recording tempo. These outrank planned tempo targets and old conversation claims.'}


def _bpm(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return None
    if not math.isfinite(value) or not 40 <= value <= 300:
        return None
    return f'{value:.2f}'.rstrip('0').rstrip('.')


def answer(text, session, now):
    if not isinstance(text, str) or not re.match(
            r'\s*(?:what|which|how|is|are|does|do|can you tell|could you tell|tell me)\b', text, re.I):
        return None
    tempo = bool(re.search(r'\b(?:bpm|tempo|how fast)\b', text, re.I))
    following = bool(re.search(r'\b(?:after|next|follows?|following)\b', text, re.I))
    if not (tempo or following):
        return None
    order = session.get('order') or []
    staged = (session.get('queuedUpdate') or {}).get('tracks') or []
    records = {}
    for row in order + staged + (session.get('candidates') or []):
        if row.get('id'):
            records.setdefault(row['id'], row)
    matches = []
    for row in records.values():
        title = row.get('title') or ''
        if len(title) >= 3 and re.search(r'(?<!\w)' + re.escape(title) + r'(?!\w)', text, re.I):
            matches.append(row)
    if len({row['title'].casefold() for row in matches}) > 1:
        return None
    # A question about what follows a recording refers to the current queue.
    # Catalog variants with the same title must not obscure its exact ID.
    if following:
        queued_matches=[row for row in matches if row['id'] in {item['id'] for item in order}]
        if queued_matches:
            matches=queued_matches
    if len(matches) != 1:
        return None
    track = matches[0]
    remainder = re.sub(re.escape(track['title']), '', text, flags=re.I)
    if re.search(r'\b(?:play|start|stop|pause|load|add|remove|move|mix|search|find|cue|replace|instead|should|would|suggest|recommend)\b', remainder, re.I):
        return None
    parts = []
    if tempo:
        bpm = _bpm(track.get('bpm'))
        if bpm is None:
            parts.append(f"The original tempo for {track['title']} is unavailable.")
        else:
            phrase = f"{track['title']} is {bpm} BPM"
            telemetry = session.get('telemetry') or {}
            sampled = telemetry.get('sampledAt')
            if isinstance(sampled, (float, int)) and -2 <= now - sampled < 3:
                deck = next((d for d in telemetry.get('decks', [])
                             if d.get('id') == track['id'] and not d.get('unavailable')), None)
                if deck and type(deck.get('deck')) is int and deck['deck'] in (1, 2, 3, 4) and _bpm(deck.get('bpm')):
                    if abs(deck['bpm'] - track['bpm']) >= .05:
                        phrase += f" originally; deck {'ABCD'[deck['deck'] - 1]} is at {_bpm(deck['bpm'])} BPM"
            parts.append(phrase + '.')
    if following:
        ids = [row.get('id') for row in order]
        if track['id'] not in ids:
            parts.append(f"{track['title']} is not in the current queue.")
        else:
            index = ids.index(track['id'])
            if index + 1 < len(order):
                parts.append(f"{order[index + 1]['title']} follows it in the current queue.")
            else:
                parts.append('Nothing follows it in the current queue.')
        staged_ids = [row.get('id') for row in staged]
        if staged and staged_ids != ids and track['id'] in staged_ids:
            index = staged_ids.index(track['id'])
            if index + 1 < len(staged):
                parts.append(f"The staged update puts {staged[index + 1]['title']} after it; that change has not been applied.")
            else:
                parts.append('It is last in the staged update, which has not been applied.')
    return ' '.join(parts)
