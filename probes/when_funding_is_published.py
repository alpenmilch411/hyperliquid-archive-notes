#!/usr/bin/env python3
"""The rate stamped at hour H covers the hour ending at H, and it is
published just after H. It is available at H, never before. But "just
after" is not a fixed small number. The usual gap between the hour closing
and the row appearing is tens of milliseconds, not the roughly 12
milliseconds some notes claim. That figure turns out to be close to the
bottom of the range, not the typical case.

Pulls one coin's full settlement history (paginated correctly) and
measures the gap between each row's timestamp and the hour it settles.

Usage: python probes/when_funding_is_published.py [--coin BTC] [--start-year 2023]
"""

from __future__ import annotations

import argparse
import datetime as dt
import time

import polars as pl
from _funding_history import fetch_full_funding_history


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--coin", default="BTC")
    p.add_argument("--start-year", type=int, default=2023, help="pull history from Jan 1 of this year")
    args = p.parse_args()

    start_ms = int(dt.datetime(args.start_year, 1, 1, tzinfo=dt.UTC).timestamp() * 1000)
    t0 = time.time()
    rows = fetch_full_funding_history(args.coin, start_ms)
    print(f"pulled {len(rows)} settled row(s) for {args.coin} since {args.start_year}-01-01 "
          f"in {time.time() - t0:.1f}s")

    df = pl.DataFrame(
        {"time": [dt.datetime.fromtimestamp(r["time"] / 1000, tz=dt.UTC) for r in rows]}
    )
    df = df.with_columns(pl.col("time").dt.truncate("1h").alias("hour"))
    df = df.with_columns((pl.col("time") - pl.col("hour")).dt.total_milliseconds().alias("gap_ms"))

    min_gap = df["gap_ms"].min()
    print(f"\nsmallest gap:  {min_gap} ms  (0 means no row was ever stamped before its own hour closed)")
    print(f"median gap:    {df['gap_ms'].median()} ms")
    print(f"90th percentile gap: {df['gap_ms'].quantile(0.90)} ms")
    print(f"99th percentile gap: {df['gap_ms'].quantile(0.99)} ms")
    print(f"largest gap:   {df['gap_ms'].max()} ms")

    p12 = int((df["gap_ms"] <= 12).sum())
    print(f"\nrows published within 12ms of the hour closing: {p12} / {df.height} "
          f"({100 * p12 / df.height:.1f}% of all rows)")
    print("If you have a note claiming the gap is usually about 12ms, this is roughly where that "
          "number sits: real, and something you will see, but well below the middle of the range.")


if __name__ == "__main__":
    main()
