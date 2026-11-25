"""Approved, named character references for an explicitly started live session."""
import hashlib
import json
from pathlib import Path


class DirectorCast:
    def __init__(self, profiles, gate, web):
        self.profiles, self.gate, self.web = profiles, gate, Path(web)
        self.prepared = None

    def roster(self):
        db = self.profiles.snapshot()
        return {'agents': [{'id': p['id'], 'name': p['name'],
                            'poster': self.profiles.visual(p['id'])['poster']}
                           for p in db['profiles'] if p.get('approvedFrame')]}

    def resolve(self, ids, lead):
        if (not isinstance(ids, list) or not 2 <= len(ids) <= 4 or
                any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids) or lead not in ids):
            raise ValueError('Choose two to four DJs, including the current performer.')
        cast = [self.profiles.visual(i) for i in ids]
        if any(not p['poster'] for p in cast):
            raise ValueError('Every DJ needs an approved performer frame in Agents.')
        if len({p['name'].casefold() for p in cast}) != len(cast):
            raise ValueError('Give each cast member a distinct name in Agents.')
        files = [(self.web / p['poster'].lstrip('/')).resolve() for p in cast]
        if any(not f.is_relative_to(self.web.resolve()) or not f.is_file() for f in files):
            raise ValueError('An approved performer image is unavailable.')
        contents = [f.read_bytes() for f in files]
        if any(len(c) > 5_000_000 for c in contents):
            raise ValueError('An approved performer image exceeds 5 MB.')
        for p, content in zip(cast, contents):
            p['referenceKey'] = p['id'] + '-' + hashlib.sha256(
                json.dumps(p, sort_keys=True).encode() + content).hexdigest()[:20]
        return cast, contents

    def prepare(self, ids, lead, consent):
        if consent is not True:
            raise ValueError('Confirm uploading the selected cast references.')
        cast, contents = self.resolve(ids, lead)
        # Validate the complete cast before any upload. Only approved local frames
        # are sent, and a changed profile/image receives a different cache key.
        for p, content in zip(cast, contents):
            self.gate.prepare_reference(p['referenceKey'], content)
        self.prepared = cast
        return {'prepared': [p['id'] for p in cast]}

    def admission(self, ids, lead):
        current, _ = self.resolve(ids, lead)
        if current != self.prepared:
            raise ValueError('The cast changed. Turn video on again to prepare its latest references.')
        return [{**p, 'url': self.gate.references[p['referenceKey']]} for p in current]
