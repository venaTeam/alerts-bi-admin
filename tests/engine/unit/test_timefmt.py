from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from alerts_bi_shared.hashing import compact_json, sha256_of
from alerts_bi_shared.timefmt import iso_date, iso_instant


def test_milliseconds_are_always_written_even_at_a_whole_second() -> None:
    assert iso_instant(datetime(2026, 8, 25, 18, 0, 0, tzinfo=UTC)) == "2026-08-25T18:00:00.000Z"


def test_sub_second_precision_is_kept_to_three_digits() -> None:
    assert (
        iso_instant(datetime(2026, 8, 25, 18, 0, 0, 123000, tzinfo=UTC))
        == "2026-08-25T18:00:00.123Z"
    )


def test_microseconds_are_truncated_rather_than_rounded_up() -> None:
    assert (
        iso_instant(datetime(2026, 8, 25, 18, 0, 0, 123999, tzinfo=UTC))
        == "2026-08-25T18:00:00.123Z"
    )


def test_a_naive_value_is_read_as_utc_not_as_local_time() -> None:
    """The SQL driver hands back naive values for DATETIME2 columns."""
    assert iso_instant(datetime(2026, 8, 25, 18, 0, 0)) == "2026-08-25T18:00:00.000Z"


def test_an_offset_instant_is_converted_to_utc() -> None:
    tehran = timezone(timedelta(hours=3, minutes=30))
    assert (
        iso_instant(datetime(2026, 8, 25, 21, 30, 0, tzinfo=tehran)) == "2026-08-25T18:00:00.000Z"
    )


def test_the_date_helper_reports_the_utc_calendar_date_not_the_local_one() -> None:
    tehran = timezone(timedelta(hours=3, minutes=30))
    assert iso_date(datetime(2026, 8, 26, 2, 0, 0, tzinfo=tehran)) == "2026-08-25"


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        # Pinned against the superseded JavaScript build's Date#toISOString output, so a
        # run keeps its identity across the port.
        (datetime(2026, 8, 25, 18, 0, 0, tzinfo=UTC), "2026-08-25T18:00:00.000Z"),
        (datetime(2026, 8, 18, 18, 0, 0, tzinfo=UTC), "2026-08-18T18:00:00.000Z"),
        (datetime(2026, 8, 20, 12, 0, 0, tzinfo=UTC), "2026-08-20T12:00:00.000Z"),
        (datetime(1970, 1, 1, 0, 0, 0, tzinfo=UTC), "1970-01-01T00:00:00.000Z"),
    ],
)
def test_matches_the_javascript_reference_encodings(moment: datetime, expected: str) -> None:
    assert iso_instant(moment) == expected


def test_the_run_id_digest_is_unchanged_by_the_shared_formatter() -> None:
    """Pinned from the JavaScript build, which hashed the same document."""
    digest = sha256_of(
        {
            "team_id": "checkout-api",
            "run_at": iso_instant(datetime(2026, 8, 25, 18, 0, 0, tzinfo=UTC)),
            "window_start": iso_instant(datetime(2026, 8, 18, 18, 0, 0, tzinfo=UTC)),
        }
    )
    assert digest == sha256_of(
        {
            "team_id": "checkout-api",
            "run_at": "2026-08-25T18:00:00.000Z",
            "window_start": "2026-08-18T18:00:00.000Z",
        }
    )


# ------------------------------------------------------------------- compact_json


def test_stored_json_carries_no_incidental_whitespace() -> None:
    assert compact_json({"a": 1, "b": [1, 2]}) == '{"a":1,"b":[1,2]}'


def test_stored_json_keeps_insertion_order_rather_than_sorting() -> None:
    """These columns are read by people; hashed values sort instead."""
    assert compact_json({"b": 1, "a": 2}) == '{"b":1,"a":2}'


def test_non_ascii_content_is_stored_as_itself_not_escaped() -> None:
    assert compact_json({"message": "café"}) == '{"message":"café"}'


def test_a_value_the_encoder_does_not_know_falls_back_to_its_string_form() -> None:
    assert compact_json({"at": datetime(2026, 8, 25, 18, 0, 0)}) == ('{"at":"2026-08-25 18:00:00"}')
