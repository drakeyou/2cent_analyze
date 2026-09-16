// Feed tests against a fake socket: subscription, framing, chunking, and the
// reconnect path that decides whether the coverage denominator is honest.
//   node src/pm/ws.test.mjs
import assert from 'node:assert/strict';
import { BookFeed } from './ws.mjs';

const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/** Minimal stand-in for the browser WebSocket the feed expects. */
class FakeSocket {
  static instances = [];
  sent = [];
  closed = false;

  constructor(url) {
    this.url = url;
    this.listeners = new Map();
    FakeSocket.instances.push(this);
  }

  addEventListener(type, handler) {
    this.listeners.set(type, handler);
  }

  emit(type, event = {}) {
    this.listeners.get(type)?.(event);
  }

  send(data) {
    if (this.closed) throw new Error('socket is closed');
    this.sent.push(data);
  }

  close() {
    this.closed = true;
  }
}

function makeFeed(overrides = {}) {
  FakeSocket.instances = [];
  const messages = [];
  const gaps = [];
  const feed = new BookFeed({
    onMessage: (m) => messages.push(m),
    onGap: (g) => gaps.push(g),
    WebSocketImpl: FakeSocket,
    reconnectMinMs: 5,
    reconnectMaxMs: 40,
    keepaliveSeconds: 10,
    ...overrides,
  });
  return { feed, messages, gaps };
}

// --- subscription and framing ----------------------------------------------
const { feed, messages, gaps } = makeFeed();
assert.equal(feed.setAssets(['a', 'b']), true, 'a new set connects');
assert.equal(feed.connectionCount, 1);

const socket = FakeSocket.instances[0];
socket.emit('open');
assert.deepEqual(JSON.parse(socket.sent[0]), { assets_ids: ['a', 'b'], type: 'market' },
  'the subscription names every asset');

// The first frame is an array of snapshots, later frames are single objects.
socket.emit('message', { data: JSON.stringify([{ event_type: 'book', asset_id: 'a' }, { event_type: 'book', asset_id: 'b' }]) });
assert.equal(messages.length, 2, 'an array frame yields one message per element');
socket.emit('message', { data: JSON.stringify({ event_type: 'price_change', market: 'c1' }) });
assert.equal(messages.length, 3);
socket.emit('message', { data: 'PONG' });
assert.equal(messages.length, 3, 'keepalive traffic is not a book message');
socket.emit('message', { data: JSON.stringify({ no: 'event type' }) });
assert.equal(messages.length, 3, 'frames without an event_type are ignored');

// --- unchanged sets must not churn the sockets ------------------------------
assert.equal(feed.setAssets(['b', 'a']), false, 'the same set in another order is a no-op');
assert.equal(FakeSocket.instances.length, 1, 'no reconnect for an unchanged set');
assert.equal(feed.setAssets(['a', 'b', 'c']), true, 'a genuine change reconnects');
assert.equal(FakeSocket.instances.length, 2);
assert.ok(FakeSocket.instances[0].closed, 'the previous socket is closed');
feed.stop();

// --- chunking ---------------------------------------------------------------
const many = makeFeed({ assetsPerConnection: 2 });
many.feed.setAssets(['a', 'b', 'c', 'd', 'e']);
assert.equal(many.feed.connectionCount, 3, '5 assets over 3 sockets at 2 each');
assert.equal(FakeSocket.instances.length, 3);
FakeSocket.instances.forEach((s) => s.emit('open'));
const subscribed = FakeSocket.instances.flatMap((s) => JSON.parse(s.sent[0]).assets_ids);
assert.deepEqual(subscribed.sort(), ['a', 'b', 'c', 'd', 'e'], 'every asset lands on exactly one socket');
many.feed.stop();

// --- reconnect and the gap record -------------------------------------------
const dropped = makeFeed();
dropped.feed.setAssets(['a']);
const first = FakeSocket.instances[0];
first.emit('open');
assert.equal(dropped.gaps.length, 0, 'a clean first connect is not a gap');

first.emit('close');
await delay(30);
assert.ok(FakeSocket.instances.length >= 2, 'the feed reconnects on its own');
const second = FakeSocket.instances[1];
second.emit('open');
assert.equal(dropped.gaps.length, 1, 'the outage is recorded');
const [gap] = dropped.gaps;
assert.equal(gap.reason, 'reconnect');
assert.equal(gap.assets, 1);
assert.ok(gap.durationMs >= 5, `gap is timed, got ${gap.durationMs}ms`);
assert.ok(Date.parse(gap.endedAt) >= Date.parse(gap.startedAt), 'the window is ordered');
assert.deepEqual(JSON.parse(second.sent[0]).assets_ids, ['a'], 'the reconnect resubscribes');

// Backoff grows rather than hammering the endpoint.
const before = FakeSocket.instances.length;
second.emit('close');
await delay(8);
second.emit('close');
await delay(8);
assert.ok(FakeSocket.instances.length - before <= 2, 'repeated drops back off, not spin');

dropped.feed.stop();
const afterStop = FakeSocket.instances.length;
await delay(60);
assert.equal(FakeSocket.instances.length, afterStop, 'a stopped feed does not reconnect');

console.log('all feed tests passed');

// Complete frames and stream invalidation; late events from retired sockets
// must not clear a replacement book or schedule another reconnect.
const resets = [];
const batches = [];
const raw = makeFeed({ onBatch: (batch, meta) => batches.push([batch, meta]),
  onReset: (assets, reason) => resets.push([assets, reason]) });
raw.feed.setAssets(['a', 'b']);
const old = FakeSocket.instances[0];
old.emit('open');
old.emit('message', { data: JSON.stringify([
  { event_type: 'book', asset_id: 'a' }, { event_type: 'book', asset_id: 'b' },
]) });
assert.equal(batches.length, 1);
assert.equal(batches[0][0].length, 2);
assert.ok(Number.isFinite(batches[0][1].monotonicMs));
assert.ok(Number.isFinite(Date.parse(batches[0][1].receivedAt)));
old.emit('error');
assert.equal(resets.at(-1)[1], 'error');
assert.equal(old.closed, true);
await delay(30);
const replacement = FakeSocket.instances[1];
replacement.emit('open');
const resetCount = resets.length;
old.emit('close');
old.emit('message', { data: JSON.stringify({ event_type: 'book', asset_id: 'a' }) });
assert.equal(resets.length, resetCount);
assert.equal(batches.length, 1);
raw.feed.stop();
assert.equal(resets.at(-1)[1], 'unsubscribe');
