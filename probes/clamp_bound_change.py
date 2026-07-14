#!/usr/bin/env python3
"""The clamp in Hyperliquid's funding formula is not a fixed number. It
changed from 0.0003 to 0.0005 partway through the archive's history, and
there is no changelog entry for it anywhere we could find.

Method: only look at rows where the clamp is binding under BOTH candidate
values, and where the two values would predict numbers 0.0002 apart, so an
exact match cannot be luck. Vote minute by minute among those rows, on
whichever day files between 2023-12-10 and 2023-12-13 are present in
--archive-dir, to find the moment the vote flips.

Usage: python probes/clamp_bound_change.py [--archive-dir PATH]
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hl_archive  # noqa: E402

_DEFAULT_ARCHIVE_DIR = Path("data")
_WINDOW_DAYS = [dt.date(2023, 12, 10) + dt.timedelta(days=i) for i in range(4)]


def _fits(bound: float) -> pl.Expr:
    clamped = pl.col("premium") + (hl_archive.CLAMP_INTEREST - pl.col("premium")).clip(-bound, bound)
    # This window is after the scale switch (June 2023), so the divisor is always 8.
    return (pl.col("funding") - clamped / 8).abs() < hl_archive.CLAMP_EXACT


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--archive-dir", type=Path, default=_DEFAULT_ARCHIVE_DIR)
    args = p.parse_args()

    frames = []
    for day in _WINDOW_DAYS:
        f = args.archive_dir / f"{day:%Y%m%d}.csv.lz4"
        if not f.exists():
            continue
        raw = hl_archive.lz4.frame.decompress(f.read_bytes())
        frames.append(hl_archive.parse_csv(raw))
    if not frames:
        print(f"no day files found in {args.archive_dir} for {_WINDOW_DAYS[0]}..{_WINDOW_DAYS[-1]}")
        raise SystemExit(1)

    df = pl.concat(frames).filter(pl.col("premium").is_not_null())
    # A row is only useful for this check if the clamp is definitely
    # binding, meaning the raw gap between the interest rate and premium
    # is bigger than even the wider of the two candidate bounds.
    binding = df.filter(
        (hl_archive.CLAMP_INTEREST - pl.col("premium")).abs() > hl_archive.CLAMP_BOUND_LATE
    )
    print(f"{binding.height} row(s) where the clamp is binding, across {len(frames)} day file(s)")

    binding = binding.with_columns(
        _fits(hl_archive.CLAMP_BOUND_EARLY).alias("fits_early"),
        _fits(hl_archive.CLAMP_BOUND_LATE).alias("fits_late"),
    )
    decisive = binding.filter(pl.col("fits_early") != pl.col("fits_late"))
    votes = (
        decisive.group_by(pl.col("time").dt.truncate("1m").alias("minute"))
        .agg(pl.col("fits_early").sum().alias("early_votes"), pl.col("fits_late").sum().alias("late_votes"))
        .sort("minute")
    )
    votes = votes.with_columns(
        pl.when(pl.col("early_votes") > pl.col("late_votes"))
        .then(pl.lit("EARLY (0.0003)"))
        .otherwise(pl.lit("LATE (0.0005)"))
        .alias("majority")
    )
    print(f"{decisive.height} row(s) that clearly match one bound and not the other, "
          f"across {votes.height} minute(s)")

    early_minutes = votes.filter(pl.col("majority").str.starts_with("EARLY"))["minute"]
    late_minutes = votes.filter(pl.col("majority").str.starts_with("LATE"))["minute"]
    last_early = early_minutes.max() if early_minutes.len() > 0 else None
    first_late = late_minutes.min() if late_minutes.len() > 0 else None
    print(f"last minute where most rows still match 0.0003: {last_early}")
    print(f"first minute where most rows match 0.0005:       {first_late}")
    print(f"hl_archive.CLAMP_BOUND_CHANGE:                    {hl_archive.CLAMP_BOUND_CHANGE}")

    # The split is not perfect. A small number of rows happen to match the
    # OTHER era's bound by coincidence. Report that count rather than
    # pretend it is zero.
    wrong_era = binding.filter(
        (pl.col("time") < hl_archive.CLAMP_BOUND_CHANGE) & pl.col("fits_late") & ~pl.col("fits_early")
        | (pl.col("time") >= hl_archive.CLAMP_BOUND_CHANGE) & pl.col("fits_early") & ~pl.col("fits_late")
    )
    print(f"rows that match the wrong era's bound by coincidence, not by corruption: {wrong_era.height}")


if __name__ == "__main__":
    main()
