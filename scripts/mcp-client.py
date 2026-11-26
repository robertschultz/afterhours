#!/usr/bin/env python3
"""Explicit local pairing; never edits host MCP configuration or starts services."""
import argparse
import json
from pathlib import Path
import sys
import time

from afterhours.mcp_auth import private_json, local_url, write_credentials
from afterhours.mcp_client import request


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--session-file',type=Path,required=True,help='Existing engine .runtime/session.json, read only for this explicit management command')
    actions=parser.add_subparsers(dest='action',required=True)
    pair=actions.add_parser('pair');pair.add_argument('--name',required=True);pair.add_argument('--output',type=Path,required=True)
    pair.add_argument('--read-only',action='store_true')
    pair.add_argument('--unmetered-music',action='store_true',
                      help='Trust this client to spend on DJ directions without a client cost or call ceiling until revoked')
    pair.add_argument('--music-usd',type=str);pair.add_argument('--music-calls',type=int);pair.add_argument('--expires-minutes',type=int)
    pair.add_argument('--prepare',action='store_true',help='Allow one bounded mix review per music turn, within the same total ceiling')
    actions.add_parser('list')
    revoke=actions.add_parser('revoke');revoke.add_argument('client_id')
    args=parser.parse_args()
    try:
        session=private_json(args.session_file)
        url=local_url(session['url']);token=session['token']
        if not isinstance(token,str) or not 1<=len(token)<=128:raise ValueError()
        payload={'action':args.action}
        if args.action=='pair':
            supplied=[args.music_usd is not None,args.music_calls is not None,args.expires_minutes is not None]
            if args.unmetered_music and (args.read_only or any(supplied) or args.prepare):
                parser.error('--unmetered-music cannot be combined with read-only or bounded allowance options')
            if any(supplied) and not all(supplied) or args.prepare and not all(supplied):
                raise ValueError('Music needs all three limits; preparation requires music.')
            if args.output.exists() or args.output.is_symlink():raise ValueError('Refusing to overwrite an existing credential file.')
            payload.update(name=args.name,readOnly=args.read_only)
            if args.unmetered_music:
                payload['unmeteredMusic']=True
            if all(supplied):
                payload['allowance']={'maxCostUSD':args.music_usd,'maxProviderCalls':args.music_calls,
                                      'expiresAt':time.time()+args.expires_minutes*60,'prepare':args.prepare}
        elif args.action=='revoke':payload['clientId']=args.client_id
        result=request(url,token,'clients',payload,operator=True)
        if args.action=='pair':
            try:write_credentials(args.output,{'version':1,'url':url,'clientId':result['clientId'],'credential':result['credential']})
            except BaseException:
                # A failed credential write must not leave an undisclosed active
                # capability. If the engine disconnects, list/revoke its ID.
                try:request(url,token,'clients',{'action':'revoke','clientId':result['clientId']},operator=True)
                except Exception:pass
                raise
            result={k:result[k] for k in ('clientId','name','scopes','allowance')}
        print(json.dumps(result,indent=2));return 0
    except Exception:
        print('MCP management failed. Check the running local engine, owner-only files and valid pairing options. No host configuration was changed.',file=sys.stderr)
        return 1


if __name__=='__main__':raise SystemExit(main())
