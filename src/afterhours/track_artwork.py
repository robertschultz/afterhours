"""Read public catalog artwork already supplied to VirtualDJ, away from audio work.

Only public cover metadata is read. No provider credentials, audio downloads,
title matching, provider login, or paid image generation is involved.
"""
from pathlib import Path
from contextlib import closing
import copy
import hashlib
import re
import sqlite3
import threading
from urllib.request import build_opener, HTTPRedirectHandler, Request
from afterhours.native_sources import BEATPORT, SOURCES, catalog_reference
import xml.etree.ElementTree as ET

VDJ_ROOT=Path.home()/'Library/Application Support/VirtualDJ'


def beatport_cover(value):
    return BEATPORT.cover(value)


def recording_id(track_id):
    return 'virtualdj:'+hashlib.sha256(f'netsearch://bp{track_id}'.encode()).hexdigest()


def artwork_path(track_id):
    if isinstance(track_id, str) and re.fullmatch(r'virtualdj:[a-f0-9]{64}', track_id):
        return '/api/track-artwork/'+track_id.removeprefix('virtualdj:')
    return None


class NoArtworkRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, url):
        return None


ARTWORK_OPENER = build_opener(NoArtworkRedirect())
IMAGE_TYPES = {'image/jpeg', 'image/png', 'image/webp'}
MAX_COVER_BYTES = 2_000_000


class TrackArtwork:
    def __init__(self,root=VDJ_ROOT,on_refresh=None):
        self.root=Path(root);self.lock=threading.Lock();self.index={}
        self.on_refresh=on_refresh
        self.stopped=threading.Event();self.worker=None

    def refresh(self):
        found={}
        # The XML link also covers browsed tracks outside the offline cache.
        try:
            path=self.root/'database.xml'
            if path.stat().st_size<=64_000_000:
                for _,song in ET.iterparse(path,events=('end',)):
                    if song.tag!='Song':continue
                    locator = song.get('FilePath','')
                    provider, provider_id = catalog_reference(locator)
                    link = song.find('Link')
                    cover = provider.cover(link.get('Cover')) if provider and link is not None else None
                    if cover:
                        exact_id = 'virtualdj:'+hashlib.sha256(locator.encode()).hexdigest()
                        found[exact_id] = {'url':cover, 'provider':provider.id, 'trackId':provider_id,
                                           'source':'virtualdj.database'}
                    song.clear()
        except (OSError,ET.ParseError):pass
        # Prefer the provider's own cached artwork over a general VDJ cover link.
        for path in sorted((self.root/'Cache/Beatport').glob('*/sqlite3.db')):
            if not (path.parent.name.isdecimal() or path.parent.name=='__guest'):continue
            try:
                with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=0.1)) as db:
                    db.execute('PRAGMA query_only=ON')
                    for track_id,raw in db.execute('SELECT id,album_artwork FROM tracks WHERE album_artwork IS NOT NULL LIMIT 100000'):
                        if not re.fullmatch(r'[1-9][0-9]{0,11}',str(track_id)):continue
                        cover=beatport_cover(raw)
                        if cover:found[recording_id(track_id)]={'url':cover,'provider':'beatport','trackId':str(track_id),'source':'virtualdj.beatport-cache'}
            except (OSError,sqlite3.Error):continue
        with self.lock:self.index=found
        if self.on_refresh:
            try:self.on_refresh(found)
            except (OSError,ValueError,sqlite3.Error):pass
        return len(found)

    def lookup(self,track_id):
        with self.lock:return copy.deepcopy(self.index.get(track_id))

    def fetch(self, track_id, opener=ARTWORK_OPENER):
        evidence = self.lookup(track_id)
        if not evidence:
            return None
        provider = next((p for p in SOURCES.values() if p.id == evidence['provider']), None)
        url = provider.cover(evidence['url']) if provider else None
        if not url:
            return None
        try:
            request = Request(url, headers={'Accept':'image/jpeg, image/png, image/webp'})
            with opener.open(request, timeout=4) as response:
                media = response.headers.get_content_type()
                content = response.read(MAX_COVER_BYTES + 1) if media in IMAGE_TYPES else b''
                if content and len(content) <= MAX_COVER_BYTES:
                    return media, content
        except OSError:
            return None
        return None

    def decorate(self,session):
        """Enrich a read snapshot; never modify the engine's planning state."""
        result=copy.deepcopy(session)
        with self.lock:art=dict(self.index)
        rows=list(result.get('order') or [])+list(result.get('candidates') or [])
        rows += list((result.get('queuedUpdate') or {}).get('tracks') or [])
        rows += list(result.get('library') or ())
        if result.get('nextTrack'):
            rows.append(result['nextTrack'])
        rows+=list((result.get('telemetry') or {}).get('decks') or [])
        rows+=list((result.get('backgroundAnalysis') or {}).get('decks') or [])
        for row in rows:
            evidence=art.get(row.get('id') or row.get('trackId'))
            if evidence:
                row['artworkUrl']=evidence['url'];row['artwork']={**evidence}
                row['artworkPath'] = artwork_path(row.get('id') or row.get('trackId'))
        return result

    def start(self):
        if self.worker and self.worker.is_alive():return
        self.stopped.clear()
        def run():
            while not self.stopped.is_set():
                self.refresh()
                self.stopped.wait(30)
        self.worker=threading.Thread(target=run,name='afterhours-cover-metadata',daemon=True);self.worker.start()

    def close(self):
        self.stopped.set()
        if self.worker:self.worker.join(timeout=1)
