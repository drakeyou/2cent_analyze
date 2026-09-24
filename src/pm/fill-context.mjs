// Module D — the book around a fill by a studied wallet.
//
// This is the ground truth the whole collection exists for. Everything else —
// the subscription window, the sweep detector, the twin token — is there so
// that when one of the wallets is filled at two cents, the state of the book on
// either side of that moment is on record.
//
// It has to be assembled afterwards. A fill is not an event we receive: it is
// found by polling the trade log tens of seconds later, and the fifteen minutes
// after it have not happened yet. So the fill is queued and written up once its
// horizons have passed, out of snapshots already stored — the same shape as the
// sweep horizons, and for the same reason.
//
// `snapshot_available` is the column that keeps this honest. A row of nulls
// because the market was never watched and a row of nulls because the book was
// empty are different findings, and without the flag they read the same.

/** Seconds before the fill the book is sampled at. */
export const BEFORE = [60, 10, 1];
/** Seconds after the fill the book is sampled at. */
export const AFTER = [60, 300, 900];
/** How far back a sweep still counts as the one that caused this fill. */
export const SWEEP_WINDOW_SECONDS = 120;
/** How stale a snapshot may be and still answer for the instant asked about. */
export const STALE_TOLERANCE_MS = 30_000;

const iso = (at) => new Date(at).toISOString();

/** Fills waiting for their horizons to pass. */
export class FillContextQueue {
  #pending = new Map();

  constructor({ settleSeconds = Math.max(...AFTER), maxAgeHours = 24 } = {}) {
    this.settleSeconds = settleSeconds;
    this.maxAgeHours = maxAgeHours;
  }

  /**
   * Queue a fill, once.
   *
   * The trade log answers with the wallet's history, not with what just
   * happened: a first poll returns hundreds of fills going back days. Writing
   * those up produces a row per fill saying the book was not being watched,
   * which is true and useless — nobody was watching because the collector did
   * not exist yet — and it buries the fills that can be answered. Only fills
   * recent enough to have snapshots behind them are taken.
   *
   * @param {object} fill  { ts, assetId, conditionId, wallet, side, price, size }
   * @returns {boolean} whether it was new
   */
  add(fill, now = Date.now()) {
    const key = [fill.ts, fill.assetId, fill.wallet, fill.side, fill.price, fill.size].join('|');
    if (this.#pending.has(key)) return false;
    const at = Date.parse(fill.ts);
    if (!Number.isFinite(at)) return false;
    if (now - at > this.maxAgeHours * 3600 * 1000) return false;
    this.#pending.set(key, { ...fill, at, dueAt: at + this.settleSeconds * 1000 });
    return true;
  }

  /** Fills whose horizons have passed, removed from the queue. */
  due(now = Date.now()) {
    const ready = [];
    for (const [key, job] of this.#pending) {
      if (now < job.dueAt) continue;
      ready.push(job);
      this.#pending.delete(key);
    }
    return ready;
  }

  /** Everything still waiting, for a shutdown that writes what it can. */
  drain() {
    const all = [...this.#pending.values()];
    this.#pending.clear();
    return all;
  }

  get size() {
    return this.#pending.size;
  }
}

/**
 * Write up one fill from the stored snapshots.
 *
 * @param {object} job  a queued fill
 * @param {object} store  anything with bookAt, sweepBefore, sawBook, fillOrdinal
 * @param {number} [now]
 * @returns {Array} a row for the fill_context table
 */
export function contextRow(job, store, now = Date.now()) {
  const at = job.at;
  const before = store.bookAt(job.assetId, iso(at));
  const [minus60, minus10, minus1] = BEFORE.map(
    (seconds) => store.bookAt(job.assetId, iso(at - seconds * 1000)));
  const [plus60, plus300, plus900] = AFTER.map(
    (seconds) => store.bookAt(job.assetId, iso(at + seconds * 1000)));

  // A sample from long before the moment asked for is not a sample. The newest
  // row at or before t-60 can be hours old on a market nobody was watching, and
  // would read as a live quote. A watched book is heartbeated every five
  // seconds, so half a minute of slack is generous.
  const sample = (row, offsetSeconds) =>
    (row && row.ts >= iso(at - offsetSeconds * 1000 - STALE_TOLERANCE_MS) ? row : null);

  // Likewise after: a row at or before t+60 that predates the fill is just the
  // fill's own snapshot again, and says nothing about the recovery.
  const after = (row) => (row && row.ts > job.ts ? row : null);

  return [
    job.ts, job.assetId, job.conditionId, job.wallet, job.side, job.price, job.size,
    store.fillOrdinal(job.assetId, job.side, job.ts, job.wallet),
    sample(minus60, 60)?.best_bid ?? null,
    sample(minus10, 10)?.best_bid ?? null,
    sample(minus1, 1)?.best_bid ?? null,
    sample(minus1, 1)?.best_ask ?? null,
    before?.size_at_002 ?? null,
    before?.depth_bid_total ?? null,
    before?.paired_bid ?? null,
    // The floor the twin token puts under this one, at the moment of the fill.
    before?.paired_ask === null || before?.paired_ask === undefined
      ? null
      : Math.round((1 - before.paired_ask) * 1e6) / 1e6,
    before?.book_sum ?? null,
    after(plus60)?.best_bid ?? null,
    after(plus300)?.best_bid ?? null,
    after(plus900)?.best_bid ?? null,
    store.sweepBefore(job.assetId, job.ts, iso(at - SWEEP_WINDOW_SECONDS * 1000)),
    store.sawBook(job.assetId, iso(at - 900_000), iso(at + 900_000)) ? 1 : 0,
    iso(now),
  ];
}

/** Column order of the fill_context table, for the export. */
export const FILL_CONTEXT_COLUMNS = [
  'fill_ts', 'asset_id', 'condition_id', 'wallet', 'side', 'price', 'size', 'fill_index',
  'bid_t_minus_60', 'bid_t_minus_10', 'bid_t_minus_1', 'ask_t_minus_1',
  'size_at_002_before', 'depth_before',
  'paired_bid_before', 'fair_lower_bound_before', 'book_sum_before',
  'bid_plus_60', 'bid_plus_300', 'bid_plus_900',
  'matched_sweep_id', 'snapshot_available', 'filled_at',
];
