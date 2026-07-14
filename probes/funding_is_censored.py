#!/usr/bin/env python3
"""Funding is a censored value. Hyperliquid's funding formula is

    F_8h = premium + clip(interest - premium, -C, +C)

When the clamp is not binding, the clip term is just `interest - premium`,
so the whole formula becomes `premium + interest - premium`. The premium
cancels out. What is left is the interest rate, and nothing else. A large
share of settled funding rows sit exactly on that interest rate (an eighth
of it, in the hourly era), not because the market was calm, but because the
formula puts them there no matter what the market did.

Pulls each sampled coin's full settlement history (paginated correctly).
Each row's `funding` here is computed directly from that same row's
`premium`, unlike the archive's per-minute column, which is a running
prediction and does not match the formula row by row. Reports the split
between rows pinned on the interest rate, rows at exactly zero, and rows
that actually carry information, plus a check of the reason itself: among
rows where the clamp is definitely binding, how often do they still land
on the pinned value? It should be close to zero if the formula really is
what is causing the pin.

Usage: python probes/funding_is_censored.py [--coins BTC,ETH,ARB,AVAX,SOL,ATOM]
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from pathlib import Path

import polars as pl
from _funding_history import fetch_full_funding_history

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hl_archive  # noqa: E402

_DEFAULT_COINS = "BTC,ETH,ARB,AVAX,SOL,ATOM"
_PINNED_VALUE = hl_archive.CLAMP_INTEREST / 8  # 1.25e-5


def _clamp_bound(t: dt.datetime) -> float:
    return hl_archive.CLAMP_BOUND_EARLY if t < hl_archive.CLAMP_BOUND_CHANGE else hl_archive.CLAMP_BOUND_LATE


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--coins", default=_DEFAULT_COINS)
    p.add_argument("--start-year", type=int, default=2023)
    args = p.parse_args()

    coins = args.coins.split(",")
    start_ms = int(
        max(dt.datetime(args.start_year, 1, 1), hl_archive.FUNDING_HISTORY_SCALE_FLIP.replace(tzinfo=None)).replace(
            tzinfo=dt.UTC
        ).timestamp()
        * 1000
    )

    frames = []
    t0 = time.time()
    for coin in coins:
        rows = fetch_full_funding_history(coin, start_ms)
        frames.append(
            pl.DataFrame(
                {
                    "time": [dt.datetime.fromtimestamp(r["time"] / 1000, tz=dt.UTC) for r in rows],
                    "premium": [float(r["premium"]) for r in rows],
                    "funding": [float(r["fundingRate"]) for r in rows],
                }
            ).with_columns(pl.lit(coin).alias("coin"))
        )
        print(f"  {coin}: {len(rows)} settled row(s), {time.time() - t0:.1f}s elapsed")

    df = pl.concat(frames)
    n_total = df.height
    n_pinned = int((df["funding"] == _PINNED_VALUE).sum())
    n_zero = int((df["funding"] == 0.0).sum())
    n_informative = n_total - n_pinned - n_zero

    print(f"\n{len(coins)} coin(s), {n_total} settled row(s), hourly era only, all pulled since "
          f"{hl_archive.FUNDING_HISTORY_SCALE_FLIP}")
    print(f"pinned on the interest rate ({_PINNED_VALUE}): {n_pinned} ({100 * n_pinned / n_total:.2f}%)")
    print(f"exactly zero:                                  {n_zero} ({100 * n_zero / n_total:.2f}%)")
    print(f"neither, so actually informative:               {n_informative} ({100 * n_informative / n_total:.2f}%)")

    bound = pl.col("time").map_elements(_clamp_bound, return_dtype=pl.Float64)
    binding = df.filter((hl_archive.CLAMP_INTEREST - pl.col("premium")).abs() > bound)
    n_binding = binding.height
    n_binding_and_pinned = int((binding["funding"] == _PINNED_VALUE).sum())
    frac = n_binding_and_pinned / n_binding if n_binding else float("nan")
    print("\ncheck: among rows where the clamp is definitely binding (using the right bound for that "
          "row's era), how many still land exactly on the pinned value?")
    print(f"  {n_binding_and_pinned} / {n_binding} row(s) ({frac:.2e})")
    print("  This should be close to zero. A binding clamp predicts a value away from the pin, by "
          "the shape of the formula itself, so landing on the pin anyway would be a coincidence.")


if __name__ == "__main__":
    main()
