"""Explicit operator device directions, delivered once by their native owner.

This module neither operates hardware nor calls a provider. The native app must
claim a short-lived command and report its observed result before success appears.
"""
import copy
import math
import re

ACTIVE = ('pending', 'dispatching')


def intent(text):
    value = re.sub(r'\s+', ' ', text.strip().lower()).rstrip('.!')
    value = re.sub(r'^please ', '', value)
    commands = {
        'camera on': ('camera', True), 'turn the camera on': ('camera', True),
        'turn on the camera': ('camera', True), 'enable camera input': ('camera', True),
        'camera off': ('camera', False), 'turn the camera off': ('camera', False),
        'turn off the camera': ('camera', False), 'disable camera input': ('camera', False),
        'open room display': ('room', True), 'open the room display': ('room', True),
        'close room display': ('room', False), 'close the room display': ('room', False),
        'make the room display dark': ('roomTheme', 'dark'), 'dark room display': ('roomTheme', 'dark'),
        'make the room display light': ('roomTheme', 'light'), 'light room display': ('roomTheme', 'light'),
        'show track details in the room': ('roomDetails', True),
        'hide track details in the room': ('roomDetails', False),
        'show nova in the room': ('showRob', True), 'hide nova in the room': ('showRob', False),
        'enable audience requests': ('requests', True), 'turn on audience requests': ('requests', True),
        'disable audience requests': ('requests', False), 'turn off audience requests': ('requests', False),
        'open virtualdj': ('engine', None), 'open virtual dj': ('engine', None),
        'open catalog': ('catalog', None), 'open the catalog': ('catalog', None),
    }
    if value in commands:
        target, setting = commands[value]
        return {'target': target, 'value': setting}
    unsupported = {
        'stop recording': 'Set audio recording is controlled in VirtualDJ. I have not changed it.',
        'start recording': 'Set audio recording is controlled in VirtualDJ. I have not changed it.',
        'stop audio recording': 'Set audio recording is controlled in VirtualDJ. I have not changed it.',
        'start audio recording': 'Set audio recording is controlled in VirtualDJ. I have not changed it.',
        'stop video recording': 'Video capture is controlled in Studio. I have not ended the active performance.',
        'start video recording': 'Video capture is controlled in Studio. I have not started a paid performance.',
        'turn spoken commentary on': 'The room performance stays silent. Private Talk is controlled separately.',
        'turn spoken commentary off': 'The room performance is silent. Private Talk is controlled separately.',
    }
    if re.fullmatch(r'(?:set )?(?:audio )?latency (?:to )?(?:low|balanced|safe)', value):
        return {'unsupported': 'Audio latency is controlled in VirtualDJ. No latency change was made.'}
    if value in unsupported:
        return {'unsupported': unsupported[value]}
    return None


def context(value, session):
    if not isinstance(value, dict) or set(value) != {'ownerId', 'sessionId'}:
        raise ValueError('A current native settings owner is required.')
    if not isinstance(value['ownerId'], str) or not re.fullmatch(r'[A-Za-z0-9-]{1,64}', value['ownerId']):
        raise ValueError('Invalid native settings owner.')
    if value['sessionId'] != session.get('id'):
        raise ValueError('The native settings direction belongs to a previous set.')
    return copy.deepcopy(value)


def create(change, owner, identity, now):
    names = {'camera': 'camera input', 'room': 'the room display', 'roomTheme': 'room appearance',
             'roomDetails': 'room track details', 'showRob': 'the room performer',
             'requests': 'audience requests', 'engine': 'VirtualDJ', 'catalog': 'Catalog'}
    return {**copy.deepcopy(change), **owner, 'id': identity, 'status': 'pending',
            'at': now, 'expiresAt': now + 30,
            'message': 'Updating ' + names[change['target']] + '. Waiting for the native result.'}


def finish(state, command, status, message, now):
    command.update(status=status, message=message, finishedAt=now)
    reply_id = command['id'] + '-reply'
    reply = next((m for m in state['messages'] if m['id'] == reply_id), None)
    if reply:
        reply['text'] = message
    else:
        index = next((i for i,m in enumerate(state['messages']) if m['id'] == command['id']), None)
        if index is not None:
            state['messages'].insert(index + 1, {'id': reply_id, 'role': 'assistant', 'source': 'engine', 'text': message, 'at': now})
        state['messages'] = state['messages'][-24:]
    # A late device result belongs to its own turn, not a newer music direction.
    newest = next((m for m in reversed(state['messages']) if m['role'] == 'user'), {})
    if newest.get('id') == command['id']:
        state.update(message=message)


def expire(state, now, session_id=None, restart=False):
    command = state.get('nativeSettingsCommand')
    if not isinstance(command, dict) or not all(k in command for k in ('id', 'ownerId', 'sessionId', 'status', 'expiresAt')):
        state['nativeSettingsCommand'] = None
        return False
    if (any(not isinstance(command[k],str) or not command[k] for k in ('id','ownerId','sessionId','status'))
            or type(command['expiresAt']) not in (float,int) or not math.isfinite(command['expiresAt'])):
        state['nativeSettingsCommand'] = None
        return False
    if command['status'] not in ACTIVE:
        return False
    if restart or session_id != command['sessionId'] or now > command['expiresAt']:
        finish(state, command, 'unavailable', 'The native setting was not confirmed. It will not be replayed.', now)
        return True
    return False


def delivery(state, session, identity, owner, session_id, status, message, now):
    command = state.get('nativeSettingsCommand')
    if not command or (command['id'], command['ownerId'], command['sessionId']) != (identity, owner, session_id):
        raise ValueError('This native setting belongs to another command or app session.')
    if session.get('id') != session_id or now > command['expiresAt']:
        raise ValueError('This native setting expired. It will not be replayed.')
    if status == 'dispatching':
        if command['status'] != 'pending':
            raise ValueError('This native setting has already been claimed.')
        command['status'] = 'dispatching'
        return
    if status not in ('confirmed', 'unavailable') or not isinstance(message, str) or not 1 <= len(message.strip()) <= 600:
        raise ValueError('A bounded native result is required.')
    if command['status'] == status and command['message'] == message.strip():
        return  # An acknowledgement retry never dispatches the action again.
    if command['status'] != 'dispatching':
        raise ValueError('This native setting was not claimed or has already finished.')
    finish(state, command, status, message.strip(), now)
