#!/usr/bin/env python3
"""Build a small, self-describing bundle for analysis elsewhere.

    python3 export.py                 -> export/ plus export.zip
    python3 export.py --db data --out export

A day of collection is a couple of gigabytes, almost all of it the book table.
None of that belongs in a conversation. What does is every sweep with the
context that prices it, the labelled fills, and enough of the registry to say
what each market was. That comes to a fraction of a megabyte.
"""

import argparse
import csv
import datetime
import os
import shutil
import statistics
import subprocess
import sys
import time
import zipfile
from collections import Counter, defaultdict

from analyze import (databases, highs_after, iso_shift, newest, parse_timestamp,
                     query, target_wallets, with_column, with_table)
from trade_identity import unique_fills

CONTEXT_MINUTES = (1, 5, 15)

# A detector that logs liberally is right — a collapse that stopped short of a
# cent is still the denominator — but "the book emptied for a moment" is not an
# event. Significance is applied here rather than at collection so the raw log
# stays intact and the filter can be re-tuned without recollecting.
SIGNIFICANT_SWEEPS = """
    SELECT s.*, m.sport, m.level AS market_level, m.kind, m.question
    FROM sweeps s LEFT JOIN markets m ON m.condition_id = s.condition_id
    WHERE s.bid_after IS NOT NULL AND s.bid_before IS NOT NULL
      AND s.bid_before >= ? AND s.size_consumed >= ?
      AND s.bid_after < s.bid_before
    ORDER BY s.ts
"""


def stratify(sweeps, cap):
    """A sample with equal quotas per day, instead of the newest N.

    Taking the tail of a run ordered by time gave five thousand sweeps that were
    all from the last day, in a bundle described as "the most recent 5000 of
    117616". Any frequency computed from the length of that file is wrong, and
    nothing in the file said so.

    Days that hold fewer sweeps than their quota give the remainder back to the
    others, and within a day the sample is spread evenly rather than taken from
    one end, so the hours of the day survive too.

    @returns (sample, strata) where strata is [day, significant, exported]
    """
    by_day = defaultdict(list)
    for sweep in sweeps:
        by_day[str(sweep["ts"])[:10]].append(sweep)
    days = sorted(by_day)
    if not days:
        return [], []
    if len(sweeps) <= cap:
        return sweeps, [[day, len(by_day[day]), len(by_day[day])] for day in days]

    sample = []
    strata = []
    left = cap
    # Smallest days first: a day that cannot fill its quota releases the rest.
    for index, day in enumerate(sorted(days, key=lambda d: len(by_day[d]))):
        quota = left // (len(days) - index)
        rows = by_day[day]
        take = min(len(rows), quota)
        if take >= len(rows):
            picked = rows
        elif take == 1:
            picked = [rows[len(rows) // 2]]
        else:
            step = (len(rows) - 1) / (take - 1)
            picked = [rows[round(i * step)] for i in range(take)]
        sample.extend(picked)
        strata.append([day, len(rows), len(picked)])
        left -= len(picked)
    sample.sort(key=lambda row: row["ts"])
    strata.sort()
    return sample, strata


def dedupe(rows, key_index=0):
    """Keep the newest row per key.

    Registry tables live in every daily database, so reading a week of them
    concatenates seven copies of the same market. Left alone that turned a
    bundle meant to be tens of kilobytes into 25 MB.

    `key_index` may be a tuple where one column is not a key on its own: two
    fills by the same wallet land on the same second often enough, and keying
    those on the timestamp alone silently threw one of each pair away.
    """
    columns = key_index if isinstance(key_index, tuple) else (key_index,)
    seen = {}
    for row in rows:
        seen[tuple(row[i] for i in columns)] = row
    return list(seen.values())


class SchemaError(RuntimeError):
    """A file was about to be written with a column list that does not match it."""


def write_csv(path, header, rows):
    """Write a CSV, checking the rows against the header first.

    Rows may be lists, which are written as they come, or dicts, which are
    projected through the header. The dict form is what makes a check possible,
    and the check runs in BOTH directions on purpose.

    A header column that no row carries is the obvious failure. The one that
    actually happened is the reverse: the collector computed thirteen
    paired-token columns, stored them, the query selected them, and the header
    — written before those columns existed — dropped every one on the floor.
    Three bundles went out with the central quantity missing and nothing said a
    word. So a key present in the rows and absent from the header is an error
    too, and the exporter stops rather than writing a file that looks complete.
    """
    materialised = list(rows)
    if materialised and isinstance(materialised[0], dict):
        name = os.path.basename(path)
        declared, present = set(header), set(materialised[0])
        if declared - present:
            raise SchemaError(f"{name}: no value for declared column(s) "
                              f"{', '.join(sorted(declared - present))}")
        if present - declared:
            raise SchemaError(
                f"{name}: {', '.join(sorted(present - declared))} "
                f"{'are' if len(present - declared) > 1 else 'is'} computed and would"
                f" not be written. Add to the header, or drop from the row.")
        if len(header) != len(set(header)):
            duplicated = sorted({c for c in header if header.count(c) > 1})
            raise SchemaError(f"{name}: duplicated column(s) {', '.join(duplicated)}")
        materialised = [[row[column] for column in header] for row in materialised]

    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(materialised)
    return len(materialised)


def gap_seconds_by_hour(gaps):
    """Split each disconnect across the hours it covers.

    A gap is recorded as one row with a start and an end, but coverage is read
    per hour, and a gap that straddles the hour belongs to both.
    """
    blind = defaultdict(float)
    for gap in gaps:
        started = parse_timestamp(gap["started_at"])
        if started is None:
            continue
        remaining = (gap["duration_ms"] or 0) / 1000.0
        at = started
        while remaining > 0:
            hour_end = (at + datetime.timedelta(hours=1)).replace(
                minute=0, second=0, microsecond=0)
            slice_seconds = min(remaining, (hour_end - at).total_seconds())
            blind[at.strftime("%Y-%m-%dT%H")] += slice_seconds
            remaining -= slice_seconds
            at = hour_end
    return blind


def has_column(paths, table, column):
    """Databases whose table carries this column."""
    return with_column(paths, table, column)


def game_starts(paths, resolutions_path="pm-resolutions.csv"):
    """condition_id -> when the match started.

    Read from the registry where the collector recorded it, and from the
    resolution file otherwise, which is what makes the field answerable for
    collections made before the registry carried it.
    """
    starts = {}
    for row in query(has_column(paths, "markets", "game_start_time"),
                     "SELECT condition_id, game_start_time FROM markets"
                     " WHERE game_start_time IS NOT NULL"):
        starts[row["condition_id"]] = row["game_start_time"]
    if os.path.exists(resolutions_path):
        with open(resolutions_path, newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                start = (row.get("game_start_time") or "").strip()
                if start and row.get("condition_id"):
                    starts.setdefault(row["condition_id"], start)
    return starts


def resolution_labels(path="pm-resolutions.csv"):
    """condition_id -> what the market was, from the resolution file.

    The registry only holds markets the collector subscribed to, and the wallets
    trade in disciplines it never watches, so joining fills against it alone
    left level and kind empty in 2195 rows out of 2200. The resolver classifies
    every market it fetches, which is exactly the gap.
    """
    labels = {}
    if not os.path.exists(path):
        return labels
    with open(path, newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            key = row.get("condition_id")
            if not key or key in labels:
                continue
            labels[key] = {
                "question": row.get("question") or None,
                "sport": row.get("sport") or None,
                "market_level": row.get("market_level") or None,
                "kind": row.get("kind") or None,
                "segment_no": row.get("segment_no") or None,
            }
    return labels


def minutes_from(start, ts):
    """Minutes from the match start to an event, negative before the first point."""
    began = parse_timestamp(start)
    at = parse_timestamp(ts)
    if began is None or at is None:
        return None
    return round((at - began).total_seconds() / 60, 2)


# The column list lives next to the code that fills it, because the two drifting
# apart is precisely the failure this file has had three times: the paired-token
# bounds, book_frozen and the touched-level counts were all computed, stored and
# then dropped here. write_csv() checks the row against this list, so a column
# added to one and not the other stops the export instead of vanishing.
#
# Order is append-only. Something downstream reads these by index.
SWEEP_COLUMNS = [
    "ts", "asset_id", "condition_id", "rule", "bid_before", "bid_after",
    "size_consumed", "levels_crossed", "depth_before", "depth_after",
    "prior_size_at_001", "prior_size_at_002", "prior_size_at_003", "prior_size_at_005",
    "prior_depth_bid_total", "prior_n_bid_levels",
    "ggbet_fair", "dislocation_ratio", "seconds_since_ggbet_quote", "ggbet_market_state",
    "high_1m", "high_5m", "high_15m", "sport", "market_level", "kind", "question",
    "sweep_id", "resolved_before_1m", "resolved_before_5m", "resolved_before_15m",
    "minutes_from_game_start",
    # The twin token, and what it bounds. fair_lower_bound is the metric the
    # gg.bet-derived dislocation_ratio above failed to be: it needs no external
    # feed and no name matching, only the other half of the same market.
    "paired_asset_id", "paired_bid", "paired_ask", "paired_ask_size",
    "fair_lower_bound", "fair_upper_bound", "fair_mid", "book_sum",
    "internal_dislocation", "paired_stale_seconds",
    # What levels_crossed misses, and whether the book was alive afterwards.
    "levels_touched", "size_eaten_partial", "n_bid_levels_before", "book_frozen",
]


def sweep_context(paths, sweeps, starts=None, report_every=100):
    """Every sweep, with what the book and gg.bet said around it.

    This is the long step of the export — several queries per sweep — so it
    says where it is. Silence for half an hour is indistinguishable from a
    hang, and a run that looks hung gets killed.
    """
    starts = starts if starts is not None else {}
    rows = []
    began = time.monotonic()
    for index, sweep in enumerate(sweeps):
        if report_every and index and index % report_every == 0:
            done = time.monotonic() - began
            rate = index / done if done else 0
            left = (len(sweeps) - index) / rate if rate else 0
            # Seconds when there are seconds left: "about 0.0 min left" on a run
            # that finishes in two is noise pretending to be information.
            remaining = f"{left / 60:.0f} min" if left >= 90 else f"{left:.0f}s"
            print(f"    {index}/{len(sweeps)} sweeps priced"
                  f" ({rate:.0f}/s, {remaining} left)", flush=True)
        # newest() across files, not rows[0]: query() asks every daily database
        # and returns one "latest row" per file, so the first of them is the
        # newest row of the oldest day — days away from the sweep being priced.
        prior = newest(query(paths, """
            SELECT ts, best_bid, size_at_001, size_at_002, size_at_003, size_at_005,
                   depth_bid_total, n_bid_levels
            FROM book WHERE asset_id = ? AND ts < ? ORDER BY ts DESC LIMIT 1
        """, (sweep["asset_id"], sweep["ts"])))
        # Only worth asking when the collector had no quote in hand at the time.
        # The lookup is a scan of the joined table per sweep per daily file, and
        # on rows that already carry the quote it can only confirm them.
        row = dict(sweep)
        fair = None if row.get("ggbet_fair") is not None else newest(query(paths, """
            SELECT ts, ggbet_fair, dislocation_ratio, seconds_since_ggbet_quote,
                   ggbet_market_state
            FROM joined WHERE asset_id = ? AND ts <= ? ORDER BY ts DESC LIMIT 1
        """, (sweep["asset_id"], sweep["ts"])))
        # sqlite3.Row indexes like a tuple and a mapping but has no .get
        sweep_id = row.get("sweep_id")
        followed = highs_after(paths, sweep["asset_id"], sweep["ts"],
                               CONTEXT_MINUTES, sweep_id)
        highs = [followed[minutes][0] for minutes in CONTEXT_MINUTES]
        settled = [followed[minutes][1] for minutes in CONTEXT_MINUTES]
        frozen = [followed[minutes][2] for minutes in CONTEXT_MINUTES]

        before = dict(prior) if prior else {}
        # The collector prices the sweep as it happens, from the resident cache
        # of gg.bet quotes. The lookup is the fallback for rows written before
        # it did, where it finds a quote about one time in five thousand.
        quote = dict(fair) if fair else {}
        if row.get("ggbet_fair") is not None:
            quote = {
                "ggbet_fair": row.get("ggbet_fair"),
                "dislocation_ratio": row.get("dislocation_ratio"),
                "seconds_since_ggbet_quote": row.get("seconds_since_ggbet_quote"),
                "ggbet_market_state": row.get("ggbet_market_state"),
            }
        rows.append({
            "ts": sweep["ts"], "asset_id": sweep["asset_id"],
            "condition_id": sweep["condition_id"], "rule": sweep["rule"],
            "bid_before": sweep["bid_before"], "bid_after": sweep["bid_after"],
            "size_consumed": sweep["size_consumed"],
            "levels_crossed": sweep["levels_crossed"],
            "depth_before": sweep["depth_before"], "depth_after": sweep["depth_after"],
            "prior_size_at_001": before.get("size_at_001"),
            "prior_size_at_002": before.get("size_at_002"),
            "prior_size_at_003": before.get("size_at_003"),
            "prior_size_at_005": before.get("size_at_005"),
            "prior_depth_bid_total": before.get("depth_bid_total"),
            "prior_n_bid_levels": before.get("n_bid_levels"),
            "ggbet_fair": quote.get("ggbet_fair"),
            "dislocation_ratio": quote.get("dislocation_ratio"),
            "seconds_since_ggbet_quote": quote.get("seconds_since_ggbet_quote"),
            "ggbet_market_state": quote.get("ggbet_market_state"),
            "high_1m": highs[0], "high_5m": highs[1], "high_15m": highs[2],
            "sport": sweep["sport"], "market_level": sweep["market_level"],
            "kind": sweep["kind"], "question": sweep["question"],
            "sweep_id": sweep_id,
            "resolved_before_1m": settled[0], "resolved_before_5m": settled[1],
            "resolved_before_15m": settled[2],
            "minutes_from_game_start":
                minutes_from(starts.get(sweep["condition_id"]), sweep["ts"]),
            # Straight out of the sweeps row: the collector computed these at the
            # moment of the event, from the twin token's live book. .get() rather
            # than [] because a daily file written before the columns existed
            # answers SELECT s.* without them.
            "paired_asset_id": row.get("paired_asset_id"),
            "paired_bid": row.get("paired_bid"),
            "paired_ask": row.get("paired_ask"),
            "paired_ask_size": row.get("paired_ask_size"),
            "fair_lower_bound": row.get("fair_lower_bound"),
            "fair_upper_bound": row.get("fair_upper_bound"),
            "fair_mid": row.get("fair_mid"),
            "book_sum": row.get("book_sum"),
            "internal_dislocation": row.get("internal_dislocation"),
            "paired_stale_seconds": row.get("paired_stale_seconds"),
            "levels_touched": row.get("levels_touched"),
            "size_eaten_partial": row.get("size_eaten_partial"),
            "n_bid_levels_before": row.get("n_bid_levels_before"),
            # The longest horizon, because the flag is cumulative: a book that
            # had not moved in fifteen minutes had not moved in one either.
            "book_frozen": frozen[-1],
        })
    return rows


# What each file in the bundle is. Keyed by the name actually written, so a file
# that stops being produced stops being described, and one that starts being
# produced shows up in the README the same run — undescribed and saying so,
# rather than absent.
FILE_NOTES = {
    "report.txt": "output of `analyze.py`, the four headline questions",
    "sweeps.csv": "detected book collapses, with the context that prices them",
    "book-series.csv": "price-stratified sweep windows (75% budget for <=5c),"
                       " one snapshot every 30s from five minutes before to fifteen"
                       " minutes after",
    "sweeps-strata.csv": "how many significant sweeps each day held, and how many are here",
    "markets.csv": "market registry: question, discipline, level, kind, segment,"
                   " and the subscription window",
    "target-fills.csv": "fills by the wallets under study in THIS collection,"
                        " tagged maker or taker",
    "target-fills-prior.csv": "an earlier export from the project root, retained only"
                              " for reference; not used for this bundle's PnL report",
    "fill-context.csv": "the book on either side of each wallet fill — the ground truth",
    "coverage.csv": "heartbeats per sport per hour, with the seconds nobody saw",
    "gaps.csv": "websocket disconnects",
    "universe-summary.csv": "every market considered, counted by discipline and by the"
                            " reason it was or was not subscribed",
    "pm-resolutions.csv": "which outcome won each market, and its metadata",
    "fills-report.txt": "position-level PnL: cost, revenue and exit mode per position",
    "pm-position-gaps.csv": "positions whose trade history is incomplete",
    "mapping.csv": "Polymarket market to gg.bet market correspondence",
    "pm.config.json": "thresholds and the subscription window the collection ran with",
    "README.md": "this file",
}

SWEEP_NOTES = {
    "ts": "when the collapse was detected, UTC",
    "asset_id": "the token whose bid side collapsed",
    "condition_id": "the market it belongs to; joins to `markets.csv`",
    "rule": "which detector fired: `bid_drop`, `levels`, `depth_collapse`, or a"
            " combination. Thresholds are in `pm.config.json`",
    "bid_before": "best bid before the update",
    "bid_after": "best bid after it",
    "size_consumed": "size that left the bid side across the update",
    "levels_crossed": "price levels that disappeared entirely",
    "depth_before": "total resting bid size before",
    "depth_after": "and after",
    "prior_size_at_001": "resting bid size at 1c in the last snapshot BEFORE the sweep",
    "prior_size_at_002": "the same at 2c — the capacity question: how much of someone"
                         " else's money already stands in the queue",
    "prior_size_at_003": "the same at 3c",
    "prior_size_at_005": "the same at 5c",
    "prior_depth_bid_total": "total resting bid size in that snapshot",
    "prior_n_bid_levels": "how many price levels it had",
    "ggbet_fair": "de-vigged probability from gg.bet for the same question. Null"
                  " wherever the market could not be matched, which is most of them",
    "dislocation_ratio": "`ggbet_fair / pm_mid`. Superseded — it needs an external feed"
                         " and fuzzy name matching, and is empty in the great majority"
                         " of rows. `fair_lower_bound` below is what replaced it",
    "seconds_since_ggbet_quote": "age of that fair value; gg.bet writes only on a price"
                                 " change, so a large number means it is stale",
    "ggbet_market_state": "what gg.bet said the market was doing",
    "high_1m": "highest best bid within a minute of the sweep — the exit side",
    "high_5m": "the same within five minutes",
    "high_15m": "the same within fifteen",
    "sport": "discipline",
    "market_level": "match-level or segment-level market",
    "kind": "what is being bet on",
    "question": "the market question verbatim",
    "sweep_id": "`asset_id:ts`; joins to `matched_sweep_id` in `fill-context.csv`",
    "resolved_before_1m": "1 when the market resolved before that horizon was up. The"
                          " `high_*` beside it is then the payout, not a bid",
    "resolved_before_5m": "the same at five minutes",
    "resolved_before_15m": "the same at fifteen",
    "minutes_from_game_start": "minutes from the first point of the match, negative"
                               " before it started",
    "paired_asset_id": "the other outcome token of the same market",
    "paired_bid": "its best bid at the moment of the sweep",
    "paired_ask": "its best ask",
    "paired_ask_size": "size resting at that ask — how much the floor below is worth"
                       " taking. A bound behind 40 shares is a bound behind 40 shares",
    "fair_lower_bound": "`1 - paired_ask`, a complementary-book quote; NOT an"
                        " independent fair value. Mirrored liquidity can make it"
                        " identical to this token's bid; sequential legacy updates"
                        " can create a temporary apparent discrepancy",
    "fair_upper_bound": "`1 - paired_bid`, the ceiling from the same identity",
    "fair_mid": "midpoint of the two bounds",
    "book_sum": "`bid_after + paired_bid`. Near 1 is a working market; near 0 means"
                " both sides are empty and no bound below is worth reading",
    "internal_dislocation": "`fair_lower_bound / bid_after`. How far under its own"
                            " floor the token was left",
    "paired_stale_seconds": "age of the twin's book at the sweep. It moves only on a"
                            " real change, never on a heartbeat, so a large number"
                            " means the floor is an old quote",
    "levels_touched": "price levels that lost size, whether or not they cleared."
                      " `levels_crossed` counts only the ones that vanished",
    "size_eaten_partial": "size taken out of levels that survived",
    "n_bid_levels_before": "levels on the bid side before the sweep, from the live"
                           " book rather than the previous snapshot",
    "book_frozen": "1 when the book's own clock never moved for fifteen minutes after"
                   " the sweep. A market that died at the collapse — a total decided,"
                   " a handicap closed — reads as a sweep nobody bid back into, and"
                   " drags the statistics with it. Null where the horizon was"
                   " reconstructed from snapshots rather than watched live",
}

FILL_NOTES = {
    "fill_ts": "when the fill happened, UTC",
    "asset_id": "the token bought or sold",
    "condition_id": "its market",
    "wallet": "which studied wallet",
    "side": "BUY or SELL",
    "price": "fill price",
    "size": "fill size",
    "fill_index": "count of observed fills through this timestamp for the same"
                  " wallet/token/side in the collector's recent files; older rows"
                  " can contain duplicates or pooled wallets. Recompute from unique"
                  " target-fills.csv for comparisons across days",
    "bid_t_minus_60": "best bid a minute before. Empty when no snapshot that close"
                      " exists, rather than filled in from an older one",
    "bid_t_minus_10": "ten seconds before",
    "bid_t_minus_1": "one second before",
    "ask_t_minus_1": "best ask one second before",
    "size_at_002_before": "what was already resting at 2c in the queue the fill joined",
    "depth_before": "total resting bid size then",
    "paired_bid_before": "the twin token's bid at the fill",
    "fair_lower_bound_before": "`1 - paired_ask` at the fill: the floor the twin put"
                               " under this token at the moment of entry",
    "book_sum_before": "`bid + paired_bid`; near 1 is a working market",
    "bid_plus_60": "where the bid was a minute later",
    "bid_plus_300": "five minutes later",
    "bid_plus_900": "fifteen minutes later",
    "matched_sweep_id": "a sweep on the same token within two minutes before the fill,"
                        " if there was one. Joins to `sweep_id` in `sweeps.csv`",
    "snapshot_available": "**read this first.** 0 means the market was not being"
                          " watched, so every empty column above says nothing. 1 with"
                          " empty columns means the book really was empty",
    "filled_at": "when this row was written up, which is after the horizons passed",
}


def header_of(out_dir, name):
    """The header a file was actually written with, or nothing if it was not."""
    path = os.path.join(out_dir, name)
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as handle:
        return next(csv.reader(handle), [])


def column_guide(header, notes):
    """A markdown table of the columns a file actually has.

    An undescribed column is listed and says so. Describing the schema from a
    template instead is how the README came to explain a column that had been
    superseded and none of the thirteen that replaced it.
    """
    if not header:
        return "_(not in this bundle)_"
    lines = ["| Column | Meaning |", "|---|---|"]
    for column in header:
        note = notes.get(column, "**undescribed** — add it to export.py")
        lines.append(f"| `{column}` | {note} |")
    return "\n".join(lines)


def bundle_contents(out_dir):
    """A markdown table of the files this run actually produced."""
    lines = ["| File | Contents |", "|---|---|"]
    for name in sorted(os.listdir(out_dir)) + ["README.md"]:
        note = FILE_NOTES.get(name, "**undescribed** — add it to export.py")
        size = os.path.getsize(os.path.join(out_dir, name)) / 1024 if name != "README.md" else 0
        measure = f" ({size:.0f} KB)" if size >= 1 else ""
        lines.append(f"| `{name}` | {note}{measure} |")
    return "\n".join(lines)


BOOK_SERIES_COLUMNS = [
    "sweep_id", "ts", "seconds_from_sweep", "asset_id", "condition_id", "sport",
    "best_bid", "best_ask", "mid", "size_at_001", "size_at_002", "size_at_003",
    "size_at_005", "depth_bid_total", "n_bid_levels",
    "paired_bid", "paired_ask", "fair_lower_bound", "fair_mid", "book_sum",
    "paired_stale_seconds",
]

BOOK_SERIES_NOTES = {
    "sweep_id": "the sweep this window is centred on; joins to `sweeps.csv`",
    "ts": "snapshot time, UTC",
    "seconds_from_sweep": "negative before the collapse, positive after",
    "asset_id": "the swept token",
    "condition_id": "its market",
    "sport": "discipline",
    "best_bid": "best bid in this snapshot",
    "best_ask": "best ask",
    "mid": "midpoint",
    "size_at_001": "resting bid size at 1c",
    "size_at_002": "at 2c — the price the strategy under study bids at",
    "size_at_003": "at 3c",
    "size_at_005": "at 5c",
    "depth_bid_total": "total resting bid size",
    "n_bid_levels": "how many price levels the bid side had",
    "paired_bid": "the twin token's bid",
    "paired_ask": "the twin token's ask",
    "fair_lower_bound": "`1 - paired_ask`: the floor the twin puts under this token"
                        " at this instant. Computed here rather than stored, because"
                        " the book table runs to tens of millions of rows",
    "fair_mid": "midpoint of the two bounds",
    "book_sum": "`bid + paired_bid`; near 1 is a working market, near 0 voids the row",
    "paired_stale_seconds": "age of the twin's book. It moves only on a real change,"
                            " never on a heartbeat",
}


def select_series(sweeps, cap):
    """Price strata, not removed-volume rank; missing strata donate slots."""
    if cap <= 0:
        return []
    deep = [s for s in sweeps if s['bid_after'] is not None and s['bid_after'] <= .05]
    other = [s for s in sweeps if s['bid_after'] is None or s['bid_after'] > .05]
    cheap_n = min(len(deep), (cap * 3 + 3) // 4)
    other_n = min(len(other), max(0, cap - cheap_n))
    cheap_n = min(len(deep), cap - other_n)
    return sorted(stratify(deep, cheap_n)[0] + stratify(other, other_n)[0],
                  key=lambda s: s['ts'])


def book_series(paths, sweeps, before_minutes=5, after_minutes=15,
                every_seconds=30, cap=300):
    """The book around a sweep, thinned to one snapshot every half minute.

    sweeps.csv is one row per collapse, and a row cannot show a recovery taking
    shape. This is the same quantities as a time series — including the twin
    token, which is what makes the floor readable at each instant rather than
    only at the moment of the event.

    Thinned and windowed because it has to be: the book table is heartbeated
    every five seconds per token and runs to tens of millions of rows a day.
    Twenty minutes at one sample per thirty seconds is forty rows a sweep, and
    only a price-stratified sample of sweeps gets one.
    """
    if not sweeps:
        return []
    # Large removed SIZE is not a deep PRICE collapse. It selected almost only
    # 99c books. Reserve 3/4 of the budget for <=5c, stratified across days;
    # keep the rest for the upper-price leg. Empty strata donate their slots.
    ordered = select_series(sweeps, cap)

    paired = set(with_column(paths, "book", "paired_ask"))
    rows = []
    for sweep in ordered:
        at = parse_timestamp(sweep["ts"])
        if at is None:
            continue
        window = query(paths, """
            SELECT ts, asset_id, condition_id, best_bid, best_ask, mid,
                   size_at_001, size_at_002, size_at_003, size_at_005,
                   depth_bid_total, n_bid_levels
            FROM book WHERE asset_id = ? AND ts >= ? AND ts <= ? ORDER BY ts
        """, (sweep["asset_id"], iso_shift(sweep["ts"], -before_minutes),
              iso_shift(sweep["ts"], after_minutes)))
        twin = {}
        for row in query(list(paired), """
            SELECT ts, paired_bid, paired_ask, fair_mid, book_sum, paired_stale_seconds
            FROM book WHERE asset_id = ? AND ts >= ? AND ts <= ?
        """, (sweep["asset_id"], iso_shift(sweep["ts"], -before_minutes),
              iso_shift(sweep["ts"], after_minutes))):
            twin[row["ts"]] = row

        taken = set()
        for row in sorted(window, key=lambda r: r['ts']):
            stamp = parse_timestamp(row["ts"])
            if stamp is None:
                continue
            offset = (stamp - at).total_seconds()
            bucket = int(offset // every_seconds)
            if bucket in taken:
                continue
            taken.add(bucket)
            other = twin.get(row["ts"])
            ask = other["paired_ask"] if other else None
            rows.append({
                "sweep_id": sweep["sweep_id"] if "sweep_id" in sweep.keys() else None,
                "ts": row["ts"], "seconds_from_sweep": round(offset, 1),
                "asset_id": row["asset_id"], "condition_id": row["condition_id"],
                "sport": sweep["sport"],
                "best_bid": row["best_bid"], "best_ask": row["best_ask"],
                "mid": row["mid"],
                "size_at_001": row["size_at_001"], "size_at_002": row["size_at_002"],
                "size_at_003": row["size_at_003"], "size_at_005": row["size_at_005"],
                "depth_bid_total": row["depth_bid_total"],
                "n_bid_levels": row["n_bid_levels"],
                "paired_bid": other["paired_bid"] if other else None,
                "paired_ask": ask,
                # The one derived value in the bundle, and derived on purpose:
                # storing it per book row would add a column to the largest
                # table in the collection to save a subtraction.
                "fair_lower_bound": None if ask is None else round(1 - ask, 6),
                "fair_mid": other["fair_mid"] if other else None,
                "book_sum": other["book_sum"] if other else None,
                "paired_stale_seconds": other["paired_stale_seconds"] if other else None,
            })
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data")
    parser.add_argument("--out", default="export")
    parser.add_argument("--since")
    parser.add_argument("--min-bid-before", type=float, default=0.05,
                        help="ignore collapses that started below this price")
    parser.add_argument("--min-consumed", type=float, default=100.0,
                        help="ignore collapses that ate less size than this")
    parser.add_argument("--max-sweeps", type=int, default=5000,
                        help="cap on how many sweeps get the expensive context")
    parser.add_argument("--series-sweeps", type=int, default=300,
                        help="price-stratified sweep windows (75%% budget for <=5c);"
                             " 0 leaves book-series.csv out")
    args = parser.parse_args()

    paths = databases(args.db, args.since)
    if not paths:
        raise SystemExit(f"no databases in {args.db}")
    shutil.rmtree(args.out, ignore_errors=True)
    os.makedirs(args.out, exist_ok=True)
    sizes = {}

    # analyze.py builds the helper indexes and prints its own progress to
    # stderr, which is left connected so a long first run does not look hung.
    print("running analyze.py (this builds indexes on a first run)...", flush=True)
    report = subprocess.run(
        [sys.executable, "analyze.py", "--db", args.db] + (["--since", args.since] if args.since else []),
        stdout=subprocess.PIPE, text=True)
    with open(os.path.join(args.out, "report.txt"), "w", encoding="utf-8") as handle:
        handle.write(report.stdout or "")

    print("reading sweeps...", flush=True)
    sweeps = query(paths, SIGNIFICANT_SWEEPS, (args.min_bid_before, args.min_consumed))
    raw = query(paths, "SELECT count(*) AS c FROM sweeps")
    raw_count = sum(r["c"] for r in raw)
    print(f"  {raw_count} sweeps logged, {len(sweeps)} pass the significance filter", flush=True)
    span = (f"{sweeps[0]['ts'][:19]} .. {sweeps[-1]['ts'][:19]}" if sweeps else "none")
    sweeps, strata = stratify(sweeps, args.max_sweeps)
    if len(strata) > 1 and sum(row[1] for row in strata) > len(sweeps):
        print(f"  sampling {len(sweeps)} of them, evenly across {len(strata)} days", flush=True)
    sizes["sweeps-strata.csv"] = write_csv(
        os.path.join(args.out, "sweeps-strata.csv"),
        ["day", "significant", "exported"], strata)
    starts = game_starts(paths)
    print(f"pricing {len(sweeps)} sweeps...", flush=True)
    sizes["sweeps.csv"] = write_csv(
        os.path.join(args.out, "sweeps.csv"), SWEEP_COLUMNS,
        sweep_context(paths, sweeps, starts))

    if args.series_sweeps:
        print(f"building the book series around up to {args.series_sweeps} price-stratified"
              f" sweeps...", flush=True)
        sizes["book-series.csv"] = write_csv(
            os.path.join(args.out, "book-series.csv"), BOOK_SERIES_COLUMNS,
            book_series(paths, sweeps, cap=args.series_sweeps))

    print("reading markets, fills, coverage...", flush=True)
    markets = query(paths, """
        SELECT condition_id, asset_id_a, asset_id_b, question, event_slug, sport,
               level, kind, segment_kind, segment_no, line, team_a, team_b,
               end_date, tick_size, min_size, first_seen, last_seen FROM markets
    """)
    # The subscription window, read separately because a database written before
    # it existed has none of these columns. observed_during_game is the whole
    # point: it says, without any analysis, whether the book was recorded while
    # the match was being played or only in the hours before it.
    lifecycle = {}
    for row in query(has_column(paths, "markets", "observed_during_game"), """
        SELECT condition_id, game_start_time, subscribed_at, unsubscribed_at,
               release_reason, coalesce(observed_during_game, 0) AS observed_during_game
        FROM markets
    """):
        # Later files win, and a market seen in play stays seen in play.
        seen = lifecycle.get(row["condition_id"])
        merged = dict(row)
        if seen:
            merged["observed_during_game"] = max(
                seen["observed_during_game"], merged["observed_during_game"])
        lifecycle[row["condition_id"]] = merged

    extra = ["game_start_time", "subscribed_at", "unsubscribed_at",
             "release_reason", "observed_during_game"]
    # Only the markets something else in the bundle refers to. The registry as a
    # whole is thirteen thousand rows and nothing joins against most of them.
    referenced = {row["condition_id"] for row in sweeps}
    referenced.update(row["condition_id"] for row in query(paths,
        "SELECT DISTINCT condition_id FROM trades"))
    kept = []
    for row in markets:
        if row["condition_id"] not in referenced:
            continue
        window = lifecycle.get(row["condition_id"], {})
        kept.append(list(row) + [window.get(name) for name in extra])
    sizes["markets.csv"] = write_csv(os.path.join(args.out, "markets.csv"),
        (list(markets[0].keys()) if markets else ["condition_id"]) + extra,
        dedupe(kept))

    wallets = target_wallets()
    if wallets:
        placeholders = ",".join("?" * len(wallets))
        fills = query(paths, f"""
            SELECT t.ts, t.condition_id, t.asset_id, t.wallet, t.side, t.price, t.size,
                   t.role, t.tx_hash, m.question, m.sport, m.level AS market_level, m.kind
            FROM trades t LEFT JOIN markets m ON m.condition_id = t.condition_id
            WHERE lower(t.wallet) IN ({placeholders}) ORDER BY t.ts
        """, wallets)
        raw_fill_count = len(fills)
        fills = unique_fills(fills)
        print(f"  fills: {len(fills)} unique, {raw_fill_count - len(fills)} duplicate copies removed",
              flush=True)
        # Three things that are cheap here and expensive to reconstruct later.
        #
        # Index unique observed fills per wallet, token and side. It is not the
        # wallet's lifetime fill count when API history or --since is truncated.
        labels = resolution_labels()
        seen_fills = Counter()
        priced_fills = []
        for fill in fills:
            row = dict(fill)
            meta = labels.get(row["condition_id"], {})
            for field in ("question", "sport", "market_level", "kind"):
                if not row.get(field):
                    row[field] = meta.get(field)
            key = (row["wallet"].lower(), row["asset_id"], row["side"])
            seen_fills[key] += 1
            row["fill_index"] = seen_fills[key]
            row["minutes_from_game_start"] = minutes_from(
                starts.get(row["condition_id"]), row["ts"])
            priced_fills.append(row)
        fill_columns = (list(fills[0].keys()) if fills else
                        ["ts", "condition_id", "asset_id", "wallet", "side", "price",
                         "size", "role", "tx_hash", "question", "sport", "market_level",
                         "kind"]) + ["fill_index", "minutes_from_game_start"]
        sizes["target-fills.csv"] = write_csv(os.path.join(args.out, "target-fills.csv"),
            fill_columns, [[row.get(name) for name in fill_columns] for row in priced_fills])

    # Per asset per hour is one row per token per hour and runs to megabytes.
    # What coverage is asked for is per sport per hour: how many tokens were
    # watched, and how completely.
    coverage = query(paths, """
        SELECT substr(b.ts, 1, 13) AS hour, coalesce(m.sport, '(unknown)') AS sport,
               count(DISTINCT b.asset_id) AS assets, count(*) AS heartbeats,
               min(b.best_bid) AS lowest_bid, max(b.best_bid) AS highest_bid
        FROM book b LEFT JOIN markets m ON m.condition_id = b.condition_id
        WHERE b.trigger = 'heartbeat' GROUP BY hour, sport ORDER BY hour
    """)
    # Seconds of that hour nobody saw. Without this the denominator counts the
    # disconnects as watched time: 421 minutes over 174 reconnects in the last
    # full run, silently inflating every rate computed per market-hour.
    blind = gap_seconds_by_hour(query(paths,
        "SELECT started_at, ended_at, duration_ms FROM gaps"))
    sizes["coverage.csv"] = write_csv(os.path.join(args.out, "coverage.csv"),
        ["hour", "sport", "assets", "heartbeats", "lowest_bid", "highest_bid", "gap_seconds"],
        [list(r) + [round(blind.get(r["hour"], 0.0), 1)] for r in coverage])

    # The book around each wallet fill, written up by the collector once the
    # horizons after the fill had passed. This is the ground truth the whole
    # collection is for, so it goes out whole rather than summarised.
    context = query(with_table(paths, "fill_context"),
                    "SELECT * FROM fill_context ORDER BY fill_ts")
    if context:
        # Keyed as the table is: two fills a second apart are one row each, and
        # two by the same wallet in the same second are still two fills.
        sizes["fill-context.csv"] = write_csv(
            os.path.join(args.out, "fill-context.csv"),
            list(context[0].keys()),
            dedupe([list(r) for r in context], key_index=(0, 1, 3, 4, 5, 6)))

    universe = query(paths, """
        SELECT ts, condition_id, discovered_via, subscribed, unsubscribed_at,
               reason_skipped, question, sport, level, kind
        FROM universe ORDER BY ts
    """)
    # The journal answers "what was considered and why was it passed over".
    # That is a count per sport and reason, not fifty thousand rows: the full
    # table is ten megabytes and no conversation will read it.
    seen = {}
    for row in universe:
        seen[row["condition_id"]] = row
    summary = {}
    for row in seen.values():
        key = (row["sport"] or "(unmapped)", int(row["subscribed"] or 0),
               row["reason_skipped"] or "")
        summary[key] = summary.get(key, 0) + 1
    sizes["universe-summary.csv"] = write_csv(
        os.path.join(args.out, "universe-summary.csv"),
        ["sport", "subscribed", "reason_skipped", "markets"],
        sorted(([k[0], k[1], k[2], n] for k, n in summary.items()), key=lambda r: -r[3]))

    gaps = query(paths, "SELECT started_at, ended_at, duration_ms, reason, assets_resubscribed FROM gaps ORDER BY started_at")
    sizes["gaps.csv"] = write_csv(os.path.join(args.out, "gaps.csv"),
        ["started_at", "ended_at", "duration_ms", "reason", "assets_resubscribed"],
        [list(r) for r in gaps])

    # The wallet side of the study: outcomes, the PnL report built from them,
    # and the positions whose history is incomplete.
    #
    # The fills to report on are the unique ones this export just wrote.
    fills_path = os.path.join(args.out, "target-fills.csv")
    # The report must describe the very fills shipped in this bundle. An old
    # root CSV must not silently replace this collection's cleaned history.
    for_report = fills_path if os.path.exists(fills_path) else None
    report_error = None
    if for_report:
        fills_report = subprocess.run(
            [sys.executable, "analyze_fills.py", "--fills", for_report],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if fills_report.returncode == 0 and fills_report.stdout:
            with open(os.path.join(args.out, "fills-report.txt"), "w", encoding="utf-8") as handle:
                handle.write(fills_report.stdout)
            sizes["fills-report.txt"] = "written"
        else:
            # Its own message says what is missing; swallowing it here is how the
            # bundle ends up short with no explanation.
            report_error = (fills_report.stderr or fills_report.stdout).strip()

    for name in ("pm-resolutions.csv", "pm-position-gaps.csv",
                 "mapping.csv", "pm.config.json"):
        if os.path.exists(name):
            shutil.copy(name, os.path.join(args.out, name))
            sizes[name] = "copied"

    # The project root may hold an earlier, larger fills export — the one the
    # resolutions were built from. It is not the same file as the one generated
    # from this collection, so it travels under its own name instead of
    # overwriting it.
    if os.path.exists("target-fills.csv"):
        shutil.copy("target-fills.csv", os.path.join(args.out, "target-fills-prior.csv"))
        sizes["target-fills-prior.csv"] = "copied"

    rules = Counter(r["rule"] for r in sweeps)
    swept = [r["bid_after"] for r in sweeps if r["bid_after"] is not None]
    # Built from what is on disk and from the header that was just written, not
    # from a list kept alongside. The old template described a bundle two
    # schemas out of date: it named a file the export does not write, listed
    # another twice, and called the dead gg.bet ratio "the central metric".
    contents = bundle_contents(args.out)
    with open(os.path.join(args.out, "README.md"), "w", encoding="utf-8") as handle:
        handle.write(DESCRIPTION.format(
            days=len(paths), files=", ".join(os.path.basename(p) for p in paths),
            sweeps=len(sweeps), rules=dict(rules), markets=len(markets),
            coverage_hours=len(coverage), gaps=len(gaps),
            median_after=f"{statistics.median(swept):.3f}" if swept else "n/a",
            span=span, significant=sum(row[1] for row in strata),
            strata=", ".join(f"{row[0]}: {row[2]}/{row[1]}" for row in strata) or "none",
            contents=contents,
            sweep_columns=column_guide(SWEEP_COLUMNS, SWEEP_NOTES),
            fill_columns=column_guide(header_of(args.out, "fill-context.csv"), FILL_NOTES),
            series_columns=column_guide(header_of(args.out, "book-series.csv"),
                                        BOOK_SERIES_NOTES),
        ))

    # The wallet half of the bundle depends on files produced by other steps.
    # Leaving them out quietly is how an incomplete archive gets sent.
    steps = []
    missing = []
    if not os.path.exists("pm-resolutions.csv"):
        missing.append("the market outcomes")
        steps.append(f"node src/pm/resolve.mjs --from {fills_path}")
    if not os.path.exists(os.path.join(args.out, "fills-report.txt")):
        missing.append("the position report")
        steps.append(f"python3 analyze_fills.py --fills {for_report or fills_path}")
    if steps:
        steps.append(f"python3 export.py --db {args.db}")

    with zipfile.ZipFile(f"{args.out}.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(os.listdir(args.out)):
            archive.write(os.path.join(args.out, name), name)

    print(f"wrote {args.out}/ and {args.out}.zip")
    for name, count in sizes.items():
        path = os.path.join(args.out, name)
        size = f"{os.path.getsize(path) / 1024:.0f} KB" if os.path.exists(path) else ""
        print(f"  {name:22} {str(count):>8}  {size}")
    archive_kb = os.path.getsize(f"{args.out}.zip") / 1024
    print(f"  archive               {archive_kb:.0f} KB")
    if archive_kb > 5000:
        print("  ! larger than a conversation will take. The biggest files above are"
              " the ones to trim or leave out.")
    if steps:
        absent = " and ".join(missing)
        print(f"\n  ! no PnL in this bundle — {absent} "
              f"{'are' if len(missing) > 1 else 'is'} missing.")
        if report_error:
            print("    " + report_error.replace("\n", "\n    "))
        print("    This export just wrote the fills it needs. Run, in order:")
        for step in steps:
            print(f"      {step}")


DESCRIPTION = """# Polymarket / gg.bet collection extract

Collected by the repository this file came from. {days} day-file(s): {files}.

Sweeps in this extract span {span}. That is the range of the *sample*; see
`sweeps-strata.csv` for how many significant sweeps each day actually held.
Sample per day (exported/significant): {strata}

The raw store is a few gigabytes, almost all of it order-book snapshots. This
extract keeps what analysis needs and drops the bulk.

## What is here

{contents}

## How to read sweeps.csv

One row per detected collapse of the bid side: {sweeps} rows, sampled from
{significant} that passed the significance filter. Median `bid_after`:
{median_after}. Rule counts: {rules}

The columns below are read off the file this run wrote, not off a description
kept beside it. A column that exists and is not explained here says so in its
own row.

{sweep_columns}

### The three groups that matter

- **The capacity question** — `prior_size_at_00X`, `prior_depth_bid_total`.
  How much of someone else's money already stands in the queue at the price
  the strategy bids at.
- **The value question** — `fair_lower_bound`, `paired_ask_size`, `book_sum`,
  `paired_stale_seconds`. Whether the token was left below what the other half
  of its own market says it is worth, how much size backs that claim, and
  whether the book making it was live. `book_sum` near zero voids the rest of
  the row: both sides were empty.
- **The outcome question** — `high_*`, `resolved_before_*`, `book_frozen`. What
  the bid did next, and whether there was a market left for it to do it in.
  A frozen book and a book nobody bid into look identical without that flag.

## How to read book-series.csv

The same quantities as `sweeps.csv`, as a series rather than a single instant:
one snapshot every thirty seconds from five minutes before each collapse to
fifteen minutes after. A row of `sweeps.csv` cannot show a recovery taking
shape; this can. The window budget reserves 75% for bid_after <= 0.05 and the
rest for higher prices, with day stratification and unused slots redistributed.
This is a sample of sweeps.csv, not an unbiased sample of all markets. The book
table is heartbeated every five seconds per token and a full export of it is
the thing this bundle exists to avoid.

{series_columns}

## How to read fill-context.csv

One row per fill by a studied wallet, assembled from the stored snapshots after
the horizons around it had passed.

{fill_columns}

## How to read markets.csv

- `game_start_time` — when the match started. The subscription window is built
  on it; `end_date` is a resolution deadline (game + 6h for CS2, game + a week
  for tennis) and dates nothing.
- `subscribed_at` / `unsubscribed_at` / `release_reason` — when the book was
  watched, and why watching stopped: `resolved`, `past the hold window`, or
  `window passed unwatched` (the market was first seen after its match).
- `observed_during_game` — 1 when book snapshots exist inside the match itself.
  This is the self-check: rows with 0 were never watched in play, and nothing
  measured on them describes what happened during the match.

## Caveats that matter

- A book *swept* to two cents and a book *resting* at two cents are different
  events; only the first is an opportunity, and `report.txt` reports them
  separately.
- `fair_lower_bound` is a complementary quote, not independent evidence of fair
  value. Outcome books can mirror the same liquidity. Older collector versions
  calculated paired metrics partway through a multi-asset update; apparent
  dislocations may therefore be timing artefacts. Use the continuous research
  journal to reconstruct atomic frames and exclude gaps/stale/crossed books.
- `ggbet_fair` and `dislocation_ratio` are null wherever the market could not be
  matched to gg.bet, which is the great majority of rows. They are kept for
  continuity with earlier extracts; the twin-token columns answer the same
  question without an external feed.
- `resolved_before_*` is filled from resolutions the collector learned about,
  and the CLOB reports those late. A market that settled during the horizon can
  still read 0. `book_frozen` is the signal that does not depend on that.
- Tick size is per market: mostly 0.01, sometimes 0.001. A three-tick move is
  not a fixed number of cents.
- Coverage is counted from heartbeats. Hours with fewer than expected are
  incomplete, and dividing by them overstates any rate.
- `fills-report.txt` separates five exit modes. A position sold in part and
  left to resolve is neither sold nor held: it carries both proceeds and a
  payout, and pooling it with either corrupts that group's hit rate.
- A position with no buys means its purchases happened before collection
  started. Its size is a lower bound, and it is listed in
  `pm-position-gaps.csv` rather than silently averaged in.
- `universe-summary.csv` is what separates "the bot never traded here" from "the
  logger never looked here". It only runs forward: markets seen before the
  journal existed are not in it, so a sample drawn from before that point still
  describes a slice of unknown origin.
"""


if __name__ == "__main__":
    main()
