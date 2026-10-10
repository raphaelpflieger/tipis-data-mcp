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

from datamcp.contract import DEFINITIONS_SQL

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
        assert "## Domain rules" in listing and "auto_validated" in listing  # the model's only copy in claude.ai

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


# the city views as tipis-web builds them, with three cities over two days, plus Lyon's rents for rent_trend
CITY_WORLD_SQL = """
DROP TABLE IF EXISTS public.city_snapshots CASCADE;
CREATE TABLE public.city_snapshots (
  canonical_path text, area_name text, country_code text, currency_code text, computed_on date,
  online_listings integer, online_listings_date date, online_profiles integer, online_profiles_date date,
  median_rent_per_m2 numeric, median_rent_samples integer, median_rent_computed_on date,
  median_profile_budget integer, median_profile_budget_samples integer, members_count integer, members_date date,
  trend jsonb);
INSERT INTO public.city_snapshots
SELECT path, name, 'fr', 'EUR', day, listings, day - 1, 100, day - 1, rent, samples, DATE '2026-09-20',
       600, 800, 9000, DATE '2026-09-20', '[{"month": "2025-10-01", "average": 421}, {"month": "2025-11-01", "average": 540}]'
FROM (VALUES ('/fr/auvergne-rhone-alpes/lyon', 'Lyon', 410, 42.86, 1897),
             ('/fr/ile-de-france/paris', 'Paris', 466, NULL, 12),
             ('/fr/nouvelle-aquitaine/bordeaux', 'Bordeaux', 316, 50.34, 1298)) AS c(path, name, listings, rent, samples),
     (VALUES (DATE '2026-10-09'), (DATE '2026-10-10')) AS d(day);
CREATE VIEW analytics.city_figures_snapshots AS SELECT * FROM public.city_snapshots;
DROP TABLE IF EXISTS public.areas, public.ads, public.ad_rooms, public.area_months CASCADE;
CREATE TABLE public.areas (canonical_path text, city jsonb, data_type text, historical boolean, online_listings_count integer);
INSERT INTO public.areas VALUES ('/fr/auvergne-rhone-alpes/lyon', '{"fr": "Lyon"}', 'city', false, 410),
                                ('/fr/auvergne-rhone-alpes/lyon/1er-arrondissement', '{"fr": "Lyon"}', 'district', false, 500);
CREATE TABLE public.ads (id integer, utm_geotag text, blacklisted boolean, from_xml_feed boolean, total_rooms_count integer,
                         listing_type text, cost_total_rent integer, lodging_surface double precision,
                         cost_periodicity text, created_at timestamp);
-- 40 organic rooms at 40 €/m² this month, 10 at 50 €/m² last month (too few), 5 feed rooms at 90 €/m² (ignored)
INSERT INTO public.ads
SELECT g, 'fr_auvergne-rhone-alpes_lyon', false, g > 50, 1, 'flatshare', 0, 0, NULL,
       CASE WHEN g <= 40 OR g > 50 THEN date_trunc('month', now()) + interval '1 hour'
            ELSE date_trunc('month', now()) - interval '1 day' END
FROM generate_series(1, 55) g;
CREATE TABLE public.ad_rooms (listing_id integer, cost_total_rent integer, surface double precision, cost_periodicity text);
INSERT INTO public.ad_rooms SELECT id, CASE WHEN id <= 40 THEN 400 WHEN id <= 50 THEN 500 ELSE 900 END, 10, 'month' FROM public.ads;
CREATE TABLE public.area_months (canonical_path text, month date, avg_online_listings integer);
INSERT INTO public.area_months VALUES ('/fr/auvergne-rhone-alpes/lyon', (date_trunc('month', now()) - interval '1 month')::date, 400);
CREATE VIEW analytics.viewports AS SELECT * FROM public.areas;
CREATE VIEW analytics.listings AS SELECT * FROM public.ads;
CREATE VIEW analytics.rooms AS SELECT * FROM public.ad_rooms;
CREATE VIEW analytics.monthly_city_listings AS SELECT * FROM public.area_months;
CREATE VIEW analytics.city_figures AS SELECT s.* FROM analytics.city_figures_snapshots s
  WHERE s.computed_on = (SELECT max(computed_on) FROM analytics.city_figures_snapshots);
GRANT SELECT ON ALL TABLES IN SCHEMA analytics TO tipis_analytics;
""" + f"UPDATE analytics._contract SET definitions = ({DEFINITIONS_SQL}) WHERE id = 1;"


async def test_city_tools_answer_with_their_defaults(served: Served, prepared_db):
    with changed_database(prepared_db, CITY_WORLD_SQL):
        async with client(served, "alice-token") as c:
            ranked = text_of(await c.call_tool("top_cities", {"limit": 2}))  # the default metric is also a fixed column
            assert ranked.startswith("area_name,canonical_path,online_listings,online_listings_date,computed_on\nParis,")
            assert "\nLyon," in ranked and "Bordeaux" not in ranked
            rents = text_of(await c.call_tool("top_cities", {"metric": "median_rent_per_m2"}))
            assert "median_rent_per_m2,median_rent_samples" in rents and "Paris" not in rents  # no median, not ranked
            lyon = text_of(await c.call_tool("city_figures", {"city": "lyon"}))
            assert ",410," in lyon and "2025-10: 421; 2025-11: 540" in lyon and '""month""' not in lyon
            rents = text_of(await c.call_tool("rent_trend", {"city": "lyon", "months": 2}))
            lines = rents.splitlines()
            assert lines[0] == "month,median_rent_per_m2,rent_samples,avg_online_listings"
            assert lines[1].endswith(",,10,400") and lines[2].endswith(",40.00,40,")  # under 30 rents: no median
            assert "(/fr/auvergne-rhone-alpes/lyon)" in rents and "42.86 EUR/m² over 1897 rents" in rents
            with pytest.raises(ToolError, match="no city"):
                await c.call_tool("rent_trend", {"city": "Atlantis"})


async def test_every_tool_is_read_only_titled_and_plain_text(served: Served):
    async with client(served, "alice-token") as c:
        tools = await c.list_tools()
    assert len(tools) == 9
    for tool in tools:
        assert tool.title and tool.title != tool.name.replace("_", " ").title(), tool.name  # a French title, not the auto one
        assert tool.annotations.read_only_hint is True and tool.annotations.destructive_hint is False, tool.name
        assert tool.annotations.open_world_hint is False and tool.annotations.title == tool.title, tool.name
        assert tool.output_schema is None, tool.name  # no structuredContent copy of the text


def test_first_sentence_keeps_abbreviations():
    from datamcp.tools import _first_sentence

    assert _first_sentence("Monthly completed revenue (excl. and incl. VAT, converted to EUR) by type. Refunds excluded") == \
        "Monthly completed revenue (excl. and incl. VAT, converted to EUR) by type"
    assert _first_sentence("Daily state (MRR ledger): plan, MRR excl. VAT. More") == "Daily state (MRR ledger): plan, MRR excl. VAT"
    assert _first_sentence("Accommodation ads. Online = published") == "Accommodation ads"


def test_instructions_fit_what_chatgpt_reads():
    from datamcp.server import INSTRUCTIONS

    assert len(INSTRUCTIONS) <= 512 and "data://" not in INSTRUCTIONS  # claude.ai cannot read a resource itself
