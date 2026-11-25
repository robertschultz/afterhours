import {CrowdClient, CrowdError, safeArt, currentProgram, currentRequest, discardInvalidSaved, fallbackSleeve} from './core.mjs';

const $ = id => document.getElementById(id);
const empty = node => node.replaceChildren();
function node(tag, className, text) { const e = document.createElement(tag); if (className) e.className = className; if (text !== undefined) e.textContent = text; return e; }
function art(track, record = false) {
  const {base, accent, css} = fallbackSleeve(track.id); const wrapper = node('div', record ? 'record-art' : 'sleeve');
  wrapper.style.setProperty('--art-base', base); wrapper.style.setProperty('--art-accent', accent);
  if (record) wrapper.append(node('span', 'vinyl'));
  const sleeve = record ? node('span', 'sleeve') : wrapper;
  sleeve.style.background = css;
  if (safeArt(track.artworkUrl)) {
    const img = document.createElement('img'); img.alt = ''; img.referrerPolicy = 'no-referrer'; img.loading = 'lazy';
    img.src = track.artworkUrl; img.addEventListener('error', () => img.remove(), {once: true}); sleeve.append(img);
  }
  if (record) wrapper.append(sleeve); return wrapper;
}
function recordArt(target, track, playing = false) {
  const key = track?.id + ':' + (track?.artworkUrl || '');
  if (target.dataset.track !== key) {
    target.dataset.track = key; empty(target);
    if (track) { const made = art(track, true); target.style.setProperty('--art-base', made.style.getPropertyValue('--art-base'));
      target.style.setProperty('--art-accent', made.style.getPropertyValue('--art-accent')); target.append(...made.childNodes); }
  }
  target.classList.toggle('playing', playing);
}
function button(label, action) { const b = node('button', '', label); b.addEventListener('click', action); return b; }
function say(text) { $('announcement').textContent = text; }
let client, selection = null, searching = false, searchSequence = 0, debounce, refreshTimer, refreshBusy = false;
let waiting = false, lastReceipt = null, failure = null, retryAllowed = false, pendingMessage = '', lastStages = new Map();
let voteWaiting = false, voteRetryAllowed = false, voteMessage = '', renderedPollId = null;
const pollCards = new Map();
const dj = () => client?.state?.performer.name || 'the DJ';
const needsReload = code => code === 'session-changed';
const stopsWork = code => ['guest-expired','guest-required','invitation-closed','storage-unavailable','storage-lock-unavailable','session-changed'].includes(code);
const time = value => new Intl.DateTimeFormat(undefined, {hour: 'numeric', minute: '2-digit'}).format(new Date(value * 1000));
function statusText(row, fresh) {
  if (!fresh) return {sent: 'Last update: sent', listening: 'Last update: listening', coming: 'Last update: coming up',
    declined: row.reason || 'Last update: not tonight', playing: row.playingNow ? 'Last seen playing' : 'Played earlier'}[row.stage];
  if (row.stage === 'playing') return row.playingNow ? 'Playing now' : 'Played';
  if (row.stage === 'coming') return row.at ? 'Coming up around ' + time(row.at) : 'Coming up';
  if (row.stage === 'listening') return dj() + ' is listening';
  if (row.stage === 'declined') return row.reason || 'Not tonight';
  return row.reason || 'Sent to ' + dj();
}
function showError(error) {
  failure = error instanceof CrowdError ? error : new CrowdError('connection-unknown', 'The room could not confirm this update. Check again when connected.');
  $('connection').hidden = false; $('connection-copy').textContent = failure.message; empty($('connection-actions'));
  const expired = ['guest-expired', 'guest-required'].includes(failure.code);
  if (needsReload(failure.code)) {
    client?.suspend(); if (client) { client.state = null; client.searchResult = null; client.poll = null; }
    lastReceipt = null; selection = null; closeSheet(); closeSearch();
    $('connection-actions').append(button('Reload current session', () => location.reload()));
  } else if (expired) {
    $('connection-copy').textContent += ' Starting a new guest session does not resend previous requests.';
    $('connection-actions').append(button('Start a new guest session', async () => {
      try { await client.newGuest(); lastReceipt = null; selection = null; voteMessage = ''; voteRetryAllowed = false; closeSheet(); closeSearch(); await join(); }
      catch (error) { showError(error); }
    }));
  } else if (!['invitation-closed', 'storage-unavailable', 'storage-lock-unavailable'].includes(failure.code)) {
    $('connection-actions').append(button(client?.state ? 'Refresh status' : 'Try joining again', () => client?.state ? refresh() : join()));
  }
  render();
}
function clearError() { failure = null; $('connection').hidden = true; empty($('connection-actions')); }
function render() {
  if (!client) return;
  const state = client.state; const fresh = client.fresh(); const program = state?.program;
  const observed = currentProgram(state, client.serverNow());
  const title = state?.night.title || 'Tonight'; $('night-title').textContent = title; document.title = title + ' · Cue';
  $('night-subtitle').textContent = state ? [state.night.venue, fresh ? 'Room connected' : 'Waiting for a room update'].filter(Boolean).join(' · ') : 'Joining the room…';
  $('search').disabled = !state || !!client.pending || waiting || stopsWork(failure?.code);
  $('search').placeholder = 'Ask ' + dj() + ' for a song'; $('search-title').textContent = 'In ' + dj() + '’s crate';
  $('sheet-heading').textContent = 'Ask ' + dj(); $('send-request').textContent = waiting ? 'Checking request…' : 'Send to ' + dj();
  $('send-request').disabled = waiting || !!client.pending || !selection || !state || state.guest.openCount >= 3;
  if (program) {
    const playing = !!observed && program.playing; recordArt($('now-art'), program.track, playing);
    $('now-status').textContent = observed ? (program.playing ? 'Now playing' : 'Paused') : (program.playing ? 'Last seen playing' : 'Last seen paused');
    $('now-title').textContent = program.track.title; $('now-artist').textContent = program.track.artist;
    const duration = program.durationSeconds, position = program.positionSeconds;
    $('progress-row').hidden = !(observed && Number.isFinite(duration) && Number.isFinite(position));
    if (!$('progress-row').hidden) {
      const passed = playing ? Math.min(3, Math.max(0, client.serverNow() - program.sampledAt)) : 0;
      const elapsed = Math.min(duration, position + passed), left = Math.max(0, Math.ceil(duration - elapsed));
      $('progress').value = elapsed / duration; $('time-left').textContent = Math.floor(left / 60) + ':' + String(left % 60).padStart(2, '0') + ' left';
    }
  } else {
    recordArt($('now-art'), null); $('now-status').textContent = state ? 'Music status unavailable' : 'Room status';
    $('now-title').textContent = 'Waiting for the music'; $('now-artist').textContent = 'The current record will appear here.'; $('progress-row').hidden = true;
  }
  $('performer-name').textContent = state ? dj() + ' is your DJ' : 'Your DJ';
  $('performer-status').textContent = fresh ? 'Ask for a record from tonight’s crate.' : 'Requests stay with this room.';
  const avatar = $('performer-art');
  if (avatar.dataset.profile !== state?.performer.id) {
    avatar.dataset.profile = state?.performer.id || ''; empty(avatar); avatar.textContent = (state?.performer.name || 'DJ').slice(0, 2).toUpperCase();
    if (safeArt(state?.performer.imageUrl)) { const img = document.createElement('img'); img.alt = ''; img.src = state.performer.imageUrl;
      img.referrerPolicy = 'no-referrer'; img.addEventListener('error', () => { avatar.textContent = dj().slice(0, 2).toUpperCase(); }, {once: true}); empty(avatar); avatar.append(img); }
  }
  const rows = [...(state?.requests || [])];
  if (lastReceipt) { const index = rows.findIndex(r => r.id === lastReceipt.request.id);
    if (index < 0) rows.unshift(lastReceipt.request); else if (rows[index].revision < lastReceipt.request.revision) rows[index] = lastReceipt.request; }
  $('your-requests').hidden = !rows.length; empty($('request-list'));
  $('request-count').textContent = state && state.revision === client.revision ? `${state.guest.openCount} of 3 open` : 'Up to 3 open';
  for (const raw of rows.slice(0, 32)) {
    const request = currentRequest(raw, observed, client.serverNow(), state?.next);
    const row = node('div', 'request-row'); row.append(art(request.track)); const copy = node('div', 'copy');
    copy.append(node('p', 'title', request.track.title)); copy.append(node('p', 'secondary small', request.track.artist));
    const steps = node('div', 'steps'); steps.setAttribute('aria-hidden', 'true');
    const stage = {sent: 1, listening: 2, coming: 3, declined: 1, playing: 3}[request.stage];
    for (let i = 0; i < 3; i++) steps.append(node('span', 'step' + (i < stage ? (i && fresh ? ' decision' : ' done') : '')));
    copy.append(steps, node('p', 'status' + (fresh && ['listening','coming'].includes(request.stage) ? ' decision' : ''), statusText(request, fresh)));
    row.append(copy); $('request-list').append(row);
    if (lastStages.has(request.id) && lastStages.get(request.id) !== request.stage) say(request.track.title + '. ' + statusText(request, fresh));
    lastStages.set(request.id, request.stage);
  }
  for (const id of lastStages.keys()) if (!rows.some(row => row.id === id)) lastStages.delete(id);
  $('next-up').hidden = !(observed?.playing && state?.next);
  if (!$('next-up').hidden) $('next-copy').textContent = 'Next · ' + state.next.track.title
    + (state.next.estimatedAt > client.serverNow() ? ' · around ' + time(state.next.estimatedAt) : '');
  $('room-note').textContent = dj() + ' plays what fits the night. Not every request makes it.';
  $('pending').hidden = !client.pending || needsReload(failure?.code);
  if (client.pending) $('pending-copy').textContent = pendingMessage || client.pending.track.title + ' has not been confirmed here yet. Check the saved request before trying again.';
  $('retry-request').hidden = !retryAllowed; $('retry-request').disabled = waiting; $('check-request').disabled = waiting;
  renderPoll();
}
function renderPoll() {
  const poll = client.poll, pending = client.pendingVote, fresh = client.pollFresh();
  $('poll').hidden = (!poll && !pending) || needsReload(failure?.code);
  // Counts and countdowns update in place. Keeping the same buttons preserves
  // keyboard focus, touch targets and accessibility identity on every tick.
  if (renderedPollId !== (poll?.id || null)) {
    if (!pending) { voteMessage = ''; voteRetryAllowed = false; }
    renderedPollId = poll?.id || null; pollCards.clear(); empty($('poll-options'));
    for (const option of poll?.options || []) {
      const card = button('', () => castVote(poll.id, option.track.id)); card.className = 'poll-card glass';
      const sleeve = art(option.track); sleeve.setAttribute('aria-hidden', 'true');
      const title = node('span', 'poll-track', option.track.title);
      const meter = node('span', 'poll-meter'), bar = node('span', 'poll-bar'), fill = node('span', 'poll-fill');
      const percent = node('span', 'poll-percent'); bar.append(fill); meter.append(bar, percent); meter.setAttribute('aria-hidden', 'true');
      card.append(sleeve, title, meter); $('poll-options').append(card);
      pollCards.set(option.track.id, {card, fill, percent});
    }
  }
  $('poll-options').style.setProperty('--poll-columns', poll?.options.length || 3);
  $('poll-title').textContent = !poll ? 'Checking your vote' : poll.status !== 'open' ? 'The room’s choice'
    : fresh && client.clock() - client.pollRefresh < 3000 && poll.slotAt > client.pollNow()
      ? 'Pick the ' + time(poll.slotAt) + ' record' : 'Pick a future record';
  $('poll-count').textContent = poll ? `${poll.totalVotes} ${poll.totalVotes === 1 ? 'vote' : 'votes'}` : '';
  const high = Math.max(0, ...(poll?.options || []).map(option => option.votes));
  const leaders = (poll?.options || []).filter(option => option.votes === high);
  const leader = fresh && high > 0 && leaders.length === 1 ? leaders[0].track.id : null;
  for (const option of poll?.options || []) {
    const {card, fill, percent} = pollCards.get(option.track.id), share = poll.totalVotes ? option.votes / poll.totalVotes : 0;
    const percentage = Math.round(share * 100), selected = option.track.id === poll.myChoiceId;
    card.disabled = !client.canVote() || voteWaiting || stopsWork(failure?.code);
    card.setAttribute('aria-pressed', String(selected));
    card.setAttribute('aria-label', `${option.track.title}, ${option.track.artist}, ${option.votes} votes, ${percentage} percent`);
    card.setAttribute('aria-busy', String(!!pending && pending.body.pollId === poll.id && pending.body.trackId === option.track.id));
    fill.style.transform = 'scaleX(' + share + ')'; fill.classList.toggle('leader', option.track.id === leader);
    percent.textContent = percentage + '%';
  }
  $('poll-context').textContent = poll?.status === 'open' ? dj() + ' picked these for a future slot.' : '';
  const chosen = poll?.options.find(option => option.track.id === poll.chosenTrackId);
  $('poll-status').classList.toggle('decision', !!chosen && fresh);
  $('poll-status').textContent = !poll ? '' : !fresh ? 'Waiting for the room’s latest vote.'
    : poll.status === 'open' ? (client.pollNow() < poll.closesAt
      ? 'Voting closes in ' + Math.max(0, Math.ceil(poll.closesAt - client.pollNow())) + 's.'
      : 'Voting closed. The DJ is confirming the record.')
    : chosen ? 'The DJ selected ' + chosen.track.title + ' for the upcoming queue.' : (poll.reason || 'This vote has ended.');
  $('vote-message').textContent = voteMessage || (pending ? pending.track.title + ' is waiting for confirmation. Check the saved vote before choosing again.' : '');
  $('check-vote').hidden = !pending; $('check-vote').disabled = voteWaiting;
  $('check-vote').textContent = voteWaiting ? 'Checking vote…' : voteRetryAllowed ? 'Retry same vote' : 'Check vote status';
}
async function join() {
  if (!client) return;
  try { await client.join(); clearError(); render(); if (client.pending) await checkPending(); if (client.pendingVote) await checkPendingVote(); }
  catch (error) { showError(error); }
  schedule();
}
async function refresh() {
  if (!client?.credential || refreshBusy || document.hidden) return;
  refreshBusy = true;
  try { await client.refresh(); clearError(); render(); }
  catch (error) { showError(error); }
  finally { refreshBusy = false; schedule(); }
}
function schedule() {
  clearTimeout(refreshTimer);
  if (document.hidden || !client?.state || stopsWork(failure?.code)) return;
  refreshTimer = setTimeout(refresh, failure ? Math.max(10000, failure.retryAfter * 1000) : 2000);
}
function openSearch() { if ($('search').disabled) return; searching = true; $('search-panel').hidden = false; $('close-search').hidden = false; document.body.classList.add('search-open'); }
function closeSearch() { searching = false; searchSequence++; clearTimeout(debounce); $('search-panel').hidden = true; $('close-search').hidden = true; document.body.classList.remove('search-open'); $('search').blur(); }
function renderResults(result) {
  empty($('search-results')); $('search-message').textContent = result.records.length ? '' : 'No matching record in this crate. Try another track or artist.';
  for (const track of result.records) {
    const row = node('div', 'result'); row.append(art(track)); const copy = node('div', 'copy');
    copy.append(node('p', 'title', track.title), node('p', 'secondary', track.artist)); row.append(copy);
    const asked = client.state.requests.some(r => r.track.id === track.id) || lastReceipt?.request.track.id === track.id;
    const choose = button(asked ? 'ASKED' : 'ASK', () => openSheet(track.id)); choose.disabled = asked || !!client.pending;
    choose.setAttribute('aria-label', (asked ? 'Already asked for ' : 'Ask for ') + track.title + ' by ' + track.artist); row.append(choose); $('search-results').append(row);
  }
}
async function search() {
  const query = $('search').value.trim(), sequence = ++searchSequence; openSearch(); empty($('search-results'));
  if (query.length < 2) { $('search-message').textContent = 'Enter at least two characters for a track or artist.'; return; }
  $('search-message').textContent = 'Searching the crate…';
  try { const result = await client.search(query); if (sequence !== searchSequence || !searching) return; renderResults(result); }
  catch (error) { if (sequence === searchSequence && searching) $('search-message').textContent = error.message; if (stopsWork(error.code)) showError(error); }
}
function openSheet(trackId) {
  try { selection = client.selection(trackId); $('sheet-title').textContent = selection.track.title; $('sheet-artist').textContent = selection.track.artist;
    recordArt($('sheet-art'), selection.track); $('sheet-message').textContent = client.state.guest.openCount >= 3 ? 'Three requests are open. A slot opens when one plays or is declined.' : '';
    $('refresh-selection').hidden = true; render(); $('request-sheet').showModal(); }
  catch (error) { showError(error); }
}
function closeSheet() { $('request-sheet').close(); }
async function accepted(result) {
  lastReceipt = result; retryAllowed = false; pendingMessage = ''; closeSheet(); closeSearch(); render(); say(result.request.track.title + '. ' + statusText(result.request, true));
  await refresh(); if (searching && client.searchResult) renderResults(client.searchResult);
}
async function send() {
  if (!selection || waiting) return; waiting = true; pendingMessage = ''; $('sheet-message').textContent = ''; render();
  try { await accepted(await client.send(selection, false)); }
  catch (error) { $('sheet-message').textContent = error.message; $('refresh-selection').hidden = error.code !== 'selection-expired';
    if (client.pending) { $('sheet-message').textContent = 'The room has not confirmed this request. Its identity is saved; check its status before retrying.'; closeSheet(); closeSearch(); }
    if (stopsWork(error.code)) showError(error); }
  finally { waiting = false; render(); }
}
async function checkPending(retry = false) {
  if (!client.pending || waiting) return; waiting = true; retryAllowed = false; render();
  try { await accepted(await (retry ? client.retry() : client.reconcile())); }
  catch (error) { retryAllowed = error.code === 'receipt-not-found';
    pendingMessage = retryAllowed ? 'No accepted request was found. Retry sends the same saved request, once.' : error.message;
    if (!client.pending || stopsWork(error.code)) showError(error); }
  finally { waiting = false; render(); }
}
async function voteAccepted(result) {
  voteRetryAllowed = false;
  const current = client.poll;
  const choice = current?.id === result.poll.id && current.options.find(option => option.track.id === current.myChoiceId);
  voteMessage = choice ? 'Your vote for ' + choice.track.title + ' is confirmed.' : 'Your saved vote is confirmed.';
  render(); say(voteMessage); await refresh();
}
async function castVote(pollId, trackId) {
  if (voteWaiting || !client.canVote()) return;
  voteWaiting = true; voteRetryAllowed = false; voteMessage = 'Checking your vote…'; renderPoll();
  try { await voteAccepted(await client.vote(pollId, trackId)); }
  catch (error) { voteMessage = error.message; if (stopsWork(error.code)) showError(error); }
  finally { voteWaiting = false; render(); }
}
async function checkPendingVote(retry = false) {
  if (!client.pendingVote || voteWaiting) return;
  voteWaiting = true; voteRetryAllowed = false; renderPoll();
  try { await voteAccepted(await (retry ? client.retryVote() : client.reconcileVote())); }
  catch (error) {
    voteRetryAllowed = error.code === 'receipt-not-found' && error.status === 404;
    voteMessage = voteRetryAllowed ? 'No accepted vote was found. Retry sends the same saved vote, once.' : error.message;
    if (stopsWork(error.code)) showError(error);
  }
  finally { voteWaiting = false; render(); }
}
$('search').addEventListener('focus', openSearch);
$('search').addEventListener('input', () => { clearTimeout(debounce); debounce = setTimeout(search, 350); });
$('search-form').addEventListener('submit', event => { event.preventDefault(); clearTimeout(debounce); search(); });
$('close-search').addEventListener('click', closeSearch); $('close-sheet').addEventListener('click', closeSheet);
$('send-request').addEventListener('click', send); $('check-request').addEventListener('click', () => checkPending());
$('retry-request').addEventListener('click', () => checkPending(true)); $('refresh').addEventListener('click', () => client?.state ? refresh() : join());
$('check-vote').addEventListener('click', () => checkPendingVote(voteRetryAllowed));
$('refresh-selection').addEventListener('click', async () => {
  if (!selection || waiting) return; waiting = true; render();
  try { selection = await client.refreshSelection(selection); $('refresh-selection').hidden = true; $('sheet-message').textContent = 'This exact recording is ready to send.'; }
  catch (error) { $('sheet-message').textContent = error.message; if (stopsWork(error.code)) showError(error); }
  finally { waiting = false; render(); }
});
$('theme').addEventListener('click', () => { const light = document.documentElement.dataset.theme !== 'light';
  document.documentElement.dataset.theme = light ? 'light' : 'night'; $('theme').setAttribute('aria-label', light ? 'Use dark appearance' : 'Use light appearance');
  document.querySelector('meta[name="theme-color"]')?.setAttribute('content', getComputedStyle(document.documentElement).getPropertyValue('--bg').trim()); });
document.addEventListener('visibilitychange', () => { if (document.hidden) { clearTimeout(refreshTimer); clearTimeout(debounce); client?.suspend(); render(); } else { refresh(); if (client?.pending) checkPending(); if (client?.pendingVote) checkPendingVote(); } });
window.addEventListener('pagehide', () => { clearTimeout(refreshTimer); client?.suspend(); });
window.addEventListener('pageshow', event => { if (event.persisted) { refresh(); if (client?.pending) checkPending(); if (client?.pendingVote) checkPendingVote(); } });
window.addEventListener('online', () => refresh());
window.addEventListener('storage', event => {
  if (!client || (event.key !== null && event.key !== client.key)) return;
  try {
    const previous = client.pending?.body.requestId, previousVote = client.pendingVote?.body.requestId; client.storageChanged();
    if (previous !== client.pending?.body.requestId) { retryAllowed = false; pendingMessage = ''; }
    if (previousVote !== client.pendingVote?.body.requestId) { voteRetryAllowed = false; voteMessage = ''; }
    render();
  }
  catch (error) { showError(error); }
});
// Mobile keyboards can scroll the visual viewport independently of layout.
// Position the fixed search surface from these measured viewport coordinates.
function syncViewport() {
  const viewport = window.visualViewport;
  document.documentElement.style.setProperty('--viewport-offset', (viewport?.offsetTop || 0) + 'px');
  document.documentElement.style.setProperty('--viewport-height', (viewport?.height || window.innerHeight) + 'px');
}
window.visualViewport?.addEventListener('resize', syncViewport);
window.visualViewport?.addEventListener('scroll', syncViewport);
window.addEventListener('resize', syncViewport); syncViewport();
setInterval(() => { if (!document.hidden) render(); }, 1000);
try { client = new CrowdClient(location.origin + location.pathname); render(); join(); }
catch (error) {
  $('connection').hidden = false; $('connection-copy').textContent = error.message; $('night-subtitle').textContent = 'Room unavailable';
  if (error.code === 'invalid-response' && typeof error.invalidSavedValue === 'string') {
    $('connection-copy').textContent += ' A new guest session will not resend previous requests.';
    $('connection-actions').append(button('Start a new guest session', async () => {
      try { await discardInvalidSaved('afterhours.engine.crowd.v1:' + location.origin + location.pathname, error.invalidSavedValue); location.reload(); }
      catch (failure) { $('connection-copy').textContent = failure.message || 'Allow website storage, then reload this page.'; }
    }));
  }
}
