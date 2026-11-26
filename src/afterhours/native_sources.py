"""Provider metadata recognized from exact native catalog locators.

VirtualDJ owns its provider sessions, catalog navigation, and media loading.
The adapter exposes only safe metadata; it never supplies provider credentials.
"""
from dataclasses import dataclass
import re
from urllib.parse import urlsplit


@dataclass(frozen=True)
class NativeSource:
    id: str
    prefix: str
    cover_host: str
    cover_path: str

    def cover(self, value):
        if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 33 or ord(c) > 126 for c in value):
            return None
        try:
            url = urlsplit(value)
            if (url.scheme != 'https' or not re.fullmatch(self.cover_host, url.hostname or '')
                    or url.username or url.password or url.port not in (None, 443)
                    or url.query or url.fragment or not re.fullmatch(self.cover_path, url.path)
                    or any(part in ('.', '..') for part in url.path.split('/'))):
                return None
        except ValueError:
            return None
        return value


BEATPORT = NativeSource('beatport', 'bp', r'(?:geo-media|media)\.beatport\.com',
                        r'/image_size/\d{1,4}x\d{1,4}/[a-fA-F0-9-]+\.(?:jpg|jpeg|png|webp)')
APPLE_MUSIC = NativeSource('apple-music', 'ap', r'is[1-9][0-9]?-ssl\.mzstatic\.com',
                           r'/image/thumb/[A-Za-z0-9._/-]+/[0-9]{1,4}x[0-9]{1,4}[A-Za-z0-9_-]*\.(?:jpg|jpeg|png|webp)')
SOURCES = {source.prefix: source for source in (BEATPORT, APPLE_MUSIC)}


def catalog_reference(locator):
    match = re.fullmatch(r'netsearch://([a-z]{2})([1-9][0-9]{0,17})', locator or '')
    if match and match[1] in SOURCES:
        return SOURCES[match[1]], match[2]
    return None, None
