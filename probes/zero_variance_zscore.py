#!/usr/bin/env python3
"""A rolling z-score of funding can come out as exactly plus or minus
infinity on a real, current row that is not dead. Which sign it lands on
is decided by rounding noise in floating-point math, not by the market.

Reason: a coin can sit exactly on the pinned interest rate (see
funding_is_censored.py) for a whole rolling window without moving. That
makes the rolling standard deviation over that window exactly 0.0.
Dividing by that gives infinity. When the top of the division is not
exactly zero either, it turns out to be the smallest number a float64 can
represent next to the pinned value, left over from rounding inside the
rolling sum. This script checks that directly against `math.ulp`, which is
Python's name for that smallest representable step.

Builds an hourly series per coin from the archive's per-minute data (the
value at exactly HH:00:00, hourly era only, built entirely from
--archive-dir with no API calls), then computes a rolling z-score and finds
where the rolling standard deviation is exactly zero on a row that is
genuinely pinned.

Usage: python probes/zero_variance_zscore.py [--archive-dir PATH] [--window-hours 720]
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hl_archive  # noqa: E402

_DEFAULT_ARCHIVE_DIR = Path("data")
_PINNED_VALUE = hl_archive.CLAMP_INTEREST / 8


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--archive-dir", type=Path, default=_DEFAULT_ARCHIVE_DIR)
    p.add_argument("--window-hours", type=int, default=720, help="rolling window, in hourly samples (720 = 30 days)")
    p.add_argument("--min-samples", type=int, default=20)
    args = p.parse_args()

    files = sorted(args.archive_dir.glob("*.csv.lz4"))
    if not files:
        print(f"no *.csv.lz4 files found in {args.archive_dir}")
        raise SystemExit(1)

    hourly_frames = []
    t0 = time.time()
    for f in files:
        raw = hl_archive.lz4.frame.decompress(f.read_bytes())
        df = hl_archive.parse_csv(raw)
        df = df.filter(
            (pl.col("time") >= hl_archive.ARCHIVE_FUNDING_SCALE_FLIP)
            & (pl.col("time").dt.minute() == 0)
            & (pl.col("time").dt.second() == 0)
        )
        if df.height > 0:
            hourly_frames.append(df.select("time", "coin", "funding"))

    hourly = pl.concat(hourly_frames).sort(["coin", "time"])
    print(f"built {hourly.height} hourly row(s) across {hourly['coin'].n_unique()} coin(s) "
          f"from {len(files)} day file(s), {time.time() - t0:.1f}s")

    hourly = hourly.with_columns(
        pl.col("funding").rolling_mean(window_size=args.window_hours, min_samples=args.min_samples)
        .over("coin").alias("roll_mean"),
        pl.col("funding").rolling_std(window_size=args.window_hours, min_samples=args.min_samples)
        .over("coin").alias("roll_std"),
    )
    hourly = hourly.with_columns((pl.col("funding") - pl.col("roll_mean")).alias("top"))

    pinned = hourly.filter((pl.col("funding") == _PINNED_VALUE) & (pl.col("roll_std") == 0.0))
    print(f"\nrows where the coin is pinned and its rolling standard deviation is exactly 0.0: "
          f"{pinned.height} across {pinned['coin'].n_unique() if pinned.height else 0} coin(s)")

    if pinned.height == 0:
        print("none found at this window and min-samples setting. Try a different --window-hours.")
        return

    z = pinned["top"] / pinned["roll_std"]
    n_pos_inf = int((z == float("inf")).sum())
    n_neg_inf = int((z == float("-inf")).sum())
    n_nan = int(z.is_nan().sum())
    print(f"z-score on those rows: +infinity on {n_pos_inf}, -infinity on {n_neg_inf}, "
          f"not a number on {n_nan}, anything else on {pinned.height - n_pos_inf - n_neg_inf - n_nan}")

    ulp = math.ulp(_PINNED_VALUE)
    nonzero_top = pinned.filter(pl.col("top") != 0.0)
    if nonzero_top.height > 0:
        at_ulp = int((nonzero_top["top"].abs() == ulp).sum())
        print(f"\nthe smallest step a float64 can take next to {_PINNED_VALUE} is {ulp}")
        print(f"of {nonzero_top.height} row(s) where the top of the division is not exactly zero, "
              f"{at_ulp} ({100 * at_ulp / nonzero_top.height:.1f}%) are exactly that one step in size")
        print("That is rounding noise, not a real difference. Its sign is what decides "
              "+infinity versus -infinity.")


if __name__ == "__main__":
    main()
