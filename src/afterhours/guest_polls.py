"""Durable exact-choice polls, isolated from music and network operations.

The caller must validate every complete queue variant before opening a poll.
This ledger can admit votes and claim a deadline once, but cannot select a new
slot, spend planner allowance, search, prepare, load or play any recording.
"""
import copy
import hashlib
import json
import math
import re
import secrets

from afterhours.guest_requests import GuestError,command_id,encoded,exact_id,track_ref

WINDOW_SECONDS=60
MAX_POLLS=64
MAX_RECEIPTS=128
STATUSES=('open','applied','kept','cancelled','interrupted')
REASONS={
    'applied':'The crowd choice is in the upcoming queue.',
    'kept':'The DJ’s original record stays in this slot.',
    'changed':'The set moved on before this choice could be confirmed.',
    'unavailable':'These recordings are no longer available for this slot.',
    'unconfirmed':'This queue change could not be confirmed. The vote will not be replayed.',
    'night-ended':'This night has ended.',
    'disabled':'Voting is closed for this room.',
    'restart':'This vote ended when the DJ service restarted.',
}


def timestamp(value):
    if type(value) not in (int,float) or not math.isfinite(value) or not 0<=value<=1e12:
        raise ValueError('Invalid poll clock.')
    return value


def identity(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,128}',value):
        raise GuestError('invalid-request','Use a valid vote identity.')
    return value


def guarded_offer(night,tracks,base_order,slot,binding):
    """Validate the bounded private handoff shape, not live audio suitability."""
    if not isinstance(tracks,list) or not 2<=len(tracks)<=3:
        raise ValueError('A poll needs two or three exact recordings.')
    tracks=[track_ref(t) for t in tracks]
    ids=[t['id'] for t in tracks]
    if len(set(ids))!=len(ids):raise ValueError('Poll recordings must be distinct.')
    if (not isinstance(base_order,list) or not 2<=len(base_order)<=12
            or any(not exact_id(tid) for tid in base_order) or len(set(base_order))!=len(base_order)
            or type(slot) is not int or not 0<=slot<len(base_order)):
        raise ValueError('A poll needs an exact bounded queue slot.')
    if base_order[slot] not in ids or any(tid in base_order[:slot]+base_order[slot+1:] for tid in ids):
        raise ValueError('A poll option cannot duplicate another queue position.')
    if (not isinstance(binding,dict) or set(binding)!={'sessionId','agentRevision','agentGeneration','contextHash'}
            or binding['sessionId']!=night
            or any(type(binding[k]) is not int or not 0<=binding[k]<=2**53-1 for k in ('agentRevision','agentGeneration'))
            or not isinstance(binding['contextHash'],str) or not re.fullmatch(r'[a-f0-9]{64}',binding['contextHash'])):
        raise ValueError('The poll is not bound to an accepted engine context.')
    return tracks


class GuestPolls:
    """Shares the guest ledger's transaction/lock and night revision sequence."""
    def __init__(self,ledger):
        self.ledger=ledger;self.db=ledger.db;self.lock=ledger.lock;self.clock=ledger.clock
        with self.lock:
            self.db.executescript('''
              CREATE TABLE IF NOT EXISTS guest_polls(
                id TEXT PRIMARY KEY,night TEXT NOT NULL REFERENCES nights(id),offer_id TEXT NOT NULL,
                revision INTEGER NOT NULL,status TEXT NOT NULL,opened_at REAL NOT NULL,closes_at REAL NOT NULL,
                tracks TEXT NOT NULL,base_order TEXT NOT NULL,slot INTEGER NOT NULL,binding TEXT NOT NULL,
                incumbent TEXT NOT NULL,choice TEXT,claim TEXT,reason TEXT,UNIQUE(night,offer_id));
              CREATE TABLE IF NOT EXISTS guest_votes(
                poll TEXT NOT NULL REFERENCES guest_polls(id),guest TEXT NOT NULL REFERENCES guests(id),
                track_id TEXT NOT NULL,PRIMARY KEY(poll,guest));
              CREATE TABLE IF NOT EXISTS guest_vote_receipts(
                guest TEXT NOT NULL REFERENCES guests(id),command TEXT NOT NULL,payload_hash TEXT NOT NULL,
                poll TEXT NOT NULL REFERENCES guest_polls(id),receipt TEXT NOT NULL,PRIMARY KEY(guest,command));
              CREATE INDEX IF NOT EXISTS guest_poll_night ON guest_polls(night,opened_at);
            ''')
            # An in-flight queue mutation cannot be reconstructed from a crash.
            # Even an open poll is ended, never silently rearmed after startup.
            with self.db:
                nights=self.db.execute("SELECT DISTINCT night FROM guest_polls WHERE status IN ('open','closing')").fetchall()
                for row in nights:
                    revision=self.ledger._bump(row[0])
                    self.db.execute("UPDATE guest_polls SET status='interrupted',revision=?,choice=NULL,claim=NULL,reason='restart' WHERE night=? AND status IN ('open','closing')",(revision,row[0]))

    def open(self,night,offer_id,tracks,base_order,slot,binding):
        """Caller already authorized and validated every option. No live IO."""
        identity(offer_id);tracks=guarded_offer(night,tracks,base_order,slot,binding)
        with self.lock,self.db:
            self.ledger._night_open(night)
            previous=self.db.execute('SELECT * FROM guest_polls WHERE night=? AND offer_id=?',(night,offer_id)).fetchone()
            if previous:
                if (previous['tracks']!=encoded(tracks) or previous['base_order']!=encoded(base_order)
                        or previous['slot']!=slot or previous['binding']!=encoded(binding)):
                    raise ValueError('An accepted poll offer cannot change its options or context.')
                return self._view(previous)
            if self.db.execute("SELECT 1 FROM guest_polls WHERE night=? AND status IN ('open','closing')",(night,)).fetchone():
                raise ValueError('A crowd choice is already open.')
            if self.db.execute('SELECT count(*) FROM guest_polls WHERE night=?',(night,)).fetchone()[0]>=MAX_POLLS:
                raise GuestError('capacity','This night has reached its voting limit.',503)
            now=timestamp(self.clock());identifier='poll-'+secrets.token_hex(16);revision=self.ledger._bump(night)
            self.db.execute('INSERT INTO guest_polls(id,night,offer_id,revision,status,opened_at,closes_at,tracks,base_order,slot,binding,incumbent) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (identifier,night,offer_id,revision,'open',now,now+WINDOW_SECONDS,encoded(tracks),encoded(base_order),slot,encoded(binding),base_order[slot]))
            return self._view(self._row(night,identifier))

    def _row(self,night,identifier):
        row=self.db.execute('SELECT * FROM guest_polls WHERE night=? AND id=?',(night,identifier)).fetchone()
        if not row:raise GuestError('poll-not-found','This vote is no longer available.',404)
        return row

    def _view(self,row,guest=None):
        counts={r['track_id']:r['count'] for r in self.db.execute('SELECT track_id,count(*) AS count FROM guest_votes WHERE poll=? GROUP BY track_id',(row['id'],)).fetchall()}
        options=[{'track':t,'votes':counts.get(t['id'],0)} for t in json.loads(row['tracks'])]
        result={'id':row['id'],'revision':row['revision'],'status':'open' if row['status']=='closing' else row['status'],
                'openedAt':row['opened_at'],'closesAt':row['closes_at'],'options':options,
                'totalVotes':sum(o['votes'] for o in options)}
        if guest:
            choice=self.db.execute('SELECT track_id FROM guest_votes WHERE poll=? AND guest=?',(row['id'],guest)).fetchone()
            if choice:result['myChoiceId']=choice[0]
        if row['status'] in ('applied','kept'):result['chosenTrackId']=row['choice']
        if row['reason'] in REASONS:result['reason']=REASONS[row['reason']]
        return result

    def public(self,night,guest,identifier=None):
        with self.lock:
            self.ledger._guest(night,guest)
            row=(self._row(night,identity(identifier)) if identifier is not None else
                 self.db.execute('SELECT * FROM guest_polls WHERE night=? ORDER BY opened_at DESC,revision DESC,id LIMIT 1',(night,)).fetchone())
            return self._view(row,guest) if row else None

    def pending(self,night):
        """Private bounded open offer for the engine recheck, never a guest DTO."""
        with self.lock:
            row=self.db.execute("SELECT * FROM guest_polls WHERE night=? AND status='open' ORDER BY opened_at,id LIMIT 1",(night,)).fetchone()
            if not row:return None
            return {'id':row['id'],'nightId':night,'offerId':row['offer_id'],'tracks':json.loads(row['tracks']),
                    'baseOrder':json.loads(row['base_order']),'slot':row['slot'],'binding':json.loads(row['binding'])}

    def receipt(self,night,guest,command,scope,payload=None):
        command_id(command)
        with self.lock:
            self.ledger._guest(night,guest)
            row=self.db.execute('SELECT * FROM guest_vote_receipts WHERE guest=? AND command=?',(guest,command)).fetchone()
            if not row:return None
            if payload is not None and hashlib.sha256(encoded(payload).encode()).hexdigest()!=row['payload_hash']:
                raise GuestError('command-conflict','This vote identity was already used for a different choice.',409)
            receipt=json.loads(row['receipt']);receipt['scope']=copy.deepcopy(scope)
            return {'receipt':receipt,'poll':self._view(self._row(night,row['poll']),guest)}

    def vote(self,night,guest,payload,scope):
        if not isinstance(payload,dict) or set(payload)!={'requestId','pollId','trackId'} or not exact_id(payload.get('trackId')):
            raise GuestError('invalid-request','Choose one exact option from this vote.')
        command_id(payload['requestId']);identity(payload['pollId'])
        with self.lock,self.db:
            previous=self.receipt(night,guest,payload['requestId'],scope,payload)
            if previous:return previous
            row=self._row(night,payload['pollId']);now=timestamp(self.clock())
            if row['status']!='open' or now>=row['closes_at']:
                raise GuestError('poll-closed','This vote has closed.',409)
            if payload['trackId'] not in [t['id'] for t in json.loads(row['tracks'])]:
                raise GuestError('option-unavailable','Choose one of this vote’s recordings.',409)
            if self.db.execute('SELECT count(*) FROM guest_vote_receipts WHERE guest=?',(guest,)).fetchone()[0]>=MAX_RECEIPTS:
                raise GuestError('capacity','This guest session has reached its voting limit.',503)
            old=self.db.execute('SELECT track_id FROM guest_votes WHERE poll=? AND guest=?',(row['id'],guest)).fetchone()
            duplicate=bool(old and old[0]==payload['trackId'])
            self.db.execute('INSERT INTO guest_votes VALUES(?,?,?) ON CONFLICT(poll,guest) DO UPDATE SET track_id=excluded.track_id',
                (row['id'],guest,payload['trackId']))
            revision=self.ledger._bump(night)
            self.db.execute('UPDATE guest_polls SET revision=? WHERE id=?',(revision,row['id']))
            receipt={'id':payload['requestId'],'status':'accepted','change':{'pollId':row['id'],'trackId':payload['trackId']},
                     'revision':revision,'pollRevision':revision,'duplicate':duplicate}
            self.db.execute('INSERT INTO guest_vote_receipts VALUES(?,?,?,?,?)',
                (guest,payload['requestId'],hashlib.sha256(encoded(payload).encode()).hexdigest(),row['id'],encoded(receipt)))
            return self.receipt(night,guest,payload['requestId'],scope)

    def claim_due(self,night):
        """Reserve one deadline once. The returned claim alone grants no audio IO."""
        with self.lock,self.db:
            self.ledger._night_open(night);now=timestamp(self.clock())
            row=self.db.execute("SELECT * FROM guest_polls WHERE night=? AND status='open' AND closes_at<=? ORDER BY opened_at,id LIMIT 1",(night,now)).fetchone()
            if not row:return None
            view=self._view(row);counts={o['track']['id']:o['votes'] for o in view['options']};highest=max(counts.values())
            winners=[tid for tid,count in counts.items() if count==highest]
            choice=winners[0] if highest>0 and len(winners)==1 else row['incumbent']
            claim='claim-'+secrets.token_hex(16);revision=self.ledger._bump(night)
            self.db.execute("UPDATE guest_polls SET status='closing',choice=?,claim=?,revision=? WHERE id=?",(choice,claim,revision,row['id']))
            return {'id':row['id'],'claimId':claim,'nightId':night,'offerId':row['offer_id'],
                    'choiceId':choice,'incumbentId':row['incumbent'],'baseOrder':json.loads(row['base_order']),
                    'slot':row['slot'],'binding':json.loads(row['binding']),'tracks':json.loads(row['tracks'])}

    def finish(self,claim,status,reason=None):
        if status not in ('applied','kept','cancelled') or reason not in (*REASONS,None):
            raise ValueError('Invalid final poll outcome.')
        if status!='cancelled' and reason not in (None,status):raise ValueError('The outcome reason must match its confirmed result.')
        with self.lock,self.db:
            row=self._row(claim['nightId'],claim['id'])
            if row['status']!='closing' or not isinstance(claim.get('claimId'),str) or not secrets.compare_digest(row['claim'] or '',claim['claimId']):
                return False
            if status=='applied' and row['choice']==row['incumbent'] or status=='kept' and row['choice']!=row['incumbent']:
                raise ValueError('The final poll outcome does not match its reserved choice.')
            revision=self.ledger._bump(row['night'])
            self.db.execute('UPDATE guest_polls SET status=?,revision=?,claim=NULL,choice=?,reason=? WHERE id=?',
                (status,revision,None if status=='cancelled' else row['choice'],reason or (status if status!='cancelled' else 'unconfirmed'),row['id']))
            return True

    def cancel_open(self,night,reason='changed'):
        if reason not in REASONS:raise ValueError('Invalid public poll reason.')
        with self.lock,self.db:
            rows=self.db.execute("SELECT id FROM guest_polls WHERE night=? AND status='open'",(night,)).fetchall()
            if not rows:return False
            revision=self.ledger._bump(night)
            self.db.execute("UPDATE guest_polls SET status='cancelled',revision=?,choice=NULL,reason=? WHERE night=? AND status='open'",(revision,reason,night))
            return True
