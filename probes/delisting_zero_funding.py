#!/usr/bin/env python3
"""Before a coin dies, Hyperliquid stops publishing a real funding rate
for it. Funding goes to exactly `0.0` and can stay there for the rest of
the coin's recorded life. This is not a brief window right before
delisting, and the row still looks like it has volume, because
`day_ntl_vlm` freezes at its last live value instead of dropping to zero.

For each named coin, goes through --archive-dir day by day and reports:
the day its order book died (mid_px stuck at zero), whether funding
dropped to zero in that same window, whether day_ntl_vlm froze at a
constant value afterward, and how many days the zero-funding stretch has
run as of the most recent day present.

Usage: python probes/delisting_zero_funding.py [--coins STRAX,FRIEND]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hl_archive  # noqa: E402

_DEFAULT_ARCHIVE_DIR = Path("data")
_DEFAULT_COINS = "STRAX,FRIEND"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--archive-dir", type=Path, default=_DEFAULT_ARCHIVE_DIR)
    p.add_argument("--coins", default=_DEFAULT_COINS)
    args = p.parse_args()

    coins = args.coins.split(",")
    files = sorted(args.archive_dir.glob("*.csv.lz4"))
    if not files:
        print(f"no *.csv.lz4 files found in {args.archive_dir}")
        raise SystemExit(1)

    per_day: dict[str, list[dict]] = {c: [] for c in coins}
    for f in files:
        raw = hl_archive.lz4.frame.decompress(f.read_bytes())
        raw_df = hl_archive.parse_csv(raw)
        day = f.name.replace(".csv.lz4", "")
        for coin in coins:
            cdf = raw_df.filter(pl.col("coin") == coin)
            if cdf.height == 0:
                continue
            clean = hl_archive.validate_and_clean(cdf, expect_minutes=1440)
            per_day[coin].append(
                {
                    "day": day,
                    "minutes": clean.height,
                    "mid_missing_frac": float(clean["mid_px_missing"].mean()),
                    "funding_zero_frac": float((clean["funding"] == 0.0).mean()),
                    "last_funding": clean["funding"][-1],
                    "day_ntl_vlm": clean["day_ntl_vlm"][-1],
                }
            )

    for coin in coins:
        rows = per_day[coin]
        if not rows:
            print(f"{coin}: not present in any day file in {args.archive_dir}")
            continue

        dead_days = [r for r in rows if r["mid_missing_frac"] >= 0.99]
        if not dead_days:
            print(f"{coin}: {len(rows)} day(s) present, book never went 99% or more missing. "
                  f"Still trading normally.")
            continue
        block_start = dead_days[0]["day"]
        block_start_idx = rows.index(dead_days[0])
        last_day = rows[-1]

        zero_from_start = all(r["funding_zero_frac"] >= 0.99 for r in rows[block_start_idx:])
        span_days = len(rows) - block_start_idx

        # day_ntl_vlm can freeze at its last live value for a while, then
        # LATER reset to 0.0. Report the sequence of distinct values after
        # the book died, with how many days each one held, rather than
        # assume it freezes forever.
        after = rows[block_start_idx:]
        vlm_runs: list[tuple[float, int]] = []
        for r in after:
            v = r["day_ntl_vlm"]
            if vlm_runs and vlm_runs[-1][0] == v:
                vlm_runs[-1] = (v, vlm_runs[-1][1] + 1)
            else:
                vlm_runs.append((v, 1))

        print(f"\n{coin}:")
        print(f"  day the book died (99% or more of minutes missing a mid price): {block_start}")
        print(f"  funding stays 99% or more zero from that day to the most recent day present "
              f"({last_day['day']}): {zero_from_start}")
        print(f"  days from the book dying to the most recent day present: {span_days}")
        print(f"  day_ntl_vlm after the book died, as (value, days it held that value): "
              f"{vlm_runs[:6]}{' and more' if len(vlm_runs) > 6 else ''}")
        if vlm_runs and vlm_runs[0][1] > 1:
            ending = "eventually reset to 0" if len(vlm_runs) > 1 else "the data available here ends"
            print(f"  It froze at its last live value ({vlm_runs[0][0]}) for {vlm_runs[0][1]} day(s) "
                  f"before it {ending}. For that whole stretch, the row looks like it still has "
                  f"volume, even though the market is dead.")


if __name__ == "__main__":
    main()
