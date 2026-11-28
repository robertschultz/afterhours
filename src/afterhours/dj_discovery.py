"""Public popularity signals and catalog relationships for Afterhours planning."""
import json
import re
import threading
import time
import unicodedata
from collections import Counter
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from afterhours.paths import data_root

DANCE = 'https://itunes.apple.com/us/rss/topsongs/limit=50/genre=17/json'
LISTENBRAINZ = 'https://api.listenbrainz.org/1/'
HOUSE_STYLES = ('house', 'deep house', 'tech house')
MAX_AGE = 3 * 86400
FEED_ERRORS = (OSError, URLError, ValueError, KeyError, TypeError, AttributeError)


def normalized(value):
    return ' '.join(re.findall(r'[^\W_]+', unicodedata.normalize('NFKC', str(value or '')).casefold()))


def credit(value):
    return frozenset(credited_names(value))


def credited_names(value):
    """Map normalized credit names to their original spelling."""
    return {key: name.strip()
            for name in re.split(r'[,;]|\s+(?:&|feat\.?|ft\.?)\s+', str(value or ''), flags=re.I)
            if (key := normalized(name))}


def identity(title, artist):
    return normalized(title), tuple(sorted(credit(artist)))


def read_json(url):
    request = Request(url, headers={'User-Agent': 'Afterhours/1.0 DJ chart discovery', 'Accept': 'application/json'})
    with urlopen(request, timeout=7) as response:
        content = response.read(1_000_001)
    if len(content) > 1_000_000:
        raise ValueError('Music discovery response exceeds 1 MB')
    return json.loads(content)


def dance_chart(now, fetch):
    feed = fetch(DANCE)['feed']
    return {'source': 'iTunes Store US Dance Top Songs', 'observedAt': now,
            'publishedAt': feed.get('updated', {}).get('label'),
            'rows': [{'title': row['im:name']['label'], 'artist': row['im:artist']['label'], 'rank': rank}
                     for rank, row in enumerate(feed.get('entry', [])[:50], 1)]}


def listenbrainz_style(style, now, fetch):
    candidates = fetch(LISTENBRAINZ + 'lb-radio/tags?' +
                       urlencode({'tag': style, 'pop_begin': 55, 'pop_end': 100, 'count': 30}))
    # Artist-level tags alone can give unrelated genres from an artist's catalog.
    candidates = [row for row in candidates if row.get('source') in ('recording', 'release-group')
                  and re.fullmatch(r'[a-f0-9-]{36}', str(row.get('recording_mbid')))]
    ids = list(dict.fromkeys(row['recording_mbid'] for row in candidates))
    metadata = fetch(LISTENBRAINZ + 'metadata/recording/?' + urlencode({'recording_mbids': ','.join(ids), 'inc': 'artist release'})) if ids else {}
    rows = []
    for item in candidates:
        info = metadata.get(item['recording_mbid']) or {}
        row = info.get('recording') or {}
        title, artist = row.get('name'), (info.get('artist') or {}).get('name')
        year = (info.get('release') or {}).get('year')
        percentile = item.get('percent')
        if title and artist and type(percentile) in (float, int) and 0 <= percentile <= 100:
            rows.append({'title': title, 'artist': artist, 'percentile': round(percentile, 1),
                         **({'releaseYear': year} if isinstance(year, int) else {})})
    return {'source': 'ListenBrainz genre-tagged popularity', 'observedAt': now, 'style': style, 'rows': rows}


def style_for(request):
    text = normalized(request)
    for style in ('tech house', 'deep house', 'house'):
        if re.search(r'\b'+style+r'\b', text):
            return style
    return ''


def set_slot(request):
    text=normalized(request)
    for name, pattern, target in (
        ('closing',r'\b(closing set|last track|wind down|after hours|afterhours set)\b',.40),
        ('warm-up',r'\b(warm up|warmup|opening set|early evening)\b',.38),
        ('peak hour',r'\b(peak hour|peak time|midnight set|main room peak)\b',.86),
        ('build',r'\b(build up|build the energy|build toward|build the set)\b',.64),
    ):
        if re.search(pattern,text):
            return {'name':name,'targetEnergy':target}
    return None


class Discovery:
    def __init__(self, fetch=read_json, clock=time.time):
        self.fetch, self.clock = fetch, clock
        self.stop = threading.Event()
        self.worker = None
        self.feeds = {}

    def refresh(self):
        now = self.clock()
        feeds = dict(self.feeds)
        for key in ('dance', *HOUSE_STYLES):
            if self.stop.is_set():
                break
            try:
                feed = dance_chart(now, self.fetch) if key == 'dance' else listenbrainz_style(key, now, self.fetch)
                if feed['rows']:
                    feeds[key] = feed
                    self.feeds = dict(feeds)
            except FEED_ERRORS:
                continue
        return feeds

    def start(self):
        path = data_root() / 'music-discovery.json'
        try:
            with path.open() as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                self.feeds = data
        except FEED_ERRORS:
            pass
        self.stop.clear()

        def run():
            while not self.stop.is_set():
                feeds = self.refresh()
                if feeds:
                    try:
                        temporary = path.with_suffix('.tmp')
                        temporary.write_text(json.dumps(feeds))
                        temporary.replace(path)
                    except OSError:
                        pass
                self.stop.wait(6*3600)

        self.worker = threading.Thread(target=run, name='afterhours-music-discovery', daemon=True)
        self.worker.start()

    def close(self):
        self.stop.set()

    def fresh(self, key):
        feed = self.feeds.get(key)
        if (isinstance(feed, dict) and isinstance(feed.get('observedAt'), (int, float))
                and isinstance(feed.get('rows'), list) and 0 <= self.clock()-feed['observedAt'] <= MAX_AGE):
            return feed
        return None

    def evidence(self, track):
        key = identity(track.get('title'), track.get('artist'))
        result = {}
        dance = self.fresh('dance')
        if dance:
            hit = next((row for row in dance['rows'] if identity(row['title'], row['artist']) == key), None)
            if hit:
                result['danceChart'] = {'rank': hit['rank'], 'source': dance['source'],
                                        'observedAt': dance['observedAt']}
        preferred = style_for(track.get('genre')) or 'house'
        feed = self.fresh(preferred)
        if feed:
            hit = next((row for row in feed['rows'] if identity(row['title'], row['artist']) == key), None)
            if hit:
                result['genrePopularity'] = {'percentile': hit['percentile'], 'source': feed['source'],
                                             'style': preferred, 'observedAt': feed['observedAt']}
        return result

    def hints(self, style, planned, *, searched=()):
        feed = self.fresh(style)
        if not feed:
            return []
        known = {identity(track.get('title'), track.get('artist')) for track in planned}
        used = set()
        results = []
        # Give the DJ a mix of familiar and less familiar directions.
        rows = sorted(feed['rows'], key=lambda row: -row['percentile'])
        current = [row for row in rows if row.get('releaseYear', 0) >= time.gmtime(self.clock()).tm_year-1]
        for row in current[:2] + rows[:len(rows)//2:2] + rows[len(rows)//2::2] + rows:
            artist = credit(row['artist'])
            query = normalized(f"{row['artist']} {row['title']}")
            if query in searched or artist & used or identity(row['title'], row['artist']) in known:
                continue
            used.update(artist)
            results.append({**row, 'source': feed['source'], 'style': style, 'observedAt': feed['observedAt']})
            if len(results) >= 6:
                break
        return results


DISCOVERY = Discovery()


def relationships(track, anchors, played):
    artist = credit(track.get('artist'))
    label = normalized(track.get('label'))
    genre = normalized(track.get('genre'))
    facts = {}
    repeat = sum(bool(artist & credit(row.get('artist'))) for row in played)
    if repeat:
        facts['recentArtistAppearances'] = repeat
    for name, field, value in [('sharedArtists', 'artist', artist), ('sharedLabel', 'label', label),
                               ('sharedGenre', 'genre', genre)]:
        related = []
        for anchor in anchors:
            if track['id'] == anchor.get('id'):
                continue
            same = (value & credit(anchor.get(field))) if field == 'artist' else value and value == normalized(anchor.get(field))
            if same and anchor.get('id'):
                related.append(anchor['id'])
        if related:
            facts[name] = related
    return facts


def rank_candidates(tracks, requested_style, anchors, recent, preferred, discovery=DISCOVERY, set_slot=None):
    """Weigh evidence and diversify supply without claiming that a candidate is loadable."""
    tracks=list(tracks)
    graph={}
    for record in tracks:
        artists=credit(record.get('artist'))
        for artist in artists:
            graph.setdefault(artist,set()).update(artists-{artist})
    seed_artists=set().union(*(credit(anchor.get('artist')) for anchor in anchors))
    related=set().union(*(graph.get(artist,set()) for artist in seed_artists))-seed_artists
    facts = {}
    ranked = []
    for track in tracks:
        track_id = track['id']
        edges = relationships(track, anchors, recent)
        related_credit=credit(track.get('artist')) & related
        if related_credit:
            edges['collaborationLinks']=sorted(related_credit)[:4]
        evidence = discovery.evidence(track)
        facts[track_id] = {**edges, **evidence}
        genre = style_for(track.get('genre'))
        score = (3 if requested_style and genre == requested_style else 0)
        if not requested_style and edges.get('sharedGenre'):
            score += 1.25
        score += 1.2 if edges.get('sharedLabel') else 0
        score += .7 if edges.get('sharedArtists') else 0
        score += .8 if related_credit else 0
        energy=track.get('energy')
        if set_slot and type(energy) in (int,float) and 0<=energy<=1:
            score += 1.5*(1-abs(energy-set_slot['targetEnergy']))
        score -= 3.5 * edges.get('recentArtistAppearances', 0)
        chart = evidence.get('danceChart')
        if chart:
            score += 1.25*(51-chart['rank'])/50
        if evidence.get('genrePopularity'):
            score += .85*evidence['genrePopularity']['percentile']/100
        if str(track.get('year')).isdigit():
            score += .4 if int(track['year']) >= time.gmtime(discovery.clock()).tm_year-1 else 0
        relevance, tempo = preferred(track)
        rank = (relevance, -(score-min(tempo,20)*.055), tempo)
        ranked.append((rank, track))
    ranked.sort(key=lambda item: item[0])
    appearances = Counter()
    result = []
    for (relevance, score, tempo), track in ranked:
        names = credit(track.get('artist'))
        penalty = max((appearances[name] for name in names), default=0) * 1.4
        appearances.update(names)
        result.append(((relevance, score+penalty, tempo),track))
    result.sort(key=lambda item: item[0])
    return [track for _,track in result], facts
