"""Two isolated preparation decks, serviced by the sole playback OSC owner.

Only one mailbox is serviced per slice. Loading owns catalog navigation until
its exact recording is confirmed; afterward C/D share slices, weighted toward
the earliest queued recording. No audio output or model request is created.
"""
import copy
from afterhours.background_analysis import BackgroundAnalysis,guard_deck
from afterhours.dj_bridge import BridgeError,as_bool
from afterhours import cue_planner


class PreparationPool:
    def __init__(self,session,revision,tracks,cache=None):
        self.session=session;self.revision=revision;self.tracks=copy.deepcopy(tracks)
        self.cache=copy.deepcopy(cache or {});self.result=[];self.jobs=[];self.pending=[]
        self.done=False;self.error=None;self.analysis_timed_out=False;self.suspended=True;self.slice=0;self.closed=False
        self.catalog_miss=None
        self.analysis_miss=None
        self.osc_reply_timeout=False
        state = session.snapshot()
        self.timed = bool(state.get('entryCadence'))
        for track in self.tracks:
            try:session.availability.require_track(track)
            except BridgeError as exc:
                self.error=str(exc);self.done=True;self.result=[];self.pending=[];return
            cached=self.cache.get(track['id'])
            if (cached and cached.get('cueCandidates') and cached.get('preparationSeconds')==state['secondsPerTrack']
                    and bool(cached.get('timedPreparation')) == self.timed):
                try:cue_planner.measured_rows(cached);self.result.append(cached);continue
                except (ValueError,OSError):pass
            self.pending.append(track)

    def decks(self):
        choose=getattr(self.session,'preparation_decks',None)
        return choose() if choose else (3,4)

    def release(self,client,deck):
        """The set needs this deck now. End its preparation; its record goes back to the front of the line."""
        for job in self.jobs:
            if job.deck!=deck or getattr(job,'pool_closed',False):continue
            unfinished=not job.done
            job.cancelled.set();self.cache.update(job.cache)
            job.close(client);job.pool_closed=True
            if unfinished:
                job.done=True;self.pending[:0]=copy.deepcopy(job.tracks)
            elif job.error:self.remember_failure(job)
            else:self.result.extend(job.result)

    def invalidate_catalog(self):
        for job in self.jobs:
            if not job.done:job.invalidate_catalog()

    def remember_failure(self,job):
        self.error=job.error
        self.analysis_timed_out=job.analysis_timed_out
        self.catalog_miss=getattr(job,'catalog_miss',None)
        self.analysis_miss=getattr(job,'analysis_miss',None)
        self.osc_reply_timeout=getattr(job,'osc_reply_timeout',False)
        self.done=True

    def step(self,client,seconds_available):
        self.suspended=seconds_available<12
        if self.suspended or self.done or self.closed:return
        # A deck that joined the playing pair is no longer a spare.
        pair=getattr(self.session,'pair_decks',None) or ()
        for job in self.jobs:
            if not job.done and job.deck in pair and len(getattr(self.session,'decks',()))>2:self.release(client,job.deck)
        try:
            for track in self.tracks:self.session.availability.require_track(track)
        except BridgeError as exc:
            self.error=str(exc);self.done=True;return
        for job in self.jobs:
            if job.done and not getattr(job,'pool_closed',False):
                self.cache.update(job.cache);job.close(client);job.pool_closed=True
                if job.error:self.remember_failure(job);return
                self.result.extend(job.result)
        active=[j for j in self.jobs if not j.done]
        # A loading child owns the shared catalog. Its measured scan can share
        # the connection once Load has an exact identity readback.
        loading=next((j for j in active if j.loaded is None),None)
        if self.pending and len(active)<2 and loading is None:
            occupied={j.deck for j in active}
            for deck in self.decks():
                if deck in occupied:continue
                guard_deck(client,deck)
                if (as_bool(client.query(f'deck {deck} loaded','bool')) or
                        as_bool(client.query(f'deck {deck} play','bool')) or
                        as_bool(client.query(f'deck {deck} effect_active 1','bool'))):continue
                job=BackgroundAnalysis(self.session,self.revision,[self.pending.pop(0)],self.cache,deck=deck)
                self.jobs.append(job);active.append(job);loading=job;break
            if not active:
                self.error='Both preparation decks are in use. Current playback keeps priority.';self.done=True;return
        if not active:
            self.done=not self.pending
            ids={t['id']:i for i,t in enumerate(self.tracks)}
            self.result.sort(key=lambda t:ids[t['id']]);return
        # Prioritize the immediate next item, while letting the later decoder
        # and analysis progress every third slice.
        if loading and (not self.timed or loading is active[0]):
            job = loading
        else:
            job = active[0 if len(active)==1 or self.slice%3 else 1]
        self.slice+=1;job.step(client,seconds_available)

    def close(self,client):
        self.closed=True;errors=[]
        for job in self.jobs:job.cancelled.set()
        for job in self.jobs:
            self.cache.update(job.cache)
            try:job.close(client)
            except (BridgeError,OSError,ValueError) as exc:errors.append(f'Deck {job.deck}: {exc}')
        if errors:raise BridgeError('; '.join(errors))

    def cleanup_descriptors(self):
        return [row for job in self.jobs for row in job.cleanup_descriptors()]

    def deferred_cleanup_owner(self):
        """Retain typed local proof only for the failed spare owned by this run."""
        for job in self.jobs:
            if (job.deck == 4 and job.owned and getattr(job, 'cleanup_reply_timeout', False) is True
                    and job.cleanup_state.get('unloadRequested')
                    and not job.cleanup_state.get('restoreRequested')
                    and not job.cleanup_state.get('loadPending')):
                return job.owner_id
        return None

    def snapshot(self):
        active=[j for j in self.jobs if not j.done or j.owned]
        status='blocked' if self.error else 'complete' if self.done else 'analyzing' if active else 'queued'
        message=self.error or ('Upcoming recordings verified.' if self.done else
            ' · '.join(f"Deck {'ABCD'[j.deck-1]}: {j.message}" for j in active) or 'Preparing the upcoming queue.')
        return {'revision':self.revision,'status':status,'message':message,
                'completed':len(self.result),'total':len(self.tracks),'deck':active[0].deck if active else None,
                'decks':[{'deck':j.deck,'trackId':j.tracks[0]['id'],'title':j.tracks[0].get('title'),
                          'artist':j.tracks[0].get('artist'),'bpm':j.tracks[0].get('bpm'),
                          'status':j.status,'loaded':j.loaded is not None,'owned':j.owned} for j in active],
                'operations':sum(j.serviced for j in self.jobs),'suspendedForMix':self.suspended,'audioSaved':False}
