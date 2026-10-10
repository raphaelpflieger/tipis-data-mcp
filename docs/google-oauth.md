# Google OAuth client for the connector (Raphaël, once)

claude.ai signs the coworkers in with the organisation's Google account. The connector needs one
OAuth client on Google Cloud; nothing else on Google's side (no API enabled, no billing).

1. https://console.cloud.google.com → a project (any; e.g. the existing tipis project).
2. **APIs & Services → OAuth consent screen**: user type *External*; app name « Données LCDC »;
   support and developer e-mails: yours; scopes: `openid`, `.../auth/userinfo.email` (and
   `.../auth/userinfo.profile` if you want names in the audit log — not required). **Publishing
   status: Testing** — do not publish. **Test users**: the organisation's Google account, yours.
   (Testing status: only test users can sign in; refresh tokens do not expire with only these
   scopes; the consent screen shows an "unverified app" warning that test users can pass.)
3. **APIs & Services → Credentials → Create credentials → OAuth client ID**: type *Web
   application*; name « tipis-data-mcp »; **Authorized redirect URIs**:
   `https://data-mcp.tipis.com/auth/callback`. No JavaScript origin needed.
4. Copy the client id (`…apps.googleusercontent.com`) and the secret (`GOCSPX-…`) into the
   tipis-data-mcp vault (`datamcp_google_client_id`, `datamcp_google_client_secret`).
5. Later, to add a coworker's own Google account: add it as a test user here **and** to
   `datamcp_allowed_emails` (`--tags allowlist`).

Rotation: Credentials → the client → *Reset secret* → vault → `--tags secrets,app`. Existing
claude.ai connections keep working (the proxy holds the upstream refresh tokens); if Google
invalidates them, users reconnect once.
