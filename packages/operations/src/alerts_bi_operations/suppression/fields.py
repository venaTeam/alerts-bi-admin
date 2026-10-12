"""The authoritative field-classification table (design section 5.2).

The discriminator between suppression and scoping is NOT the operator - scoping predicates
are commonly negations too. It is what the field identifies:

- an INSTANCE-LEVEL field points at specific alerts the team knows are junk, so a negation
  on it is the team's own written admission that those alerts are worthless;
- a CLASSIFICATION DIMENSION narrows the view, and reading ``severity = 'critical'`` as
  suppression would mark a team's entire non-critical inventory as bad alerts.

Unknown fields are ignored and their names logged, never guessed at. That biases the parse
toward false negatives, which is the direction the design demands: an unrecognised field
quietly under-reports ``suppressed``, whereas guessing could mark good alerts bad - and a
team only has to catch us wrong once.
"""

from __future__ import annotations

from typing import Final, Literal

__all__ = [
    "CLASSIFICATION_FIELDS",
    "IDENTITY_FIELDS",
    "INSTANCE_FIELDS",
    "MECHANICAL_MACROS",
    "FieldClass",
    "classify_field",
    "identity_attribute_for",
    "is_identity_field",
    "record_attribute_for",
]

FieldClass = Literal["instance", "classification", "unknown"]

#: A negation on one of these is suppression.
INSTANCE_FIELDS: Final = frozenset(
    {
        "node_name",
        "message",
        "object",
        "component",
        "key_field",
        "alert_rule_url",
        # Debatable, and deliberately included: `application != 'legacy-app'` hides an
        # entire application's alerts from the team that owns them, which is the phase-0
        # problem in its purest form. A team legitimately running one panel per
        # application is protected by the multi-panel unanimity rule rather than by this
        # table.
        "application",
    }
)

#: Narrowing the view. Ignored entirely, negated or not.
CLASSIFICATION_FIELDS: Final = frozenset(
    {"severity", "environment", "status", "provider", "operator"}
)

#: A POSITIVE predicate on one of these narrows a panel to a team's own alerts. It is read
#: only for `unseen` (spec section 6), never for ownership and never as suppression.
#: `operator` stays a classification field for suppression and is an identity field here.
IDENTITY_FIELDS: Final = frozenset({"operator", "application", "node_name", "object", "component"})

#: Mechanical constructs that carry no ownership or suppression meaning.
MECHANICAL_MACROS: Final = frozenset({"__timeFilter", "__timeFrom", "__timeTo", "__interval"})

#: Which internal record attribute backs a panel field name.
#:
#: ``object`` and ``component`` are the same concept under the two schemas, so a v1 panel
#: writing ``object`` and a v2 panel writing ``component`` both resolve to the record's
#: component field.
_RECORD_ATTRIBUTES: Final = {
    "node_name": "node_name",
    "message": "message",
    "object": "component",
    "component": "component",
    "key_field": "key_field",
    "alert_rule_url": "alert_rule_url",
    "application": "application",
}


#: Which record attribute an identity field reads. ``operator`` is mapped here only; the
#: suppression table above deliberately has no entry for it.
_IDENTITY_ATTRIBUTES: Final = {
    "operator": "operator",
    "application": "application",
    "node_name": "node_name",
    "object": "component",
    "component": "component",
}


def is_identity_field(field_name: str) -> bool:
    return field_name.lower() in IDENTITY_FIELDS


def identity_attribute_for(field_name: str) -> str | None:
    return _IDENTITY_ATTRIBUTES.get(field_name.lower())


def classify_field(field_name: str) -> FieldClass:
    name = field_name.lower()
    if name in INSTANCE_FIELDS:
        return "instance"
    if name in CLASSIFICATION_FIELDS:
        return "classification"
    return "unknown"


def record_attribute_for(field_name: str) -> str | None:
    return _RECORD_ATTRIBUTES.get(field_name.lower())
