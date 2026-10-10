"""The MCP tools, resources and prompts. Few, well described, and worded for the model: every
refusal says what to do instead. Errors a caller can fix come back as tool errors (isError),
never as 500s."""

from __future__ import annotations

import re
import unicodedata
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
# the column that qualifies each ranked figure (sample size or date)
TOP_METRIC_BASIS = {"online_listings": "online_listings_date", "online_profiles": "online_profiles_date",
                    "median_rent_per_m2": "median_rent_samples", "median_profile_budget": "median_profile_budget_samples",
                    "members_count": "members_date"}
# Every tool only reads a closed database: clients group them as read-only, no confirmation needed.
READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}


# rent_trend: the city row, then the site's median method (Viewports::MedianRents) per creation month
CITY_SQL = """
    SELECT canonical_path, city ->> 'fr' AS name
    FROM analytics.viewports
    WHERE data_type = 'city' AND NOT historical
      AND (canonical_path = %(city)s OR canonical_path LIKE %(suffix)s OR lower(city ->> 'fr') = lower(%(city)s))
    ORDER BY online_listings_count DESC NULLS LAST LIMIT 1
"""
RENT_HISTORY_SQL = """
    WITH months AS (
        SELECT generate_series(date_trunc('month', CURRENT_DATE) - ((%(months)s - 1) || ' months')::interval,
                               date_trunc('month', CURRENT_DATE), interval '1 month')::date AS month
    ), rents AS (
        SELECT date_trunc('month', l.created_at)::date AS month, r.cost_total_rent::float / r.surface AS rent_per_m2
        FROM analytics.rooms r JOIN analytics.listings l ON l.id = r.listing_id
        WHERE l.utm_geotag = %(geotag)s AND NOT l.blacklisted AND NOT l.from_xml_feed AND l.total_rooms_count > 0
          AND r.cost_total_rent > 0 AND r.surface >= 5
          AND (r.cost_periodicity IN ('month', 'monthly') OR r.cost_periodicity IS NULL)
          AND l.created_at >= (SELECT min(month) FROM months)
        UNION ALL
        SELECT date_trunc('month', created_at)::date, cost_total_rent::float / lodging_surface
        FROM analytics.listings
        WHERE utm_geotag = %(geotag)s AND NOT blacklisted AND NOT from_xml_feed AND total_rooms_count = 0
          AND listing_type = 'rental' AND cost_total_rent > 0 AND lodging_surface > 0
          AND (cost_periodicity = 'month' OR cost_periodicity IS NULL)
          AND created_at >= (SELECT min(month) FROM months)
    ), medians AS (
        SELECT month, count(*) AS rent_samples,
               CASE WHEN count(*) >= 30
                    THEN round(percentile_cont(0.5) WITHIN GROUP (ORDER BY rent_per_m2)::numeric, 2) END AS median_rent_per_m2
        FROM rents WHERE rent_per_m2 > 0 GROUP BY month
    )
    SELECT m.month, d.median_rent_per_m2, coalesce(d.rent_samples, 0) AS rent_samples, c.avg_online_listings
    FROM months m
    LEFT JOIN medians d ON d.month = m.month
    LEFT JOIN analytics.monthly_city_listings c ON c.canonical_path = %(path)s AND c.month = m.month
    ORDER BY m.month
"""
PUBLISHED_RENT_SQL = """
    SELECT median_rent_per_m2, median_rent_samples, median_rent_computed_on, currency_code, online_listings, online_listings_date
    FROM analytics.city_figures WHERE canonical_path = %(path)s AND median_rent_per_m2 IS NOT NULL
"""


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

    def tool(name: str, title: str, description: str):
        # the title twice (clients read either); text content only: no structuredContent copy of the CSV
        return mcp.tool(name=name, title=title, description=description, annotations={**READ_ONLY, "title": title},
                        output_schema=None)

    @tool(
        name="list_datasets", title="Lister les jeux de données",
        description=(
            "Catalogue of the datasets (views) of one or both databases, one line each: 'primary' = the "
            "site's data (members, listings, chats, payments, guides, geography), 'metrics' = the event "
            "stream and its daily/monthly counts — followed by the domain rules every query must apply "
            "(what 'online' means, partial months, where each series starts, brands). Call it once before "
            "your first SQL, then describe_dataset on what you will query. Skip it when a dedicated tool "
            "answers: top_cities / city_figures "
            "(cities today), rent_trend (a city's rents by month), traffic_series (area pages' audience), "
            "feed_volumes (partner feeds)."
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
        # claude.ai gives the model neither the server instructions nor data://dictionary: the rules ride here
        lines += ["", dictionary.RULES_PATH.read_text(encoding="utf-8")]
        services.audit.log("list_datasets", email=current_email(), database=database)
        return "\n".join(lines)

    @tool(
        name="describe_dataset", title="Décrire un jeu de données",
        description=(
            "One dataset's description (what a row is, its filters and caveats) and its columns (name, "
            "type, meaning). Call it before writing SQL against a dataset. include_values=true adds the "
            "values of up to eight categorical columns, counted on a 5000-row sample (not totals)."
        ),
    )
    async def describe_dataset(
        database: Annotated[DatabaseName, Field(description="The database list_datasets shows it in")],
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

    @tool(
        name="query", title="Requête SQL (lecture seule)",
        description=(
            "Run one read-only PostgreSQL SELECT on one database; returns CSV and a line with the row "
            "count. Only analytics.* is readable. Aggregate in SQL (GROUP BY month or city, count, sum, "
            "percentile_cont) rather than fetching rows. Limits: "
            f"{settings.query_timeout_s} s, {settings.max_rows} rows, {settings.max_result_chars} characters. "
            "Traps: an online listing is published AND NOT blacklisted AND (validated OR auto_validated); "
            "daily_event_counts and monthly_event_counts hold one row per action AND canonical_domain, so "
            "sum over domains; count creations (sign-ups, listings) there too, since deleted rows vanish from "
            "the tables; the current month is partial; compare lower(payment_status). Individual rows are "
            "for internal analysis only: publish aggregates."
        ),
    )
    async def query(
        database: Annotated[DatabaseName, Field(description="primary (site data) or metrics (events and their counts)")],
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

    @tool(
        name="explain_query", title="Plan d'exécution d'une requête",
        description=("The PostgreSQL plan of a SELECT (EXPLAIN, never ANALYZE), without running it. Only "
                     "useful before a heavy query on metrics.events (≈ 200 M rows); series come from "
                     "daily_event_counts instead."),
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

    @tool(
        name="city_figures", title="Chiffres clés d'une ville",
        description=(
            "Today's figures of one city exactly as its page on the site shows them: listings and tenant "
            "profiles online (nightly count and its date), median rent per m² with its sample size and "
            "computation date, median tenant budget, members, 12-month trend of listings online. Quote "
            "these in an article: they cannot contradict the site. Covers the ≈ 70 cities with a guide hub "
            "or at least 30 listings online."
        ),
    )
    async def city_figures(city: Annotated[str, Field(description="City name (Lyon) or path (/fr/auvergne-rhone-alpes/lyon)")]) -> str:
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
        result.rows = [(*row[:-1], _compact_trend(row[-1])) for row in result.rows]
        text, _ = result.csv(settings.max_result_chars)
        services.audit.log("city_figures", email=current_email(), city=city)
        return f"{text}\n\n[figures as of their own *_date / *_computed_on; a null median means fewer than 30 rents over 365 days]"

    @tool(
        name="rent_trend", title="Évolution des loyers d'une ville",
        description=(
            "Month-by-month rent history of one city: the median rent per m² of the organic listings "
            "created each month, computed per room as the site does (no median under 30 rents), with its "
            "sample size, and the monthly average of listings online (from 2024-03). Ends with the figure "
            "the city page shows today, a 365-day median recomputed monthly. One month is noisier than "
            "that 365-day figure: read each median with its sample size, quote the page's figure for today."
        ),
    )
    async def rent_trend(city: Annotated[str, Field(description="City name (Lyon) or path (/fr/auvergne-rhone-alpes/lyon)")],
                         months: Annotated[int, Field(ge=1, le=60, description="How many months back, the current one included")] = 24) -> str:
        found = await _fetch(services, "primary", CITY_SQL, {"city": city.strip(), "suffix": f"%/{_slug(city)}"})
        if not found.rows:
            raise ToolError(f"no city {city!r}: give its name as the site writes it (Lyon) or its path "
                            "(/fr/auvergne-rhone-alpes/lyon)")
        path, name = found.rows[0]
        # listings.utm_geotag is the city's path with '/' -> '_' (Viewports::MedianRents)
        params = {"path": path, "geotag": path.strip("/").replace("/", "_"), "months": int(months)}
        result = await _fetch(services, "primary", RENT_HISTORY_SQL, params)
        today = await _fetch(services, "primary", PUBLISHED_RENT_SQL, {"path": path})
        text, _ = result.csv(settings.max_result_chars)
        services.audit.log("rent_trend", email=current_email(), city=city, months=months)
        notes = [f"{name} ({path}): median rent per m² of the organic listings created that month, per room as "
                 "the site computes it, null under 30 rents; avg_online_listings = monthly average of the nightly "
                 "count (from 2024-03); the current month is partial; older months keep only the listings still "
                 "stored (deleted ones vanish), so their samples shrink"]
        if today.rows:
            median, samples, computed_on, currency, online, online_date = today.rows[0]
            notes.append(f"the city page shows today {median} {currency}/m² over {samples} rents created in the 365 "
                         f"days before {computed_on}, and {online} listings online on {online_date}")
        return f"{text}\n\n[{'; '.join(notes)}]"

    @tool(
        name="top_cities", title="Classement des villes",
        description=("Rank the cities of a country by one of today's city-page figures: the answer to « the N "
                     "cities with the most listings / the highest rents ». Each row carries the figure's date or "
                     "sample size. Covers the ≈ 70 cities with a guide hub or at least 30 listings online, "
                     "nearly all in France."),
    )
    async def top_cities(metric: Annotated[TopMetric, Field(description="The figure to rank by")] = "online_listings",
                         country: Annotated[str, Field(description="ISO country code: fr (France), be, ca…")] = "fr",
                         limit: Annotated[int, Field(ge=1, le=100, description="How many cities")] = 20) -> str:
        # dict.fromkeys: the ranked metric may already be one of the fixed columns
        columns = ", ".join(dict.fromkeys(["area_name", "canonical_path", metric, TOP_METRIC_BASIS[metric],
                                           "online_listings", "computed_on"]))
        sql = f"""
            SELECT {columns}
            FROM analytics.city_figures
            WHERE country_code = %(country)s AND {metric} IS NOT NULL
            ORDER BY {metric} DESC NULLS LAST LIMIT %(limit)s
        """
        result = await _fetch(services, "primary", sql, {"country": country.strip().lower(), "limit": int(limit)})
        text, _ = result.csv(settings.max_result_chars)
        services.audit.log("top_cities", email=current_email(), metric=metric, country=country)
        return f"{text}\n\n[{len(result.rows)} cities (those with a guide hub or at least 30 listings online); a median rests on its *_samples rents or budgets over 365 days]"

    @tool(
        name="traffic_series", title="Audience des pages de zones",
        description=(
            "Daily visitors, visits and pageviews of the site's area pages (Plausible) by acquisition source "
            "(Google, direct, e-mail, paid, AI assistants), for one area or summed over all tracked area "
            "pages; data from 2026-02-19, the last day can be partial. Visitors and visits are sums of "
            "page-level counts, not unique people or site-wide sessions."
        ),
    )
    async def traffic_series(viewport_path: Annotated[str, Field(description="Area path (/fr/auvergne-rhone-alpes/lyon); empty = all tracked area pages")] = "",
                             months: Annotated[int, Field(ge=1, le=36, description="How far back, in months")] = 6) -> str:
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

    @tool(
        name="feed_volumes", title="Volumes des flux partenaires",
        description=("Monthly import volumes of the partner XML feeds (listings published, updated, removed, "
                     "average online) by feed identity (agency type), feed standard and country; the current "
                     "month is partial."),
    )
    async def feed_volumes(months: Annotated[int, Field(ge=1, le=36, description="How far back, in months")] = 12) -> str:
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


# a full stop that ends a sentence, not an abbreviation ("MRR excl. VAT")
_SENTENCE_END = re.compile(r"(?<!\bexcl)(?<!\bincl)(?<!\be\.g)(?<!\bi\.e)\.\s+(?=[A-Z0-9])")


def _first_sentence(text: str | None) -> str:
    if not text:
        return ""
    head = _SENTENCE_END.split(text, maxsplit=1)[0]
    return head if len(head) < 160 else head[:157] + "…"


def _compact_trend(trend):
    """[{"month": "2025-10-01", "average": 421}, …] → "2025-10: 421; …": a third of the tokens, no CSV-escaped JSON."""
    if not isinstance(trend, list):
        return trend
    return "; ".join(f"{str(item.get('month'))[:7]}: {item.get('average')}" for item in trend if isinstance(item, dict))


def _slug(city: str) -> str:
    normalized = unicodedata.normalize("NFKD", city).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")


def today() -> date:
    return date.today()
