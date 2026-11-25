"""Durable anonymous exact-recording requests. No transport or provider access.

All admissions and receipts commit in one SQLite transaction. Credentials are
hashed; invitation and selection bindings live in the separate HTTP boundary.
"""
import copy
import hashlib
import json
import math
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

OPEN=('sent','listening','coming')
STAGES=(*OPEN,'declined','playing')
FOLLOW={'status':'configuration-required','foregroundStatus':True}
CAPABILITIES={'search':True,'ask':True,'status':True,'poll':False,'liveActivity':False}
REASONS={'unavailable':'This recording is not available for the set.',
         'not-tonight':'This recording does not fit tonight’s set.',
         'night-ended':'This night has ended.',
         'waiting':'The DJ is keeping this request in mind.',
         'preparing':'The DJ is checking this recording.'}


class GuestError(ValueError):
    def __init__(self,code,message,status=400,retry_after=None):
        super().__init__(message);self.code=code;self.status=status;self.retry_after=retry_after


def command_id(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,80}',value):
        raise GuestError('invalid-request','Use a valid request identity.')
    return value


def exact_id(value):
    return isinstance(value,str) and re.fullmatch(r'virtualdj:[a-f0-9]{64}',value) is not None


def plain(value,limit):
    return ' '.join(str(value or '').split())[:limit]


def safe_art(value):
    if not isinstance(value,str) or not 1<=len(value)<=2048 or any(ord(c)<33 for c in value):return None
    try:
        url=urlsplit(value)
        if url.scheme not in ('http','https') or not url.hostname or url.username or url.password:return None
        if url.fragment:return None
        _=url.port
    except ValueError:return None
    return value


def track_ref(track):
    if not isinstance(track,dict) or not exact_id(track.get('id')):
        raise GuestError('recording-unavailable','This recording is not available for requests.',409)
    result={'id':track['id'],'title':plain(track.get('title'),240),'artist':plain(track.get('artist'),240) or 'Unknown artist'}
    if not result['title']:raise GuestError('recording-unavailable','This recording is not available for requests.',409)
    art=safe_art(track.get('artworkUrl'))
    if art:result['artworkUrl']=art
    return result


def encoded(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True,allow_nan=False)


class GuestLedger:
    def __init__(self,path=None,clock=time.time):
        self.clock=clock;self.lock=threading.RLock();self.path=Path(path) if path else None
        if self.path:
            self.path.parent.mkdir(parents=True,exist_ok=True)
            if self.path.is_symlink():raise ValueError('The guest ledger cannot be a symbolic link.')
        self.db=sqlite3.connect(str(self.path) if self.path else ':memory:',check_same_thread=False,timeout=3)
        try:self._initialize()
        except Exception:
            self.db.close()
            raise

    def _initialize(self):
        self.db.row_factory=sqlite3.Row
        if self.path:self.path.chmod(0o600)
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS nights(id TEXT PRIMARY KEY,revision INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL,closed REAL);
          CREATE TABLE IF NOT EXISTS guests(id TEXT PRIMARY KEY,night TEXT NOT NULL REFERENCES nights(id),token TEXT NOT NULL UNIQUE,expires REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS requests(id TEXT PRIMARY KEY,night TEXT NOT NULL REFERENCES nights(id),track_id TEXT NOT NULL,
            track TEXT NOT NULL,asks INTEGER NOT NULL,first_at REAL NOT NULL,stage TEXT NOT NULL,revision INTEGER NOT NULL,
            reason TEXT,played_at REAL,closed_at REAL,playing INTEGER NOT NULL DEFAULT 0,estimated_at REAL,round_id TEXT,
            UNIQUE(night,track_id));
          CREATE TABLE IF NOT EXISTS asks(guest TEXT NOT NULL REFERENCES guests(id),request TEXT NOT NULL REFERENCES requests(id),
            track_id TEXT NOT NULL,follow INTEGER NOT NULL,PRIMARY KEY(guest,track_id));
          CREATE TABLE IF NOT EXISTS receipts(guest TEXT NOT NULL REFERENCES guests(id),command TEXT NOT NULL,
            payload_hash TEXT NOT NULL,receipt TEXT NOT NULL,PRIMARY KEY(guest,command));
          CREATE INDEX IF NOT EXISTS request_night ON requests(night,first_at);
          CREATE INDEX IF NOT EXISTS guests_night ON guests(night);
        ''')
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO meta VALUES('room',?)",('room-'+secrets.token_hex(16),))
            # A process restart never pretends an old model invocation resumed.
            for night in self.db.execute("SELECT DISTINCT night FROM requests WHERE stage='listening'").fetchall():
                revision=self._bump(night[0])
                self.db.execute("UPDATE requests SET stage='sent',round_id=NULL,reason=NULL,revision=? WHERE night=? AND stage='listening'",(revision,night[0]))
        self.room_id=self.db.execute("SELECT value FROM meta WHERE key='room'").fetchone()[0]

    def close(self):
        with self.lock:self.db.close()

    def _bump(self,night):
        self.db.execute('UPDATE nights SET revision=revision+1 WHERE id=?',(night,))
        return self.db.execute('SELECT revision FROM nights WHERE id=?',(night,)).fetchone()[0]

    def ensure_night(self,night):
        if not isinstance(night,str) or not 1<=len(night)<=100:raise ValueError('Invalid night identity.')
        with self.lock,self.db:
            existing=self.db.execute('SELECT closed FROM nights WHERE id=?',(night,)).fetchone()
            if existing and existing['closed'] is not None:
                raise GuestError('night-closed','This night has ended.',409)
            for old in self.db.execute('SELECT id FROM nights WHERE id<>? AND closed IS NULL',(night,)).fetchall():
                self._close_night(old[0])
            self.db.execute('INSERT OR IGNORE INTO nights(id,created) VALUES(?,?)',(night,self.clock()))
            return self.revision(night)

    def _close_night(self,night):
        now=self.clock();revision=self._bump(night)
        self.db.execute('UPDATE nights SET closed=? WHERE id=?',(now,night))
        self.db.execute("UPDATE requests SET stage='declined',reason='night-ended',closed_at=?,playing=0,estimated_at=NULL,round_id=NULL,revision=? WHERE night=? AND stage IN ('sent','listening','coming')",(now,revision,night))
        self.db.execute('UPDATE requests SET playing=0,estimated_at=NULL WHERE night=?',(night,))

    def revision(self,night):
        with self.lock:
            row=self.db.execute('SELECT revision FROM nights WHERE id=?',(night,)).fetchone()
            return row[0] if row else 0

    def _night_open(self,night):
        row=self.db.execute('SELECT closed FROM nights WHERE id=?',(night,)).fetchone()
        if not row or row['closed'] is not None:raise GuestError('night-closed','This night has ended.',409)

    def create_guest(self,night):
        with self.lock,self.db:
            self._night_open(night)
            if self.db.execute('SELECT count(*) FROM guests WHERE night=?',(night,)).fetchone()[0]>=1024:
                raise GuestError('capacity','Requests are full for this night.',503)
            token=secrets.token_urlsafe(32);identifier='guest-'+secrets.token_hex(16);expires=self.clock()+86400
            self.db.execute('INSERT INTO guests VALUES(?,?,?,?)',(identifier,night,hashlib.sha256(token.encode()).hexdigest(),expires))
            return {'id':identifier,'expiresAt':expires},token

    def authenticate(self,night,token):
        if not isinstance(token,str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}',token):
            raise GuestError('guest-required','Open the room invitation to continue.',401)
        with self.lock:
            self._night_open(night)
            row=self.db.execute('SELECT id,expires FROM guests WHERE night=? AND token=?',(night,hashlib.sha256(token.encode()).hexdigest())).fetchone()
            if not row:raise GuestError('guest-required','Open the room invitation to continue.',401)
            if row['expires']<=self.clock():raise GuestError('guest-expired','This guest session has expired. Open the room invitation again.',401)
            return {'id':row['id'],'expiresAt':row['expires']}

    def _guest(self,night,guest):
        self._night_open(night)
        row=self.db.execute('SELECT expires FROM guests WHERE id=? AND night=?',(guest,night)).fetchone()
        if not row or row[0]<=self.clock():raise GuestError('guest-expired','This guest session has expired.',401)

    def request_view(self,row,follow=False):
        result={'id':row['id'],'revision':row['revision'],'track':json.loads(row['track']),
            'asks':row['asks'],'firstAt':row['first_at'],'stage':row['stage'],
            'followed':bool(follow),'playingNow':bool(row['playing'])}
        for source,key in (('played_at','playedAt'),('closed_at','closedAt'),('estimated_at','at')):
            if row[source] is not None:result[key]=row[source]
        if row['reason'] in REASONS:result['reason']=REASONS[row['reason']]
        return result

    def own(self,night,guest):
        with self.lock:
            self._guest(night,guest)
            rows=self.db.execute('SELECT r.*,a.follow FROM asks a JOIN requests r ON r.id=a.request WHERE a.guest=? AND r.night=? ORDER BY r.first_at DESC,r.id LIMIT 32',(guest,night)).fetchall()
            count=self.db.execute("SELECT count(*) FROM asks a JOIN requests r ON r.id=a.request WHERE a.guest=? AND r.night=? AND r.stage IN ('sent','listening','coming')",(guest,night)).fetchone()[0]
            return [self.request_view(r,r['follow']) for r in rows],count

    def receipt(self,night,guest,command,scope,payload=None):
        command_id(command)
        with self.lock:
            self._guest(night,guest)
            row=self.db.execute('SELECT * FROM receipts WHERE guest=? AND command=?',(guest,command)).fetchone()
            if not row:return None
            if payload is not None and hashlib.sha256(encoded(payload).encode()).hexdigest()!=row['payload_hash']:
                raise GuestError('command-conflict','This request identity was already used for a different choice.',409)
            receipt=json.loads(row['receipt']);receipt['scope']=copy.deepcopy(scope)
            request=self.db.execute('SELECT r.*,a.follow FROM requests r JOIN asks a ON a.request=r.id WHERE r.id=? AND a.guest=?',(receipt['requestId'],guest)).fetchone()
            return {'receipt':receipt,'request':self.request_view(request,request['follow']),'follow':copy.deepcopy(FOLLOW)}

    def ask(self,night,guest,payload,track,scope):
        """Caller checks short-lived selection and current availability first.

        Receipt/quota/dedupe checks repeat under this transaction for concurrent
        senders. A duplicate command never becomes a second ask or Follow edit.
        """
        with self.lock,self.db:
            previous=self.receipt(night,guest,payload['requestId'],scope,payload)
            if previous:return previous
            self._guest(night,guest);track=track_ref(track)
            if track['id']!=payload['trackId']:raise GuestError('invalid-request','The selected recording changed.')
            if self.db.execute('SELECT count(*) FROM receipts WHERE guest=?',(guest,)).fetchone()[0]>=128:
                raise GuestError('capacity','This guest session has reached its request limit.',503)
            existing=self.db.execute('SELECT request,follow FROM asks WHERE guest=? AND track_id=?',(guest,track['id'])).fetchone()
            if existing:
                # Follow may be changed explicitly without incrementing an ask.
                self.db.execute('UPDATE asks SET follow=? WHERE guest=? AND track_id=?',(int(payload['follow']),guest,track['id']))
                request_id=existing['request'];duplicate=True
            else:
                open_count=self.db.execute("SELECT count(*) FROM asks a JOIN requests r ON r.id=a.request WHERE a.guest=? AND r.stage IN ('sent','listening','coming')",(guest,)).fetchone()[0]
                if open_count>=3:raise GuestError('open-limit','You already have three open requests.',409)
                grouped=self.db.execute('SELECT * FROM requests WHERE night=? AND track_id=?',(night,track['id'])).fetchone()
                if grouped:request_id=grouped['id']
                else:
                    counts=self.db.execute("SELECT count(*),sum(stage IN ('sent','listening','coming')) FROM requests WHERE night=?",(night,)).fetchone()
                    if counts[0]>=4096 or (counts[1] or 0)>=256:raise GuestError('capacity','The DJ has a full request list. Try again later.',503)
                    request_id='request-'+secrets.token_hex(16)
                    self.db.execute('INSERT INTO requests(id,night,track_id,track,asks,first_at,stage,revision) VALUES(?,?,?,?,0,?,?,0)',(request_id,night,track['id'],encoded(track),self.clock(),'sent'))
                self.db.execute('INSERT INTO asks VALUES(?,?,?,?)',(guest,request_id,track['id'],int(payload['follow'])))
                self.db.execute('UPDATE requests SET asks=asks+1 WHERE id=?',(request_id,));duplicate=False
            revision=self._bump(night)
            self.db.execute('UPDATE requests SET revision=? WHERE id=?',(revision,request_id))
            receipt={'id':payload['requestId'],'status':'accepted','change':{'trackId':track['id'],'follow':payload['follow']},
                'requestId':request_id,'revision':revision,'duplicate':duplicate}
            self.db.execute('INSERT INTO receipts VALUES(?,?,?,?)',(guest,payload['requestId'],hashlib.sha256(encoded(payload).encode()).hexdigest(),encoded(receipt)))
            return self.receipt(night,guest,payload['requestId'],scope)

    def pending(self,night,limit=8):
        with self.lock:
            rows=self.db.execute("SELECT * FROM requests WHERE night=? AND stage IN ('sent','listening') ORDER BY first_at,id LIMIT ?",(night,min(256,max(1,limit)))).fetchall()
            return [self.request_view(row) for row in rows]

    def stage(self,night,identifier,stage,*,reason=None,round_id=None,at=None,playing=False,require_round=None):
        if stage not in STAGES or reason not in (*REASONS,None):raise ValueError('Invalid guest request stage.')
        with self.lock,self.db:
            row=self.db.execute('SELECT * FROM requests WHERE id=? AND night=?',(identifier,night)).fetchone()
            if not row:return False
            if require_round is not None and row['round_id']!=require_round:return False
            if row['stage']=='declined':return False
            # An observed play is a terminal result even after the deck stops.
            if row['played_at'] is not None:stage='playing';reason=None;at=None;round_id=None
            if stage!='coming':at=None
            if at is not None and (type(at) not in (int,float) or not math.isfinite(at) or at<self.clock()):at=None
            fields={'stage':stage,'reason':reason,'round_id':round_id,'estimated_at':at,'playing':int(playing and stage=='playing'),
                'played_at':row['played_at'] if row['played_at'] is not None else self.clock() if stage=='playing' else None,
                'closed_at':row['closed_at'] if row['closed_at'] is not None else self.clock() if stage in ('playing','declined') else None}
            if all(row[k]==v for k,v in fields.items()):return False
            revision=self._bump(night);fields['revision']=revision
            self.db.execute('UPDATE requests SET '+','.join(k+'=?' for k in fields)+' WHERE id=?',(*fields.values(),identifier))
            return True

    def reconciliation_rows(self,night):
        with self.lock:
            rows=self.db.execute("SELECT * FROM requests WHERE night=? AND (stage IN ('sent','listening','coming') OR playing=1) ORDER BY first_at LIMIT 512",(night,)).fetchall()
            return [dict(row) for row in rows]

    def operator_page(self,night,page=0,page_size=50):
        if type(page) is not int or page<0 or type(page_size) is not int or not 1<=page_size<=50:raise ValueError('Invalid request page.')
        with self.lock:
            count=self.db.execute('SELECT count(*) FROM requests WHERE night=?',(night,)).fetchone()[0]
            pages=max(1,math.ceil(count/page_size));page=min(page,pages-1)
            rows=self.db.execute('SELECT * FROM requests WHERE night=? ORDER BY first_at DESC,id LIMIT ? OFFSET ?',(night,page_size,page*page_size)).fetchall()
            return {'version':1,'nightId':night,'revision':self.revision(night),'page':page,'pages':pages,'count':count,
                'requests':[self.request_view(row) for row in rows],'capabilities':{'override':False}}
