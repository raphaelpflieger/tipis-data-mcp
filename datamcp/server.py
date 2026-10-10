"""Assembly: settings → auth → databases → catalog → tools; /healthz; the entrypoint.

Run: `python -m datamcp.server` (the container CMD). Configuration: datamcp/config.py.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from cryptography.fernet import Fernet
from fastmcp import FastMCP
from fastmcp.server.middleware.rate_limiting import RateLimitingMiddleware, TokenBucketRateLimiter
from key_value.aio.stores.filetree import (
    FileTreeStore,
    FileTreeV1CollectionSanitizationStrategy,
    FileTreeV1KeySanitizationStrategy,
)
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import __version__
from .allowlist import EmailAllowlist
from .audit import Audit
from .auth import AllowlistedGoogleProvider, AllowlistMiddleware
from .catalog import Catalog
from .config import Settings
from .db import Database
from .execution import ExecutionMiddleware
from .tools import Services, register

logger = logging.getLogger("datamcp")

# claude.ai does not pass instructions to the model (Claude Code does; ChatGPT reads the first 512
# characters): the rules that matter live in the tool descriptions and outputs, this is the routing.
INSTRUCTIONS = (
    "Read-only analytics of La Carte des Colocs (pseudonymous records; publish aggregates only). "
    "Cities today: top_cities, city_figures; a city's rents by month: rent_trend; area pages' audience: "
    "traffic_series; partner feeds: feed_volumes. Otherwise list_datasets, describe_dataset, then one "
    "aggregated SELECT with query. Answer in the user's language (French by default); cite the dataset, "
    "the 'as of' date and the sample size of every figure."
)


class MonotonicTokenBucket(TokenBucketRateLimiter):
    """FastMCP's bucket reads the wall clock: a clock stepping back (NTP, a VM resync) drives its
    tokens negative and refuses every call for about as long as the step (seen in the tests on WSL,
    -2.5 s → -300 tokens). The monotonic clock never steps back."""

    def __init__(self, capacity: int, refill_rate: float):
        super().__init__(capacity, refill_rate)
        self.last_refill = time.monotonic()

    async def consume(self, tokens: int = 1) -> bool:
        async with self._lock:
            now = time.monotonic()
            self.tokens = min(self.capacity, self.tokens + (now - self.last_refill) * self.refill_rate)
            self.last_refill = now
            if self.tokens < tokens:
                return False
            self.tokens -= tokens
            return True


class GlobalRateLimit(RateLimitingMiddleware):
    def __init__(self, max_requests_per_second: float, burst_capacity: int):
        super().__init__(max_requests_per_second=max_requests_per_second, burst_capacity=burst_capacity,
                         global_limit=True)
        self.global_limiter = MonotonicTokenBucket(self.burst_capacity, self.max_requests_per_second)


def client_storage(settings: Settings):
    """The OAuth state (client registrations, encrypted upstream tokens) on a volume, encrypted
    with our own key: survives image rebuilds and never lands in the image."""
    directory = settings.storage_dir
    directory.mkdir(parents=True, exist_ok=True)
    store = FileTreeStore(
        data_directory=directory,
        key_sanitization_strategy=FileTreeV1KeySanitizationStrategy(directory),
        collection_sanitization_strategy=FileTreeV1CollectionSanitizationStrategy(directory),
    )
    return FernetEncryptionWrapper(key_value=store, fernet=Fernet(settings.storage_key.encode()),
                                   raise_on_decryption_error=False)


def build_auth(settings: Settings, allowlist: EmailAllowlist, audit: Audit) -> AllowlistedGoogleProvider:
    return AllowlistedGoogleProvider(
        allowlist=allowlist,
        audit=audit,
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        base_url=settings.base_url,
        redirect_path="/auth/callback",
        required_scopes=["openid", "email"],
        allowed_client_redirect_uris=settings.allowed_client_redirect_uris,
        client_storage=client_storage(settings),
        jwt_signing_key=settings.jwt_signing_key,
        require_authorization_consent=settings.require_consent,
        fastmcp_access_token_expiry_seconds=3600,
        fallback_refresh_token_expiry_seconds=30 * 24 * 3600,
        token_expiry_threshold_seconds=60,
    )


def build_server(settings: Settings, *, auth=None) -> tuple[FastMCP, Services]:
    allowlist = EmailAllowlist(settings.allowlist_file)
    audit = Audit(settings.audit_dir)
    databases = {key: Database(config, settings) for key, config in settings.databases.items()}
    catalog = Catalog(databases, ttl_s=settings.catalog_ttl_s)
    services = Services(settings, databases, catalog, audit)

    @asynccontextmanager
    async def lifespan(server: FastMCP):
        for database in databases.values():
            await database.open()
        try:
            await catalog.refresh(force=True)
        except Exception as error:  # noqa: BLE001 — start anyway; list_datasets retries
            logger.error("catalog refresh failed at startup: %s", error)
        logger.info("tipis-data-mcp %s ready (%d datasets)", __version__, len(catalog.list()))
        try:
            yield {}
        finally:
            for database in databases.values():
                await database.close()

    mcp = FastMCP(
        "Données LCDC",
        instructions=INSTRUCTIONS,
        version=__version__,
        auth=auth if auth is not None else build_auth(settings, allowlist, audit),
        middleware=[
            AllowlistMiddleware(allowlist, audit, enabled=settings.enabled),
            # MCP-level throttle on top of nginx's limit_req (two users, one model: a burst is a chat turn)
            GlobalRateLimit(settings.rate_limit_rps, settings.rate_limit_burst),
            ExecutionMiddleware(services),
        ],
        lifespan=lifespan,
        mask_error_details=False,
    )
    register(mcp, services)

    @mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(request: Request) -> JSONResponse:
        # no auth: nginx restricts the route to the admin IPs and the app host's probe
        status = {key: await database.health() for key, database in databases.items()}
        healthy = all(problem is None for problem in status.values())
        body = {"ok": healthy, "version": __version__, "enabled": settings.enabled,
                "databases": {key: (problem or "ok") for key, problem in status.items()},
                "allowed_accounts": len(allowlist.emails())}
        return JSONResponse(body, status_code=200 if healthy else 503)

    return mcp, services


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env()
    mcp, _ = build_server(settings)
    mcp.run(
        transport="http",
        host=settings.host,
        port=settings.port,
        path=settings.mcp_path,
        stateless_http=True,
        show_banner=False,
        allowed_hosts=[settings.hostname, "localhost", "127.0.0.1"],
    )


if __name__ == "__main__":
    main()
