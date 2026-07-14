#!/usr/bin/env python3
"""HyperEVM's read precompiles return today's state no matter what block
height you ask `eth_call` for. There is no error. A question like "what
was the price a year ago" succeeds and silently gives you today's price
instead. `eth_call` is a free, unsigned, local simulation, so this is
cheap to test directly rather than just reading it in the documentation.

Needs only a network connection. Calls the public HyperEVM RPC.

Usage: python probes/precompiles_ignore_block_height.py
"""

from __future__ import annotations

import time

import requests

_EVM_RPC = "https://rpc.hyperliquid.xyz/evm"
# The documented oracle-price read precompile, perp index 0 (BTC in meta.universe).
_ORACLE_PX_PRECOMPILE = "0x0000000000000000000000000000000000000807"
_CALLDATA_PERP_0 = "0x" + "0" * 64


def _rpc_call(method: str, params: list) -> str:
    r = requests.post(_EVM_RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=15)
    r.raise_for_status()
    return r.json()["result"]


def main() -> None:
    current_block = int(_rpc_call("eth_blockNumber", []), 16)
    print(f"current HyperEVM block: {current_block}")

    # The clean test: call the SAME fixed historical block (0x1, near
    # genesis) five times in a row, a second apart. If the precompile
    # really read historical state, block 1's answer could never change
    # between calls, because history does not change. Comparing DIFFERENT
    # block heights against each other, done below too, does not prove
    # this cleanly on its own, because BTC's live price can move between
    # one request and the next anyway. Asking the same question five times
    # removes that problem.
    print("\ncalling the SAME historical block (0x1) 5 times, about 1 second apart:")
    same_block_vals = []
    for i in range(5):
        val = _rpc_call("eth_call", [{"to": _ORACLE_PX_PRECOMPILE, "data": _CALLDATA_PERP_0}, "0x1"])
        same_block_vals.append(val)
        print(f"  call {i}: {val} = {int(val, 16)}")
        time.sleep(1)
    same_block_changed = len(set(same_block_vals)) > 1
    print(f"the value changed across calls that all asked for the same block: {same_block_changed}")
    if same_block_changed:
        print("  Block 1's state cannot change. This is today's price, not history.")

    print("\nfor context, five different requested heights, no error at any of them:")
    blocks_to_try = {
        "latest": "latest",
        "current-1": hex(current_block - 1),
        "current-1,000": hex(current_block - 1000),
        "current-1,000,000": hex(current_block - 1_000_000),
        "block 0x1 (near genesis)": "0x1",
    }
    for label, blk in blocks_to_try.items():
        val = _rpc_call("eth_call", [{"to": _ORACLE_PX_PRECOMPILE, "data": _CALLDATA_PERP_0}, blk])
        print(f"  eth_call at {label:<26} (block={blk:>10}) -> {val} = {int(val, 16)}")
    print(
        "No error at any height, including block 1. A question about the past succeeds and "
        "hands back a price that is still tracking today's market, with no way to get real "
        "history from this RPC. Any small differences between the heights above are real BTC "
        "price movement between one request and the next, not an effect of the block height. "
        "See the repeated-call test above for the clean version of this check."
    )


if __name__ == "__main__":
    main()
