"""Tests for hl_archive.py: turning placeholder values into nulls
(including negative open interest), the funding-scale label on both sides
of the boundary, and that the corruption checks actually raise on the
inputs they claim to catch."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hl_archive

_HEADER = ",".join(hl_archive.ARCHIVE_COLUMNS)

# A single, healthy, fully-live row. Tests override individual fields by
# copying this dict and mutating the fields they care about.
_LIVE_ROW = {
    "time": "2024-06-15T12:00:00Z",
    "coin": "BTC",
    "funding": "0.0000125",
    "open_interest": "1000.0",
    "prev_day_px": "60000.0",
    "day_ntl_vlm": "500000000.0",
    "premium": "0.00005",
    "oracle_px": "60100.0",
    "mark_px": "60105.0",
    "mid_px": "60104.5",
    "impact_bid_px": "60100.0",
    "impact_ask_px": "60110.0",
}


def _row_csv(row: dict) -> str:
    return ",".join(row[c] for c in hl_archive.ARCHIVE_COLUMNS)


def _csv(rows: list[dict]) -> bytes:
    return ("\n".join([_HEADER, *[_row_csv(r) for r in rows]]) + "\n").encode()


def _row(**overrides) -> dict:
    row = dict(_LIVE_ROW)
    row.update(overrides)
    return row


# --------------------------------------------------------------------------
# parse_csv: schema and literal-null corruption
# --------------------------------------------------------------------------


def test_parse_csv_reads_a_healthy_row():
    df = hl_archive.parse_csv(_csv([_row()]))
    assert df.height == 1
    assert df["coin"][0] == "BTC"
    assert df["mark_px"][0] == pytest.approx(60105.0)


def test_parse_csv_rejects_changed_header():
    bad = b"time,coin,funding\n2024-06-15T12:00:00Z,BTC,0.0000125\n"
    with pytest.raises(hl_archive.ArchiveSchemaError):
        hl_archive.parse_csv(bad)


def test_parse_csv_rejects_literal_null_field():
    # An empty CSV cell parses to a literal null. The archive is
    # documented to never do this. A reader that let it through would
    # mishandle it later, once it reaches the zero-for-missing checks.
    header = _HEADER
    good_row = _row_csv(_row())
    bad_row = good_row.rsplit(",", 1)[0] + ","  # blank out impact_ask_px
    raw = (header + "\n" + good_row + "\n" + bad_row + "\n").encode()
    with pytest.raises(hl_archive.ArchiveCorruptionError):
        hl_archive.parse_csv(raw)


# --------------------------------------------------------------------------
# sentinel nulling
# --------------------------------------------------------------------------


def test_dead_book_nulls_impact_and_premium_not_mid_or_mark():
    df = hl_archive.parse_csv(_csv([_row(impact_bid_px="0.0", impact_ask_px="0.0")]))
    clean = hl_archive.validate_and_clean(df)
    assert clean["impact_bid_px_missing"][0] is True
    assert clean["impact_ask_px_missing"][0] is True
    assert clean["premium_missing"][0] is True
    assert clean["impact_bid_px"][0] is None
    assert clean["premium"][0] is None
    # mid_px and mark_px are checked on their own. A dead impact book does
    # not, by itself, turn either of them into a null.
    assert clean["mid_px_missing"][0] is False
    assert clean["mark_px_missing"][0] is False


def test_live_book_with_zero_premium_is_not_nulled():
    # The reason this test exists: premium can legitimately land on exactly
    # 0.0 on a live, healthy book. Turning it into a null just because its
    # own value is zero would silently delete real data.
    df = hl_archive.parse_csv(_csv([_row(premium="0.0")]))
    clean = hl_archive.validate_and_clean(df)
    assert clean["premium_missing"][0] is False
    assert clean["premium"][0] == pytest.approx(0.0)


def test_mid_px_and_mark_px_zero_are_nulled_independently():
    df = hl_archive.parse_csv(_csv([_row(mid_px="0.0", mark_px="0.0")]))
    clean = hl_archive.validate_and_clean(df)
    assert clean["mid_px_missing"][0] is True
    assert clean["mid_px"][0] is None
    assert clean["mark_px_missing"][0] is True
    assert clean["mark_px"][0] is None
    # the book itself still counts as live, since the impact quotes are live
    assert clean["impact_bid_px_missing"][0] is False


def test_negative_open_interest_is_nulled_and_flagged_not_dropped():
    # The rule for open_interest is "missing if <= 0.0", not "missing if
    # == 0.0". A negative value is real, bounded data tied to a delisting
    # settlement, and it has to come through as a flagged null, never
    # silently rejected and never passed through unflagged.
    df = hl_archive.parse_csv(_csv([_row(open_interest="-114047.0")]))
    clean = hl_archive.validate_and_clean(df)
    assert clean.height == 1
    assert clean["open_interest_missing"][0] is True
    assert clean["open_interest"][0] is None


def test_positive_open_interest_is_not_nulled():
    df = hl_archive.parse_csv(_csv([_row(open_interest="42.0")]))
    clean = hl_archive.validate_and_clean(df)
    assert clean["open_interest_missing"][0] is False
    assert clean["open_interest"][0] == pytest.approx(42.0)


def test_zero_open_interest_is_nulled():
    df = hl_archive.parse_csv(_csv([_row(open_interest="0.0")]))
    clean = hl_archive.validate_and_clean(df)
    assert clean["open_interest_missing"][0] is True


def test_missing_booleans_are_never_null():
    df = hl_archive.parse_csv(
        _csv(
            [
                _row(time="2024-06-15T12:00:00Z"),
                _row(time="2024-06-15T12:01:00Z", open_interest="-5.0"),
                _row(time="2024-06-15T12:02:00Z", mid_px="0.0", impact_bid_px="0.0", impact_ask_px="0.0"),
            ]
        )
    )
    clean = hl_archive.validate_and_clean(df)
    for col in (
        "mid_px_missing",
        "impact_bid_px_missing",
        "impact_ask_px_missing",
        "premium_missing",
        "mark_px_missing",
        "open_interest_missing",
        "funding_is_8h",
    ):
        assert clean[col].null_count() == 0


# --------------------------------------------------------------------------
# funding-era labelling on both sides of the boundary
# --------------------------------------------------------------------------


def test_funding_era_label_before_boundary():
    before = hl_archive.ARCHIVE_FUNDING_SCALE_FLIP - timedelta(minutes=1)
    df = hl_archive.parse_csv(_csv([_row(time=before.strftime("%Y-%m-%dT%H:%M:%SZ"))]))
    clean = hl_archive.validate_and_clean(df)
    assert clean["funding_is_8h"][0] is True


def test_funding_era_label_at_and_after_boundary():
    at = hl_archive.ARCHIVE_FUNDING_SCALE_FLIP
    after = hl_archive.ARCHIVE_FUNDING_SCALE_FLIP + timedelta(minutes=1)
    df = hl_archive.parse_csv(
        _csv(
            [
                _row(time=at.strftime("%Y-%m-%dT%H:%M:%SZ")),
                _row(time=after.strftime("%Y-%m-%dT%H:%M:%SZ")),
            ]
        )
    )
    clean = hl_archive.validate_and_clean(df)
    assert clean["funding_is_8h"][0] is False
    assert clean["funding_is_8h"][1] is False


def test_funding_cap_uses_the_rows_own_unit():
    # The limit is 4% per hour for hourly rows, and 32% per 8 hours (4%
    # times 8) before the scale switch. The same raw value has to be
    # accepted before the switch and rejected after it.
    before = hl_archive.ARCHIVE_FUNDING_SCALE_FLIP - timedelta(days=1)
    borderline_8h_funding = str(hl_archive.FUNDING_ABS_CAP_HOURLY * 8 * 0.5)  # comfortably under the 8-hour cap
    df = hl_archive.parse_csv(
        _csv([_row(time=before.strftime("%Y-%m-%dT%H:%M:%SZ"), funding=borderline_8h_funding)])
    )
    hl_archive.validate_and_clean(df)  # must not raise

    after = hl_archive.ARCHIVE_FUNDING_SCALE_FLIP + timedelta(days=1)
    df2 = hl_archive.parse_csv(
        _csv([_row(time=after.strftime("%Y-%m-%dT%H:%M:%SZ"), funding=borderline_8h_funding)])
    )
    with pytest.raises(hl_archive.ArchiveCorruptionError):
        hl_archive.validate_and_clean(df2)  # the same size is now far over the hourly cap


# --------------------------------------------------------------------------
# corruption checks raise
# --------------------------------------------------------------------------


def test_duplicate_time_coin_key_raises():
    df = hl_archive.parse_csv(_csv([_row(), _row()]))  # identical (time, coin) twice
    with pytest.raises(hl_archive.ArchiveCorruptionError):
        hl_archive.validate_and_clean(df)


def test_off_grid_extra_row_in_one_minute_does_not_raise():
    # A check written as `rows == coins * minutes` would wrongly reject
    # this. A genuine extra row inside the same minute, at a different
    # second and not a duplicate timestamp, is normal archive data.
    t0 = datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)
    t1 = t0 + timedelta(seconds=33)
    df = hl_archive.parse_csv(
        _csv([_row(time=t0.strftime("%Y-%m-%dT%H:%M:%SZ")), _row(time=t1.strftime("%Y-%m-%dT%H:%M:%SZ"))])
    )
    clean = hl_archive.validate_and_clean(df, expect_minutes=1440)
    assert clean.height == 2


def test_coin_covering_more_than_expect_minutes_raises():
    rows = []
    t = datetime(2024, 6, 15, 0, 0, 0, tzinfo=UTC)
    for _ in range(5):
        rows.append(_row(time=t.strftime("%Y-%m-%dT%H:%M:%SZ")))
        t += timedelta(minutes=1)
    df = hl_archive.parse_csv(_csv(rows))
    with pytest.raises(hl_archive.ArchiveCorruptionError):
        hl_archive.validate_and_clean(df, expect_minutes=3)  # 5 distinct minutes > 3 allowed


def test_non_positive_oracle_px_raises():
    df = hl_archive.parse_csv(_csv([_row(oracle_px="0.0")]))
    with pytest.raises(hl_archive.ArchiveCorruptionError):
        hl_archive.validate_and_clean(df)


def test_negative_mark_px_raises():
    # For a price column that uses zero to mean missing, negative is
    # corruption. Zero is the only value that legitimately means missing.
    df = hl_archive.parse_csv(_csv([_row(mark_px="-1.0")]))
    with pytest.raises(hl_archive.ArchiveCorruptionError):
        hl_archive.validate_and_clean(df)


def test_negative_day_ntl_vlm_raises():
    df = hl_archive.parse_csv(_csv([_row(day_ntl_vlm="-1.0")]))
    with pytest.raises(hl_archive.ArchiveCorruptionError):
        hl_archive.validate_and_clean(df)


# --------------------------------------------------------------------------
# end to end: read_day over a real lz4-compressed file
# --------------------------------------------------------------------------


def test_read_day_end_to_end(tmp_path):
    import lz4.frame

    raw = _csv([_row(), _row(coin="ETH", open_interest="-1.0")])
    path = tmp_path / "20240615.csv.lz4"
    path.write_bytes(lz4.frame.compress(raw))

    df = hl_archive.read_day(path)
    assert df.height == 2
    assert set(df["coin"].to_list()) == {"BTC", "ETH"}
    eth_row = df.filter(df["coin"] == "ETH")
    assert eth_row["open_interest_missing"][0] is True
