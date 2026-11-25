"""Persistent metadata for every recording considered by the DJ.

The archive never opens audio, talks to a provider, or changes a queue. Its
stable slots back the Catalog shelves; a separate, bounded session pool backs
planning. Exact engine identities remain mandatory when a record is loaded.
"""
import copy
import json
import math
from pathlib import Path
import re
import sqlite3
import threading
import time


FIELDS = set(('schema id engine catalog title artist album genre remix label year '
              'bpm key camelot duration energy playback_verified provenance '
              'missing_fields engineChecked artworkUrl artwork tags reason').split())
MAX_PAGE_SIZE = 64
MAX_INCLUDED = 64
WORKING_LIMIT = 48


def _known_duration(record):
    value = record.get('duration')
    return value if type(value) in (int, float) and math.isfinite(value) and 0 < value <= 604800 else 0


def _sample_art(record):
    result = {key: str(record.get(key) or '')[:300] for key in ('id', 'title', 'artist')}
    artwork = record.get('artworkUrl')
    if isinstance(artwork, str) and 0 < len(artwork) <= 2048:
        result['artworkUrl'] = artwork
    return result


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _metadata(track):
    if (not isinstance(track, dict) or not isinstance(track.get('id'), str)
            or not 1 <= len(track['id']) <= 200):
        raise ValueError('A crate record needs an exact recording identity.')
    result = {k: copy.deepcopy(v) for k, v in track.items() if k in FIELDS}
    if len(_json(result)) > 64_000:
        raise ValueError('Crate metadata is too large.')
    return result


def _merge(previous, incoming, *, measured=False):
    result = copy.deepcopy(previous)
    for key, value in incoming.items():
        # Optional unread browser fields do not erase previously known facts.
        if value is not None and value != '' and value != []:
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = {**result[key], **value}
            else:
                result[key] = value
        elif key not in result:
            result[key] = value
    # Browser tags can lag a measured grid. A later successful native analysis
    # may refine these values, but another metadata search must not undo it.
    if measured:
        # A fresh native unknown key is also evidence: do not keep an older
        # known key merely because sparse browser metadata preserves blanks.
        for key in ('bpm', 'key', 'camelot'):
            if key in incoming:
                result[key] = copy.deepcopy(incoming[key])
        result['engineChecked'] = True
    elif previous.get('engineChecked'):
        for key in ('bpm', 'key', 'camelot', 'engineChecked'):
            if key in previous:
                result[key] = previous[key]
        if isinstance(previous.get('provenance'), dict):
            result.setdefault('provenance', {})
            for key in ('bpm', 'key', 'camelot'):
                if key in previous['provenance']:
                    result['provenance'][key] = copy.deepcopy(previous['provenance'][key])
    if isinstance(result.get('missing_fields'), list):
        result['missing_fields'] = [k for k in result['missing_fields'] if result.get(k) is None]
    return result


class RecordCrate:
    def __init__(self, path=None, clock=time.time):
        self.lock = threading.RLock()
        self.clock = clock
        self.availability = None
        self.path = Path(path) if path is not None else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.is_symlink():
                raise ValueError('The local record crate cannot be a symbolic link.')
        self.db = sqlite3.connect(str(self.path) if self.path else ':memory:', check_same_thread=False, timeout=2)
        self.db.row_factory = sqlite3.Row
        if self.path:
            self.path.chmod(0o600)
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=NORMAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS crate_meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
            INSERT OR IGNORE INTO crate_meta VALUES ('revision', 0);
            CREATE TABLE IF NOT EXISTS records (
                slot INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
                metadata TEXT NOT NULL, origin TEXT NOT NULL,
                hard_text TEXT NOT NULL, soft_text TEXT NOT NULL,
                first_seen REAL NOT NULL, last_seen REAL NOT NULL,
                analysis_status TEXT NOT NULL, analyzed_at REAL
            );
            CREATE TABLE IF NOT EXISTS dig_terms (
                term TEXT PRIMARY KEY, dug_at REAL NOT NULL, examined INTEGER NOT NULL, added INTEGER NOT NULL
            );
        ''')
        self.db.commit()
        self._summary = {'count': self.db.execute('SELECT count(*) FROM records').fetchone()[0],
                         'revision': self.db.execute("SELECT value FROM crate_meta WHERE key='revision'").fetchone()[0]}
        # One startup pass; subsequent snapshots copy only these aggregates
        # and five real covers. No archive scan on the UI/telemetry lane.
        self._library = {'knownDurationSeconds': 0, 'durationKnownCount': 0, 'sampleArt': []}
        for row in self.db.execute('SELECT metadata FROM records ORDER BY slot'):
            metadata = json.loads(row['metadata'])
            duration = _known_duration(metadata)
            self._library['knownDurationSeconds'] += duration
            self._library['durationKnownCount'] += int(duration > 0)
            if len(self._library['sampleArt']) < 5:
                self._library['sampleArt'].append(_sample_art(metadata))

    # --- Names the digger may follow -------------------------------------
    DIG_AGAIN_SECONDS = 7 * 24 * 3600
    DIG_EMPTY_AGAIN_SECONDS = 3600   # a name that showed nothing is tried again sooner

    @staticmethod
    def _dig_names(metadata):
        """Artists (split out of collaborations), then the label and genre."""
        artists = [part.strip() for part in re.split(r',|&| feat\.? | ft\.? | x ', str(metadata.get('artist') or ''), flags=re.I)]
        for kind, values in (('artist', artists), ('label', [metadata.get('label')]), ('genre', [metadata.get('genre')])):
            for value in values:
                value = ' '.join(str(value or '').split())
                if 2 <= len(value) <= 80 and not any(ord(c) < 32 or ord(c) == 127 or c in '"`/\\*?[]{}#' for c in value):
                    yield kind, value

    def dig_candidates(self, limit=8):
        """Names already in the crate worth searching for, most common first.

        Artists come before labels and genres: an artist search is narrow and
        mostly new, a genre search is broad and mostly known. A name dug
        recently is left alone until it is stale.
        """
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError('Ask for 1 to 200 names.')
        with self.lock:
            now = self.clock()
            recent = {row['term'].casefold() for row in self.db.execute(
                'SELECT term FROM dig_terms WHERE (examined>0 AND dug_at>?) OR (examined=0 AND dug_at>?)',
                (now - self.DIG_AGAIN_SECONDS, now - self.DIG_EMPTY_AGAIN_SECONDS))}
            counts = {}
            for row in self.db.execute('SELECT metadata FROM records ORDER BY slot'):
                for kind, name in self._dig_names(json.loads(row['metadata'])):
                    key = name.casefold()
                    if key in recent:
                        continue
                    entry = counts.setdefault(key, [kind, name, 0])
                    entry[2] += 1
            rank = {'artist': 0, 'label': 1, 'genre': 2}
            ordered = sorted(counts.values(), key=lambda e: (rank[e[0]], -e[2]))
            return [name for _, name, _ in ordered[:limit]]

    def facts(self, limit=20000):
        """The plain facts of every stored recording, newest slot last.

        For the planner's lookups only: one read when an evaluation starts,
        never on the telemetry or UI lane.
        """
        with self.lock:
            return [json.loads(row['metadata']) for row in
                    self.db.execute('SELECT metadata FROM records ORDER BY slot LIMIT ?', (int(limit),))]

    def mark_dug(self, term, examined, added):
        with self.lock, self.db:
            self.db.execute('INSERT INTO dig_terms (term,dug_at,examined,added) VALUES (?,?,?,?) '
                            'ON CONFLICT(term) DO UPDATE SET dug_at=excluded.dug_at,examined=excluded.examined,added=dig_terms.added+excluded.added',
                            (str(term)[:160], self.clock(), int(examined), int(added)))

    def dig_summary(self):
        with self.lock:
            row = self.db.execute('SELECT count(*) AS terms, coalesce(sum(added),0) AS added FROM dig_terms WHERE examined>0').fetchone()
            return {'termsDug': row['terms'], 'recordsFound': row['added']}

    def close(self):
        with self.lock:
            self.db.close()

    def summary(self):
        # No disk work or full-library copies on an engine/UI snapshot.
        with self.lock:
            return dict(self._summary)

    def library_summary(self):
        with self.lock:
            return {**self._summary, **copy.deepcopy(self._library)}

    def _record(self, row):
        if row is None:
            return None
        origin = json.loads(row['origin'])
        result = {**json.loads(row['metadata']), 'crateSlot': row['slot'] - 1,
                'crate': {'firstSeen': row['first_seen'], 'lastSeen': row['last_seen'],
                          'sources': origin.get('sources', []), 'queries': origin.get('queries', []),
                          'analysisStatus': row['analysis_status'], 'analyzedAt': row['analyzed_at']}}
        if self.availability:
            blocked=self.availability.blocked(result['id'])
            if blocked and blocked['code']=='provider-preview-only':result['playback_verified']=False
        return result

    def get(self, track_id):
        with self.lock:
            return self._record(self.db.execute('SELECT * FROM records WHERE id=?', (track_id,)).fetchone())

    def locator(self, track_id):
        """Only the saved search/cursor, never a raw provider/file locator."""
        with self.lock:
            row = self.db.execute('SELECT origin FROM records WHERE id=?', (track_id,)).fetchone()
            return copy.deepcopy(json.loads(row[0]).get('locator')) if row else None

    def upsert(self, track, *, source='session', query=None, position=None, analysis=None):
        """Return (record, inserted). Re-reading identical metadata is inert."""
        incoming = _metadata(track)
        if self.availability:
            blocked=self.availability.blocked(incoming['id'])
            if blocked and blocked['code']=='provider-preview-only':incoming['playback_verified']=False
        if analysis not in (None, 'attempted', 'analyzed'):
            raise ValueError('Unknown crate analysis status.')
        source = str(source)[:80]
        if query is not None and (not isinstance(query, str) or len(query) > 160):
            raise ValueError('A crate search must have at most 160 characters.')
        if position is not None and (type(position) is not int or not 0 <= position <= 10000):
            raise ValueError('Invalid crate search position.')
        with self.lock, self.db:
            old = self.db.execute('SELECT * FROM records WHERE id=?', (incoming['id'],)).fetchone()
            # engineChecked in a saved queue is not a fresh measurement. Only
            # the successful native-analysis path can replace measured facts.
            measured = analysis == 'analyzed' and source == 'analysis'
            previous = json.loads(old['metadata']) if old else {}
            meta = _merge(previous, incoming, measured=measured)
            origin = json.loads(old['origin']) if old else {'sources': [], 'queries': []}
            # A dig passes every known recording again and again. Where a
            # recording was first found is the better way back to it, and
            # seeing it again is not a change to the crate.
            passing = source == 'dig' and old is not None and bool(origin.get('locator'))
            if source and source not in origin['sources'] and not passing:
                origin['sources'] = (origin['sources'] + [source])[-16:]
            if query and not passing:
                if query not in origin['queries']:
                    origin['queries'] = (origin['queries'] + [query])[-16:]
                origin['locator'] = {'query': query, 'position': position or 0}
            status = old['analysis_status'] if old else 'considered'
            if analysis == 'analyzed' or meta.get('engineChecked') is True:
                status = 'analyzed'
            elif analysis == 'attempted' and status != 'analyzed':
                status = 'attempted'
            metadata, provenance = _json(meta), _json(origin)
            if old and old['metadata'] == metadata and old['origin'] == provenance and old['analysis_status'] == status:
                return self._record(old), False
            now = self.clock()
            analyzed_at = now if measured else ((old['analyzed_at'] if old else None) or (now if status == 'analyzed' else None))
            hard = ' '.join(str(meta.get(k) or '') for k in ('title', 'artist')).casefold()
            soft = ' '.join(str(meta.get(k) or '') for k in ('genre', 'tags', 'reason', 'album', 'label', 'remix')).casefold()
            if old:
                self.db.execute('UPDATE records SET metadata=?,origin=?,hard_text=?,soft_text=?,last_seen=?,analysis_status=?,analyzed_at=? WHERE id=?',
                                (metadata, provenance, hard, soft, now, status, analyzed_at, incoming['id']))
            else:
                self.db.execute('INSERT INTO records (id,metadata,origin,hard_text,soft_text,first_seen,last_seen,analysis_status,analyzed_at) VALUES (?,?,?,?,?,?,?,?,?)',
                                (incoming['id'], metadata, provenance, hard, soft, now, now, status, analyzed_at))
            self.db.execute("UPDATE crate_meta SET value=value+1 WHERE key='revision'")
            self._summary['revision'] += 1
            self._summary['count'] += int(old is None)
            before_duration, duration = _known_duration(previous), _known_duration(meta)
            self._library['knownDurationSeconds'] += duration - before_duration
            self._library['durationKnownCount'] += int(duration > 0) - int(before_duration > 0)
            sample = next((i for i, row in enumerate(self._library['sampleArt']) if row['id'] == incoming['id']), None)
            if sample is not None:
                self._library['sampleArt'][sample] = _sample_art(meta)
            elif len(self._library['sampleArt']) < 5:
                self._library['sampleArt'].append(_sample_art(meta))
            return self._record(self.db.execute('SELECT * FROM records WHERE id=?', (incoming['id'],)).fetchone()), old is None

    def remember(self, records, *, source='session'):
        return [self.upsert(record, source=source)[0] for record in records]

    def refresh_artwork(self, index):
        """Enrich only already-considered IDs on the artwork worker thread."""
        with self.lock:
            # Fetch IDs only. Neither a provider cache nor this metadata index
            # implicitly imports an entire external catalog into the crate.
            wanted = [r[0] for r in self.db.execute('SELECT id FROM records') if r[0] in index]
        changed = 0
        for track_id in wanted:
            evidence = index[track_id]
            record = self.get(track_id)
            if record.get('artworkUrl') == evidence['url'] and record.get('artwork') == evidence:
                continue
            self.upsert({'id': track_id, 'artworkUrl': evidence['url'], 'artwork': evidence}, source='artwork')
            changed += 1
        return changed

    def page(self, page=0, page_size=28, include=()):
        if type(page) is not int or not 0 <= page <= 1_000_000:
            raise ValueError('Choose a nonnegative Catalog page.')
        if type(page_size) is not int or not 1 <= page_size <= MAX_PAGE_SIZE:
            raise ValueError('Choose 1–64 records per Catalog page.')
        if (not isinstance(include, (list, tuple)) or len(include) > MAX_INCLUDED
                or any(not isinstance(v, str) or not 1 <= len(v) <= 200 for v in include)):
            raise ValueError('Include at most 64 exact Catalog recording identities.')
        include = list(dict.fromkeys(include))
        with self.lock:
            summary = dict(self._summary)
            pages = max(1, math.ceil(summary['count'] / page_size))
            page = min(page, pages - 1)
            rows = self.db.execute('SELECT * FROM records ORDER BY slot LIMIT ? OFFSET ?', (page_size, page * page_size)).fetchall()
            ids = {r['id'] for r in rows}
            missing = [i for i in include if i not in ids]
            if missing:
                extra = self.db.execute('SELECT * FROM records WHERE id IN (' + ','.join('?' for _ in missing) + ') ORDER BY slot', missing).fetchall()
                rows.extend(extra)
            return {**summary, 'page': page, 'pageSize': page_size, 'pages': pages,
                    'records': [self._record(row) for row in rows]}

    def search(self, text, limit=3):
        # Rank in SQLite and materialize only the bounded results. The archive
        # size never becomes a planner prompt or a frequent UI payload.
        from afterhours.catalog_browser import query_words
        if not isinstance(text, str) or len(text) > 1600 or type(limit) is not int or not 1 <= limit <= 32:
            raise ValueError('Invalid Catalog search.')
        terms = list(dict.fromkeys(query_words(text)))[:32]
        if not terms:
            return []
        weights, values = [], []
        for term in terms:
            weights.append('(CASE WHEN instr(hard_text,?)>0 THEN 3 WHEN instr(soft_text,?)>0 THEN 1 ELSE 0 END)')
            values.extend((term, re.sub(r'(er|y|s)$', '', term)))
        with self.lock:
            rows = self.db.execute('SELECT *, (' + '+'.join(weights) + ') AS score FROM records WHERE score>0 ORDER BY score DESC,slot LIMIT ?', values + [limit]).fetchall()
            return [self._record(row) for row in rows]


def page_parameters(query):
    """Parse urllib.parse_qs output with a bounded inclusion list."""
    try:
        page = int(query.get('page', ['0'])[0])
        size = int(query.get('pageSize', ['28'])[0])
    except (ValueError, TypeError, IndexError):
        raise ValueError('Catalog page and pageSize must be integers.') from None
    include = [v for group in query.get('include', []) for v in group.split(',') if v]
    return page, size, include
