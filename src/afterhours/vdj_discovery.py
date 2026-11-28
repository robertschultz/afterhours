"""Cooperative metadata-only crate discovery. Never loads, seeks or plays audio."""
import copy
import re
import time
from afterhours.dj_bridge import BridgeError
from afterhours.dj_discovery import credit, normalized
from afterhours.track_metadata import clean_text
from afterhours.vdj_catalog import folder_state, selection, search_literal


class CatalogSearch:
    def __init__(self, session, terms, artist, genre, bpm_min, bpm_max, clock=time.monotonic, recheck_missing=False,
                 external_request_id=None,authorize=None,finish=None, *, opening_query=None):
        self.session=session;self.terms=terms;self.clock=clock;self.folder=None
        self.external_request_id=external_request_id;self.authorize=authorize;self.external_finish=finish
        self.recheck_missing=recheck_missing
        self.artist=[v.strip().casefold() for v in artist.split(';') if v.strip()]
        self.rolling_artist = frozenset()
        self.rolling_title = ''
        self.genre=re.sub(r'[^a-z0-9]','',genre.casefold())
        self.bpm_min=bpm_min;self.bpm_max=bpm_max
        self.filters={'artist':artist,'genre':genre,'bpmMin':bpm_min,'bpmMax':bpm_max}
        self.examined=0
        self.term_index=0;self.position=0;self.added=0;self.per_term=0
        self.matched=0;self.seen=set()
        self.phase='begin';self.ready_at=0;self.deadline=None;self.done=False
        self.last_id=None;self.repeated=0
        self.latest=None
        # A set opening can reuse its first matching records immediately.
        # An explicit import or expansion must dig beyond known archive rows.
        opening=not session.snapshot().get('order') and not recheck_missing
        self.reuse_matches=opening
        self.opening_query=opening_query if opening else None
        self.target=3 if opening else 6
        self.max_rows=12 if opening else 30
        self.seconds=25 if opening else 45
        self.quota=max(2,self.target//len(terms))

    def set_rolling_seed(self, artist, title):
        """Keep native text hits tied to the artist or recording that seeded the search."""
        self.rolling_artist = credit(artist)
        self.rolling_title = normalized(title)

    def publish(self,status,message):
        self.session.update(catalogSearch={'status':status,'query':'; '.join(self.terms),
            **({'externalRequestId':self.external_request_id} if self.external_request_id else {}),
            'term':self.terms[min(self.term_index,len(self.terms)-1)],'filters':copy.deepcopy(self.filters),
            'examined':self.examined,'added':self.added,'matched':self.matched,'latest':self.latest,
            'foundIds':sorted(self.seen)[:32],'message':message,'updated':time.time()})
        if self.external_finish and status in ('complete','error'):
            self.external_finish('confirmed' if status=='complete' else 'blocked',
                'The engine recorded the catalog search outcome.',
                'engine-confirmed' if status=='complete' else 'discovery-failed')

    def finish(self,message=None):
        self.done=True
        self.publish('complete',message or f'{self.added} new recordings added to the crate.')
        self.session.event(message or f'Crate search finished: {self.added} new recordings. Existing queue kept.','catalog')

    def resume_search(self):
        # Preloading may use a different browser search on the same engine.
        # Re-enter this term before reading more rows; retain every found track.
        if not self.done:self.phase='begin';self.position=0;self.last_id=None;self.repeated=0

    def step(self,client):
        """One bounded browser operation; the music worker chooses a safe gap."""
        s=self.session;now=self.clock();s.check()
        if self.done:return
        if self.authorize:self.authorize()
        if self.deadline is None:self.deadline=now+self.seconds
        if now>=self.deadline:self.finish('Search window ended. Keeping the recordings already found.');return
        if self.phase=='settle' and now<self.ready_at:return
        current_folder=folder_state(client)
        if self.folder is None:
            self.folder=current_folder
            # Explicit Catalog search adopts the source the operator selected.
            # Automatic set expansion keeps its existing source binding. Every
            # later load still revalidates the exact opaque recording identity.
            if s.folder and s.folder['id']!=self.folder['id'] and s.snapshot()['order'] and not self.recheck_missing:
                raise BridgeError('Catalog folder changed. The playing queue was kept.')
            s.folder=self.folder
        elif current_folder['id']!=self.folder['id']:
            raise BridgeError('Catalog folder changed during discovery. The queue was kept.')
        term=self.terms[self.term_index]
        if self.phase=='begin':
            self.latest=None
            s.send(client,'search '+search_literal(term));self.ready_at=now+1.5;self.phase='settle'
            self.publish('searching',f'Looking through {term}. Keeping the current queue.');return
        if self.phase=='settle':
            s.send(client,"browser_window 'songs' & browser_scroll 'top'");self.phase='read';return
        try:selected=selection(client,True)
        except BridgeError:selected=None
        self.examined+=1
        if selected:
            self.repeated=self.repeated+1 if selected['id']==self.last_id else 0;self.last_id=selected['id']
            track=selected['metadata'];bpm=track.get('bpm')
            self.latest={key:str(track.get(key) or '')[:120] for key in ('title','artist')}
            if self.recheck_missing:self.session.availability.found_again(track['id'])
            matches=bool(clean_text(track.get('title'))) and (
                not self.artist or any(a in selected['artist'].casefold() for a in self.artist))
            matches = matches and (not self.rolling_artist or bool(self.rolling_artist & credit(selected['artist'])))
            matches = matches and (not self.rolling_title or self.rolling_title == normalized(track.get('title')))
            matches=matches and (not self.genre or self.genre in re.sub(r'[^a-z0-9]','',str(track.get('genre') or '').casefold()))
            matches=matches and type(bpm) in (int,float) and self.bpm_min<=bpm<=self.bpm_max
            matches=matches and not s.availability.blocked(track['id'])
            if matches and track['id'] not in self.seen:
                # Every considered recording survives in the archive even when
                # its bounded planning pool already has 48 entries.
                _,inserted=s.remember_record(track,source='discovery',query=term,position=self.position)
                self.seen.add(track['id']);self.matched+=1
                self.per_term+=int(inserted or self.reuse_matches)
                self.added+=int(inserted)
                if self.opening_query:
                    from afterhours.queue_planning import quick_opening
                    if quick_opening(s.snapshot(), self.opening_query, self.seen):
                        self.finish('Live catalog found an opening and a feasible next slot.')
                        return
        self.publish('searching',f'{self.added} new recordings found. Existing queue kept.')
        self.position+=1
        progress=self.matched if self.reuse_matches else self.added
        if self.repeated>=2 or self.position>=self.max_rows or self.per_term>=self.quota or progress>=self.target:
            self.term_index+=1;self.position=0;self.per_term=0;self.repeated=0;self.last_id=None
            if self.term_index>=len(self.terms) or progress>=self.target:self.finish();return
            self.phase='begin'
        else:s.send(client,'browser_scroll +1')


class CrateDig:
    """Walk the catalog on its own and keep everything it is shown.

    The digger searches for names the crate already holds (artists first, then
    labels and genres), reads each result row and stores the recording in the
    persistent crate. New recordings bring new names, so it keeps moving
    outward. It is metadata only: it never loads, seeks or plays audio, never
    changes the working set or the queue, and runs one browser step at a time
    in the gaps the playing set leaves it.
    """
    MAX_ROWS = 100       # rows read for one name
    SETTLE_SECONDS = 1.5
    PUBLISH_EVERY = 10
    MAX_CRATE = 20000    # recordings; the digger stops adding to a crate this large
    EMPTY_READS = 8      # unanswered reads in a row before a name is given up; results can take a moment to load

    def __init__(self, session, clock=time.monotonic):
        self.session=session;self.clock=clock
        self.terms=[];self.term=None;self.phase='begin';self.ready_at=0
        self.position=0;self.term_examined=0;self.term_added=0
        self.examined=0;self.added=0;self.terms_done=0
        self.last_id=None;self.repeated=0;self.empty=0;self.term_seen=0;self.done=False

    def publish(self,status,message=''):
        self.session.update(crateDig={'status':status,'term':self.term,'examined':self.examined,'added':self.added,
            'termsDone':self.terms_done,'crateCount':self.session.crate.summary()['count'],
            'message':message,'updated':time.time()})

    def stop(self,status,message):
        self.done=True;self.publish(status,message)

    def resume_search(self):
        """Another job used the browser. Search this name again before reading on."""
        if not self.done and self.term:
            self.phase='begin';self.position=0;self.last_id=None;self.repeated=0

    def next_term(self):
        if not self.terms:
            self.terms=self.session.crate.dig_candidates(limit=8)
        self.term=self.terms.pop(0) if self.terms else None
        self.phase='begin';self.position=0;self.term_examined=0;self.term_added=0
        self.last_id=None;self.repeated=0;self.empty=0;self.term_seen=0
        return self.term

    def finish_term(self):
        # A name that showed no rows at all is recorded as such, so it is tried again soon.
        self.session.crate.mark_dug(self.term,self.term_seen,self.term_added)
        self.terms_done+=1
        self.publish('digging',f'{self.added} recordings added to the crate.')
        self.term=None

    def fault(self,message,passing=False):
        """End the dig after a fault and set its name aside, so it is not searched again at once.

        A passing fault (the bridge timed out) sets the name aside for an hour,
        like a name that showed nothing; any other fault sets it aside as dug.
        """
        try:
            if self.term:self.session.crate.mark_dug(self.term,0 if passing else max(1,self.term_seen),self.term_added)
        except Exception:pass
        self.done=True
        try:self.publish('paused',message)
        except Exception:pass

    def step(self,client):
        """One bounded browser operation. Whatever goes wrong ends the dig; it never reaches the set."""
        try:self._step(client)
        except (BridgeError,OSError) as exc:self.fault(str(exc)[:160],passing=True)
        except Exception:
            # Storage faults and malformed rows included. Say what happened in
            # plain words; never the interpreter's text.
            self.fault('Digging stopped after a fault. The crate is kept.')

    def _step(self,client):
        s=self.session
        if self.done:return
        if not s.folder:return self.stop('idle','Digging waits for a selected catalog.')
        count=s.crate.summary()['count']
        if count>=self.MAX_CRATE:
            return self.stop('idle',f'The crate is full at {count} recordings. Digging stopped.')
        if self.term is None and not self.next_term():
            return self.stop('idle','Nothing new to follow yet.')
        if folder_state(client)['id']!=s.folder['id']:
            return self.stop('paused','The catalog folder changed. Digging stopped; the crate is kept.')
        now=self.clock()
        if self.phase=='begin':
            s.send(client,'search '+search_literal(self.term));self.ready_at=now+self.SETTLE_SECONDS;self.phase='settle'
            self.publish('digging',f'Digging through {self.term}.');return
        if self.phase=='settle':
            if now<self.ready_at:return
            s.send(client,"browser_window 'songs' & browser_scroll 'top'");self.phase='read';return
        try:selected=selection(client,True)
        except BridgeError:selected=None
        self.examined+=1;self.term_examined+=1
        if selected:
            self.empty=0;self.term_seen+=1
            self.repeated=self.repeated+1 if selected['id']==self.last_id else 0;self.last_id=selected['id']
            track=selected['metadata']
            if not self.repeated and not s.availability.blocked(track['id']):
                # One row the crate refuses is skipped; the name carries on.
                try:_,inserted=s.crate.upsert(track,source='dig',query=self.term,position=self.position)
                except ValueError:inserted=False
                self.added+=int(inserted);self.term_added+=int(inserted)
            self.position+=1
        else:
            # Nothing is selected yet: the results may still be loading, or
            # the list starts unselected. Step on and look again.
            self.empty+=1
        if self.empty>=self.EMPTY_READS or self.repeated>=2 or self.position>=self.MAX_ROWS:
            self.finish_term();return
        if self.examined%self.PUBLISH_EVERY==0:
            self.publish('digging',f'{self.added} recordings added to the crate.')
        s.send(client,'browser_scroll +1')
