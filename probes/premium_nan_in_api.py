#!/usr/bin/env python3
"""Checks whether the settled `fundingHistory` API ever returns `NaN` for
`premium` on a dead or delisted coin, as opposed to the archive, which
writes a fabricated `0.0` for the same condition (see zeros_not_nulls.py).

The API returns every number as a JSON string, for example
`"fundingRate":"-0.0002254221"`. For a dead coin, the `premium` string is
literally `"NaN"`. If you check `isinstance(value, float)` on the parsed
JSON, this check silently never fires, because the value is a string, not
a float, until you convert it yourself. This script converts every
`premium` and `fundingRate` string with `float(...)` first, the way any
real caller would have to, and only then checks for NaN.

Pulls each named coin's full settlement history (paginated correctly) and
counts NaN two ways: directly in the raw text of the HTTP response, and
after converting each string to a float.

This checks a specific, previously-claimed set of coins and a specific
row count. If you are re-running this because you read a claim citing the
same 9 coins and about 4,427 rows, look at what this script finds today
and compare it to what you read. Do not assume the two still match.

Usage: python probes/premium_nan_in_api.py [--coins STRAX,FRIEND,NFTI,OX,UNIBOT,SHIA,PANDORA,ZRO,RLB]
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import time

import requests
from _funding_history import INFO_URL

_DEFAULT_COINS = "STRAX,FRIEND,NFTI,OX,UNIBOT,SHIA,PANDORA,ZRO,RLB"
_PAGE_ROWS = 500


def _to_float(value: object) -> float:
    """The API sends every number as a string. `float("NaN")` correctly
    returns a real NaN, the same as `float("0.00005")` returns a real
    number, so this is the one place a caller has to do the conversion
    Hyperliquid does not do for you."""
    return float(value)  # type: ignore[arg-type]


def _fetch_full_history_raw(coin: str, start_ms: int, *, sleep_s: float = 0.3) -> tuple[list[dict], int]:
    """Pulls one coin's full settlement history, page by page, the same
    correct way as _funding_history.fetch_full_funding_history. Also counts
    the raw 'NaN' text in each page's response body before it gets parsed,
    so nothing about how JSON parsing handles the value can hide it."""
    rows: list[dict] = []
    raw_nan_hits = 0
    cursor = start_ms
    while True:
        delay = 1.0
        for attempt in range(7):
            r = requests.post(INFO_URL, json={"type": "fundingHistory", "coin": coin, "startTime": cursor}, timeout=30)
            if r.status_code == 429 and attempt < 6:
                time.sleep(delay)
                delay = min(delay * 2, 30.0)
                continue
            r.raise_for_status()
            break
        text = r.text
        raw_nan_hits += text.count("NaN") + text.count("nan")
        page = r.json()
        if not page:
            break
        rows.extend(page)
        if len(page) < _PAGE_ROWS:
            break
        cursor = page[-1]["time"] + 1
        time.sleep(sleep_s)
    return rows, raw_nan_hits


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--coins", default=_DEFAULT_COINS)
    p.add_argument("--start-year", type=int, default=2023)
    args = p.parse_args()

    coins = args.coins.split(",")
    start_ms = int(dt.datetime(args.start_year, 1, 1, tzinfo=dt.UTC).timestamp() * 1000)

    total_rows = 0
    total_raw_nan_hits = 0
    total_nan_premium = 0
    total_nan_rate = 0
    t0 = time.time()
    for coin in coins:
        rows, raw_hits = _fetch_full_history_raw(coin, start_ms)
        total_rows += len(rows)
        total_raw_nan_hits += raw_hits
        nan_premium = sum(1 for r in rows if math.isnan(_to_float(r["premium"])))
        nan_rate = sum(1 for r in rows if math.isnan(_to_float(r["fundingRate"])))
        total_nan_premium += nan_premium
        total_nan_rate += nan_rate
        print(f"  {coin:<10} {len(rows)} row(s), raw 'NaN' text found {raw_hits} time(s), "
              f"premium is NaN on {nan_premium} row(s), fundingRate is NaN on {nan_rate} row(s), "
              f"{time.time() - t0:.1f}s elapsed")

    print(f"\n{len(coins)} coin(s), {total_rows} total settled row(s) since {args.start_year}-01-01")
    print(f"raw 'NaN'/'nan' text found in the HTTP response bodies: {total_raw_nan_hits} time(s)")
    print(f"premium converts to NaN on:     {total_nan_premium} row(s)")
    print(f"fundingRate converts to NaN on: {total_nan_rate} row(s)")


if __name__ == "__main__":
    main()
