"""Read Beatport artwork already supplied to VirtualDJ, away from audio work.

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
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

VDJ_ROOT=Path.home()/'Library/Application Support/VirtualDJ'


def beatport_cover(value):
    if not isinstance(value,str) or len(value)>2048:return None
    try:
        url=urlsplit(value)
        if (url.scheme!='https' or url.hostname not in ('geo-media.beatport.com','media.beatport.com')
                or url.username or url.password or url.port not in (None,443)
                or url.query or url.fragment or not re.fullmatch(r'/image_size/\d{1,4}x\d{1,4}/[a-fA-F0-9-]+\.(?:jpg|jpeg|png|webp)',url.path)):
            return None
    except ValueError:return None
    return value


def recording_id(track_id):
    return 'virtualdj:'+hashlib.sha256(f'netsearch://bp{track_id}'.encode()).hexdigest()


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
                    match=re.fullmatch(r'netsearch://bp([1-9][0-9]{0,11})',song.get('FilePath',''))
                    link=song.find('Link')
                    cover=beatport_cover(link.get('Cover') if link is not None else None)
                    if match and cover:found[recording_id(match[1])]={'url':cover,'provider':'beatport','trackId':match[1],'source':'virtualdj.database'}
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

    def decorate(self,session):
        """Enrich a read snapshot; never modify the engine's planning state."""
        result=copy.deepcopy(session)
        with self.lock:art=dict(self.index)
        rows=list(result.get('order') or [])+list(result.get('candidates') or [])
        rows+=list((result.get('telemetry') or {}).get('decks') or [])
        rows+=list((result.get('backgroundAnalysis') or {}).get('decks') or [])
        rows+=list((result.get('catalogSearch') or {}).get('browseRows') or [])
        rows+=list((result.get('crateDig') or {}).get('browseRows') or [])
        for row in rows:
            evidence=art.get(row.get('id') or row.get('trackId'))
            if evidence:
                row['artworkUrl']=evidence['url'];row['artwork']={**evidence}
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
