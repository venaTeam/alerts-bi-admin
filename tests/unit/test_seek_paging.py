"""Untrusted navigation hints never change run/filter scope or become SQL syntax."""

import base64
import json

from alerts_bi_shared.db.paging import cursor_for, seek


def test_forward_and_reverse_cursors_bind_values_and_scope() -> None:
    columns = (("row_count", True), ("application", False), ("key_field", False))
    row = {"row_count": 42, "application": "app'; DROP TABLE runs;--", "key_field": "clé"}
    token = cursor_for("run-filter-a", row, columns)
    clause, params, reverse = seek(token, "run-filter-a", columns)
    assert "row_count<:seek0" in clause and "application>:seek1" in clause
    assert str(row["application"]) not in clause and params["seek1"] == row["application"]
    assert not reverse
    assert seek(token, "different-run-filter", columns) == ("", {}, False)
    clause, _, reverse = seek(
        cursor_for("run-filter-a", row, columns, previous=True), "run-filter-a", columns
    )
    assert reverse and "row_count>:seek0" in clause and "application<:seek1" in clause


def test_malformed_or_out_of_range_values_fall_back_without_query_parameters() -> None:
    columns = (("row_count", True),)
    for values in (["not a number"], [2**100], [True], [None], []):
        token = base64.urlsafe_b64encode(json.dumps(["scope", False, values]).encode()).decode()
        assert seek(token, "scope", columns) == ("", {}, False)
    assert seek("invalid base64!", "scope", columns) == ("", {}, False)
