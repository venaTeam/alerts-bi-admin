from __future__ import annotations

import pytest
from src.engine.domain.normalize import (
    group_by_identity,
    identity_of,
    normalize_row,
    select_representative,
    split_identity,
)

from tests.engine.helpers.rows import v1_row, v2_row


def test_identity_is_application_plus_key_field() -> None:
    row = v1_row(application="pay", key_field="k1")
    assert row.identity == identity_of("pay", "k1")


def test_identity_encoding_cannot_collide_across_a_shifted_boundary() -> None:
    assert identity_of("ab", "c") != identity_of("a", "bc")
    assert identity_of("a|b", "c") != identity_of("a", "b|c")


@pytest.mark.parametrize(
    ("application", "key_field"),
    [("pay", "k1"), ("a|b", "c:d"), ("", ""), ("app:with:colons", "12:34")],
)
def test_identity_round_trips_back_to_its_parts(application: str, key_field: str) -> None:
    assert split_identity(identity_of(application, key_field)) == (application, key_field)


def test_v1_maps_object_onto_component_and_carries_no_v2_only_fields() -> None:
    row = v1_row(object="settlement-queue")
    assert row.schema == "v1"
    assert row.component == "settlement-queue"
    assert row.status is None
    assert row.impact is None
    assert row.environment is None


def test_v2_maps_component_and_keeps_its_own_fields() -> None:
    row = v2_row(component="edge-handler", environment="integration")
    assert row.schema == "v2"
    assert row.component == "edge-handler"
    assert row.status == "firing"
    assert row.environment == "integration"


def test_impact_and_runbook_url_keep_their_raw_type_so_r8_r9_can_see_a_non_string() -> None:
    row = v2_row(impact=42, runbook_url={"nested": True})
    assert row.impact == 42
    assert row.runbook_url == {"nested": True}


def test_the_complete_source_document_is_retained() -> None:
    row = v2_row(site="dc-1")
    assert row.source["site"] == "dc-1"
    assert row.source["@timestamp"] == "2026-08-20T12:00:00.000Z"


def test_doc_hash_is_stable_across_key_order_and_changes_with_content() -> None:
    a = normalize_row(
        "v1",
        {
            "application": "x",
            "key_field": "k",
            "@timestamp": "2026-08-20T00:00:00Z",
            "message": "m",
        },
    )
    b = normalize_row(
        "v1",
        {
            "message": "m",
            "@timestamp": "2026-08-20T00:00:00Z",
            "key_field": "k",
            "application": "x",
        },
    )
    c = normalize_row(
        "v1",
        {
            "application": "x",
            "key_field": "k",
            "@timestamp": "2026-08-20T00:00:00Z",
            "message": "n",
        },
    )
    assert a.doc_hash == b.doc_hash
    assert a.doc_hash != c.doc_hash


def test_snapshot_date_is_the_utc_date_of_the_timestamp() -> None:
    assert v1_row(**{"@timestamp": "2026-08-20T23:59:59.999Z"}).snapshot_date == "2026-08-20"
    assert v1_row(**{"@timestamp": "2026-08-21T00:00:00.000Z"}).snapshot_date == "2026-08-21"


@pytest.mark.parametrize("value", ["nope", None, 12345])
def test_an_unusable_timestamp_is_rejected(value: object) -> None:
    with pytest.raises(TypeError, match="unusable @timestamp"):
        normalize_row("v1", {"application": "a", "key_field": "k", "@timestamp": value})


def test_the_representative_is_the_most_recent_row_in_the_window() -> None:
    rows = [
        v1_row(**{"@timestamp": "2026-08-20T10:00:00Z", "message": "older"}),
        v1_row(**{"@timestamp": "2026-08-20T12:00:00Z", "message": "newest"}),
        v1_row(**{"@timestamp": "2026-08-20T11:00:00Z", "message": "middle"}),
    ]
    assert select_representative(rows).message == "newest"


def test_representative_selection_is_deterministic_when_timestamps_tie() -> None:
    a = v1_row(**{"@timestamp": "2026-08-20T12:00:00Z", "message": "variant-a"})
    b = v1_row(**{"@timestamp": "2026-08-20T12:00:00Z", "message": "variant-b"})
    assert select_representative([a, b]).doc_hash == select_representative([b, a]).doc_hash


def test_selecting_from_zero_rows_fails_loudly() -> None:
    with pytest.raises(ValueError, match="zero rows"):
        select_representative([])


def test_group_by_identity_separates_identities_and_keeps_input_order() -> None:
    rows = [
        v1_row(key_field="k1", message="first"),
        v1_row(key_field="k2"),
        v1_row(key_field="k1", message="second"),
    ]
    groups = group_by_identity(rows)
    assert len(groups) == 2
    assert [r.message for r in groups[identity_of("app-1", "k1")]] == ["first", "second"]


def test_the_same_key_field_under_two_applications_is_two_identities() -> None:
    groups = group_by_identity(
        [
            v1_row(application="app-a", key_field="shared"),
            v1_row(application="app-b", key_field="shared"),
        ]
    )
    assert len(groups) == 2
