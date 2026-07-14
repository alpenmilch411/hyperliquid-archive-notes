"""A correct reader for Hyperliquid's public historical archive.

Source: `s3://hyperliquid-archive/asset_ctxs/{YYYYMMDD}.csv.lz4`, lz4-compressed
CSV, one file per calendar day, all perp markets, one row per coin per minute
(plus the occasional extra row inside a minute, see below). Free, requester-pays
egress, no auth.

The raw file parses cleanly and gives you well-formed numbers even when it is
wrong. This module does the four things a correct read of it needs:

1. parse the CSV into a typed polars DataFrame (UTC timestamps, float columns).
2. turn Hyperliquid's fabricated-zero placeholders into real nulls, with a
   `*_missing` flag beside each one so nothing disappears without a trace.
3. label which funding-rate scale each row is on. The unit of this column
   changes partway through the archive's history, and nothing in the row
   itself tells you that.
4. refuse the corruption a naive reader would let through: a changed header,
   a day with too many rows, a negative price, a funding value out of range.

Usage:

    import hl_archive
    df = hl_archive.read_day("data/20240315.csv.lz4")

Every constant and every check below has a script in `probes/` that proves
it. This file is not the place to argue for a number, only to use one that
has already been checked.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from pathlib import Path

import lz4.frame
import polars as pl

# ---------------------------------------------------------------------------
# 1. Schema
# ---------------------------------------------------------------------------

# The exact CSV header, in order, as published by the archive today. If it
# does not match, Hyperliquid changed the file's columns. Do not guess at a
# new mapping. Refuse the file instead.
ARCHIVE_COLUMNS: tuple[str, ...] = (
    "time",
    "coin",
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

_TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_FLOAT_COLUMNS = tuple(c for c in ARCHIVE_COLUMNS if c not in ("time", "coin"))

# ---------------------------------------------------------------------------
# 2. Zero is not the same thing as missing
# ---------------------------------------------------------------------------
#
# Hyperliquid's archive writer never writes a literal NULL. When a value is
# missing, it writes a fabricated 0.0 in its place instead. Six columns do
# this. One of them, `open_interest`, can also legitimately go NEGATIVE
# around a delisting settlement, so the rule for it is "missing if <= 0.0",
# not "missing if == 0.0". If you test for `== 0.0` there, the negative
# rows slip through a "cleaning" step unflagged, and they are real data.
#
# `premium` needs its own rule too. It is NOT treated as missing just
# because its own value is 0.0. A live, healthy order book can legitimately
# push premium to exactly 0.0 when the oracle price sits inside the bid/ask
# spread, and that happens often, including for BTC on ordinary days.
# Instead, `premium` is treated as missing exactly when the order book
# itself is dead, which is the same condition that makes
# `impact_bid_px`/`impact_ask_px` missing. If you null `premium` on its own
# zero value, you delete a large amount of real, live, zero-premium data.


def _sentinel_flags(df: pl.DataFrame) -> pl.DataFrame:
    book_dead = (pl.col("impact_bid_px") == 0.0) | (pl.col("impact_ask_px") == 0.0)
    return df.with_columns(
        book_dead.alias("impact_bid_px_missing"),
        book_dead.alias("impact_ask_px_missing"),
        book_dead.alias("premium_missing"),
        (pl.col("mid_px") == 0.0).alias("mid_px_missing"),
        (pl.col("mark_px") == 0.0).alias("mark_px_missing"),
        (pl.col("open_interest") <= 0.0).alias("open_interest_missing"),
    )


_SENTINEL_COLUMNS: tuple[str, ...] = (
    "impact_bid_px",
    "impact_ask_px",
    "premium",
    "mid_px",
    "mark_px",
    "open_interest",
)


def _null_sentinels(df: pl.DataFrame) -> pl.DataFrame:
    """Runs after `_sentinel_flags` has already added the `*_missing`
    columns. Sets each sentinel column to null wherever its own flag is
    true, and keeps the flags themselves in place. After this, you can
    always tell a row that is genuinely absent from a row that was
    genuinely measured at zero."""
    _null = pl.lit(None, dtype=pl.Float64)
    return df.with_columns(
        *[
            pl.when(pl.col(f"{c}_missing")).then(_null).otherwise(pl.col(c)).alias(c)
            for c in _SENTINEL_COLUMNS
        ]
    )


# ---------------------------------------------------------------------------
# 3. The funding rate scale: two boundaries, 55 minutes apart, for two series
# ---------------------------------------------------------------------------
#
# Hyperliquid moved perp funding from an 8-hourly to an hourly settlement
# schedule on 2023-06-08. The archive's own per-minute `funding` column and
# the public API's settled `fundingHistory` series do not change scale at
# the same instant. If you join the two series and use one boundary for
# both, you will mislabel 55 minutes of rows.

ARCHIVE_FUNDING_SCALE_FLIP = datetime(2023, 6, 8, 0, 5, 0, tzinfo=UTC)
# Applies to what THIS file reads: the per-minute `funding` column in the
# archive CSV. Before this timestamp `funding` is an 8-hour rate. At and
# after it, funding is hourly. Found by comparing each row's rate against
# Hyperliquid's own funding formula (below) and looking minute by minute
# for where most rows switch from matching the 8-hour version to matching
# the hourly one: the last minute where most rows still match the 8-hour
# version is 2023-06-08T00:04:00Z, and the first minute where most rows
# match the hourly version is 2023-06-08T00:05:00Z. See
# `probes/funding_scale_flip.py`.

FUNDING_HISTORY_SCALE_FLIP = datetime(2023, 6, 8, 1, 0, 0, tzinfo=UTC)
# Applies to the settled `fundingHistory` series from the public info API.
# This file never reads that endpoint, so this constant is here for
# reference only. It is 55 minutes LATER than ARCHIVE_FUNDING_SCALE_FLIP,
# found the same way on the settled series: the last row before the switch
# is at 2023-06-08T00:00:00.254Z, and the first row after it is at
# 2023-06-08T01:00:00.054Z. See `probes/funding_scale_flip.py`.

# Hyperliquid's own funding formula. This is the reason both flips above
# exist, and the reason for CLAMP_BOUND_CHANGE below. It matches the
# published rate almost exactly on the rows where it applies:
#
#     F_8h = premium + clip(CLAMP_INTEREST - premium, -C, +C)
#     archive `funding` equals F_8h in the 8-hourly era, and F_8h / 8 in the hourly era
#
# C is not one fixed number. See CLAMP_BOUND_EARLY and CLAMP_BOUND_LATE below.
CLAMP_INTEREST = 1e-4
CLAMP_EXACT = 1e-15  # how close a computed value has to be to count as a match

CLAMP_BOUND_EARLY = 3e-4  # C before CLAMP_BOUND_CHANGE
CLAMP_BOUND_LATE = 5e-4  # C at and after CLAMP_BOUND_CHANGE
CLAMP_BOUND_CHANGE = datetime(2023, 12, 11, 23, 0, 0, tzinfo=UTC)
# Hyperliquid widened the clamp from 0.0003 to 0.0005 at this moment. There
# is no changelog entry for it anywhere we could find. Found the same way
# as the scale flip above, but only using rows where the clamp is actually
# binding (rows where the two candidate values would predict numbers far
# enough apart that a match cannot be luck): the last minute where most of
# those rows still match 0.0003 is 2023-12-11T22:00:00Z, and the first
# minute where most match 0.0005 is 2023-12-11T23:00:00Z. The split is not
# perfect. A small number of rows across the whole archive happen to match
# the wrong era's number by coincidence, so this file never builds a check
# that fails on a single disagreeing row. See `probes/clamp_bound_change.py`.

# The most funding can move in an hour, 4%, expressed in the row's own
# unit. The archive's `funding` column is an 8-hour rate before
# ARCHIVE_FUNDING_SCALE_FLIP, so the same 4%-per-hour limit is 8 times
# bigger there. Using one flat number across the boundary makes the check
# either 8 times too loose on the early archive, or 8 times too tight if
# Hyperliquid ever lowers the limit.
FUNDING_ABS_CAP_HOURLY = 0.04


def _label_funding_era(df: pl.DataFrame) -> pl.DataFrame:
    """Adds `funding_is_8h`, decided from `time` alone, never from the
    value, against the constant above. Deciding it from the value would be
    a guess. The whole point of storing this column is that whoever reads
    it later should not have to guess which scale a row is on."""
    return df.with_columns((pl.col("time") < ARCHIVE_FUNDING_SCALE_FLIP).alias("funding_is_8h"))


# ---------------------------------------------------------------------------
# 4. Corruption checks: refuse what a naive reader would let through
# ---------------------------------------------------------------------------


class ArchiveSchemaError(RuntimeError):
    """The CSV header does not match what this reader expects.
    Hyperliquid changed the archive's columns. Do not guess at a new
    mapping."""


class ArchiveCorruptionError(RuntimeError):
    """A structural or range check on the archive failed. This means the
    file is broken or was cut short in transport, not that the market did
    something unusual. Every unusual-but-real thing this project knows
    about is handled elsewhere in this file on purpose, and does not raise
    here."""


def parse_csv(csv_bytes: bytes) -> pl.DataFrame:
    """Parses one day's raw, already-decompressed CSV bytes into a typed
    frame. Checks the header exactly and sets the right dtypes. Does not
    yet check ranges, check coverage, or null out any placeholder value."""
    df = pl.read_csv(io.BytesIO(csv_bytes), try_parse_dates=False)

    if tuple(df.columns) != ARCHIVE_COLUMNS:
        raise ArchiveSchemaError(
            f"archive header changed.\n  expected: {ARCHIVE_COLUMNS}\n  got:      {tuple(df.columns)}"
        )

    df = df.with_columns(
        pl.col("time").str.to_datetime(format=_TIME_FORMAT, time_zone="UTC"),
        *[pl.col(c).cast(pl.Float64) for c in _FLOAT_COLUMNS],
    )

    # Hyperliquid never writes a literal NULL anywhere in this archive.
    # A missing value is always the fabricated-zero placeholder that
    # `_sentinel_flags`/`_null_sentinels` handle below, and those only run
    # after this check. A literal null reaching this point, from an empty
    # CSV cell or a cut-off download, has no honest reading. Treat it as a
    # broken file, not as an unusual market.
    null_counts = df.null_count()
    total_nulls = int(sum(null_counts.row(0)))
    if total_nulls > 0:
        bad = {c: int(null_counts[c][0]) for c in df.columns if null_counts[c][0] > 0}
        raise ArchiveCorruptionError(
            f"found NULL value(s) where Hyperliquid never writes one: {bad}. "
            f"This is not the archive's usual zero-for-missing pattern. Treat this "
            f"file as broken or cut short, not as unusual market data."
        )

    return df


def _check_coverage(df: pl.DataFrame, expect_minutes: int) -> None:
    """Coverage is counted in distinct minutes per coin, never in rows.

    A coin can carry an extra row inside a single minute on an ordinary
    day. That is not a replacement for the row on the minute mark, it is
    an extra sample next to it, so a coin can end up with 1,445 rows
    across only 1,440 distinct minutes. The check most people reach for
    first, `rows == coins * expect_minutes`, is wrong on roughly a third
    of all archive days for exactly this reason. See
    `probes/partial_first_day.py`.

    What actually points to a broken file is a coin covering MORE distinct
    minutes than a day can hold. That can happen if two days end up
    concatenated into one file, or if the same row was written twice with
    a different exact timestamp. Covering fewer minutes than a full day is
    normal (a partial first or last day, or a coin listed or delisted
    partway through the day) and is not treated as an error here.
    """
    dup = df.group_by(["time", "coin"]).len().filter(pl.col("len") > 1)
    if dup.height > 0:
        raise ArchiveCorruptionError(
            f"{dup.height} duplicate (time, coin) row(s): the exact same timestamp "
            f"repeated for the same coin. That is not an extra sample inside a minute, "
            f"which would carry a different timestamp. This is a straight duplicate."
        )

    per_coin = df.group_by("coin").agg(pl.col("time").dt.truncate("1m").n_unique().alias("minutes"))
    over = per_coin.filter(pl.col("minutes") > expect_minutes)
    if over.height > 0:
        pairs = ", ".join(f"{c}={m}" for c, m in over.sort("coin").iter_rows())
        raise ArchiveCorruptionError(
            f"coin(s) covering more than {expect_minutes} distinct minutes in one "
            f"day file (looks like two days were concatenated, or rows were written "
            f"twice): {pairs}"
        )


def _check_ranges(df: pl.DataFrame) -> None:
    """Range checks that ask more than a plain `> 0` would: the right cap
    for `funding` given which scale the row is on, the right sign for a
    zero-for-missing column, and the right side of zero for a value that
    should never legitimately be negative."""

    # oracle_px and prev_day_px: measured strictly positive on every real
    # archive row seen so far. Neither one is ever written as a
    # zero-for-missing placeholder, so a value that is not positive here
    # means the file did not parse correctly, not that the market did
    # something unusual.
    for col in ("oracle_px", "prev_day_px"):
        bad = int((df[col] <= 0).sum())
        if bad > 0:
            raise ArchiveCorruptionError(
                f"non-positive {col}: {bad} row(s). This looks like a parse error, not real data."
            )

    # day_ntl_vlm: daily notional volume can be zero on an inactive book,
    # but it can never be negative.
    bad_vol = int((df["day_ntl_vlm"] < 0).sum())
    if bad_vol > 0:
        raise ArchiveCorruptionError(f"negative day_ntl_vlm: {bad_vol} row(s)")

    # Price columns that use zero as the missing-value placeholder: zero
    # is fine, it means missing, but negative is not a state these columns
    # are supposed to have. A negative value here is a broken file, plain
    # and simple, and it is a different problem from the open_interest
    # case below.
    for col in ("mid_px", "impact_bid_px", "impact_ask_px", "mark_px"):
        bad = int((df[col] < 0).sum())
        if bad > 0:
            raise ArchiveCorruptionError(f"negative {col}: {bad} row(s). Zero means missing here, negative does not.")

    # funding: the cap has to be checked in the row's own unit. Using one
    # flat number across ARCHIVE_FUNDING_SCALE_FLIP would make the check
    # 8 times too loose before the boundary and 8 times too tight after
    # it, if the limit were ever lowered.
    pre_flip = pl.col("time") < ARCHIVE_FUNDING_SCALE_FLIP
    over_cap = df.filter(
        pl.when(pre_flip)
        .then(pl.col("funding").abs() > FUNDING_ABS_CAP_HOURLY * 8)
        .otherwise(pl.col("funding").abs() > FUNDING_ABS_CAP_HOURLY)
    )
    if over_cap.height > 0:
        worst = over_cap["funding"].abs().max()
        raise ArchiveCorruptionError(
            f"funding out of range on {over_cap.height} row(s): |{worst}| is past "
            f"Hyperliquid's 4%-per-hour limit, checked in that row's own unit"
        )

    # open_interest is deliberately not checked for sign here. A negative
    # value is real, bounded data tied to a delisting settlement. This
    # module turns it into a null and flags it (`open_interest_missing`,
    # using `<= 0.0`). It does not reject it. See
    # `probes/negative_open_interest.py`.


def validate_and_clean(df: pl.DataFrame, *, expect_minutes: int = 1440) -> pl.DataFrame:
    """Runs the corruption checks, then turns placeholder values into
    nulls, then adds the funding-scale label. The order matters: every
    check below runs on the raw values, because the zero-for-missing
    checks in `_sentinel_flags` need to see the original numbers, not
    already-nulled ones.

    `expect_minutes` is how many minutes a single coin can legitimately
    cover in one day file. 1440 for an ordinary day. Only pass a smaller
    number if you already know you are checking a file that covers less
    than a full day on purpose.
    """
    _check_coverage(df, expect_minutes)
    _check_ranges(df)
    df = _sentinel_flags(df)
    df = _null_sentinels(df)
    df = _label_funding_era(df)
    return df


def read_day(path: str | Path, *, expect_minutes: int = 1440) -> pl.DataFrame:
    """Reads one archive day file start to finish: decompress, parse,
    check, clean. `path` is a `{YYYYMMDD}.csv.lz4` file as published by
    the archive, or downloaded with `aws s3 cp --request-payer requester`."""
    raw = lz4.frame.decompress(Path(path).read_bytes())
    df = parse_csv(raw)
    return validate_and_clean(df, expect_minutes=expect_minutes)


def _main() -> None:
    import sys

    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <path-to-YYYYMMDD.csv.lz4>", file=sys.stderr)
        raise SystemExit(2)

    raw = lz4.frame.decompress(Path(sys.argv[1]).read_bytes())
    raw_df = parse_csv(raw)
    # Negative open interest can only be seen here, on the raw frame. By
    # the time validate_and_clean has run, it has already been turned into
    # a null (correctly), and the sign is gone from the cleaned column.
    n_neg_oi = int((raw_df["open_interest"] < 0).sum())

    df = validate_and_clean(raw_df)
    n_coins = df["coin"].n_unique()
    era = "8-hourly" if bool(df["funding_is_8h"][0]) else "hourly"
    print(f"rows:              {df.height}")
    print(f"coins:             {n_coins}")
    print(f"time range:        {df['time'].min()} .. {df['time'].max()}")
    print(f"funding scale:     {era} (funding_is_8h={bool(df['funding_is_8h'][0])})")
    for col in ("mid_px", "impact_bid_px", "impact_ask_px", "premium", "mark_px", "open_interest"):
        flag = f"{col}_missing"
        n_missing = int(df[flag].sum())
        print(f"{col + ' missing:':<19}{n_missing} row(s) ({100 * n_missing / df.height:.2f}%)")
    print(f"negative open interest rows: {n_neg_oi} (turned into a null and flagged, not dropped)")


if __name__ == "__main__":
    _main()
