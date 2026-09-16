// Module B — the websocket feed.
//
// Every disconnect is a hole in the denominator: market-hours we thought we were
// watching but were not. So a drop is never silent — it is timed, counted and
// written to the `gaps` table, and the analyzer subtracts those windows instead
// of dividing by a coverage it never had.
//
// Subscriptions are set at connect time, so changing the asset list reconnects
// that chunk. Discovery only reports genuine additions and removals, so this
// happens when a match starts or ends, not every poll.

const WS_URL = 'wss://ws-subscriptions-clob.polymarket.com/ws/market';
let connectionSequence = 0;

/** One socket carrying one chunk of the asset list. */
class Connection {
  #ws = null;
  #timer = null;
  #keepalive = null;
  #attempt = 0;
  #closedAt = null;
  #stopped = false;

  constructor(feed, assets) {
    this.feed = feed;
    this.assets = assets;
    this.id = `connection-${++connectionSequence}`;
  }

  open() {
    const { WebSocketImpl, url } = this.feed;
    this.#ws = new WebSocketImpl(url);
    const socket = this.#ws;

    this.#ws.addEventListener('open', () => {
      if (this.#stopped || this.#ws !== socket || this.#timer) return;
      this.feed.onReset?.(this.assets, 'subscribe', { connectionId: this.id });
      this.#attempt = 0;
      if (this.#closedAt !== null) {
        this.feed.onGap({
          startedAt: new Date(this.#closedAt).toISOString(),
          endedAt: new Date().toISOString(),
          durationMs: Date.now() - this.#closedAt,
          reason: 'reconnect',
          assets: this.assets.length,
        });
        this.#closedAt = null;
      }
      this.#ws.send(JSON.stringify({ assets_ids: this.assets, type: 'market' }));
      // An idle socket gets dropped; the CLOB feed answers a plain PING.
      const every = this.feed.keepaliveSeconds * 1000;
      this.#keepalive = setInterval(() => {
        try {
          this.#ws.send('PING');
        } catch {
          // the close handler owns reconnection
        }
      }, every);
      this.#keepalive.unref?.();
    });

    this.#ws.addEventListener('message', (event) => {
      if (this.#stopped || this.#ws !== socket || this.#timer) return;
      const meta = { connectionId: this.id, receivedAt: new Date().toISOString(),
        monotonicMs: performance.now() };
      const data = typeof event.data === 'string' ? event.data : String(event.data);
      let parsed;
      try {
        parsed = JSON.parse(data);
      } catch {
        return; // PONG and other non-JSON keepalive traffic
      }
      // The first frame after subscribing is an array of snapshots; later frames
      // are single objects.
      const batch = (Array.isArray(parsed) ? parsed : [parsed]).filter(m => m?.event_type);
      if (this.feed.onBatch) this.feed.onBatch(batch, meta);
      else for (const message of batch) this.feed.onMessage?.(message, meta);
    });

    this.#ws.addEventListener('close', () => {
      if (this.#ws === socket) this.#down('close');
    });
    this.#ws.addEventListener('error', () => {
      if (this.#ws === socket) this.#down('error');
    });
  }

  #down(reason) {
    clearInterval(this.#keepalive);
    if (this.#stopped || this.#timer) return;
    this.feed.onReset?.(this.assets, reason, { connectionId: this.id });
    this.#closedAt ??= Date.now();
    const { reconnectMinMs, reconnectMaxMs } = this.feed;
    const wait = Math.min(reconnectMaxMs, reconnectMinMs * 2 ** this.#attempt++);
    this.feed.onStatus?.(`socket ${reason}, reconnecting in ${Math.round(wait / 1000)}s`);
    this.#timer = setTimeout(() => {
      this.#timer = null;
      this.open();
    }, wait);
    this.#timer.unref?.();
    // An error need not emit close. Retire it before opening another socket.
    try { this.#ws?.close(); } catch { /* already closed */ }
  }

  close() {
    if (!this.#stopped) this.feed.onReset?.(this.assets, 'unsubscribe', { connectionId: this.id });
    this.#stopped = true;
    clearInterval(this.#keepalive);
    clearTimeout(this.#timer);
    try {
      this.#ws?.close();
    } catch {
      // already gone
    }
  }
}

/** Keeps the whole asset list subscribed across however many sockets it takes. */
export class BookFeed {
  #connections = [];
  #assets = [];

  constructor({
    onMessage, onBatch, onReset, onGap, onStatus, url = WS_URL, WebSocketImpl = globalThis.WebSocket,
    assetsPerConnection = 250, keepaliveSeconds = 10,
    reconnectMinMs = 1000, reconnectMaxMs = 60000,
  }) {
    Object.assign(this, {
      onMessage, onBatch, onReset, onGap, onStatus, url, WebSocketImpl,
      assetsPerConnection, keepaliveSeconds, reconnectMinMs, reconnectMaxMs,
    });
  }

  get assets() {
    return [...this.#assets];
  }

  get connectionCount() {
    return this.#connections.length;
  }

  /** Replace the subscription set. A no-op when the set is unchanged. */
  setAssets(assetIds) {
    const next = [...new Set(assetIds)].sort();
    if (next.join() === this.#assets.join()) return false;
    this.#assets = next;
    for (const connection of this.#connections) connection.close();
    this.#connections = [];
    for (let i = 0; i < next.length; i += this.assetsPerConnection) {
      const connection = new Connection(this, next.slice(i, i + this.assetsPerConnection));
      this.#connections.push(connection);
      connection.open();
    }
    return true;
  }

  stop() {
    for (const connection of this.#connections) connection.close();
    this.#connections = [];
  }
}
