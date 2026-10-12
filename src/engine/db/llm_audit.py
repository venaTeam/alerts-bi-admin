"""SQL audit independent of completed runs and production verdict reuse.

The session lock prevents simultaneous resumes. Started attempts with no committed result
consume an attempt on recovery: the remote endpoint may already have performed the work.
Successful responses are replayed locally, never requested again for the same batch bytes.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from alerts_bi_shared.db.connection import Database
from alerts_bi_shared.hashing import compact_json, sha256_of, sha256_text

from ..llm.response import response_json_schema
from .repositories import RunIsPublished


class SqlLlmJournal:
    def __init__(self, db: Database, scope_id: str, *, kind: str = "run") -> None:
        self.db = db
        self.scope_id = scope_id
        self.kind = kind

    @contextmanager
    def locked(self) -> Iterator[SqlLlmJournal]:
        resource = f"llm-review:{self.scope_id}"
        row = self.db.query_one(
            "DECLARE @result INT; EXEC @result = sys.sp_getapplock "
            "@Resource=:resource, @LockMode='Exclusive', @LockOwner='Session', @LockTimeout=0; "
            "SELECT @result AS result",
            {"resource": resource},
        )
        self.db.commit()
        if row is None or int(row["result"]) < 0:
            raise ValueError("this LLM review is already running")
        try:
            if self.kind == "run" and self.db.query_one(
                "SELECT 1 AS published FROM review_publications "
                "WHERE run_id=:scope AND withdrawn_at IS NULL",
                {"scope": self.scope_id},
            ):
                raise RunIsPublished("published runs cannot start another LLM assessment")
            yield self
        finally:
            self.db.rollback()
            self.db.execute(
                "EXEC sys.sp_releaseapplock @Resource=:resource, @LockOwner='Session'",
                {"resource": resource},
            )
            self.db.commit()

    def configure(
        self, system_prompt: str, prompt_version: str, model_version: str, settings: dict[str, Any]
    ) -> None:
        schema = compact_json(response_json_schema())
        prompt_hash, schema_hash = sha256_text(system_prompt), sha256_text(schema)
        with self.db.transaction():
            row = self.db.query_one(
                "SELECT * FROM llm_prompt_artifacts WITH (UPDLOCK, HOLDLOCK) "
                "WHERE prompt_version=:version",
                {"version": prompt_version},
            )
            if row is not None:
                if (
                    row["system_prompt_hash"] != prompt_hash
                    or row["response_schema_hash"] != schema_hash
                ):
                    raise ValueError(
                        "prompt or response schema changed without a prompt version bump"
                    )
            else:
                self.db.execute(
                    "INSERT INTO llm_prompt_artifacts VALUES (:version,:hash,:prompt,:schema_hash,:schema)",
                    {
                        "version": prompt_version,
                        "hash": prompt_hash,
                        "prompt": system_prompt,
                        "schema_hash": schema_hash,
                        "schema": schema,
                    },
                )
            settings_hash = sha256_of(settings)
            prior = self.db.query_one(
                "SELECT * FROM llm_review_scopes WHERE scope_id=:scope", {"scope": self.scope_id}
            )
            if prior is not None:
                if (
                    prior["prompt_version"],
                    prior["model_version"],
                    prior["settings_hash"],
                    prior["scope_kind"],
                ) != (prompt_version, model_version, settings_hash, self.kind):
                    raise ValueError(
                        "review settings changed for the same scope; use a new version/trial"
                    )
            else:
                self.db.execute(
                    "INSERT INTO llm_review_scopes "
                    "(scope_id,scope_kind,prompt_version,model_version,settings_hash,settings_json) "
                    "VALUES (:scope,:kind,:prompt,:model,:hash,:settings)",
                    {
                        "scope": self.scope_id,
                        "kind": self.kind,
                        "prompt": prompt_version,
                        "model": model_version,
                        "hash": settings_hash,
                        "settings": compact_json(settings),
                    },
                )

    def prepare(self, batch_id: str, request_text: str, request_hash: str) -> str:
        with self.db.transaction():
            latest = self.db.query_one(
                "SELECT TOP 1 * FROM llm_review_batches "
                "WHERE scope_id=:scope AND batch_id=:batch ORDER BY cycle_number DESC",
                {"scope": self.scope_id, "batch": batch_id},
            )
            if latest is not None:
                if latest["request_hash"] != request_hash:
                    raise ValueError(
                        "batch source changed under an existing run; use a new run/version"
                    )
                if latest["status"] != "exhausted":
                    return str(latest["cycle_id"])
            number = 1 if latest is None else int(latest["cycle_number"]) + 1
            cycle = sha256_of([self.scope_id, batch_id, number])
            self.db.execute(
                "INSERT INTO llm_review_batches "
                "(cycle_id,scope_id,batch_id,cycle_number,request_hash,request_payload,status) "
                "VALUES (:cycle,:scope,:batch,:number,:hash,:payload,'running')",
                {
                    "cycle": cycle,
                    "scope": self.scope_id,
                    "batch": batch_id,
                    "number": number,
                    "hash": request_hash,
                    "payload": request_text,
                },
            )
        return cycle

    def attempt(self, cycle: str, number: int) -> dict[str, Any] | None:
        return self.db.query_one(
            "SELECT * FROM llm_review_attempts WHERE cycle_id=:cycle AND attempt_number=:number",
            {"cycle": cycle, "number": number},
        )

    def start(self, cycle: str, number: int, created_at: datetime) -> None:
        with self.db.transaction():
            self.db.execute(
                "INSERT INTO llm_review_attempts "
                "(cycle_id,attempt_number,status,created_at) VALUES (:cycle,:number,'started',:created)",
                {"cycle": cycle, "number": number, "created": created_at.replace(tzinfo=None)},
            )

    def finish(
        self,
        cycle: str,
        number: int,
        *,
        status: str,
        response: str | None,
        reason: str | None,
        metadata: dict[str, Any],
        duration_ms: int,
        completed_at: datetime,
    ) -> datetime:
        # Match DATETIME2(3) exactly so first execution and replay share one timestamp.
        completed = completed_at.replace(
            tzinfo=None, microsecond=completed_at.microsecond // 1000 * 1000
        )
        with self.db.transaction():
            self.db.execute(
                "UPDATE llm_review_attempts SET status=:status,response_text=:response,"
                "failure_reason=:reason,metadata_json=:metadata,duration_ms=:duration,completed_at=:completed "
                "WHERE cycle_id=:cycle AND attempt_number=:number",
                {
                    "cycle": cycle,
                    "number": number,
                    "status": status,
                    "response": response,
                    "reason": reason,
                    "metadata": compact_json(metadata),
                    "duration": duration_ms,
                    "completed": completed,
                },
            )
        return completed

    def complete_batch(self, cycle: str, *, succeeded: bool) -> None:
        with self.db.transaction():
            self.db.execute(
                "UPDATE llm_review_batches SET status=:status WHERE cycle_id=:cycle",
                {"cycle": cycle, "status": "succeeded" if succeeded else "exhausted"},
            )
