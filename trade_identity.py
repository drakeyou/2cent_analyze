"""Stable API fill identities across daily databases and repeated exports."""
from decimal import Decimal, InvalidOperation


def fill_key(row):
    row = dict(row)
    if not row.get('tx_hash'):
        return None  # No reliable identity: do not merge merely equal prints.
    def number(value):
        try:
            return str(Decimal(str(value)).normalize())
        except InvalidOperation:
            return str(value)
    return (row['tx_hash'].lower(), str(row.get('wallet') or '').lower(),
            str(row.get('asset_id')), row.get('side'),
            number(row.get('size')), number(row.get('price')))


def unique_fills(rows):
    """Keep economic fills, ignoring derived fill_index and repeated daily copies."""
    result, seen = [], {}
    for original in rows:
        row = dict(original)
        key = fill_key(row)
        if key is not None and key in seen:
            # Later daily registries often supply previously missing labels.
            seen[key].update({k: v for k, v in row.items() if v is not None and v != ''})
            continue
        result.append(row)
        if key is not None:
            seen[key] = row
    return sorted(result, key=lambda r: (r.get('ts', ''), r.get('tx_hash') or '',
                                        str(r.get('asset_id') or '')))
