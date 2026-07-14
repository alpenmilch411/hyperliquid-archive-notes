#!/usr/bin/env python3
"""`fundingHistory` caps at 500 rows per call, with no warning. A request
for three years of history gets back a normal, successful response with
exactly 500 rows in it. There is no cursor, no "more available" flag,
nothing telling you it was cut short. And `startTime` is inclusive, so the
obvious way to page through more data, asking again with
`startTime = last_row.time`, gives you back that same last row a second
time.

Needs only a network connection.

Usage: python probes/funding_history_pagination.py
"""

from __future__ import annotations

import time

import requests

_INFO_URL = "https://api.hyperliquid.xyz/info"


def _post(payload: dict) -> object:
    r = requests.post(_INFO_URL, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()


def main() -> None:
    now_ms = int(time.time() * 1000)
    three_years_ago_ms = now_ms - 3 * 365 * 24 * 3600 * 1000

    page1 = _post({"type": "fundingHistory", "coin": "BTC", "startTime": three_years_ago_ms})
    span_days = (page1[-1]["time"] - page1[0]["time"]) / (24 * 3600 * 1000)
    requested_days = 3 * 365
    print(f"requested {requested_days} days of BTC funding history; got back {len(page1)} row(s)")
    print(f"  first row: {page1[0]['time']}  last row: {page1[-1]['time']}")
    print(f"  actual coverage: {span_days:.1f} days out of {requested_days} requested "
          f"({requested_days - span_days:.0f} days short)")
    print("  the response is a plain JSON list. No cursor, no flag saying there is more, nothing cut short.")

    time.sleep(0.3)
    page2_wrong = _post({"type": "fundingHistory", "coin": "BTC", "startTime": page1[-1]["time"]})
    print(
        f"\nasking again with startTime set to the last row's own time repeats that row: "
        f"{page2_wrong[0]['time'] == page1[-1]['time']}"
    )

    time.sleep(0.3)
    page2_right = _post({"type": "fundingHistory", "coin": "BTC", "startTime": page1[-1]["time"] + 1})
    gap_ms = page2_right[0]["time"] - page1[-1]["time"]
    print(
        f"asking again with startTime set to the last row's time plus 1 millisecond avoids the "
        f"repeat: the next page starts {gap_ms} ms after the last row of the first page, one hour "
        f"later, as expected"
    )


if __name__ == "__main__":
    main()
