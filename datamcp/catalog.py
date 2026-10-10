"""Schema metadata and optional bounded samples of categorical values.

Descriptions are read without scanning the underlying data. Sample counts are explicitly
not population statistics; the operation middleware applies the same deadline and slot
limits as every other tool.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from .db import Database, QueryError

CATALOG_SQL = """
SELECT c.relname AS name,
       CASE c.relkind WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view' ELSE 'table' END AS kind,
       obj_description(c.oid, 'pg_class') AS comment,
       a.attname AS column_name,
       format_type(a.atttypid, a.atttypmod) AS column_type,
       col_description(c.oid, a.attnum) AS column_comment,
       a.attnum
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
WHERE n.nspname = 'analytics' AND c.relkind IN ('v', 'm', 'r') AND c.relname <> '_contract'
ORDER BY c.relname, a.attnum
"""
CATEGORICAL_TYPES = ("text", "character varying", "boolean", "smallint", "integer", "date")
MAX_DISTINCT = 40


@dataclass
class Column:
    name: str
    type: str
    comment: str | None


@dataclass
class Dataset:
    database: str
    name: str
    kind: str
    comment: str | None
    columns: list[Column] = field(default_factory=list)

    @property
    def qualified(self) -> str:
        return f"analytics.{self.name}"


class Catalog:
    def __init__(self, databases: dict[str, Database], ttl_s: int = 3600):
        self.databases = databases
        self.ttl_s = ttl_s
        self._datasets: dict[str, dict[str, Dataset]] = {key: {} for key in databases}
        self._loaded_at: float = 0.0
        self._values: dict[tuple[str, str, str], tuple[float, list[tuple[str, int]] | None]] = {}
        self._lock = asyncio.Lock()
        self._versions = {}

    async def refresh(self, force: bool = False) -> None:
        # Cached metadata/value samples must not outlive the policy that authorized them.
        for database in self.databases.values():
            problem = await database.health(timeout=5)
            if problem:
                raise QueryError(problem)
        versions = {key: database.contract_version for key, database in self.databases.items()}
        if versions != self._versions:
            self._loaded_at = 0
            self._values.clear()
        if not force and self._loaded_at and time.monotonic() - self._loaded_at < self.ttl_s:
            return
        async with self._lock:
            for key, database in self.databases.items():
                result = await database.fetch(CATALOG_SQL, timeout=5, max_rows=20000, max_chars=4_000_000)
                if result.truncated or result.size_limited:
                    raise RuntimeError("analytics catalogue exceeds the metadata limit")
                datasets: dict[str, Dataset] = {}
                for name, kind, comment, column, ctype, ccomment, _ in result.rows:
                    dataset = datasets.setdefault(name, Dataset(database=key, name=name, kind=kind, comment=comment))
                    dataset.columns.append(Column(name=column, type=ctype, comment=ccomment))
                self._datasets[key] = datasets
            self._loaded_at = time.monotonic()
            self._versions = versions

    def list(self, database: str | None = None) -> list[Dataset]:
        keys = [database] if database else list(self._datasets)
        return [dataset for key in keys for dataset in self._datasets.get(key, {}).values()]

    def get(self, database: str, name: str) -> Dataset | None:
        return self._datasets.get(database, {}).get(name.removeprefix("analytics."))

    async def values(self, dataset: Dataset, column: Column) -> list[tuple[str, int]] | None:
        """(value, count) of a small categorical column, most frequent first; None when the
        column has more than MAX_DISTINCT values or is not categorical."""
        if not column.type.startswith(CATEGORICAL_TYPES):
            return None
        key = (dataset.database, dataset.name, column.name)
        cached = self._values.get(key)
        if cached and time.monotonic() - cached[0] < self.ttl_s:
            return cached[1]
        sql = (
            f'SELECT "{column.name}"::text AS value, count(*) AS n '
            f'FROM (SELECT "{column.name}" FROM {dataset.qualified} LIMIT 5000) AS sample '
            f'GROUP BY 1 ORDER BY 2 DESC LIMIT {MAX_DISTINCT + 1}'
        )
        try:
            result = await self.databases[dataset.database].fetch(sql, timeout=3)
        except Exception:  # noqa: BLE001 — a slow column simply has no catalogue
            values = None
        else:
            values = None if result.size_limited or len(result.rows) > MAX_DISTINCT else [(str(v), int(n)) for v, n in result.rows]
        self._values[key] = (time.monotonic(), values)
        return values
