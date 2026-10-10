# tipis-data-mcp — the coworkers' data connector

A read-only [MCP](https://modelcontextprotocol.io) server that lets the two coworkers of La Carte
des Colocs use their own Claude or ChatGPT accounts (custom connector, Google sign-in) for any table, chart or article built
from the production databases — through the **`analytics` schema only**: one PostgreSQL view per
table with minimized **pseudonymous personal data** (direct names, e-mails, phones, addresses, IPs, free text,
pictures, payment ids and birth dates excluded), maintained by tipis-web (`db/analytics_allowlist.yml`,
`rails analytics:sync_views`, skill `analytics-schema`).

```
Claude / ChatGPT (authorized Google identity) ──HTTPS──▶ nginx on etat-major (MCP: provider egress + admins; OAuth: browsers)
                                             └─▶ tipis-data-mcp container (127.0.0.1:8765, FastMCP 4)
                                                    ├─ psycopg → tipis-db-1 10.88.0.1:5432 (WireGuard, TLS, role tipis_analytics)
                                                    └─ psycopg → tipis-photos 51.210.105.166:16452 (TLS, role tipis_analytics)
```

Safety envelope, each layer independent: provider-egress IP allowlist on /mcp → Google OAuth
(client in *Testing* status, test users only) → e-mail allowlist (at the token endpoint and on
every call, hot-reloaded) → read-only role that sees only `analytics.*` → SQL guard (pglast) →
whole-operation deadline, 1000 rows, 60 000 characters → audit log. Database calls also verify
the committed analytics contract and reader privileges; a failed view rebuild closes access. The service never calls the
Anthropic or OpenAI API: the account provider runs the model. No API key anywhere.

## Layout

| Path | What |
|---|---|
| `datamcp/config.py` | every setting (env `DATAMCP_*`, secrets as files) |
| `datamcp/auth.py` | `AllowlistedGoogleProvider` (refuses strangers at the token endpoint) + `AllowlistMiddleware` |
| `datamcp/guard.py` | the SQL guard (one SELECT, analytics schema only, forbidden functions) |
| `datamcp/db.py` | pools, READ ONLY + statement_timeout + cancel, CSV, throttle |
| `datamcp/catalog.py`, `dictionary.py`, `domain_rules.md` | what the schema holds, the `data://dictionary` resource |
| `datamcp/tools.py`, `prompts.py` | the tools (`list_datasets`, `describe_dataset`, `query`, `explain_query`, `city_figures`, `rent_trend`, `top_cities`, `traffic_series`, `feed_volumes`) and the French prompts |
| `datamcp/server.py` | assembly, `/healthz`, entrypoint |
| `datamcp/cli.py` | `check-drift`: the live views vs tipis-web's allowlist |
| `tests/` | pytest with a throwaway PostgreSQL (`make test`) |
| `ansible/` | deploy to etat-major (nginx vhost, certbot, secrets, compose, logrotate) |
| `docs/rgpd.md`, `docs/guide-coequipiers.md` | the RGPD note (with the register entry) and the coworkers' one-page guide |
| `docs/eval-questions.md` | twelve real questions with their expected tool path and reference figures |

## Deploy (Raphaël, from the checkout on the dev box)

Prerequisites, in this order (see the tipis-web skill `analytics-schema` for the whole rollout):

1. tipis-postgres: role `tipis_analytics` + schema + pg_hba (`--tags postgres`); tipis-metrics:
   role + TLS + pg_hba (`--tags config,activate`, commits `ansible/files/tipis-metrics-ca.crt`).
2. tipis-web deploy (migrations + `rails analytics:sync_views`).
3. DNS: `data-mcp.tipis.com` A record → 162.19.24.164, **grey cloud** (no Cloudflare proxy: the
   OAuth endpoints must answer within 10 s and the IP allowlist is nginx's).
4. Google Cloud: an OAuth client (see `docs/google-oauth.md`).
5. `cp ansible/ansible_secrets.example.yml ansible/ansible_secrets.yml`, fill, `ansible-vault encrypt`.

Then:

```bash
cd ansible && ansible-playbook -i hosts main.yml --ask-vault-pass
```

The play clones this repo on the box, renders `.env`, writes the secrets (0400, owner 10001),
copies both cluster CAs, builds and starts the container, installs the nginx vhost, obtains the
certificate, installs logrotate. Re-running it is idempotent; `--tags app` redeploys the
container only, `--tags nginx` the vhost only, `--tags allowlist` the e-mail list only.

Post-deploy checks (from an allowed admin IP):

```bash
curl -sS https://data-mcp.tipis.com/healthz                       # {"ok": true, ...}
curl -sS -D - -o /dev/null -X POST https://data-mcp.tipis.com/mcp -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
                         # 401 + WWW-Authenticate: Bearer resource_metadata=… (a GET or HEAD answers 405)
curl -sS https://data-mcp.tipis.com/.well-known/oauth-protected-resource/mcp
make check-drift   # on the box, with tipis-web's db/analytics_allowlist.yml copied next to the compose file
npx @modelcontextprotocol/inspector   # URL https://data-mcp.tipis.com/mcp, Streamable HTTP, OAuth → Google consent
```

## Claude and ChatGPT accounts

Settings → Connectors → *Add custom connector* → name « Données LCDC », URL
`https://data-mcp.tipis.com/mcp` → Add (claude.ai registers itself through DCR; no client id to
paste) → *Connect* → Google consent with the organisation's Google account → back in claude.ai,
open the connector's tool settings and set the **Read-only tools** group (all 9 tools carry
`readOnlyHint`) to **Always allow**. claude.ai appears to key each approval on the tool's name *and
a hash of its description and parameter descriptions* (read from its client code, not documented): a
release that rewords a tool sends it back to "needs approval" (set it again once), and the tool list
is cached for hours (connector settings → refresh the tools list). Batch wording changes into one
release. A client still holding the old list can also break: after the 2026-10-10 release dropped the
`outputSchema`, every call answered « Tool … has an output schema but did not return structured
content » until the list was refreshed (claude.ai connector settings; Claude Code `/mcp` → reconnect).
Refresh every client right after a release that changes a tool's schema. Optional: a Project
« Données LCDC » whose instructions are `docs/guide-coequipiers.md`.

What the model sees (checked 2026-10-10, sources in `docs/eval-questions.md`):

- claude.ai (web, desktop) does **not** pass the server `instructions` to the model (Claude Code
  does; ChatGPT reads their first 512 characters): every rule that matters is in the tool
  descriptions and in what `list_datasets` / `describe_dataset` return.
- The model cannot read a resource itself: `data://dictionary` reaches it only when the user
  attaches it (« + » → the connector). Prompts may not show in claude.ai for a custom connector:
  the guide tells the coworkers to ask for the same report in plain words.
- Connector tools appear to be deferred (user reports, undocumented): the model sees their names,
  then loads a description through its tool search. Results reach it as the text content only
  (no `structuredContent`: `output_schema=None`).
- `docs/eval-questions.md` is the eval sheet: re-run it after any change to what the model reads.

For ChatGPT, enable the account’s custom MCP connection and register this server with OAuth.
Copy the **exact** redirect URI shown in its management page to `datamcp_chatgpt_redirect_uri`
and redeploy the settings. The stable callback is configured by default; some registrations
instead use `https://chatgpt.com/connector/oauth/{callback_id}`. Do not wildcard that path.
See [OpenAI authentication](https://developers.openai.com/plugins/build/auth).

Verify sign-in from a browser outside the administrator IP allowlist, in both products.
The Google account must remain in `datamcp_allowed_emails`. Use only the intended owned
accounts; inspect training/retention settings for each plan. `docs/rgpd.md` documents the
pseudonymous processing decision and the copies created in provider conversations.

## Runbook

- **Add / remove an account**: edit `datamcp_allowed_emails` in the vault, run the play with
  `--tags allowlist` (or edit `secrets/allowed_emails.txt` on the box). Revocation is immediate at
  the next call; a removed account also loses its refresh. New accounts must also be *test users*
  of the Google OAuth client (Testing status caps at 100).
- **Kill switches**, from soft to hard: `DATAMCP_ENABLED=0` in `.env` + `docker compose up -d`
  (tools answer "disabled", auth and healthz keep working); empty the allowlist; `docker compose
  stop`; set `datamcp_deny_all: true` and run `--tags nginx` (nginx answers 403 to everyone).
- **Rotate a secret**: Google client secret (Console → the client → *Reset secret*, then vault +
  `--tags secrets,app`); `datamcp_jwt_signing_key` (every client re-authenticates: expected);
  `datamcp_storage_key` (the OAuth state becomes unreadable → clients re-register, harmless);
  DB passwords (change in the DB repo's vault first, deploy it, then here). The play restarts the
  container after a secret change.
- **Upgrade FastMCP**: bump `fastmcp==` in `pyproject.toml`, `make lock`, `make test`, read the
  release notes for the OAuth proxy (`_extract_upstream_claims`, `_token_validator`,
  `_upstream_*_endpoint` are the private members `datamcp/auth.py` and the tests rely on),
  deploy with `--tags app`.
- **Logs**: `docker compose logs -f` (process), `logs/datamcp.jsonl` (audit: one line per call,
  12 weekly rotations), `/var/log/nginx/data-mcp.*.log`.
- **Restore**: nothing to restore beyond the repo, the vault and the DNS record; the OAuth state
  volume is a cache (clients re-register).
- **Troubleshooting**:
  - claude.ai says *Couldn't reach the MCP server*: nginx allow list (the request must come from
    Anthropic’s published range or OpenAI’s current connector ranges; refresh via `--tags nginx`), certificate, container up (`/healthz`), OAuth metadata answering in < 10 s.
  - 401 loops after connecting: the account is not in the allowlist (audit `auth_refused`), or
    the token signing key changed (reconnect), or `email_verified` is false on that Google account.
  - Google *Access blocked: app not verified* / *403 access_denied*: the account is not a test
    user of the OAuth client, or the redirect URI differs from `https://data-mcp.tipis.com/auth/callback`.
  - Token endpoint slower than 10 s: the Google token exchange or the file store is slow; check
    `docker compose logs`, the box load, and that `/data` is a local volume.
  - `query refused: …` in a chat: by design (see `datamcp/guard.py`); the message says what to do.
  - Unhealthy analytics contract: rerun `rails analytics:sync_views`, check reader grants,
    and run `make check-drift`. Do not bypass the check or update `_contract` by hand.
  - Timeouts on `metrics.events`: filter `enqueued_at`, use `daily_event_counts`.
  - `describe_dataset` is schema-only by default. Optional value catalogues sample 5000 rows
    for at most eight columns; their counts are not population totals.
  - `traffic_series` covers tracked viewport pages; page-level visitors/visits are not unique
    site-wide people/sessions. Fetch a real site-level Plausible aggregate for those questions.
  - The container is limited to 512 MiB/one CPU. Query timeouts and role settings limit work,
    but arbitrary SQL is not a hard memory sandbox for PostgreSQL itself. Keep this connector
    restricted to the authorized operators; consider a replica before expanding its audience.
    SELECT rows are bounded before transfer. EXPLAIN plans stream row by row with capped
    retained output, but PostgreSQL still sends any individual oversized plan row in full.

## Local development

```bash
make lock          # after editing pyproject.toml
make test          # pytest in Docker with a throwaway PostgreSQL
```

The tests cover the guard (60+ cases including bypass attempts), the allowlist hot reload, the
tools over HTTP (CSV, refusals, caps, cancel, kill switch, audit), the OAuth round trip against a
fake Google (allowed / stranger refused at the token endpoint / removed at refresh) and the drift check.
