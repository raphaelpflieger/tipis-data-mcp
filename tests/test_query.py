"""The tools over HTTP with a static bearer: CSV out, refusals worded for the model, the row and
size caps, the wall-clock cancel, the allowlist middleware, the kill switch, /healthz."""

from __future__ import annotations

import json
import asyncio
from contextlib import contextmanager

import httpx
import pytest
import psycopg
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError

from .conftest import ALICE, Served, SCHEMA_SQL, make_settings, serve


def client(served: Served, token: str) -> Client:
    return Client(StreamableHttpTransport(served.url + "/mcp", headers={"Authorization": f"Bearer {token}"}))


def text_of(result) -> str:
    return "".join(getattr(block, "text", "") for block in result.content)


async def test_list_and_describe_and_query(served: Served):
    async with client(served, "alice-token") as c:
        tools = {tool.name for tool in await c.list_tools()}
        assert {"list_datasets", "describe_dataset", "query", "explain_query", "city_figures"} <= tools

        listing = text_of(await c.call_tool("list_datasets", {"database": "primary"}))
        assert "**things**" in listing and "Things: one row per thing" in listing

        described = text_of(await c.call_tool("describe_dataset", {"database": "primary", "dataset": "things", "include_values": True}))
        assert "| kind | text | flatshare, rental or sublet |" in described
        assert "kind: flatshare: 60, rental: 40, sublet: 20" in described

        csv = text_of(await c.call_tool("query", {"database": "primary", "sql": "SELECT kind, count(*) AS n FROM things GROUP BY 1 ORDER BY 1"}))
        assert csv.startswith("kind,n\nflatshare,60\nrental,40\nsublet,20\n")
        assert "3 rows" in csv


async def test_refusals_are_readable(served: Served):
    async with client(served, "alice-token") as c:
        for sql, expected in [
            ("UPDATE things SET amount = 0", "only SELECT"),
            ("SET statement_timeout = 0", "only SELECT"),
            ("SELECT pg_sleep(5)", "not allowed"),
            ("SELECT 1; SELECT 2", "exactly one statement"),
            ("SELECT * FROM public.secret", "not readable"),
            ("SELECT * FROM secret", "use list_datasets"),  # public.* is not on the role's search_path
            ("SELECT * FROM nothing_here", "use list_datasets"),
            ("SELECT nope FROM things", "describe_dataset"),
        ]:
            with pytest.raises(ToolError) as raised:
                await c.call_tool("query", {"database": "primary", "sql": sql})
            assert expected.lower() in str(raised.value).lower(), (sql, str(raised.value))


async def test_row_and_size_caps(served: Served):
    async with client(served, "alice-token") as c:
        out = text_of(await c.call_tool("query", {"database": "primary", "sql": "SELECT id FROM things ORDER BY id", "max_rows": 10}))
        assert out.count("\n") >= 11 and "cut at max_rows=10" in out
        out = text_of(await c.call_tool("query", {"database": "primary", "sql": "SELECT repeat('x', 500) FROM things"}))
        assert "cut at 4000 characters" in out
        wide = "SELECT " + ", ".join(f"{i} AS {'x' * 60}{i}" for i in range(80))
        out = text_of(await c.call_tool("query", {"database": "primary", "sql": wide}))
        assert "cut at 4000 characters" in out and len(out) < 4100


async def test_one_oversized_value_is_rejected_before_transfer(served: Served):
    async with client(served, "alice-token") as c:
        out = text_of(await c.call_tool("query", {"database": "primary", "sql": "SELECT repeat('x', 1000000) AS large"}))
        assert "0 rows" in out and "cut at 4000 characters" in out
        assert len(out) < 1000
        out = text_of(await c.call_tool("query", {"database": "primary", "sql": "SELECT 123.1234567890123456789::numeric AS precise"}))
        assert "123.1234567890123456789" in out
        out = text_of(await c.call_tool("query", {"database": "primary", "sql": "SELECT jsonb_build_object('amount', 12.5) AS details"}))
        assert '""amount"": 12.5' in out


async def test_schema_description_does_not_profile_data_by_default(served: Served, monkeypatch):
    async def forbidden(*args):
        raise AssertionError("schema-only requests must not scan data")
    monkeypatch.setattr(served.services.catalog, "values", forbidden)
    async with client(served, "alice-token") as c:
        out = text_of(await c.call_tool("describe_dataset", {"database": "primary", "dataset": "things"}))
        assert "| kind | text |" in out and "Sample values" not in out


async def test_all_data_tools_and_resources_share_the_account_slot(served: Served):
    async with client(served, "alice-token") as c:
        async with await served.services.throttle(ALICE):
            for name, args in [("list_datasets", {}), ("describe_dataset", {"database": "primary", "dataset": "things"}),
                               ("top_cities", {}), ("query", {"database": "primary", "sql": "SELECT 1"})]:
                with pytest.raises(ToolError, match="still running"):
                    await c.call_tool(name, args)
            with pytest.raises(Exception, match="still running"):
                await c.read_resource("data://dictionary")
        assert "1 rows" in text_of(await c.call_tool("query", {"database": "primary", "sql": "SELECT 1"}))


async def test_description_has_one_deadline_for_all_columns(served: Served, monkeypatch):
    async def slow(*args):
        await asyncio.sleep(20)
    monkeypatch.setattr(served.services.catalog, "values", slow)
    async with client(served, "alice-token") as c:
        with pytest.raises(ToolError, match="cancelled"):
            await c.call_tool("describe_dataset", {"database": "primary", "dataset": "things", "include_values": True})
        assert "1 rows" in text_of(await c.call_tool("query", {"database": "primary", "sql": "SELECT 1"}))


@contextmanager
def changed_database(dsn, sql):
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(sql)
        try:
            yield
        finally:
            connection.execute(SCHEMA_SQL)


@pytest.mark.parametrize("change", [
    "UPDATE analytics._contract SET applied = NULL",
    "DROP VIEW analytics.monthly_things",
    "ALTER VIEW analytics.things RENAME COLUMN amount TO renamed_amount",
    "GRANT SELECT ON public.secret TO tipis_analytics",
    "GRANT SELECT (email) ON public.secret TO tipis_analytics",
    "GRANT UPDATE ON analytics._contract TO tipis_analytics",
    "GRANT UPDATE (applied) ON analytics._contract TO tipis_analytics",
    "CREATE VIEW analytics.extra AS SELECT email FROM public.secret",
])
async def test_stale_contract_or_bad_permissions_close_data_access(served: Served, prepared_db, change):
    async with client(served, "alice-token") as c:
        with changed_database(prepared_db, change):
            async with httpx.AsyncClient() as http:
                health = await http.get(served.url + "/healthz")
                assert health.status_code == 503
            with pytest.raises(ToolError, match="contract|permissions"):
                await c.call_tool("query", {"database": "primary", "sql": "SELECT 1"})
            with pytest.raises(ToolError, match="contract|permissions"):
                await c.call_tool("describe_dataset", {"database": "primary", "dataset": "things", "include_values": True})


async def test_private_reader_can_query_pseudonymous_rows(served: Served):
    async with client(served, "alice-token") as c:
        out = text_of(await c.call_tool("query", {"database": "primary", "sql": "SELECT id, amount FROM things WHERE id = 1"}))
        assert out.startswith("id,amount\n1,10\n")


def test_reader_permissions_hold_without_the_sql_guard(settings):
    with psycopg.connect(settings.databases["primary"].conninfo(), autocommit=True) as connection:
        # Disable the session default to prove the grants themselves enforce read-only access.
        connection.execute("SET default_transaction_read_only = off")
        assert connection.execute("SELECT amount FROM analytics.things WHERE id = 1").fetchone() == (10,)
        for sql in ["SELECT email FROM public.secret", "UPDATE analytics.things SET amount = 0",
                    "UPDATE analytics._contract SET applied = 'forged'"]:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                connection.execute(sql)


async def test_traffic_tool_describes_non_additive_metrics(served: Served, monkeypatch):
    from datamcp.db import QueryResult

    async def traffic(*args, **kwargs):
        return QueryResult(["date", "visitors"], [("2026-01-01", 2)], False, 0)
    monkeypatch.setattr(served.services.database("primary"), "fetch", traffic)
    async with client(served, "alice-token") as c:
        out = text_of(await c.call_tool("traffic_series", {}))
        assert "tracked viewport pages only" in out and "same person/session" in out


async def test_wall_clock_cancel(served: Served):
    async with client(served, "alice-token") as c:
        with pytest.raises(ToolError) as raised:
            await c.call_tool("query", {"database": "primary", "sql": "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) SELECT count(*) FROM r"})
        assert "cancelled" in str(raised.value)


async def test_explain(served: Served):
    async with client(served, "alice-token") as c:
        plan = text_of(await c.call_tool("explain_query", {"database": "primary", "sql": "SELECT * FROM things"}))
        assert "Seq Scan" in plan or "Scan" in plan
        with pytest.raises(ToolError):
            await c.call_tool("explain_query", {"database": "primary", "sql": "EXPLAIN ANALYZE SELECT 1"})


async def test_large_plan_is_clipped_and_connection_remains_usable(served: Served):
    async with client(served, "alice-token") as c:
        plan = text_of(await c.call_tool("explain_query", {
            "database": "primary", "sql": "EXPLAIN (VERBOSE) SELECT repeat('x', 10000) AS large",
        }))
        assert "plan cut at the output limit" in plan and len(plan) < 4100
        assert "1 rows" in text_of(await c.call_tool("query", {"database": "primary", "sql": "SELECT 1"}))


async def test_allowlist_middleware(served: Served):
    with pytest.raises(Exception) as raised:  # a valid token of an account not in the list
        async with client(served, "bob-token") as c:
            await c.list_tools()
    assert "not allowed" in str(raised.value)
    with pytest.raises(Exception):
        async with client(served, "unverified-token") as c:
            await c.list_tools()
    # removing the line revokes at the next call, no restart
    async with client(served, "alice-token") as c:
        await c.list_tools()
        served.settings.allowlist_file.write_text("nobody@example.org\n", encoding="utf-8")
        with pytest.raises(Exception) as raised:
            await c.call_tool("query", {"database": "primary", "sql": "SELECT 1"})
        assert "not allowed" in str(raised.value)


async def test_unauthenticated_gets_401_with_metadata_pointer(served: Served):
    async with httpx.AsyncClient() as http:
        response = await http.post(served.url + "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                   headers={"Accept": "application/json, text/event-stream"})
        assert response.status_code == 401
        assert "resource_metadata=" in response.headers.get("www-authenticate", "")


async def test_healthz(served: Served):
    async with httpx.AsyncClient() as http:
        response = await http.get(served.url + "/healthz")
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is True and body["databases"] == {"primary": "ok", "metrics": "ok"}
        assert body["allowed_accounts"] == 1


async def test_audit_log_lines(served: Served):
    async with client(served, "alice-token") as c:
        await c.call_tool("query", {"database": "metrics", "sql": "SELECT count(*) FROM things"})
    lines = [json.loads(line) for line in (served.settings.audit_dir / "datamcp.jsonl").read_text().splitlines()]
    entry = [line for line in lines if line["kind"] == "query"][-1]
    assert entry["email"] == ALICE and entry["database"] == "metrics" and entry["rows"] == 1
    assert "sql_sha256" in entry and "kind,n" not in json.dumps(entry)


async def test_kill_switch(prepared_db, tmp_path, secrets_dir):
    from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

    from datamcp.server import build_server
    from .conftest import STATIC_TOKENS

    settings = make_settings(prepared_db, tmp_path, secrets_dir, enabled=False)
    mcp, _ = build_server(settings, auth=StaticTokenVerifier(tokens=STATIC_TOKENS))
    async for url in serve(mcp, settings):
        async with Client(StreamableHttpTransport(url + "/mcp", headers={"Authorization": "Bearer alice-token"})) as c:
            assert await c.list_tools()  # listing still works
            with pytest.raises(ToolError) as raised:
                await c.call_tool("query", {"database": "primary", "sql": "SELECT 1"})
            assert "disabled" in str(raised.value)
