"""Operator commands: `python -m datamcp.cli check-drift` compares what the databases expose
(analytics.* columns as the reader role sees them) with tipis-web's db/analytics_allowlist.yml,
so a view that would expose an unclassified column is caught after a deploy."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import yaml

from .catalog import Catalog
from .config import Settings
from .db import Database

TRANSFORM_OUTPUTS = {
    "round_point": lambda c: [f"{c}_lat", f"{c}_lon"],
    "point": lambda c: [f"{c}_lat", f"{c}_lon"],
    "round_number": lambda c: [c],
    "age_band": lambda c: [f"{c}_age_band"],
    "length": lambda c: [f"{c}_length"],
    "presence": lambda c: [f"{c}_present"],
    "jsonb_keys": lambda c: [c],
    "postal_code": lambda c: [c],
    "department": lambda c: [f"{c}_department"],
}


def expected_columns(allowlist: dict) -> dict[tuple[str, str], list[str]]:
    """{(database, view): [column names]} the allowlist promises, table views only."""
    expected: dict[tuple[str, str], list[str]] = {}
    for database, spec in allowlist["databases"].items():
        for table, columns in spec["tables"].items():
            if "exclude_table" in columns:
                continue
            names = list((columns.get("include") or {}).keys())
            for column, transform in (columns.get("transform") or {}).items():
                (kind,) = transform.keys()
                names += TRANSFORM_OUTPUTS[kind](column)
            expected[(database, table)] = names
    return expected


def drift(allowlist: dict, catalog: Catalog) -> list[str]:
    problems = []
    expected = expected_columns(allowlist)
    for database, spec in allowlist["databases"].items():
        for view in (spec.get("views") or {}):
            if catalog.get(database, view) is None:
                problems.append(f"{database}: analytics.{view} is missing (run rails analytics:sync_views)")
    for (database, table), names in expected.items():
        dataset = catalog.get(database, table)
        if dataset is None:
            problems.append(f"{database}: analytics.{table} is missing (run rails analytics:sync_views)")
            continue
        actual = [column.name for column in dataset.columns]
        if actual != names:
            extra, missing = set(actual) - set(names), set(names) - set(actual)
            problems.append(f"{database}: analytics.{table} exposes {sorted(extra) or 'nothing extra'}, "
                            f"lacks {sorted(missing) or 'nothing'}")
    for dataset in catalog.list():
        views = set((allowlist["databases"][dataset.database].get("views") or {}).keys())
        if (dataset.database, dataset.name) not in expected and dataset.name not in views:
            problems.append(f"{dataset.database}: analytics.{dataset.name} is not in the allowlist at all")
    return problems


async def check_drift(path: Path) -> int:
    settings = Settings.from_env()
    allowlist = yaml.safe_load(path.read_text(encoding="utf-8"))
    databases = {key: Database(config, settings) for key, config in settings.databases.items()}
    for database in databases.values():
        await database.open()
    try:
        catalog = Catalog(databases)
        await catalog.refresh(force=True)
        problems = drift(allowlist, catalog)
    finally:
        for database in databases.values():
            await database.close()
    for problem in problems:
        print(problem)
    print("OK: the databases expose exactly what the allowlist says" if not problems else f"DRIFT: {len(problems)} problem(s)")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="datamcp")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check-drift", help="compare the analytics views with tipis-web's allowlist")
    check.add_argument("--allowlist", type=Path, required=True, help="path to db/analytics_allowlist.yml")
    args = parser.parse_args(argv)
    if args.command == "check-drift":
        return asyncio.run(check_drift(args.allowlist))
    return 2


if __name__ == "__main__":
    sys.exit(main())
