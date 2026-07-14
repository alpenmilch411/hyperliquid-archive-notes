"""Shared helper: pulls a coin's full settled fundingHistory using correct
pagination (see funding_history_pagination.py for why paging the wrong way
duplicates rows). Not a probe on its own. Imported by the probes that need
a full settlement series instead of one 500-row page.
"""

from __future__ import annotations

import time

import requests

INFO_URL = "https://api.hyperliquid.xyz/info"
_PAGE_ROWS = 500  # the API's own silent cap


def _post_with_backoff(payload: dict, *, max_retries: int = 6) -> object:
    """POST to the info API, retrying on HTTP 429 with exponential backoff.
    This project's own per-IP rate budget is shared across every probe run
    in a session, so a heavy pagination loop needs to survive a 429 rather
    than crash the whole probe."""
    delay = 1.0
    for attempt in range(max_retries + 1):
        r = requests.post(INFO_URL, json=payload, timeout=30)
        if r.status_code == 429 and attempt < max_retries:
            time.sleep(delay)
            delay = min(delay * 2, 30.0)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError("unreachable")


def fetch_full_funding_history(coin: str, start_ms: int, *, sleep_s: float = 0.4) -> list[dict]:
    """Every settled fundingHistory row for `coin` from `start_ms` to now,
    paginated correctly (startTime is inclusive, so each next page starts
    at last_row.time + 1, never at last_row.time verbatim)."""
    rows: list[dict] = []
    cursor = start_ms
    while True:
        page = _post_with_backoff({"type": "fundingHistory", "coin": coin, "startTime": cursor})
        if not page:
            break
        rows.extend(page)
        if len(page) < _PAGE_ROWS:
            break
        cursor = page[-1]["time"] + 1
        time.sleep(sleep_s)
    return rows
