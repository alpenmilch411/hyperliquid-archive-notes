#!/usr/bin/env python3
"""`open_interest` can legitimately go negative around a delisting
settlement. It is a quantity of the coin, not a dollar amount, so ranking
by the raw column instead of by (open_interest times mark price) picks the
wrong "worst" row, off by as much as 58 times.

Goes through every day file in --archive-dir for negative open_interest
rows, converts each one to a dollar amount, and prints the top 5 by each
ranking so the difference is visible directly.

Usage: python probes/negative_open_interest.py [--archive-dir PATH]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hl_archive  # noqa: E402

_DEFAULT_ARCHIVE_DIR = Path("data")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--archive-dir", type=Path, default=_DEFAULT_ARCHIVE_DIR)
    args = p.parse_args()

    files = sorted(args.archive_dir.glob("*.csv.lz4"))
    if not files:
        print(f"no *.csv.lz4 files found in {args.archive_dir}")
        raise SystemExit(1)

    rows = []
    t0 = time.time()
    for f in files:
        raw = hl_archive.lz4.frame.decompress(f.read_bytes())
        df = hl_archive.parse_csv(raw)
        neg = df.filter(pl.col("open_interest") < 0)
        if neg.height > 0:
            day = f.name.replace(".csv.lz4", "")
            neg = neg.with_columns(pl.lit(day).alias("day"))
            rows.append(neg.select("day", "time", "coin", "open_interest", "mark_px"))

    if not rows:
        print(f"0 negative open_interest rows found across {len(files)} day file(s) in {time.time() - t0:.1f}s")
        return

    neg_all = pl.concat(rows).with_columns((pl.col("open_interest") * pl.col("mark_px")).alias("oi_usd"))
    n_days = neg_all.select("day").n_unique()
    n_coins = neg_all.select("coin").n_unique()
    print(f"checked {len(files)} day file(s), took {time.time() - t0:.1f}s")
    print(f"negative open_interest: {neg_all.height} row(s), {n_coins} coin(s), {n_days} distinct day(s)")
    print()

    worst_raw = neg_all.sort("open_interest").head(1).row(0, named=True)
    print(
        f"worst by the raw column, in coin units: {worst_raw['coin']} on {worst_raw['day']} "
        f"= {worst_raw['open_interest']:.2f} coin, which is {worst_raw['oi_usd']:.2f} dollars "
        f"at a price of {worst_raw['mark_px']}"
    )

    print("\ntop 5 by dollar amount (open_interest times mark price), which is the number that "
          "actually matters:")
    print("(one event is one day and coin; the rows behind it are usually a handful of consecutive minutes)")
    events = (
        neg_all.group_by(["day", "coin"])
        .agg(
            pl.col("open_interest").min().alias("open_interest"),
            pl.col("oi_usd").min().alias("oi_usd"),
            pl.len().alias("rows"),
        )
        .sort("oi_usd")
        .head(5)
    )
    for r in events.iter_rows(named=True):
        print(f"  {r['day']}  {r['coin']:<12} {r['open_interest']:>18,.2f} (coin)  {r['oi_usd']:>18,.2f} USD  "
              f"({r['rows']} row(s))")


if __name__ == "__main__":
    main()
