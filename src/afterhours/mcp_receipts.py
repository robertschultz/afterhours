"""Durable local client grants and at-most-once DJ ingress.

Reservations are conservative cost ceilings, not provider billing records.
Dispatch claims survive crashes. Unknown outcomes are never replayed.
"""
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
import fcntl
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time

from afterhours.mcp_auth import ControlError

ACTIVE = {'dispatching', 'pending', 'uncertain'}
TERMINAL = {'confirmed', 'blocked', 'superseded'}
ID = re.compile(r'[A-Za-z0-9_-]{1,80}')
UNMETERED_MUSIC = "dj:music:unmetered"
MAX_REQUESTS = 10000  # Never evict identities and accidentally authorize replay.
REASONS = {'accepted','engine-confirmed','engine-guard','newer-direction','outcome-unconfirmed',
           'control-failed','control-blocked','control-cancelled','client-revoked','grant-expired',
           'grant-inactive','allowance-exhausted','preparation-held','selection-held','discovery-failed'}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


def units(value):
    try:
        amount = Decimal(str(value)) * 1_000_000
        if not amount.is_finite() or amount != amount.to_integral_value():
            raise ValueError()
        return int(amount)
    except (InvalidOperation, ValueError, TypeError):
        raise ControlError('invalid-allowance', 'Use a finite USD ceiling with at most six decimal places.') from None


class ReceiptStore:
    def __init__(self, path, clock=time.time):
        self.path = Path(path)
        self.clock = clock
        self.lock = threading.RLock()
        self.owner_fd = None
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.parent.is_symlink() or self.path.is_symlink():
            raise ControlError('storage-unavailable', 'MCP storage cannot use a symbolic link.')
        parent = self.path.parent.stat()
        if parent.st_uid != os.getuid() or parent.st_mode & 0o077:
            raise ControlError('storage-unavailable', 'MCP storage requires an owner-only directory.')
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            info = os.fstat(fd)
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ControlError('storage-unavailable', 'MCP storage requires owner-only permissions.')
        finally:
            os.close(fd)
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS clients (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, credential_hash TEXT NOT NULL,
                scopes TEXT NOT NULL, allowance TEXT, revoked INTEGER NOT NULL DEFAULT 0,
                reserved_cost INTEGER NOT NULL DEFAULT 0, reserved_calls INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS requests (
                client_id TEXT NOT NULL REFERENCES clients(id), id TEXT NOT NULL,
                engine_id TEXT NOT NULL UNIQUE, fingerprint TEXT NOT NULL,
                status TEXT NOT NULL, message TEXT NOT NULL, created REAL NOT NULL,
                session_id TEXT NOT NULL, revision INTEGER NOT NULL,
                allocation TEXT NOT NULL, consumed TEXT NOT NULL,
                reserved_cost INTEGER NOT NULL, reserved_calls INTEGER NOT NULL,
                expires REAL NOT NULL, prepare INTEGER NOT NULL,
                PRIMARY KEY(client_id,id));
        ''')

    def acquire_dispatch_owner(self):
        """Only the engine gateway owns dispatch/recovery, never a DB reader.

        flock is released by the OS on process death. Refuse a second gateway
        even in the same process, then recover only after exclusive ownership.
        Management uses the live engine's authenticated HTTP routes.
        """
        with self.lock:
            if self.owner_fd is not None:
                raise ControlError('dispatch-owner-active', 'This store already has a dispatch owner.')
            fd = os.open(self.path.with_suffix('.owner'), os.O_CREAT | os.O_RDWR
                         | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            try:
                info = os.fstat(fd)
                if info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise ControlError('storage-unavailable', 'MCP ownership requires owner-only permissions.')
                try:fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ControlError('dispatch-owner-active', 'Another engine owns MCP dispatch for this data root.') from None
                with self.transaction():
                    self.db.execute("UPDATE requests SET status='uncertain', message=? WHERE status IN ('dispatching','pending')",
                                    ('The previous engine stopped before the outcome was confirmed. No request was replayed.',))
                self.owner_fd = fd
            except BaseException:
                os.close(fd)
                raise

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                yield
                self.db.execute('COMMIT')
            except BaseException:
                self.db.execute('ROLLBACK')
                raise

    def close(self):
        with self.lock:
            self.db.close()
            if self.owner_fd is not None:
                os.close(self.owner_fd)
                self.owner_fd = None

    def pair(self, name, *, read_only=False, allowance=None, unmetered_music=False):
        if (not isinstance(name, str) or not 1 <= len(name.strip()) <= 48
                or any(not c.isprintable() for c in name)):
            raise ControlError('invalid-client', 'Use a printable client name of 1–48 characters.')
        if type(read_only) is not bool:
            raise ControlError('invalid-client', 'Read-only must be a boolean.')
        if type(unmetered_music) is not bool or (unmetered_music and (read_only or allowance is not None)):
            raise ControlError('invalid-allowance', 'Unmetered music requires an explicit, writable pairing without a bounded allowance.')
        scopes = ['state:read'] if read_only else ['state:read', 'dj:local']
        if unmetered_music:
            scopes.extend(('dj:music', UNMETERED_MUSIC))
        if allowance is not None:
            if read_only or not isinstance(allowance, dict) or set(allowance) != {'maxCostUSD', 'maxProviderCalls', 'expiresAt', 'prepare'}:
                raise ControlError('invalid-allowance', 'Music needs an explicit cost, call limit, expiry and preparation setting.')
            cost = units(allowance['maxCostUSD'])
            calls, expires = allowance['maxProviderCalls'], allowance['expiresAt']
            if (not 1 <= cost <= 100_000_000 or type(calls) is not int or not 1 <= calls <= 10000
                    or type(expires) not in (int, float) or not math.isfinite(expires)
                    or not self.clock() < expires <= self.clock() + 86400
                    or type(allowance['prepare']) is not bool):
                raise ControlError('invalid-allowance', 'Music allowance must be positive, expire within 24 hours and have bounded calls.')
            allowance = {**allowance, 'maxCostUSD': cost / 1_000_000}
            scopes.append('dj:music')
        identifier = secrets.token_hex(16)
        credential = identifier + '.' + secrets.token_urlsafe(32)
        with self.transaction():
            if self.db.execute('SELECT count(*) FROM clients').fetchone()[0] >= 128:
                raise ControlError('client-capacity', 'The pairing registry is full; retained request identities cannot be erased safely.')
            self.db.execute('INSERT INTO clients(id,name,credential_hash,scopes,allowance) VALUES(?,?,?,?,?)',
                            (identifier, name.strip(), hashlib.sha256(credential.encode()).hexdigest(),
                             canonical(scopes), canonical(allowance) if allowance else None))
        return {'clientId': identifier, 'name': name.strip(), 'credential': credential, 'scopes': scopes, 'allowance': allowance}

    def authenticate(self, credential):
        if not isinstance(credential, str) or len(credential) > 128:
            raise ControlError('unauthorized', 'A valid MCP client credential is required.')
        with self.lock:
            row = self.db.execute('SELECT * FROM clients WHERE id=?', (credential.split('.')[0],)).fetchone()
        if not row or row['revoked'] or not hmac.compare_digest(row['credential_hash'], hashlib.sha256(credential.encode()).hexdigest()):
            raise ControlError('unauthorized', 'The MCP client credential is invalid or revoked.')
        return self._client(row)

    @staticmethod
    def _client(row):
        return {'id': row['id'], 'name': row['name'], 'scopes': json.loads(row['scopes']),
                'allowance': json.loads(row['allowance']) if row['allowance'] else None,
                'committedCeilingUSD': row['reserved_cost'] / 1_000_000,
                'committedProviderCalls': row['reserved_calls']}

    def clients(self):
        with self.lock:
            return [{**self._client(row), 'revoked': bool(row['revoked'])}
                    for row in self.db.execute('SELECT * FROM clients ORDER BY id')]

    def revoke(self, client_id):
        with self.transaction():
            if self.db.execute('UPDATE clients SET revoked=1 WHERE id=?', (client_id,)).rowcount != 1:
                raise ControlError('unknown-client', 'No paired client has that identity.')
        # Already playing music and native cleanup are never stopped here.

    def lookup(self, client_id, request_id, payload=None):
        with self.lock:
            row = self.db.execute('SELECT * FROM requests WHERE client_id=? AND id=?', (client_id, request_id)).fetchone()
        if row and payload is not None and row['fingerprint'] != hashlib.sha256(canonical(payload).encode()).hexdigest():
            raise ControlError('request-conflict', 'This request ID belongs to a different payload. Read its original receipt.')
        return dict(row) if row else None

    def native_lookup(self, engine_id):
        """Internal native projection only, never an external cross-client API."""
        with self.lock:
            row=self.db.execute('SELECT * FROM requests WHERE engine_id=?',(engine_id,)).fetchone()
            if not row:return None,None
            client=self.db.execute('SELECT id,name FROM clients WHERE id=?',(row['client_id'],)).fetchone()
            return dict(row),dict(client)

    def claim(self, client, request_id, payload, context, *, music, selection_cost, preparation_cost, selection_calls, operation='local'):
        if self.owner_fd is None:
            raise ControlError('storage-unavailable', 'Only the owning engine can dispatch a direction.')
        if not isinstance(request_id, str) or not ID.fullmatch(request_id):
            raise ControlError('invalid-request', 'Use a stable request ID of 1–80 letters, digits, hyphens or underscores.')
        with self.transaction():
            previous = self.lookup(client['id'], request_id, payload)
            if previous:
                return previous, False
            row = self.db.execute('SELECT * FROM clients WHERE id=?', (client['id'],)).fetchone()
            if not row or row['revoked']:
                raise ControlError('unauthorized', 'The MCP client was revoked.')
            current = self._client(row)
            if 'dj:local' not in current['scopes']:
                raise ControlError('forbidden', 'This client has read-only access.')
            if self.db.execute('SELECT count(*) FROM requests WHERE client_id=?', (client['id'],)).fetchone()[0] >= MAX_REQUESTS:
                raise ControlError('receipt-capacity', 'The client receipt store is full. Pair a new identity; old IDs remain protected.')
            allocation = {'selection': 0, 'review': 0, 'discovery': 0}
            prepare = False
            expires = self.clock() + 180
            if type(selection_calls) is not int or selection_calls < 1:
                raise ValueError('Selection call count must be positive')
            costs = {'selection': (units(selection_cost) + selection_calls - 1) // selection_calls,
                     'review': units(preparation_cost), 'discovery': 0}
            limit = current['allowance']
            unmetered = UNMETERED_MUSIC in current['scopes']
            if music:
                if not unmetered and ('dj:music' not in current['scopes'] or not limit or self.clock() >= limit['expiresAt']):
                    raise ControlError('allowance-required', 'This client has no current music allowance. Local controls remain available.')
                prepare = unmetered or limit['prepare']
                allocation = {'selection': selection_calls, 'review': int(prepare), 'discovery': 2}
                expires = self.clock() + 900
                if not unmetered:
                    expires = min(expires, limit['expiresAt'])
            cost = sum(allocation[k] * costs[k] for k in allocation)
            calls = allocation['selection'] + allocation['review']
            if music and not unmetered and (row['reserved_cost'] + cost > units(limit['maxCostUSD']) or row['reserved_calls'] + calls > limit['maxProviderCalls']):
                raise ControlError('allowance-exhausted', 'The client allowance cannot reserve another bounded turn.')
            engine_id = 'mcp-' + hashlib.sha256((client['id'] + '\0' + request_id).encode()).hexdigest()[:40]
            self.db.execute('''INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                            (client['id'], request_id, engine_id, hashlib.sha256(canonical(payload).encode()).hexdigest(),
                             'dispatching', 'Direction accepted. Execution is not yet confirmed.', self.clock(),
                             context['sessionId'], context['revision'], canonical({'limits': allocation, 'costs': costs,'operation':operation}),
                             canonical({k: 0 for k in allocation}), cost, calls, expires, int(prepare)))
            self.db.execute('UPDATE clients SET reserved_cost=reserved_cost+?,reserved_calls=reserved_calls+? WHERE id=?',
                            (cost, calls, client['id']))
            return self.lookup(client['id'], request_id), True

    def check(self, engine_id):
        with self.lock:
            row = self.db.execute('''SELECT r.*,c.revoked FROM requests r JOIN clients c ON c.id=r.client_id WHERE engine_id=?''',
                                  (engine_id,)).fetchone()
        if not row or row['status'] not in ('dispatching', 'pending'):
            raise ControlError('grant-inactive', 'This direction expired, was revoked or no longer owns its grant.')
        if row['revoked']:raise ControlError('client-revoked','This client was revoked. Future dispatch is blocked.')
        if self.clock() >= row['expires']:raise ControlError('grant-expired','This direction expired. Future dispatch is blocked.')
        return dict(row)

    def consume(self, engine_id, kind):
        with self.transaction():
            row = self.check(engine_id)
            allocation, consumed = json.loads(row['allocation']), json.loads(row['consumed'])
            if kind not in consumed or consumed[kind] >= allocation['limits'][kind]:
                raise ControlError('allowance-exhausted', 'This direction has no remaining allowance for that operation.')
            consumed[kind] += 1
            self.db.execute('UPDATE requests SET consumed=? WHERE engine_id=?', (canonical(consumed), engine_id))

    def update(self, engine_id, status, message, reason=None):
        if status not in ACTIVE | TERMINAL:
            raise ValueError('Invalid MCP receipt status')
        with self.transaction():
            row = self.db.execute('SELECT * FROM requests WHERE engine_id=?', (engine_id,)).fetchone()
            if not row or row['status'] in TERMINAL:
                return
            # Terminal records release only never-dispatched reservations. A
            # consumed provider call retains its ceiling even when it fails.
            if status in TERMINAL:
                allocation, consumed = json.loads(row['allocation']), json.loads(row['consumed'])
                cost = sum(consumed[k] * allocation['costs'][k] for k in consumed)
                calls = consumed['selection'] + consumed['review']
                self.db.execute('UPDATE clients SET reserved_cost=reserved_cost-?,reserved_calls=reserved_calls-? WHERE id=?',
                                (row['reserved_cost'] - cost, row['reserved_calls'] - calls, row['client_id']))
                self.db.execute('UPDATE requests SET reserved_cost=?,reserved_calls=? WHERE engine_id=?', (cost, calls, engine_id))
            allocation=json.loads(row['allocation'])
            allocation['outcomeReason']=reason if reason in REASONS else {
                'confirmed':'engine-confirmed','blocked':'engine-guard','superseded':'newer-direction',
                'uncertain':'outcome-unconfirmed'}.get(status,'accepted')
            self.db.execute('UPDATE requests SET status=?,message=?,allocation=? WHERE engine_id=?',
                            (status, str(message)[:1600],canonical(allocation),engine_id))

    @staticmethod
    def public(row):
        return {'requestId': row['id'], 'commandId': row['engine_id'], 'status': row['status'],
                'message': row['message'], 'createdAt': row['created'], 'sessionId': row['session_id'],
                'reservedCeilingUSD': row['reserved_cost'] / 1_000_000,
                'reasonCode':('outcome-unconfirmed' if row['status']=='uncertain' else
                              json.loads(row['allocation']).get('outcomeReason','accepted')),
                'providerCallsDispatched': sum(json.loads(row['consumed'])[k] for k in ('selection', 'review')),
                'replayable': False, 'pollAfterSeconds': 2 if row['status'] in ACTIVE else None}


class ExternalGrant:
    """One musical turn's budget; never aliases the operator's allowance."""
    def __init__(self, store, row, client):
        self.store = store
        self.id = row['engine_id']
        self.client = {'id': client['id'], 'name': client['name']}
        self.remaining = json.loads(row['allocation'])['limits']['selection']
        self.deadline = min(store.clock() + 180, row['expires'])
        self.expires = row['expires']
        self.prepare = bool(row['prepare'])
        self.text_turn = True
        self.background_allowance = False

    def check(self):
        self.store.check(self.id)

    def consume(self, kind):
        self.store.consume(self.id, kind)

    def finish(self, status, message, reason=None):
        self.store.update(self.id, status, message, reason)
