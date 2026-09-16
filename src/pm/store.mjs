// SQLite storage, one file per UTC day.
//
// node:sqlite ships with Node 22, so the collector needs no database driver and
// no build step. Writes are buffered and flushed inside a transaction: at ~100
// book rows a second, a transaction per row would spend all its time on fsync.
//
// The daily file keeps any single database a workable size (a day is roughly a
// gigabyte at 500 tracked tokens) and lets the analyzer pick a date range by
// opening only the files it needs.

import { DatabaseSync } from 'node:sqlite';
import { existsSync, mkdirSync, readdirSync } from 'node:fs';
import { join } from 'node:path';

// The API repeats history into successive daily files. Missing hashes cannot
// establish identity; equal-size anonymous prints must not be merged.
function tradeKey(row) {
  return row.tx_hash ? JSON.stringify([row.tx_hash.toLowerCase(),
    row.wallet?.toLowerCase(), row.asset_id, row.side, Number(row.size), Number(row.price)]) : null;
}

const SCHEMA = `
CREATE TABLE IF NOT EXISTS market_events (
  session_id TEXT, seq INTEGER, received_at TEXT, monotonic_ms REAL,
  connection_id TEXT, event_type TEXT, payload_json TEXT,
  PRIMARY KEY (session_id, seq)
);
CREATE INDEX IF NOT EXISTS market_events_received ON market_events (received_at);
CREATE TABLE IF NOT EXISTS quote_observations (
  session_id TEXT, frame_seq INTEGER, received_at TEXT, asset_id TEXT,
  condition_id TEXT, trigger TEXT, best_bid REAL, best_ask REAL,
  paired_bid REAL, paired_ask REAL, source_timestamp REAL, paired_source_timestamp REAL,
  crossed INTEGER, paired_crossed INTEGER, levels_json TEXT,
  PRIMARY KEY (session_id, frame_seq, asset_id)
);
CREATE INDEX IF NOT EXISTS quote_observations_asset_time
  ON quote_observations (asset_id, received_at);
CREATE TABLE IF NOT EXISTS markets (
  condition_id TEXT PRIMARY KEY, asset_id_a TEXT, asset_id_b TEXT,
  question TEXT, slug TEXT, event_slug TEXT, event_title TEXT,
  sport TEXT, level TEXT, kind TEXT, segment_kind TEXT, segment_no INTEGER,
  line REAL, team_a TEXT, team_b TEXT, outcome_a TEXT, outcome_b TEXT,
  end_date TEXT, tick_size REAL, min_size REAL,
  first_seen TEXT, last_seen TEXT,
  -- When play starts, and whether the book was actually seen while it was
  -- being played. observed_during_game is the self-check for the subscription
  -- window: it answers, without any analysis, whether the logger watched the
  -- match or only the hours before it.
  game_start_time TEXT, subscribed_at TEXT, unsubscribed_at TEXT,
  release_reason TEXT, observed_during_game INTEGER
);
CREATE TABLE IF NOT EXISTS book (
  ts TEXT, asset_id TEXT, condition_id TEXT, trigger TEXT,
  best_bid REAL, best_ask REAL, mid REAL,
  size_at_001 REAL, size_at_002 REAL, size_at_003 REAL, size_at_005 REAL,
  depth_bid_total REAL, depth_ask_total REAL, n_bid_levels INTEGER, spread REAL,
  -- The other outcome token of the same market. Both are subscribed, so this
  -- costs a map lookup and turns every snapshot into a fair-value estimate:
  -- fair_lower_bound is 1 - paired_ask, left to the reader because this table
  -- runs to tens of millions of rows. book_sum near 1 is a working market.
  paired_bid REAL, paired_ask REAL, fair_mid REAL, book_sum REAL,
  paired_stale_seconds REAL
);
CREATE TABLE IF NOT EXISTS sweeps (
  ts TEXT, asset_id TEXT, condition_id TEXT, rule TEXT,
  bid_before REAL, bid_after REAL, size_consumed REAL, levels_crossed INTEGER,
  depth_before REAL, depth_after REAL,
  -- The gg.bet side is written here, at the moment of the event, from the
  -- resident cache of last quotes. Looking it up afterwards from the joined
  -- table found a quote for 1 sweep in 5000.
  sweep_id TEXT, ggbet_fair REAL, dislocation_ratio REAL,
  seconds_since_ggbet_quote REAL, ggbet_market_state TEXT,
  -- What levels_crossed misses: levels thinned rather than cleared.
  levels_touched INTEGER, size_eaten_partial REAL, n_bid_levels_before INTEGER,
  -- The twin token, in full. Unlike the book table this is thousands of rows,
  -- so the derived bounds are stored rather than left to be recomputed.
  paired_asset_id TEXT, paired_bid REAL, paired_ask REAL, paired_ask_size REAL,
  fair_lower_bound REAL, fair_upper_bound REAL, fair_mid REAL, book_sum REAL,
  internal_dislocation REAL, paired_stale_seconds REAL
);
-- What the best bid reached in the minutes after a sweep: the outcome the whole
-- study is about. Filled from the snapshots already being collected, by a
-- running maximum per open horizon, so it costs no query and no request.
CREATE TABLE IF NOT EXISTS sweep_followups (
  sweep_id TEXT, asset_id TEXT, condition_id TEXT, horizon INTEGER,
  high_bid REAL, filled_at TEXT, resolved_before_horizon INTEGER,
  -- The book never moved at all over the horizon. A market that died at the
  -- moment of the collapse — a total decided, a handicap closed — reads as a
  -- sweep that never recovered, and drags the statistics with it. This says so
  -- without needing to know when the market resolved, which is knowledge that
  -- arrives too late to be useful.
  book_frozen INTEGER,
  UNIQUE (sweep_id, horizon)
);
-- The book around a fill by a studied wallet: the ground truth the collection
-- exists for. Filled by a deferred worker once the horizons after the fill have
-- passed, from snapshots already stored.
CREATE TABLE IF NOT EXISTS fill_context (
  fill_ts TEXT, asset_id TEXT, condition_id TEXT, wallet TEXT, side TEXT,
  price REAL, size REAL, fill_index INTEGER,
  bid_t_minus_60 REAL, bid_t_minus_10 REAL, bid_t_minus_1 REAL, ask_t_minus_1 REAL,
  size_at_002_before REAL, depth_before REAL,
  paired_bid_before REAL, fair_lower_bound_before REAL, book_sum_before REAL,
  bid_plus_60 REAL, bid_plus_300 REAL, bid_plus_900 REAL,
  matched_sweep_id TEXT, snapshot_available INTEGER, filled_at TEXT,
  UNIQUE (fill_ts, asset_id, wallet, side, price, size)
);
CREATE TABLE IF NOT EXISTS joined (
  ts TEXT, condition_id TEXT, asset_id TEXT,
  pm_best_bid REAL, pm_best_ask REAL, pm_mid REAL,
  ggbet_fair REAL, ggbet_market_state TEXT, score TEXT, segment_score TEXT,
  dislocation_ratio REAL, seconds_since_ggbet_quote REAL
);
CREATE TABLE IF NOT EXISTS wallets (
  ts TEXT, address TEXT, activity_id TEXT, type TEXT, side TEXT,
  asset_id TEXT, condition_id TEXT, price REAL, size REAL, tx_hash TEXT,
  UNIQUE (address, activity_id)
);
CREATE TABLE IF NOT EXISTS trades (
  ts TEXT, condition_id TEXT, asset_id TEXT, wallet TEXT, side TEXT,
  price REAL, size REAL, role TEXT, tx_hash TEXT,
  UNIQUE (tx_hash, wallet, asset_id, side, size, price)
);
CREATE TABLE IF NOT EXISTS trade_scans (
  ts TEXT, condition_id TEXT, total_trades INTEGER, wallet_trades INTEGER,
  taker_share REAL, pages INTEGER, truncated INTEGER
);
CREATE TABLE IF NOT EXISTS universe (
  ts TEXT, condition_id TEXT PRIMARY KEY, discovered_via TEXT, subscribed INTEGER,
  unsubscribed_at TEXT, reason_skipped TEXT, question TEXT, sport TEXT,
  level TEXT, kind TEXT
);
CREATE TABLE IF NOT EXISTS gaps (
  started_at TEXT, ended_at TEXT, duration_ms INTEGER, reason TEXT,
  assets_resubscribed INTEGER
);
CREATE INDEX IF NOT EXISTS book_asset_ts ON book (asset_id, ts);
CREATE INDEX IF NOT EXISTS sweeps_asset_ts ON sweeps (asset_id, ts);
CREATE INDEX IF NOT EXISTS joined_cond_ts ON joined (condition_id, ts);
CREATE INDEX IF NOT EXISTS trades_asset_ts ON trades (asset_id, ts);
CREATE INDEX IF NOT EXISTS followups_sweep ON sweep_followups (sweep_id);
CREATE INDEX IF NOT EXISTS fill_context_asset ON fill_context (asset_id, fill_ts);
-- Coverage counts heartbeats and the fill rate filters on the best bid. Without
-- these both queries scan every row and sort it in a temp b-tree, which on a
-- day's collection is minutes per question.
CREATE INDEX IF NOT EXISTS book_trigger_asset_ts ON book (trigger, asset_id, ts);
CREATE INDEX IF NOT EXISTS book_bid_asset_ts ON book (best_bid, asset_id, ts);
`;

const INSERTS = {
  market_events: `INSERT INTO market_events
    (session_id, seq, received_at, monotonic_ms, connection_id, event_type, payload_json)
    VALUES (?,?,?,?,?,?,?)`,
  quote_observations: `INSERT INTO quote_observations
    (session_id, frame_seq, received_at, asset_id, condition_id, trigger, best_bid,
     best_ask, paired_bid, paired_ask, source_timestamp, paired_source_timestamp,
     crossed, paired_crossed, levels_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
  book: `INSERT INTO book VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
  sweeps: `INSERT INTO sweeps (ts, asset_id, condition_id, rule, bid_before, bid_after,
    size_consumed, levels_crossed, depth_before, depth_after,
    levels_touched, size_eaten_partial, n_bid_levels_before,
    sweep_id, ggbet_fair, dislocation_ratio, seconds_since_ggbet_quote,
    ggbet_market_state,
    paired_asset_id, paired_bid, paired_ask, paired_ask_size,
    fair_lower_bound, fair_upper_bound, fair_mid, book_sum,
    internal_dislocation, paired_stale_seconds)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
  sweep_followups: `INSERT OR IGNORE INTO sweep_followups
    (sweep_id, asset_id, condition_id, horizon, high_bid, filled_at,
     resolved_before_horizon, book_frozen)
    VALUES (?,?,?,?,?,?,?,?)`,
  fill_context: `INSERT OR IGNORE INTO fill_context VALUES
    (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
  joined: `INSERT INTO joined VALUES (?,?,?,?,?,?,?,?,?,?,?,?)`,
  wallets: `INSERT OR IGNORE INTO wallets VALUES (?,?,?,?,?,?,?,?,?,?)`,
  trades: `INSERT OR IGNORE INTO trades VALUES (?,?,?,?,?,?,?,?,?)`,
  trade_scans: `INSERT INTO trade_scans VALUES (?,?,?,?,?,?,?)`,
  universe: `INSERT INTO universe VALUES (?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(condition_id) DO UPDATE SET unsubscribed_at = excluded.unsubscribed_at`,
  gaps: `INSERT INTO gaps VALUES (?,?,?,?,?)`,
};

// Named rather than positional: the row grew, and a positional insert against a
// table that gained a column fails on every write with nothing to point at.
const MARKET_COLUMNS = ['condition_id', 'asset_id_a', 'asset_id_b', 'question', 'slug',
  'event_slug', 'event_title', 'sport', 'level', 'kind', 'segment_kind', 'segment_no',
  'line', 'team_a', 'team_b', 'outcome_a', 'outcome_b', 'end_date', 'tick_size',
  'min_size', 'first_seen', 'last_seen', 'game_start_time', 'subscribed_at',
  'unsubscribed_at', 'release_reason', 'observed_during_game'];

const MARKET_UPSERT = `
INSERT INTO markets (${MARKET_COLUMNS.join(', ')})
VALUES (${MARKET_COLUMNS.map(() => '?').join(',')})
ON CONFLICT(condition_id) DO UPDATE SET last_seen = excluded.last_seen,
  end_date = excluded.end_date, tick_size = excluded.tick_size, min_size = excluded.min_size,
  game_start_time = excluded.game_start_time, subscribed_at = excluded.subscribed_at,
  unsubscribed_at = excluded.unsubscribed_at, release_reason = excluded.release_reason,
  -- Never let a re-registration clear the flag: it is set once, live, and the
  -- daily rollover re-writes every tracked market into a fresh file.
  observed_during_game = max(coalesce(markets.observed_during_game, 0),
                             coalesce(excluded.observed_during_game, 0))`;

// Columns added after the first collections. Daily files are created with
// CREATE TABLE IF NOT EXISTS, so a database opened from before this change
// keeps the old shape and every insert against it would fail.
const COLUMN_ADDITIONS = {
  markets: [['game_start_time', 'TEXT'], ['subscribed_at', 'TEXT'],
    ['unsubscribed_at', 'TEXT'], ['release_reason', 'TEXT'],
    ['observed_during_game', 'INTEGER']],
  sweeps: [['sweep_id', 'TEXT'], ['ggbet_fair', 'REAL'], ['dislocation_ratio', 'REAL'],
    ['seconds_since_ggbet_quote', 'REAL'], ['ggbet_market_state', 'TEXT'],
    ['levels_touched', 'INTEGER'], ['size_eaten_partial', 'REAL'],
    ['n_bid_levels_before', 'INTEGER'], ['paired_asset_id', 'TEXT'],
    ['paired_bid', 'REAL'], ['paired_ask', 'REAL'], ['paired_ask_size', 'REAL'],
    ['fair_lower_bound', 'REAL'], ['fair_upper_bound', 'REAL'], ['fair_mid', 'REAL'],
    ['book_sum', 'REAL'], ['internal_dislocation', 'REAL'],
    ['paired_stale_seconds', 'REAL']],
  book: [['paired_bid', 'REAL'], ['paired_ask', 'REAL'], ['fair_mid', 'REAL'],
    ['book_sum', 'REAL'], ['paired_stale_seconds', 'REAL']],
  sweep_followups: [['book_frozen', 'INTEGER']],
};

export const utcDay = (date = new Date()) => date.toISOString().slice(0, 10);

/**
 * Markets still worth scheduling, read back from the daily files.
 *
 * The subscription window is held in memory, and a market is only visible to
 * discovery for about an hour after it is created — 11 to 15 hours before its
 * game. So a restart in between would drop every match scheduled for the rest
 * of the day and never see it again: Gamma has moved on, and nothing would say
 * anything was lost. The registry already holds everything needed to rebuild
 * the record, so it is read back at startup.
 *
 * @param {string} dir  the daily-database directory
 * @param {object} [options]
 * @param {number} [options.lookbackHours]  how far back a game may have started
 * @param {number} [options.files]  how many recent daily files to read
 * @returns {object[]} classified-market records, as the schedule expects them
 */
/**
 * Where the studied wallets have actually been trading, as shares by discipline.
 *
 * Used to hand out subscription slots under a cap. The alternative — letting
 * whatever the discovery page happened to return decide — put 60% of the
 * registry into CS2 during days the wallets worked in LoL and tennis, and only
 * 2 of the 17 markets they traded in were being watched.
 *
 * @returns {Map<string, number>} sport -> share of wallet fills, summing to 1
 */
export function walletDisciplineShares(dir, { days = 7, now = Date.now() } = {}) {
  let names;
  try {
    names = readdirSync(dir).filter((name) => /^pm-\d{4}-\d{2}-\d{2}\.sqlite$/.test(name)).sort();
  } catch {
    return new Map();
  }

  const since = new Date(now - days * 24 * 3600 * 1000).toISOString();
  const counts = new Map();
  const seen = new Set();
  let total = 0;
  for (const name of names.slice(-days)) {
    let db;
    try {
      db = new DatabaseSync(join(dir, name), { readOnly: true });
      const rows = db.prepare(`
        SELECT t.*, m.sport AS sport
        FROM trades t JOIN markets m ON m.condition_id = t.condition_id
        WHERE t.ts >= ? AND m.sport IS NOT NULL
      `).all(since);
      for (const row of rows) {
        const key = tradeKey(row);
        if (key !== null && seen.has(key)) continue;
        if (key !== null) seen.add(key);
        counts.set(row.sport, (counts.get(row.sport) ?? 0) + 1);
        total++;
      }
    } catch {
      // A file from before these tables existed has nothing to contribute.
    } finally {
      db?.close();
    }
  }
  if (!total) return new Map();
  return new Map([...counts].map(([sport, fills]) => [sport, fills / total]));
}

export function restoreSchedulableMarkets(dir, { lookbackHours = 12, files = 3, now = Date.now() } = {}) {
  let names;
  try {
    names = readdirSync(dir).filter((name) => /^pm-\d{4}-\d{2}-\d{2}\.sqlite$/.test(name)).sort();
  } catch {
    return [];
  }

  const since = new Date(now - lookbackHours * 3600 * 1000).toISOString();
  const byCondition = new Map();
  for (const name of names.slice(-files)) {
    let db;
    try {
      db = new DatabaseSync(join(dir, name), { readOnly: true });
      const rows = db.prepare(`
        SELECT * FROM markets
        WHERE game_start_time IS NOT NULL AND game_start_time > ?
          AND unsubscribed_at IS NULL
      `).all(since);
      // Later files win: they carry the newest view of the same market.
      for (const row of rows) byCondition.set(row.condition_id, row);
    } catch {
      // A file from before these columns existed has nothing to restore.
    } finally {
      db?.close();
    }
  }

  return [...byCondition.values()].map((row) => ({
    conditionId: row.condition_id,
    tokens: [row.asset_id_a, row.asset_id_b].filter(Boolean),
    question: row.question ?? '',
    slug: row.slug,
    eventSlug: row.event_slug,
    eventTitle: row.event_title,
    sport: row.sport,
    level: row.level,
    kind: row.kind,
    segmentKind: row.segment_kind,
    segmentNo: row.segment_no,
    line: row.line,
    teams: row.team_a || row.team_b ? [row.team_a, row.team_b] : null,
    outcomes: [row.outcome_a, row.outcome_b].filter((value) => value !== null),
    endDate: row.end_date,
    gameStartTime: row.game_start_time,
    tickSize: row.tick_size ?? 0.001,
    minSize: row.min_size ?? 0,
    enableOrderBook: true,
  })).filter((record) => record.tokens.length === 2);
}

export class Store {
  #dir;
  #day = null;
  #db = null;
  #statements = null;
  #market = null;
  #observed = null;
  /** Yesterday's file, read-only, so a fill near midnight still has a context. */
  #history = null;
  #buffers = { book: [], sweeps: [], sweep_followups: [], fill_context: [], joined: [],
    wallets: [], trades: [], trade_scans: [], universe: [], gaps: [],
    market_events: [], quote_observations: [] };
  #flushAt;
  #maxBuffered;

  /**
   * @param {string} dir  directory for the daily database files
   * @param {object} [options]
   * @param {number} [options.flushMs]
   * @param {number} [options.maxBuffered]
   * @param {() => Date} [options.now]  injectable clock, so rotation is testable
   * @param {(day: string) => void} [options.onRotate]  called after a new day opens
   */
  constructor(dir, { flushMs = 1000, maxBuffered = 2000, now = () => new Date(), onRotate = null } = {}) {
    this.#dir = dir;
    this.#maxBuffered = maxBuffered;
    this.now = now;
    this.onRotate = onRotate;
    mkdirSync(dir, { recursive: true });
    this.#open(utcDay(now()));
    this.#flushAt = setInterval(() => this.flush(), flushMs);
    this.#flushAt.unref?.();
  }

  #open(day) {
    this.#history?.close();
    this.#history = null;
    const previous = join(this.#dir, `pm-${new Date(Date.parse(`${day}T00:00:00Z`) - 86400000)
      .toISOString().slice(0, 10)}.sqlite`);
    if (existsSync(previous)) {
      try {
        this.#history = new DatabaseSync(previous, { readOnly: true });
      } catch {
        this.#history = null;
      }
    }
    this.#db = new DatabaseSync(join(this.#dir, `pm-${day}.sqlite`));
    this.#db.exec('PRAGMA journal_mode = WAL');
    this.#db.exec('PRAGMA synchronous = NORMAL');
    this.#db.exec(SCHEMA);
    this.#migrate();
    this.#statements = Object.fromEntries(
      Object.entries(INSERTS).map(([table, sql]) => [table, this.#db.prepare(sql)]),
    );
    this.#market = this.#db.prepare(MARKET_UPSERT);
    this.#observed = this.#db.prepare(
      'UPDATE markets SET observed_during_game = 1 WHERE condition_id = ?');
    this.#day = day;
  }

  /** Bring a database written by an older build up to the current shape. */
  #migrate() {
    for (const [table, additions] of Object.entries(COLUMN_ADDITIONS)) {
      const present = new Set(this.#db.prepare(`PRAGMA table_info(${table})`)
        .all().map((column) => column.name));
      for (const [name, type] of additions) {
        if (present.has(name)) continue;
        this.#db.exec(`ALTER TABLE ${table} ADD COLUMN ${name} ${type}`);
      }
    }
  }

  /**
   * Roll over at UTC midnight so no file grows without bound.
   *
   * The new file starts with an empty `markets` table, so anything written
   * before the next discovery round would have no discipline to join against.
   * onRotate is where the caller re-registers what it is tracking.
   */
  #rotateIfNeeded() {
    const today = utcDay(this.now());
    if (today === this.#day) return;
    this.#drain();
    this.#db.close();
    this.#open(today);
    this.onRotate?.(today);
  }

  get path() {
    return join(this.#dir, `pm-${this.#day}.sqlite`);
  }

  /** Rotate before a journal sequence number is reserved. */
  prepare() { this.#rotateIfNeeded(); }

  /** Queue a row; it reaches disk on the next flush. */
  add(table, row) {
    this.#buffers[table].push(row);
    if (this.#buffers[table].length >= this.#maxBuffered) this.flush();
  }

  /**
   * @param {object} record  a classified market
   * @param {string} [seenAt]
   * @param {object} [lifecycle]  the schedule entry, where there is one
   */
  upsertMarket(record, seenAt = new Date().toISOString(), lifecycle = null) {
    this.#rotateIfNeeded();
    const [a, b] = record.tokens;
    this.#market.run(
      record.conditionId, a ?? null, b ?? null, record.question, record.slug,
      record.eventSlug, record.eventTitle, record.sport, record.level, record.kind,
      record.segmentKind, record.segmentNo, record.line,
      record.teams?.[0] ?? null, record.teams?.[1] ?? null,
      record.outcomes?.[0] ?? null, record.outcomes?.[1] ?? null,
      record.endDate, record.tickSize, record.minSize, seenAt, seenAt,
      lifecycle?.gameStart ? new Date(lifecycle.gameStart).toISOString() : null,
      lifecycle?.subscribedAt ?? null, lifecycle?.releasedAt ?? null,
      lifecycle?.releaseReason ?? null, lifecycle?.observedDuringGame ? 1 : 0,
    );
  }

  /**
   * Mark that this market's book was seen while the match was being played.
   *
   * Written straight through rather than buffered: it happens once per market,
   * and the flag is the one thing a person checks to know the window is right.
   */
  markObserved(conditionId) {
    this.#rotateIfNeeded();
    this.#observed.run(conditionId);
  }

  /** Write everything buffered in one transaction per table. */
  flush() {
    this.#rotateIfNeeded();
    this.#drain();
  }

  #drain() {
    for (const [table, rows] of Object.entries(this.#buffers)) {
      if (!rows.length) continue;
      const statement = this.#statements[table];
      this.#db.exec('BEGIN');
      try {
        for (const row of rows) statement.run(...row);
        this.#db.exec('COMMIT');
      } catch (err) {
        this.#db.exec('ROLLBACK');
        console.error(`flush to ${table} failed: ${err.message}`);
      }
      rows.length = 0;
    }
  }

  /**
   * Run a read against today's file and yesterday's, newest answer first.
   *
   * The context around a fill reaches fifteen minutes each side of it, and the
   * daily rollover happens in the middle of the night while matches are on. A
   * fill at 23:50 would otherwise be written up from an empty database and
   * marked as never observed, which is the one thing the flag exists to rule
   * out.
   */
  #lookback(sql, params) {
    for (const db of [this.#db, this.#history]) {
      if (!db) continue;
      try {
        const row = db.prepare(sql).get(...params);
        if (row) return row;
      } catch {
        // A file from before this column existed has nothing to say.
      }
    }
    return null;
  }

  /** The newest book snapshot for an asset at or before an instant. */
  bookAt(assetId, ts) {
    return this.#lookback(`
      SELECT ts, best_bid, best_ask, size_at_002, depth_bid_total,
             paired_bid, paired_ask, book_sum
      FROM book WHERE asset_id = ? AND ts <= ? ORDER BY ts DESC LIMIT 1
    `, [assetId, ts]);
  }

  /** The newest sweep on this asset inside a window before an instant. */
  sweepBefore(assetId, ts, since) {
    return this.#lookback(`
      SELECT sweep_id FROM sweeps
      WHERE asset_id = ? AND ts <= ? AND ts >= ? ORDER BY ts DESC LIMIT 1
    `, [assetId, ts, since])?.sweep_id ?? null;
  }

  /** Was this asset's book being recorded anywhere in the window? */
  sawBook(assetId, from, to) {
    return this.#lookback(
      'SELECT 1 AS seen FROM book WHERE asset_id = ? AND ts >= ? AND ts <= ? LIMIT 1',
      [assetId, from, to]) !== null;
  }

  /**
   * Which fill this is within its position, counting the same side only.
   *
   * Read back from what is stored rather than counted in memory: the trade log
   * is pulled in pages and deduplicated on insert, so a running counter would
   * be wrong after a restart or a backfill.
   */
  fillOrdinal(assetId, side, ts, wallet = null) {
    let total = 0;
    const seen = new Set();
    for (const db of [this.#history, this.#db]) {
      if (!db) continue;
      try {
        const rows = db.prepare(
          'SELECT * FROM trades WHERE asset_id = ? AND side = ? AND ts <= ?' +
          (wallet ? ' AND lower(wallet) = ?' : ''),
        ).all(assetId, side, ts, ...(wallet ? [wallet.toLowerCase()] : []));
        for (const row of rows) {
          const key = tradeKey(row);
          if (key !== null && seen.has(key)) continue;
          if (key !== null) seen.add(key);
          total++;
        }
      } catch {
        // Same as above: an older file may not carry the table at all.
      }
    }
    return total;
  }

  /** Maker/taker split of what has been stored, for the status line. */
  roleCounts() {
    const rows = this.#db.prepare('SELECT role, count(*) AS c FROM trades GROUP BY role').all();
    return Object.fromEntries(rows.map((r) => [r.role, r.c]));
  }

  count(table) {
    return this.#db.prepare(`SELECT count(*) AS c FROM ${table}`).get().c;
  }

  close() {
    clearInterval(this.#flushAt);
    this.flush();
    this.#history?.close();
    this.#db.close();
  }
}
