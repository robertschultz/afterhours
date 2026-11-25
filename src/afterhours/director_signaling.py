"""Bounded, non-sensitive diagnostics for Fal's HTTP signaling errors."""
import json

FIELDS={'body','sdp','type','app_id','offer','audio','video','media','session_id'}
ISSUES={'missing','literal_error','string_type','extra_forbidden','value_error','json_invalid'}


def signaling_error(error):
    """Retain status and classifications, never raw messages, input, URLs or SDP."""
    result={'error':f'Fal signaling returned HTTP {error.code}; no retry was submitted.',
            'validation':[]}
    if error.code not in (422,503):return result
    try:
        raw=error.read(65_537)
        if len(raw)>65_536:return result
        pending=[json.loads(raw)]
    except (ValueError,OSError):return result
    visited=0
    while pending and visited<24 and len(result['validation'])<4:
        item=pending.pop(0);visited+=1
        if isinstance(item,list):pending.extend(item[:8]);continue
        if isinstance(item,str):
            if error.code==503 and item.strip().lower().rstrip('.')=='unable to verify session credit':
                result['category']='credit_verification_unavailable'
                result['error']='Fal could not verify session credit (HTTP 503). Check the account used by this API key; no automatic retry.'
                continue
            # Some bridge versions wrap the runner's JSON error in a string.
            try:pending.append(json.loads(item))
            except ValueError:
                if error.code!=422:continue
                text=item.lower()
                if any(s in text for s in ('incompatible','media section','none is not in list','media negotiation')):
                    result['validation'].append({'field':'sdp','issue':'media_contract'})
                elif any(s in text for s in ('invalid sdp','malformed sdp','invalid offer','invalid webrtc offer')):
                    result['validation'].append({'field':'sdp','issue':'invalid_offer'})
            continue
        if not isinstance(item,dict):continue
        if error.code==422 and isinstance(item.get('loc'),list):
            # Use only known field names; loc may also contain user input.
            known=[part for part in item['loc'] if isinstance(part,str) and part in FIELDS]
            issue=item.get('type');issue=issue if isinstance(issue,str) and issue in ISSUES else 'invalid_value'
            result['validation'].append({'field':'.'.join(known[-3:]) or 'request','issue':issue})
        for name in ('detail','error','message','msg','errors'):
            if name in item:pending.append(item[name])
    # Stable, short classifications are safe for both UI and persisted receipts.
    result['validation']=[dict(pair) for pair in dict.fromkeys(tuple(v.items()) for v in result['validation'])]
    return result
