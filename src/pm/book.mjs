// Module B — order book state and sweep detection.
//
// Protocol, as recorded in samples-pm/ws-frames.json:
//   - the first frame after subscribing is an ARRAY of `book` snapshots, one per
//     asset; every later frame is a single OBJECT
//   - `book` is a full replacement, resent periodically, which resyncs us
//   - `price_change` carries { asset_id, price, size, side } where `size` is the
//     NEW resting size at that price, not a delta; zero removes the level
//   - `last_trade_price` is a print, not a book event
//
// Prices are keyed as integer thousandths so "0.1" and "0.10" cannot become two
// different levels.

const key = (price) => Math.round(Number(price) * 1000);
const toPrice = (k) => k / 1000;

/** Live state of one asset's book. */
export class BookState {
  #bids = new Map();
  #asks = new Map();

  constructor(assetId, { conditionId = null, tickSize = 0.01 } = {}) {
    this.assetId = assetId;
    this.conditionId = conditionId;
    this.tickSize = tickSize;
    this.lastUpdate = 0;
  }

  /** Replace the whole book from a `book` message. */
  applyBook(message) {
    this.#bids = new Map();
    this.#asks = new Map();
    for (const level of message.bids ?? []) this.#set(this.#bids, level.price, level.size);
    for (const level of message.asks ?? []) this.#set(this.#asks, level.price, level.size);
    if (message.tick_size) this.tickSize = Number(message.tick_size);
    if (message.market) this.conditionId ??= message.market;
    this.lastUpdate = Number(message.timestamp) || Date.now();
  }

  /** Apply one entry of a `price_change` message. */
  applyPriceChange(entry, timestamp) {
    const side = entry.side === 'SELL' ? this.#asks : this.#bids;
    this.#set(side, entry.price, entry.size);
    this.lastUpdate = Number(timestamp) || Date.now();
  }

  #set(side, price, size) {
    const amount = Number(size);
    const at = key(price);
    if (!Number.isFinite(amount) || amount <= 0) side.delete(at);
    else side.set(at, amount);
  }

  get bestBid() {
    let best = null;
    for (const at of this.#bids.keys()) if (best === null || at > best) best = at;
    return best === null ? null : toPrice(best);
  }

  get bestAsk() {
    let best = null;
    for (const at of this.#asks.keys()) if (best === null || at < best) best = at;
    return best === null ? null : toPrice(best);
  }

  /** Resting bid size at exactly this price. The columns fill rate is read from. */
  sizeAt(price) {
    return this.#bids.get(key(price)) ?? 0;
  }

  /** Resting ask size at exactly this price, for weighing the twin's offer. */
  askSizeAt(price) {
    return this.#asks.get(key(price)) ?? 0;
  }

  /** Bid levels as they stand, price -> size, for comparing against a snapshot. */
  bidSizes() {
    const out = new Map();
    for (const [at, size] of this.#bids) out.set(toPrice(at), size);
    return out;
  }

  /** Complete local replay anchor. A checkpoint does not refresh lastUpdate. */
  snapshot() {
    const levels = side => [...side].map(([p, size]) => ({ price: toPrice(p), size }));
    return { event_type: 'book', asset_id: this.assetId, market: this.conditionId,
      timestamp: this.lastUpdate, tick_size: this.tickSize,
      bids: levels(this.#bids), asks: levels(this.#asks) };
  }

  /** Total resting bid size strictly above a price, for the depth-collapse rule. */
  bidDepthAbove(price) {
    const floor = key(price);
    let total = 0;
    for (const [at, size] of this.#bids) if (at > floor) total += size;
    return total;
  }

  #total(side) {
    let sum = 0;
    for (const size of side.values()) sum += size;
    return sum;
  }

  /** Bid levels at or above a price, newest state, for counting what was eaten. */
  bidLevelsAbove(price) {
    const floor = key(price);
    return [...this.#bids.entries()].filter(([at]) => at > floor)
      .sort((a, b) => b[0] - a[0]).map(([at, size]) => [toPrice(at), size]);
  }

  /**
   * One row for the `book` table.
   * @param {object|null} [paired]  from pairedView(), the twin token's side
   */
  metrics(ts, trigger, paired = null) {
    const bid = this.bestBid;
    const ask = this.bestAsk;
    return [
      ts, this.assetId, this.conditionId, trigger,
      bid, ask, bid !== null && ask !== null ? (bid + ask) / 2 : null,
      this.sizeAt(0.01), this.sizeAt(0.02), this.sizeAt(0.03), this.sizeAt(0.05),
      this.#total(this.#bids), this.#total(this.#asks), this.#bids.size,
      bid !== null && ask !== null ? ask - bid : null,
      // The twin, compactly: enough for a dislocation series and the broken-book
      // detector. fair_lower_bound is 1 - paired_ask and is left to the reader
      // rather than stored, because this table runs to tens of millions of rows.
      paired?.bid ?? null, paired?.ask ?? null, paired?.mid ?? null,
      bookSum(bid, paired?.bid ?? null), paired?.staleSeconds ?? null,
    ];
  }
}

/**
 * What the other outcome token of the same market implies about this one.
 *
 * A binary market's two tokens pay out $1 between them, so P(a) + P(b) = 1. A
 * resting offer to sell b at `ask` is somebody willing to part with it at that
 * price, so the market is not pricing b above it — which puts a floor under a:
 *
 *     P(a) = 1 - P(b) >= 1 - ask_b
 *
 * That floor is the point. When the bid on a has been swept to a cent and the
 * twin's ask still says a is worth at least fifteen, the dislocation is derived
 * from resting orders rather than guessed at, and no outside price feed is
 * involved. Both tokens are already subscribed and both books are already in
 * memory, so this costs a map lookup.
 *
 * The floor is only as good as the offer behind it: a lone share offered at
 * 0.99 implies a floor of 0.01 and means nothing, hence `askSize`. And an offer
 * nobody has refreshed in minutes is not evidence about now, hence
 * `staleSeconds` — measured from the book's last real change, not from the last
 * heartbeat.
 */
export function pairedView(pairedBook, now = Date.now()) {
  if (!pairedBook) return null;
  const bid = pairedBook.bestBid;
  const ask = pairedBook.bestAsk;
  return {
    assetId: pairedBook.assetId,
    bid,
    ask,
    askSize: ask === null ? null : pairedBook.askSizeAt(ask),
    lower: ask === null ? null : round(1 - ask),
    upper: bid === null ? null : round(1 - bid),
    mid: bid !== null && ask !== null ? round(1 - (bid + ask) / 2) : null,
    staleSeconds: pairedBook.lastUpdate ? (now - pairedBook.lastUpdate) / 1000 : null,
  };
}

/**
 * The two sides' best bids together.
 *
 * In a working market this sits just under 1 — the pair pays out a dollar and
 * the shortfall is the spread. A sum far below that is not a market pricing
 * something differently, it is a book with a side missing, which is exactly the
 * event under study and worth flagging on its own.
 */
export function bookSum(bid, pairedBid) {
  return bid === null || pairedBid === null ? null : round(bid + pairedBid);
}

/** How far below the twin's floor the bid has fallen. Undefined at zero. */
export function internalDislocation(lowerBound, bid) {
  return lowerBound === null || !(bid > 0) ? null : round(lowerBound / bid, 4);
}

/** Prices are thousandths; keep the arithmetic from printing 0.30000000000000004. */
function round(value, places = 6) {
  const scale = 10 ** places;
  return Math.round(value * scale) / scale;
}

/**
 * Flags the book collapses the strategy feeds on.
 *
 * Two independent rules, both logged with which one fired:
 *   - the best bid falls by at least `bidDropTicks` of THIS market's tick, or
 *     `bidDropMin` absolute, whichever is larger. The tick is per-market — the
 *     esports books sampled run at 0.01 but some at 0.001, where three ticks
 *     would be 0.003 and would fire on noise.
 *   - resting depth above `depthAbovePrice` loses more than `depthDropRatio`
 *     within `depthWindowSeconds`.
 *
 * Sweeps that stop short of 1-2 cents matter as much as the ones that reach it:
 * they are the denominator of the fill rate.
 */
export class SweepDetector {
  #history = new Map();
  #lastFired = new Map();

  constructor(config) {
    this.config = config;
  }

  /**
   * @param {BookState} book  state AFTER the update
   * @param {{bestBid: number|null, depth: number, levels: Array}} before  state BEFORE
   * @param {string} ts  ISO timestamp
   * @returns {Array|null} a row for the `sweeps` table
   */
  observe(book, before, ts) {
    const { levelsCrossed, bidDropTicks = 3, bidDropMin = 0.02,
      depthWindowSeconds, depthDropRatio, depthAbovePrice,
      minBidBefore = 0.05, minSizeConsumed = 100, minDepth = 100,
      cooldownSeconds = 30 } = this.config;

    const after = book.bestBid;
    const depthAfter = book.bidDepthAbove(depthAbovePrice);
    const at = Date.parse(ts);

    // Depth history is kept whatever happens, so a suppressed update still
    // informs the window that follows it.
    const window = this.#history.get(book.assetId) ?? [];
    const cutoff = at - depthWindowSeconds * 1000;
    while (window.length && window[0].at < cutoff) window.shift();
    const peak = window.reduce((max, point) => Math.max(max, point.depth), before.depth);
    window.push({ at, depth: depthAfter });
    this.#history.set(book.assetId, window);

    // One collapse arrives as a burst of updates. Without this, the same event
    // is logged on every one of them and the log stops being a list of events.
    const fired = this.#lastFired.get(book.assetId);
    if (fired !== undefined && at - fired < cooldownSeconds * 1000) return null;

    // A book that was already at a cent has nothing to collapse: the move under
    // study starts from a real price.
    if (before.bestBid === null || before.bestBid < minBidBefore) return null;

    // Levels that stood above the new best bid and are gone or thinner. An
    // emptied book counts them all, so size decides whether that was a sweep or
    // the maker briefly pulling its quotes.
    const eaten = before.levels.filter(([price]) => after === null || price > after);
    const consumed = eaten.reduce((sum, size) => sum + size[1], 0);

    const rules = [];
    if (after !== null && after < before.bestBid) {
      const threshold = Math.max(bidDropTicks * book.tickSize, bidDropMin);
      if (before.bestBid - after >= threshold - 1e-9 && consumed >= minSizeConsumed) {
        rules.push('bid_drop');
      }
    }
    if (eaten.length >= levelsCrossed && consumed >= minSizeConsumed) rules.push('levels');
    if (peak >= minDepth && depthAfter < peak * (1 - depthDropRatio)) rules.push('depth_collapse');

    if (!rules.length) return null;
    this.#lastFired.set(book.assetId, at);

    // `levels_crossed` counts only the levels that stood ABOVE the new best
    // bid, which in these books is usually one: a maker quote at thirty cents
    // with nothing between it and the penny bids. That is the truth, but it
    // carries almost no variation, so it cannot be tested as a predictor. What
    // is missing from it is every level that was thinned rather than cleared.
    const remaining = book.bidSizes();
    let levelsTouched = 0;
    let sizeEatenPartial = 0;
    for (const [price, size] of before.levels) {
      const left = remaining.get(price) ?? 0;
      if (left >= size) continue;
      levelsTouched++;
      if (left > 0) sizeEatenPartial += size - left;
    }

    return [ts, book.assetId, book.conditionId, rules.join('+'),
      before.bestBid, after, consumed, eaten.length, before.depth, depthAfter,
      levelsTouched, sizeEatenPartial, before.levels.length];
  }

  /** State to capture before applying an update, so the two can be compared. */
  static snapshot(book, depthAbovePrice) {
    return {
      bestBid: book.bestBid,
      depth: book.bidDepthAbove(depthAbovePrice),
      levels: book.bidLevelsAbove(0),
    };
  }

  forget(assetId) {
    this.#history.delete(assetId);
    this.#lastFired.delete(assetId);
  }
}
