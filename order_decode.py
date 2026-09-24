"""Strict, read-only CTF Exchange V2 direct matchOrders decoding.

Sources: Polymarket/ctf-exchange-v2 Structs.sol and ITrading.sol.
Client timestamps are signed declarations, NOT exchange acceptance times.
"""
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal

EXCHANGES = {'0xe111180000d2663c0091e4f400237545b87b996b',
             '0xe2222d279d744050d28e00520010520000310f59'}
TOPIC = '0xd543adfd945773f1a62f74f0ee55a5e3b9b1a28262980ba90b1a89f2ea84d8ee'
SCALE = Decimal(1000000)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def iso(ms):
    try:
        return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
    except (ValueError, OverflowError, OSError):
        return None


def decode_input(tx):
    require((tx.get('to') or '').lower() in EXCHANGES, 'unsupported exchange')
    require(tx.get('input', '').startswith('0x3c2b4399'), 'unsupported function/version')
    data = bytes.fromhex(tx['input'][10:])
    def uint(offset):
        require(offset % 32 == 0 and 0 <= offset <= len(data)-32, 'ABI offset out of bounds')
        return int.from_bytes(data[offset:offset+32], 'big')
    def order(offset):
        v = [uint(offset+32*i) for i in range(12)]
        require(v[1] < 2**160 and v[2] < 2**160, 'invalid address')
        require(v[6] in (0, 1) and v[7] in (0, 1, 2, 3), 'invalid enum')
        require(v[4] > 0 and v[5] > 0, 'zero original amount')
        require(v[11] >= 384, 'invalid signature offset')
        sig = offset+v[11]; size = uint(sig)
        require(sig+32+size <= len(data), 'truncated signature')
        return dict(salt=str(v[0]), wallet=f'0x{v[1]:040x}', signer=f'0x{v[2]:040x}',
                    asset_id=str(v[3]), maker_amount=str(v[4]), taker_amount=str(v[5]),
                    side='BUY' if v[6] == 0 else 'SELL', signature_type=v[7],
                    client_timestamp_ms=v[8], client_created_utc=iso(v[8]),
                    metadata=f'0x{v[9]:064x}', builder=f'0x{v[10]:064x}',
                    limit_price=str(Decimal(v[4] if v[6] == 0 else v[5]) / Decimal(v[5] if v[6] == 0 else v[4])),
                    original_shares=str(Decimal(v[5] if v[6] == 0 else v[4]) / SCALE))
    h = [uint(32*i) for i in range(7)]
    start = h[2]; n = uint(start)
    require(0 < n <= len(data)//32 and n == uint(h[4]), 'invalid maker/fill array')
    offsets = [uint(start+32+32*i) for i in range(n)]
    require(all(o >= n*32 for o in offsets), 'maker offset overlaps array head')
    return dict(condition_id='0x'+data[:32].hex(), taker=order(h[1]),
                makers=[order(start+32+o) for o in offsets],
                maker_fills=[uint(h[4]+32+32*i) for i in range(n)], taker_fill=h[3])


def decode_transaction(tx, receipt, block):
    d = decode_input(tx); exchange = tx['to'].lower()
    require(receipt['transactionHash'].lower() == tx['hash'].lower(), 'receipt transaction mismatch')
    require(int(receipt['status'], 16) == 1, 'failed transaction')
    require(tx['blockHash'] == receipt['blockHash'] == block['hash'], 'block hash mismatch')
    require(tx['blockNumber'] == receipt['blockNumber'] == block['number'], 'block number mismatch')
    events = sorted((e for e in receipt['logs'] if e['address'].lower() == exchange and
                     e.get('topics', [None])[0] == TOPIC), key=lambda e: int(e['logIndex'], 16))
    require(len(events) == len(d['makers'])+1, 'unexpected OrderFilled count')
    records = []; fill_ts = int(block['timestamp'], 16)
    for i, (o, e) in enumerate(zip(d['makers']+[d['taker']], events)):
        role = 'maker' if i < len(d['makers']) else 'taker'
        require(len(e['topics']) == 4 and not e.get('removed', False), 'invalid event')
        require(len(e['data']) == 2+64*7, 'invalid event data length')
        v = [int(e['data'][2+j*64:2+(j+1)*64], 16) for j in range(7)]
        require('0x'+e['topics'][2][-40:].lower() == o['wallet'], 'maker sequence mismatch')
        expected_counterparty = d['taker']['wallet'] if role == 'maker' else exchange
        require('0x'+e['topics'][3][-40:].lower() == expected_counterparty, 'counterparty mismatch')
        require(v[0] == (0 if o['side'] == 'BUY' else 1) and str(v[1]) == o['asset_id'], 'asset/side mismatch')
        require(f'0x{v[5]:064x}' == o['builder'] and f'0x{v[6]:064x}' == o['metadata'], 'metadata mismatch')
        if role == 'maker':
            require(v[2] == d['maker_fills'][i], 'maker fill mismatch')
            require(v[3] == v[2]*int(o['taker_amount'])//int(o['maker_amount']), 'maker ratio mismatch')
        else:
            # Aggregate taker event is one fill, not one copy for each maker.
            # Price improvement can change its proceeds/shares relative to limit.
            require(v[2] <= d['taker_fill'], 'taker amount exceeds match input')
        shares = Decimal(v[3] if o['side'] == 'BUY' else v[2])/SCALE
        cash = Decimal(v[2] if o['side'] == 'BUY' else v[3])/SCALE
        require(shares > 0, 'empty fill')
        age = (fill_ts*1000-o['client_timestamp_ms'])/1000
        records.append(dict(o, chain_id=137, exchange=exchange, tx_hash=tx['hash'].lower(),
                            block_number=int(block['number'],16), block_hash=block['hash'],
                            tx_index=int(tx['transactionIndex'],16), log_index=int(e['logIndex'],16),
                            order_hash=e['topics'][1].lower(), condition_id=d['condition_id'], role=role,
                            block_timestamp=fill_ts, fill_utc=iso(fill_ts*1000),
                            client_age_seconds=age, clock_status='negative_age' if age < 0 else
                            ('unverified_client_clock' if o['client_created_utc'] else 'invalid_client_time'),
                            filled_shares=str(shares), filled_cash=str(cash), execution_price=str(cash/shares),
                            fee=str(Decimal(v[4])/SCALE), taker_wallet=d['taker']['wallet'],
                            taker_asset_id=d['taker']['asset_id'], taker_side=d['taker']['side'],
                            match_type=('COMPLEMENTARY' if o['side'] != d['taker']['side'] else
                                        'MINT' if o['side']=='BUY' else 'MERGE') if role=='maker' else 'AGGREGATE'))
    return records


def summarize_orders(fills):
    groups = defaultdict(list)
    unique = {(r['chain_id'], r['tx_hash'], r['log_index']): r for r in fills}
    for r in unique.values():
        groups[(r['chain_id'], r['exchange'], r['order_hash'])].append(r)
    result = []
    for group in groups.values():
        group.sort(key=lambda r: (r['block_number'], r['tx_index'], r['log_index']))
        first, last = group[0], group[-1]
        total = sum(Decimal(r['filled_shares']) for r in group)
        result.append(dict(first, observed_fill_count=len(group),
                           observed_total_shares=str(total), observed_total_cash=str(sum(Decimal(r['filled_cash']) for r in group)),
                           observed_fraction_at_limit=str(total/Decimal(first['original_shares'])),
                           first_observed_fill_utc=first['fill_utc'], last_observed_fill_utc=last['fill_utc'],
                           observed_fill_span_seconds=last['block_timestamp']-first['block_timestamp']))
    return result
