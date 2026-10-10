# Google OAuth client for the connector (Raphaël, once)

claude.ai signs the coworkers in with the organisation's Google account. The connector needs one
OAuth client on Google Cloud; nothing else on Google's side (no API enabled, no billing).

1. https://console.cloud.google.com → a **dedicated project** (ours: `tipis-data-mcp`, under the
   Google account authuser=1). The publishing status and the test users belong to the project, not
   to the client: in a shared project another app's status or audience would decide this one's.
2. **Google Auth Platform** (the former « OAuth consent screen »):
   - **Branding**: app name « Données LCDC »; support and developer e-mails: yours.
   - **Audience**: user type *External*; **publishing status: Testing** — do not publish; **Test
     users**: the organisation's Google account, yours. (Testing status: only test users can sign
     in; refresh tokens do not expire with only these scopes; the consent screen shows an
     "unverified app" warning that test users can pass.)
   - **Data Access**: scopes `openid` and `.../auth/userinfo.email` (`.../auth/userinfo.profile`
     only if you want names in the audit log — not required). Leave the "AI-powered agent" box
     unticked.
3. **Google Auth Platform → Clients → Create client**: type *Web application*; name
   « tipis-data-mcp »; **Authorized redirect URIs**: `https://data-mcp.tipis.com/auth/callback`.
   No JavaScript origin needed.
4. Copy the client id (`…apps.googleusercontent.com`) and the secret (`GOCSPX-…`) into the
   tipis-data-mcp vault (`datamcp_google_client_id`, `datamcp_google_client_secret`) **at once**:
   the console shows the secret only when it is created (download the JSON if you need a copy).
5. Later, to add a coworker's own Google account: add it as a test user (Audience) **and** to
   `datamcp_allowed_emails` (`--tags allowlist`).

Rotation: Clients → the client → *Add secret* (shown once: straight into the vault) →
`--tags secrets,app` → then disable and delete the old secret. Existing claude.ai connections keep
working (the proxy holds the upstream refresh tokens); if Google invalidates them, users reconnect
once.
