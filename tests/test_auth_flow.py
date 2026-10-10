"""The OAuth round trip claude.ai performs, against a fake Google: dynamic client registration,
authorize with PKCE, the upstream callback, the token exchange. An allowed account gets tokens and
can call tools; a stranger is refused at the token endpoint (unauthorized_client) and never holds a
FastMCP token; a refresh re-checks the allowlist."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import pytest
import uvicorn
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.auth import TokenVerifier
from fastmcp.server.auth.auth import AccessToken
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route

from datamcp.allowlist import EmailAllowlist
from datamcp.audit import Audit
from datamcp.auth import AllowlistedGoogleProvider, google_identity
from datamcp.server import build_server, client_storage

from .conftest import ALICE, BOB, free_port, serve

CLIENT_ID = "test-client-id.apps.googleusercontent.com"
CLIENT_CALLBACK = "http://127.0.0.1:9999/cb"


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def fake_id_token(email: str, *, aud: str = CLIENT_ID, iss: str = "https://accounts.google.com") -> str:
    header = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = b64url(json.dumps({"iss": iss, "aud": aud, "sub": "sub-" + email, "email": email,
                                 "email_verified": True, "exp": int(time.time()) + 3600}).encode())
    return f"{header}.{payload}.{b64url(b'signature-not-checked')}"


class FakeGoogle:
    """accounts.google.com in two routes: authorize redirects straight back with a code, the
    token endpoint hands out an upstream token set with an id_token for the signed-in user."""

    def __init__(self):
        self.user = ALICE
        self.token_calls = 0
        self.app = Starlette(routes=[Route("/o/oauth2/v2/auth", self.authorize),
                                     Route("/token", self.token, methods=["POST"])])

    async def authorize(self, request: Request):
        params = request.query_params
        assert params["client_id"] == CLIENT_ID and params.get("code_challenge_method") == "S256"
        query = urlencode({"code": "code-" + self.user, "state": params["state"]})
        return RedirectResponse(f"{params['redirect_uri']}?{query}", status_code=302)

    async def token(self, request: Request):
        form = await request.form()
        self.token_calls += 1
        if form.get("grant_type") == "refresh_token":
            email = str(form.get("refresh_token", "")).removeprefix("refresh-")
        else:
            assert form.get("code_verifier"), "the proxy forwards PKCE upstream"
            email = str(form.get("code", "")).removeprefix("code-")
        return JSONResponse({"access_token": "upstream-" + email, "token_type": "Bearer", "expires_in": 3600,
                             "refresh_token": "refresh-" + email,
                             "scope": "openid https://www.googleapis.com/auth/userinfo.email",
                             "id_token": fake_id_token(email)})


class FakeVerifier(TokenVerifier):
    """Stands in for Google's tokeninfo: validates the upstream tokens the fake issued."""

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token.startswith("upstream-"):
            return None
        email = token.removeprefix("upstream-")
        return AccessToken(token=token, client_id="sub-" + email, expires_at=int(time.time()) + 3600,
                           scopes=["openid", "https://www.googleapis.com/auth/userinfo.email"],
                           claims={"email": email, "email_verified": True, "sub": "sub-" + email})


async def run_app(app, port: int):
    import asyncio

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off"))
    task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started or task.done():
            break
        await asyncio.sleep(0.05)
    if task.done() or not server.started:
        raise RuntimeError(f"fake IdP did not start: {task.exception() if task.done() else 'timeout'}")
    try:
        yield server
    finally:
        server.should_exit = True
        await task


@pytest.fixture
async def fake_google():
    fake = FakeGoogle()
    port = free_port()
    async for _ in run_app(fake.app, port):
        fake.url = f"http://127.0.0.1:{port}"
        yield fake


@pytest.fixture
async def oauth_server(settings, fake_google):
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"
    settings = settings.__class__(**{**settings.__dict__, "base_url": base_url})
    provider = AllowlistedGoogleProvider(
        allowlist=EmailAllowlist(settings.allowlist_file), audit=Audit(settings.audit_dir),
        client_id=CLIENT_ID, client_secret="secret", base_url=base_url, redirect_path="/auth/callback",
        required_scopes=["openid", "email"], allowed_client_redirect_uris=["http://127.0.0.1:*", "https://claude.ai/api/mcp/auth_callback", "https://chatgpt.com/connector_platform_oauth_redirect"],
        client_storage=client_storage(settings), jwt_signing_key=settings.jwt_signing_key,
        require_authorization_consent=False,
    )
    provider._upstream_authorization_endpoint = fake_google.url + "/o/oauth2/v2/auth"
    provider._upstream_token_endpoint = fake_google.url + "/token"
    provider._token_validator = FakeVerifier()
    mcp, services = build_server(settings, auth=provider)
    async for url in serve(mcp, settings, port=port):
        yield url, settings


async def oauth_round_trip(http: httpx.AsyncClient, base: str, redirect: str = CLIENT_CALLBACK) -> httpx.Response:
    """DCR → authorize → fake IdP → callback → token; returns the token endpoint's response."""
    metadata = (await http.get(f"{base}/.well-known/oauth-authorization-server")).json()
    registered = await http.post(metadata["registration_endpoint"], json={
        "client_name": "test", "redirect_uris": [redirect], "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"], "token_endpoint_auth_method": "none"})
    assert registered.status_code == 201, registered.text
    client_id = registered.json()["client_id"]
    verifier = secrets.token_urlsafe(48)
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    scope = " ".join(metadata.get("scopes_supported") or ["openid"])  # what claude.ai requests: the advertised scopes
    authorize = await http.get(metadata["authorization_endpoint"], params={
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": "client-state", "scope": scope})
    assert authorize.status_code == 302, authorize.text
    idp = await http.get(authorize.headers["location"])  # the fake Google
    assert idp.status_code == 302
    callback = await http.get(idp.headers["location"])  # our /auth/callback
    assert callback.status_code == 302, callback.text
    location = urlparse(callback.headers["location"])
    assert location.netloc == urlparse(redirect).netloc
    assert location.path == urlparse(redirect).path
    query = parse_qs(location.query)
    assert query["state"] == ["client-state"]
    return await http.post(metadata["token_endpoint"], data={
        "grant_type": "authorization_code", "code": query["code"][0], "redirect_uri": redirect,
        "client_id": client_id, "code_verifier": verifier}), metadata, client_id


async def test_allowed_account_gets_tokens_and_tools(oauth_server, fake_google):
    base, settings = oauth_server
    async with httpx.AsyncClient() as http:
        response, metadata, client_id = await oauth_round_trip(http, base)
        assert response.status_code == 200, response.text
        tokens = response.json()
        assert tokens["access_token"] and tokens["refresh_token"]
        async with Client(StreamableHttpTransport(base + "/mcp", headers={"Authorization": f"Bearer {tokens['access_token']}"})) as c:
            names = {tool.name for tool in await c.list_tools()}
            assert "query" in names
            result = await c.call_tool("query", {"database": "primary", "sql": "SELECT count(*) AS n FROM things"})
            assert "n\n120" in "".join(block.text for block in result.content)
        # a refresh re-runs the allowlist check and issues new tokens
        refreshed = await http.post(metadata["token_endpoint"], data={
            "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client_id})
        assert refreshed.status_code == 200, refreshed.text
        assert refreshed.json()["access_token"] != tokens["access_token"]
    lines = [json.loads(line) for line in (settings.audit_dir / "datamcp.jsonl").read_text().splitlines()]
    assert any(line["kind"] == "auth_granted" and line["email"] == ALICE for line in lines)


async def test_stranger_is_refused_at_the_token_endpoint(oauth_server, fake_google):
    base, settings = oauth_server
    fake_google.user = BOB
    async with httpx.AsyncClient() as http:
        response, _, _ = await oauth_round_trip(http, base)
        assert response.status_code == 400, response.text
        assert response.json()["error"] == "unauthorized_client"
    lines = [json.loads(line) for line in (settings.audit_dir / "datamcp.jsonl").read_text().splitlines()]
    assert any(line["kind"] == "auth_refused" and line["email"] == BOB for line in lines)


async def test_removed_account_is_refused_at_refresh(oauth_server, fake_google):
    base, settings = oauth_server
    async with httpx.AsyncClient() as http:
        response, metadata, client_id = await oauth_round_trip(http, base)
        tokens = response.json()
        settings.allowlist_file.write_text("nobody@example.org\n", encoding="utf-8")
        refreshed = await http.post(metadata["token_endpoint"], data={
            "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client_id})
        assert refreshed.status_code == 400 and refreshed.json()["error"] == "unauthorized_client"


def test_google_identity_checks_issuer_and_audience():
    tokens = {"id_token": fake_id_token(ALICE)}
    assert google_identity(tokens, CLIENT_ID) == (ALICE, True)
    with pytest.raises(ValueError):
        google_identity({"id_token": fake_id_token(ALICE, aud="someone-else")}, CLIENT_ID)
    with pytest.raises(ValueError):
        google_identity({"id_token": fake_id_token(ALICE, iss="https://evil.example")}, CLIENT_ID)
    with pytest.raises(ValueError):
        google_identity({"access_token": "x"}, CLIENT_ID)


@pytest.mark.parametrize("redirect", ["https://claude.ai/api/mcp/auth_callback", "https://chatgpt.com/connector_platform_oauth_redirect"])
async def test_provider_callbacks_complete_pkce_round_trip(oauth_server, redirect):
    base, _ = oauth_server
    async with httpx.AsyncClient() as http:
        response, _, _ = await oauth_round_trip(http, base, redirect)
        assert response.status_code == 200


async def test_unapproved_redirect_is_refused(oauth_server):
    base, _ = oauth_server
    async with httpx.AsyncClient() as http:
        metadata = (await http.get(base + "/.well-known/oauth-authorization-server")).json()
        response = await http.post(metadata["registration_endpoint"], json={
            "client_name": "stranger", "redirect_uris": ["https://stranger.example/callback"],
            "grant_types": ["authorization_code"], "response_types": ["code"], "token_endpoint_auth_method": "none"})
        assert response.status_code == 400
