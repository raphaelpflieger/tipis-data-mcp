"""Who may use the connector: Google sign-in through FastMCP's OAuth proxy, then an e-mail
allowlist enforced twice.

1. At the token endpoint (`_extract_upstream_claims`, called by FastMCP when it exchanges the
   authorization code — and again on every refresh): the identity comes from Google's own
   id_token, obtained server-side from Google's token endpoint over TLS with our client secret.
   A stranger's Google account is refused with `unauthorized_client` (HTTP 400: the token endpoint
   only knows the RFC 6749 §5.2 codes) before any FastMCP token exists.
2. On every MCP request (`AllowlistMiddleware`): the e-mail of the validated token must still be
   in the allowlist file, which is hot-reloaded — removing a line revokes within one call.

The Google OAuth client is in "Testing" publishing status with the allowed accounts as test
users; that is a third gate, on Google's side.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

from fastmcp.exceptions import FastMCPError, ToolError
from fastmcp.server.auth.providers.google import GoogleProvider
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from mcp.server.auth.provider import TokenError

from .allowlist import EmailAllowlist
from .audit import Audit

logger = logging.getLogger("datamcp.auth")
GOOGLE_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}


class NotAllowed(Exception):
    pass


def decode_jwt_payload(token: str) -> dict[str, Any]:
    """The payload of a JWT without signature verification: only for a token we just received
    directly from the identity provider's token endpoint (see the module docstring)."""
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("not a JWT")
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


def google_identity(idp_tokens: dict[str, Any], client_id: str) -> tuple[str, bool]:
    """(email, email_verified) from Google's id_token; ValueError when absent or not ours."""
    id_token = idp_tokens.get("id_token")
    if not id_token:
        raise ValueError("Google returned no id_token (is the openid scope requested?)")
    claims = decode_jwt_payload(id_token)
    if claims.get("iss") not in GOOGLE_ISSUERS:
        raise ValueError(f"unexpected id_token issuer {claims.get('iss')!r}")
    if claims.get("aud") != client_id:
        raise ValueError("id_token audience is not this OAuth client")
    email = EmailAllowlist.normalize(claims.get("email"))
    if not email:
        raise ValueError("id_token carries no email claim")
    return email, _truthy(claims.get("email_verified"))


def _truthy(value: Any) -> bool:
    return str(value).lower() == "true"


class AllowlistedGoogleProvider(GoogleProvider):
    """GoogleProvider that refuses to mint tokens for any Google account outside the allowlist."""

    def __init__(self, *, allowlist: EmailAllowlist, audit: Audit, client_id: str, **kwargs: Any):
        super().__init__(client_id=client_id, **kwargs)
        self.allowlist = allowlist
        self.audit = audit
        self.client_id = client_id

    async def _extract_upstream_claims(self, idp_tokens: dict[str, Any]) -> dict[str, Any] | None:
        try:
            email, verified = google_identity(idp_tokens, self.client_id)
        except ValueError as error:
            # no id_token (or not ours): fall back to Google's tokeninfo through the verifier
            email, verified = await self._identity_from_tokeninfo(idp_tokens, str(error))
        if not verified:
            self.audit.log("auth_refused", email=email, reason="email not verified by Google")
            raise TokenError("unauthorized_client", "This Google account's e-mail is not verified")
        if not self.allowlist.contains(email):
            self.audit.log("auth_refused", email=email, reason="not in allowlist")
            raise TokenError("unauthorized_client", "This Google account is not allowed on this connector")
        self.audit.log("auth_granted", email=email)
        return {"email": email, "email_verified": True}

    async def _identity_from_tokeninfo(self, idp_tokens: dict[str, Any], why: str) -> tuple[str, bool]:
        logger.info("id_token unusable (%s): asking Google tokeninfo", why)
        access_token = idp_tokens.get("access_token")
        verified = await self._token_validator.verify_token(access_token) if access_token else None
        if verified is None:
            raise TokenError("unauthorized_client", "Google did not confirm this account")
        claims = verified.claims or {}
        return EmailAllowlist.normalize(claims.get("email")), _truthy(claims.get("email_verified"))


def current_email() -> str | None:
    """The validated e-mail of the caller, or None outside a request."""
    token = get_access_token()
    if token is None:
        return None
    claims = token.claims or {}
    upstream = claims.get("upstream_claims") or {}
    email = claims.get("email") or upstream.get("email")
    verified = _truthy(claims.get("email_verified")) or _truthy(upstream.get("email_verified"))
    return EmailAllowlist.normalize(email) if email and verified else None


class AllowlistMiddleware(Middleware):
    """Refuses every request whose caller is not (any more) in the allowlist, and every tool call
    while the connector is disabled (DATAMCP_ENABLED=0)."""

    OPEN_METHODS = {"ping"}

    def __init__(self, allowlist: EmailAllowlist, audit: Audit, *, enabled: bool = True):
        self.allowlist = allowlist
        self.audit = audit
        self.enabled = enabled

    async def on_request(self, context: MiddlewareContext, call_next: CallNext):
        method = context.method or ""
        if method in self.OPEN_METHODS:
            return await call_next(context)
        email = current_email()
        if not self.allowlist.contains(email):
            self.audit.log("call_refused", email=email, method=method, reason="not in allowlist")
            raise _refusal(method, "This Google account is not allowed on this connector")
        if not self.enabled and method in {"tools/call", "resources/read"}:
            raise ToolError("The connector is disabled by its operator (DATAMCP_ENABLED=0)")
        return await call_next(context)


def _refusal(method: str, message: str) -> Exception:
    return ToolError(message) if method == "tools/call" else FastMCPError(message)
