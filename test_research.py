"""Regressions for duplicate fills, independent wallets and continuous export."""
import csv
import gzip
import json
import sqlite3
import subprocess
import tempfile
from pathlib import Path

from analyze_fills import build_positions
from export import select_series
from research_export import export_research
from trade_identity import unique_fills

fill = dict(ts='2026-09-16T12:00:00Z', tx_hash='0xabc', wallet='0xA', asset_id='a',
            side='BUY', size='200.0', price='0.90', fill_index='99')
assert len(unique_fills([fill, {**fill, 'size': 200, 'price': .9, 'fill_index': 2}])) == 1
other_wallet = {**fill, 'wallet': '0xB'}
assert len(build_positions([fill, fill, other_wallet])) == 2
assert len(unique_fills([{**fill, 'tx_hash': ''}] * 2)) == 2
assert len(unique_fills([fill, {**fill, 'price': '.85'}])) == 2

cheap = [dict(ts=f'2026-09-16T12:{i:02d}:00Z', bid_after=.02, size_consumed=100)
         for i in range(20)]
large = [dict(ts=f'2026-09-16T13:{i:02d}:00Z', bid_after=.999, size_consumed=1000000)
         for i in range(20)]
picked = select_series(cheap + large, 8)
assert len(picked) == 8 and sum(r['bid_after'] <= .05 for r in picked) == 6
assert len(select_series(cheap, 8)) == 8
assert len(select_series(large, 8)) == 8
assert select_series(cheap, 0) == []

ROOT = Path(__file__).resolve().parent
with tempfile.TemporaryDirectory() as tmp:
    data, out = Path(tmp) / 'data', Path(tmp) / 'out'
    # Use production Store/Capture schema; then duplicate the same API fill into
    # two daily files, reproducing repeated history polling after midnight.
    script = """
      import { Store } from './src/pm/store.mjs';
      import { MarketCapture } from './src/pm/capture.mjs';
      const store = new Store(process.argv[1], {now: () => new Date('2026-09-16T12:00:00Z')});
      const capture = new MarketCapture({store, books: new Map(), sessionId: 'export-test'});
      capture.processBatch([{event_type:'book', asset_id:'a', market:'c', timestamp:'1000',
        bids:[{price:'.02',size:'800'}], asks:[{price:'.03',size:'100'}]}]);
      capture.processBatch([{event_type:'last_trade_price',asset_id:'a',price:'.02',size:'10'}]);
      capture.checkpoint();
      store.add('trades', ['2026-09-16T12:00:00Z','c','a','0xa','BUY',.02,10,'maker','0xabc']);
      store.close();
    """
    subprocess.run(['node', '--input-type=module', '-e', script, str(data)], cwd=ROOT, check=True)
    first = data / 'pm-2026-09-16.sqlite'
    second = data / 'pm-2026-09-17.sqlite'
    with sqlite3.connect(first) as a, sqlite3.connect(second) as b:
        a.backup(b)
        b.execute('DELETE FROM market_events')
        b.execute('DELETE FROM quote_observations')
    result = export_research([first, second], out)
    assert result['counts']['market_events'] == 3
    assert result['counts']['market_trades'] == 1
    assert result['counts']['unique_fills'] == 1
    assert result['counts']['duplicate_fills_removed'] == 1
    with gzip.open(out / 'market-events.jsonl.gz', 'rt') as handle:
        events = [json.loads(line) for line in handle]
    assert events[-1]['event_type'] == 'checkpoint'
    assert events[-1]['payload'][0]['bids'][0]['size'] == 800
    with gzip.open(out / 'quote-observations.csv.gz', 'rt') as handle:
        quotes = list(csv.DictReader(handle))
    assert len(quotes) == 2 and quotes[-1]['trigger'] == 'checkpoint'
    with (out / 'target-fills.csv').open() as handle:
        fills = list(csv.DictReader(handle))
    assert len(fills) == 1 and fills[0]['fill_index'] == '1'
    assert Path(str(out) + '.zip').exists()
    try:
        export_research([first], out)
    except ValueError:
        pass
    else:
        raise AssertionError('existing output must not be silently removed')
    # A pre-upgrade database is usable, with an explicit missing-journal warning.
    legacy = Path(tmp) / 'pm-2026-09-15.sqlite'
    with sqlite3.connect(legacy) as db:
        db.execute('CREATE TABLE trades (ts TEXT, wallet TEXT, asset_id TEXT, side TEXT, tx_hash TEXT)')
    old = export_research([legacy], Path(tmp) / 'old')
    assert len(old['warnings']) == 2

print('all research tests passed')
