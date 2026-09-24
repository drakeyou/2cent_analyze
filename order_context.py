"""Anonymous book context BEFORE declared client creation. Never claims order ownership."""
import datetime as dt
import json
import sqlite3
from pathlib import Path
from order_decode import iso


def creation_context(orders, paths, max_sample_age_ms=65000):
    connections={}; result=[]
    try:
        for path in paths:
            p=Path(path); day=p.name[3:13]
            db=sqlite3.connect(p.resolve().as_uri()+'?mode=ro',uri=True); db.row_factory=sqlite3.Row
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if {'quote_observations','market_events'}<=tables:
                db.execute('BEGIN'); db.execute('SELECT count(*) FROM sqlite_master').fetchone()
                connections[day]=db
            else: db.close()
        for order in orders:
            for offset in (-60,-10,-1,0):
                anchor=order['client_timestamp_ms']+offset*1000; stamp=iso(anchor)
                row=dict(order_hash=order['order_hash'],exchange=order['exchange'],wallet=order['wallet'],
                         asset_id=order['asset_id'],client_created_utc=order['client_created_utc'],
                         offset_seconds=offset,anchor_utc=stamp,context_status='missing',
                         clock_basis='client and collector clocks; offset unknown',quote_json=None)
                result.append(row)
                if not stamp or order['client_timestamp_ms']<=0:
                    row['context_status']='invalid_client_time';continue
                day=dt.datetime.fromtimestamp(anchor/1000,dt.timezone.utc).date()
                candidates=[]; dbs=[]
                for date in (day-dt.timedelta(days=1),day):
                    db=connections.get(date.isoformat())
                    if db is None:continue
                    dbs.append(db)
                    q=db.execute('SELECT * FROM quote_observations WHERE asset_id=? AND received_at<=? ORDER BY received_at DESC,frame_seq DESC LIMIT 1',
                                 (order['asset_id'],stamp)).fetchone()
                    if q:candidates.append(dict(q))
                if not candidates:continue
                quote=max(candidates,key=lambda q:(q['received_at'],q['frame_seq']))
                received=dt.datetime.fromisoformat(quote['received_at'].replace('Z','+00:00')).timestamp()*1000
                row.update(sample_age_ms=round(anchor-received,3),source_age_ms=(anchor-quote['source_timestamp']) if quote['source_timestamp'] else None,
                           paired_source_age_ms=(anchor-quote['paired_source_timestamp']) if quote['paired_source_timestamp'] else None,
                           context_status='available_unverified_clocks',quote_json=json.dumps(quote,separators=(',',':')))
                if anchor-received>max_sample_age_ms:row['context_status']='stale_sample'
                elif quote['crossed'] or quote['paired_crossed']:row['context_status']='crossed_book'
                elif any(quote[k] and quote[k]>anchor for k in ('source_timestamp','paired_source_timestamp')):row['context_status']='source_after_anchor'
                for db in dbs:
                    for event in db.execute("SELECT * FROM market_events WHERE received_at>=? AND received_at<=? AND event_type IN ('capture_start','stream_reset')",
                                            (quote['received_at'],stamp)):
                        if event['session_id']==quote['session_id'] and event['seq']<=quote['frame_seq']:continue
                        payload=json.loads(event['payload_json'])
                        if event['event_type']=='capture_start' or order['asset_id'] in payload.get('assets',[]):
                            row['context_status']='capture_reset'
        return result
    finally:
        for db in connections.values():db.close()
