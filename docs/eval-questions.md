# Eval sheet — what the model does with the connector

Twelve questions a coworker really asks, with the expected tool path and reference figures computed
read-only through the connector on **2026-10-10 (~19:30 UTC)**. Re-run them in claude.ai (new chat,
connector on, no Project) after every change to the instructions, the tool descriptions, the domain
rules or the dataset descriptions, then compare the audit log (`logs/datamcp.jsonl`: calls per
question, `query_failed`, `query_refused`) and the figures. The figures move every night: a re-run
another day checks the path and the method, and the order of magnitude.

Scoring per question: **calls** (tool calls before the answer), **errors** (failed calls),
**right** (figures and caveats as below), **as of** (the data date is quoted).

| # | Question (paste as is) | Expected path | Reference (2026-10-10) | Traps |
|---|---|---|---|---|
| 1 | Liste les 15 villes avec le plus d'annonces en ligne actuellement. | `top_cities(limit=15)` — 1 call | Paris 466, Lyon 410, Toulouse 388, Marseille 354, Bordeaux 316, Rennes 260, Nantes 248, Grenoble 242, Montpellier 195, Villeurbanne 186, Strasbourg 174, Lille 145, Angers 121, Saint-Étienne 110, Mérignac 106 (nightly count of 2026-10-09) | before the fix the default call failed (duplicate column) |
| 2 | Combien d'annonces sont en ligne en France aujourd'hui, et quelle part vient des flux partenaires ? | `query` on `listings` with the online rule — 1–3 calls | 11,357 online (search rule), 3,416 from feeds = 30 %; France page count 11,162 (2026-10-09) | adding `desactivated_at IS NULL` drops 123 live listings |
| 3 | Inscriptions par mois depuis janvier 2024. | `query` on `monthly_event_counts`, action `USER_CREATED` — 1–2 calls | 2024-01 7,624 · 2024-07 15,928 · 2025-07 17,767 · 2026-08 19,835 · 2026-09 13,694 · 2026-10 partial | `users.created_at` is 7–9 % lower (deleted accounts); October is partial |
| 4 | Fais-moi un tableau de bord de l'activité des 24 derniers mois, en artifact : inscriptions, annonces créées, conversations, messages. | one `query` on `monthly_event_counts` (4 actions, pivot) | 2026-09: 13,694 sign-ups · 6,126 listings created · 261,434 conversations · 478,151 messages | sum over `canonical_domain`; flag the partial month; listings created = `LISTING_CREATED`, not `monthly_new_listings` (about 3 in 10 survive) |
| 5 | Montre l'historique depuis 2013 des inscriptions et des annonces créées, en échelle logarithmique. | one `query` on `monthly_event_counts` | USER_CREATED from 2013-07-04; LISTING_CREATED only from 2015-08-31 | must say the listing series starts in 2015, not draw zeros |
| 6 | Quel est le loyer médian au m² à Paris, Lyon, Bordeaux, Toulouse et Lille, et comment a-t-il évolué sur 24 mois ? | `top_cities(metric=median_rent_per_m2)` (or `city_figures` ×5) for today + `rent_trend(city, 24)` ×5 for the history | Paris 57.66 € (2,610 rents) · Lyon 42.86 € (1,897) · Bordeaux 50.34 € (1,298) · Toulouse 39.66 € (1,864) · Lille 41.15 € (540) | quote the page's 365-day figure for today, the monthly medians only with their samples (Lyon 2025-09: 25 rents, no median; 2026-06: 42.31 € on 289); older months shrink (deleted listings vanish); recomputing the 365-day figure gives Lyon 42.76 € (1,954) ≠ the site's 42.86 € |
| 7 | CA mensuel HT des 12 derniers mois, par type (options, abonnements, dons). | one `query` on `monthly_revenue` | 2025-10 8,176.04 € … 2026-06 33,411.80 € · 2026-09 22,853.54 € · 2026-10 partial | gross (refunds not deducted); `lower(payment_status)` if built from `transactions` |
| 8 | Quelles requêtes Google ont apporté le plus de clics en septembre 2026 ? Top 10. | one `query` on `search_console_queries` | « la carte des colocs » 54,612 clics · « carte des colocs » 16,554 · « lacartedescolocs » 6,888 · … · « colocation paris » 1,626 (CTR 16.6 %) | queries carry no page (« sur nos guides » cannot be filtered) |
| 9 | Volumes des flux partenaires sur les 6 derniers mois. | `feed_volumes(months=6)` — 1 call | 2026-09: website/lacartedescolocs 138 published, locservice 619 published | current month partial |
| 10 | Donne-moi les chiffres clés de Bordeaux pour un article. | `city_figures("Bordeaux")` — 1 call | 316 annonces en ligne (2026-10-09), médiane 50.34 €/m² sur 1,298 loyers (calculée le 2026-09-20), methodology line | quote the dates and the sample |
| 11 | Combien de messages envoyés par jour sur les 30 derniers jours ? | one `query` on `daily_event_counts`, `MESSAGE_CREATED`, `GROUP BY day` | 2026-09-09 → 2026-10-08: 415,163 messages = 13,839 per day | one row per day **and domain**: `avg(events_count)` gives 6,919 (wrong); latest day = 2026-10-08, not yesterday |
| 12 | Combien de profils de locataires actifs, par tranche d'âge ? | one `query` on `users` with the online-profile rule | 19,915 active: 18–24 9,507 · 25–34 8,376 · 35–44 1,271 · 45–54 453 · 55–64 169 · under 18 81 · 65+ 60 | groups under 30 (under 18, 65+) not publishable as such |

## Results

Before = the calls of 2026-10-10 17:34–18:17 UTC in the audit log (the questions themselves are not
logged; the paths are). Raphaël attached nothing: the model read `data://dictionary` on its own, which
claude.ai web cannot do, so these were most likely a Claude Code session, not claude.ai. Calls from
18:56 on are the audit session itself. The "After" run must be done in claude.ai to measure it.

| # | Before (claude.ai, audit log 2026-10-10) | After |
|---|---|---|
| 5 (history since 2013) | 15 calls, 0 errors: list_datasets → describe monthly_event_counts → 4 queries → describe daily_event_counts → 4 queries probing where each series starts and its spikes → `data://dictionary` read once, mid-way → 1 more probe → describe + 1 query on monthly_new_listings. Sums over domains done right. ~5 calls were the model rediscovering series start dates; the organic / imported split per year came from `monthly_new_listings`, i.e. surviving listings only (2024: 25,409 rows vs 94,432 created) | |
| 1 (city ranking) | 3 calls, 0 errors: describe city_figures → query top 25 → a count query. `top_cities` never called (the broken default was not hit by luck) | |
| — (sign-ups today, Paris time) | 8 calls, 0 errors: list_datasets → describe users + events → 5 queries, two on raw `events` with a narrow `enqueued_at` range (33–130 ms) | |
| 2–4, 6–12 | not asked yet | |
| audit session (Claude Code, 18:58–19:13) | `top_cities` default failed (duplicate column); 1 ambiguous-column error of my own | |

Reading: list_datasets opened both sessions (the rules now ride on it); the dictionary was read once,
after nine queries; the city question went to raw SQL rather than the dedicated tool.

## What each product shows the model (checked 2026-10-10)

- claude.ai drops the server `instructions` (open issue, Anthropic « tracking in our backlog »):
  https://github.com/anthropics/claude-ai-mcp/issues/93
- Only the user can attach a resource; no model-side read planned short term:
  https://github.com/anthropics/claude-ai-mcp/issues/23
- Prompts of custom connectors may not appear anywhere (reproduced 2026-08-24):
  https://github.com/anthropics/claude-ai-mcp/issues/333
- Every tool needs a `title` and its hints; « read-only tools can run without per-call
  confirmation »: https://claude.com/docs/connectors/building/review-criteria — permission groups:
  https://support.claude.com/en/articles/11176164-use-connectors-to-extend-claude-s-capabilities
- Approvals keyed on name + a hash of the descriptions (client code read by a user, undocumented):
  https://github.com/anthropics/claude-ai-mcp/issues/1032
- Result size ≈ 150 000 characters, 240 s per call: https://claude.com/docs/connectors/building/index
- Writing tools for agents: https://www.anthropic.com/engineering/writing-tools-for-agents and
  https://platform.claude.com/docs/en/agents-and-tools/tool-use/define-tools
- ChatGPT reads the first 512 characters of the instructions; the hints are required:
  https://developers.openai.com/plugins/build/mcp-server
