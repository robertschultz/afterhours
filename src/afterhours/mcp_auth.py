"""Private local MCP credentials. No discovery, token bootstrap or service launch."""
import json
import os
from pathlib import Path
import stat
from urllib.parse import urlsplit


class ControlError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def local_url(value):
    if not isinstance(value, str):
        raise ControlError('invalid-configuration', 'An explicit local engine URL is required.')
    try:
        url = urlsplit(value)
        valid = (url.scheme == 'http' and url.hostname == '127.0.0.1'
                 and url.port is not None and 1024 <= url.port <= 65535
                 and not url.username and not url.password and url.path in ('', '/')
                 and not url.query and not url.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise ControlError('invalid-configuration', 'Use an explicit http://127.0.0.1:PORT engine URL.')
    return f'http://127.0.0.1:{url.port}'


def private_json(path):
    """Refuse symlinks, another owner, and group/world-readable credentials."""
    path = Path(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > 8192):
            raise ControlError('invalid-configuration', 'Credentials must be an owner-only regular file.')
        with os.fdopen(fd, 'r') as stream:
            fd = None
            value = json.load(stream)
        if not isinstance(value, dict):
            raise ValueError('Expected an object')
        return value
    except (ValueError, UnicodeError):
        raise ControlError('invalid-configuration', 'The local credential file is invalid.') from None
    finally:
        if fd is not None:
            os.close(fd)


def write_credentials(path, value):
    """An explicit pairing command creates a new file; never overwrites one."""
    fd = os.open(Path(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL
                 | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, separators=(',', ':'))
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())


def client_config(path):
    value = private_json(path)
    if (set(value) != {'version', 'url', 'clientId', 'credential'} or value['version'] != 1
            or not isinstance(value['credential'], str) or not isinstance(value['clientId'], str)
            or not value['credential'].startswith(value['clientId'] + '.')):
        raise ControlError('invalid-configuration', 'The MCP client credential file is invalid.')
    return {**value, 'url': local_url(value['url'])}
