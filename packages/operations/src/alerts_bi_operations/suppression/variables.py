"""Grafana template-variable resolution from the frozen registry snapshot.

The MVP never calls Grafana. The standardization team supplies each panel's variable
definitions alongside the SQL, which removes live configuration drift from a reproducible
run.

Only ``custom``, ``constant`` and ``interval`` resolve. A ``query`` variable is never
executed, and a missing required definition is never guessed: either condition makes the
suppression leaf PRESENT BUT UNMEASURED, counted in ``suppression_unmeasured`` rather than
silently dropped, so an under-reported number is visible as under-reported instead of
passing for zero.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from alerts_bi_operations.registry import PanelVariable
from alerts_bi_operations.suppression.parser import Operand

__all__ = ["ResolvedVariable", "resolve_operand", "resolve_variable"]


@dataclass(frozen=True, slots=True)
class ResolvedVariable:
    resolved: bool
    values: tuple[str, ...] = ()
    all_selected: bool = False
    reason: str | None = None


def resolve_variable(name: str, definitions: Sequence[PanelVariable]) -> ResolvedVariable:
    definition = next((d for d in definitions if d.name == name), None)

    if definition is None:
        return ResolvedVariable(
            False, reason=f'no frozen definition supplied for variable "{name}"'
        )

    if definition.type == "query":
        return ResolvedVariable(
            False, reason=f'variable "{name}" is a query variable and is never executed'
        )

    if definition.type in ("constant", "interval"):
        if not isinstance(definition.value, str):
            return ResolvedVariable(
                False, reason=f'variable "{name}" is {definition.type} but supplies no value'
            )
        return ResolvedVariable(True, values=(definition.value,))

    # custom: the complete selected-value list, plus whether "all" was selected.
    if definition.values is None:
        return ResolvedVariable(False, reason=f'variable "{name}" is custom but supplies no values')
    return ResolvedVariable(
        True,
        values=tuple(definition.values),
        # The $__all case: a multi-value variable expanding to everything is exactly what
        # the blast-radius guard exists to catch, so it is resolved and then measured
        # rather than rejected here.
        all_selected=definition.all_selected,
    )


def resolve_operand(
    operand: Operand | None, definitions: Sequence[PanelVariable]
) -> ResolvedVariable:
    """Resolve an operand to the concrete set of values it stands for."""
    if operand is None:
        return ResolvedVariable(False, reason="missing operand")

    if operand.kind == "literal":
        return ResolvedVariable(True, values=(str(operand.value),))

    if operand.kind == "variable":
        return resolve_variable(str(operand.name), definitions)

    if operand.kind == "field":
        # A column-to-column comparison is not a value-based exclusion the team wrote to
        # hide specific alerts, so it is not interpreted.
        return ResolvedVariable(
            False, reason="comparison against another column is not interpreted"
        )

    return ResolvedVariable(False, reason=f'operand of kind "{operand.kind}" is not interpreted')
