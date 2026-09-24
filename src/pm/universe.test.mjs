// The journal of what the logger considered.
//   node src/pm/universe.test.mjs
import assert from 'node:assert/strict';
import { classifyMarket, skipReason } from './classify.mjs';
import { DEFAULTS } from './config.mjs';
import { UniverseJournal, UNIVERSE_COLUMNS, universeRow } from './universe.mjs';

const market = (over = {}) => classifyMarket({
  conditionId: over.id ?? 'c1',
  question: over.question ?? 'Counter-Strike: A vs B - Map 1 Winner',
  slug: 's',
  events: [{ slug: over.slug ?? 'cs2-a-b-2026-08-25', title: 'Counter-Strike: A vs B' }],
  outcomes: '["A","B"]',
  clobTokenIds: over.tokens ?? '["t1","t2"]',
  enableOrderBook: over.enableOrderBook !== false,
}, DEFAULTS.disciplines);

// --- why a market was passed over -------------------------------------------
assert.equal(skipReason(market()), null, 'a mapped esports market is subscribed');
assert.match(skipReason(market({ slug: 'fed-decision-2026' })), /unmapped discipline: fed/);
assert.equal(skipReason(market({ enableOrderBook: false })), 'order book disabled');
assert.match(skipReason(market({ tokens: '["only-one"]' })), /expected 2 tokens, got 1/);

// --- the journal ------------------------------------------------------------
const journal = new UniverseJournal();
const fresh = journal.observe([
  market({ id: 'c1' }),
  market({ id: 'c2', slug: 'fed-decision-2026' }),
  market({ id: 'c3', enableOrderBook: false }),
]);
assert.equal(fresh.length, 3, 'everything considered is journalled, not just what was kept');
assert.equal(journal.size, 3);
assert.equal(journal.subscribedCount, 0, 'eligible is not yet subscribed');
assert.equal(journal.decision('c1','capacity').reasonSkipped, 'capacity');
assert.equal(journal.subscribedCount, 0);
assert.equal(journal.decision('c1','subscribed').subscribed, true);
assert.equal(journal.subscribedCount, 1);
assert.equal(journal.decision('c1','subscribed'), null, 'unchanged decisions are not repeated');

// The reason is the whole point: "the bot never traded here" and "we never
// looked here" are different claims about the same empty cell.
const reasons = Object.fromEntries(journal.skipCounts());
assert.equal(reasons['order book disabled'], 1);
assert.ok(Object.keys(reasons).some((r) => r.includes('unmapped discipline')));

// A second round re-sees the same markets and must not duplicate them.
assert.equal(journal.observe([market({ id: 'c1' })]).length, 0, 'a market is journalled once');
assert.equal(journal.size, 3);

// Closing a market records when it left, without losing that it was watched.
const released = journal.release('c1', '2026-08-25T18:00:00Z');
assert.equal(released.unsubscribedAt, '2026-08-25T18:00:00Z');
assert.equal(released.subscribed, true, 'it was subscribed, and that stays true');
assert.equal(journal.release('c1'), null, 'releasing twice changes nothing');
assert.equal(journal.release('unknown'), null);

// --- the stored row ---------------------------------------------------------
const row = universeRow(journal.rows().find((r) => r.conditionId === 'c1'));
assert.equal(row.length, UNIVERSE_COLUMNS.length, 'a row matches the table');
const at = Object.fromEntries(UNIVERSE_COLUMNS.map((name, i) => [name, row[i]]));
assert.equal(at.condition_id, 'c1');
assert.equal(at.subscribed, 1);
assert.equal(at.unsubscribed_at, '2026-08-25T18:00:00Z');
assert.equal(at.sport, 'esports_counter_strike');
assert.equal(at.reason_skipped, '');

const skipped = universeRow(journal.rows().find((r) => r.conditionId === 'c2'));
assert.equal(skipped[UNIVERSE_COLUMNS.indexOf('subscribed')], 0);
assert.match(skipped[UNIVERSE_COLUMNS.indexOf('reason_skipped')], /unmapped/);

console.log('all universe tests passed');
