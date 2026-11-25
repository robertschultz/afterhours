"""Bounded poll event/deadline worker; engine callbacks own actual guards.

This is an optional collaborator of the existing slow guest worker, not another
audio loop. SQL is never executed while the caller publishes an engine offer.
The adopter must recheck its context and mutate under the engine's own locks.
"""
from collections import deque
import copy
import threading

from afterhours.guest_polls import guarded_offer,identity


class PollCoordinator:
    MAX_EVENTS=16
    def __init__(self,polls,night,enabled,verify_offer=None,adopt=None,scope_generation=None):
        self.polls=polls;self.night=night;self.enabled=enabled
        self.verify_offer=verify_offer;self.adopt=adopt
        # A memory-only scope read avoids acquiring the HTTP/listener lock
        # from inside an engine guard. A rapid close/reopen cannot reuse an
        # old poll even if the slow worker missed the disabled interval.
        self.scope_generation=scope_generation or (lambda:None)
        self.lock=threading.Lock();self.work_lock=threading.Lock();self.events=deque(maxlen=self.MAX_EVENTS)
        self.previous_night=None;self.finalizing=None;self.error=None

    @property
    def supported(self):return callable(self.verify_offer) and callable(self.adopt)

    def submit(self,night,offer_id,tracks,base_order,slot,binding):
        if not self.supported:return False
        identity(offer_id);tracks=guarded_offer(night,tracks,base_order,slot,binding)
        event=copy.deepcopy({'nightId':night,'offerId':offer_id,'tracks':tracks,'baseOrder':base_order,'slot':slot,'binding':binding})
        # Called under an agent lock: only a bounded memory enqueue, no DB,
        # callback or potentially reverse-order engine/session lock here.
        with self.lock:
            if len(self.events)>=self.MAX_EVENTS:return False
            self.events.append(event)
        return True

    def _finish(self):
        if self.finalizing is None:return
        claim,status,reason=self.finalizing
        self.polls.finish(claim,status,reason)
        self.finalizing=None

    def step(self):
        if not self.work_lock.acquire(blocking=False):return
        try:
            self._finish() # A persistence retry never invokes the adopter again.
            night=self.night()
            if self.previous_night is not None and self.previous_night!=night:
                self.polls.cancel_open(self.previous_night,'night-ended')
            self.previous_night=night
            with self.lock:events=list(self.events);self.events.clear()
            if not self.supported or not self.enabled():
                self.polls.cancel_open(night,'disabled');self.error=None;return
            for event in events:
                if event['nightId']!=night or not self.verify_offer(copy.deepcopy(event)):continue
                if not self.enabled() or self.night()!=night:break
                try:self.polls.open(night,event['offerId'],event['tracks'],event['baseOrder'],event['slot'],event['binding'])
                except ValueError:
                    # A second compatible future offer must not replace the
                    # visible stable choice or restart its existing deadline.
                    if self.polls.pending(night) is None:raise
            pending=self.polls.pending(night)
            if pending and not self.verify_offer(copy.deepcopy(pending)):
                self.polls.cancel_open(night,'changed');self.error=None;return
            claim=self.polls.claim_due(night)
            if claim:
                status='cancelled';reason='unconfirmed'
                try:
                    # The callback performs its own final atomic engine guards;
                    # the prechecks here are not authority to bypass them.
                    if self.enabled() and self.night()==night:
                        receipt=self.adopt(copy.deepcopy(claim))
                        order=list(claim['baseOrder']);order[claim['slot']]=claim['choiceId']
                        wanted='kept' if claim['choiceId']==claim['incumbentId'] else 'applied'
                        if (isinstance(receipt,dict) and set(receipt)=={'status','sessionId','trackId','orderIds'}
                                and receipt['status']==wanted and receipt['sessionId']==night
                                and receipt['trackId']==claim['choiceId'] and receipt['orderIds']==order):
                            status=wanted;reason=None
                    else:reason='changed'
                except Exception:
                    # The single attempt may have reached the engine. Retain
                    # its closure claim and persist uncertainty; never retry it.
                    status='cancelled';reason='unconfirmed'
                self.finalizing=(claim,status,reason)
                self._finish()
            self.error=None
        except Exception:
            # Optional voting failure cannot stop the music worker. No private
            # exception/provider text is exposed. The durable claim prevents a
            # second adoption if final persistence remains unavailable.
            self.error='Voting is temporarily unavailable.'
        finally:self.work_lock.release()
