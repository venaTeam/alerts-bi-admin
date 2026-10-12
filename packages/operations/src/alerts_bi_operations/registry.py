"""Team registry loading and validation (design section 3.1, flow step 1).

Ownership is supplied, never inferred. Validation completes before any Elasticsearch
query, because a registry mistake silently changes which alerts belong to a team and that
error is invisible in the output.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any

from alerts_bi_shared.hashing import sha256_text
from jsonschema import Draft202012Validator

__all__ = [
    "DEFAULT_REGISTRY_PATH",
    "SCHEMA_PATH",
    "LoadedRegistry",
    "Panel",
    "PanelVariable",
    "RegistryError",
    "TeamEntry",
    "load_registry",
    "select_team",
    "snapshot_team_entry",
    "validate_registry",
]

DEFAULT_REGISTRY_PATH = Path("config") / "teams.json"
SCHEMA_PATH = files("alerts_bi_operations").joinpath("resources", "teams.schema.json")


class RegistryError(Exception):
    """Raised for any registry problem, before alerts are queried."""

    def __init__(self, message: str, details: list[str] | None = None) -> None:
        self.details = details or []
        if self.details:
            message = message + "\n  - " + "\n  - ".join(self.details)
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class PanelVariable:
    name: str
    type: str
    value: str | None = None
    values: list[str] | None = None
    multi: bool = False
    all_selected: bool = False

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> PanelVariable:
        return PanelVariable(
            name=raw["name"],
            type=raw["type"],
            value=raw.get("value"),
            values=list(raw["values"]) if isinstance(raw.get("values"), list) else None,
            multi=bool(raw.get("multi", False)),
            all_selected=bool(raw.get("all_selected", False)),
        )


@dataclass(frozen=True, slots=True)
class Panel:
    panel_id: str
    schema: str
    sql: str
    variables: tuple[PanelVariable, ...] = ()

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> Panel:
        return Panel(
            panel_id=raw["panel_id"],
            schema=raw["schema"],
            sql=raw["sql"],
            variables=tuple(PanelVariable.from_dict(v) for v in raw.get("variables", [])),
        )


@dataclass(frozen=True, slots=True)
class TeamEntry:
    team_id: str
    display_name: str
    v1_operators: tuple[str, ...]
    v2_operator: str | None
    panels: tuple[Panel, ...] = ()
    #: Enrolled in the automatic weekly review (design section 7.11).
    weekly_review: bool = False
    #: Optional planning override: working days to rebuild one v1 alert rule in v2, used by
    #: the time-to-v2 estimate (team summary spec section 7.2). None means the default.
    v1_rule_effort_days: float | None = None
    #: The entry exactly as it appeared in the registry document, preserved so the stored
    #: snapshot is the supplied text rather than a re-serialization of our own model.
    raw: dict[str, Any] | None = None

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> TeamEntry:
        return TeamEntry(
            team_id=raw["team_id"],
            display_name=raw["display_name"],
            v1_operators=tuple(raw["v1_operators"]),
            v2_operator=raw["v2_operator"],
            panels=tuple(Panel.from_dict(p) for p in raw.get("panels", [])),
            weekly_review=bool((raw.get("weekly_review") or {}).get("enabled", False)),
            v1_rule_effort_days=(raw.get("planning") or {}).get("v1_rule_effort_days"),
            raw=raw,
        )

    def panels_for(self, schema: str) -> list[Panel]:
        """Panels that can describe rows of one schema.

        A v2 panel cannot exclude a v1 row, so suppression is always evaluated within a
        schema.
        """
        return [panel for panel in self.panels if panel.schema == schema]


@dataclass(frozen=True, slots=True)
class LoadedRegistry:
    registry: dict[str, Any]
    registry_version: str
    #: SHA-256 of the complete registry document as read, covering entries for teams this
    #: run did not select.
    file_sha256: str
    file_path: Path
    teams: tuple[TeamEntry, ...]


def validate_registry(
    document: Any, schema_path: Traversable = SCHEMA_PATH
) -> tuple[TeamEntry, ...]:
    """Validate against the checked-in JSON Schema plus the cross-entry rules.

    The schema cannot express the rules that span entries, and those are precisely the
    ones that corrupt attribution: an operator claimed by two teams, or an entry with no
    source operator at all.
    """
    schema = json.loads(schema_path.read_bytes().decode("utf-8"))
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(document), key=lambda e: list(e.absolute_path))
    if errors:
        details = [
            f"{'/' + '/'.join(str(p) for p in e.absolute_path) if e.absolute_path else '/'} "
            f"{e.message}"
            for e in errors
        ]
        raise RegistryError(f"registry does not match {schema_path}", details)

    problems: list[str] = []
    seen_team_ids: set[str] = set()
    # Exact, case-sensitive operator value -> owning team_id.
    seen_operators: dict[str, str] = {}
    entries: list[TeamEntry] = []

    for raw_team in document["teams"]:
        team = TeamEntry.from_dict(raw_team)
        entries.append(team)

        if team.team_id in seen_team_ids:
            problems.append(f'duplicate team_id "{team.team_id}"')
        seen_team_ids.add(team.team_id)

        # "At least one source operator must exist": an entry with neither cannot be
        # queried at all, and silently returning zero alerts would read as a clean team.
        if not team.v1_operators and team.v2_operator is None:
            problems.append(
                f'team "{team.team_id}" has no source operator: '
                "v1_operators is empty and v2_operator is null"
            )

        # Uniqueness is a CROSS-team rule. One team may legitimately carry the same string
        # as both a v1 operator and its v2 operator - a team that kept its name through
        # the migration - so the same value twice inside one entry is not a conflict.
        own = set(team.v1_operators)
        if team.v2_operator is not None:
            own.add(team.v2_operator)
        for operator in sorted(own):
            owner = seen_operators.get(operator)
            if owner is not None and owner != team.team_id:
                problems.append(
                    f'operator "{operator}" is claimed by both "{owner}" and '
                    f'"{team.team_id}" (matching is exact and case-sensitive)'
                )
            else:
                seen_operators[operator] = team.team_id

        panel_ids: set[str] = set()
        for panel in team.panels:
            if panel.panel_id in panel_ids:
                problems.append(f'team "{team.team_id}" has duplicate panel_id "{panel.panel_id}"')
            panel_ids.add(panel.panel_id)

            variable_names: set[str] = set()
            for variable in panel.variables:
                if variable.name in variable_names:
                    problems.append(
                        f'panel "{panel.panel_id}" of team "{team.team_id}" defines '
                        f'variable "{variable.name}" twice'
                    )
                variable_names.add(variable.name)
                if variable.type in ("constant", "interval") and variable.value is None:
                    problems.append(
                        f'panel "{panel.panel_id}" variable "{variable.name}" is '
                        f'{variable.type} but has no "value"'
                    )
                if variable.type == "custom" and variable.values is None:
                    problems.append(
                        f'panel "{panel.panel_id}" variable "{variable.name}" is custom '
                        'but has no "values"'
                    )

    if problems:
        raise RegistryError("registry validation failed", problems)
    return tuple(entries)


def load_registry(
    file_path: Path | str = DEFAULT_REGISTRY_PATH,
    schema_path: Traversable = SCHEMA_PATH,
) -> LoadedRegistry:
    """Read, hash and validate the registry document.

    The hash covers the complete file BYTES rather than the parsed object, so a run can
    prove exactly which document it used - including the entries for teams it did not
    select. Bytes are decoded explicitly rather than read through universal-newline
    translation, which would silently change the hash on a CRLF checkout.
    """
    path = Path(file_path)
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise RegistryError(f"cannot read registry at {path.as_posix()}: {exc}") from exc

    raw_text = raw_bytes.decode("utf-8")
    try:
        document = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise RegistryError(f"registry at {path.as_posix()} is not valid JSON: {exc}") from exc

    teams = validate_registry(document, schema_path)
    return LoadedRegistry(
        registry=document,
        registry_version=document["registry_version"],
        file_sha256=sha256_text(raw_text),
        file_path=path,
        teams=teams,
    )


def select_team(loaded: LoadedRegistry, team_id: str) -> TeamEntry:
    """Select exactly one team. A run never defaults to all teams (design section 6)."""
    for team in loaded.teams:
        if team.team_id == team_id:
            return team
    known = ", ".join(sorted(t.team_id for t in loaded.teams))
    raise RegistryError(f'team "{team_id}" is not in the registry. Known teams: {known}')


def snapshot_team_entry(team: TeamEntry) -> str:
    """Immutable snapshot of the selected entry, stored with the run.

    Serialized compactly and without ASCII escaping so the stored text is byte-identical
    to what the superseded JavaScript implementation stored, which keeps run records
    comparable across the port.
    """
    source = team.raw if team.raw is not None else _entry_to_dict(team)
    return json.dumps(source, separators=(",", ":"), ensure_ascii=False)


def _entry_to_dict(team: TeamEntry) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "team_id": team.team_id,
        "display_name": team.display_name,
        "v1_operators": list(team.v1_operators),
        "v2_operator": team.v2_operator,
    }
    if team.panels:
        entry["panels"] = [
            {"panel_id": p.panel_id, "schema": p.schema, "sql": p.sql} for p in team.panels
        ]
    return entry
