"""Read-only, identity-bound native analysis. Never reads streamed audio or auth."""
import hashlib
import json
import math
from pathlib import Path
import time
import xml.etree.ElementTree as ET
from afterhours.dj_bridge import BridgeError
from afterhours.vdj_catalog import identity
from afterhours.audio_analysis import verified_energy
from afterhours.native_database import complete_xml_prefix

class NativeAnalysisUnsettled(BridgeError):
    """A confirmed, paused recording never exposed a stable native grid."""


class NativeAnalysisDatabaseUpdating(ValueError):
    """The native database could not be parsed as a complete document."""


DATABASE = Path.home() / 'Library/Application Support/VirtualDJ/database.xml'
DATABASE_LIMIT = 128 * 1024 * 1024


def analysis_source(database):
    """Handle native NUL padding and a torn closing-tag suffix after the root."""
    with database.open('rb') as source:
        if source.seek(0, 2) > DATABASE_LIMIT:
            raise ValueError('VirtualDJ analysis database exceeds the bounded reader size.')
        xml = complete_xml_prefix(source, DATABASE_LIMIT)
        return database if xml is source else xml


def finite(value, low=0, high=86400):
    try:
        n=float(value)
        return n if math.isfinite(n) and low <= n <= high else None
    except (ValueError,TypeError):
        return None


def track_analysis(tracks, database=DATABASE):
    """Match exact engine identities; filenames/provider locators never leave here."""
    wanted={t['engine']['selection_id']:t for t in tracks}
    found={}
    if not database.is_file() or database.stat().st_size > DATABASE_LIMIT:
        raise ValueError('VirtualDJ analysis database is unavailable or exceeds the bounded reader size.')
    try:
        for _,song in ET.iterparse(analysis_source(database),events=('end',)):
            if song.tag != 'Song':continue
            sid=identity(song.get('FilePath',''))
            if sid not in wanted:
                song.clear();continue
            track=wanted[sid];info=song.find('Infos');scan=song.find('Scan')
            # VirtualDJ may retain an older catalog length in the saved
            # database. A fresh exact-deck total binds the whole-buffer proof.
            duration = finite(track.get('nativeDuration'), .001)
            if duration is None and info is not None:
                duration = finite(info.get('SongLength'))
            points=[]
            for poi in song.findall('Poi')[:128]:
                seconds=finite(poi.get('Pos'))
                kind=poi.get('Type','')
                if seconds is None or duration is None or seconds>duration or kind not in ('automix','remix','cue'):continue
                label=(poi.get('Point') or poi.get('Name') or 'Cue')[:100]
                point_id=hashlib.sha256(f'{sid}:{kind}:{label}:{seconds:.6f}'.encode()).hexdigest()[:16]
                points.append({'id':point_id,'type':kind,'label':label,'seconds':round(seconds,6),
                               'source':'VirtualDJ saved analysis'})
            energy = verified_energy(sid, duration)
            found[sid]={'id':track['id'],'title':track['title'],'artist':track.get('artist',''),
                        'bpm':track['bpm'],'camelot':track.get('camelot'),'genre':track.get('genre',''),
                        'duration':duration,'points':points,'variableTempo':bool(scan is not None and scan.get('BeatGrid')),
                        'vocals':None,'energyMap':energy,'gridAuditioned':False,
                        'limitations':['Saved markers are engine estimates, not verified song sections.',
                                       'No vocal or semantic section detection. Energy does not verify phrase alignment.' if energy else
                                       'No verified decoded-audio energy map is available.']}
            song.clear()
    except ET.ParseError as exc:
        raise NativeAnalysisDatabaseUpdating(
            'VirtualDJ is updating its analysis database. Try preparing again.') from exc
    items=[]
    for track in tracks:
        sid=track['engine']['selection_id']
        items.append(found.get(sid,{'id':track['id'],'title':track['title'],'artist':track.get('artist',''),
                    'bpm':track['bpm'],'camelot':track.get('camelot'),'genre':track.get('genre',''),
                    'duration':None,'points':[],'variableTempo':None,'vocals':None,'energyMap':None,
                    'gridAuditioned':False,'limitations':['No saved analysis matched this exact recording.']}))
    report={'tracks':items,'source':'VirtualDJ native markers and verified local audio features; exact recording identity',
            'databaseModifiedAt':database.stat().st_mtime,'preparedAt':time.time(),
            'audioIncluded':False,'status':'evidence-ready'}
    report['fingerprint']=hashlib.sha256(json.dumps(items,sort_keys=True).encode()).hexdigest()
    return report
