"""Test world: a throwaway PostgreSQL (docker-compose.test.yml) with a private table, an analytics
view and the reader role; a server on a loopback port; a static bearer token for the query tests.
The OAuth round trip has its own fixtures in test_auth_flow.py."""

from __future__ import annotations

import asyncio
import os
import socket
from pathlib import Path

import psycopg
import pytest
import uvicorn
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from pydantic import AnyHttpUrl

from datamcp.config import DatabaseConfig, Settings
from datamcp.contract import DEFINITIONS_SQL
from datamcp.server import build_server

DSN = os.environ.get("DATAMCP_TEST_DSN", "postgresql://datamcp:datamcp@127.0.0.1:5433/datamcp")
READER_PASSWORD = "reader-pw"
ALICE = "alice@example.org"
BOB = "bob@example.org"

SCHEMA_SQL = """
DROP SCHEMA IF EXISTS analytics CASCADE;
DROP TABLE IF EXISTS public.secret;
CREATE TABLE public.secret (id serial PRIMARY KEY, email text NOT NULL, kind text NOT NULL, amount integer, day date);
INSERT INTO public.secret (email, kind, amount, day)
SELECT 'user' || g || '@example.org', CASE WHEN g <= 60 THEN 'flatshare' WHEN g <= 100 THEN 'rental' ELSE 'sublet' END, g * 10, DATE '2026-01-01' + g
FROM generate_series(1, 120) g;
CREATE SCHEMA analytics;
CREATE VIEW analytics.things AS SELECT id, kind, amount, day FROM public.secret;
COMMENT ON VIEW analytics.things IS 'Things: one row per thing (test fixture)';
COMMENT ON COLUMN analytics.things.kind IS 'flatshare, rental or sublet';
CREATE VIEW analytics.monthly_things AS
  SELECT date_trunc('month', day)::date AS month, kind, count(*) AS things FROM analytics.things GROUP BY 1, 2;
COMMENT ON VIEW analytics.monthly_things IS 'Things per month and kind';
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tipis_analytics') THEN
    CREATE ROLE tipis_analytics LOGIN;
  END IF;
END $$;
ALTER ROLE tipis_analytics WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD 'reader-pw';
ALTER ROLE tipis_analytics SET default_transaction_read_only = on;
ALTER ROLE tipis_analytics SET search_path = analytics;
GRANT CONNECT ON DATABASE datamcp TO tipis_analytics;
GRANT USAGE ON SCHEMA analytics TO tipis_analytics;
CREATE TABLE analytics._contract (id integer PRIMARY KEY CHECK (id = 1), expected text NOT NULL,
                                 applied text, definitions jsonb NOT NULL);
INSERT INTO analytics._contract VALUES (1, 'test', 'test', '{}');
GRANT SELECT ON ALL TABLES IN SCHEMA analytics TO tipis_analytics;
""" + f"UPDATE analytics._contract SET definitions = ({DEFINITIONS_SQL}) WHERE id = 1;"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="session")
def prepared_db() -> str:
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(SCHEMA_SQL)
    return DSN


def reader_config(key: str, dsn: str) -> DatabaseConfig:
    info = psycopg.conninfo.conninfo_to_dict(dsn)
    return DatabaseConfig(key=key, label=f"{key} (test)", host=str(info["host"]), port=int(info.get("port", 5432)),
                          dbname=str(info["dbname"]), user="tipis_analytics", password=READER_PASSWORD,
                          sslmode="disable", sslrootcert=None)


@pytest.fixture
def secrets_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "secrets"
    directory.mkdir()
    (directory / "allowed_emails.txt").write_text(f"# test\n{ALICE}\n", encoding="utf-8")
    return directory


def make_settings(dsn: str, tmp_path: Path, secrets_dir: Path, **overrides) -> Settings:
    from cryptography.fernet import Fernet

    base = dict(
        base_url="http://127.0.0.1:0", mcp_path="/mcp", host="127.0.0.1", port=0, enabled=True,
        google_client_id="test-client-id.apps.googleusercontent.com", google_client_secret="secret",
        jwt_signing_key="a-very-long-test-signing-key-0123456789", storage_key=Fernet.generate_key().decode(),
        storage_dir=tmp_path / "oauth", allowed_client_redirect_uris=["http://127.0.0.1:*", "http://localhost:*"],
        require_consent=False, allowlist_file=secrets_dir / "allowed_emails.txt", audit_dir=tmp_path / "logs",
        query_timeout_s=2, max_rows=50, max_result_chars=4000, max_concurrent_queries=2, max_queries_per_user=1,
        rate_limit_rps=200.0, rate_limit_burst=200,
        catalog_ttl_s=3600, allowlist_yaml=None,
        databases={"primary": reader_config("primary", dsn), "metrics": reader_config("metrics", dsn)},
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def settings(prepared_db: str, tmp_path: Path, secrets_dir: Path) -> Settings:
    return make_settings(prepared_db, tmp_path, secrets_dir)


class Served:
    def __init__(self, url: str, settings: Settings, services):
        self.url = url
        self.settings = settings
        self.services = services


async def serve(mcp, settings: Settings, port: int | None = None):
    """Run the FastMCP app with uvicorn on a loopback port (free by default) until the fixture ends."""
    port = port or free_port()
    app = mcp.http_app(path=settings.mcp_path, stateless_http=True, allowed_hosts=["127.0.0.1", "localhost"])
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started or task.done():
            break
        await asyncio.sleep(0.05)
    if task.done() or not server.started:
        raise RuntimeError(f"server did not start: {task.exception() if task.done() else 'timeout'}")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


# StaticTokenVerifier returns the whole entry as the token's claims: the e-mail sits at the top level
STATIC_TOKENS = {
    "alice-token": {"client_id": "alice", "scopes": ["openid", "email"], "email": ALICE, "email_verified": True},
    "bob-token": {"client_id": "bob", "scopes": ["openid", "email"], "email": BOB, "email_verified": True},
    "unverified-token": {"client_id": "eve", "scopes": ["openid", "email"], "email": ALICE, "email_verified": False},
}


@pytest.fixture
async def served(settings: Settings):
    """The server with a static bearer verifier (no OAuth): tokens of STATIC_TOKENS."""
    port = free_port()
    verifier = StaticTokenVerifier(tokens=STATIC_TOKENS)
    verifier.base_url = AnyHttpUrl(f"http://127.0.0.1:{port}")  # so the 401 challenge names the resource metadata
    mcp, services = build_server(settings, auth=verifier)
    async for url in serve(mcp, settings, port=port):
        yield Served(url, settings, services)
