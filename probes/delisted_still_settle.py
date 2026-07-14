#!/usr/bin/env python3
"""Delisted perps do not stop settling funding. They keep publishing an
hourly settlement of exactly `0.0`, for as long as the API keeps answering
at all. Assuming a coin's history ends the day it was delisted is wrong
for roughly a quarter of the whole universe.

Needs only a network connection.

Usage: python probes/delisted_still_settle.py [--sample N]
"""

from __future__ import annotations

import argparse
import time

import requests

_INFO_URL = "https://api.hyperliquid.xyz/info"


def _post(payload: dict) -> object:
    r = requests.post(_INFO_URL, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--sample", type=int, default=7, help="how many delisted coins to check")
    args = p.parse_args()

    meta = _post({"type": "meta"})
    universe = meta["universe"]
    delisted = [u["name"] for u in universe if u.get("isDelisted")]
    print(f"total perps in meta.universe: {len(universe)}")
    print(f"isDelisted=true count:        {len(delisted)} ({100 * len(delisted) / len(universe):.1f}%)")

    now_ms = int(time.time() * 1000)
    start_ms = now_ms - 3 * 24 * 3600 * 1000  # last 3 days
    sample = delisted[: args.sample]
    print(f"\nchecking {len(sample)} of {len(delisted)} delisted coins over the last 3 days:")
    for coin in sample:
        time.sleep(0.2)
        rows = _post({"type": "fundingHistory", "coin": coin, "startTime": start_ms})
        rates = sorted({r["fundingRate"] for r in rows})
        expected = 3 * 24
        print(f"  {coin:<12} {len(rows)}/{expected} hourly row(s), distinct fundingRate value(s) = {rates}")


if __name__ == "__main__":
    main()
