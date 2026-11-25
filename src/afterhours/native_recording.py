"""Private exact recording lookup in VirtualDJ's local metadata database.

No provider, audio, entitlement or title matching. Raw locators stay in memory
and are never part of a session snapshot, archive record or public receipt.
"""
from collections import OrderedDict
from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import re
import stat
import threading
import xml.etree.ElementTree as ET

from afterhours.dj_bridge import BridgeError


DATABASE = Path.home() / 'Library/Application Support/VirtualDJ/database.xml'
MAX_DATABASE_BYTES = 128 * 1024 * 1024


def _digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _supported(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 4096 or
            any(ord(c) < 32 or ord(c) == 127 for c in value)):
        return False
    # A saved opaque streaming identifier is not an entitlement or provider
    # name. Never turn arbitrary http(s), file, device or executable URIs into
    # a direct load. Local absolute filenames remain literal OSC string data.
    return bool(re.fullmatch(r'netsearch://[A-Za-z0-9._-]{1,200}', value) or
                (value.startswith('/') and not value.startswith('//') and '://' not in value))


@dataclass(frozen=True, slots=True)
class ExactRecording:
    selection_id: str
    _locator: str = field(repr=False)

    def value_for(self, expected_id):
        if (not re.fullmatch(r'[a-f0-9]{64}', str(expected_id)) or
                expected_id != self.selection_id or _digest(self._locator) != expected_id or
                not _supported(self._locator)):
            raise BridgeError('The private recording locator does not match the requested exact identity.')
        return self._locator


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class _BoundedReader:
    def __init__(self, stream):
        self.stream = stream
        self.count = 0

    def read(self, size=-1):
        data = self.stream.read(min(size if size >= 0 else MAX_DATABASE_BYTES + 1,
                                    MAX_DATABASE_BYTES + 1 - self.count))
        self.count += len(data)
        if self.count > MAX_DATABASE_BYTES:
            raise BridgeError('The native recording database exceeds the bounded reader size.')
        return data


class NativeRecordings:
    def __init__(self, database=DATABASE, cache_size=64):
        self.database = Path(database)
        self.cache_size = cache_size
        self.lock = threading.Lock()
        self.signature = None
        self.cache = OrderedDict()

    def find(self, selection_id):
        if not isinstance(selection_id, str) or not re.fullmatch(r'[a-f0-9]{64}', selection_id):
            raise BridgeError('Exact native lookup needs a recording identity.')
        with self.lock:
            try:
                before = self.database.stat()
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise BridgeError('The native recording database could not be inspected.') from exc
            if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_DATABASE_BYTES:
                raise BridgeError('The native recording database is unavailable or exceeds the bounded reader size.')
            signature = _signature(before)
            if signature != self.signature:
                self.cache.clear()
                self.signature = signature
            if selection_id in self.cache:
                self.cache.move_to_end(selection_id)
                return self.cache[selection_id]
            found = None
            matches = 0
            try:
                with self.database.open('rb') as stream:
                    for _, song in ET.iterparse(_BoundedReader(stream), events=('end',)):
                        if song.tag != 'Song':
                            continue
                        locator = song.get('FilePath', '')
                        if locator and _digest(locator) == selection_id:
                            matches += 1
                            if matches > 1:
                                raise BridgeError('The native database has ambiguous duplicate entries for this recording.')
                            if not _supported(locator):
                                raise BridgeError('The saved recording uses a locator that direct restoration does not support.')
                            found = ExactRecording(selection_id, locator)
                        song.clear()
                if _signature(self.database.stat()) != signature:
                    raise BridgeError('The native recording database changed during lookup; no load was sent.')
            except ET.ParseError as exc:
                raise BridgeError('The native recording database is updating; exact restoration is unconfirmed.') from exc
            except (OSError, UnicodeError) as exc:
                raise BridgeError('The native recording database could not be read; exact restoration is unconfirmed.') from exc
            self.cache[selection_id] = found
            while len(self.cache) > self.cache_size:
                self.cache.popitem(last=False)
            return found


RECORDINGS = NativeRecordings()
