"""Panel-suppression measurement (design section 5.2; flow step 5).

Its entire job is one thing: find the predicates by which a team deliberately filters its
own alerts out of its own panel, and mark those alerts under core rule 5.

This step feeds ``flagged``, so a scoping predicate misread as suppression would mark good
alerts as bad - the single most expensive error this design can make. Every ambiguity
therefore resolves to ``unmeasured`` rather than to a suppression.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from typing import Final

from alerts_bi_operations.registry import Panel, PanelVariable
from alerts_bi_operations.suppression.fields import (
    MECHANICAL_MACROS,
    classify_field,
    identity_attribute_for,
    is_identity_field,
    record_attribute_for,
)
from alerts_bi_operations.suppression.parser import (
    Leaf,
    SqlParseError,
    collect_leaves,
    parse_panel_sql,
)
from alerts_bi_operations.suppression.variables import resolve_operand
from alerts_bi_shared.hashing import sha256_text
from alerts_bi_shared.logging_setup import log
from alerts_bi_shared.versions import PARSER_VERSION

from ..domain.normalize import AlertRecord
from ..rules.core import Finding

__all__ = [
    "BLAST_RADIUS_LIMIT",
    "LeafOutcome",
    "PanelInterpretation",
    "SuppressionResult",
    "build_r5_findings",
    "evaluate_suppression",
    "interpret_panel",
    "like_to_regex",
]

#: A suppression leaf may not reach further than this share of the team's owned rows.
BLAST_RADIUS_LIMIT: Final = 0.5


@dataclass(slots=True)
class LeafOutcome:
    kind: str
    """``suppression`` | ``ignored`` | ``unmeasured`` | ``identity`` | ``identity_unmeasured``

    The two ``identity`` kinds serve `unseen` only: a positive narrowing predicate that can
    hide a row from the panel, and one that could not be evaluated. Neither is suppression.
    """
    field: str | None = None
    operator: str | None = None
    reason: str | None = None
    values: tuple[str, ...] = ()
    matched_rows: int | None = None
    attribute: str | None = None
    """Internal only: which record attribute this leaf reads."""

    def to_public(self) -> dict[str, object]:
        """The published shape, without the internal attribute pointer."""
        out: dict[str, object] = {"kind": self.kind}
        for name in ("field", "operator", "reason", "matched_rows"):
            value = getattr(self, name)
            if value is not None:
                out[name] = value
        if self.values:
            out["values"] = list(self.values)
        return out


@dataclass(slots=True)
class PanelInterpretation:
    panel_id: str
    schema: str
    sql_text_hash: str
    parser_version: str
    safety_state: str
    """``parsed`` | ``unparseable``"""
    unmeasured_reason: str | None = None
    leaves: list[LeafOutcome] = field(default_factory=list)
    unknown_fields: list[str] = field(default_factory=list)


@dataclass(slots=True)
class SuppressionResult:
    suppressed_row_ids: set[int]
    """``id()`` of each suppressed row; identity comparison, not value comparison."""
    unmeasured_leaves: int
    interpretations: list[PanelInterpretation]
    notes: list[str]
    unseen_row_ids: set[int] | None = None
    """``id()`` of each row no panel shows through identity narrowing, disjoint from
    ``suppressed_row_ids``. None when the schema has no supplied panel (spec section 6)."""
    unseen_unmeasured: int = 0
    """Identity leaves that could not be evaluated (OR-nested or unresolved variable)."""


@lru_cache(maxsize=1024)
def like_to_regex(pattern: str) -> re.Pattern[str]:
    """Convert a SQL LIKE pattern to an anchored regular expression.

    Matching is case-SENSITIVE. A panel runs against SQL Server, whose default collation is
    case-insensitive, but both sides of this comparison are written by the same team and
    match exactly in practice. Case-insensitive matching could only ever widen the
    suppression set, and widening is the direction that marks good alerts bad
    (design section 7.6).
    """
    out: list[str] = []
    for char in pattern:
        if char == "%":
            out.append("[\\s\\S]*")
        elif char == "_":
            out.append("[\\s\\S]")
        else:
            out.append(re.escape(char))
    return re.compile("^" + "".join(out) + "$")


def _leaf_excludes_row(leaf: LeafOutcome, row: AlertRecord) -> bool:
    """Does a leaf exclude this row from the panel?

    Deliberately positive-match semantics: a leaf excludes a row only when the row's value
    actually equals (or LIKE-matches) the value the team wrote. Strict SQL three-valued
    logic would also filter out rows whose field is NULL - ``node_name != 'X'`` is UNKNOWN
    when node_name is NULL - but that is an artefact of NULL handling, not a team's
    admission that the alert is worthless, and treating it as one would flag every
    node-less alert of every team that writes a single node exclusion (design section 7.6).
    """
    raw = getattr(row, leaf.attribute) if leaf.attribute else None
    if not isinstance(raw, str):
        return False
    if leaf.operator == "NOT LIKE":
        return any(like_to_regex(pattern).search(raw) is not None for pattern in leaf.values)
    return raw in leaf.values


def _leaf_hides_row(leaf: LeafOutcome, row: AlertRecord) -> bool:
    """Does an identity leaf positively mismatch this row?

    A NULL (or non-string) value never hides: an alert with no ``node_name`` is not shown to
    be outside a panel that narrows on ``node_name``.
    """
    raw = getattr(row, leaf.attribute) if leaf.attribute else None
    if not isinstance(raw, str):
        return False
    if leaf.operator == "LIKE":
        return not any(like_to_regex(pattern).search(raw) is not None for pattern in leaf.values)
    return raw not in leaf.values


def _positive_form(leaf: Leaf) -> tuple[str, tuple[object, ...]] | None:
    """Recognize ``=``, ``IN`` and ``LIKE`` (not negated) and return their operands."""
    if leaf.type == "comparison" and leaf.operator == "=":
        return "=", (leaf.value,)
    if leaf.type == "in" and not leaf.negated:
        return "IN", tuple(leaf.values)
    if leaf.type == "like" and not leaf.negated:
        return "LIKE", (leaf.pattern,)
    return None


def _interpret_identity_leaf(
    leaf: Leaf,
    field_name: str,
    nested: bool,
    definitions: Sequence[PanelVariable],
) -> LeafOutcome | None:
    """Interpret a positive narrowing predicate for `unseen`, or None if it is not one."""
    positive = _positive_form(leaf)
    if positive is None:
        return None
    operator, operands = positive

    if nested:
        return LeafOutcome(
            "identity_unmeasured",
            field=field_name,
            operator=operator,
            reason="identity leaf is nested inside OR/NOT and cannot be evaluated safely",
        )

    values: list[str] = []
    for operand in operands:
        resolved = resolve_operand(operand, definitions)  # type: ignore[arg-type]
        if not resolved.resolved:
            return LeafOutcome(
                "identity_unmeasured",
                field=field_name,
                operator=operator,
                reason=resolved.reason or "operand could not be resolved",
            )
        if resolved.all_selected:
            # "All" shows every alert, so the leaf can hide nothing.
            return LeafOutcome(
                "ignored", field=field_name, reason="multi-value variable with all selected"
            )
        values.extend(resolved.values)

    attribute = identity_attribute_for(field_name)
    if not values or attribute is None:
        return LeafOutcome(
            "identity_unmeasured",
            field=field_name,
            operator=operator,
            reason="identity leaf resolved to no values or has no alert attribute",
        )
    return LeafOutcome(
        "identity", field=field_name, operator=operator, values=tuple(values), attribute=attribute
    )


def _negation_of(leaf: Leaf) -> tuple[str, tuple[object, ...]] | None:
    """Recognize the negation forms and return the operands they exclude."""
    if leaf.type == "comparison" and leaf.operator in ("!=", "<>"):
        return leaf.operator, (leaf.value,)
    if leaf.type == "in" and leaf.negated:
        return "NOT IN", tuple(leaf.values)
    if leaf.type == "like" and leaf.negated:
        return "NOT LIKE", (leaf.pattern,)
    return None


def _interpret_leaf(
    leaf: Leaf,
    nested: bool,
    definitions: Sequence[PanelVariable],
    interpretation: PanelInterpretation,
) -> LeafOutcome:
    # Mechanical constructs carry no ownership or suppression meaning.
    if leaf.type == "call":
        if leaf.macro and leaf.name in MECHANICAL_MACROS:
            return LeafOutcome("ignored", reason=f"mechanical macro ${leaf.name}")
        return LeafOutcome("ignored", reason=f"function call {leaf.name}() is not interpreted")

    if leaf.field is None or leaf.field.kind != "field":
        return LeafOutcome("ignored", reason="predicate does not compare a plain column")

    field_name = str(leaf.field.name)
    field_class = classify_field(field_name)

    # Decided before the classification branch: `operator` is a classification field for
    # suppression but an identity field for `unseen`. Negations fall through unchanged.
    if is_identity_field(field_name):
        identity = _interpret_identity_leaf(leaf, field_name, nested, definitions)
        if identity is not None:
            return identity

    if field_class == "classification":
        # Scoping, not suppression - and this distinction is the one that cannot be dropped.
        return LeafOutcome("ignored", field=field_name, reason="classification dimension")

    if field_class == "unknown":
        if field_name not in interpretation.unknown_fields:
            interpretation.unknown_fields.append(field_name)
        log.info("suppression.unknown_field", panel_id=interpretation.panel_id, field=field_name)
        return LeafOutcome("ignored", field=field_name, reason="unknown field, ignored and logged")

    # Instance-level field: only a NEGATION is suppression.
    negation = _negation_of(leaf)
    if negation is None:
        return LeafOutcome("ignored", field=field_name, reason="instance field, but not a negation")
    operator, operands = negation

    if nested:
        # Rewrite safety: a leaf inside an OR or a NOT cannot be lifted out without
        # changing the query's meaning, so it is present but unmeasured.
        return LeafOutcome(
            "unmeasured",
            field=field_name,
            operator=operator,
            reason="suppression leaf is nested inside OR/NOT and cannot be evaluated safely",
        )

    values: list[str] = []
    for operand in operands:
        resolved = resolve_operand(operand, definitions)  # type: ignore[arg-type]
        if not resolved.resolved:
            return LeafOutcome(
                "unmeasured",
                field=field_name,
                operator=operator,
                reason=resolved.reason or "operand could not be resolved",
            )
        values.extend(resolved.values)

    if not values:
        return LeafOutcome(
            "unmeasured",
            field=field_name,
            operator=operator,
            reason="negation resolved to no values",
        )

    attribute = record_attribute_for(field_name)
    if attribute is None:
        return LeafOutcome(
            "unmeasured",
            field=field_name,
            operator=operator,
            reason="instance field has no corresponding alert attribute",
        )

    return LeafOutcome(
        "suppression",
        field=field_name,
        operator=operator,
        values=tuple(values),
        attribute=attribute,
    )


def interpret_panel(panel: Panel) -> PanelInterpretation:
    """Interpret one panel's SQL into suppression leaves."""
    interpretation = PanelInterpretation(
        panel_id=panel.panel_id,
        schema=panel.schema,
        sql_text_hash=sha256_text(panel.sql),
        parser_version=PARSER_VERSION,
        safety_state="parsed",
    )

    try:
        where = parse_panel_sql(panel.sql)
    except SqlParseError as exc:
        # An unparseable panel is not a suppression finding. A team with no panel, an
        # unparseable panel, or a panel nobody can find gets null visibility and the run
        # completes normally.
        interpretation.safety_state = "unparseable"
        interpretation.unmeasured_reason = str(exc)
        return interpretation

    if where is None:
        return interpretation

    for leaf, nested in collect_leaves(where):
        interpretation.leaves.append(_interpret_leaf(leaf, nested, panel.variables, interpretation))
    return interpretation


def evaluate_suppression(rows: Sequence[AlertRecord], panels: Sequence[Panel]) -> SuppressionResult:
    """Evaluate suppression for one schema's owned rows against the team's panels.

    Multi-panel semantics: a row counts as suppressed only if EVERY applicable panel
    excludes it. A row filtered out of one panel but visible in another is not hidden from
    the team, and flagging it would be a false positive of exactly the kind this section
    warns about. Rows are counted once regardless of how many panels exclude them.
    """
    notes: list[str] = []
    interpretations = [interpret_panel(panel) for panel in panels]
    unmeasured_leaves = 0
    excluded_per_panel: list[set[int]] = []
    hidden_per_panel: list[set[int]] = []
    unseen_unmeasured = 0

    for interpretation in interpretations:
        excluded: set[int] = set()
        # Rows this panel hides by a positive identity narrowing. An unparseable panel
        # hides nothing, so it can never make a row unseen on incomplete evidence.
        hidden: set[int] = set()
        hidden_per_panel.append(hidden)

        if interpretation.safety_state == "unparseable":
            unmeasured_leaves += 1
            # The panel's narrowing is unknown too, so `unseen` is under-reported.
            unseen_unmeasured += 1
            notes.append(f"panel {interpretation.panel_id}: {interpretation.unmeasured_reason}")
            # An unparseable panel cannot be shown to exclude anything, and unanimity
            # requires every panel to exclude a row, so it contributes an empty set -
            # which correctly prevents any row from being unanimously suppressed on
            # incomplete evidence.
            excluded_per_panel.append(excluded)
            continue

        for leaf in interpretation.leaves:
            if leaf.kind == "identity_unmeasured":
                unseen_unmeasured += 1
                notes.append(f"panel {interpretation.panel_id}: {leaf.field} - {leaf.reason}")
                continue
            if leaf.kind == "identity":
                # No blast-radius guard: nothing is marked bad.
                hidden.update(id(row) for row in rows if _leaf_hides_row(leaf, row))
                continue
            if leaf.kind == "unmeasured":
                unmeasured_leaves += 1
                notes.append(f"panel {interpretation.panel_id}: {leaf.field} - {leaf.reason}")
                continue
            if leaf.kind != "suppression":
                continue

            matched = [row for row in rows if _leaf_excludes_row(leaf, row)]

            # Blast-radius guard: a multi-value variable expanding to everything turns
            # `node_name != '$nodes'` into an exclusion of the team's entire inventory,
            # and marking half a team's alerts bad on a parse artefact is the most
            # expensive error available here. No legitimate suppression clause has that
            # reach.
            if rows and len(matched) / len(rows) > BLAST_RADIUS_LIMIT:
                leaf.kind = "unmeasured"
                leaf.matched_rows = len(matched)
                leaf.reason = (
                    f"blast radius {len(matched)}/{len(rows)} exceeds "
                    f"{BLAST_RADIUS_LIMIT * 100:.0f}% of owned rows; routed to human review"
                )
                unmeasured_leaves += 1
                notes.append(f"panel {interpretation.panel_id}: {leaf.field} - {leaf.reason}")
                continue

            leaf.matched_rows = len(matched)
            excluded.update(id(row) for row in matched)

        excluded_per_panel.append(excluded)

    suppressed: set[int] = set()
    if excluded_per_panel:
        # Unanimity: intersect across panels.
        suppressed = set(excluded_per_panel[0])
        for other in excluded_per_panel[1:]:
            suppressed &= other

    unseen: set[int] | None = None
    if hidden_per_panel:
        # Unanimity again, then disjoint from suppression: a suppressed row counts once.
        unseen = set(hidden_per_panel[0])
        for other in hidden_per_panel[1:]:
            unseen &= other
        unseen -= suppressed

    return SuppressionResult(
        suppressed_row_ids=suppressed,
        unmeasured_leaves=unmeasured_leaves,
        interpretations=interpretations,
        notes=notes,
        unseen_row_ids=unseen,
        unseen_unmeasured=unseen_unmeasured,
    )


def build_r5_findings(suppressed_row_ids: set[int], panels: Sequence[Panel]) -> dict[int, Finding]:
    """Build the R5 findings for the suppressed rows, keyed by ``id(row)``."""
    panel_ids = [p.panel_id for p in panels]
    evidence = {
        "panels": panel_ids,
        "reason": "excluded by every supplied panel for this schema",
    }
    return {row_id: Finding("R5", "core", dict(evidence)) for row_id in suppressed_row_ids}


def utc_now() -> datetime:
    """Timestamp for cached parse records."""
    return datetime.now(UTC)
