#!/usr/bin/env python3
"""The archive's first day is partial, and coverage has to be counted in
distinct minutes, never in rows. An off-grid tick, an extra row inside a
minute, adds to the row count without replacing the row already on the
minute mark.

Three things, from --archive-dir:
1. the first day file's real minute count. Hyperliquid started recording
   partway through the day, so expect well under 1,440.
2. one concrete example of an off-grid tick: a day and coin where the row
   count is higher than the count of distinct minutes, with both
   timestamps shown.
3. the share of all days where the check most people write first,
   `rows == coins * 1440`, would wrongly flag the day, and the smaller
   share that is actually caused by an off-grid tick rather than an
   ordinary listing or delisting partway through the day.

Usage: python probes/partial_first_day.py [--archive-dir PATH]
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

    first = files[0]
    raw = hl_archive.lz4.frame.decompress(first.read_bytes())
    first_df = hl_archive.parse_csv(raw)
    first_minutes = first_df["time"].dt.truncate("1m").n_unique()
    print(f"first day file: {first.name}")
    print(f"  span: {first_df['time'].min()} .. {first_df['time'].max()}")
    print(f"  distinct minutes: {first_minutes} (of a possible 1,440)")
    print()

    naive_fail_days = 0
    offgrid_example: tuple[str, str, int, int] | None = None
    offgrid_coin_days = 0
    t0 = time.time()
    for f in files:
        raw = hl_archive.lz4.frame.decompress(f.read_bytes())
        df = hl_archive.parse_csv(raw)
        n_coins = df["coin"].n_unique()
        expected = n_coins * 1440
        if df.height != expected:
            naive_fail_days += 1

        per_coin = df.group_by("coin").agg(
            pl.len().alias("rows"), pl.col("time").dt.truncate("1m").n_unique().alias("minutes")
        )
        dup = per_coin.filter(pl.col("rows") > pl.col("minutes"))
        if dup.height > 0:
            offgrid_coin_days += 1
            if offgrid_example is None:
                coin = dup.sort("coin").row(0, named=True)["coin"]
                day_rows = (
                    df.filter(pl.col("coin") == coin)
                    .with_columns(pl.col("time").dt.truncate("1m").alias("minute"))
                )
                dup_minute = (
                    day_rows.group_by("minute").len().filter(pl.col("len") > 1).sort("minute").row(0, named=True)
                )["minute"]
                stamps = sorted(
                    day_rows.filter(pl.col("minute") == dup_minute)["time"].to_list()
                )
                offgrid_example = (f.name.replace(".csv.lz4", ""), coin, stamps)

    print(f"checked {len(files)} day file(s) in {time.time() - t0:.1f}s")
    print(f"the check `rows == coins * 1440` wrongly flags: {naive_fail_days} / {len(files)} day(s) "
          f"({100 * naive_fail_days / len(files):.1f}%)")
    print(f"days where at least one coin has an off-grid extra row: {offgrid_coin_days} / {len(files)} day(s) "
          f"({100 * offgrid_coin_days / len(files):.1f}%)")
    if offgrid_example:
        day, coin, stamps = offgrid_example
        print(f"\nconcrete example: {coin} on {day} has two rows in the same minute: {stamps}")
        print("That is an extra row inside the minute. It does not replace the row on the minute mark.")


if __name__ == "__main__":
    main()
