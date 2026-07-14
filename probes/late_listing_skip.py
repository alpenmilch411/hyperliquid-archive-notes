#!/usr/bin/env python3
"""One thing this repo could not fully explain: a small number of coins do
not get a funding settlement for the hour they were first seen in. Their
first settlement lands one hour later than "first hour seen, plus one"
would predict. The obvious explanation, that a coin listed too late in the
hour gets skipped for that hour, does not hold up. Some coins first seen
even later in the hour settle completely normally, and at least one coin
settles earlier than the rule would predict.

Archive check: a full scan of --archive-dir finds each coin's true first
minute in the data. Coins already present on the archive's own first day
are left out, since that reflects when the archive started recording, not
a real listing event.

API check (network): for a small named sample, the coins already known to
behave differently plus some for comparison, pulls each coin's first
settlement and compares it to first hour seen, plus one.

Usage: python probes/late_listing_skip.py [--archive-dir PATH]
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
_INFO_URL = "https://api.hyperliquid.xyz/info"
# 3 coins already known to skip a settlement, plus coins for comparison:
# some listed even later in the hour that settle normally, and some listed
# exactly on the hour, including the one that settles early.
_SAMPLE_COINS = ["RUNE", "ZRO", "MEME", "CELO", "HBAR", "BSV", "HMSTR", "RENDER", "XAI", "BLZ"]


def archive_first_minutes(archive_dir: Path) -> dict[str, dt.datetime]:
    files = sorted(archive_dir.glob("*.csv.lz4"))
    if not files:
        print(f"no *.csv.lz4 files found in {archive_dir}")
        raise SystemExit(1)

    first_seen: dict[str, dt.datetime] = {}
    bootstrap_coins: set[str] = set()
    t0 = time.time()
    for i, f in enumerate(files):
        raw = hl_archive.lz4.frame.decompress(f.read_bytes())
        df = hl_archive.parse_csv(raw)
        mins = df.group_by("coin").agg(pl.col("time").min().alias("t"))
        for coin, t in mins.iter_rows():
            if coin not in first_seen:
                first_seen[coin] = t
                if i == 0:
                    bootstrap_coins.add(coin)
    print(f"scanned {len(files)} day file(s) in {time.time() - t0:.1f}s, "
          f"{len(first_seen)} distinct coin(s), {len(bootstrap_coins)} on the archive's own first day "
          f"(left out, since that is not a real listing)")
    return {c: t for c, t in first_seen.items() if c not in bootstrap_coins}


def first_settlement(coin: str) -> dt.datetime | None:
    r = requests.post(_INFO_URL, json={"type": "fundingHistory", "coin": coin, "startTime": 0}, timeout=30)
    r.raise_for_status()
    rows = r.json()
    if not rows:
        return None
    return dt.datetime.fromtimestamp(rows[0]["time"] / 1000, tz=dt.UTC)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--archive-dir", type=Path, default=_DEFAULT_ARCHIVE_DIR)
    args = p.parse_args()

    first_minutes = archive_first_minutes(args.archive_dir)

    print(f"\nsample of {len(_SAMPLE_COINS)} coin(s):")
    for coin in _SAMPLE_COINS:
        fm = first_minutes.get(coin)
        if fm is None:
            print(f"  {coin}: not found (or left out because it was on the archive's first day)")
            continue
        fs = first_settlement(coin)
        time.sleep(0.2)
        if fs is None:
            print(f"  {coin}: first minute {fm}, no settlement history returned")
            continue
        expected = fm.replace(minute=0, second=0, microsecond=0, tzinfo=dt.UTC) + dt.timedelta(hours=1)
        skipped_hours = round((fs - expected).total_seconds() / 3600)
        print(f"  {coin:<8} first minute {fm}  (:{fm.minute:02d})  "
              f"expected settlement {expected}  actual {fs}  "
              f"{'MATCHES' if skipped_hours == 0 else f'OFFSET {skipped_hours:+d}h'}")


if __name__ == "__main__":
    main()
