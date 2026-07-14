#!/usr/bin/env python3
"""`spotMeta`'s `index` field is not the same as the pair's position in
the array. If you write `for pos, pair in enumerate(spot_meta["universe"])`
and use `pos` as the market index, you will silently point at the wrong
market past the first gap. The first pair, `PURR/USDC`, does have index 0
at position 0, so this looks fine until you are many pairs in.

Needs only a network connection.

Usage: python probes/spot_meta_index.py
"""

from __future__ import annotations

import requests

_INFO_URL = "https://api.hyperliquid.xyz/info"


def main() -> None:
    r = requests.post(_INFO_URL, json={"type": "spotMeta"}, timeout=30)
    r.raise_for_status()
    universe = r.json()["universe"]

    indices = [u["index"] for u in universe]
    print(f"pairs in spotMeta.universe: {len(universe)}")
    print(f"max index:                  {max(indices)}")
    print(f"gaps (max_index + 1 - n_pairs): {max(indices) + 1 - len(universe)}")

    for pos, u in enumerate(universe):
        if u["index"] != pos:
            print(f"first divergence: array position {pos} holds index={u['index']} (name={u['name']!r})")
            break
    else:
        print("no difference found. Index equals position for every pair, today.")


if __name__ == "__main__":
    main()
