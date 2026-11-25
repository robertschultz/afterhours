"""Read-only Fal account diagnostics. Never creates a model session or retries."""
import json
import math
import re
from urllib.error import HTTPError
from urllib.request import Request


def check_account(key, opener):
    if not key:return {'status':'missing_key','message':'Connect a Fal key first.'}
    request=Request('https://api.fal.ai/v1/account/billing?expand=credits',method='GET',
                    headers={'Authorization':'Key '+key,'Accept':'application/json'})
    try:
        with opener.open(request,timeout=10) as response:
            raw=response.read(65_537)
        if len(raw)>65_536:raise ValueError('Oversized account response')
        data=json.loads(raw)
        username=data.get('username');credits=data.get('credits')
        if not isinstance(username,str) or not re.fullmatch(r'[A-Za-z0-9_.@-]{1,100}',username):raise ValueError('Missing account')
        if not isinstance(credits,dict):raise ValueError('Missing credits')
        balance=credits.get('current_balance');currency=credits.get('currency')
        if type(balance) not in (int,float) or not math.isfinite(balance) or not 0<=balance<1e12:raise ValueError('Missing balance')
        if not isinstance(currency,str) or not re.fullmatch(r'[A-Z]{3}',currency):raise ValueError('Missing currency')
        return {'status':'verified','account':username,'balance':balance,'currency':currency,
                'message':'Account credit read successfully. This does not guarantee Director session admission.'}
    except HTTPError as error:
        status=error.code;error.close()
        if status==401:return {'status':'key_rejected','httpStatus':401,'message':'Fal rejected this key. Reconnect a valid key from the funded account.'}
        if status==403:return {'status':'billing_not_accessible','httpStatus':403,'message':'This key cannot read account billing. That does not mean the key cannot generate video. Compare its account and credits in the Fal dashboard.'}
        return {'status':'unavailable','httpStatus':status if 400<=status<=599 else 502,'message':'Fal could not return account credit. No balance was inferred and no retry was sent.'}
    except Exception:
        return {'status':'unavailable','message':'Account credit could not be verified. No balance was inferred and no retry was sent.'}
