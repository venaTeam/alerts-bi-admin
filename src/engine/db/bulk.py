"""Bounded parameterized multi-row inserts within the caller's transaction."""

from collections.abc import Mapping, Sequence
from typing import Any

from alerts_bi_shared.db.connection import Database, quote_identifier


def insert_rows(
    db: Database, table: str, columns: Sequence[str], rows: Sequence[Mapping[str, Any]]
) -> None:
    if not rows:
        return
    if not columns or len(columns) > 2000:
        raise ValueError("Insert columns must fit the SQL Server parameter limit")
    prefix = f"INSERT INTO {quote_identifier(table)} ({','.join(map(quote_identifier, columns))}) VALUES "
    # Leave headroom below SQL Server's 2100 parameters and 1000 VALUES-row limits.
    size = min(1000, 2000 // len(columns))
    for start in range(0, len(rows), size):
        params: dict[str, Any] = {}
        values = []
        for i, row in enumerate(rows[start : start + size]):
            names = [f"p{i}_{j}" for j in range(len(columns))]
            params.update(
                (name, row.get(column)) for name, column in zip(names, columns, strict=True)
            )
            values.append("(" + ",".join(":" + name for name in names) + ")")
        db.execute(prefix + ",".join(values), params)
