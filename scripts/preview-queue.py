"""Replay queue decisions from a saved set-session snapshot without a live engine."""

import argparse
import json
from pathlib import Path

from afterhours import queue_planning
from afterhours.agent_loop import Lookups
from afterhours.dj_agent import music_context


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot', type=Path, help='JSON snapshot from /api/set-session')
    parser.add_argument('--after', help='Exact recording to compare as the outgoing track')
    parser.add_argument('--insert', help='Exact recording to preview inserting')
    args = parser.parse_args()
    try:
        session = json.loads(args.snapshot.read_text())
        sampled = (session.get('telemetry') or {}).get('sampledAt', 0)
        output = {'sessionId': session.get('id'), 'queueRunwaySeconds': queue_planning.runway(session, sampled)}
        if args.after:
            rows = music_context(session, now=sampled, full=True)['catalog']
            book = Lookups(session, rows)
            request = {'afterId': args.after, 'limit': 3}
            output['tempoAndKey'] = book.suggest(request, prefer_prepared=False)['matches']
            output['withCueReadiness'] = book.suggest(request)['matches']
        if args.insert:
            output['insertionOptions'] = queue_planning.placement(session, args.insert)['options']
    except (OSError, TypeError, ValueError, KeyError) as exc:
        parser.error(str(exc))
    print(json.dumps(output, indent=2))


if __name__ == '__main__':
    main()
