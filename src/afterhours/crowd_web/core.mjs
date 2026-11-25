// Shared browser protocol/state boundary. No operator, native or provider API.
export const VERSION = 1;
const stages = new Set(['sent', 'listening', 'coming', 'declined', 'playing']);
const pollStages = new Set(['open', 'applied', 'kept', 'cancelled', 'interrupted']);
const exactID = /^virtualdj:[a-f0-9]{64}$/;
const commandID = /^[A-Za-z0-9_-]{8,80}$/;
const tokenPattern = /^[A-Za-z0-9_-]{32,256}$/;
const plain = (v, max = 240) => typeof v === 'string' && v.length > 0 && v.length <= max && !/[\u0000-\u001f\u007f]/.test(v);
const finite = v => typeof v === 'number' && Number.isFinite(v);
const integer = v => Number.isSafeInteger(v) && v >= 0;
const object = v => v !== null && typeof v === 'object' && !Array.isArray(v);
const optional = (v, key, check) => !(key in v) || check(v[key]);
function check(ok, message = 'The room returned an invalid response.') { if (!ok) throw new CrowdError('invalid-response', message); }
export class CrowdError extends Error {
  constructor(code, message, status = 0, retryAfter = 0) { super(message); this.code = code; this.status = status; this.retryAfter = retryAfter; }
}
export function scopeKey(scope) { return [scope.origin, scope.roomId, scope.nightId, scope.generation].join('\n'); }
export function sameScope(a, b) { return !!a && !!b && scopeKey(a) === scopeKey(b); }
export function safeArt(value) {
  if (!plain(value, 2048)) return undefined;
  try {
    const url = new URL(value);
    if (!['https:', 'http:'].includes(url.protocol) || !url.hostname || url.username || url.password || url.hash) return undefined;
    return url.href;
  } catch { return undefined; }
}
// Supplied art.ts rules, using the existing desktop/phone stable ID seed.
// Kept pure so exact spec vectors can verify fallback identity and geometry.
const sleevePalette = [['#22324A','#E9DCC4'],['#1E4A43','#EFC35A'],['#5E4467','#F1E2D6'],['#13293D','#86BBC2'],['#3B2B2D','#D7A675'],['#2E3B2C','#C9D6A3'],['#4A2F4D','#E7B4A8'],['#1C1C24','#B9B4E8'],['#6E5A3E','#F3EBDD'],['#28445C','#F2C2A8'],['#3F3F46','#E4E4DD'],['#1F3A3D','#E6C8E0']];
const sleeveShapes = ['circle', 'split', 'stripes', 'rings', 'diagonal'];
const sleeveDemo = {53:[9,0],61:[7,4],83:[1,0],71:[0,2],101:[6,1],113:[4,3],97:[8,0],109:[5,4],103:[3,2],31:[10,1],
  127:[11,0],107:[2,3],131:[9,2],137:[3,1],139:[1,4],149:[5,0],7:[11,4],19:[2,0],43:[8,2]};
export function fallbackSleeve(id) {
  let hash = 2166136261;
  for (const byte of new TextEncoder().encode(id)) hash = Math.imul(hash ^ byte, 16777619) >>> 0;
  const seed = hash % 100000, demo = sleeveDemo[seed];
  const random = index => { const value = Math.sin(seed * 12.9898 + index * 78.233) * 43758.5453; return value - Math.floor(value); };
  const [base, accent] = sleevePalette[demo ? demo[0] : Math.floor(random(2) * sleevePalette.length)];
  const shape = sleeveShapes[demo ? demo[1] : Math.floor(random(1) * sleeveShapes.length)];
  let css;
  switch (shape) {
    case 'circle': css = `radial-gradient(circle at 62% 58%, ${accent} 0 31%, rgba(0,0,0,0) 31.6%), linear-gradient(${base}, ${base})`; break;
    case 'split': css = `radial-gradient(circle at 30% 58%, ${base} 0 12%, rgba(0,0,0,0) 12.6%), linear-gradient(180deg, ${base} 0 58%, ${accent} 58% 100%)`; break;
    case 'stripes': css = `repeating-linear-gradient(90deg, ${accent} 0 8%, ${base} 8% 20%)`; break;
    case 'rings': css = `radial-gradient(circle at 50% 50%, ${accent} 0 6%, rgba(0,0,0,0) 6.5%), repeating-radial-gradient(circle at 50% 50%, ${base} 0 9%, ${accent} 9% 11%)`; break;
    case 'diagonal': css = `radial-gradient(circle at 72% 28%, ${accent} 0 9%, rgba(0,0,0,0) 9.6%), linear-gradient(135deg, ${base} 0 50%, ${accent} 50% 100%)`; break;
  }
  return {seed, base, accent, shape, css};
}
export function validateScope(value, origin) {
  check(object(value) && Object.keys(value).sort().join(',') === 'generation,nightId,origin,roomId');
  check(value.origin === origin && ['roomId', 'nightId', 'generation'].every(k => plain(value[k], 128)));
  return value;
}
export function validateTrack(value) {
  check(object(value) && exactID.test(value.id) && plain(value.title) && plain(value.artist));
  check(optional(value, 'artworkUrl', v => !!safeArt(v)));
  return value;
}
export function validateRequest(value, revision) {
  check(object(value) && plain(value.id, 128) && integer(value.revision) && value.revision <= revision);
  validateTrack(value.track);
  check(integer(value.asks) && value.asks >= 1 && finite(value.firstAt) && stages.has(value.stage));
  check(typeof value.followed === 'boolean' && typeof value.playingNow === 'boolean');
  check(!value.playingNow || value.stage === 'playing');
  check(value.stage !== 'playing' || finite(value.playedAt));
  check(optional(value, 'reason', v => plain(v)) && optional(value, 'at', finite));
  check(optional(value, 'closedAt', finite) && optional(value, 'playedAt', finite));
  check(!('at' in value) || value.stage === 'coming');
  return value;
}
export function validatePoll(value, revision) {
  check(object(value) && plain(value.id, 128) && integer(value.revision) && value.revision <= revision);
  check(pollStages.has(value.status) && finite(value.openedAt) && finite(value.closesAt)
    && Math.abs(value.closesAt - value.openedAt - 60) < .001);
  check(optional(value, 'slotAt', finite) && optional(value, 'reason', v => plain(v)));
  check(Array.isArray(value.options) && value.options.length >= 2 && value.options.length <= 3);
  const ids = new Set(); let total = 0;
  for (const option of value.options) {
    check(object(option)); validateTrack(option.track);
    check(!ids.has(option.track.id) && integer(option.votes) && option.votes <= 1024);
    ids.add(option.track.id); total += option.votes;
  }
  check(integer(value.totalVotes) && value.totalVotes <= 1024 && value.totalVotes === total);
  check(optional(value, 'myChoiceId', v => ids.has(v)) && optional(value, 'chosenTrackId', v => ids.has(v)));
  check(['applied', 'kept'].includes(value.status) === ('chosenTrackId' in value));
  return value;
}
const trackIdentity = track => JSON.stringify([track.id, track.title, track.artist, track.artworkUrl || null]);
function pollFollows(value, previous) {
  check(value.id === previous.id && value.openedAt === previous.openedAt && value.closesAt === previous.closesAt
    && value.options.length === previous.options.length
    && value.options.every((option, i) => trackIdentity(option.track) === trackIdentity(previous.options[i].track)),
  'The room changed the recordings in this vote. Refresh to check again.');
  check(value.revision >= previous.revision && (previous.status === 'open' || value.status === previous.status),
    'The room returned an older vote. Refresh to check again.');
  if (value.revision === previous.revision) check(value.status === previous.status
    && value.totalVotes === previous.totalVotes && value.myChoiceId === previous.myChoiceId
    && value.chosenTrackId === previous.chosenTrackId
    && value.options.every((option, i) => option.votes === previous.options[i].votes),
  'The room returned conflicting vote counts. Refresh to check again.');
}
function common(value, origin, scope, revision = 0) {
  check(object(value) && value.version === VERSION && integer(value.revision) && finite(value.serverAt));
  validateScope(value.scope, origin);
  check(!scope || sameScope(scope, value.scope), 'This response belongs to a different room invitation.');
  check(value.revision >= revision, 'The room returned an older update. Refresh to check again.');
  return value;
}
function follow(value) { check(object(value) && value.status === 'configuration-required' && value.foregroundStatus === true); }
export function validateState(value, origin, scope, revision = 0) {
  common(value, origin, scope, revision);
  check(object(value.guest) && plain(value.guest.id, 128) && finite(value.guest.expiresAt));
  check(integer(value.guest.openCount) && value.guest.openCount <= 3 && value.guest.openLimit === 3);
  check(object(value.capabilities) && value.capabilities.search === true && value.capabilities.ask === true
    && value.capabilities.status === true && typeof value.capabilities.poll === 'boolean' && value.capabilities.liveActivity === false);
  if ('poll' in value) {
    check(value.capabilities.poll); validatePoll(value.poll, value.revision);
    check(value.poll.openedAt <= value.serverAt + 2);
  }
  follow(value.follow);
  check(object(value.night) && optional(value.night, 'title', v => plain(v)) && optional(value.night, 'venue', v => plain(v)));
  check(object(value.performer) && plain(value.performer.id, 128) && plain(value.performer.name, 100)
    && optional(value.performer, 'imageUrl', v => !!safeArt(v)));
  check('program' in value);
  if (value.program !== null) {
    const p = value.program; check(object(p)); validateTrack(p.track);
    check(typeof p.playing === 'boolean' && finite(p.sampledAt)
      && optional(p, 'positionSeconds', v => finite(v) && v >= 0 && v <= 86400)
      && optional(p, 'durationSeconds', v => finite(v) && v > 0 && v <= 86400));
  }
  if ('next' in value) { check(object(value.next)); validateTrack(value.next.track); check(optional(value.next, 'estimatedAt', finite)); }
  check(Array.isArray(value.requests) && value.requests.length <= 32);
  const ids = new Set(); let visibleOpen = 0;
  for (const row of value.requests) {
    validateRequest(row, value.revision); check(!ids.has(row.id)); ids.add(row.id);
    if (['sent', 'listening', 'coming'].includes(row.stage)) visibleOpen++;
  }
  check(value.guest.openCount >= visibleOpen);
  check(optional(value, 'guestToken', v => typeof v === 'string' && tokenPattern.test(v)));
  return value;
}
export function validateSearch(value, origin, scope, revision, query) {
  common(value, origin, scope, revision);
  check(value.query === query && plain(value.searchId, 80) && finite(value.expiresAt)
    && value.expiresAt > value.serverAt && value.expiresAt <= value.serverAt + 120.1 && value.scopeLabel === 'known-crate');
  check(Array.isArray(value.records) && value.records.length <= 5);
  const ids = new Set(); for (const row of value.records) { validateTrack(row); check(!ids.has(row.id)); ids.add(row.id); }
  return value;
}
export function validateReceipt(value, origin, scope, revision, pending) {
  common(value, origin, scope, revision); follow(value.follow);
  const r = value.receipt; check(object(r) && r.id === pending.body.requestId && r.status === 'accepted');
  check(sameScope(scope, r.scope) && integer(r.revision) && r.revision <= value.revision && typeof r.duplicate === 'boolean');
  check(object(r.change) && r.change.trackId === pending.body.trackId && r.change.follow === pending.body.follow);
  validateRequest(value.request, value.revision);
  check(r.requestId === value.request.id && value.request.track.id === pending.body.trackId);
  return value;
}
export function validateVoteReceipt(value, origin, scope, revision, pending) {
  common(value, origin, scope, revision); validatePoll(value.poll, value.revision);
  const r = value.receipt; check(object(r) && r.id === pending.body.requestId && r.status === 'accepted');
  check(sameScope(scope, r.scope) && integer(r.revision) && r.revision <= value.revision
    && integer(r.pollRevision) && r.pollRevision <= r.revision && r.pollRevision <= value.poll.revision
    && typeof r.duplicate === 'boolean');
  check(object(r.change) && r.change.pollId === pending.body.pollId && r.change.trackId === pending.body.trackId
    && value.poll.id === pending.body.pollId && value.poll.closesAt === pending.closesAt
    && value.poll.openedAt <= value.serverAt + 2
    && value.poll.options.some(option => trackIdentity(option.track) === trackIdentity(pending.track)));
  return value;
}
// Keep foreground promises on the same three-second lease as the service.
// A cached historical stage remains useful after its current playback claim expires.
export function currentProgram(state, now) {
  const program = state?.program;
  return program && finite(now) && now - program.sampledAt >= -2 && now - program.sampledAt < 3 ? program : null;
}
export function currentRequest(row, program, now, upcoming) {
  const result = {...row, playingNow: !!(row.playingNow && row.stage === 'playing' && program?.playing
    && row.track.id === program.track.id)};
  if (!program?.playing || row.stage !== 'coming' || !finite(row.at) || row.at <= now
    || upcoming?.track.id !== row.track.id || upcoming?.estimatedAt !== row.at) delete result.at;
  return result;
}
function newCommand() {
  const bytes = new Uint8Array(16); globalThis.crypto.getRandomValues(bytes);
  return 'web-' + Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('');
}
async function boundedJSON(response) {
  const length = response.headers.get('content-length');
  if (length !== null) check(/^\d+$/.test(length) && Number(length) <= 131072, 'The room response was too large.');
  check((response.headers.get('content-type') || '').toLowerCase().startsWith('application/json'));
  const reader = response.body?.getReader();
  if (!reader) { const text = await response.text(); check(new TextEncoder().encode(text).length <= 131072); return JSON.parse(text); }
  let total = 0; const pieces = [];
  try {
    for (;;) { const {value, done} = await reader.read(); if (done) break;
      total += value.length; check(total <= 131072, 'The room response was too large.'); pieces.push(value); }
  } catch (error) { await reader.cancel().catch(() => {}); throw error; }
  const bytes = new Uint8Array(total); let offset = 0;
  for (const piece of pieces) { bytes.set(piece, offset); offset += piece.length; }
  return JSON.parse(new TextDecoder('utf-8', {fatal: true}).decode(bytes));
}
function browserStoreLock(name, action) {
  const locks = globalThis.navigator?.locks;
  if (!locks?.request) throw new CrowdError('storage-lock-unavailable',
    'Requests are unavailable on this browser connection. Open the room on a supported secure connection.');
  return locks.request(name, {mode: 'exclusive'}, action);
}
function owner(saved) {
  const c = saved.credential;
  return c ? JSON.stringify([scopeKey(c.scope), c.guestId, c.token]) : saved.bootstrap || '';
}
function samePending(a, b) {
  return !!a && !!b && a.guestId === b.guestId && sameScope(a.scope, b.scope)
    && ['requestId','searchId','trackId','follow'].every(key => a.body[key] === b.body[key]);
}
function sameVote(a, b) {
  return !!a && !!b && a.guestId === b.guestId && sameScope(a.scope, b.scope) && a.closesAt === b.closesAt
    && trackIdentity(a.track) === trackIdentity(b.track)
    && ['requestId', 'pollId', 'trackId'].every(key => a.body[key] === b.body[key]);
}
export async function discardInvalidSaved(key, expectedRaw, {storage = globalThis.localStorage, lock = browserStoreLock} = {}) {
  return lock(key, () => {
    if (storage.getItem(key) !== expectedRaw) throw new CrowdError('session-changed',
      'The guest session changed in another tab. Reload this page to use the current session.');
    storage.removeItem(key);
  });
}
export class CrowdClient {
  constructor(baseURL, {fetcher = globalThis.fetch.bind(globalThis), storage = globalThis.localStorage,
    clock = () => Date.now(), command = newCommand, timeout = 8000, lock = browserStoreLock} = {}) {
    const url = new URL(baseURL);
    check(['http:', 'https:'].includes(url.protocol) && !url.username && !url.password && !url.search && !url.hash
      && /^\/r\/[A-Za-z0-9_-]{16,128}$/.test(url.pathname), 'Open the room’s request code again.');
    this.base = url.href; this.origin = url.origin; this.path = url.pathname;
    this.fetcher = fetcher; this.storage = storage; this.clock = clock; this.command = command; this.timeout = timeout; this.lock = lock;
    this.key = 'afterhours.engine.crowd.v1:' + this.base; this.saved = {}; this.state = null; this.searchResult = null;
    this.revision = 0; this.epoch = 0; this.searchEpoch = 0; this.controllers = new Set(); this.lastRefresh = 0; this.lastServerAt = 0;
    this.poll = null; this.polls = new Map(); this.pollRefresh = 0; this.pollServerAt = 0;
    this.saved = this.readSaved();
  }
  readSaved() {
    let saved, raw;
    try { raw = this.storage.getItem(this.key); saved = raw ? JSON.parse(raw) : {}; }
    catch { throw new CrowdError('storage-unavailable', 'This browser cannot retain your request safely. Allow website storage, then reload.'); }
    try { this.validateSaved(saved); }
    catch (error) { error.invalidSavedValue = raw; throw error; }
    return saved;
  }
  validateSaved(saved) {
    check(object(saved), 'Saved request data is unavailable.');
    const c = saved.credential;
    if (c) { validateScope(c.scope, this.origin); check(tokenPattern.test(c.token) && plain(c.guestId, 128) && finite(c.expiresAt)); }
    if (saved.pending) {
      const p = saved.pending;
      check(c && object(p) && sameScope(c.scope, p.scope) && p.guestId === c.guestId && object(p.body)
        && commandID.test(p.body.requestId) && plain(p.body.searchId, 80) && exactID.test(p.body.trackId)
        && typeof p.body.follow === 'boolean'); validateTrack(p.track); check(p.track.id === p.body.trackId);
    }
    if (saved.pendingVote) {
      const p = saved.pendingVote;
      check(c && object(p) && sameScope(c.scope, p.scope) && p.guestId === c.guestId && finite(p.closesAt) && object(p.body)
        && Object.keys(p.body).sort().join(',') === 'pollId,requestId,trackId'
        && commandID.test(p.body.requestId) && plain(p.body.pollId, 128) && exactID.test(p.body.trackId));
      validateTrack(p.track); check(p.track.id === p.body.trackId);
      check(!saved.pending || saved.pending.body.requestId !== p.body.requestId, 'Saved request identities conflict.');
    }
    check(!saved.bootstrap || commandID.test(saved.bootstrap));
  }
  assertOwner(current, expected = this.saved) {
    if (owner(current) !== owner(expected)) throw new CrowdError('session-changed',
      'The guest session changed in another tab. Reload this page to use the current session.');
  }
  syncSaved() {
    const current = this.readSaved(); this.assertOwner(current); this.saved = current; return current;
  }
  storageChanged() {
    try { this.syncSaved(); }
    catch (error) { this.suspend(); this.state = null; this.searchResult = null; this.poll = null; throw error; }
  }
  async updateSaved(change, expected = this.saved, epoch = this.epoch) {
    // Every writer shares one browser-managed lock. A cached tab can neither
    // erase another tab's pending command nor resurrect an earlier guest.
    return this.lock(this.key, () => {
      check(epoch === this.epoch, 'This request-storage update was interrupted.');
      const current = this.readSaved(); this.assertOwner(current, expected);
      this.saved = current;
      const next = change(current); this.validateSaved(next);
      if (JSON.stringify(next) !== JSON.stringify(current)) {
        try { this.storage.setItem(this.key, JSON.stringify(next)); }
        catch { throw new CrowdError('storage-unavailable', 'This browser cannot save the request check. Allow website storage before sending.'); }
      }
      this.saved = next; return next;
    });
  }
  get credential() { return this.saved.credential; }
  get pending() { return this.saved.pending || null; }
  get pendingVote() { return this.saved.pendingVote || null; }
  serverNow() { return this.lastServerAt + Math.max(0, (this.clock() - this.lastRefresh) / 1000); }
  fresh() { return !!this.state && this.clock() - this.lastRefresh < 12000; }
  pollNow() { return this.pollServerAt + Math.max(0, (this.clock() - this.pollRefresh) / 1000); }
  pollFresh() { const age = this.clock() - this.pollRefresh; return !!this.poll && age >= 0 && age < 8000; }
  canVote() { return !!(this.credential && this.state?.capabilities.poll && this.pollFresh()
    && this.poll.status === 'open' && this.pollNow() >= this.poll.openedAt - 2 && this.pollNow() < this.poll.closesAt && !this.pendingVote); }
  checkPoll(poll) {
    const previous = this.polls.get(poll.id);
    if (previous) pollFollows(poll, previous);
    else check(this.polls.size < 64, 'The room returned too many votes. Reopen this invitation.');
  }
  suspend() { this.epoch++; for (const c of this.controllers) c.abort(); this.controllers.clear(); }
  async request(route, body, token = this.credential?.token) {
    const controller = new AbortController(); this.controllers.add(controller);
    const timer = setTimeout(() => controller.abort(), this.timeout);
    try {
      const response = await this.fetcher(this.base + '/v1' + route, {method: body === undefined ? 'GET' : 'POST',
        headers: {'Content-Type': 'application/json', ...(token ? {Authorization: 'Bearer ' + token} : {})},
        body: body === undefined ? undefined : JSON.stringify(body), credentials: 'omit', redirect: 'error',
        cache: 'no-store', referrerPolicy: 'no-referrer', signal: controller.signal});
      const value = await boundedJSON(response);
      if (!response.ok) {
        check(object(value) && value.version === 1 && plain(value.code, 80) && plain(value.message));
        // An unscoped 404 is not proof that this guest's durable command was
        // absent. In particular it must never authorize a same-command retry.
        if (token) check(value.scope && this.credential && sameScope(value.scope, this.credential.scope),
          'The room could not verify this response for your invitation.');
        if (value.scope) { validateScope(value.scope, this.origin); check(!this.credential || sameScope(value.scope, this.credential.scope)); }
        throw new CrowdError(value.code, value.message, response.status, finite(value.retryAfter) ? Math.min(120, Math.max(0, value.retryAfter)) : 0);
      }
      return value;
    } catch (error) {
      if (error instanceof CrowdError) throw error;
      throw new CrowdError('connection-unknown', 'The room could not confirm this update. Check again when connected.');
    } finally { clearTimeout(timer); this.controllers.delete(controller); }
  }
  applyState(value) {
    validateState(value, this.origin, this.credential?.scope, this.revision);
    if (this.credential) check(value.guest.id === this.credential.guestId, 'This response belongs to another guest session.');
    if (value.poll) this.checkPoll(value.poll);
    const pollTime = this.poll ? Math.max(value.serverAt, this.pollNow()) : value.serverAt;
    this.state = value; this.revision = value.revision; this.lastRefresh = this.clock(); this.lastServerAt = value.serverAt;
    this.poll = value.poll || null; this.pollRefresh = this.clock(); this.pollServerAt = pollTime;
    if (this.poll) this.polls.set(this.poll.id, this.poll);
    return value;
  }
  async join() {
    const epoch = this.epoch;
    const starting = await this.updateSaved(current => current.bootstrap ? current : {...current, bootstrap: this.command()}, this.saved, epoch);
    const old = starting.credential;
    const value = await this.request('/session', {requestId: starting.bootstrap}, old?.token);
    check(epoch === this.epoch, 'This room update was interrupted.');
    validateState(value, this.origin, old?.scope, this.revision);
    if (old) check(value.guest.id === old.guestId && !('guestToken' in value));
    else check(tokenPattern.test(value.guestToken));
    const credential = old || {token: value.guestToken, guestId: value.guest.id, expiresAt: value.guest.expiresAt, scope: value.scope};
    await this.updateSaved(current => ({...current, credential}), starting, epoch);
    this.applyState(value); return value;
  }
  async newGuest() {
    const expected = this.saved; this.suspend();
    await this.updateSaved(() => ({}), expected);
    this.state = null; this.searchResult = null; this.revision = 0;
    this.lastRefresh = 0; this.lastServerAt = 0;
    this.poll = null; this.polls.clear(); this.pollRefresh = 0; this.pollServerAt = 0;
  }
  async refresh() {
    this.syncSaved(); check(this.credential, 'Join this room before checking requests.'); const epoch = this.epoch;
    const value = await this.request('/state'); check(epoch === this.epoch, 'This update was interrupted.');
    this.syncSaved();
    return this.applyState(value);
  }
  async search(query) {
    this.syncSaved();
    check(this.credential && plain(query, 160) && query.trim().length >= 2, 'Use at least two characters to search.');
    query = query.trim(); const epoch = this.epoch, searchEpoch = ++this.searchEpoch;
    const value = await this.request('/search?q=' + encodeURIComponent(query)); check(epoch === this.epoch, 'This search was interrupted.');
    check(searchEpoch === this.searchEpoch, 'A newer search replaced this one.');
    this.syncSaved();
    validateSearch(value, this.origin, this.credential.scope, this.revision, query);
    this.revision = value.revision; this.searchResult = value; return value;
  }
  selection(trackId) {
    const result = this.searchResult; const track = result?.records.find(r => r.id === trackId);
    check(track, 'Refresh this exact recording before sending.');
    return {track, query: result.query, searchId: result.searchId, expiresAt: result.expiresAt, scope: result.scope, guestId: this.credential.guestId};
  }
  async refreshSelection(selection) {
    const result = await this.search(selection.query);
    const track = result.records.find(r => r.id === selection.track.id);
    if (!track) throw new CrowdError('recording-unavailable', 'This exact recording is no longer in the results. Nothing was replaced.');
    return this.selection(track.id);
  }
  async complete(value, pending) {
    check(this.credential?.guestId === pending.guestId);
    validateReceipt(value, this.origin, this.credential.scope, this.revision, pending);
    await this.updateSaved(current => {
      // Another tab may already have resolved this receipt, or begun a later
      // command. This accepted response must never clear that later command.
      if (current.pending?.body.requestId !== pending.body.requestId) return current;
      check(samePending(current.pending, pending), 'The saved request identity changed.');
      return {...current, pending: null};
    });
    // A concurrent state refresh can advance the revision while this receipt
    // waits for the storage lock. Resolving its command must not rewind state.
    this.revision = Math.max(this.revision, value.revision); return value;
  }
  async dispatch(pending, retry = false) {
    try {
      this.syncSaved(); check(samePending(this.pending, pending), 'The pending request changed.');
      const value = await this.request('/requests', pending.body); return this.complete(value, pending);
    }
    catch (error) {
      // A valid rejection proves no acceptance for this exact command. Unknown
      // network/parse/identity outcomes retain it for receipt lookup only.
      if (!retry && ['invalid-request','open-limit','recording-unavailable','night-closed','selection-expired',
        'body-too-large','json-required','rate-limited'].includes(error.code) && [400, 409, 410, 413, 415, 429].includes(error.status)
        && this.pending?.body.requestId === pending.body.requestId && this.credential?.guestId === pending.guestId)
        await this.updateSaved(current => samePending(current.pending, pending) ? {...current, pending: null} : current);
      throw error;
    }
  }
  async send(selection, wantsFollow = false) {
    const epoch = this.epoch;
    const saved = await this.updateSaved(current => {
      if (current.pending) throw new CrowdError('request-pending', 'Check the saved request before sending another. It may have been sent in another tab.');
      check(current.credential && sameScope(selection.scope, current.credential.scope)
        && selection.guestId === current.credential.guestId, 'This selection belongs to another guest session.');
      check(this.state && this.state.guest.openCount < 3, 'Three requests are open. A slot opens when one plays or is declined.');
      const pending = {scope: selection.scope, guestId: selection.guestId, track: selection.track,
        body: {requestId: this.command(), searchId: selection.searchId, trackId: selection.track.id, follow: wantsFollow}};
      return {...current, pending};
    }, this.saved, epoch);
    check(epoch === this.epoch, 'This request was interrupted.');
    return this.dispatch(saved.pending);
  }
  async reconcile() {
    this.syncSaved();
    const pending = this.pending; check(pending, 'There is no request awaiting confirmation.');
    const value = await this.request('/receipts/' + encodeURIComponent(pending.body.requestId));
    return this.complete(value, pending);
  }
  async retry() {
    this.syncSaved();
    const pending = this.pending; check(pending, 'There is no request to retry.');
    try { return await this.reconcile(); }
    catch (error) { if (error.code !== 'receipt-not-found' || error.status !== 404) throw error; }
    await this.updateSaved(current => {
      check(samePending(current.pending, pending), 'The pending request changed.'); return current;
    });
    // After an unknown write, a rejected later attempt cannot establish that an
    // earlier in-flight attempt was never accepted. Keep its receipt identity.
    return this.dispatch(pending, true); // User chose Retry; same exact command/body.
  }
  async completeVote(value, pending) {
    check(this.credential?.guestId === pending.guestId);
    validateVoteReceipt(value, this.origin, this.credential.scope, this.revision, pending); this.checkPoll(value.poll);
    await this.updateSaved(current => {
      if (current.pendingVote?.body.requestId !== pending.body.requestId) return current;
      check(sameVote(current.pendingVote, pending), 'The saved vote identity changed.');
      return {...current, pendingVote: null};
    });
    // A state read can advance while this receipt waits on the lock. Never
    // regress its counts, nor show an earlier poll instead of the current one.
    const known = this.polls.get(value.poll.id);
    if (!known || value.poll.revision >= known.revision) {
      this.checkPoll(value.poll); this.polls.set(value.poll.id, value.poll);
      if (this.poll?.id === value.poll.id) {
        const pollTime = Math.max(value.serverAt, this.pollNow());
        this.poll = value.poll; this.pollRefresh = this.clock(); this.pollServerAt = pollTime;
      }
    }
    this.revision = Math.max(this.revision, value.revision); return value;
  }
  async dispatchVote(pending, retry = false) {
    try {
      this.syncSaved(); check(sameVote(this.pendingVote, pending), 'The pending vote changed.');
      const value = await this.request('/votes', pending.body); return this.completeVote(value, pending);
    } catch (error) {
      if (!retry && ['invalid-request','poll-closed','option-unavailable','poll-not-found','night-closed',
        'body-too-large','json-required','rate-limited'].includes(error.code) && [400,404,409,410,413,415,429].includes(error.status)
        && this.pendingVote?.body.requestId === pending.body.requestId && this.credential?.guestId === pending.guestId)
        await this.updateSaved(current => sameVote(current.pendingVote, pending) ? {...current, pendingVote: null} : current);
      throw error;
    }
  }
  async vote(pollId, trackId) {
    const epoch = this.epoch;
    const saved = await this.updateSaved(current => {
      if (current.pendingVote) throw new CrowdError('vote-pending', 'Check the saved vote before choosing another record. It may have been sent in another tab.');
      check(this.canVote() && this.poll.id === pollId, 'Voting is closed or waiting for a room update.');
      const option = this.poll.options.find(option => option.track.id === trackId);
      check(option, 'This exact recording is not an option in this vote.');
      const pendingVote = {scope: current.credential.scope, guestId: current.credential.guestId,
        track: option.track, closesAt: this.poll.closesAt,
        body: {requestId: this.command(), pollId, trackId}};
      return {...current, pendingVote};
    }, this.saved, epoch);
    check(epoch === this.epoch, 'This vote was interrupted.'); return this.dispatchVote(saved.pendingVote);
  }
  async reconcileVote() {
    this.syncSaved(); const pending = this.pendingVote; check(pending, 'There is no vote awaiting confirmation.');
    const value = await this.request('/vote-receipts/' + encodeURIComponent(pending.body.requestId));
    return this.completeVote(value, pending);
  }
  async retryVote() {
    this.syncSaved(); const pending = this.pendingVote; check(pending, 'There is no vote to retry.');
    try { return await this.reconcileVote(); }
    catch (error) { if (error.code !== 'receipt-not-found' || error.status !== 404) throw error; }
    await this.updateSaved(current => { check(sameVote(current.pendingVote, pending), 'The pending vote changed.'); return current; });
    return this.dispatchVote(pending, true);
  }
}
