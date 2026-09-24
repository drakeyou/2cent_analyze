#!/usr/bin/env python3
"""Position-level PnL for the target wallets.

    node src/pm/resolve.mjs --from target-fills.csv    # first, to get outcomes
    python3 analyze_fills.py

Joins target-fills.csv to pm-resolutions.csv and reports what each position
actually returned. The join is on asset_id, which is the resolution file's
token_id: a position is one wallet's holding of one outcome token.

The point of the resolution join is that most positions are never sold. Without
knowing whether an unsold position expired worthless or was redeemed at a
dollar, return can only be measured on the ones that sold — which is
survivorship bias, and makes any strategy look profitable.

Standard library only.
"""

import argparse
import csv
import os
import statistics
from collections import Counter, defaultdict

# Shared so the two scripts cannot disagree about what a timestamp is.
from analyze import parse_timestamp
from trade_identity import unique_fills

TOLERANCE = 1.001  # sells may exceed buys by rounding, not by a position
MODES = ("sold", "partial_won", "partial_lost", "held_won", "held_lost")

# The strategy under study enters at one or two cents. Positions opened near a
# dollar are a different activity, and pooling them hides both.
STRATEGY_ENTRY = 0.05


# export.py copies both files into export/; fall back to there rather than
# making the caller copy them back out before running this.
def _first_present(*paths):
    return next((p for p in paths if os.path.exists(p)), paths[0])


FILLS_DEFAULT = _first_present("target-fills.csv", "export/target-fills.csv")
RESOLUTIONS_DEFAULT = _first_present("pm-resolutions.csv", "export/pm-resolutions.csv")


def read_csv(path):
    if not os.path.exists(path):
        raise SystemExit(f"{path} not found")
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def number(value, fallback=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def load_resolutions(path):
    """token_id -> its outcome, and condition_id -> whether the market settled."""
    if not os.path.exists(path):
        raise SystemExit(f"{path} not found — create it first:\n"
                         f"  node src/pm/resolve.mjs --from {FILLS_DEFAULT} --out {path}")
    by_token = {}
    settled = {}
    for row in read_csv(path):
        by_token[row["token_id"]] = row
        settled[row["condition_id"]] = row.get("closed") == "1"
    return by_token, settled


def build_positions(fills):
    """One position per wallet AND outcome token, in time order."""
    positions = defaultdict(list)
    for row in unique_fills(fills):
        positions[(row['wallet'].lower(), row["asset_id"])].append(row)
    for trades in positions.values():
        trades.sort(key=lambda r: r["ts"])
    return positions


def summarize(asset_id, trades, resolution):
    """Cost, proceeds and fate of one position."""
    buys = [t for t in trades if t["side"] == "BUY"]
    sells = [t for t in trades if t["side"] == "SELL"]
    buy_size = sum(number(t["size"]) for t in buys)
    sell_size = sum(number(t["size"]) for t in sells)
    cost = sum(number(t["size"]) * number(t["price"]) for t in buys)

    # A position whose sells exceed its buys is missing history: the buys
    # happened before the logger was watching. Its size is a lower bound.
    complete = bool(buys) and trades[0]["side"] == "BUY" and sell_size <= buy_size * TOLERANCE
    reason = None
    if not buys:
        reason = "no buys recorded"
    elif trades[0]["side"] != "BUY":
        reason = "first trade is a sell"
    elif sell_size > buy_size * TOLERANCE:
        reason = "sells exceed buys"

    # Only shares we watched being bought can be counted as sold. One Dota
    # position sold 3487.6 against 2615.7 bought, and taking the sale at face
    # value credited it with $1022 of revenue on shares acquired before the
    # collection started. The sales are taken in time order and the overflow
    # dropped, rather than prorated: the earliest sells are the ones the
    # recorded buys could actually have supplied.
    sold = min(sell_size, buy_size)
    proceeds = 0.0
    left = sold
    for trade in sells:
        take = min(number(trade["size"]), left)
        if take <= 0:
            break
        proceeds += take * number(trade["price"])
        left -= take

    remaining = max(0.0, buy_size - sold)
    winner = resolution.get("winner") if resolution else None
    settled = bool(resolution) and resolution.get("closed") == "1" and winner in ("0", "1")
    # What the token actually redeems at, as the resolver read it from CLOB.
    # Assuming a dollar is right today and wrong the day Polymarket resolves
    # something at a fraction.
    payout_price = number(resolution.get("payout_price"), 1.0) if resolution else 1.0

    # Four exits, not two. A position sold in part and left to resolve is
    # neither "sold" nor "held": pooling it with either corrupts that group's
    # hit rate and its multiples, since it carries both proceeds and a payout.
    if not settled and remaining > 0:
        fate = "unresolved"
        payout = None
    elif remaining <= 0:
        fate = "sold"
        payout = 0.0
    else:
        payout = remaining * payout_price if winner == "1" else 0.0
        outcome = "won" if winner == "1" else "lost"
        fate = f"{'partial' if proceeds > 0 else 'held'}_{outcome}"

    revenue = None if payout is None else proceeds + payout
    return {
        "asset_id": asset_id,
        "condition_id": trades[0]["condition_id"],
        "wallet": trades[0]["wallet"],
        "question": (resolution or {}).get("question", ""),
        "sport": (resolution or {}).get("sport", ""),
        "market_level": (resolution or {}).get("market_level", ""),
        "buys": len(buys), "sells": len(sells),
        "buy_size": buy_size, "sell_size": sell_size, "sold_size": sold,
        "remaining": remaining, "incomplete": 0 if complete else 1,
        "entry_price": cost / buy_size if buy_size else None,
        "cost": cost, "proceeds": proceeds, "payout": payout, "revenue": revenue,
        "multiple": (revenue / cost) if revenue is not None and cost > 0 else None,
        "fate": fate, "winner": winner, "complete": complete, "reason": reason,
        "opened": trades[0]["ts"],
        "closed_at": sells[-1]["ts"] if sells else None,
    }


def hold_seconds(position):
    if not position["closed_at"]:
        return None
    start = parse_timestamp(position["opened"])
    end = parse_timestamp(position["closed_at"])
    if start is None or end is None:
        return None
    return (end - start).total_seconds()


def show(values, unit="", places=3):
    if not values:
        return "n/a"
    ordered = sorted(values)
    pick = lambda p: ordered[min(len(ordered) - 1, int(len(ordered) * p))]
    return (f"n={len(values)} min={ordered[0]:.{places}f}{unit} "
            f"p25={pick(0.25):.{places}f}{unit} median={statistics.median(ordered):.{places}f}{unit} "
            f"p75={pick(0.75):.{places}f}{unit} max={ordered[-1]:.{places}f}{unit}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fills", default=FILLS_DEFAULT)
    parser.add_argument("--resolutions", default=RESOLUTIONS_DEFAULT)
    parser.add_argument("--gaps-out", default="pm-position-gaps.csv")
    args = parser.parse_args()

    raw_fills = read_csv(args.fills)
    fills = unique_fills(raw_fills)
    by_token, _ = load_resolutions(args.resolutions)
    positions = [summarize(asset, trades, by_token.get(asset))
                 for (_, asset), trades in build_positions(fills).items()]

    if len(raw_fills) != len(fills):
        print(f"Removed {len(raw_fills) - len(fills)} duplicate daily fill records")

    print(f"{len(fills)} fills over {len(positions)} positions"
          f", {len(by_token)} outcome tokens resolved\n")

    # --- history completeness ------------------------------------------------
    broken = [p for p in positions if not p["complete"]]
    with open(args.gaps_out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["wallet", "asset_id", "condition_id", "buy_size", "sell_size", "sold_size",
                         "first_side", "reason", "incomplete", "cost", "revenue"])
        for p in broken:
            writer.writerow([p["wallet"], p["asset_id"], p["condition_id"], p["buy_size"], p["sell_size"],
                             p["sold_size"], "BUY" if p["buys"] and p["opened"] else "SELL",
                             p["reason"], p["incomplete"], round(p["cost"], 4),
                             None if p["revenue"] is None else round(p["revenue"], 4)])
    print("== history completeness ==")
    print(f"  complete positions   : {len(positions) - len(broken)}/{len(positions)}")
    print(f"  incomplete           : {len(broken)} -> {args.gaps_out}")
    for reason, count in Counter(p["reason"] for p in broken).most_common():
        print(f"      {count:5}  {reason}")

    usable = [p for p in positions if p["complete"]]

    # --- PnL by exit mode ----------------------------------------------------
    # Incomplete positions are reported, not dropped and not pooled. Dropping
    # them made the headline PnL differ from a hand recount by $1022 on a single
    # Dota position, with nothing in the report to say a position was missing.
    modes = defaultdict(list)
    for p in usable:
        modes[p["fate"]].append(p)
    print("\n== PnL by exit mode ==")
    print(f"  {'mode':<14} {'n':>4} {'cost':>10} {'revenue':>10} {'pnl':>10} {'multiple':>9}")
    total_cost = total_revenue = 0.0
    for mode in MODES:
        group = modes.get(mode, [])
        if not group:
            continue
        cost = sum(p["cost"] for p in group)
        revenue = sum(p["revenue"] or 0 for p in group)
        total_cost += cost
        total_revenue += revenue
        print(f"  {mode:<14} {len(group):>4} {cost:>10.2f} {revenue:>10.2f}"
              f" {revenue - cost:>10.2f} {(revenue / cost if cost else 0):>9.2f}x")
    print(f"  {'TOTAL':<14} {len(usable) - len(modes['unresolved']):>4} {total_cost:>10.2f}"
          f" {total_revenue:>10.2f} {total_revenue - total_cost:>10.2f}"
          f" {(total_revenue / total_cost if total_cost else 0):>9.2f}x")

    priced_broken = [p for p in broken if p["revenue"] is not None]
    if priced_broken:
        broken_cost = sum(p["cost"] for p in priced_broken)
        broken_revenue = sum(p["revenue"] for p in priced_broken)
        print(f"  {'incomplete':<14} {len(priced_broken):>4} {broken_cost:>10.2f}"
              f" {broken_revenue:>10.2f} {broken_revenue - broken_cost:>10.2f}"
              f" {(broken_revenue / broken_cost if broken_cost else 0):>9.2f}x")
        grand_cost = total_cost + broken_cost
        grand_revenue = total_revenue + broken_revenue
        grand_n = len(usable) - len(modes["unresolved"]) + len(priced_broken)
        grand_multiple = grand_revenue / grand_cost if grand_cost else 0
        print(f"  {'WITH THOSE':<14} {grand_n:>4} {grand_cost:>10.2f}"
              f" {grand_revenue:>10.2f} {grand_revenue - grand_cost:>10.2f}"
              f" {grand_multiple:>9.2f}x")
        print("    incomplete positions sell more than was seen bought; the sale is"
              " counted only up to what was bought")
    print(f"  excluded as unresolved: {len(modes['unresolved'])} positions"
          f" (cost {sum(p['cost'] for p in modes['unresolved']):.2f})")

    # --- the two-cent strategy, measured apart from everything else ---------
    print(f"\n== split by entry price (strategy enters <= {STRATEGY_ENTRY:.2f}) ==")
    for label, group in (
        ("cheap entries", [p for p in usable if (p["entry_price"] or 0) <= STRATEGY_ENTRY]),
        ("everything else", [p for p in usable if (p["entry_price"] or 0) > STRATEGY_ENTRY]),
    ):
        priced_group = [p for p in group if p["revenue"] is not None]
        cost = sum(p["cost"] for p in priced_group)
        revenue = sum(p["revenue"] for p in priced_group)
        print(f"  {label:<16} n={len(group):<4} cost {cost:>9.2f}  pnl {revenue - cost:>9.2f}"
              f"  {(revenue / cost if cost else 0):>6.2f}x")

    # --- hit rate and multiples, kept apart ---------------------------------
    print("\n== hit rate and multiples ==")
    print("  sold and held to resolution are different exits and are not pooled")
    for mode in MODES:
        group = [p for p in modes.get(mode, []) if p["multiple"] is not None]
        if not group:
            continue
        wins = sum(1 for p in group if p["multiple"] > 1)
        print(f"  {mode:<14} hit {wins}/{len(group)} = {wins / len(group):.1%}")
        print(f"               {show([p['multiple'] for p in group], 'x', 2)}")

    # --- what early exits gave up -------------------------------------------
    sold_winners = [p for p in modes.get("sold", []) if p["winner"] == "1" and p["cost"] > 0]
    print(f"\n== cost of exiting early (sold, then the outcome won: n={len(sold_winners)}) ==")
    if sold_winners:
        left = []
        for p in sold_winners:
            held_revenue = p["buy_size"] * 1.0
            if held_revenue > 0:
                left.append(1 - (p["proceeds"] / held_revenue))
        print(f"  fraction of the payout left on the table: {show(left, '', 3)}")
        print(f"  total actually taken : {sum(p['proceeds'] for p in sold_winners):.2f}")
        print(f"  total if held        : {sum(p['buy_size'] for p in sold_winners):.2f}")
    else:
        print("  no position was both sold and a winner")

    # --- holding time, on survivors only ------------------------------------
    held = [hold_seconds(p) for p in usable if p["closed_at"]]
    held = [h for h in held if h is not None]
    print(f"\n== holding time ==")
    print(f"  measured on {len(held)} of {len(usable)} positions — the ones that sold.")
    print(f"  {len(usable) - len(held)} never sold, so this describes survivors, not the strategy.")
    if held:
        print(f"  seconds: {show(held, 's', 0)}")

    # --- entry price, and whether it varies enough to model -----------------
    entries = [p["entry_price"] for p in usable if p["entry_price"] is not None]
    print("\n== entry price ==")
    print(f"  {show(entries, '', 4)}")
    ticks = Counter(round(e, 3) for e in entries)
    print(f"  distinct entry prices: {len(ticks)} -> {dict(list(ticks.most_common(6)))}")

    priced = [p for p in usable if p["entry_price"] and p["multiple"] is not None]
    print("\n== multiple vs entry price ==")
    if len({round(p["entry_price"], 3) for p in priced}) < 3:
        print("  not enough variation in the predictor: entries sit on one or two ticks.")
        print("  A correlation here would describe noise, so none is reported.")
    else:
        buckets = defaultdict(list)
        for p in priced:
            buckets[round(p["entry_price"], 2)].append(p["multiple"])
        for price, multiples in sorted(buckets.items()):
            print(f"  entry {price:.2f}: n={len(multiples):<4} median {statistics.median(multiples):.2f}x")

    # --- by sport ------------------------------------------------------------
    print("\n== by sport ==")
    by_sport = defaultdict(list)
    for p in usable:
        by_sport[p["sport"] or "(unlabelled)"].append(p)
    for sport, group in sorted(by_sport.items(), key=lambda kv: -len(kv[1])):
        cost = sum(p["cost"] for p in group)
        revenue = sum(p["revenue"] or 0 for p in group if p["revenue"] is not None)
        print(f"  {sport:<26} n={len(group):<4} cost {cost:>8.2f}  pnl {revenue - cost:>9.2f}")


if __name__ == "__main__":
    main()
