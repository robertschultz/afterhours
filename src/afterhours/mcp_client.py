"""Small loopback-only client shared by pairing and the optional stdio adapter."""
import json
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from afterhours.mcp_auth import ControlError, local_url

ERRORS = {
    'unauthorized':'The paired client credential is invalid or revoked.',
    'forbidden':'This client does not have that capability.',
    'invalid-request':'The request does not match the tool contract.',
    'invalid-page':'The queue page is outside the bounded range.',
    'stale-context':'The set changed. Read state before a new direction.',
    'busy':'The DJ is handling an earlier direction. Read its receipt first.',
    'request-conflict':'This request ID already belongs to another payload. Read its original receipt.',
    'unknown-request':'This client has no receipt with that request ID.',
    'allowance-required':'Pair this client with an explicit music allowance or trusted unmetered music access.',
    'allowance-exhausted':'This client has no remaining allowance for that operation.',
    'unsupported-operation':'This connection controls music, not devices, characters or services.',
    'storage-unavailable':'MCP control storage is unavailable. No new direction was dispatched.',
    'receipt-capacity':'This client has reached its retained receipt limit.',
    'unavailable':'The outcome is unavailable. Read the original receipt before any new direction.',
}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ControlError('unavailable', ERRORS['unavailable'])


def request(url, credential, operation, payload, *, operator=False):
    if operation not in ({'clients'} if operator else {'state','direct','receipt'}):
        raise ControlError('invalid-request',ERRORS['invalid-request'])
    base=local_url(url)
    body=json.dumps(payload,allow_nan=False,separators=(',',':')).encode()
    if len(body)>8192:raise ControlError('invalid-request',ERRORS['invalid-request'])
    headers={'Content-Type':'application/json',
             'X-DJ-Token' if operator else 'Authorization':credential if operator else 'Bearer '+credential}
    opener=build_opener(ProxyHandler({}),NoRedirect())
    try:
        try:response=opener.open(Request(base+'/api/mcp/'+operation,data=body,headers=headers),timeout=8)
        except HTTPError as exc:response=exc
        with response:
            raw=response.read(65537)
            if len(raw)>65536:raise ValueError()
            value=json.loads(raw)
            if not isinstance(value,dict):raise ValueError()
            if response.status!=200 or 'error' in value:
                code=(value.get('error') or {}).get('code')
                code=code if code in ERRORS else 'unavailable'
                raise ControlError(code,ERRORS[code])
            return value
    except ControlError:raise
    except Exception:
        raise ControlError('unavailable',ERRORS['unavailable']) from None
