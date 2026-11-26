"""Scoped routes on the existing loopback engine; no new HTTP listener."""
import hmac
import json

from afterhours.mcp_auth import ControlError

PATHS = frozenset('/api/mcp/'+name for name in ('state','direct','receipt','clients'))


def handle(handler, control, operator_token):
    """Called after the existing Host/Origin checks, before legacy token auth."""
    if handler.path not in PATHS:
        return False
    try:
        if control is None:
            raise ControlError('storage-unavailable', 'MCP control is unavailable; the local DJ controls remain available.')
        management=handler.path=='/api/mcp/clients'
        if management:
            if not hmac.compare_digest(handler.headers.get('X-DJ-Token',''),operator_token):
                raise ControlError('unauthorized','The local operator token is required to manage pairing.')
        elif handler.headers.get('Origin'):
            raise ControlError('forbidden','MCP client routes are not browser endpoints.')
        credential=handler.headers.get('Authorization','')
        if not management:
            if not credential.startswith('Bearer '):
                raise ControlError('unauthorized','A paired client credential is required.')
            credential=credential[7:]
            control.store.authenticate(credential)
        try:
            size=int(handler.headers.get('Content-Length','0'))
            if not 0<size<=8192 or handler.headers.get('Transfer-Encoding'):raise ValueError()
            data=json.loads(handler.rfile.read(size),parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
            if not isinstance(data,dict):raise ValueError()
        except (ValueError,UnicodeError,RecursionError):
            raise ControlError('invalid-request','Use a JSON object of at most 8192 bytes.') from None
        if management:
            action=data.get('action')
            if action=='pair' and not set(data)-{'action','name','readOnly','allowance','unmeteredMusic'}:
                result=control.store.pair(data.get('name'),read_only=data.get('readOnly',False),
                                          allowance=data.get('allowance'),unmetered_music=data.get('unmeteredMusic',False))
            elif action=='list' and set(data)=={'action'}:
                result={'clients':control.store.clients()}
            elif action=='revoke' and set(data)=={'action','clientId'} and isinstance(data['clientId'],str):
                control.store.revoke(data['clientId']);result={'revoked':True}
            else:raise ControlError('invalid-request','Use pair, list or revoke with their documented fields.')
        elif handler.path.endswith('/state') and not set(data)-{'offset','limit'}:
            result=control.state(credential,**data)
        elif handler.path.endswith('/direct'):
            result=control.direct(credential,data)
        elif handler.path.endswith('/receipt') and set(data)=={'requestId'}:
            result=control.receipt(credential,data['requestId'])
        else:raise ControlError('invalid-request','Unsupported MCP operation fields.')
        handler.reply(result)
    except ControlError as exc:
        handler.reply({'error':{'code':exc.code,'message':str(exc)}},403 if exc.code in ('unauthorized','forbidden') else 409)
    except Exception:
        # SQLite, bridge, provider and programming errors never leak their
        # request bodies, credentials, private paths or previous chat.
        handler.reply({'error':{'code':'unavailable','message':'The outcome is unavailable. Read the original receipt before any new direction.'}},503)
    return True
