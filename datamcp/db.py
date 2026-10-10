"""The two databases: psycopg 3 pools, guarded execution with a wall-clock cancel, CSV output.

Every user query runs in a READ ONLY transaction with SET LOCAL statement_timeout; the service
also cancels the backend itself after the wall clock (a role may raise its own timeout, the
connector may not). Concurrency: MAX_CONCURRENT_QUERIES in total, MAX_QUERIES_PER_USER per e-mail.
"""

from __future__ import annotations

import asyncio
from contextlib import aclosing
import csv
import io
import json
from decimal import Decimal
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from psycopg import errors
from psycopg_pool import AsyncConnectionPool

from .config import DatabaseConfig, Settings
from .guard import GuardedQuery


class QueryError(RuntimeError):
    """A database refusal or failure, worded for the model."""


class QueryTimeout(QueryError):
    pass


class Busy(QueryError):
    pass


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple[Any, ...]]
    truncated: bool
    ms: int
    size_limited: bool = False

    def csv(self, max_chars: int) -> tuple[str, bool]:
        """CSV text capped at max_chars characters; the flag says rows were dropped for size."""
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(self.columns)
        if out.tell() > max_chars:
            return "", True
        clipped = self.size_limited
        for row in self.rows:
            mark = out.tell()
            writer.writerow(["" if value is None else _cell(value) for value in row])
            if out.tell() > max_chars:
                out.seek(mark)
                out.truncate(mark)
                clipped = True
                break
        return out.getvalue(), clipped


def _cell(value: Any) -> str:
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False,
                          default=lambda item: float(item) if isinstance(item, Decimal) else str(item))
    return str(value)


class Database:
    def __init__(self, config: DatabaseConfig, settings: Settings):
        self.config = config
        self.settings = settings
        self.contract_version = None
        self.pool = AsyncConnectionPool(
            config.conninfo(), min_size=0, max_size=settings.max_concurrent_queries + 1, open=False,
            kwargs={"autocommit": False}, timeout=15,
        )

    async def open(self) -> None:
        await self.pool.open()

    async def close(self) -> None:
        await self.pool.close()

    async def health(self, timeout: float = 5.0) -> str | None:
        """Verify the committed exposure and reader privileges, not just connectivity."""
        from .contract import verify

        try:
            async with asyncio.timeout(timeout):
                async with self.pool.connection() as conn:
                    async with conn.transaction():
                        await conn.execute("SET LOCAL statement_timeout = 3000")
                        self.contract_version = await verify(conn)
            return None
        except Exception as error:
            return f"{type(error).__name__}: {_first_line(error)}"

    async def fetch(self, sql: str, params: Any = None, timeout: float = 30.0,
                    *, max_rows: int | None = None, max_chars: int | None = None) -> QueryResult:
        return await self._execute(sql, params, timeout, max_rows or self.settings.max_rows,
                                   max_chars or self.settings.max_result_chars)

    async def run(self, query: GuardedQuery, max_rows: int) -> QueryResult:
        return await self._execute(query.sql, None, self.settings.query_timeout_s, max_rows,
                                   self.settings.max_result_chars, explain=query.kind == "explain")

    async def _execute(self, sql: str, params, timeout: float, max_rows: int,
                       max_chars: int, *, explain: bool = False) -> QueryResult:
        from .contract import verify

        started = time.monotonic()
        try:
            # Includes waiting for a connection and every fetch, not just execute().
            async with asyncio.timeout(timeout + 2):
                async with self.pool.connection() as conn:
                    await conn.set_read_only(True)
                    async with conn.transaction():
                        await conn.execute(f"SET LOCAL statement_timeout = {int(timeout * 1000)}")
                        self.contract_version = await verify(conn)
                        if explain:
                            result = await self._explain(conn, sql, max_rows, max_chars)
                        else:
                            result = await self._select(conn, sql, params, max_rows, max_chars)
        except (TimeoutError, errors.QueryCanceled) as error:
            raise QueryTimeout(f"query cancelled after {timeout} s; narrow the date range or aggregate more") from error
        except errors.InsufficientPrivilege as error:
            raise QueryError("permission denied: this connector reads only the analytics schema") from error
        except errors.ReadOnlySqlTransaction as error:
            raise QueryError("this connector is read-only") from error
        except errors.UndefinedTable as error:
            raise QueryError(f"{_first_line(error)} — use list_datasets; ask the operator to sync missing analytics views") from error
        except errors.UndefinedColumn as error:
            raise QueryError(f"{_first_line(error)} — use describe_dataset to see the columns") from error
        except errors.Error as error:
            raise QueryError(f"PostgreSQL error: {_first_line(error)}") from error
        result.ms = int((time.monotonic() - started) * 1000)
        return result

    async def _explain(self, conn, sql: str, max_rows: int, max_chars: int) -> QueryResult:
        # EXPLAIN cannot be wrapped in a SELECT or a server-side cursor. Stream plan rows
        # and bound retained output; a single plan row still arrives whole from PostgreSQL.
        result = QueryResult(["QUERY PLAN"], [], False, 0)
        used = 0
        async with conn.cursor() as cursor:
            async with aclosing(cursor.stream(sql)) as rows:
                async for row in rows:
                    if len(result.rows) >= max_rows:
                        result.truncated = True
                        break
                    line = _cell(row[0])
                    if used + len(line) + 1 > max_chars:
                        result.size_limited = True
                        break
                    result.rows.append((line,))
                    used += len(line) + 1
        return result

    async def _select(self, conn, sql: str, params, max_rows: int, max_chars: int) -> QueryResult:
        # Resolve headers without executing the result, including for empty SELECTs.
        sql = sql.rstrip().rstrip(";")
        metadata = await conn.execute(f"SELECT * FROM (\n{sql}\n) AS q LIMIT 0", params)
        columns = [c.name for c in metadata.description or []]
        if len(columns) != len(set(columns)):
            raise QueryError("give result columns distinct aliases (duplicate column names are ambiguous)")
        # Bound each row before it crosses the network. A row count alone cannot bound an
        # array_agg/string_agg or one large text value. SQL evaluation still consumes database
        # resources, so statement and operation deadlines remain necessary.
        max_bytes = max_chars * 4
        bounded = (
            f"SELECT CASE WHEN octet_length(row_to_json(q)::text) <= {max_bytes} "
            f"THEN row_to_json(q)::text ELSE NULL END "
            f"FROM (\n{sql}\n) AS q LIMIT {max_rows + 1}"
        )
        result = QueryResult(columns, [], False, 0)
        used = 0
        async with conn.cursor(name="datamcp_result") as cursor:
            await cursor.execute(bounded, params)
            # One bounded row per fetch: never buffer the complete result in libpq.
            while row := await cursor.fetchone():
                if len(result.rows) >= max_rows:
                    result.truncated = True
                    break
                payload = row[0]
                if payload is None or used + len(payload.encode("utf-8")) > max_bytes:
                    result.size_limited = True
                    break
                used += len(payload.encode("utf-8"))
                values = json.loads(payload, parse_float=Decimal)
                result.rows.append(tuple(values[column] for column in columns))
        return result


def _first_line(error: Exception) -> str:
    return str(error).strip().splitlines()[0] if str(error).strip() else type(error).__name__


class Throttle:
    """One query at a time per e-mail, MAX_CONCURRENT_QUERIES in total; refuses instead of queueing
    so the model gets an answer within its own timeout."""

    def __init__(self, settings: Settings):
        self._global = asyncio.Semaphore(settings.max_concurrent_queries)
        self._per_user_limit = settings.max_queries_per_user
        self._per_user: dict[str, int] = defaultdict(int)

    async def __call__(self, email: str):
        return _Slot(self, email)

    def _acquire(self, email: str) -> None:
        if self._per_user[email] >= self._per_user_limit:
            raise Busy("a query of yours is still running: wait for it before sending another")
        if self._global.locked():
            raise Busy("the connector is busy with other queries: retry in a few seconds")
        self._per_user[email] += 1

    def _release(self, email: str) -> None:
        self._per_user[email] -= 1


class _Slot:
    def __init__(self, throttle: Throttle, email: str):
        self.throttle, self.email = throttle, email

    async def __aenter__(self):
        self.throttle._acquire(self.email)
        try:
            await asyncio.wait_for(self.throttle._global.acquire(), timeout=0.01)
        except BaseException as error:
            self.throttle._release(self.email)
            if isinstance(error, TimeoutError):
                raise Busy("the connector is busy with other queries: retry in a few seconds") from None
            raise
        return self

    async def __aexit__(self, *exc):
        self.throttle._global.release()
        self.throttle._release(self.email)
