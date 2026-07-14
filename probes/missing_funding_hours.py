#!/usr/bin/env python3
""""Every hour has a settlement, always" is a reasonable assumption about
funding, and it is false. A small number of hours are missing across the
whole exchange at once, meaning every coin that was active skips the same
settlement. If you build a check that expects a full, unbroken hourly
grid, it will fail on real data.

For each sampled coin, pulls its full settlement history (paginated
correctly), builds that coin's own expected settlement grid from its first
row to its last (every 8 hours before the funding scale switch, every hour
after, see hl_archive.FUNDING_HISTORY_SCALE_FLIP), and finds the gaps. A
gap shared by every sampled coin that was active at that hour is reported
as exchange-wide.

Usage: python probes/missing_funding_hours.py [--coins BTC,ETH,ARB,AVAX,SOL,ATOM]
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
from pathlib import Path

from _funding_history import fetch_full_funding_history

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hl_archive  # noqa: E402

_DEFAULT_COINS = "BTC,ETH,ARB,AVAX,SOL,ATOM"


def _coin_gaps(times: list[dt.datetime]) -> set[dt.datetime]:
    """Finds missing settlements against that coin's own expected grid.
    The grid's spacing is not one hour across the whole history.
    Hyperliquid settled every 8 hours (at 00:00, 08:00 and 16:00 UTC)
    before FUNDING_HISTORY_SCALE_FLIP, and every hour from then on.
    Building one flat hourly grid across that boundary would wrongly flag
    21 of every 24 hours in the 8-hourly era as missing, when that is
    simply the correct schedule for that era. Each side of the boundary is
    checked against its own grid, the same way ARCHIVE_FUNDING_SCALE_FLIP
    is handled elsewhere in this repo.
    """
    flip = hl_archive.FUNDING_HISTORY_SCALE_FLIP
    hours = sorted({t.replace(minute=0, second=0, microsecond=0) for t in times})
    if len(hours) < 2:
        return set()

    gaps: set[dt.datetime] = set()
    for step, seg_start, seg_end in (
        (8, hours[0], min(hours[-1], flip - dt.timedelta(hours=1))),
        (1, max(hours[0], flip), hours[-1]),
    ):
        if seg_start > seg_end:
            continue
        present = {h for h in hours if seg_start <= h <= seg_end}
        # Grid points are always aligned to UTC 00:00, 08:00 and 16:00 in
        # the 8-hourly era, or every hour in the hourly era. Never stepped
        # from seg_start itself, since a coin's very first row can land at
        # any hour, not necessarily one of those three.
        full_grid = set()
        cur = seg_start.replace(hour=(seg_start.hour // step) * step)
        while cur < seg_start:
            cur += dt.timedelta(hours=step)
        while cur <= seg_end:
            full_grid.add(cur)
            cur += dt.timedelta(hours=step)
        gaps |= full_grid - present
    return gaps


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--coins", default=_DEFAULT_COINS)
    p.add_argument("--start-year", type=int, default=2023)
    args = p.parse_args()

    coins = args.coins.split(",")
    start_ms = int(dt.datetime(args.start_year, 1, 1, tzinfo=dt.UTC).timestamp() * 1000)

    per_coin_gaps: dict[str, set[dt.datetime]] = {}
    t0 = time.time()
    for coin in coins:
        rows = fetch_full_funding_history(coin, start_ms)
        times = [dt.datetime.fromtimestamp(r["time"] / 1000, tz=dt.UTC) for r in rows]
        gaps = _coin_gaps(times)
        per_coin_gaps[coin] = gaps
        print(f"  {coin}: {len(rows)} settled row(s), {len(gaps)} gap hour(s) in its own grid, "
              f"{time.time() - t0:.1f}s elapsed")

    # An hour is missing across the exchange if every sampled coin that was
    # active around that hour (meaning the hour falls inside that coin's
    # own observed range) is missing it.
    all_gap_hours = set().union(*per_coin_gaps.values()) if per_coin_gaps else set()
    print(f"\nall gap hours found across {len(coins)} sampled coin(s), combined: {len(all_gap_hours)}")
    shared = sorted(h for h in all_gap_hours if all(h in gaps for gaps in per_coin_gaps.values()))
    print(f"gap hours missing for every sampled coin at once: {len(shared)}")
    for h in shared:
        print(f"  {h}")
    if not shared and all_gap_hours:
        print("  Gaps were found for individual coins, but none were shared by every sampled coin. "
              "This sample's coins may not all have been trading during the same events. "
              "Try more coins with --coins, or look at the per-coin counts printed above.")


if __name__ == "__main__":
    main()
