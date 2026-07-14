#!/usr/bin/env python3
"""There is no feed of every liquidation across the whole market on
Hyperliquid's public API. This script tries three plausible request types
against the live API and shows they are all rejected, then prints the
documented list of WebSocket subscriptions for reference. Liquidation data
only ever shows up inside a single user's own `userEvents` or
`userNonFundingLedgerUpdates` stream, which needs that user's address. It
is never a stream of everyone's liquidations at once.

Needs only a network connection.

Usage: python probes/no_liquidation_feed.py
"""

from __future__ import annotations

import requests

_INFO_URL = "https://api.hyperliquid.xyz/info"

# The full documented list of WebSocket subscription `type` values
# (Hyperliquid docs, WebSocket subscriptions page). There is no standalone
# "liquidations" entry in it.
_DOCUMENTED_WS_TYPES = (
    "allMids", "notification", "webData3", "twapStates", "clearinghouseState",
    "openOrders", "candle", "l2Book", "trades", "orderUpdates", "userEvents",
    "userFills", "userFundings", "userNonFundingLedgerUpdates", "activeAssetCtx",
    "activeAssetData", "userTwapSliceFills", "userTwapHistory", "bbo", "spotState",
    "allDexsClearinghouseState", "allDexsAssetCtxs", "outcomeMetaUpdates", "fastAssetCtxs",
)


def main() -> None:
    print("trying three made-up liquidation request types against the REST Info API:")
    for t in ("liquidations", "liquidationHistory", "allLiquidations"):
        r = requests.post(_INFO_URL, json={"type": t}, timeout=30)
        print(f'  {{"type": "{t}"}} gets back HTTP {r.status_code}')

    has_liquidation_channel = any("liquidation" in t.lower() for t in _DOCUMENTED_WS_TYPES)
    print(f"\ndocumented WebSocket subscription types ({len(_DOCUMENTED_WS_TYPES)} total): {_DOCUMENTED_WS_TYPES}")
    print(f'a standalone "liquidations" channel is in that list: {has_liquidation_channel}')
    print(
        "Liquidation data only exists inside userEvents or userNonFundingLedgerUpdates, "
        "which are subscriptions for one user's own account and require that account's "
        "address. There is no channel for every liquidation across the market."
    )


if __name__ == "__main__":
    main()
