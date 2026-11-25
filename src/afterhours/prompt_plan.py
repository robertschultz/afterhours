"""A relative night plan from the existing music decision, not another planner.

Language interpretation belongs to that decision. This boundary validates its
numbers and binds the resulting native settings receipt to the applied plan.
"""
import copy
import math

from afterhours import ai_mix


MIN_MINUTES = 30
MAX_MINUTES = 360
FIELDS = {'durationMinutes', 'peakAfterMinutes', 'warmupMinutes',
          'warm', 'onePeak', 'darkLate', 'explanation'}
NUMBER = {'type': 'number'}
SCHEMA = {'anyOf': [{'type': 'null'}, ai_mix.obj({
    'durationMinutes': NUMBER, 'peakAfterMinutes': NUMBER,
    'warmupMinutes': {'anyOf': [{'type': 'null'}, NUMBER]},
    'warm': {'type': 'boolean'}, 'onePeak': {'type': 'boolean'},
    'darkLate': {'type': 'boolean'}, 'explanation': {'type': 'string'},
})]}

INSTRUCTIONS = '''
sessionPlan needs canSetPlan=true and operatorRequest's definite current duration: two hours=120; an hour and a half=90 minutes. No duration: null, keep nightPlan. Use durationMinutes (30–360), interior peakAfterMinutes, nullable warmupMinutes, energy flags. Honor requested shape; infer the rest from brief/style, never a fixed percentage; explanation distinguishes each. Warm-up leaves min(20,duration/12) before peak and min(30,duration/8) after, in minutes. Never clamp.
Ambiguous/unsupported, solely negated, hypothetical/conditional/future timing or retiming with canSetPlan=false: sessionPlan/playbackAction/search=null, trackIds=[], sessionScope=clarify with one question/hold. Future requests never start now. Plan/prepare-only: timing allowed, playbackAction=null; duration alone never starts. Followups/background: keep nightPlan, sessionPlan=null. Timed ongoing needs scope=ongoing and its native grant. Plan receipts prove settings, not playback or an automatic audio stop.
'''


def validate(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise ValueError('The interpreted set plan has an invalid format.')
    for key in ('durationMinutes', 'peakAfterMinutes', 'warmupMinutes'):
        number = value[key]
        if key == 'warmupMinutes' and number is None:
            continue
        if type(number) not in (int, float) or not 0 <= number <= MAX_MINUTES or not math.isfinite(number):
            raise ValueError('Set timing must use finite numbers of minutes.')
    duration, peak, warm = (value[key] for key in
                            ('durationMinutes', 'peakAfterMinutes', 'warmupMinutes'))
    if not MIN_MINUTES <= duration <= MAX_MINUTES:
        raise ValueError('Prompt-planned sets need a duration from 30 minutes to 6 hours. Clarify the requested duration; nothing was started.')
    if not 0 < peak < duration:
        raise ValueError('The interpreted peak must fall inside the requested set.')
    if any(type(value[key]) is not bool for key in ('warm', 'onePeak', 'darkLate')):
        raise ValueError('Interpreted energy preferences must be on or off.')
    if (not isinstance(value['explanation'], str) or
            not 1 <= len(value['explanation'].strip()) <= 400):
        raise ValueError('The interpreted set plan needs a short explanation.')
    if warm is not None:
        if (not value['warm'] or warm < 1 or
                warm > peak - min(20, duration / 12) or
                not math.isclose(warm * 60, round(warm * 60), abs_tol=1e-7) or
                peak + min(30, duration / 8) > duration):
            raise ValueError('The requested warm-up, build, peak and ending do not fit. Clarify the timing; nothing was started.')
    result = copy.deepcopy(value)
    result['explanation'] = result['explanation'].strip()
    return result


def native(value, now):
    """Use the native clock and its existing plan validator, without rounding."""
    from afterhours.dj_settings import validate_plan
    value = validate(value)
    plan = {'startAt': now, 'endAt': now + value['durationMinutes'] * 60,
            'peakAt': now + value['peakAfterMinutes'] * 60,
            **{key: value[key] for key in ('warm', 'onePeak', 'darkLate')}}
    if value['warmupMinutes'] is not None:
        plan['warmLen'] = round(value['warmupMinutes'] * 60)
    return validate_plan(plan)


def readback(state):
    """Only the latest effective plan edit can describe the current plan."""
    for receipt in reversed(state['receipts']):
        if receipt.get('status') != 'confirmed':
            continue
        change = receipt.get('change') or {}
        kind = change.get('kind')
        if kind not in ('plan', 'peakShift', 'warmLength') and not (
                kind == 'rule' and (change.get('value') or {}).get('id') in ('warm', 'peak', 'dark')):
            continue
        if kind != 'plan' or change.get('value') != state.get('plan'):
            return None
        try:
            value = validate(receipt.get('interpretation'))
        except (ValueError, TypeError):
            return None
        if value is None:
            return None
        try:
            if native(value, change['value']['startAt']) != state['plan']:
                return None
        except (ValueError, TypeError, KeyError):
            return None
        return {'requestId': receipt['id'],
                **{key: value[key] for key in ('durationMinutes', 'peakAfterMinutes', 'warmupMinutes', 'explanation')},
                'appliedAt': receipt['at'], 'restored': receipt.get('restored') is True}
    return None
