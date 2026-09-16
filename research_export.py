#!/usr/bin/env python3
"""Export the continuous research journal, without sweep or volume sampling.

python3 research_export.py --db data --since 2026-09-16 --until 2026-09-16
Dates select complete UTC daily files (both endpoints inclusive). Standard library only.
"""
import argparse
import csv
import datetime as dt
import gzip
import json
import sqlite3
import zipfile
from collections import Counter
from pathlib import Path

from analyze import databases
from trade_identity import unique_fills


def columns(db, table):
    return [r['name'] for r in db.execute(f'PRAGMA table_info({table})')]


def export_research(paths, out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    if any(out.iterdir()):
        raise ValueError(f'{out} is not empty; choose a new --out directory')
    manifest = {'version': 1, 'created_at': dt.datetime.now(dt.timezone.utc).isoformat(),
                'sampling': 'none', 'files': [], 'counts': {}, 'warnings': []}
    connections = []
    try:
        for path in paths:
            db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
            db.row_factory = sqlite3.Row
            db.execute('BEGIN')
            # Pin a consistent read snapshot, including on an active WAL store.
            db.execute('SELECT count(*) FROM sqlite_master').fetchone()
            connections.append((Path(path).name, db))

        counts = Counter()
        with gzip.open(out / 'market-events.jsonl.gz', 'wt', encoding='utf-8') as events, \
                gzip.open(out / 'market-trades.jsonl.gz', 'wt', encoding='utf-8') as prints:
            for name, db in connections:
                summary = {'database': name, 'events': 0, 'first_received_at': None,
                           'last_received_at': None}
                if not columns(db, 'market_events'):
                    manifest['warnings'].append(f'{name}: no raw journal (older collector)')
                else:
                    for row in db.execute('SELECT * FROM market_events ORDER BY session_id, seq'):
                        event = dict(row)
                        event['database'] = name
                        event['payload'] = json.loads(event.pop('payload_json'))
                        events.write(json.dumps(event, separators=(',', ':')) + '\n')
                        counts['market_events'] += 1
                        counts[event['event_type']] += 1
                        summary['events'] += 1
                        ts = event['received_at']
                        summary['first_received_at'] = min(summary['first_received_at'] or ts, ts)
                        summary['last_received_at'] = max(summary['last_received_at'] or ts, ts)
                        if event['event_type'] == 'frame':
                            for index, message in enumerate(event['payload']):
                                if message.get('event_type') != 'last_trade_price':
                                    continue
                                # Preserve the original print and the journal join, not a
                                # guessed aggressor wallet or reconstructed trade identity.
                                prints.write(json.dumps({
                                    'session_id': event['session_id'], 'frame_seq': event['seq'],
                                    'message_index': index, 'received_at': ts,
                                    'connection_id': event['connection_id'], 'message': message,
                                }, separators=(',', ':')) + '\n')
                                counts['market_trades'] += 1
                manifest['files'].append(summary)

        # Stream large tables; preserve daily provenance and evolving column sets.
        for table in ('quote_observations', 'markets', 'universe', 'gaps'):
            header = list(dict.fromkeys(c for _, db in connections for c in columns(db, table)))
            with gzip.open(out / f'{table.replace("_", "-")}.csv.gz', 'wt',
                           encoding='utf-8', newline='') as handle:
                writer = csv.DictWriter(handle, ['database'] + header)
                writer.writeheader()
                for name, db in connections:
                    if not columns(db, table):
                        continue
                    for row in db.execute(f'SELECT * FROM {table}'):
                        writer.writerow({'database': name, **dict(row)})
                        counts[table] += 1

        fills = []
        for _, db in connections:
            if columns(db, 'trades'):
                fills.extend(dict(r) for r in db.execute('SELECT * FROM trades'))
        counts['fill_copies'] = len(fills)
        fills = unique_fills(fills)
        counts['unique_fills'] = len(fills)
        counts['duplicate_fills_removed'] = counts['fill_copies'] - len(fills)
        header = list(dict.fromkeys(c for row in fills for c in row))
        header = [c for c in header if c != 'fill_index'] + ['fill_index']
        indices = Counter()
        with (out / 'target-fills.csv').open('w', encoding='utf-8', newline='') as handle:
            writer = csv.DictWriter(handle, header)
            writer.writeheader()
            for row in fills:
                key = (row['wallet'].lower(), row['asset_id'], row['side'])
                indices[key] += 1
                writer.writerow({**row, 'fill_index': indices[key]})
        if not counts['market_events']:
            manifest['warnings'].append('No raw events: collect with capture.enabled=true first')
        manifest['counts'] = dict(counts)
        (out / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
        (out / 'README.md').write_text(README, encoding='utf-8')
    finally:
        for _, db in connections:
            db.close()
    archive_path = Path(str(out) + '.zip')
    with zipfile.ZipFile(archive_path, 'w') as archive:
        for path in sorted(out.iterdir()):
            archive.write(path, path.name,
                          compress_type=zipfile.ZIP_STORED if path.suffix == '.gz'
                          else zipfile.ZIP_DEFLATED)
    return manifest


README = """# Continuous Polymarket research capture

`manifest.json` lists source UTC files, actual receive-time spans, counts and missing
raw coverage. The whole selected daily files are exported, including ordinary
periods without detected sweeps or known wallet fills. There is no row sampling.
Files may be large. Every database is read from a consistent SQLite snapshot.

## Files and replay

- `market-events.jsonl.gz`: complete parsed market messages (all prices/sizes),
  checkpoints, resets and `capture_start` with the effective configuration.
  Within each session replay ascending `seq`, across daily files. Sessions are
  independent: do not join a previous session's book to a restarted process.
  `frame` payloads are arrays; apply ALL messages before calculating paired
  features. `book` replaces a token's entire book; `price_change.size` is the
  NEW absolute size at that side/price, not an increment. `checkpoint` replaces
  the complete known book set and retains each book's last source timestamp.
  `stream_reset` invalidates its assets until the next full `book`. Retain but
  do not apply deltas for unknown books. A gap is unknown state, not zero depth.
  A later day begins with a local checkpoint even if no new market message
  arrives at midnight. A checkpoint repeats state; it is not a fresh quote.
- `market-trades.jsonl.gz`: every `last_trade_price` message, with a join back
  to session/frame/message index. These public prints have no reliable wallet
  attribution; they are evidence for execution, not proof who owned a bid.
- `quote-observations.csv.gz`: features after each changed frame, plus periodic
  `trigger=checkpoint` observations across all currently known books. The latter
  provide time-based control candidates without selecting on future fills.
  They are not automatically negative examples. `levels_json` is an array of
  `[price, bid_shares, ask_shares, bid_shares_strictly_above, bid_dollars_strictly_above]`.
  Levels: .01, .011, .02, .03, .05, .25, .40, .50, .70, .81, .85, .90.
  `crossed` and `paired_crossed`: 1 means bid > ask; null means no two-sided book.
  Source timestamps are exchange epoch milliseconds; receive times are UTC ISO.
  `monotonic_ms` in the journal is process-local, comparable only within session.
- `target-fills.csv`: unique observed economic fills across daily copies, keyed
  by tx hash, wallet, token, side, size and price; no-hash records are retained.
  Equal API rows cannot distinguish multiple identical logs inside a transaction.
  `fill_index` is within this export, per wallet/token/side, not lifetime history.
- `markets.csv.gz`: daily market registries, including paired token identifiers,
  labels and subscription windows. Preserve provenance; later files have newer
  metadata, not necessarily information that was known at a past decision time.
- `universe.csv.gz`: discovery and scheduling decisions, including markets not
  subscribed. This describes discovery coverage, not every market on Polymarket.
- `gaps.csv.gz`: legacy socket outage summaries; use asset-specific raw resets
  and the next full snapshot to invalidate research intervals precisely.

## Interpretation

Resting levels are anonymous aggregated liquidity. A size change can be an order,
a cancellation, an execution, or several of these. A recurring size is a candidate
fingerprint, never a labelled order from the target wallet. Compare with public
prints and confirmed wallet fills, and exclude missing/stale/crossed intervals.
Both outcome books can mirror each other. `1 - paired_ask` is not an independent
fair-value estimate and can equal this token's bid. Do not select decisions using
later winners, later fills, or future recovery. Split evaluation by match and date.
Activity/API pagination and polling can miss fills; no observed fill is not proof
that no limit order was placed. Watching a wallet's market after its first fill
does not supply its pre-entry book. Existing historical databases cannot recreate
raw events that were never logged. Use export.py for the compact summary bundle.
"""


def date_argument(value):
    return dt.date.fromisoformat(value).isoformat()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default='data')
    parser.add_argument('--since', type=date_argument)
    parser.add_argument('--until', type=date_argument)
    parser.add_argument('--out', default='research-export-' +
                        dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S'))
    args = parser.parse_args()
    paths = databases(args.db, args.since)
    if args.until:
        paths = [p for p in paths if Path(p).name[3:13] <= args.until]
    if not paths:
        parser.error('no daily databases in the requested date range')
    result = export_research(paths, args.out)
    print(f'Wrote {args.out}/ and {args.out}.zip')
    print(json.dumps(result['counts'], indent=2))
    for warning in result['warnings']:
        print('WARNING: ' + warning)


if __name__ == '__main__':
    main()
