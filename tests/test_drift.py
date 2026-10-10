from __future__ import annotations

import pytest

from datamcp.catalog import Catalog
from datamcp.cli import drift, expected_columns
from datamcp.db import Database

from .conftest import Served


def test_expected_columns_mirror_the_transform_naming():
    allowlist = {"databases": {"primary": {"tables": {
        "things": {"include": {"id": None, "kind": None},
                   "transform": {"coordinates": {"round_point": "x"}, "birthday": {"age_band": None},
                                 "description": {"length": None}, "phone": {"presence": None},
                                 "postal": {"department": None}, "filters": {"jsonb_keys": {"keys": ["a"]}}}},
        "admins": {"exclude_table": "staff"},
    }}, "metrics": {"tables": {}}}}
    assert expected_columns(allowlist) == {("primary", "things"): [
        "id", "kind", "coordinates_lat", "coordinates_lon", "birthday_age_band", "description_length",
        "phone_present", "postal_department", "filters"]}


async def test_drift_against_the_database(served: Served):
    catalog = served.services.catalog
    await catalog.refresh(force=True)
    exact = {"databases": {
        "primary": {"tables": {"things": {"include": {"id": None, "kind": None, "amount": None, "day": None}}},
                    "views": {"monthly_things": {"sql": "…"}}},
        "metrics": {"tables": {"things": {"include": {"id": None, "kind": None, "amount": None, "day": None}}},
                    "views": {"monthly_things": {"sql": "…"}}}}}
    assert drift(exact, catalog) == []
    exact["databases"]["primary"]["views"]["missing_aggregate"] = {"sql": "…"}
    assert any("analytics.missing_aggregate is missing" in p for p in drift(exact, catalog))
    leaking = {"databases": {
        "primary": {"tables": {"things": {"include": {"id": None, "kind": None, "amount": None},
                                          "exclude": {"day": "internal"}}},
                    "views": {"monthly_things": {"sql": "…"}}},
        "metrics": {"tables": {"things": {"include": {"id": None, "kind": None, "amount": None, "day": None}}},
                    "views": {}}}}
    problems = drift(leaking, catalog)
    assert any("analytics.things exposes ['day']" in p for p in problems)
    assert any("analytics.monthly_things is not in the allowlist" in p for p in problems)
