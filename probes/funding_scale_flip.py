#!/usr/bin/env python3
"""The funding rate switches from an 8-hour schedule to an hourly one on
2023-06-08, but the archive's per-minute `funding` column and the API's
settled `fundingHistory` series do not switch at the same moment.

Method: Hyperliquid's own funding formula,

    F_8h = premium + clip(interest - premium, -C, +C)

predicts the 8-hour rate from `premium`. A row "matches" a divisor if the
published rate equals F_8h divided by that divisor, to float64 precision.
Around the switch, take a vote among the rows that match exactly one
divisor (a row that matches both or neither tells you nothing) and find
the moment the vote flips from mostly dividing by 1 to mostly dividing by 8.

Archive check (--archive-dir): does this minute by minute on the raw
per-minute `funding` column, for whichever day files between
2023-06-06 and 2023-06-12 are present.

API check (network): does this hour by hour on the settled `fundingHistory`
series, for a sample of coins that were already listed on the archive's
first day.

Usage: python probes/funding_scale_flip.py [--archive-dir PATH]
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from pathlib import Path

import polars as pl
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hl_archive  # noqa: E402

_DEFAULT_ARCHIVE_DIR = Path("data")
_WINDOW_DAYS = [dt.date(2023, 6, 6) + dt.timedelta(days=i) for i in range(7)]
_INFO_URL = "https://api.hyperliquid.xyz/info"
# Coins already listed on the archive's first day (2023-05-20), so they are
# guaranteed to have both funding history and archive coverage across the
# June 2023 switch window.
_SAMPLE_COINS = ["BTC", "ETH", "ARB", "AVAX", "SOL", "MATIC", "LINK", "ATOM"]


def _clamp_fit_exprs(divisor: int) -> pl.Expr:
    bound = hl_archive.CLAMP_BOUND_EARLY  # this window is months before CLAMP_BOUND_CHANGE
    clamped = pl.col("premium") + (hl_archive.CLAMP_INTEREST - pl.col("premium")).clip(-bound, bound)
    return (pl.col("funding") - clamped / divisor).abs() < hl_archive.CLAMP_EXACT


def _majority_flip(df: pl.DataFrame, time_col: str, granularity: str) -> tuple[object, object]:
    """Votes by timestamp, using only rows that match exactly one divisor.
    Returns (last_time_dividing_by_1, first_time_dividing_by_8)."""
    df = df.with_columns(
        _clamp_fit_exprs(1).alias("fits_d1"),
        _clamp_fit_exprs(8).alias("fits_d8"),
    )
    decisive = df.filter(pl.col("fits_d1") != pl.col("fits_d8"))
    bucket = pl.col(time_col).dt.truncate(granularity)
    votes = decisive.group_by(bucket.alias("bucket")).agg(
        pl.col("fits_d1").sum().alias("d1_votes"),
        pl.col("fits_d8").sum().alias("d8_votes"),
    ).sort("bucket")
    votes = votes.with_columns(
        pl.when(pl.col("d1_votes") > pl.col("d8_votes")).then(pl.lit("D1")).otherwise(pl.lit("D8")).alias("majority")
    )
    d1_buckets = votes.filter(pl.col("majority") == "D1")["bucket"]
    d8_buckets = votes.filter(pl.col("majority") == "D8")["bucket"]
    last_d1 = d1_buckets.max() if d1_buckets.len() > 0 else None
    first_d8 = d8_buckets.min() if d8_buckets.len() > 0 else None
    return last_d1, first_d8


def archive_half(archive_dir: Path) -> None:
    frames = []
    for day in _WINDOW_DAYS:
        f = archive_dir / f"{day:%Y%m%d}.csv.lz4"
        if not f.exists():
            continue
        raw = hl_archive.lz4.frame.decompress(f.read_bytes())
        frames.append(hl_archive.parse_csv(raw))
    if not frames:
        print(f"[archive] no day files found in {archive_dir} for {_WINDOW_DAYS[0]}..{_WINDOW_DAYS[-1]}, skipping")
        return
    df = pl.concat(frames).filter(pl.col("premium").is_not_null() & (pl.col("premium") != 0.0))
    last_d1, first_d8 = _majority_flip(df, "time", "1m")
    print(f"[archive] {df.height} candidate rows across {len(frames)} day file(s)")
    print(f"[archive] last minute with D1 (8-hourly) majority:  {last_d1}")
    print(f"[archive] first minute with D8 (hourly) majority:   {first_d8}")
    print(f"[archive] hl_archive.ARCHIVE_FUNDING_SCALE_FLIP:    {hl_archive.ARCHIVE_FUNDING_SCALE_FLIP}")


def api_half() -> None:
    start_ms = int(dt.datetime(2023, 6, 6, tzinfo=dt.UTC).timestamp() * 1000)
    rows = []
    for coin in _SAMPLE_COINS:
        r = requests.post(_INFO_URL, json={"type": "fundingHistory", "coin": coin, "startTime": start_ms}, timeout=30)
        r.raise_for_status()
        for row in r.json():
            rows.append(
                {
                    "time": dt.datetime.fromtimestamp(row["time"] / 1000, tz=dt.UTC),
                    "premium": float(row["premium"]),
                    "funding": float(row["fundingRate"]),
                }
            )
        time.sleep(0.2)
    df = pl.DataFrame(rows).filter(pl.col("premium") != 0.0)
    if df.height == 0:
        print("[api] no non-zero-premium rows returned, skipping")
        return
    last_d1, first_d8 = _majority_flip(df, "time", "1h")
    print(f"[api] {df.height} settled rows across {len(_SAMPLE_COINS)} coin(s)")
    print(f"[api] last hour with D1 (8-hourly) majority:  {last_d1}")
    print(f"[api] first hour with D8 (hourly) majority:   {first_d8}")
    print(f"[api] hl_archive.FUNDING_HISTORY_SCALE_FLIP:  {hl_archive.FUNDING_HISTORY_SCALE_FLIP}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--archive-dir", type=Path, default=_DEFAULT_ARCHIVE_DIR)
    args = p.parse_args()

    archive_half(args.archive_dir)
    print()
    api_half()
    print()
    gap = hl_archive.FUNDING_HISTORY_SCALE_FLIP - hl_archive.ARCHIVE_FUNDING_SCALE_FLIP
    print(f"gap between the two documented boundaries: {gap}")


if __name__ == "__main__":
    main()
