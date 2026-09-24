// Gap #4 — the journal of what the logger saw.
//
// Every distribution drawn from collected markets describes the slice that got
// collected, not the strategy. Without a record of what was considered and why
// it was or was not subscribed to, "the bot never traded here" is
// indistinguishable from "we never watched here" — and that is precisely the
// denominator the fill rate needs.
//
// The journal only runs forward: it cannot say what was seen before it existed.

import { skipReason } from './classify.mjs';

export class UniverseJournal {
  /** condition id -> the journal row, so a market is written once and updated. */
  #seen = new Map();

  /**
   * Note every market a discovery round considered.
   *
   * Discovery records eligibility. Actual subscription/capacity/window decisions
   * update the row later; the raw schedule journal preserves their history.
   * `alsoSkipped` supplies the scheduler's reason when a market cannot be dated.
   *
   * @param {object[]} records  classified markets, tracked or not
   * @param {string} discoveredVia  which query surfaced them
   * @param {(record: object) => string|null} [alsoSkipped]  a further reason
   * @returns {object[]} rows for markets seen for the first time
   */
  observe(records, discoveredVia = 'gamma:startDate', alsoSkipped = null) {
    const fresh = [];
    for (const record of records) {
      if (this.#seen.has(record.conditionId)) continue;
      const reason = skipReason(record) ?? (alsoSkipped ? alsoSkipped(record) : null);
      const row = {
        ts: new Date().toISOString(),
        conditionId: record.conditionId,
        discoveredVia,
        subscribed: false,
        unsubscribedAt: null,
        reasonSkipped: reason ?? 'awaiting subscription window',
        question: record.question ?? '',
        sport: record.sport ?? '',
        level: record.level ?? '',
        kind: record.kind ?? '',
      };
      this.#seen.set(record.conditionId, row);
      fresh.push(row);
    }
    return fresh;
  }

  /** Actual scheduler decision; eligibility alone never means subscribed. */
  decision(conditionId, status, at = new Date().toISOString()) {
    const row = this.#seen.get(conditionId);
    if (!row) return null;
    const before = JSON.stringify(row);
    if (status === 'subscribed') {
      row.subscribed = true; // ever subscribed, not current socket health
      row.unsubscribedAt = null;
      row.reasonSkipped = '';
    } else if (!row.subscribed) row.reasonSkipped = status;
    if (status === 'resolved' || status === 'past the hold window' || status === 'window passed unwatched') {
      row.unsubscribedAt = at;
    }
    return JSON.stringify(row) === before ? null : row;
  }

  /** A market that closed or left the discovery window. */
  release(conditionId, at = new Date().toISOString()) {
    const row = this.#seen.get(conditionId);
    if (!row || row.unsubscribedAt) return null;
    row.unsubscribedAt = at;
    return row;
  }

  get size() {
    return this.#seen.size;
  }

  get subscribedCount() {
    return [...this.#seen.values()].filter((row) => row.subscribed).length;
  }

  /** Why markets were passed over, most common first. */
  skipCounts() {
    const counts = new Map();
    for (const row of this.#seen.values()) {
      if (row.subscribed) continue;
      counts.set(row.reasonSkipped, (counts.get(row.reasonSkipped) ?? 0) + 1);
    }
    return [...counts].sort((a, b) => b[1] - a[1]);
  }

  rows() {
    return [...this.#seen.values()];
  }
}

/** Column order for the `universe` table and its export. */
export const UNIVERSE_COLUMNS = ['ts', 'condition_id', 'discovered_via', 'subscribed',
  'unsubscribed_at', 'reason_skipped', 'question', 'sport', 'level', 'kind'];

export const universeRow = (row) => [
  row.ts, row.conditionId, row.discoveredVia, row.subscribed ? 1 : 0,
  row.unsubscribedAt, row.reasonSkipped, row.question, row.sport, row.level, row.kind,
];
