from __future__ import annotations

from typing import Any

import pytest
from alerts_bi_operations.registry import Panel, PanelVariable, load_registry
from alerts_bi_operations.suppression.fields import classify_field
from alerts_bi_operations.suppression.lexer import SqlParseError, tokenize
from alerts_bi_operations.suppression.parser import collect_leaves, parse_panel_sql
from alerts_bi_operations.suppression.variables import resolve_variable
from src.engine.suppression.evaluate import (
    BLAST_RADIUS_LIMIT,
    LeafOutcome,
    SuppressionResult,
    build_r5_findings,
    evaluate_suppression,
    interpret_panel,
    like_to_regex,
)

from tests.engine.helpers.rows import v1_row


def panel(sql: str, variables: list[dict[str, Any]] | None = None, panel_id: str = "p1") -> Panel:
    return Panel(
        panel_id=panel_id,
        schema="v1",
        sql=sql,
        variables=tuple(PanelVariable.from_dict(v) for v in (variables or [])),
    )


def leaves_of(sql: str, variables: list[dict[str, Any]] | None = None) -> list[LeafOutcome]:
    return interpret_panel(panel(sql, variables)).leaves


def suppression_leaves(
    sql: str, variables: list[dict[str, Any]] | None = None
) -> list[LeafOutcome]:
    return [leaf for leaf in leaves_of(sql, variables) if leaf.kind == "suppression"]


def unmeasured_leaves(sql: str, variables: list[dict[str, Any]] | None = None) -> list[LeafOutcome]:
    return [leaf for leaf in leaves_of(sql, variables) if leaf.kind == "unmeasured"]


# ------------------------------------------------------------------- lexing


def test_tokenizes_strings_with_escaped_quotes() -> None:
    tokens = tokenize("WHERE node_name != 'it''s-a-node'")
    assert next(t for t in tokens if t.type == "string").value == "it's-a-node"


@pytest.mark.parametrize(
    ("text", "name"),
    [("$nodes", "nodes"), ("${nodes}", "nodes"), ("${nodes:csv}", "nodes"), ("[[nodes]]", "nodes")],
)
def test_tokenizes_all_grafana_variable_syntaxes(text: str, name: str) -> None:
    tokens = tokenize(f"WHERE node_name != {text}")
    assert next(t for t in tokens if t.type == "variable").value == name


def test_skips_line_and_block_comments() -> None:
    where = parse_panel_sql("SELECT * FROM t WHERE /* hidden */ node_name != 'x' -- trailing\n")
    assert where is not None
    assert where.type == "comparison"


def test_an_unterminated_string_is_a_parse_error_not_a_silent_truncation() -> None:
    with pytest.raises(SqlParseError):
        tokenize("WHERE node_name != 'unclosed")


# ------------------------------------------------------------------ parsing


def test_a_query_with_no_where_clause_suppresses_nothing() -> None:
    assert parse_panel_sql("SELECT * FROM appchi_v1_hot") is None
    assert leaves_of("SELECT * FROM appchi_v1_hot") == []


def test_and_chains_flatten_to_top_level_leaves() -> None:
    where = parse_panel_sql(
        "SELECT * FROM t WHERE operator = 'x' AND node_name != 'a' AND severity = 'critical'"
    )
    leaves = collect_leaves(where)
    assert len(leaves) == 3
    assert all(not nested for _, nested in leaves)


def test_leaves_inside_parentheses_that_are_still_and_ed_stay_top_level() -> None:
    where = parse_panel_sql(
        "SELECT * FROM t WHERE (operator = 'x' AND node_name != 'a') AND severity = 'critical'"
    )
    assert all(not nested for _, nested in collect_leaves(where))


def test_leaves_inside_an_or_are_marked_nested() -> None:
    where = parse_panel_sql(
        "SELECT * FROM t WHERE operator = 'x' AND (node_name != 'junk' OR severity = 'critical')"
    )
    leaves = collect_leaves(where)
    by_field = {leaf.field.name: nested for leaf, nested in leaves if leaf.field}
    assert by_field["operator"] is False
    assert by_field["node_name"] is True


def test_trailing_order_by_and_limit_end_the_where_expression() -> None:
    where = parse_panel_sql("SELECT * FROM t WHERE node_name != 'a' ORDER BY time DESC LIMIT 100")
    assert where is not None
    assert where.type == "comparison"


def test_a_grafana_time_macro_parses_as_a_predicate_and_is_ignored() -> None:
    leaves = leaves_of("SELECT * FROM t WHERE $__timeFilter(time_created) AND node_name != 'a'")
    assert leaves[0].kind == "ignored"
    assert "mechanical macro" in str(leaves[0].reason)
    assert leaves[1].kind == "suppression"


def test_a_table_qualified_column_resolves_to_the_bare_column_name() -> None:
    leaves = suppression_leaves("SELECT * FROM t a WHERE a.node_name != 'junk'")
    assert len(leaves) == 1
    assert leaves[0].field == "node_name"


def test_an_unparseable_panel_is_unmeasured_never_an_assumed_suppression() -> None:
    interpretation = interpret_panel(panel("SELECT * FROM t WHERE node_name !="))
    assert interpretation.safety_state == "unparseable"
    assert interpretation.unmeasured_reason
    assert interpretation.leaves == []


# ------------------------------------------------------- field classification


@pytest.mark.parametrize(
    "name",
    ["node_name", "message", "object", "component", "key_field", "alert_rule_url", "application"],
)
def test_instance_fields_are_classified_as_instance(name: str) -> None:
    assert classify_field(name) == "instance"


@pytest.mark.parametrize("name", ["severity", "environment", "status", "provider", "operator"])
def test_classification_dimensions_are_classified_as_such(name: str) -> None:
    assert classify_field(name) == "classification"


def test_an_unrecognised_field_is_unknown() -> None:
    assert classify_field("some_new_column") == "unknown"


def test_a_negation_on_a_classification_dimension_is_scoping_not_suppression() -> None:
    # This is the error that would mark a team's entire non-critical inventory bad.
    assert suppression_leaves("SELECT * FROM t WHERE severity = 'critical'") == []
    assert suppression_leaves("SELECT * FROM t WHERE environment != 'test'") == []
    assert suppression_leaves("SELECT * FROM t WHERE status != 'resolved'") == []


def test_an_identity_predicate_on_operator_is_ignored() -> None:
    assert suppression_leaves("SELECT * FROM t WHERE operator IN ('batch-team','BATCH_JOBS')") == []


def test_an_unknown_field_is_ignored_and_recorded_never_guessed_at() -> None:
    interpretation = interpret_panel(panel("SELECT * FROM t WHERE mystery_col != 'x'"))
    assert interpretation.unknown_fields == ["mystery_col"]
    assert interpretation.leaves[0].kind == "ignored"


def test_an_instance_field_compared_positively_is_not_suppression() -> None:
    assert suppression_leaves("SELECT * FROM t WHERE application = 'checkout'") == []
    assert suppression_leaves("SELECT * FROM t WHERE node_name IN ('a','b')") == []
    assert suppression_leaves("SELECT * FROM t WHERE message LIKE '%error%'") == []


@pytest.mark.parametrize(
    ("sql", "operator"),
    [
        ("SELECT * FROM t WHERE node_name != 'a'", "!="),
        ("SELECT * FROM t WHERE node_name <> 'a'", "<>"),
        ("SELECT * FROM t WHERE node_name NOT IN ('a','b')", "NOT IN"),
        ("SELECT * FROM t WHERE message NOT LIKE '%test%'", "NOT LIKE"),
    ],
)
def test_all_negation_forms_on_an_instance_field_are_suppression(sql: str, operator: str) -> None:
    assert suppression_leaves(sql)[0].operator == operator


def test_application_exclusion_counts_as_suppression() -> None:
    leaves = suppression_leaves("SELECT * FROM t WHERE application != 'legacy-app'")
    assert len(leaves) == 1
    assert leaves[0].values == ("legacy-app",)


# ------------------------------------------------------------ rewrite safety


def test_a_suppression_leaf_nested_inside_an_or_is_unmeasured_not_applied() -> None:
    leaves = unmeasured_leaves(
        "SELECT * FROM t WHERE operator = 'x' AND (node_name != 'junk' OR severity = 'critical')"
    )
    assert len(leaves) == 1
    assert "nested inside OR" in str(leaves[0].reason)


def test_a_suppression_leaf_under_a_not_is_unmeasured() -> None:
    assert len(unmeasured_leaves("SELECT * FROM t WHERE NOT (node_name != 'junk')")) == 1


# ------------------------------------------------------- template variables


def test_custom_constant_and_interval_variables_resolve_from_frozen_definitions() -> None:
    custom = resolve_variable("nodes", [PanelVariable("nodes", "custom", values=["a", "b"])])
    assert custom.values == ("a", "b")
    constant = resolve_variable("env", [PanelVariable("env", "constant", value="prod")])
    assert constant.values == ("prod",)
    interval = resolve_variable("step", [PanelVariable("step", "interval", value="5m")])
    assert interval.values == ("5m",)


def test_a_query_variable_is_never_executed_and_makes_its_leaf_unmeasured() -> None:
    quoted = unmeasured_leaves(
        "SELECT * FROM t WHERE node_name != '$nodes'", [{"name": "nodes", "type": "query"}]
    )
    assert quoted == [], "a quoted variable is a literal string, not a variable"

    unquoted = unmeasured_leaves(
        "SELECT * FROM t WHERE node_name != $nodes", [{"name": "nodes", "type": "query"}]
    )
    assert len(unquoted) == 1
    assert "query variable and is never executed" in str(unquoted[0].reason)


def test_a_missing_variable_definition_is_unmeasured_never_guessed() -> None:
    leaves = unmeasured_leaves("SELECT * FROM t WHERE node_name != $nodes", [])
    assert len(leaves) == 1
    assert "no frozen definition supplied" in str(leaves[0].reason)


def test_a_resolved_multi_value_variable_expands_to_its_complete_selected_list() -> None:
    leaves = suppression_leaves(
        "SELECT * FROM t WHERE node_name NOT IN ($nodes)",
        [{"name": "nodes", "type": "custom", "values": ["a", "b", "c"], "multi": True}],
    )
    assert leaves[0].values == ("a", "b", "c")


# ------------------------------------------------------------ LIKE semantics


def test_like_wildcards_translate_to_anchored_patterns() -> None:
    assert like_to_regex("%test%").search("this-is-a-test-node")
    assert not like_to_regex("test%").search("node-test")
    assert like_to_regex("test_").search("tests")
    assert not like_to_regex("a.b").search("axb"), "a dot must be literal, not any-char"


def test_matching_is_case_sensitive_which_can_only_narrow_the_suppression_set() -> None:
    assert not like_to_regex("%TEST%").search("a-test-node")


# ------------------------------------------------------------- row exclusion

ROWS = [
    v1_row(key_field="k1", node_name="legacy-heartbeat-node"),
    v1_row(key_field="k2", node_name="real-node-1"),
    v1_row(key_field="k3", node_name="real-node-2"),
    v1_row(key_field="k4", node_name=None, message="a real failure"),
]


def suppressed_nodes(result: SuppressionResult) -> list[str]:
    """Node names of the suppressed rows. A suppressed row always has a node name here;
    the NULL-node case is asserted separately by its own test."""
    return sorted(r.node_name or "" for r in ROWS if id(r) in result.suppressed_row_ids)


def test_a_suppression_leaf_excludes_exactly_the_rows_the_team_named() -> None:
    result = evaluate_suppression(
        ROWS, [panel("SELECT * FROM t WHERE node_name != 'legacy-heartbeat-node'")]
    )
    assert suppressed_nodes(result) == ["legacy-heartbeat-node"]


def test_a_row_whose_field_is_null_is_not_suppressed_by_a_negation_on_that_field() -> None:
    # Strict SQL three-valued logic would filter it out, but that is a NULL artefact and
    # not the team's admission that the alert is worthless.
    result = evaluate_suppression(
        ROWS, [panel("SELECT * FROM t WHERE node_name != 'legacy-heartbeat-node'")]
    )
    null_node_row = next(r for r in ROWS if r.node_name is None)
    assert id(null_node_row) not in result.suppressed_row_ids


def test_rows_are_counted_once_regardless_of_how_many_leaves_exclude_them() -> None:
    result = evaluate_suppression(
        ROWS,
        [
            panel(
                "SELECT * FROM t WHERE node_name != 'legacy-heartbeat-node' "
                "AND node_name NOT IN ('legacy-heartbeat-node')"
            )
        ],
    )
    assert len(result.suppressed_row_ids) == 1


# -------------------------------------------------------- multi-panel rules


def test_a_row_hidden_by_every_panel_is_suppressed() -> None:
    result = evaluate_suppression(
        ROWS,
        [
            panel("SELECT * FROM t WHERE node_name != 'legacy-heartbeat-node'", panel_id="a"),
            panel("SELECT * FROM t WHERE node_name NOT IN ('legacy-heartbeat-node')", panel_id="b"),
        ],
    )
    assert len(result.suppressed_row_ids) == 1


def test_a_row_visible_in_one_panel_is_not_suppressed() -> None:
    result = evaluate_suppression(
        ROWS,
        [
            panel("SELECT * FROM t WHERE node_name != 'legacy-heartbeat-node'", panel_id="a"),
            panel("SELECT * FROM t WHERE severity = 'error'", panel_id="b"),
        ],
    )
    assert result.suppressed_row_ids == set()


def test_an_unparseable_panel_prevents_unanimity_rather_than_being_skipped() -> None:
    result = evaluate_suppression(
        ROWS,
        [
            panel("SELECT * FROM t WHERE node_name != 'legacy-heartbeat-node'", panel_id="a"),
            panel("SELECT * FROM t WHERE node_name !=", panel_id="b"),
        ],
    )
    assert result.suppressed_row_ids == set()
    assert result.unmeasured_leaves >= 1


def test_a_team_with_no_panels_has_no_rule_5_findings_and_the_run_completes() -> None:
    result = evaluate_suppression(ROWS, [])
    assert result.suppressed_row_ids == set()
    assert result.unmeasured_leaves == 0


# --------------------------------------------------------- blast-radius guard


def test_a_leaf_reaching_more_than_half_the_owned_rows_is_unmeasured() -> None:
    result = evaluate_suppression(
        ROWS,
        [
            panel(
                "SELECT * FROM t WHERE node_name NOT IN "
                "('legacy-heartbeat-node','real-node-1','real-node-2')"
            )
        ],
    )
    assert result.suppressed_row_ids == set()
    assert result.unmeasured_leaves == 1
    assert "blast radius 3/4" in str(result.interpretations[0].leaves[0].reason)


def test_a_leaf_at_exactly_the_50_percent_limit_still_applies() -> None:
    result = evaluate_suppression(
        ROWS,
        [panel("SELECT * FROM t WHERE node_name NOT IN ('legacy-heartbeat-node','real-node-1')")],
    )
    assert len(result.suppressed_row_ids) == 2
    assert result.unmeasured_leaves == 0
    assert BLAST_RADIUS_LIMIT == 0.5


def test_an_all_selected_variable_is_caught_by_the_blast_radius_guard() -> None:
    result = evaluate_suppression(
        ROWS,
        [
            panel(
                "SELECT * FROM t WHERE node_name NOT IN ($nodes)",
                [
                    {
                        "name": "nodes",
                        "type": "custom",
                        "values": ["legacy-heartbeat-node", "real-node-1", "real-node-2"],
                        "multi": True,
                        "all_selected": True,
                    }
                ],
            )
        ],
    )
    assert result.suppressed_row_ids == set()
    assert result.unmeasured_leaves == 1


# -------------------------------------------------------------- R5 findings


def test_suppressed_rows_become_core_r5_findings_naming_the_panels() -> None:
    panels = [panel("SELECT * FROM t WHERE node_name != 'legacy-heartbeat-node'")]
    result = evaluate_suppression(ROWS, panels)
    findings = build_r5_findings(result.suppressed_row_ids, panels)
    assert len(findings) == 1
    finding = next(iter(findings.values()))
    assert finding.rule_id == "R5"
    assert finding.set == "core"
    assert finding.evidence["panels"] == ["p1"]


def test_the_interpretation_is_keyed_by_sql_text_hash_and_parser_version() -> None:
    a = interpret_panel(panel("SELECT * FROM t WHERE node_name != 'x'"))
    b = interpret_panel(panel("SELECT * FROM t WHERE node_name != 'x'"))
    c = interpret_panel(panel("SELECT * FROM t WHERE node_name != 'y'"))
    assert a.sql_text_hash == b.sql_text_hash
    assert a.sql_text_hash != c.sql_text_hash
    assert a.parser_version == "1.1.0"


def test_every_panel_in_the_checked_in_registry_interprets_without_a_parse_failure() -> None:
    for team in load_registry().teams:
        for team_panel in team.panels:
            interpretation = interpret_panel(team_panel)
            assert interpretation.safety_state == "parsed", (
                f"{team_panel.panel_id}: {interpretation.unmeasured_reason}"
            )


# ------------------------------------------------------------------- unseen


def _unseen(rows: list[Any], *panels: Panel) -> SuppressionResult:
    return evaluate_suppression(rows, list(panels))


def test_unseen_a_row_outside_the_application_narrowing_is_unseen() -> None:
    inside = v1_row(application="a")
    outside = v1_row(application="b", key_field="k2")
    result = _unseen([inside, outside], panel("SELECT * FROM t WHERE application = 'a'"))
    assert result.unseen_row_ids == {id(outside)}


def test_unseen_a_row_matching_the_narrowing_is_not_unseen() -> None:
    row = v1_row(application="a")
    result = _unseen([row], panel("SELECT * FROM t WHERE application = 'a'"))
    assert result.unseen_row_ids == set()


def test_unseen_a_null_node_name_is_never_hidden() -> None:
    row = v1_row(node_name=None)
    result = _unseen([row], panel("SELECT * FROM t WHERE node_name LIKE 'n%'"))
    assert result.unseen_row_ids == set()


def test_unseen_like_hides_a_non_matching_value() -> None:
    row = v1_row(node_name="x-1")
    result = _unseen([row], panel("SELECT * FROM t WHERE node_name LIKE 'n%'"))
    assert result.unseen_row_ids == {id(row)}


def test_unseen_needs_every_panel_to_hide_the_row() -> None:
    row = v1_row(application="b")
    showing = panel("SELECT * FROM t WHERE application = 'b'", panel_id="p1")
    hiding = panel("SELECT * FROM t WHERE application = 'a'", panel_id="p2")
    assert _unseen([row], showing, hiding).unseen_row_ids == set()
    assert _unseen([row], hiding, hiding).unseen_row_ids == {id(row)}


def test_unseen_is_disjoint_from_suppressed() -> None:
    row = v1_row(application="b", node_name="junk")
    others = [v1_row(application="a", node_name=f"n{i}", key_field=f"k{i}") for i in range(3)]
    result = _unseen(
        [row, *others],
        panel("SELECT * FROM t WHERE application = 'a' AND node_name != 'junk'"),
    )
    assert result.suppressed_row_ids == {id(row)}
    assert result.unseen_row_ids == set()


def test_unseen_or_nested_identity_leaf_is_unmeasured_and_hides_nothing() -> None:
    row = v1_row(application="b")
    result = _unseen(
        [row],
        panel("SELECT * FROM t WHERE (application = 'a' OR severity = 'critical')"),
    )
    assert result.unseen_unmeasured == 1
    assert result.unseen_row_ids == set()


def test_unseen_unresolved_query_variable_is_unmeasured() -> None:
    row = v1_row(application="b")
    result = _unseen(
        [row],
        panel(
            "SELECT * FROM t WHERE application = $app",
            [{"name": "app", "type": "query"}],
        ),
    )
    assert result.unseen_unmeasured == 1
    assert result.unseen_row_ids == set()


def test_unseen_all_selected_variable_never_hides() -> None:
    row = v1_row(application="b")
    result = _unseen(
        [row],
        panel(
            "SELECT * FROM t WHERE application IN ($apps)",
            [
                {
                    "name": "apps",
                    "type": "custom",
                    "values": ["a"],
                    "multi": True,
                    "all_selected": True,
                }
            ],
        ),
    )
    assert result.unseen_row_ids == set()


def test_unseen_is_none_when_the_schema_has_no_panel() -> None:
    result = _unseen([v1_row()])
    assert result.unseen_row_ids is None
    assert result.unseen_unmeasured == 0


def test_unseen_operator_narrowing_hides_a_different_operator() -> None:
    row = v1_row(operator="y")
    result = _unseen([row], panel("SELECT * FROM t WHERE operator IN ('x')"))
    assert result.unseen_row_ids == {id(row)}


def test_unseen_operator_remains_a_classification_field_for_suppression() -> None:
    result = _unseen([v1_row()], panel("SELECT * FROM t WHERE operator != 'team-op'"))
    assert result.suppressed_row_ids == set()


def test_unseen_classification_fields_never_hide() -> None:
    row = v1_row(severity="error")
    result = _unseen([row], panel("SELECT * FROM t WHERE severity = 'critical'"))
    assert result.unseen_row_ids == set()


def test_unseen_an_unparseable_panel_hides_nothing() -> None:
    row = v1_row()
    result = _unseen([row], panel("SELECT * FROM t WHERE node_name !="))
    assert result.unseen_row_ids == set()
    # The unmeasured zero must read as under-reported rather than as a measured zero.
    assert result.unseen_unmeasured == 1


def test_unseen_component_maps_the_v1_object_field() -> None:
    row = v1_row(object="c2")
    result = _unseen([row], panel("SELECT * FROM t WHERE object = 'c1'"))
    assert result.unseen_row_ids == {id(row)}
