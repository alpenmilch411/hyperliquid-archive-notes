#!/usr/bin/env python3
"""Hyperliquid's archive never writes a literal NULL for missing data. It
writes a fabricated `0.0` instead, in the same type as a real measurement,
in every one of the six columns that use this pattern.

Goes through every day file in --archive-dir: counts literal nulls (should
be zero, in every column, on every day) and counts `mark_px == 0.0` rows,
the one most likely to reach a calculation without anyone noticing. A zero
mark reads as a fake drop to zero.

Usage: python probes/zeros_not_nulls.py [--archive-dir PATH]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hl_archive  # noqa: E402

_DEFAULT_ARCHIVE_DIR = Path("data")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--archive-dir", type=Path, default=_DEFAULT_ARCHIVE_DIR)
    args = p.parse_args()

    files = sorted(args.archive_dir.glob("*.csv.lz4"))
    if not files:
        print(f"no *.csv.lz4 files found in {args.archive_dir}")
        raise SystemExit(1)

    total_nulls = 0
    total_rows = 0
    mark_zero_by_day: dict[str, int] = {}
    t0 = time.time()
    for f in files:
        raw = hl_archive.lz4.frame.decompress(f.read_bytes())
        df = hl_archive.parse_csv(raw)  # raises ArchiveCorruptionError if it finds a literal null
        total_rows += df.height
        mz = int((df["mark_px"] == 0.0).sum())
        if mz > 0:
            mark_zero_by_day[f.name.replace(".csv.lz4", "")] = mz

    mark_zero_total = sum(mark_zero_by_day.values())
    worst_day, worst_count = max(mark_zero_by_day.items(), key=lambda kv: kv[1]) if mark_zero_by_day else (None, 0)

    print(f"checked {len(files)} day file(s), {total_rows} row(s), took {time.time() - t0:.1f}s")
    print(f"literal NULLs found across every column and day: {total_nulls} "
          f"(parse_csv would have raised an error if it found even one)")
    print(f"mark_px == 0.0 rows: {mark_zero_total} across {len(mark_zero_by_day)} day(s)")
    print(f"worst day: {worst_day} ({worst_count} rows)")


if __name__ == "__main__":
    main()
