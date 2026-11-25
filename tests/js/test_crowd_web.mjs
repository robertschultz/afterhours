// Pure browser boundary tests: fake fetch/clock/storage, no listener or provider.
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {CrowdClient, CrowdError, validateState, validateSearch, validateReceipt, validatePoll, validateVoteReceipt,
  currentProgram, currentRequest, safeArt, discardInvalidSaved, fallbackSleeve} from '../../src/afterhours/crowd_web/core.mjs';
import {artFor, sleeveCss, DEMO_ART, PALETTE} from '../fixtures/client/art.ts';

const base = 'http://127.0.0.1:19009/r/invitationfixture0001';
const origin = new URL(base).origin;
const scope = {origin, roomId:'room-fixture', nightId:'night-fixture', generation:'generation-fixture'};
const track = n => ({id:'virtualdj:' + String(n).padStart(64, '0'), title:'Fixture record ' + n, artist:'Fixture artist'});
const follow = {status:'configuration-required', foregroundStatus:true};
const token = 'a'.repeat(43);
const common = (revision = 1) => ({version:1, scope:{...scope}, revision, serverAt:1000});
const state = (overrides = {}) => ({...common(), guest:{id:'guest-fixture', expiresAt:87400, openCount:0, openLimit:3},
  capabilities:{search:true, ask:true, status:true, poll:false, liveActivity:false}, follow:{...follow},
  night:{title:'Fixture night'}, performer:{id:'nova', name:'Nova'}, program:null, requests:[], ...overrides});
const search = (query = 'Fixture', items = [track(1),track(2)]) => ({...common(), query, searchId:'search-fixture',
  expiresAt:1120, scopeLabel:'known-crate', records:items});
const row = (overrides = {}) => ({id:'request-fixture', revision:2, track:track(1), asks:1, firstAt:1000,
  stage:'sent', followed:false, playingNow:false, ...overrides});
const accepted = (body, overrides = {}) => ({...common(2), follow:{...follow},
  receipt:{id:body.requestId, status:'accepted', scope:{...scope}, change:{trackId:body.trackId, follow:body.follow},
    requestId:'request-fixture', revision:2, duplicate:false}, request:row(), ...overrides});
const poll = (overrides = {}) => ({id:'poll-fixture', revision:1, status:'open', openedAt:1000, closesAt:1060,
  options:[1,2,3].map(n => ({track:track(n),votes:0})), totalVotes:0, ...overrides});
const pollState = (overrides = {}) => state({capabilities:{...state().capabilities,poll:true},poll:poll(),...overrides});
const voteAccepted = (body, overrides = {}) => ({...common(2), receipt:{id:body.requestId,status:'accepted',scope:{...scope},
  change:{pollId:body.pollId,trackId:body.trackId},revision:2,pollRevision:2,duplicate:false},
  poll:poll({id:body.pollId,revision:2,options:[1,2,3].map(n=>({track:track(n),votes:track(n).id===body.trackId?1:0})),
    totalVotes:1,myChoiceId:body.trackId}),...overrides});
const clone = value => structuredClone(value);
const json = (value, status = 200) => new Response(JSON.stringify(value), {status, headers:{'Content-Type':'application/json'}});
const rejection = (code, status) => json({version:1, scope, code, message:'Fixture rejection.'}, status);
class MemoryStorage {
  data = new Map(); failing = false;
  getItem(key) { return this.data.get(key) ?? null; }
  setItem(key, value) { if (this.failing) throw Error('Storage denied'); this.data.set(key, value); }
  removeItem(key) { if (this.failing) throw Error('Storage denied'); this.data.delete(key); }
}
const lockTails = new Map();
function testLock(name, action) {
  const next = (lockTails.get(name) || Promise.resolve()).catch(() => {}).then(action);
  lockTails.set(name, next); return next;
}
function harness({storage = new MemoryStorage(), command, clock, lock = testLock} = {}) {
  const calls = [], queue = []; let counter = 0;
  const fetcher = async (url, options) => {
    const call = {url, ...options, payload:options.body ? JSON.parse(options.body) : undefined}; calls.push(call);
    assert.ok(queue.length, 'Unexpected request: ' + url);
    const next = queue.shift(); return typeof next === 'function' ? next(call) : next;
  };
  const client = new CrowdClient(base, {fetcher, storage, clock:clock || (() => 1000000),
    command:command || (() => 'command-fixture-' + ++counter), timeout:100, lock});
  return {client, queue, calls, storage, fetcher};
}
async function ready(h) {
  h.queue.push(json(state({guestToken:token}))); await h.client.join();
  h.queue.push(json(search())); await h.client.search('Fixture');
  return h.client.selection(track(1).id);
}
async function readyPoll(h, overrides = {}) {
  h.queue.push(json(pollState({guestToken:token,...overrides}))); await h.client.join();
  return h.client.poll;
}
function unknown() { throw Error('Connection lost after the server may have accepted.'); }

test('the browser fetch default retains its required Window receiver', async () => {
  const original = globalThis.fetch; let calls = 0;
  globalThis.fetch = async function(url, options) {
    assert.equal(this, globalThis); calls++;
    assert.equal(url, base + '/v1/session'); assert.equal(options.method, 'POST');
    return json(state({guestToken:token}));
  };
  try {
    const client = new CrowdClient(base, {storage:new MemoryStorage(), clock:() => 1000000,
      command:() => 'browser-receiver-01', lock:testLock});
    await client.join();
    assert.equal(calls, 1); assert.equal(client.state.guest.id, 'guest-fixture');
  } finally { globalThis.fetch = original; }
});

test('actual disposable HTTP envelopes decode with their exact identities', async () => {
  const fixture = JSON.parse(await readFile(new URL('../fixtures/client/guest-http-contract-fixture.json', import.meta.url)));
  const o = fixture.scope.origin;
  for (const key of ['bootstrap','resumed','sent','listening','coming','playing','declined'])
    assert.equal(validateState(fixture[key], o, fixture.scope).scope.nightId, fixture.scope.nightId);
  validateSearch(fixture.search, o, fixture.scope, 0, fixture.search.query);
  for (const key of ['accepted','lookup','duplicate','replayedAfterExpiry'])
    validateReceipt(fixture[key], o, fixture.scope, 0, {body:fixture.sendBody});
  assert.equal(fixture.actions.acceptedTrackId, fixture.sendBody.trackId);
  assert.notEqual(fixture.actions.acceptedTrackId, fixture.actions.declinedTrackId);
});
test('normal admission is persisted before sending and receipt clears only its matching pending identity', async () => {
  const h = harness(), choice = await ready(h);
  h.queue.push(call => {
    assert.deepEqual(JSON.parse(h.storage.getItem(h.client.key)).pending.body, call.payload);
    assert.equal(call.headers.Authorization, 'Bearer ' + token);
    assert.equal(call.credentials, 'omit'); assert.equal(call.redirect, 'error'); assert.equal(call.referrerPolicy, 'no-referrer');
    assert.equal(call.url, base + '/v1/requests'); return json(accepted(call.payload));
  });
  const result = await h.client.send(choice);
  assert.equal(result.request.track.id, track(1).id); assert.equal(h.client.pending, null);
  assert.equal(JSON.parse(h.storage.getItem(h.client.key)).pending, null);
});
test('blocked local storage sends no admission or new bootstrap', async () => {
  const h = harness(), choice = await ready(h); h.storage.failing = true;
  await assert.rejects(h.client.send(choice), {code:'storage-unavailable'});
  assert.equal(h.calls.length, 2); assert.equal(h.client.pending, null);
  const isolated = harness(); isolated.storage.failing = true;
  await assert.rejects(isolated.client.join(), {code:'storage-unavailable'}); assert.equal(isolated.calls.length, 0);
});
test('unknown admission survives reload and receipt lookup resolves without a second POST', async () => {
  const h = harness(), choice = await ready(h); h.queue.push(unknown);
  await assert.rejects(h.client.send(choice), {code:'connection-unknown'}); const saved = clone(h.client.pending);
  const resumed = harness({storage:h.storage});
  resumed.queue.push(json(state())); await resumed.client.join();
  assert.equal(resumed.calls[0].headers.Authorization, 'Bearer ' + token);
  resumed.queue.push(json(accepted(saved.body))); await resumed.client.reconcile();
  assert.equal(resumed.client.pending, null);
  assert.deepEqual(resumed.calls.map(c => c.method), ['POST','GET']);
  assert.ok(resumed.calls[1].url.endsWith('/receipts/' + saved.body.requestId));
});
test('unknown receipt stays pending and explicit retry first looks up then sends the identical body', async () => {
  const h = harness(), choice = await ready(h); h.queue.push(unknown);
  await assert.rejects(h.client.send(choice)); const pending = clone(h.client.pending);
  h.queue.push(rejection('receipt-not-found', 404)); await assert.rejects(h.client.reconcile(), {code:'receipt-not-found'});
  assert.deepEqual(h.client.pending, pending);
  h.queue.push(rejection('receipt-not-found',404), call => json(accepted(call.payload)));
  await h.client.retry(); assert.deepEqual(h.calls.at(-1).payload, pending.body);
  assert.equal(h.calls.at(-2).method, 'GET'); assert.equal(h.client.pending, null);
});
test('a failed explicit retry never discards the earlier unknown write identity', async () => {
  for (const [code,status] of [['rate-limited',429],['selection-expired',410],['guest-expired',401],['temporarily-unavailable',503]]) {
    const h = harness(), choice = await ready(h); h.queue.push(unknown); await assert.rejects(h.client.send(choice));
    const pending = clone(h.client.pending); h.queue.push(rejection('receipt-not-found',404), rejection(code,status));
    await assert.rejects(h.client.retry(), {code}); assert.deepEqual(h.client.pending, pending);
  }
});
test('an original definitive expiry keeps the exact chosen record available for explicit refresh', async () => {
  const h = harness(), choice = await ready(h); h.queue.push(rejection('selection-expired',410));
  await assert.rejects(h.client.send(choice), {code:'selection-expired'}); assert.equal(h.client.pending, null);
  assert.equal(choice.track.id, track(1).id);
  h.queue.push(json({...search(),searchId:'refreshed-exact'})); const refreshed = await h.client.refreshSelection(choice);
  assert.equal(refreshed.track.id, choice.track.id); assert.equal(refreshed.searchId, 'refreshed-exact');
  h.queue.push(json(search('Fixture',[track(2)])));
  await assert.rejects(h.client.refreshSelection(choice), {code:'recording-unavailable'});
  assert.equal(choice.track.id, track(1).id);
});
test('wrong receipt scope, recording, command or revision cannot clear an unknown admission', async () => {
  for (const mutate of [v => v.scope.nightId = 'other', v => v.request.track.id = track(2).id,
    v => v.receipt.id = 'different-command', v => v.revision = 0, v => v.receipt.change.follow = true]) {
    const h = harness(), choice = await ready(h); h.queue.push(unknown); await assert.rejects(h.client.send(choice));
    const pending = clone(h.client.pending), result = accepted(pending.body); mutate(result); h.queue.push(json(result));
    await assert.rejects(h.client.reconcile(), {code:'invalid-response'}); assert.deepEqual(h.client.pending, pending);
  }
});
test('pending command conflicts and invalid credentials never discard or replace a guest automatically', async () => {
  const h = harness(), choice = await ready(h); h.queue.push(rejection('command-conflict',409));
  await assert.rejects(h.client.send(choice), {code:'command-conflict'}); assert.ok(h.client.pending);
  const before = clone(h.client.saved); h.queue.push(rejection('guest-expired',401));
  await assert.rejects(h.client.join(), {code:'guest-expired'}); assert.deepEqual(h.client.saved, before);
  assert.ok(h.calls.at(-1).headers.Authorization); assert.equal(h.calls.length, 4);
});
test('three open requests prevent a new local send', async () => {
  const h = harness(), choice = await ready(h); h.client.state.guest.openCount = 3;
  await assert.rejects(h.client.send(choice)); assert.equal(h.calls.length, 2); assert.equal(h.client.pending, null);
});
test('pending request binds both the guest and invitation scope in local storage', async () => {
  const h = harness(), choice = await ready(h); h.queue.push(unknown); await assert.rejects(h.client.send(choice));
  const saved = clone(h.client.saved); saved.credential.guestId = 'different-guest'; h.storage.setItem(h.client.key, JSON.stringify(saved));
  assert.throws(() => harness({storage:h.storage}), {code:'invalid-response'});
  const different = new CrowdClient(base.replace('0001','0002'), {storage:h.storage}); assert.equal(different.credential, undefined);
});
test('new guest state cannot be cleared by a late old admission response', async () => {
  const h = harness(), choice = await ready(h); let resolve, started;
  const began = new Promise(r => started = r);
  h.queue.push(call => { started(call.payload); return new Promise(r => resolve = r); });
  const sending = h.client.send(choice); const oldBody = await began;
  await h.client.newGuest(); h.queue.push(json(state({guestToken:token,guest:{...state().guest,id:'new-guest'}}))); await h.client.join();
  resolve(json(accepted(oldBody))); await assert.rejects(sending);
  assert.equal(h.client.credential.guestId, 'new-guest'); assert.equal(h.client.pending, null);
});
test('out-of-order search cannot overwrite the currently displayed selection binding', async () => {
  const h = harness(); await ready(h); let resolve;
  h.queue.push(() => new Promise(r => resolve = r)); const first = h.client.search('First');
  h.queue.push(json({...search('Second',[track(2)]),searchId:'search-second'})); await h.client.search('Second');
  resolve(json(search('First'))); await assert.rejects(first, {code:'invalid-response'});
  assert.equal(h.client.searchResult.searchId, 'search-second'); assert.equal(h.client.selection(track(2).id).query, 'Second');
});
test('interrupted and stale state responses never replace newer state', async () => {
  const h = harness(); await ready(h); let resolve;
  h.queue.push(() => new Promise(r => resolve = r)); const loading = h.client.refresh(); h.client.suspend();
  resolve(json(state({revision:3}))); await assert.rejects(loading); assert.equal(h.client.state.revision, 1);
  h.queue.push(json(state({revision:0}))); await assert.rejects(h.client.refresh()); assert.equal(h.client.revision, 1);
});
test('strict bounds reject missing/null fields, unknown stages, duplicate rows and unsupported capabilities', () => {
  for (const mutate of [s => s.night = null, s => s.performer.name = '', s => s.capabilities.poll = 'unknown',
    s => s.program = undefined, s => s.requests = [row({stage:'approved'})], s => s.requests = [row(),row()],
    s => s.requests = [row({stage:'playing'})], s => s.requests = [row({at:1001})], s => s.guest.openCount = 4,
    s => s.night.title = null]) {
    const s = state({revision:2}); mutate(s); assert.throws(() => validateState(s,origin,scope));
  }
  assert.throws(() => validateSearch({...search(),records:Array.from({length:6},(_,i)=>track(i))},origin,scope,0,'Fixture'));
});
test('streamed response cap cancels oversized streams without accepting credentials', async () => {
  const h = harness(); let cancelled = false;
  h.queue.push(new Response(new ReadableStream({pull(controller) { controller.enqueue(new Uint8Array(70000)); },
    cancel() { cancelled = true; }}), {headers:{'Content-Type':'application/json'}}));
  await assert.rejects(h.client.join(), {code:'invalid-response'}); assert.equal(cancelled, true); assert.equal(h.client.credential, undefined);
});
test('malformed and non-JSON write responses remain unknown with the exact command saved', async () => {
  for (const response of [new Response('not json',{headers:{'Content-Type':'application/json'}}),
    new Response('<html>wrong origin</html>',{headers:{'Content-Type':'text/html'}})]) {
    const h = harness(), choice = await ready(h); h.queue.push(response); await assert.rejects(h.client.send(choice));
    assert.equal(h.client.pending.body.trackId, track(1).id);
  }
});
test('playback and promised time expire together while historical stages remain', () => {
  const p = {track:track(1),playing:true,sampledAt:1000}; const s = state({program:p,next:{track:track(2),estimatedAt:1010}});
  assert.equal(currentProgram(s,1002.9),p); assert.equal(currentProgram(s,1003),null);
  assert.equal(currentRequest(row({stage:'playing',playedAt:999,playingNow:true}),p,1001).playingNow,true);
  const stale = currentRequest(row({stage:'playing',playedAt:999,playingNow:true}),null,1003);
  assert.equal(stale.stage,'playing'); assert.equal(stale.playingNow,false);
  const incoming = row({track:track(2),stage:'coming',at:1010});
  assert.equal(currentRequest(incoming,p,1001,s.next).at,1010);
  assert.equal(currentRequest(incoming,null,1003,s.next).at,undefined);
  assert.equal(currentRequest(incoming,p,1011,s.next).at,undefined);
  assert.equal(currentRequest(incoming,p,1001,{track:track(1),estimatedAt:1010}).at,undefined);
});
test('invalid invitation URLs and unsafe external artwork never pass the boundary', () => {
  for (const url of ['https://other.test/api/config',base+'?token=private',base+'#fragment',base.replace('127.0.0.1','user:secret@127.0.0.1'), 'file:///r/'+'a'.repeat(20)])
    assert.throws(() => new CrowdClient(url, {storage:new MemoryStorage()}));
  for (const url of ['javascript:alert(1)','data:image/png,a','https://user:secret@art.test/a','https://art.test/a#token']) assert.equal(safeArt(url),undefined);
  assert.equal(safeArt('https://art.test/record.jpg'),'https://art.test/record.jpg');
});

test('a stale tab resume merges the durable pending command saved while its response was in flight', async () => {
  const a = harness(), choice = await ready(a), b = harness({storage:a.storage});
  let reply, began; const started = new Promise(r => began = r);
  b.queue.push(() => { began(); return new Promise(r => reply = r); }); const joining = b.client.join(); await started;
  a.queue.push(unknown); await assert.rejects(a.client.send(choice)); const pending = clone(a.client.pending);
  reply(json(state())); await joining;
  assert.deepEqual(b.client.pending, pending);
  assert.deepEqual(JSON.parse(a.storage.getItem(a.client.key)).pending, pending);
  const reloaded = harness({storage:a.storage}); assert.deepEqual(reloaded.client.pending, pending);
  assert.equal(b.calls.filter(call => call.url.endsWith('/requests')).length, 0);
});

test('concurrent tabs serialize admission and retain exactly one unknown command', async () => {
  let aId = 0, bId = 0;
  const a = harness({command:() => 'tab-a-command-' + ++aId}), choiceA = await ready(a);
  const b = harness({storage:a.storage, command:() => 'tab-b-command-' + ++bId});
  b.queue.push(json(state())); await b.client.join(); b.queue.push(json(search())); await b.client.search('Fixture');
  const choiceB = b.client.selection(track(2).id);
  a.queue.push(unknown); b.queue.push(unknown);
  const results = await Promise.allSettled([a.client.send(choiceA), b.client.send(choiceB)]);
  assert.deepEqual(results.map(r => r.reason.code).sort(), ['connection-unknown','request-pending']);
  const sends = [...a.calls,...b.calls].filter(call => call.url.endsWith('/requests'));
  assert.equal(sends.length, 1);
  const reloaded = harness({storage:a.storage});
  assert.equal(reloaded.client.pending.body.requestId, sends[0].payload.requestId);
  assert.equal(reloaded.client.pending.body.trackId, sends[0].payload.trackId);
});

test('a late old-tab join cannot restore the guest replaced explicitly in another tab', async () => {
  const a = harness(); await ready(a); const b = harness({storage:a.storage});
  let reply, began; const started = new Promise(r => began = r);
  b.queue.push(() => { began(); return new Promise(r => reply = r); }); const joining = b.client.join(); await started;
  await a.client.newGuest();
  a.queue.push(json(state({guestToken:'b'.repeat(43),guest:{...state().guest,id:'replacement-guest'}}))); await a.client.join();
  const current = clone(a.client.saved); reply(json(state()));
  await assert.rejects(joining, {code:'session-changed'});
  assert.deepEqual(JSON.parse(a.storage.getItem(a.client.key)), current);
  assert.equal(harness({storage:a.storage}).client.credential.guestId, 'replacement-guest');
});

test('a late old-tab admission cannot write across a changed guest owner', async () => {
  const a = harness(), choice = await ready(a), b = harness({storage:a.storage});
  let reply, began; const started = new Promise(r => began = r);
  a.queue.push(call => { began(call.payload); return new Promise(r => reply = r); });
  const sending = a.client.send(choice), body = await started;
  await b.client.newGuest(); b.queue.push(json(state({guestToken:'b'.repeat(43),guest:{...state().guest,id:'replacement-guest'}}))); await b.client.join();
  const current = clone(b.client.saved); reply(json(accepted(body)));
  await assert.rejects(sending, {code:'session-changed'});
  assert.deepEqual(JSON.parse(a.storage.getItem(a.client.key)), current);
});

test('a late accepted response cannot erase a newer pending command from another tab', async () => {
  const a = harness(), choice = await ready(a); let reply, began;
  const started = new Promise(r => began = r);
  a.queue.push(call => { began(call.payload); return new Promise(r => reply = r); });
  const sending = a.client.send(choice), body = await started;
  const b = harness({storage:a.storage, command:() => 'tab-b-later-command'});
  b.queue.push(json(state())); await b.client.join(); b.queue.push(json(accepted(body))); await b.client.reconcile();
  b.queue.push(json({...search(),revision:2})); await b.client.search('Fixture');
  b.queue.push(unknown); await assert.rejects(b.client.send(b.client.selection(track(2).id)));
  const pending = clone(b.client.pending); reply(json(accepted(body))); await sending;
  assert.deepEqual(JSON.parse(a.storage.getItem(a.client.key)).pending, pending);
  assert.deepEqual(a.client.pending, pending);
});

test('a receipt waiting on storage cannot lower the revision advanced by a concurrent refresh', async () => {
  const h = harness(), choice = await ready(h);
  h.queue.push(unknown); await assert.rejects(h.client.send(choice));
  const pending = clone(h.client.pending); let release, entered;
  const waiting = new Promise(resolve => entered = resolve);
  h.client.lock = (_key, action) => new Promise((resolve, reject) => {
    release = () => { try { resolve(action()); } catch (error) { reject(error); } }; entered();
  });
  h.queue.push(json(accepted(pending.body))); const reconciling = h.client.reconcile(); await waiting;
  const newer = state({revision:3, guest:{...state().guest,openCount:1},
    requests:[row({revision:3,stage:'listening'})]});
  h.queue.push(json(newer)); await h.client.refresh();
  release(); const receipt = await reconciling;
  assert.equal(receipt.revision,2); assert.equal(h.client.revision,3);
  assert.deepEqual(h.client.state,newer); assert.equal(h.client.pending,null);
  assert.equal(JSON.parse(h.storage.getItem(h.client.key)).pending,null);
  h.queue.push(json(state({revision:2})));
  await assert.rejects(h.client.refresh(),{code:'invalid-response'});
  assert.deepEqual(h.client.state,newer);
});

test('storage change and read completions detect a replaced guest without silently adopting it', async () => {
  const a = harness(); await ready(a); const b = harness({storage:a.storage});
  let reply; b.queue.push(() => new Promise(r => reply = r)); const refreshing = b.client.refresh();
  await a.client.newGuest(); a.queue.push(json(state({guestToken:'b'.repeat(43),guest:{...state().guest,id:'replacement-guest'}}))); await a.client.join();
  reply(json(state())); await assert.rejects(refreshing, {code:'session-changed'});
  assert.throws(() => b.client.storageChanged(), {code:'session-changed'}); assert.equal(b.client.state,null);
  const calls = b.calls.length; await assert.rejects(b.client.refresh(), {code:'session-changed'}); assert.equal(b.calls.length,calls);
});

test('suspending while a storage lock is queued prevents admission or a persisted command', async () => {
  const h = harness(), choice = await ready(h); let enter;
  h.client.lock = (_key, action) => new Promise((resolve, reject) => { enter = () => { try { resolve(action()); } catch (e) { reject(e); } }; });
  const sending = h.client.send(choice); h.client.suspend(); enter(); await assert.rejects(sending);
  assert.equal(h.client.pending,null); assert.equal(h.calls.length,2);
});

test('unavailable browser locks fail before bootstrap and never use an advisory storage fallback', async () => {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis,'navigator');
  Object.defineProperty(globalThis,'navigator',{value:{},configurable:true});
  try {
    const storage = new MemoryStorage(); let calls = 0;
    const client = new CrowdClient(base,{storage,fetcher:async()=>{calls++;return json(state({guestToken:token}));}});
    await assert.rejects(client.join(),{code:'storage-lock-unavailable'});
    assert.equal(calls,0); assert.equal(storage.data.size,0);
    storage.setItem(client.key,'{"invalid":true}');
    await assert.rejects(discardInvalidSaved(client.key,'{"invalid":true}',{storage}),{code:'storage-lock-unavailable'});
    assert.equal(storage.getItem(client.key),'{"invalid":true}');
  } finally { if (descriptor) Object.defineProperty(globalThis,'navigator',descriptor); else delete globalThis.navigator; }
});

test('explicit invalid-storage reset is locked and cannot delete a newer session', async () => {
  const storage = new MemoryStorage(), key = 'afterhours.engine.crowd.v1:' + base;
  storage.setItem(key,'{"credential":null,"pending":{}}');
  let invalid; try { harness({storage}); } catch(error) { invalid = error; }
  assert.equal(invalid.code,'invalid-response');
  storage.setItem(key,'{"bootstrap":"new-owner-command"}');
  await assert.rejects(discardInvalidSaved(key,invalid.invalidSavedValue,{storage,lock:testLock}),{code:'session-changed'});
  assert.equal(JSON.parse(storage.getItem(key)).bootstrap,'new-owner-command');
  await discardInvalidSaved(key,storage.getItem(key),{storage,lock:testLock}); assert.equal(storage.getItem(key),null);
});

test('actual disposable HTTP poll and immutable vote receipt envelopes decode without substitutions', async () => {
  const fixture = JSON.parse(await readFile(new URL('../fixtures/client/guest-poll-http-contract-fixture.json', import.meta.url)));
  for (const key of ['bootstrap','open','finalState','secondOpen','interruptedState'])
    validateState(fixture[key],fixture.scope.origin,fixture.scope);
  const pending = body => ({body,closesAt:fixture.open.poll.closesAt,
    track:fixture.open.poll.options.find(option=>option.track.id===body.trackId).track});
  for (const key of ['accepted','oldReceipt','replayed','replayedAfterDeadline'])
    validateVoteReceipt(fixture[key],fixture.scope.origin,fixture.scope,0,pending(fixture.voteBody));
  for (const key of ['moved','finalReceipt'])
    validateVoteReceipt(fixture[key],fixture.scope.origin,fixture.scope,0,pending(fixture.movedBody));
  assert.notEqual(fixture.oldReceipt.receipt.change.trackId,fixture.oldReceipt.poll.myChoiceId);
  assert.equal(fixture.finalState.poll.chosenTrackId,fixture.actions.movedChoiceTrackId);
});

test('poll bounds, exact identities, totals, deadline and chosen outcomes are validated', () => {
  validatePoll(poll(),1); validateState(state({capabilities:{...state().capabilities,poll:true}}),origin,scope);
  for (const mutate of [p=>p.status='closing',p=>p.id='',p=>p.revision=2,p=>p.closesAt=1061,
    p=>p.options=[p.options[0]],p=>p.options.push({track:track(4),votes:0}),p=>p.options[1]=p.options[0],
    p=>p.options[0].votes=-1,p=>p.totalVotes=1,p=>{p.options[0].votes=1025;p.totalVotes=1025;},
    p=>p.myChoiceId=track(4).id,p=>p.chosenTrackId=track(1).id,p=>p.status='applied',p=>p.slotAt=null]) {
    const value=poll(); mutate(value); assert.throws(()=>validatePoll(value,1));
  }
  assert.throws(()=>validateState(state({poll:poll()}),origin,scope));
  assert.throws(()=>validateState(pollState({serverAt:997}),origin,scope));
  assert.throws(()=>validateState(pollState({capabilities:{...state().capabilities,poll:true,liveActivity:true}}),origin,scope));
  for (const status of ['applied','kept']) validatePoll(poll({status,chosenTrackId:track(1).id}),1);
  for (const status of ['cancelled','interrupted']) validatePoll(poll({status}),1);
});

test('voting persists its exact body before sending and never optimistically selects or consumes an ASK slot', async () => {
  const h=harness(); await readyPoll(h,{guest:{...state().guest,openCount:3}}); const before=clone(h.client.poll);
  let reply,began; const started=new Promise(resolve=>began=resolve);
  h.queue.push(call=>{assert.deepEqual(JSON.parse(h.storage.getItem(h.client.key)).pendingVote.body,call.payload);
    began(call);return new Promise(resolve=>reply=resolve);});
  const voting=h.client.vote(before.id,track(2).id), call=await started;
  assert.equal(call.url,base+'/v1/votes'); assert.deepEqual(Object.keys(call.payload).sort(),['pollId','requestId','trackId']);
  assert.deepEqual(h.client.poll,before); assert.equal(h.client.pending,null); assert.equal(h.client.canVote(),false);
  reply(json(voteAccepted(call.payload))); await voting;
  assert.equal(h.client.pendingVote,null); assert.equal(h.client.poll.myChoiceId,track(2).id);
  assert.equal(h.client.state.guest.openCount,3); assert.equal(h.calls.filter(c=>c.url.endsWith('/requests')).length,0);
});

test('pending votes and ASK commands survive independently and neither receipt erases the other', async () => {
  const h=harness(); await readyPoll(h); h.queue.push(json(search())); await h.client.search('Fixture');
  h.queue.push(unknown); await assert.rejects(h.client.send(h.client.selection(track(1).id)));
  const ask=clone(h.client.pending);
  h.queue.push(unknown); await assert.rejects(h.client.vote(h.client.poll.id,track(2).id));
  const vote=clone(h.client.pendingVote); assert.notEqual(vote.body.requestId,ask.body.requestId);
  const resumed=harness({storage:h.storage}); resumed.queue.push(json(pollState())); await resumed.client.join();
  assert.deepEqual(resumed.client.pending,ask); assert.deepEqual(resumed.client.pendingVote,vote);
  resumed.queue.push(json(voteAccepted(vote.body))); await resumed.client.reconcileVote();
  assert.deepEqual(resumed.client.pending,ask); assert.equal(resumed.client.pendingVote,null);
  resumed.queue.push(json(accepted(ask.body))); await resumed.client.reconcile();
  assert.equal(resumed.client.pending,null); assert.equal(resumed.client.pendingVote,null);
});

test('unknown vote survives reload; receipt recovery never resends and uses confirmed current choice', async () => {
  const h=harness(); await readyPoll(h); h.queue.push(unknown); await assert.rejects(h.client.vote(h.client.poll.id,track(1).id));
  const saved=clone(h.client.pendingVote), resumed=harness({storage:h.storage});
  resumed.queue.push(json(pollState())); await resumed.client.join();
  const result=voteAccepted(saved.body,{revision:3,poll:poll({revision:3,totalVotes:1,myChoiceId:track(2).id,
    options:[{track:track(1),votes:0},{track:track(2),votes:1},{track:track(3),votes:0}]})});
  resumed.queue.push(json(result)); await resumed.client.reconcileVote();
  assert.equal(resumed.client.pendingVote,null); assert.equal(resumed.client.poll.myChoiceId,track(2).id);
  assert.deepEqual(resumed.calls.map(c=>c.method),['POST','GET']);
  assert.ok(resumed.calls[1].url.endsWith('/vote-receipts/'+saved.body.requestId));
});

test('explicit vote retry looks up first and repeats only the saved body', async () => {
  const h=harness(); await readyPoll(h); h.queue.push(unknown); await assert.rejects(h.client.vote(h.client.poll.id,track(2).id));
  const pending=clone(h.client.pendingVote); h.queue.push(rejection('receipt-not-found',404));
  await assert.rejects(h.client.reconcileVote(),{code:'receipt-not-found'}); assert.deepEqual(h.client.pendingVote,pending);
  h.queue.push(rejection('receipt-not-found',404),call=>json(voteAccepted(call.payload))); await h.client.retryVote();
  assert.equal(h.calls.at(-2).method,'GET'); assert.deepEqual(h.calls.at(-1).payload,pending.body);
  assert.equal(h.client.pendingVote,null);
});

test('missing or wrong scoped absent-receipt errors cannot authorize a vote retry', async () => {
  for (const badScope of [undefined,{...scope,nightId:'another-night'}]) {
    const h=harness(); await readyPoll(h); h.queue.push(unknown); await assert.rejects(h.client.vote(h.client.poll.id,track(1).id));
    const pending=clone(h.client.pendingVote), calls=h.calls.length;
    h.queue.push(json({version:1,code:'receipt-not-found',message:'Missing.',...(badScope?{scope:badScope}:{})},404));
    await assert.rejects(h.client.retryVote(),{code:'invalid-response'});
    assert.equal(h.calls.length,calls+1); assert.equal(h.calls.at(-1).method,'GET'); assert.deepEqual(h.client.pendingVote,pending);
  }
});

test('original definitive vote rejection clears only that vote; later retry rejection preserves unknown identity', async () => {
  for (const [code,status] of [['poll-closed',409],['option-unavailable',409],['poll-not-found',404],['rate-limited',429]]) {
    const h=harness(); await readyPoll(h); h.queue.push(rejection(code,status));
    await assert.rejects(h.client.vote(h.client.poll.id,track(1).id),{code}); assert.equal(h.client.pendingVote,null);
    h.queue.push(unknown); await assert.rejects(h.client.vote(h.client.poll.id,track(1).id)); const pending=clone(h.client.pendingVote);
    h.queue.push(rejection('receipt-not-found',404),rejection(code,status));
    await assert.rejects(h.client.retryVote(),{code}); assert.deepEqual(h.client.pendingVote,pending);
  }
});

test('malformed, wrong-identity or stale vote receipts never clear an unknown command', async () => {
  for (const mutate of [v=>v.scope.nightId='other',v=>v.receipt.id='another-command',v=>v.receipt.change.trackId=track(2).id,
    v=>v.receipt.change.pollId='other-poll',v=>v.receipt.pollRevision=3,v=>v.poll.closesAt=1061,
    v=>v.poll.options[0].track.title='Substituted record',v=>v.revision=0]) {
    const h=harness(); await readyPoll(h); h.queue.push(unknown); await assert.rejects(h.client.vote(h.client.poll.id,track(1).id));
    const pending=clone(h.client.pendingVote), result=voteAccepted(pending.body); mutate(result); h.queue.push(json(result));
    await assert.rejects(h.client.reconcileVote(),{code:'invalid-response'}); assert.deepEqual(h.client.pendingVote,pending);
  }
});

test('voting closes at the actual deadline and becomes unavailable when its confirmed snapshot is stale', async () => {
  let now=1000000; const h=harness({clock:()=>now}); await readyPoll(h); assert.equal(h.client.canVote(),true);
  now=1008000; assert.equal(h.client.canVote(),false); await assert.rejects(h.client.vote(h.client.poll.id,track(1).id));
  h.queue.push(json(pollState({serverAt:1059}))); await h.client.refresh(); assert.equal(h.client.canVote(),true);
  now+=1000; assert.equal(h.client.canVote(),false); await assert.rejects(h.client.vote(h.client.poll.id,track(1).id));
  assert.equal(h.calls.length,2); assert.equal(h.client.pendingVote,null);
});

test('old poll receipts clear their own pending vote without replacing the newer visible poll', async () => {
  const h=harness(); await readyPoll(h); h.queue.push(unknown); await assert.rejects(h.client.vote(h.client.poll.id,track(1).id));
  const pending=clone(h.client.pendingVote), next=poll({id:'next-poll',revision:4,options:[{track:track(4),votes:0},{track:track(5),votes:0}]});
  h.queue.push(json(pollState({revision:4,poll:next}))); await h.client.refresh();
  h.queue.push(json(voteAccepted(pending.body,{revision:4}))); await h.client.reconcileVote();
  assert.deepEqual(h.client.poll,next); assert.equal(h.client.pendingVote,null); assert.equal(h.client.revision,4);
});

test('observed polls preserve ordered recording metadata and never reopen a terminal outcome', async () => {
  for (const mutate of [p=>p.options.reverse(),p=>p.options[0].track.artist='Other artist',p=>p.openedAt=999,
    p=>{p.totalVotes=1;p.options[0].votes=1;}]) {
    const h=harness(); await readyPoll(h); const changed=poll(); mutate(changed);
    h.queue.push(json(pollState({revision:2,poll:changed}))); await assert.rejects(h.client.refresh(),{code:'invalid-response'});
    assert.deepEqual(h.client.poll,poll());
  }
  const h=harness(); await readyPoll(h);
  h.queue.push(json(pollState({revision:2,poll:poll({revision:2,status:'kept',chosenTrackId:track(1).id})}))); await h.client.refresh();
  h.queue.push(json(pollState({revision:3,poll:poll({revision:3})})));
  await assert.rejects(h.client.refresh(),{code:'invalid-response'}); assert.equal(h.client.poll.status,'kept');
});

test('a delayed vote receipt cannot regress counts or revision advanced while its storage lock waits', async () => {
  const h=harness(); await readyPoll(h); h.queue.push(unknown); await assert.rejects(h.client.vote(h.client.poll.id,track(1).id));
  const pending=clone(h.client.pendingVote); let entered,release; const waiting=new Promise(resolve=>entered=resolve);
  h.client.lock=(_key,action)=>new Promise((resolve,reject)=>{release=()=>{try{resolve(action());}catch(e){reject(e);}};entered();});
  h.queue.push(json(voteAccepted(pending.body))); const resolving=h.client.reconcileVote(); await waiting;
  const newer=poll({revision:3,totalVotes:2,myChoiceId:track(2).id,
    options:[{track:track(1),votes:0},{track:track(2),votes:2},{track:track(3),votes:0}]});
  h.queue.push(json(pollState({revision:3,poll:newer}))); await h.client.refresh(); release(); await resolving;
  assert.deepEqual(h.client.poll,newer); assert.equal(h.client.revision,3); assert.equal(h.client.pendingVote,null);
});

test('simultaneous votes in two tabs serialize to one unknown admission', async () => {
  let aId=0,bId=0;
  const a=harness({command:()=> 'vote-tab-a-'+ ++aId}); await readyPoll(a);
  const b=harness({storage:a.storage,command:()=> 'vote-tab-b-'+ ++bId}); b.queue.push(json(pollState())); await b.client.join();
  a.queue.push(unknown); b.queue.push(unknown);
  const results=await Promise.allSettled([a.client.vote(a.client.poll.id,track(1).id),b.client.vote(b.client.poll.id,track(2).id)]);
  assert.deepEqual(results.map(r=>r.reason.code).sort(),['connection-unknown','vote-pending']);
  const sends=[...a.calls,...b.calls].filter(c=>c.url.endsWith('/votes')); assert.equal(sends.length,1);
  assert.deepEqual(harness({storage:a.storage}).client.pendingVote.body,sends[0].payload);
});

test('late old-guest vote receipt cannot change the replacement guest or its pending vote', async () => {
  const a=harness(); await readyPoll(a); let reply,began; const started=new Promise(resolve=>began=resolve);
  a.queue.push(call=>{began(call.payload);return new Promise(resolve=>reply=resolve);});
  const voting=a.client.vote(a.client.poll.id,track(1).id), body=await started;
  const b=harness({storage:a.storage,command:()=> 'replacement-vote-command'}); await b.client.newGuest();
  b.queue.push(json(pollState({guestToken:'b'.repeat(43),guest:{...state().guest,id:'replacement-guest'}}))); await b.client.join();
  b.queue.push(unknown); await assert.rejects(b.client.vote(b.client.poll.id,track(2).id));
  const saved=clone(b.client.saved); reply(json(voteAccepted(body))); await assert.rejects(voting,{code:'session-changed'});
  assert.deepEqual(JSON.parse(a.storage.getItem(a.client.key)),saved);
});

test('a stale tab resume preserves a vote saved by another tab while its session response was delayed', async () => {
  const a=harness(); await readyPoll(a); const b=harness({storage:a.storage});
  let reply,began; const started=new Promise(resolve=>began=resolve);
  b.queue.push(()=>{began();return new Promise(resolve=>reply=resolve);}); const joining=b.client.join(); await started;
  a.queue.push(unknown); await assert.rejects(a.client.vote(a.client.poll.id,track(1).id)); const pending=clone(a.client.pendingVote);
  reply(json(pollState())); await joining;
  assert.deepEqual(b.client.pendingVote,pending); assert.deepEqual(harness({storage:a.storage}).client.pendingVote,pending);
});

test('concurrent ASK and vote commands in separate tabs retain both identities under the shared lock', async () => {
  let aId=0,bId=0;
  const a=harness({command:()=> 'ask-tab-a-'+ ++aId}); await readyPoll(a); a.queue.push(json(search())); await a.client.search('Fixture');
  const b=harness({storage:a.storage,command:()=> 'vote-tab-b-'+ ++bId}); b.queue.push(json(pollState())); await b.client.join();
  a.queue.push(unknown); b.queue.push(unknown);
  const results=await Promise.allSettled([a.client.send(a.client.selection(track(1).id)),b.client.vote(b.client.poll.id,track(2).id)]);
  assert.ok(results.every(result=>result.reason.code==='connection-unknown'));
  const reloaded=harness({storage:a.storage});
  assert.deepEqual(reloaded.client.pending.body,a.calls.at(-1).payload);
  assert.deepEqual(reloaded.client.pendingVote.body,b.calls.at(-1).payload);
});

test('a late accepted vote receipt never clears a newer pending vote from another tab', async () => {
  const a=harness(); await readyPoll(a); let reply,began; const started=new Promise(resolve=>began=resolve);
  a.queue.push(call=>{began(call.payload);return new Promise(resolve=>reply=resolve);});
  const voting=a.client.vote(a.client.poll.id,track(1).id), body=await started;
  const b=harness({storage:a.storage,command:()=> 'later-tab-vote-command'}); b.queue.push(json(pollState())); await b.client.join();
  b.queue.push(json(voteAccepted(body))); await b.client.reconcileVote();
  b.queue.push(unknown); await assert.rejects(b.client.vote(b.client.poll.id,track(2).id)); const pending=clone(b.client.pendingVote);
  reply(json(voteAccepted(body))); await voting;
  assert.deepEqual(JSON.parse(a.storage.getItem(a.client.key)).pendingVote,pending); assert.deepEqual(a.client.pendingVote,pending);
  assert.equal(a.client.poll.myChoiceId,track(1).id);
});

test('a stale same-poll projection in an otherwise current receipt remains unknown', async () => {
  const h=harness(); await readyPoll(h); h.queue.push(unknown); await assert.rejects(h.client.vote(h.client.poll.id,track(1).id));
  const pending=clone(h.client.pendingVote), latest=poll({revision:3,totalVotes:2,
    options:[{track:track(1),votes:2},{track:track(2),votes:0},{track:track(3),votes:0}],myChoiceId:track(1).id});
  h.queue.push(json(pollState({revision:3,poll:latest}))); await h.client.refresh();
  h.queue.push(json(voteAccepted(pending.body,{revision:3}))); await assert.rejects(h.client.reconcileVote(),{code:'invalid-response'});
  assert.deepEqual(h.client.pendingVote,pending); assert.deepEqual(h.client.poll,latest);
});

test('a delayed equal-revision snapshot cannot rewind the deadline clock and a local clock rollback disables voting', async () => {
  let now=1000000; const h=harness({clock:()=>now}); await readyPoll(h);
  now=1005000; h.queue.push(json(pollState({serverAt:1059}))); await h.client.refresh();
  assert.equal(h.client.pollNow(),1059); now+=1000;
  h.queue.push(json(pollState({serverAt:1050}))); await h.client.refresh();
  assert.equal(h.client.pollNow(),1060); assert.equal(h.client.canVote(),false);
  now-=1; assert.equal(h.client.pollFresh(),false);
});

test('fallback sleeves keep the shared exact-recording identities and every supplied demo mapping', async () => {
  const fixture=JSON.parse(await readFile(new URL('../fixtures/client/sleeve-parity-vectors.json',import.meta.url)));
  assert.equal(fixture.cases.filter(row=>row.label.startsWith('DEMO_ART')).length,Object.keys(DEMO_ART).length);
  for(const {id,seed,base,accent,shape,css,label} of fixture.cases){
    assert.deepEqual(fallbackSleeve(id),{seed,base,accent,shape,css},label);
    assert.deepEqual({base,accent,shape},artFor(seed),'Supplied art.ts palette/shape: '+label);
  }
  const pollCases=fixture.cases.filter(row=>['Selected 2','Selected 4','Selected 5'].includes(row.label));
  assert.deepEqual(pollCases.map(({base,accent,shape})=>({base,accent,shape})),[
    {base:'#13293D',accent:'#86BBC2',shape:'rings'},
    {base:'#22324A',accent:'#E9DCC4',shape:'circle'},
    {base:'#2E3B2C',accent:'#C9D6A3',shape:'rings'}
  ]);
});

test('all five browser sleeve geometries match the handoff CSS and the native bundled palette', async () => {
  const fixture=JSON.parse(await readFile(new URL('../fixtures/client/sleeve-parity-vectors.json',import.meta.url)));
  assert.deepEqual([...new Set(fixture.cases.map(row=>row.shape))].sort(),['circle','diagonal','rings','split','stripes']);
  for(const row of fixture.cases)assert.equal(fallbackSleeve(row.id).css,sleeveCss(row.seed),row.label);
  const nativePalette=JSON.parse(await readFile(new URL('../fixtures/client/sleeve-art.json',import.meta.url)));
  assert.deepEqual(nativePalette,PALETTE);
});
