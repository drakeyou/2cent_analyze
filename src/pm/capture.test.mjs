import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { MarketCapture } from './capture.mjs';
import { BookState } from './book.mjs';
import { Store } from './store.mjs';

const journal = [];
const books = new Map();
const seen = [];
const forgotten = [];
const capture = new MarketCapture({
  store: { add: (table, row) => journal.push({ table, row }) }, books,
  pairOf: asset => asset === 'a' ? 'b' : 'a',
  onBook: (book, before) => seen.push([book.assetId, before, books.get('b').bestAsk]),
  onForget: asset => forgotten.push(asset), sessionId: 'test',
});
const full = (asset, bid, ask) => ({ event_type: 'book', asset_id: asset,
  market: 'market', timestamp: '1000', tick_size: '.001',
  bids: [{ price: bid, size: '200' }, { price: '.011', size: '500' }],
  asks: [{ price: ask, size: '300' }] });
capture.processBatch([full('a', '.7', '.8'), full('b', '.2', '.3')]);
assert.equal(seen.length, 2);
assert.equal(seen[0][2], .3, 'initial paired snapshot is applied before callbacks');
assert.equal(seen[0][1], null, 'first snapshot must not trigger a sweep');
capture.processBatch([{ event_type: 'price_change', timestamp: '2000', price_changes: [
  { asset_id: 'a', price: '.7', size: '0', side: 'BUY' },
  { asset_id: 'a', price: '.02', size: '800', side: 'BUY' },
  { asset_id: 'b', price: '.3', size: '0', side: 'SELL' },
  { asset_id: 'b', price: '.98', size: '2000', side: 'SELL' },
] }]);
assert.equal(seen.length, 4, 'one observation per asset, not per changed level');
assert.equal(seen[2][2], .98, 'a sees b after the whole batch, not the old .3 ask');
const quote = journal.filter(r => r.table === 'quote_observations')[2].row;
assert.equal(quote[9], .98);
assert.deepEqual(JSON.parse(quote[14]).find(r => r[0] === .011), [.011, 500, 0, 800, 16]);

const beforePrint = books.get('a').snapshot();
const print = { event_type: 'last_trade_price', asset_id: 'a', price: '.02',
  size: '800', side: 'SELL', timestamp: '2050' };
capture.processBatch([print]);
assert.deepEqual(books.get('a').snapshot(), beforePrint, 'a trade print is not a book delta');
assert.deepEqual(JSON.parse(journal.at(-1).row[6]), [print], 'retain complete trade prints');
capture.checkpoint();
assert.equal(books.get('a').lastUpdate, 2000, 'checkpoints do not refresh source age');
const anchor = JSON.parse(journal.filter(r => r.table === 'market_events').at(-1).row[6]);
const replay = new BookState('a');
replay.applyBook(anchor.find(b => b.asset_id === 'a'));
assert.deepEqual(replay.snapshot(), beforePrint, 'checkpoint roundtrip retains all levels');
capture.reset(['a'], 'error');
assert.deepEqual(forgotten, ['a']);
capture.processBatch([{ event_type: 'price_change', timestamp: '3000', price_changes: [
  { asset_id: 'a', price: '.02', size: '900', side: 'BUY' },
] }]);
assert.equal(books.has('a'), false, 'a partial delta cannot initialise a book after a gap');
capture.processBatch([full('a', '.02', '.03')]);
assert.equal(seen.at(-1)[1], null, 'reconnect snapshot is not compared with pre-gap depth');

// Actual SQLite path, including a midnight rotate before the next frame.
const dir = mkdtempSync(join(tmpdir(), 'capture-'));
let clock = new Date('2026-09-16T23:59:59Z');
let saved;
const store = new Store(dir, { now: () => clock, onRotate: () => saved.checkpoint() });
try {
  saved = new MarketCapture({ store, books: new Map(), sessionId: 'rotation' });
  saved.processBatch([full('a', '.9', '.91')]);
  const fill = ['2026-09-16T23:59:59Z', 'market', 'a', '0xa', 'BUY', .9, 200, 'maker', '0xabc'];
  store.add('trades', fill);
  clock = new Date('2026-09-17T00:00:01Z');
  saved.processBatch([{ event_type: 'price_change', timestamp: '4000', price_changes: [
    { asset_id: 'a', price: '.9', size: '0', side: 'BUY' },
  ] }]);
  store.add('trades', fill);
  store.add('trades', [...fill.slice(0, 3), '0xb', ...fill.slice(4)]);
  store.flush();
  assert.equal(store.fillOrdinal('a', 'BUY', '2026-09-17T00:00:02Z', '0xA'), 1,
    'daily copies are deduplicated and another wallet cannot inflate the ordinal');
  const db = new DatabaseSync(join(dir, 'pm-2026-09-17.sqlite'), { readOnly: true });
  try {
    const rows = db.prepare('SELECT seq, event_type, payload_json FROM market_events ORDER BY seq').all();
    assert.deepEqual(rows.map(r => r.event_type), ['checkpoint', 'frame']);
    assert.deepEqual(rows.map(r => r.seq), [2, 3], 'the anchor precedes the midnight delta');
    assert.equal(JSON.parse(rows[0].payload_json)[0].bids[0].price, .9);
    assert.equal(db.prepare('SELECT count(*) AS n FROM quote_observations').get().n, 2);
  } finally { db.close(); }
} finally {
  store.close();
  rmSync(dir, { recursive: true, force: true });
}
console.log('all capture tests passed');
