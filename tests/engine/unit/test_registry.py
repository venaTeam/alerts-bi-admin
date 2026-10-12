"""Registry validation.

Ported from the JavaScript suite at the ``javascript-mvp`` tag. Every boundary is asserted
in both directions, because the registry is the one input that can silently corrupt every
number for a team.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest
from alerts_bi_operations.registry import (
    DEFAULT_REGISTRY_PATH,
    RegistryError,
    load_registry,
    select_team,
    snapshot_team_entry,
    validate_registry,
)


def base() -> dict[str, Any]:
    """A minimal valid registry document."""
    return {
        "registry_version": "1.0.0",
        "teams": [
            {
                "team_id": "team-a",
                "display_name": "Team A",
                "v1_operators": ["op-a"],
                "v2_operator": None,
            }
        ],
    }


def test_accepts_a_minimal_valid_registry() -> None:
    teams = validate_registry(base())
    assert len(teams) == 1
    assert teams[0].team_id == "team-a"


def test_rejects_a_document_that_is_not_schema_valid() -> None:
    doc = base()
    del doc["teams"][0]["display_name"]
    with pytest.raises(RegistryError):
        validate_registry(doc)


def test_rejects_unknown_properties_so_a_typo_is_never_silently_ignored() -> None:
    doc = base()
    doc["teams"][0]["v1_operator"] = ["typo-singular"]
    with pytest.raises(RegistryError, match="additionalProperties|not allowed|Additional"):
        validate_registry(doc)


def test_requires_at_least_one_source_operator() -> None:
    doc = base()
    doc["teams"][0]["v1_operators"] = []
    doc["teams"][0]["v2_operator"] = None
    with pytest.raises(RegistryError, match="no source operator"):
        validate_registry(doc)


def test_allows_an_empty_v1_list_when_a_v2_operator_exists() -> None:
    doc = base()
    doc["teams"][0]["v1_operators"] = []
    doc["teams"][0]["v2_operator"] = "team-a"
    validate_registry(doc)


def test_allows_a_null_v2_operator_when_v1_operators_exist() -> None:
    validate_registry(base())


def test_rejects_the_same_operator_assigned_to_two_teams() -> None:
    doc = base()
    doc["teams"].append(
        {
            "team_id": "team-b",
            "display_name": "Team B",
            "v1_operators": ["op-a"],
            "v2_operator": None,
        }
    )
    with pytest.raises(RegistryError, match="claimed by both"):
        validate_registry(doc)


def test_operator_uniqueness_is_case_sensitive() -> None:
    doc = base()
    doc["teams"].append(
        {
            "team_id": "team-b",
            "display_name": "Team B",
            "v1_operators": ["OP-A"],
            "v2_operator": None,
        }
    )
    validate_registry(doc)


def test_one_team_may_carry_the_same_value_as_v1_and_v2_operator() -> None:
    doc = base()
    doc["teams"][0]["v2_operator"] = "op-a"
    validate_registry(doc)


def test_rejects_a_v2_operator_colliding_with_another_team_v1_operator() -> None:
    doc = base()
    doc["teams"].append(
        {
            "team_id": "team-b",
            "display_name": "Team B",
            "v1_operators": [],
            "v2_operator": "op-a",
        }
    )
    with pytest.raises(RegistryError, match="claimed by both"):
        validate_registry(doc)


def test_rejects_duplicate_team_ids() -> None:
    doc = base()
    doc["teams"].append(
        {
            "team_id": "team-a",
            "display_name": "Team A again",
            "v1_operators": ["op-z"],
            "v2_operator": None,
        }
    )
    with pytest.raises(RegistryError, match="duplicate team_id"):
        validate_registry(doc)


def test_rejects_duplicate_operator_values_inside_one_v1_list() -> None:
    doc = base()
    doc["teams"][0]["v1_operators"] = ["op-a", "op-a"]
    with pytest.raises(RegistryError):
        validate_registry(doc)


def test_rejects_a_constant_variable_with_no_value() -> None:
    doc = base()
    doc["teams"][0]["panels"] = [
        {
            "panel_id": "p1",
            "schema": "v1",
            "sql": "SELECT * FROM t WHERE operator = $op",
            "variables": [{"name": "op", "type": "constant"}],
        }
    ]
    with pytest.raises(RegistryError, match='is constant but has no "value"'):
        validate_registry(doc)


def test_rejects_a_custom_variable_with_no_values_list() -> None:
    doc = base()
    doc["teams"][0]["panels"] = [
        {
            "panel_id": "p1",
            "schema": "v1",
            "sql": "SELECT * FROM t WHERE node_name != $nodes",
            "variables": [{"name": "nodes", "type": "custom"}],
        }
    ]
    with pytest.raises(RegistryError, match='is custom but has no "values"'):
        validate_registry(doc)


def test_accepts_a_query_variable_with_no_value_because_it_is_never_executed() -> None:
    doc = base()
    doc["teams"][0]["panels"] = [
        {
            "panel_id": "p1",
            "schema": "v1",
            "sql": "SELECT * FROM t WHERE node_name != $nodes",
            "variables": [{"name": "nodes", "type": "query"}],
        }
    ]
    validate_registry(doc)


def test_rejects_duplicate_panel_ids_within_a_team() -> None:
    doc = base()
    doc["teams"][0]["panels"] = [
        {"panel_id": "p1", "schema": "v1", "sql": "SELECT 1"},
        {"panel_id": "p1", "schema": "v1", "sql": "SELECT 2"},
    ]
    with pytest.raises(RegistryError, match="duplicate panel_id"):
        validate_registry(doc)


def test_the_checked_in_registry_loads_and_its_hash_covers_the_complete_file() -> None:
    loaded = load_registry()
    assert loaded.registry_version == loaded.registry["registry_version"]
    expected = hashlib.sha256(DEFAULT_REGISTRY_PATH.read_bytes()).hexdigest()
    assert loaded.file_sha256 == expected
    assert len(loaded.file_sha256) == 64


#: The registry file exactly as the javascript-mvp build read it. The live config/teams.json
#: moves on (teams are enrolled, operators change); this copy stays, so the hashing itself
#: is still pinned to what the JavaScript implementation computed.
PARITY_REGISTRY = (
    DEFAULT_REGISTRY_PATH.parents[1] / "test" / "fixtures" / "registry-javascript-mvp.json"
)


def test_the_registry_hash_matches_the_javascript_implementation() -> None:
    # Recorded from the javascript-mvp build against the same registry file. If this
    # fails, the two implementations would stamp different run records for one input.
    assert (
        load_registry(PARITY_REGISTRY).file_sha256
        == "ad7472d8c8e744a91eb9826956482899d70d1cf979bf5a4844fa076f018e87bc"
    )


def test_select_team_returns_exactly_one_entry_and_never_defaults() -> None:
    team = select_team(load_registry(), "checkout-api")
    assert team.team_id == "checkout-api"
    assert team.v1_operators == ("checkout", "Checkout-API")
    assert team.v2_operator == "checkout-api"


def test_select_team_fails_loudly_on_an_unknown_team_and_lists_the_known_ones() -> None:
    loaded = load_registry()
    with pytest.raises(RegistryError, match="is not in the registry"):
        select_team(loaded, "no-such-team")
    with pytest.raises(RegistryError, match="Known teams: .*checkout-api"):
        select_team(loaded, "no-such-team")


def test_the_team_snapshot_is_stable_and_compact() -> None:
    team = select_team(load_registry(), "fraud-detection")
    snapshot = snapshot_team_entry(team)
    assert snapshot_team_entry(team) == snapshot
    assert json.loads(snapshot)["v1_operators"] == ["fraud-detection"]
    # Compact separators, matching what the JavaScript implementation stored.
    assert ", " not in snapshot
    assert '": ' not in snapshot


def test_panels_are_selected_by_schema() -> None:
    team = select_team(load_registry(), "checkout-api")
    assert [p.panel_id for p in team.panels_for("v1")] == ["checkout-api-v1-main"]
    assert [p.panel_id for p in team.panels_for("v2")] == ["checkout-api-v2-main"]


def test_a_missing_registry_file_fails_before_any_query_could_run() -> None:
    with pytest.raises(RegistryError, match="cannot read registry"):
        load_registry("config/does-not-exist.json")


def test_invalid_json_fails_with_a_clear_message(tmp_path: Any) -> None:
    bad = tmp_path / "teams.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(RegistryError, match="is not valid JSON"):
        load_registry(bad)
