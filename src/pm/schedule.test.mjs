// Subscription-window tests. The clock is passed in everywhere, so a whole
// match is played out in a few lines with no timers.
//   node src/pm/schedule.test.mjs
import assert from 'node:assert/strict';
import { MarketSchedule, gameStartOf, parseTimestamp } from './schedule.mjs';
import { BookState, pairedView } from './book.mjs';

const MINUTE = 60 * 1000;
const HOUR = 60 * MINUTE;

// --- timestamps ------------------------------------------------------------
// gameStartTime comes back as "2026-09-01 07:30:00+00": a space where ISO has a
// T and a two-digit offset. Read as local time it moves the whole window by the
// machine's offset, and that would be silent.
assert.equal(parseTimestamp('2026-09-01 07:30:00+00'), Date.parse('2026-09-01T07:30:00Z'));
assert.equal(parseTimestamp('2026-09-01T07:30:00Z'), Date.parse('2026-09-01T07:30:00Z'));
assert.equal(parseTimestamp('2026-09-01 07:30:00'), Date.parse('2026-09-01T07:30:00Z'),
  'a bare Polymarket timestamp is UTC');
assert.equal(parseTimestamp('2026-09-01 07:30:00-0730'), Date.parse('2026-09-01T07:30:00-07:30'));
assert.equal(parseTimestamp(null), null);
assert.equal(parseTimestamp(''), null);
assert.equal(parseTimestamp('not a date'), null);

// --- dating a match --------------------------------------------------------
const game = Date.parse('2026-09-01T12:00:00Z');
const cs2 = (over = {}) => ({
  conditionId: 'c1', sport: 'esports_counter_strike', tokens: ['a', 'b'],
  question: 'Map 1 Winner: A vs B',
  gameStartTime: '2026-09-01 12:00:00+00',
  endDate: '2026-09-01T18:00:00Z', // the CS2 template: game + 6h
  ...over,
});

assert.deepEqual(gameStartOf(cs2(), {}, game - HOUR), { at: game, source: 'game_start_time' });

// Without a game start, the resolution deadline minus a typical match is the
// best available guess — and for CS2 it lands exactly on the game.
assert.deepEqual(
  gameStartOf(cs2({ gameStartTime: null }), {}, game - HOUR),
  { at: game, source: 'end_date_minus_typical' });

// The tennis template sets the deadline a week out, so backing a match length
// off it would schedule the subscription days after the match was played.
assert.equal(
  gameStartOf({ sport: 'tennis', gameStartTime: null, endDate: '2026-09-08T07:30:00Z', tokens: [] },
    {}, Date.parse('2026-09-01T00:00:00Z')),
  null, 'a deadline a week out does not date a match');

// --- the window ------------------------------------------------------------
const schedule = new MarketSchedule({ leadMinutes: 10, holdHours: { esports_counter_strike: 6 } });

// Discovery sees the market 15 hours early. That is not a reason to watch it.
const discovered = game - 15 * HOUR;
const { scheduled, skipped } = schedule.observe([cs2()], discovered);
assert.equal(scheduled.length, 1);
assert.equal(skipped.length, 0);
assert.deepEqual(schedule.refresh(discovered).added, [], 'not subscribed on discovery');
assert.equal(schedule.liveSize, 0);
assert.equal(schedule.pendingSize, 1);
assert.equal(schedule.decisions(discovered)[0].status, 'awaiting subscription window');

// This is the regression that made the whole collection unusable: the market
// falls out of Gamma's page about an hour after it appears, and used to be
// unsubscribed for it. The schedule is not told again, and holds it anyway.
schedule.refresh(discovered + HOUR);
assert.equal(schedule.pendingSize, 1, 'leaving the discovery window is not an event');

// Eleven minutes before the game: still nothing. Ten: on.
assert.equal(schedule.refresh(game - 11 * MINUTE).added.length, 0);
const opened = schedule.refresh(game - 10 * MINUTE);
assert.deepEqual(opened.added.map((e) => e.conditionId), ['c1']);
assert.equal(schedule.liveSize, 1);
assert.equal(schedule.decisions(game)[0].status, 'subscribed');
assert.deepEqual(schedule.assetIds(), ['a', 'b'], 'both tokens are subscribed');
assert.equal(schedule.conditionOf('b'), 'c1');

// Both tokens of a binary market are watched, and their prices are tied:
// P(a) + P(b) = 1. The link between them is what lets a fair value be read off
// the twin's book instead of an outside feed, so it is kept with the
// subscription and dropped with it.
assert.equal(schedule.pairOf('a'), 'b');
assert.equal(schedule.pairOf('b'), 'a');
assert.equal(schedule.pairOf('never-seen'), null);

// Through the match, a quiet book is still a watched book.
assert.deepEqual(schedule.refresh(game + 2 * HOUR).removed, [], 'silence does not unsubscribe');
assert.equal(schedule.liveSize, 1);

// The self-check flag: only inside the game, and only the first time.
assert.equal(schedule.noteObserved('c1', game - 30 * MINUTE), false, 'before the first point');
assert.equal(schedule.noteObserved('c1', game + MINUTE), true);
assert.equal(schedule.noteObserved('c1', game + 2 * MINUTE), false, 'written once');
assert.equal(schedule.entry('c1').observedDuringGame, true);

// Past the hold, it goes.
const closed = schedule.refresh(game + 6 * HOUR + MINUTE);
assert.deepEqual(closed.removed.map((e) => e.conditionId), ['c1']);
assert.equal(schedule.entry('c1').releaseReason, 'past the hold window');
assert.equal(schedule.liveSize, 0);
assert.deepEqual(schedule.assetIds(), []);
assert.equal(schedule.pairOf('a'), null, 'the pair goes when the subscription goes');

// A finished match is not rescheduled when Gamma serves it again.
assert.equal(schedule.observe([cs2()], game + 7 * HOUR).scheduled.length, 0);
assert.equal(schedule.refresh(game + 7 * HOUR).added.length, 0);

// --- resolution ends it early ---------------------------------------------
const early = new MarketSchedule({ leadMinutes: 10 });
early.observe([cs2({ conditionId: 'c2' })], game - HOUR);
early.refresh(game);
assert.equal(early.liveSize, 1);
assert.equal(early.markResolved('c2'), true);
const done = early.refresh(game + 90 * MINUTE);
assert.deepEqual(done.removed.map((e) => e.conditionId), ['c2']);
assert.equal(early.entry('c2').releaseReason, 'resolved');

// --- what cannot be dated is skipped, with a reason -----------------------
const blind = new MarketSchedule();
const { skipped: noDate } = blind.observe([
  { conditionId: 'c3', sport: 'tennis', tokens: ['x', 'y'], gameStartTime: null, endDate: null },
], game);
assert.equal(noDate.length, 1);
assert.match(noDate[0].reason, /no game start time/);
assert.equal(blind.size, 0, 'an undatable market is not scheduled');

// --- resolution checks are rationed --------------------------------------
const polled = new MarketSchedule({ resolutionCheckAfterMinutes: 45, resolutionCheckMinutes: 15 });
polled.observe([cs2({ conditionId: 'c4' })], game - HOUR);
polled.refresh(game);
assert.equal(polled.dueForResolutionCheck(game + 10 * MINUTE).length, 0,
  'not asked while the match is obviously still on');
assert.equal(polled.dueForResolutionCheck(game + 50 * MINUTE).length, 1);
assert.equal(polled.dueForResolutionCheck(game + 55 * MINUTE).length, 0, 'and not again at once');
assert.equal(polled.dueForResolutionCheck(game + 70 * MINUTE).length, 1);

// --- markets the wallets are actually in ------------------------------------
// Discovery scans the disciplines the study is about. The wallets also trade
// baseball, and in one window they made 113 fills across 17 markets of which 2
// were being watched. A market a wallet is in is watched from the moment it is
// noticed, in any discipline, until it resolves.
const followed = new MarketSchedule({ leadMinutes: 10 });
const baseball = {
  conditionId: 'mlb1', sport: 'baseball', tokens: ['m1', 'm2'], level: 'match',
  question: 'Yankees vs Red Sox', gameStartTime: null, endDate: null,
};
assert.equal(followed.observe([baseball], game).skipped.length, 1,
  'undatable and unremarkable: discovery would pass it over');

const wanted = new MarketSchedule({ leadMinutes: 10 });
const { scheduled: onWallet } = wanted.observe([baseball], game, { priority: true });
assert.equal(onWallet.length, 1, 'the same market is taken when a wallet is in it');
assert.deepEqual(wanted.refresh(game).added.map((e) => e.conditionId), ['mlb1'],
  'and subscribed at once, with no match time to wait for');
assert.equal(wanted.entry('mlb1').source, 'wallet');
assert.deepEqual(wanted.refresh(game + 40 * HOUR).removed, [],
  'no hold window releases it — only a resolution does');
wanted.markResolved('mlb1');
assert.deepEqual(wanted.refresh(game + 41 * HOUR).removed.map((e) => e.conditionId), ['mlb1']);

// A market already waiting for its match is promoted when a wallet turns up in
// it, rather than scheduled a second time.
const promoted = new MarketSchedule({ leadMinutes: 10 });
promoted.observe([cs2({ conditionId: 'c8' })], game - 10 * HOUR);
assert.equal(promoted.refresh(game - 10 * HOUR).added.length, 0, 'waiting for kick-off');
promoted.observe([cs2({ conditionId: 'c8' })], game - 10 * HOUR, { priority: true });
assert.deepEqual(promoted.refresh(game - 10 * HOUR).added.map((e) => e.conditionId), ['c8'],
  'the wallet stops it waiting');
assert.equal(promoted.entry('c8').source, 'wallet');

// A market discovery already let go of. Most of what a round returns is games
// that are over — 1035 of them in one collection — and the schedule drops them
// on sight. When a wallet turns out to be trading in one, that outranks the
// earlier decision: otherwise the fill is recorded against a book nobody is
// watching, which is how 88% of fills came back with no context.
const revived = new MarketSchedule({ leadMinutes: 10 });
revived.observe([cs2({ conditionId: 'c9' })], game + 20 * HOUR);
assert.equal(revived.refresh(game + 20 * HOUR).added.length, 0);
assert.equal(revived.entry('c9').releaseReason, 'window passed unwatched');
revived.observe([cs2({ conditionId: 'c9' })], game + 20 * HOUR, { priority: true });
assert.deepEqual(revived.refresh(game + 20 * HOUR).added.map((e) => e.conditionId), ['c9'],
  'the wallet brings it back');
assert.equal(revived.entry('c9').releaseReason, null);
assert.equal(revived.entry('c9').source, 'wallet');

// A resolved market stays gone: there is no book left to watch.
const settled = new MarketSchedule({ leadMinutes: 10 });
settled.observe([cs2({ conditionId: 'c10' })], game - HOUR);
settled.refresh(game);
settled.markResolved('c10');
settled.refresh(game + MINUTE);
settled.observe([cs2({ conditionId: 'c10' })], game + 2 * MINUTE, { priority: true });
assert.equal(settled.refresh(game + 2 * MINUTE).added.length, 0,
  'a resolved market is not revived by a wallet trade');

// --- slots under a cap ------------------------------------------------------
// With a cap, what gets watched is decided by where the wallets work, not by
// how many markets a discipline happens to have on Polymarket.
const capped = new MarketSchedule({ leadMinutes: 10 });
const market = (id, sport, level = 'segment') => ({
  conditionId: id, sport, tokens: [`${id}a`, `${id}b`], level, kind: 'winner',
  question: `${sport} ${id}`, gameStartTime: '2026-09-01 12:00:00+00',
  endDate: '2026-09-01T18:00:00Z',
});
capped.observe([market('cs-1', 'esports_counter_strike'), market('cs-2', 'esports_counter_strike'),
  market('lol-1', 'esports_league_of_legends')], game);
capped.observe([market('w-1', 'baseball')], game, { priority: true });
const under = capped.refresh(game, {
  maxLive: 2,
  quota: new Map([['esports_league_of_legends', 0.9], ['esports_counter_strike', 0.1]]),
});
const taken = under.added.map((e) => e.conditionId);
assert.ok(taken.includes('w-1'), 'a wallet market is never held back by the cap');
assert.ok(taken.includes('lol-1'), 'the discipline the wallets work in gets the slot');
assert.ok(!taken.includes('cs-1') && !taken.includes('cs-2'), 'CS2 waits, not LoL');
assert.equal(capped.liveSize, 2);

// No cap, no rationing: everything due is taken.
const open = new MarketSchedule({ leadMinutes: 10 });
open.observe([market('a', 'esports_counter_strike'), market('b', 'tennis')], game);
assert.equal(open.refresh(game).added.length, 2, 'without a cap nothing is deferred');

// --- the per-discipline cap -------------------------------------------------
// Ranking decides who goes first when slots are short. It does not bound
// composition: with slots to spare, whichever discipline Polymarket creates
// most sub-markets for takes them all. One collection came out 73% tennis and
// the one before it 60% CS2, and a finding measured on either does not describe
// the other. This is the hard limit that makes the shares comparable.
const quotas = new MarketSchedule({ leadMinutes: 10 });
const many = [];
for (let i = 0; i < 5; i++) many.push(market(`t-${i}`, 'tennis'));
for (let i = 0; i < 3; i++) many.push(market(`c-${i}`, 'esports_counter_strike'));
quotas.observe(many, game);
const rationed = quotas.refresh(game, { maxLivePerSport: 2 });
const bySport = new Map();
for (const entry of rationed.added) {
  bySport.set(entry.record.sport, (bySport.get(entry.record.sport) ?? 0) + 1);
}
assert.equal(bySport.get('tennis'), 2, 'the abundant discipline stops at its cap');
assert.equal(bySport.get('esports_counter_strike'), 2, 'the scarce one gets its share');
assert.equal(quotas.liveSize, 4, 'and nothing beyond the caps is subscribed');

// The cap is a limit on other people's markets, not on the study's own. A
// wallet fill in a discipline that is already full still gets watched.
quotas.observe([market('w-2', 'tennis')], game + MINUTE, { priority: true });
const priority = quotas.refresh(game + MINUTE, { maxLivePerSport: 2 });
assert.deepEqual(priority.added.map((e) => e.conditionId), ['w-2'],
  'a wallet market is never held back by the per-discipline cap');

// A wallet market is exempt from the cap but still counts against it: it is a
// tennis market being watched. Tennis now holds three of a budget of two, so
// nothing else tennis is taken until two of them are let go.
const stillFull = quotas.refresh(game + 90 * 1000, { maxLivePerSport: 2 });
assert.equal(stillFull.added.length, 0, 'a wallet market fills the discipline budget');

// Releasing frees the slot in the same round, not the next one.
const refill = new MarketSchedule({ leadMinutes: 10 });
refill.observe([market('r-1', 'tennis'), market('r-2', 'tennis')], game);
refill.refresh(game, { maxLivePerSport: 1 });
refill.markResolved('r-1');
const refilled = refill.refresh(game + MINUTE, { maxLivePerSport: 1 });
assert.ok(refilled.removed.some((e) => e.conditionId === 'r-1'));
assert.ok(refilled.added.some((e) => e.conditionId === 'r-2'),
  'a market let go this round frees its slot immediately');

// Deferred, not dropped: the cap does not release anything.
const deferred = new MarketSchedule({ leadMinutes: 10 });
deferred.observe([market('d-1', 'tennis'), market('d-2', 'tennis')], game);
deferred.refresh(game, { maxLivePerSport: 1 });
assert.equal(deferred.entry('d-2').releasedAt, null, 'a capped market keeps waiting');
assert.equal(deferred.entry('d-2').deferredForCapacity, true);
assert.equal(deferred.refresh(game + MINUTE).added.length, 1,
  'and is taken as soon as the cap is lifted');

// --- the composition the collector performs on every book event -------------
// schedule.pairOf -> books.get -> pairedView -> metrics. Pinned together
// because each part passing on its own would not catch a mismatch between them.
const twinned = new MarketSchedule({ leadMinutes: 10 });
twinned.observe([cs2({ conditionId: 'c7', tokens: ['yes', 'no'] })], game - HOUR);
twinned.refresh(game);
const noBook = new BookState('no', { conditionId: 'c7' });
noBook.applyBook({
  bids: [{ price: '0.80', size: '400' }],
  asks: [{ price: '0.84', size: '90' }],
  timestamp: String(game),
});
const live = new Map([['no', noBook]]);
const view = pairedView(live.get(twinned.pairOf('yes')), game + 2000);
assert.equal(view.lower, 0.16, 'the twin ask puts a floor under this token');
assert.equal(view.staleSeconds, 2);

const yesBook = new BookState('yes', { conditionId: 'c7' });
yesBook.applyBook({ bids: [{ price: '0.01', size: '900' }], asks: [], timestamp: String(game) });
const row = yesBook.metrics('2026-09-01T12:00:02Z', 'heartbeat', view);
assert.deepEqual(row.slice(15), [0.8, 0.84, 0.18, 0.81, 2],
  'paired bid, ask, fair mid, book sum and staleness reach the book row');

// A market whose whole window passed while the collector was down is closed
// out rather than subscribed to a match that is long over.
const late = new MarketSchedule();
late.observe([cs2({ conditionId: 'c5' })], game + 20 * HOUR);
assert.deepEqual(late.refresh(game + 20 * HOUR).added, []);
assert.equal(late.entry('c5').releaseReason, 'window passed unwatched');

console.log('all schedule tests passed');

// Capacity denial and expiry without a subscription must be visible in the audit.
const auditSchedule = new MarketSchedule({leadMinutes:10});
auditSchedule.observe([cs2()], game);
auditSchedule.refresh(game, {maxLive:0});
assert.equal(auditSchedule.decisions(game)[0].status, 'capacity');
auditSchedule.refresh(game+7*HOUR);
assert.equal(auditSchedule.decisions(game+7*HOUR)[0].status, 'window passed unwatched');
assert.equal(auditSchedule.decisions(game+7*HOUR)[0].subscribedAt, null);
