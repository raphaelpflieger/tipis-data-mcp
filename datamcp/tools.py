"""The MCP tools, resources and prompts. Few, well described, and worded for the model: every
refusal says what to do instead. Errors a caller can fix come back as tool errors (isError),
never as 500s."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from . import dictionary, prompts
from .audit import Audit
from .auth import current_email
from .catalog import Catalog, Dataset
from .config import Settings
from .db import Busy, Database, QueryError, QueryResult, Throttle
from .guard import GuardError, guard

DatabaseName = Literal["primary", "metrics"]
TopMetric = Literal["online_listings", "online_profiles", "median_rent_per_m2", "median_profile_budget", "members_count"]


class Services:
    def __init__(self, settings: Settings, databases: dict[str, Database], catalog: Catalog, audit: Audit):
        self.settings = settings
        self.databases = databases
        self.catalog = catalog
        self.audit = audit
        self.throttle = Throttle(settings)

    def database(self, name: str) -> Database:
        try:
            return self.databases[name]
        except KeyError:
            raise ToolError(f"unknown database {name!r}: use 'primary' or 'metrics'") from None


def register(mcp: FastMCP, services: Services) -> None:  # noqa: C901 — one registration per tool
    settings, catalog = services.settings, services.catalog

    @mcp.tool(
        name="list_datasets",
        description=(
            "List the datasets (views) available in one or both databases with their description. "
            "'primary' holds the site's data (members, listings, chats, payments, guides, geography), "
            "'metrics' the event stream and its daily counts. Start here, then describe_dataset."
        ),
    )
    async def list_datasets(database: DatabaseName | None = None) -> str:
        await catalog.refresh()
        lines = []
        for key in ([database] if database else ["primary", "metrics"]):
            datasets = catalog.list(key)
            lines.append(f"## {key} ({settings.databases[key].label}) — {len(datasets)} datasets")
            for dataset in datasets:
                lines.append(f"- **{dataset.name}**: {_first_sentence(dataset.comment)}")
            lines.append("")
        lines.append("Every dataset is read as analytics.<name> (or just <name>) with the query tool.")
        services.audit.log("list_datasets", email=current_email(), database=database)
        return "\n".join(lines)

    @mcp.tool(
        name="describe_dataset",
        description=(
            "The columns of one dataset: name, type and meaning. Optional categorical values come "
            "with sample counts. Always call it before writing SQL against a dataset."
        ),
    )
    async def describe_dataset(
        database: DatabaseName,
        dataset: Annotated[str, Field(description="Dataset name, e.g. listings or analytics.listings")],
        include_values: Annotated[bool, Field(description="Sample up to eight categorical columns (5000 rows each); counts are sample counts")] = False,
    ) -> str:
        await catalog.refresh()
        found = catalog.get(database, dataset)
        if not found:
            raise ToolError(f"no dataset {dataset!r} in {database}: call list_datasets for the names")
        lines = [f"# analytics.{found.name} ({database}, {found.kind})", found.comment or "", "", "| column | type | meaning |", "|---|---|---|"]
        for column in found.columns:
            lines.append(f"| {column.name} | {column.type} | {column.comment or ''} |")
        if include_values:
            lines.append("")
            lines.append("## Sample values (up to eight columns, first 5000 rows each; counts are not population totals)")
            for column in [c for c in found.columns if c.type.startswith(("text", "character varying", "boolean", "smallint"))][:8]:
                values = await catalog.values(found, column)
                if values:
                    shown = ", ".join(f"{value}: {count}" for value, count in values[:25])
                    lines.append(f"- {column.name}: {shown}")
        lines.append("")
        lines.append(f"Example: SELECT count(*) FROM analytics.{found.name}")
        services.audit.log("describe_dataset", email=current_email(), database=database, dataset=found.name)
        return "\n".join(lines)

    @mcp.tool(
        name="query",
        description=(
            "Run one read-only SELECT on one database and get CSV back. Only the analytics schema is "
            "readable; aggregates are what you want (GROUP BY, count, avg, percentile_cont). Limits: "
            f"{settings.query_timeout_s} s, {settings.max_rows} rows, {settings.max_result_chars} characters "
            "— aggregate or filter on a date range when a result is cut. Never publish a single row "
            "about a member or a listing."
        ),
    )
    async def query(
        database: DatabaseName,
        sql: Annotated[str, Field(description="One SELECT statement (EXPLAIN goes to explain_query)")],
        max_rows: Annotated[int, Field(ge=1, description="Row cap (the server caps it at its own maximum)")] = 500,
    ) -> str:
        email = current_email() or "?"
        max_rows = min(int(max_rows), settings.max_rows)
        try:
            guarded = guard(sql)
        except GuardError as error:
            services.audit.log("query_refused", email=email, database=database, sql=sql, error=str(error))
            raise ToolError(f"query refused: {error}") from None
        if guarded.kind != "select":
            raise ToolError("use explain_query for EXPLAIN")
        result = await _run(services, database, guarded, max_rows, email)
        text, clipped = result.csv(settings.max_result_chars)
        notes = [f"{len(result.rows)} rows, {result.ms} ms"]
        if result.truncated:
            notes.append(f"cut at max_rows={max_rows}: aggregate more or raise max_rows (≤ {settings.max_rows})")
        if clipped:
            notes.append(f"cut at {settings.max_result_chars} characters: select fewer columns or aggregate more")
        return f"{text}\n\n[{'; '.join(notes)}]"

    @mcp.tool(
        name="explain_query",
        description="The PostgreSQL plan of a SELECT (EXPLAIN without ANALYZE): use it before a heavy query on the 200 M-row events table.",
    )
    async def explain_query(database: DatabaseName, sql: str) -> str:
        email = current_email() or "?"
        try:
            guarded = guard(sql if sql.strip().upper().startswith("EXPLAIN") else f"EXPLAIN {sql}")
        except GuardError as error:
            raise ToolError(f"query refused: {error}") from None
        result = await _run(services, database, guarded, min(500, settings.max_rows), email)
        text = "\n".join(row[0] for row in result.rows)
        if result.truncated or result.size_limited:
            text += "\n[plan cut at the output limit; simplify the query]"
        return text

    @mcp.tool(
        name="city_figures",
        description=(
            "The figures the site shows on a city page today (listings and tenant profiles online, "
            "median rent per m² with its sample size, tenant budget, members, 12-month trend). "
            "Quote these in an article: they cannot contradict the site. City by name (Lyon) or path (/fr/auvergne-rhone-alpes/lyon)."
        ),
    )
    async def city_figures(city: str) -> str:
        sql = """
            SELECT canonical_path, area_name, country_code, currency_code, computed_on,
                   online_listings, online_listings_date, online_profiles, online_profiles_date,
                   median_rent_per_m2, median_rent_samples, median_rent_computed_on,
                   median_profile_budget, median_profile_budget_samples, members_count, members_date, trend
            FROM analytics.city_figures
            WHERE lower(area_name) = lower(%(city)s) OR canonical_path = %(city)s OR canonical_path LIKE %(suffix)s
            ORDER BY online_listings DESC NULLS LAST LIMIT 5
        """
        result = await _fetch(services, "primary", sql, {"city": city.strip(), "suffix": f"%/{_slug(city)}"})
        if not result.rows:
            raise ToolError(f"no city figures for {city!r}: the city has no hub or fewer than 30 listings online; "
                            "try query on analytics.viewport_daily_stats or analytics.viewports")
        text, _ = result.csv(settings.max_result_chars)
        services.audit.log("city_figures", email=current_email(), city=city)
        return f"{text}\n\n[as of computed_on; a null median means fewer than 30 listings over 365 days]"

    @mcp.tool(
        name="rent_trend",
        description=(
            "Day-by-day history of a city's figures (median rent per m², sample size, listings online) "
            "from the daily snapshots, plus the 12-month trend of listings online. City by name or path."
        ),
    )
    async def rent_trend(city: str, months: Annotated[int, Field(ge=1, le=60)] = 12) -> str:
        sql = """
            SELECT computed_on, area_name, median_rent_per_m2, median_rent_samples, online_listings, online_profiles
            FROM analytics.city_figures_snapshots
            WHERE (lower(area_name) = lower(%(city)s) OR canonical_path = %(city)s OR canonical_path LIKE %(suffix)s)
              AND computed_on >= CURRENT_DATE - (%(months)s || ' months')::interval
            ORDER BY computed_on
        """
        params = {"city": city.strip(), "suffix": f"%/{_slug(city)}", "months": int(months)}
        result = await _fetch(services, "primary", sql, params)
        trend = await _fetch(services, "primary",
                             "SELECT trend FROM analytics.city_figures WHERE lower(area_name) = lower(%(city)s) "
                             "OR canonical_path = %(city)s OR canonical_path LIKE %(suffix)s LIMIT 1",
                             {"city": city.strip(), "suffix": f"%/{_slug(city)}"})
        text, _ = result.csv(settings.max_result_chars)
        trend_text = trend.rows[0][0] if trend.rows else None
        services.audit.log("rent_trend", email=current_email(), city=city, months=months)
        return f"{text}\n\n12-month trend of listings online (monthly averages): {trend_text}\n[{len(result.rows)} daily snapshots]"

    @mcp.tool(
        name="top_cities",
        description="Rank the cities of a country by one of today's figures (online_listings, online_profiles, median_rent_per_m2, median_profile_budget, members_count).",
    )
    async def top_cities(metric: TopMetric = "online_listings", country: str = "fr",
                         limit: Annotated[int, Field(ge=1, le=100)] = 20) -> str:
        sql = f"""
            SELECT area_name, canonical_path, {metric}, median_rent_samples, online_listings, computed_on
            FROM analytics.city_figures
            WHERE country_code = %(country)s AND {metric} IS NOT NULL
            ORDER BY {metric} DESC NULLS LAST LIMIT %(limit)s
        """
        result = await _fetch(services, "primary", sql, {"country": country.strip().lower(), "limit": int(limit)})
        text, _ = result.csv(settings.max_result_chars)
        services.audit.log("top_cities", email=current_email(), metric=metric, country=country)
        return f"{text}\n\n[{len(result.rows)} cities; medians rest on median_rent_samples listings over 365 days]"

    @mcp.tool(
        name="traffic_series",
        description=(
            "Daily visitors, visits and pageviews of an area's pages (Plausible), by acquisition source, "
            "for a viewport path (/fr/auvergne-rhone-alpes/lyon), or all tracked viewport pages when empty. "
            "Visitors and visits are sums of page-level counts, not unique people or site-wide sessions."
        ),
    )
    async def traffic_series(viewport_path: str = "", months: Annotated[int, Field(ge=1, le=36)] = 6) -> str:
        if viewport_path.strip():
            sql = """
                SELECT s.date, s.visitors, s.visits, s.pageviews, s.visitors_seo_google, s.visitors_direct,
                       s.visitors_email, s.visitors_paid, s.visitors_ai, s.bounce_rate, s.visit_duration_s
                FROM analytics.viewport_plausible_daily_stats s
                JOIN analytics.viewports v ON v.id = s.viewport_id
                WHERE v.canonical_path = %(path)s AND s.kind = 'all' AND s.subtype = 'all'
                  AND s.date >= CURRENT_DATE - (%(months)s || ' months')::interval
                ORDER BY s.date
            """
        else:
            sql = """
                SELECT date, sum(visitors) AS visitors, sum(visits) AS visits, sum(pageviews) AS pageviews,
                       sum(visitors_seo_google) AS visitors_seo_google, sum(visitors_direct) AS visitors_direct,
                       sum(visitors_email) AS visitors_email, sum(visitors_paid) AS visitors_paid, sum(visitors_ai) AS visitors_ai
                FROM analytics.viewport_plausible_daily_stats
                WHERE kind = 'all' AND subtype = 'all' AND date >= CURRENT_DATE - (%(months)s || ' months')::interval
                GROUP BY date ORDER BY date
            """
        result = await _fetch(services, "primary", sql, {"path": viewport_path.strip(), "months": int(months)})
        text, clipped = result.csv(settings.max_result_chars)
        services.audit.log("traffic_series", email=current_email(), viewport_path=viewport_path, months=months)
        return (f"{text}\n\n[{len(result.rows)} days{'; cut for size' if clipped else ''}; "
                "tracked viewport pages only; visitors and visits can count the same person/session across pages]")

    @mcp.tool(
        name="feed_volumes",
        description="Monthly import volumes of the partner feeds (listings published, updated, removed, average online) by feed identity and country.",
    )
    async def feed_volumes(months: Annotated[int, Field(ge=1, le=36)] = 12) -> str:
        sql = """
            SELECT m.month, f.identity, f.standard, f.country, count(DISTINCT m.xml_feed_id) AS feeds,
                   sum(m.published) AS published, sum(m.updated) AS updated, sum(m.removed) AS removed,
                   sum(m.avg_online) AS avg_online
            FROM analytics.monthly_feed_volumes m
            JOIN analytics.xml_feeds f ON f.id = m.xml_feed_id
            WHERE m.month >= date_trunc('month', CURRENT_DATE) - (%(months)s || ' months')::interval
            GROUP BY 1, 2, 3, 4 ORDER BY 1, 2
        """
        result = await _fetch(services, "primary", sql, {"months": int(months)})
        text, _ = result.csv(settings.max_result_chars)
        services.audit.log("feed_volumes", email=current_email(), months=months)
        return f"{text}\n\n[{len(result.rows)} rows]"

    @mcp.resource("data://dictionary", name="data-dictionary", mime_type="text/markdown",
                  description="Every dataset and column with its meaning, plus the domain rules (what 'online' means, UTC, brands, event catalogue, thresholds, methodology line).")
    async def data_dictionary() -> str:
        await catalog.refresh()
        services.audit.log("dictionary", email=current_email())
        return dictionary.render(catalog)

    @mcp.prompt(name="rapport_ville", description="Rapport chiffré sur le marché d'une ville, pour un article du guide")
    def rapport_ville(ville: str) -> str:
        return prompts.rapport_ville(ville)

    @mcp.prompt(name="barometre_loyers", description="Baromètre des loyers en colocation pour une zone (France par défaut)")
    def barometre_loyers(zone: str = "France") -> str:
        return prompts.barometre_loyers(zone)

    @mcp.prompt(name="fiche_chiffres_article", description="Fiche de chiffres sourcés pour un article sur un sujet")
    def fiche_chiffres_article(sujet: str) -> str:
        return prompts.fiche_chiffres_article(sujet)


async def _run(services: Services, database: str, guarded, max_rows: int, email: str) -> QueryResult:
    db = services.database(database)
    try:
        result = await db.run(guarded, max_rows)
    except Busy as error:
        raise ToolError(str(error)) from None
    except QueryError as error:
        services.audit.log("query_failed", email=email, database=database, sql=guarded.sql, error=str(error))
        raise ToolError(str(error)) from None
    services.audit.log("query", email=email, database=database, sql=guarded.sql, rows=len(result.rows),
                       truncated=result.truncated, ms=result.ms)
    return result


async def _fetch(services: Services, database: str, sql: str, params: dict) -> QueryResult:
    try:
        result = await services.database(database).fetch(sql, params, timeout=services.settings.query_timeout_s)
        if result.truncated or result.size_limited:
            raise QueryError("result exceeds the connector limits; reduce the period or use query to aggregate")
        return result
    except Exception as error:  # noqa: BLE001 — worded for the model
        services.audit.log("query_failed", email=current_email(), database=database, sql=sql, error=str(error))
        raise ToolError(f"the built-in query failed: {str(error).splitlines()[0]}") from None


def _first_sentence(text: str | None) -> str:
    if not text:
        return ""
    head = text.split(". ")[0]
    return head if len(head) < 160 else head[:157] + "…"


def _slug(city: str) -> str:
    import re
    import unicodedata

    normalized = unicodedata.normalize("NFKD", city).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")


def today() -> date:
    return date.today()
