#!/usr/bin/env python3
"""What does the time on an archive row mean? A row stamped 14:57:00 is one
snapshot of the whole exchange, taken at 14:57:00 to within about a
second, with every field from the same instant. It is not a minute
aggregate. Lined up against a live websocket, its values can show up a
couple of seconds after 14:57:00, which looks like look-ahead; most of
that is the websocket's own delivery delay.

This probe measures that against a recording of Hyperliquid's public
websocket. Two subcommands:

  record   subscribe `allDexsAssetCtxs` (the whole universe, one push
           every ~15 s) plus `activeAssetCtx` (one push a second), `bbo` and
           `trades` for a few coins, and write every frame as JSONL with the
           time it arrived here, in milliseconds. Main dex only; trade
           rows are written without the `users` and `hash` fields.
           Nothing private is involved: no key, no address.

  compare  for every archive row inside the recording, and every field,
           find the recorded frames whose value equals the row's value,
           and report where those frames sit relative to the row's
           timestamp T. Values are compared as decimals, never as floats
           or text (see values_match for the one tolerance).

The recording has to overlap archive days you have on disk, so record,
wait for the day to appear in the bucket (it lands the next morning, UTC),
download it, then compare.

Every offset here is measured against the time a frame ARRIVED on my
machine, not the time Hyperliquid produced it. The websocket delivers late
by a few hundred milliseconds, so the true offset on the exchange's clock
is earlier than the measured one by about that much. `compare` estimates
the delivery lag from the `trades` frames, which do carry the exchange's
time, and more roughly for the context frames themselves by lining their
mid price up against `bbo`.

Usage:
  python probes/row_timestamp_semantics.py record --minutes 60 --out rec.jsonl [--coins BTC,ETH]
  python probes/row_timestamp_semantics.py compare --archive-dir ./data --recording rec.jsonl
"""

from __future__ import annotations

import argparse
import bisect
import csv
import io
import json
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import lz4.frame
import requests

INFO_URL = "https://api.hyperliquid.xyz/info"
WS_URL = "wss://api.hyperliquid.xyz/ws"

# archive column -> how to read the same value out of a websocket ctx
FIELDS: tuple[str, ...] = (
    "funding",
    "open_interest",
    "prev_day_px",
    "day_ntl_vlm",
    "premium",
    "oracle_px",
    "mark_px",
    "mid_px",
    "impact_bid_px",
    "impact_ask_px",
)
_CTX_KEY = {
    "funding": "funding",
    "open_interest": "openInterest",
    "prev_day_px": "prevDayPx",
    "day_ntl_vlm": "dayNtlVlm",
    "premium": "premium",
    "oracle_px": "oraclePx",
    "mark_px": "markPx",
    "mid_px": "midPx",
}
WINDOW_MS = 30_000  # look this far either side of T for a matching frame (two universe pushes)
INF = float("inf")


# ---------------------------------------------------------------------------
# pure helpers
# ---------------------------------------------------------------------------


def ctx_value(ctx: dict, field: str) -> str:
    """The websocket's value for an archive column, as a string. The
    websocket sends null where the archive writes 0 (README trap 5), so
    null comes back as "0" here."""
    if field in ("impact_bid_px", "impact_ask_px"):
        pxs = ctx.get("impactPxs")
        v = pxs[0 if field == "impact_bid_px" else 1] if pxs else None
    else:
        v = ctx.get(_CTX_KEY[field])
    return "0" if v is None else str(v)


NEAR_REL = 1e-12  # far below one lot or one tick on any coin, far above float64 noise


def values_match(archive: str, ws: str) -> str | None:
    """How an archive string matches a websocket string: "exact" when the
    two are the same decimal number, "near" when they differ only in float
    noise (relative difference at most NEAR_REL, a few thousand times
    float64's own resolution, and still far below a single lot or tick);
    None otherwise. The near case exists because open interest and daily
    volume are running sums: the websocket prints them with their float
    noise ("1106151.6259999992"), and the archive sometimes carries
    different noise for the same quantity ("1106151.626")."""
    try:
        a, w = Decimal(archive), Decimal(ws)
    except InvalidOperation:
        return None
    if a == w:
        return "exact"
    scale = max(abs(a), abs(w))
    if scale and abs(a - w) <= scale * Decimal(NEAR_REL):
        return "near"
    return None


def match_run(times: list[int], hits: list[bool], t: int) -> tuple[float, int, int, float] | None:
    """Given frames at `times` (ascending) and whether each one matched,
    take the run of consecutive matching frames whose nearest frame is
    closest to `t`. Returns (lo, first, last, hi): the value was on screen
    from `first` to `last`, and not on screen at `lo` (the frame before
    the run) or `hi` (the frame after it). lo/hi are -inf/+inf when the run
    touches the edge of what we have. None when no frame matched."""
    idx = [i for i, h in enumerate(hits) if h]
    if not idx:
        return None
    best = min(idx, key=lambda i: (abs(times[i] - t), times[i]))
    a = b = best
    while a > 0 and hits[a - 1]:
        a -= 1
    while b + 1 < len(hits) and hits[b + 1]:
        b += 1
    lo = times[a - 1] if a > 0 else -INF
    hi = times[b + 1] if b + 1 < len(times) else INF
    return lo, times[a], times[b], hi


def common_window(windows: list[tuple[float, float]]) -> tuple[float, float] | None:
    """The instants (open interval) every field's value was consistent
    with, or None when no single instant fits them all."""
    lo = max(w[0] for w in windows)
    hi = min(w[1] for w in windows)
    return (lo, hi) if lo < hi else None


def pct(values: list[float], q: float) -> float:
    s = sorted(values)
    if not s:
        return float("nan")
    k = min(len(s) - 1, max(0, round(q / 100 * (len(s) - 1))))
    return s[k]


# ---------------------------------------------------------------------------
# record
# ---------------------------------------------------------------------------


def record(minutes: float, out: Path, coins: list[str]) -> None:
    import asyncio

    import websockets

    universe = requests.post(INFO_URL, json={"type": "meta"}, timeout=30).json()["universe"]
    names = [u["name"] for u in universe]

    async def run() -> int:
        n = 0
        deadline = time.time() + minutes * 60
        with open(out, "w") as fh:
            fh.write(json.dumps({"universe": {"": names}, "recorded_from": WS_URL}) + "\n")
            async with websockets.connect(WS_URL, max_size=2**24, ping_interval=None) as ws:
                subs = [{"type": "allDexsAssetCtxs"}]
                for c in coins:
                    subs += [{"type": "activeAssetCtx", "coin": c}, {"type": "trades", "coin": c},
                             {"type": "bbo", "coin": c}]
                for s in subs:
                    await ws.send(json.dumps({"method": "subscribe", "subscription": s}))
                last_ping = time.time()
                while time.time() < deadline:
                    if time.time() - last_ping > 50:
                        await ws.send(json.dumps({"method": "ping"}))
                        last_ping = time.time()
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=5)
                    except TimeoutError:
                        continue
                    recv_ms = int(time.time() * 1000)
                    frame = json.loads(raw)
                    ch = frame.get("channel")
                    if ch == "allDexsAssetCtxs":
                        main = [c for c in frame["data"]["ctxs"] if c[0] == ""]
                        frame = {"channel": ch, "data": {"ctxs": main}}
                    elif ch == "trades":
                        for t in frame["data"]:
                            t.pop("users", None)
                            t.pop("hash", None)
                    elif ch not in ("activeAssetCtx", "bbo"):
                        continue
                    fh.write(json.dumps({"recv_ms": recv_ms, "frame": frame}, separators=(",", ":")) + "\n")
                    n += 1
        return n

    n = asyncio.run(run())
    print(f"wrote {n} frames over {minutes} minutes to {out}")


# ---------------------------------------------------------------------------
# compare
# ---------------------------------------------------------------------------


def _load_recording(path: Path):
    """Per coin: universe frames (allDexsAssetCtxs, every ~15 s) and fine
    frames (activeAssetCtx, every ~1 s), each as ([recv_ms], [ctx]). The
    two channels are kept apart: frames from two channels can arrive in a
    different order from the one they were made in, and the windows below
    rely on that order."""
    names: list[str] = []
    uni: dict[str, tuple[list[int], list[dict]]] = defaultdict(lambda: ([], []))
    fine: dict[str, tuple[list[int], list[dict]]] = defaultdict(lambda: ([], []))
    uni_times: list[int] = []
    lat: list[int] = []
    bbo: dict[str, list[tuple[int, Decimal]]] = defaultdict(list)
    with open(path) as fh:
        for line in fh:
            x = json.loads(line)
            if "universe" in x:
                names = x["universe"][""]
                continue
            r, fr = x["recv_ms"], x["frame"]
            ch = fr["channel"]
            if ch == "allDexsAssetCtxs":
                ctxs = fr["data"]["ctxs"][0][1]
                if len(ctxs) != len(names):
                    raise SystemExit(f"universe length changed mid-recording ({len(ctxs)} vs {len(names)}); "
                                     "re-record with a fresh meta")
                uni_times.append(r)
                for name, ctx in zip(names, ctxs, strict=True):
                    uni[name][0].append(r)
                    uni[name][1].append(ctx)
            elif ch == "activeAssetCtx":
                coin = fr["data"]["coin"]
                fine[coin][0].append(r)
                fine[coin][1].append(fr["data"]["ctx"])
            elif ch == "bbo":
                d = fr["data"]
                bid, ask = d["bbo"]
                if bid and ask:
                    bbo[d["coin"]].append((d["time"], (Decimal(bid["px"]) + Decimal(ask["px"])) / 2))
            elif ch == "trades" and fr["data"]:
                lat.append(r - max(t["time"] for t in fr["data"]))
    for d in (uni, fine):
        for c, (ts, cs) in d.items():
            order = sorted(range(len(ts)), key=ts.__getitem__)
            d[c] = ([ts[i] for i in order], [cs[i] for i in order])
    return names, dict(uni), fine, sorted(uni_times), lat, {c: sorted(v) for c, v in bbo.items()}


def ctx_lag_windows(fine_times: list[int], fine_ctxs: list[dict],
                    bbo: list[tuple[int, Decimal]]) -> list[tuple[int, int]]:
    """activeAssetCtx frames carry no exchange time, but their midPx is the
    book's mid, and `bbo` frames carry the book with the exchange's time.
    When a ctx frame shows a new mid, find the one stretch of exchange time
    in which the book had that mid; the frame's state is from inside it, so
    its delivery lag lies in (recv - stretch end, recv - stretch start].
    Frames where the mid maps to no stretch or to more than one are skipped."""
    bt = [b[0] for b in bbo]
    out = []
    for k in range(1, len(fine_times)):
        m0, m1 = fine_ctxs[k - 1].get("midPx"), fine_ctxs[k].get("midPx")
        if m1 is None or m1 == m0:
            continue
        mid, r = Decimal(m1), fine_times[k]
        i, j = bisect.bisect_left(bt, r - 5_000), bisect.bisect_right(bt, r)
        runs = []
        x = i
        while x < j:
            if bbo[x][1] == mid:
                start = x
                while x + 1 < len(bbo) and bbo[x + 1][1] == mid:
                    x += 1
                runs.append((bbo[start][0], bbo[x + 1][0] if x + 1 < len(bbo) else None))
            x += 1
        if len(runs) == 1 and runs[0][1] is not None:
            a, e = runs[0]
            out.append((r - e, r - a))
    return out


def _load_archive(archive_dir: Path, t_lo: int, t_hi: int) -> tuple[list[dict], Counter]:
    rows: list[dict] = []
    dex_prefixes: Counter = Counter()
    for f in sorted(archive_dir.glob("*.csv.lz4")):
        text = lz4.frame.decompress(f.read_bytes()).decode()
        for row in csv.DictReader(io.StringIO(text)):
            if ":" in row["coin"]:
                dex_prefixes[row["coin"].split(":")[0]] += 1
                continue
            t = int(datetime.strptime(row["time"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp() * 1000)
            if t_lo <= t <= t_hi:
                row["_t"] = t
                rows.append(row)
    return rows, dex_prefixes


def _covered(uni_times: list[int], t: int, max_gap_ms: int = 20_000) -> bool:
    """True when the universe feed was flowing around t: a frame within
    max_gap_ms on both sides and no hole wider than that within ±60 s."""
    i = bisect.bisect_left(uni_times, t - 60_000)
    j = bisect.bisect_right(uni_times, t + 60_000)
    seg = uni_times[i:j]
    if not seg or seg[0] > t - 60_000 + max_gap_ms or seg[-1] < t + 60_000 - max_gap_ms:
        return False
    return all(b - a <= max_gap_ms for a, b in zip(seg, seg[1:], strict=False))


def bracket(archive: str, values: list[str], times: list[int], t: int, reach_ms: int = 20_000):
    """For a value no frame shows: the pair of consecutive frames within
    reach of t whose values sit strictly either side of it. That is where
    the value lived, between two pushes. Returns (t_before, t_after) or None."""
    try:
        a = Decimal(archive)
    except InvalidOperation:
        return None
    best = None
    for k in range(len(times) - 1):
        if times[k + 1] < t - reach_ms or times[k] > t + reach_ms:
            continue
        try:
            v0, v1 = Decimal(values[k]), Decimal(values[k + 1])
        except InvalidOperation:
            continue
        if min(v0, v1) < a < max(v0, v1):
            cand = (times[k], times[k + 1])
            if best is None or abs(sum(cand) / 2 - t) < abs(sum(best) / 2 - t):
                best = cand
    return best


def offset_profile(windows: list[tuple[float, float]], grid: range) -> list[tuple[int, float]]:
    """For each candidate offset d on the grid: the share of rows whose
    window (lo, hi), relative to T, contains d."""
    n = len(windows)
    return [(d, sum(1 for lo, hi in windows if lo < d < hi) / n) for d in grid] if n else []


def _row_windows(row: dict, ts: list[int], cs: list[dict], stats: dict | None):
    """Per field: ("match", lo, first, last, hi), ("bracket", lo, hi) or None."""
    t = row["_t"]
    i, j = bisect.bisect_left(ts, t - WINDOW_MS), bisect.bisect_right(ts, t + WINDOW_MS)
    wts, wcs = ts[i:j], cs[i:j]
    out = {}
    hit_sets = []
    for f in FIELDS:
        vals = [ctx_value(c, f) for c in wcs]
        modes = [values_match(row[f], v) for v in vals]
        if stats is not None:
            for v, m in zip(vals, modes, strict=True):
                if m:
                    stats["modes"][f][m] += 1
                    stats["same_text"][f] += v == row[f]
                if m == "near":
                    a, w = Decimal(row[f]), Decimal(v)
                    stats["near_max"][f] = max(stats["near_max"][f], float(abs(a - w) / max(abs(a), abs(w))))
        hits = [m is not None for m in modes]
        hit_sets.append(hits)
        run = match_run(wts, hits, t)
        if run is not None:
            out[f] = ("match", *run)
        else:
            br = bracket(row[f], vals, wts, t)
            out[f] = ("bracket", *br) if br else None
    one_frame = [wts[k] for k in range(len(wts)) if all(h[k] for h in hit_sets)]
    return out, one_frame


def _analyse(rows: list[dict], streams: dict, label: str, *, example: tuple[str, int] | None = None,
             per_minute: bool = False, lag_marks: tuple[tuple[str, float], ...] = (), coarse: bool = False) -> None:
    rows = [r for r in rows if r["coin"] in streams]
    stats = {"modes": {f: Counter() for f in FIELDS}, "same_text": Counter(), "near_max": Counter()}
    n = len(rows)
    print(f"\n== {label}: {n} archive rows ==")
    if not n:
        return
    field_rows = {f: Counter() for f in FIELDS}
    first_after: dict[str, list[float]] = {f: [] for f in FIELDS}
    one_frame_rows = 0
    one_frame_offsets: list[float] = []
    win_match: list[tuple[float, float]] = []  # rows where every field matched a frame
    win_all: list[tuple[float, float]] = []  # rows where every field matched or was bracketed
    split_rows = Counter()
    unexplained = 0
    by_minute: dict[int, list[tuple[float, float]]] = defaultdict(list)
    for row in rows:
        ts, cs = streams[row["coin"]]
        t = row["_t"]
        w, one = _row_windows(row, ts, cs, stats)
        if one:
            one_frame_rows += 1
            one_frame_offsets.append(min(one, key=lambda x: abs(x - t)) - t)
        spans_m, spans_a = [], []
        for f in FIELDS:
            x = w[f]
            if x is None:
                field_rows[f]["none"] += 1
                continue
            if x[0] == "bracket":
                field_rows[f]["bracket"] += 1
                spans_a.append((x[1] - t, x[2] - t))
                continue
            _, lo, first, last, hi = x
            field_rows[f]["match"] += 1
            if first > t:
                field_rows[f]["after_only"] += 1
                first_after[f].append(first - t)
            elif last < t:
                field_rows[f]["before_only"] += 1
            spans_m.append((lo - t, hi - t))
            spans_a.append((lo - t, hi - t))
        if any(w[f] is None for f in FIELDS):
            unexplained += 1
            continue
        cw = common_window(spans_a)
        if cw is None:
            for k, f in enumerate(FIELDS):
                if common_window(spans_a[:k] + spans_a[k + 1:]) is not None:
                    split_rows[f] += 1
            split_rows["(total)"] += 1
            continue
        win_all.append(cw)
        by_minute[t].append(cw)
        if len(spans_m) == len(FIELDS):
            win_match.append(cw)
        if example and row["coin"] == example[0] and t == example[1]:
            print(f"worked example {row['coin']} {row['time']}: every field fits a frame received "
                  f"between T{cw[0]:+.0f} ms and T{cw[1]:+.0f} ms")

    print(f"{'field':<14} {'matched':>8} {'between':>8} {'neither':>8} {'near':>8} "
          f"{'only after T':>13} {'only before T':>14}   first seen after T: p50 / p99 / max ms")
    for f in FIELDS:
        c = field_rows[f]
        m = max(c["match"], 1)
        mc = stats["modes"][f]
        fa = first_after[f]
        print(f"{f:<14} {100 * c['match'] / n:>7.1f}% {100 * c['bracket'] / n:>7.1f}% {100 * c['none'] / n:>7.1f}% "
              f"{100 * mc['near'] / max(sum(mc.values()), 1):>7.1f}% {100 * c['after_only'] / m:>12.1f}% "
              f"{100 * c['before_only'] / m:>13.1f}%   {pct(fa, 50):>6.0f} / {pct(fa, 99):>6.0f} / "
              f"{max(fa, default=float('nan')):>6.0f}")
    print("  matched = some frame shows exactly this value; between = no frame does, but it sits strictly")
    print("  between two consecutive frames' values (it lived between two pushes); neither = no explanation")
    print("  within reach. 'only after T' is a share of the matched rows.")
    print("  number formatting, over every matching (row, frame) pair: the same TEXT in both / near-only, "
          "largest relative gap:")
    for f in FIELDS:
        mc = stats["modes"][f]
        tot = max(sum(mc.values()), 1)
        print(f"    {f:<14} same text {100 * stats['same_text'][f] / tot:5.1f}%   near {100 * mc['near'] / tot:5.1f}%"
              f"   largest near gap {stats['near_max'][f]:.1e}")

    print(f"\nrows where one single frame matches all 10 fields: {one_frame_rows} / {n} "
          f"({100 * one_frame_rows / n:.1f}%); that frame's offset from T: p1 {pct(one_frame_offsets, 1):.0f}  "
          f"p50 {pct(one_frame_offsets, 50):.0f}  p99 {pct(one_frame_offsets, 99):.0f} ms")
    judged = len(win_all) + split_rows["(total)"]
    print(f"rows where every field is explained (matched or between): {judged} / {n}; "
          f"of those, one instant fits all 10 fields: {len(win_all)} ({100 * len(win_all) / max(judged, 1):.2f}%)")
    if split_rows["(total)"]:
        culprits = {k: v for k, v in split_rows.most_common() if k != "(total)"}
        print(f"  rows no single instant fits: {split_rows['(total)']}; removing one field fixes it when "
              f"that field is: {culprits}")
    if not win_all:
        return

    def show(name: str, wins: list[tuple[float, float]]) -> None:
        """lo/hi are on MY receive clock. A frame received at T+x shows the
        exchange's state from T+x-lag, so a snapshot is provably after T on
        the exchange's clock only when lo exceeds the delivery lag."""
        prof = offset_profile(wins, range(-5000, 10001, 100))
        top = max(s for _, s in prof)
        best = [d for d, s in prof if s == top]
        near = [d for d, s in prof if s >= 0.99 * top]
        if coarse:
            print(f"{name}: {len(wins)}")
            print(f"  the one offset that fits the most rows: {best[0]}..{best[-1]} ms, fitting {100 * top:.2f}%; "
                  f"within 1% of that: {near[0]}..{near[-1]} ms")
            print("  (one push every ~15 s: a price that moves and comes back between pushes gives false early or")
            print("  late matches here, so read the timing from the fine section; this only checks the same offset")
            print("  holds across the whole universe)")
            return
        los = [lo for lo, _ in wins if lo != -INF]
        his = [hi for _, hi in wins if hi != INF]
        after = sum(1 for lo, _ in wins if lo >= 0)
        before = sum(1 for _, hi in wins if hi <= 0)
        print(f"{name}: {len(wins)}")
        print(f"  earliest the snapshot can be (lo - T): p50 {pct(los, 50):.0f}  p99 {pct(los, 99):.0f}  "
              f"max {max(los, default=float('nan')):.0f} ms")
        print(f"  latest the snapshot can be   (hi - T): min {min(his, default=float('nan')):.0f}  "
              f"p1 {pct(his, 1):.0f}  p50 {pct(his, 50):.0f} ms")
        print(f"  provably taken AFTER T: {after} ({100 * after / len(wins):.1f}%);  "
              f"provably BEFORE T: {before} ({100 * before / len(wins):.1f}%)  [receive clock]")
        for name_l, L in lag_marks:
            n_l = sum(1 for lo, _ in wins if lo >= L)
            print(f"  still after T once a {L:.0f} ms delivery lag ({name_l}) is taken off: "
                  f"{n_l} ({100 * n_l / len(wins):.1f}%)")
        print(f"  the one offset that fits the most rows: {best[0]}..{best[-1]} ms, fitting {100 * top:.2f}%; "
              f"within 1% of that: {near[0]}..{near[-1]} ms")
        at = dict(prof)
        print("  share of rows a snapshot at T+d fits: " + "  ".join(
            f"d={d}: {100 * at[d]:.1f}%" for d in (-1000, 0, 200, 400, 600, 800, 1000, 1500, 2000, 3000)))

    print("\nwhen was the snapshot taken? (receive time, relative to the row's stamp T)")
    show("rows, one instant fits every field", win_all)
    if per_minute:
        joint = []
        for _t, ws in by_minute.items():
            if len(ws) == len({r["coin"] for r in rows}):
                cw = common_window(ws)
                joint.append(cw)
        ok = [j for j in joint if j is not None]
        print(f"\nminutes where every fine coin is judged: {len(joint)}; one instant fits ALL coins' rows: "
              f"{len(ok)} ({100 * len(ok) / max(len(joint), 1):.1f}%)")
        if ok:
            show("minutes, joint window across coins", ok)


def compare(archive_dir: Path, recording: Path, example: str | None) -> None:
    names, uni, fine, uni_times, lat, bbo = _load_recording(recording)
    if not uni_times:
        raise SystemExit("no allDexsAssetCtxs frames in the recording")
    t_lo, t_hi = uni_times[0] + WINDOW_MS, uni_times[-1] - WINDOW_MS
    rows, dex_prefixes = _load_archive(archive_dir, t_lo, t_hi)
    print(f"recording: {len(uni_times)} universe frames, {len(names)} main-dex coins, "
          f"{datetime.fromtimestamp(uni_times[0] / 1000, UTC):%Y-%m-%d %H:%M} .. "
          f"{datetime.fromtimestamp(uni_times[-1] / 1000, UTC):%Y-%m-%d %H:%M} UTC")
    gaps = [b - a for a, b in zip(uni_times, uni_times[1:], strict=False)]
    print(f"universe push interval: median {statistics.median(gaps) / 1000:.1f} s, "
          f"max {max(gaps) / 1000:.1f} s")
    for c, (ts, _) in sorted(fine.items()):
        g = [b - a for a, b in zip(ts, ts[1:], strict=False)]
        print(f"fine stream {c}: {len(ts)} frames, median interval {statistics.median(g) / 1000:.2f} s")
    if dex_prefixes:
        print(f"archive rows for other dexes (skipped): {dict(dex_prefixes)}")
    else:
        print("archive has no rows for any HIP-3 dex (no 'dex:' coins): it is main dex only")
    if lat:
        print(f"delivery lag, trades frames (receive time minus the exchange's trade time), n={len(lat)}: "
              f"p1 {pct(lat, 1)}  p50 {pct(lat, 50)}  p99 {pct(lat, 99)} ms")

    for c in sorted(fine):
        if c in bbo:
            lw = ctx_lag_windows(*fine[c], bbo[c])
            if lw:
                prof = offset_profile(lw, range(0, 2001, 50))
                top = max(s for _, s in prof)
                best = [d for d, s in prof if s == top]
                print(f"delivery lag of {c}'s activeAssetCtx frames, from {len(lw)} mid changes lined up against bbo: "
                      f"the one lag that fits the most is {best[0]}..{best[-1]} ms ({100 * top:.0f}% fit); "
                      f"lower bounds p50 {pct([a for a, _ in lw], 50):.0f}, upper bounds p50 "
                      f"{pct([b for _, b in lw], 50):.0f} ms")
    covered = [r for r in rows if _covered(uni_times, r["_t"])]
    minutes = sorted({r["_t"] for r in covered})
    print(f"archive rows inside the recording: {len(rows)}; with the feed flowing ±60 s around them: "
          f"{len(covered)} ({len(minutes)} distinct minutes, "
          f"{len({r['coin'] for r in covered})} coins)")
    off_grid = sum(1 for r in covered if r["_t"] % 60_000)
    print(f"off-grid rows among them: {off_grid}")

    ex = None
    if example:
        coin, stamp = example.split("@")
        ex = (coin, int(datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp() * 1000))
    marks = (("trades p50", pct(lat, 50)), ("trades p99", pct(lat, 99))) if lat else ()
    _analyse(covered, fine, f"FINE: {', '.join(sorted(fine))}, one frame a second", example=ex,
             per_minute=True, lag_marks=marks)
    _analyse(covered, uni, "UNIVERSE: every main-dex coin, one frame every ~15 s", lag_marks=marks, coarse=True)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("--minutes", type=float, default=60)
    r.add_argument("--out", type=Path, required=True)
    r.add_argument("--coins", default="BTC,ETH")
    c = sub.add_parser("compare")
    c.add_argument("--archive-dir", type=Path, default=Path("data"))
    c.add_argument("--recording", type=Path, required=True)
    c.add_argument("--example", help="COIN@YYYY-MM-DDTHH:MM:SSZ to print a worked example for")
    args = p.parse_args()
    if args.cmd == "record":
        record(args.minutes, args.out, args.coins.split(","))
    else:
        compare(args.archive_dir, args.recording, args.example)


if __name__ == "__main__":
    sys.exit(main())
