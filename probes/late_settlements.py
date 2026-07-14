#!/usr/bin/env python3
"""Funding settlements can arrive late, and not just from ordinary
per-coin jitter. There are moments where every active coin settles the
same number of milliseconds late, all at once. If you try to fix this by
widening how far apart two timestamps can be and still count as a match,
you end up accepting a value that had not been published yet at the time
you say you are measuring.

Pulls each sampled coin's full settlement history (paginated correctly)
and looks for hours where every coin that settled that hour has the exact
same gap from the hour boundary, above a cutoff.

This uses a small sample of coins, not every coin, so the count here is a
lower bound. See the README for the count across the whole archive. It
will still catch the largest known event, as long as the sampled coins
were trading at the time.

Usage: python probes/late_settlements.py [--coins BTC,ETH,ARB,AVAX,SOL,ATOM] [--cutoff-s 10]
"""

from __future__ import annotations

import argparse
import datetime as dt
import time

import polars as pl
from _funding_history import fetch_full_funding_history

_DEFAULT_COINS = "BTC,ETH,ARB,AVAX,SOL,ATOM"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--coins", default=_DEFAULT_COINS)
    p.add_argument("--cutoff-s", type=float, default=10.0)
    p.add_argument("--start-year", type=int, default=2023)
    args = p.parse_args()

    coins = args.coins.split(",")
    start_ms = int(dt.datetime(args.start_year, 1, 1, tzinfo=dt.UTC).timestamp() * 1000)

    frames = []
    t0 = time.time()
    for coin in coins:
        rows = fetch_full_funding_history(coin, start_ms)
        frames.append(
            pl.DataFrame({"time": [dt.datetime.fromtimestamp(r["time"] / 1000, tz=dt.UTC) for r in rows]})
            .with_columns(pl.lit(coin).alias("coin"))
        )
        print(f"  {coin}: {len(rows)} settled row(s), {time.time() - t0:.1f}s elapsed")

    df = pl.concat(frames).with_columns(pl.col("time").dt.truncate("1h").alias("hour"))
    df = df.with_columns((pl.col("time") - pl.col("hour")).dt.total_milliseconds().alias("offset_ms"))

    by_hour = df.group_by("hour").agg(
        pl.col("offset_ms").min().alias("min_off"),
        pl.col("offset_ms").max().alias("max_off"),
        pl.len().alias("n_coins"),
    )
    events = by_hour.filter(
        (pl.col("min_off") == pl.col("max_off")) & (pl.col("min_off") > args.cutoff_s * 1000)
    ).sort("min_off", descending=True)

    print(f"\n{len(coins)} coin(s), {df.height} total settled row(s), cutoff {args.cutoff_s}s")
    print("moments where every sampled active coin settled the same number of milliseconds late:")
    if events.height == 0:
        print("  none found in this sample and time window")
    for hour, off, _, n in events.iter_rows():
        print(f"  {hour}  +{off / 1000:.3f}s  ({n}/{len(coins)} sampled coins were active that hour)")


if __name__ == "__main__":
    main()
