"""Configuration: environment variables only (DATAMCP_*), secrets read from files.

Every knob is listed here with its default so the runbook (README) and the Ansible templates have
one source of truth. Secrets never come from the environment: they are files under a 0400
directory provisioned from the Ansible vault (`*_FILE` variables).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

PREFIX = "DATAMCP_"


class ConfigError(RuntimeError):
    pass


def _env(name: str, default: str | None = None, *, required: bool = False) -> str | None:
    value = os.environ.get(PREFIX + name, default)
    if required and (value is None or value == ""):
        raise ConfigError(f"{PREFIX}{name} is required")
    return value


def _secret(name: str, *, required: bool = True) -> str | None:
    """The content of the file named by DATAMCP_<name>_FILE, stripped."""
    path = _env(f"{name}_FILE")
    if not path:
        if required:
            raise ConfigError(f"{PREFIX}{name}_FILE is required")
        return None
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError as error:
        raise ConfigError(f"cannot read {PREFIX}{name}_FILE={path}: {error}") from error


def _bool(name: str, default: bool) -> bool:
    return (_env(name, "1" if default else "0") or "").strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    return int(_env(name, str(default)) or default)


def _list(name: str, default: list[str]) -> list[str]:
    raw = _env(name)
    if raw is None:
        return default
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class DatabaseConfig:
    key: str  # "primary" | "metrics"
    label: str
    host: str
    port: int
    dbname: str
    user: str
    password: str
    sslmode: str
    sslrootcert: str | None

    def conninfo(self) -> str:
        parts = {
            "host": self.host, "port": str(self.port), "dbname": self.dbname, "user": self.user,
            "password": self.password, "sslmode": self.sslmode, "application_name": "tipis-data-mcp",
            # the role already sets these; repeated here so a misconfigured role still cannot write
            "options": "-c default_transaction_read_only=on",
        }
        if self.sslrootcert:
            parts["sslrootcert"] = self.sslrootcert
        return " ".join(f"{key}={_quote(value)}" for key, value in parts.items())


def _quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


@dataclass(frozen=True)
class Settings:
    base_url: str
    mcp_path: str
    host: str
    port: int
    enabled: bool
    google_client_id: str
    google_client_secret: str
    jwt_signing_key: str
    storage_key: str
    storage_dir: Path
    allowed_client_redirect_uris: list[str]
    require_consent: bool
    allowlist_file: Path
    audit_dir: Path
    query_timeout_s: int
    max_rows: int
    max_result_chars: int
    max_concurrent_queries: int
    max_queries_per_user: int
    rate_limit_rps: float
    rate_limit_burst: int
    catalog_ttl_s: int
    allowlist_yaml: Path | None
    databases: dict[str, DatabaseConfig] = field(default_factory=dict)

    @property
    def hostname(self) -> str:
        return urlparse(self.base_url).hostname or "localhost"

    @property
    def mcp_url(self) -> str:
        return self.base_url.rstrip("/") + self.mcp_path

    @classmethod
    def from_env(cls) -> Settings:
        base_url = (_env("BASE_URL", required=True) or "").rstrip("/")
        databases = {
            "primary": _database("PRIMARY", "primary", "La Carte des Colocs — primary database (tipis_production)"),
            "metrics": _database("METRICS", "metrics", "Events / metrics database (TimescaleDB)"),
        }
        allowlist_yaml = _env("ALLOWLIST_YAML")
        return cls(
            base_url=base_url,
            mcp_path=_env("MCP_PATH", "/mcp") or "/mcp",
            host=_env("HOST", "127.0.0.1") or "127.0.0.1",
            port=_int("PORT", 8765),
            enabled=_bool("ENABLED", True),
            google_client_id=_env("GOOGLE_CLIENT_ID", required=True) or "",
            google_client_secret=_secret("GOOGLE_CLIENT_SECRET") or "",
            jwt_signing_key=_secret("JWT_SIGNING_KEY") or "",
            storage_key=_secret("STORAGE_KEY") or "",
            storage_dir=Path(_env("STORAGE_DIR", "/data/oauth") or "/data/oauth"),
            allowed_client_redirect_uris=_list("ALLOWED_CLIENT_REDIRECT_URIS", [
                "https://claude.ai/api/mcp/auth_callback",
                "https://claude.com/api/mcp/auth_callback",
                "https://chatgpt.com/connector_platform_oauth_redirect",
            ]),
            require_consent=_bool("REQUIRE_CONSENT", True),
            allowlist_file=Path(_env("ALLOWLIST_FILE", "/secrets/allowed_emails.txt") or ""),
            audit_dir=Path(_env("AUDIT_DIR", "/logs") or "/logs"),
            query_timeout_s=_int("QUERY_TIMEOUT_S", 60),
            max_rows=_int("MAX_ROWS", 1000),
            max_result_chars=_int("MAX_RESULT_CHARS", 60_000),
            max_concurrent_queries=_int("MAX_CONCURRENT_QUERIES", 3),
            max_queries_per_user=_int("MAX_QUERIES_PER_USER", 1),
            rate_limit_rps=float(_env("RATE_LIMIT_RPS", "5") or 5),
            rate_limit_burst=_int("RATE_LIMIT_BURST", 20),
            catalog_ttl_s=_int("CATALOG_TTL_S", 3600),
            allowlist_yaml=Path(allowlist_yaml) if allowlist_yaml else None,
            databases=databases,
        )


def _database(env_key: str, key: str, label: str) -> DatabaseConfig:
    return DatabaseConfig(
        key=key,
        label=_env(f"{env_key}_DB_LABEL", label) or label,
        host=_env(f"{env_key}_DB_HOST", required=True) or "",
        port=_int(f"{env_key}_DB_PORT", 5432),
        dbname=_env(f"{env_key}_DB_NAME", required=True) or "",
        user=_env(f"{env_key}_DB_USER", "tipis_analytics") or "tipis_analytics",
        password=_secret(f"{env_key}_DB_PASSWORD") or "",
        sslmode=_env(f"{env_key}_DB_SSLMODE", "verify-full") or "verify-full",
        sslrootcert=_env(f"{env_key}_DB_SSLROOTCERT"),
    )
