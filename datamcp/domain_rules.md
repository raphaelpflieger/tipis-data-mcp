## Domain rules (hand-written, read this before writing SQL)

- **Two databases.** `primary` = the site's own data (members, listings, chats, payments, guides,
  geography). `metrics` = the event stream of everything that happens on the site (one row per
  action, since 2013) plus its daily counts. A query runs on one database at a time; join across
  the two by ids in your own reasoning, not in SQL.
- **Everything lives in the `analytics` schema** (views), written `analytics.listings` or just
  `listings` (the search path is set). `public.*` is not readable, by design.
- **Pseudonymous personal records.** Identifiers and relationships are retained for authorized
  internal queries and joins; this is not anonymous data. Direct contact fields and message /
  profile text are excluded. Individual rows may be used for internal analysis, never published.
  The editorial minimum of 30 observations is guidance, not an enforced SQL rule or a guarantee
  of anonymization. Review rare groups and linkage risks before publishing.
- **"Online" listing** = `published AND NOT blacklisted AND (validated OR auto_validated) AND
  desactivated_at IS NULL`. The nightly counts in `viewport_daily_stats` and `city_figures`
  already apply it: prefer them for "how many listings" questions.
- **Amounts** are monthly and in `currency_code` (EUR for France; CHF, CAD elsewhere).
  `cost_total_rent` is the price shown (charges included). Payments: `transactions` with
  `lower(payment_status) = 'completed'` and `credit_note_mode = false`; `canonical_*` amounts
  are converted to EUR.
- **Timestamps are UTC** in `metrics` (`enqueued_at`) and in `created_at`-style columns; French
  calendar days use Europe/Paris: local midnight is 23:00/22:00 UTC on the previous day. Dates (`date`, `day`, `snapshot_on`) are calendar days.
- **Brands.** `canonical_domain` / `published_brand`: `lacartedescolocs.fr` (France, historic),
  `lacartedescolocs.com`, and the `tipis.*` domains (international, 2026). `localhost` rows are
  development noise: exclude them.
- **Geography.** `viewports` are the areas of the site (country, region = `administrative`,
  county, city, district) keyed by `canonical_path` (e.g. `/fr/auvergne-rhone-alpes/lyon`).
  `viewport_ancestries` links a city to its parents. Listing locations are rounded to ≈ 100 m
  (`coordinates_lat/lon`) and carry H3 cells (`h3_r7`, `h3_r8`) for hexagon maps.
- **City figures.** `city_figures` (latest day) and `city_figures_snapshots` (history) copy what
  the guides' city pages show: listings and tenant profiles online, median rent per m² over
  365 days with its sample size (published only from 30 listings), tenant budget, members, and
  a 12-month trend. Quote these in articles; they cannot contradict the site.
- **Events (`metrics.events`).** `action` is one of 52 codes; the common ones: LISTING_VISITED,
  PROFILE_VISITED, LISTINGS_SEARCH, PROFILES_SEARCH, LISTING_CONTACTED, PROFILE_CONTACTED,
  CONVERSATION_CREATED, MESSAGE_CREATED, LISTING_CREATED, LISTING_VALIDATED,
  LISTING_AUTO_VALIDATED, LISTING_DESACTIVATED, LISTING_DESTROYED, LISTING_NUMBER_SHOWN,
  PROFILE_CREATED, PROFILE_ACTIVATED, USER_CREATED, USER_SIGNED_IN, USER_CONFIRMED_EMAIL,
  USER_BLACKLISTED, EMAIL_ALERT_CREATED, EMAIL_ALERT_ACTIVATED, TRANSACTION_CREATED,
  XML_FEED_CREATED, PRO_PROFILE_VISITED, PRO_PROFILE_CONTACTED. `resource_data` keeps only
  explicitly typed pseudonymous attributes of the resource at that time. The table is 200 M rows: always filter
  `enqueued_at` on a range, and prefer `daily_event_counts` / `monthly_event_counts` for series.
- **Web analytics.** Page-level visitor and visit counts are not additive unique people or
  site-wide sessions. The traffic tool covers tracked viewport pages only.
  `viewport_plausible_daily_stats` (visitors per area page per day, by source),
  `viewport_plausible_origin_daily_stats` (where visitors come from), `search_console_queries` /
  `search_console_pages` (Google Search), `google_ads_*` (paid), `competitor_daily_stats`
  (competitors' public rankings).
- **Partners.** `xml_feeds` are agencies' feeds importing listings (`from_xml_feed = true`);
  `xml_feed_daily_stats` / `monthly_feed_volumes` their volumes.
- **Moderation and fraud** tables carry scores, decisions and costs, never the texts.
- **Methodology line for every article:** « Méthodologie : chiffres issus de la base de données
  de La Carte des Colocs (annonces en ligne, profils, loyers déclarés dans les annonces), au
  <date>. Une médiane de loyer n'est publiée qu'à partir de 30 annonces sur 365 jours. »
