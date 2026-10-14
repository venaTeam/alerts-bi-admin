"""Versioned JSON snapshots of *finished* analysis, with no executable serialization.

Only explicit resume callers use these. Fresh manual analyses continue to scan their
source. Successful model batches and retry budgets remain in the existing SQL journal.
"""

from __future__ import annotations

import gzip
import json
from dataclasses import asdict, fields
from datetime import datetime
from typing import Any

from alerts_bi_operations.registry import TeamEntry
from alerts_bi_shared.db.connection import Database
from alerts_bi_shared.hashing import sha256_of
from alerts_bi_shared.versions import PARSER_VERSION
from alerts_bi_shared.window import RunWindow

from ..config import AppConfig
from ..domain.normalize import normalize_row
from ..suppression.evaluate import LeafOutcome, PanelInterpretation
from ..suppression.stream import SuppressionSummary
from .streaming import IdentityDay, SchemaAccumulator, ScopeDay, StreamIdentity

FORMAT = 1


def source_hash(config: AppConfig) -> str:
    return sha256_of(
        [config.es.url, config.es.v1_index, config.es.v2_index, PARSER_VERSION, FORMAT]
    )


def encode(analyses: dict[str, SchemaAccumulator]) -> bytes:
    result: dict[str, Any] = {}
    for schema, analysis in analyses.items():
        if not analysis.finished:
            raise ValueError("Only complete scans can be checkpointed")
        identities = []
        for identity in analysis.identities.values():
            item = {
                f.name: getattr(identity, f.name)
                for f in fields(StreamIdentity)
                if f.name not in {"representative", "firing", "days"}
            }
            item["source"] = identity.representative.source
            item["days"] = {day: asdict(value) for day, value in identity.days.items()}
            identities.append(item)
        result[schema] = {
            "row_count": analysis.row_count,
            "scopes": {
                day: {
                    "count": scope.count,
                    "scopes": sorted(scope.scopes),
                    "nodes": sorted(scope.nodes),
                    "node_scopes": sorted(scope.node_scopes),
                }
                for day, scope in analysis.scopes.items()
            },
            "identities": identities,
            "suppression": asdict(analysis.suppression),
        }
    return gzip.compress(
        json.dumps(
            result,
            ensure_ascii=False,
            separators=(",", ":"),
            default=lambda value: value.isoformat(),
        ).encode(),
        compresslevel=1,
    )


def decode(payload: bytes, team: TeamEntry, window: RunWindow) -> dict[str, SchemaAccumulator]:
    raw = json.loads(gzip.decompress(payload))
    result = {}
    for schema in ("v1", "v2"):
        item = raw[schema]
        analysis = SchemaAccumulator(schema, window, team.panels_for(schema))
        analysis.finished = True
        analysis.row_count = item["row_count"]
        analysis.scopes = {
            day: ScopeDay(
                s["count"],
                {tuple(v) for v in s["scopes"]},
                {tuple(v) for v in s["nodes"]},
                {tuple(v) for v in s["node_scopes"]},
            )
            for day, s in item["scopes"].items()
        }
        for saved in item["identities"]:
            source = saved.pop("source")
            saved["first_seen"] = datetime.fromisoformat(saved["first_seen"])
            saved["open_since"] = (
                datetime.fromisoformat(saved["open_since"]) if saved["open_since"] else None
            )
            saved["days"] = {day: IdentityDay(**values) for day, values in saved["days"].items()}
            saved["sample_order"] = {
                rule: tuple(order) for rule, order in saved["sample_order"].items()
            }
            identity = StreamIdentity(representative=normalize_row(schema, source), **saved)
            analysis.identities[identity.identity] = identity
        suppression = item["suppression"]
        panels = []
        for saved_panel in suppression.pop("interpretations"):
            leaves = [
                LeafOutcome(**{**leaf, "values": tuple(leaf["values"])})
                for leaf in saved_panel.pop("leaves")
            ]
            panels.append(PanelInterpretation(**saved_panel, leaves=leaves))
        analysis.suppression = SuppressionSummary(**suppression, interpretations=panels)
        result[schema] = analysis
    return result


def load(
    db: Database, run_id: str, config: AppConfig, team: TeamEntry, window: RunWindow
) -> dict[str, SchemaAccumulator] | None:
    row = db.query_one(
        "SELECT payload FROM run_analysis_checkpoints WHERE run_id=:run AND source_hash=:source AND format_version=:format",
        {"run": run_id, "source": source_hash(config), "format": FORMAT},
    )
    return decode(row["payload"], team, window) if row else None


def save(
    db: Database, run_id: str, config: AppConfig, analysis: dict[str, SchemaAccumulator]
) -> None:
    payload = encode(analysis)
    with db.transaction():
        db.execute("DELETE FROM run_analysis_checkpoints WHERE run_id=:run", {"run": run_id})
        db.execute(
            "INSERT INTO run_analysis_checkpoints(run_id,source_hash,format_version,payload) VALUES(:run,:source,:format,:payload)",
            {"run": run_id, "source": source_hash(config), "format": FORMAT, "payload": payload},
        )
