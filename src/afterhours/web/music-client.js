// External engine only: never claims the Web Audio session or commands its queue.
export class MusicConnection {
  constructor(fetcher = (...args) => fetch(...args), clock = () => Date.now()) {
    this.fetcher = fetcher; this.clock = clock; this.token = null;
    this.busy = false; this.status = null; this.catalog = null; this.checkedAt = 0;
    this.needsRefresh = true;
    this.polling = false; this.pollPromise = null; this.waitingCommand = false;
    this.queue = null; this.queueReadAt = 0; this.queueStale = true;
    this.queueReading = false; this.queueError = ''; this.queueUnsupported = false;
  }
  get ready() { return !!this.status?.connected && !this.needsRefresh && this.clock() - this.checkedAt < 15000; }
  get queueFresh() { return !!this.queue && this.ready && !this.queueStale && this.clock() - this.queueReadAt < 15000; }
  queueAnchor() { return JSON.stringify([this.status?.automix, this.status?.decks?.map(d => [d.deck,d.title,d.artist,d.playing])]); }
  async request(path, data) {
    if (!this.token) {
      const config = await this.fetcher('/api/config', {cache:'no-store', signal:AbortSignal.timeout(45000)});
      if (!config.ok) throw new Error('Local server unavailable. Reload after the server is running.');
      this.token = (await config.json()).token;
    }
    const timeout = path === '/api/vdj/status' ? 3000 : path === '/api/vdj/queue' ? 5000 : 45000;
    const options = {headers:{'X-DJ-Token':this.token}, cache:'no-store', signal:AbortSignal.timeout(timeout)};
    if (data) { options.method = 'POST'; options.headers['Content-Type'] = 'application/json'; options.body = JSON.stringify(data); }
    const response = await this.fetcher(path, options);
    const value = await response.json();
    if (response.status === 403) this.token = null;
    if (!response.ok) {
      const error = new Error(value.error || 'VirtualDJ did not respond. Check connection before retrying.');
      error.status = response.status; throw error;
    }
    return value;
  }
  startNewQueue({messageId, text, approveRequest = false, approvePreparation = false, approveSessionCuration = false}) {
    return this.request('/api/dj-agent', {action:'start-new-queue', messageId, text, approveRequest, approvePreparation, approveSessionCuration});
  }
  previewInsertion(trackId) {
    return this.request('/api/set-session', {action:'preview-insertion', trackId});
  }
  insertTrack(trackId) {
    return this.request('/api/set-session', {action:'insert-track', trackId});
  }
  async refresh({includeCatalog = true, background = false} = {}) {
    if (this.busy) throw new Error('A request is still in progress.');
    this.busy = true;
    if (!background) this.needsRefresh = true;
    if (includeCatalog) this.catalog = null;
    try {
      const before = this.queueAnchor();
      this.status = await this.request('/api/vdj/status');
      if (before !== this.queueAnchor()) this.queueStale = true;
      this.checkedAt = this.clock(); this.needsRefresh = false;
      if (!includeCatalog) return {};
      // Catalog failure need not disable a working deck transport connection.
      try { this.catalog = await this.request('/api/vdj/catalog'); }
      catch (error) { return {catalogError:error.message}; }
      return {};
    } catch (error) { this.status = null; this.catalog = null; this.needsRefresh = true; this.queueStale = true; throw error; }
    finally { this.busy = false; }
  }
  async readQueue() {
    if (this.busy || this.waitingCommand) throw new Error('A request is still in progress.');
    if (!this.ready) throw new Error('Check connection before reading upcoming tracks.');
    this.busy = true; this.queueReading = true; this.queue = null;
    this.queueStale = true; this.queueError = ''; this.queueUnsupported = false;
    try {
      const value = await this.request('/api/vdj/queue');
      validateQueue(value);
      this.queue = value; this.queueReadAt = this.clock();
      this.queueStale = value.automix !== this.status.automix;
      return value;
    } catch (error) {
      this.queueUnsupported = error.status === 404;
      this.queueError = this.queueUnsupported
        ? 'Upcoming preview is not available in this server session. Restart Afterhours at a break, then check connection and try again.'
        : error.name === 'TimeoutError'
          ? 'Upcoming tracks took too long to read. Read upcoming to try again.'
          : 'Upcoming tracks could not be read. Read upcoming to try again.';
      if (error.status === 403) {
        this.needsRefresh = true;
        this.queueError = 'The local session changed. Check connection before reading upcoming tracks.';
      }
      throw error;
    } finally { this.busy = false; this.queueReading = false; }
  }
  poll() {
    // Preserve the last fresh state while a read is in flight. A user command
    // may reserve the next slot, but must await this read and pass readiness
    // again. This never cancels an HTTP request whose server may still be busy.
    if (this.busy || this.waitingCommand) return Promise.reject(new Error('A request is still in progress.'));
    if (!this.ready) return Promise.reject(new Error('Check connection before refreshing live state.'));
    this.polling = true;
    const pending = this.refresh({includeCatalog:false, background:true});
    this.pollPromise = pending;
    return pending.finally(() => { this.polling = false; this.pollPromise = null; });
  }
  async command(action, extra = {}) {
    if (this.waitingCommand) throw new Error('A request is still in progress.');
    if (this.pollPromise) {
      this.waitingCommand = true;
      try { await this.pollPromise; }
      catch (error) { throw new Error('The state check failed; no command was sent.', {cause:error}); }
      finally { this.waitingCommand = false; }
    }
    if (this.busy) throw new Error('A request is still in progress.');
    if (!this.ready) throw new Error('State is out of date. Check connection before sending a command.');
    this.busy = true;
    // Each user action gets one dispatch. Refresh is required after any outcome.
    this.needsRefresh = true;
    this.queueStale = true;
    try { return await this.request('/api/vdj/command', {...extra, action, execute:true}); }
    finally { this.busy = false; }
  }
}

function validateQueue(value) {
  const states = ['available', 'automix-off', 'empty', 'metadata-unavailable'];
  const rows = value?.upcoming;
  if (!value || value.connected !== true || value.read_only !== true || typeof value.automix !== 'boolean'
      || !Number.isSafeInteger(value.queue_count) || value.queue_count < 0
      || !states.includes(value.availability) || value.complete !== false || value.snapshot_atomic !== false
      || !Array.isArray(rows) || rows.length > 3
      || rows.some(row => !row || ![1,2,3].includes(row.offset) || typeof row.title !== 'string' || typeof row.artist !== 'string')
      || new Set(rows.map(row => row.offset)).size !== rows.length
      || (value.availability === 'available' ? !value.automix || !value.queue_count || !rows.length : rows.length > 0)
      || (value.availability === 'automix-off' && value.automix)
      || (value.availability === 'empty' && value.queue_count !== 0)) {
    throw new Error('Invalid upcoming preview response');
  }
}
