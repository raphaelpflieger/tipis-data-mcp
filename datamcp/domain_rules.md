## Domain rules (hand-written, read this before writing SQL)

- **Two databases.** `primary` = the site's own data (members, listings, chats, payments, guides,
  geography). `metrics` = the event stream of everything that happens on the site (one row per
  action) plus its daily counts. A query runs on one database at a time; join across the two by
  ids in your own reasoning, not in SQL.
- **Everything lives in the `analytics` schema** (views), written `analytics.listings` or just
  `listings` (the search path is set). `public.*` is not readable, by design.
- **Pseudonymous personal records.** Identifiers and relationships are retained for authorized
  internal queries and joins; this is not anonymous data. Direct contact fields and message /
  profile text are excluded. Individual rows may be used for internal analysis, never published.
  The editorial minimum of 30 observations is guidance, not an enforced SQL rule or a guarantee
  of anonymization. Review rare groups and linkage risks before publishing.
- **Which source answers what.** Ranking or key figures of cities → `top_cities` / `city_figures`
  (exactly what the city pages show); a city's rents month by month → `rent_trend`. Listings or
  profiles online in any area, today or as a daily history since 2024-03-07 →
  `viewport_daily_stats` (or `monthly_city_listings`). Activity series (sign-ups, listings
  created, messages, contacts, listing visits) → `daily_event_counts` / `monthly_event_counts`,
  never `events`; their `users_count` (distinct members) does not add up across days or domains.
  Revenue → `monthly_revenue`.
- **"Online" listing** (what the search shows) = `published AND NOT blacklisted AND (validated OR
  auto_validated)`. Do not add `desactivated_at IS NULL`: a reactivated listing keeps its old date.
  The nightly counts (`viewport_daily_stats`, `city_figures`) count the `published AND validated
  AND NOT blacklisted` listings located inside the area (≤ 0.1 % fewer): quote them as they are.
  **"Online" tenant profile** = `users.account_type = 'searching' AND profile_activated AND
  profile_validated AND NOT blacklisted`.
- **Deleted rows vanish.** Tables hold what still exists; the events keep every creation.
  Sign-ups: `USER_CREATED` counts every account created, `analytics.users` only the accounts left
  (7–9 % fewer per month) but splits by `account_type` (searching, offering, professional).
  Listings created: `LISTING_CREATED`; most listings are deleted once offline, so `listings` and
  `monthly_new_listings` keep about 3 in 10 of those created in 2024 (use them for the split
  organic / partner feeds of the listings left, never for "how many were created"). Say which one
  you used.
- **Freshness.** Monthly views include the current month, which is partial: say so or stop at
  the last complete month. `daily_event_counts` is built nightly at 01:35 Paris time for the
  previous UTC day, so its latest day is yesterday or, in summer time, the day before: check
  `max(day)` and quote it. Late events leave a day ≈ 0.04 % low. Other daily tables end
  yesterday; always quote the latest date you used.
- **History starts** (nothing before): event counts from 2013-07-04, but most actions were
  tracked later — USER_CREATED, PROFILE_CREATED, EMAIL_ALERT_CREATED since 2013; LISTING_CREATED,
  CONVERSATION_CREATED since 2015; TRANSACTION_CREATED 2016-10; LISTING_VISITED, PROFILE_VISITED,
  LISTING_CONTACTED, PROFILE_CONTACTED, USER_SIGNED_IN, LISTING_DESACTIVATED since 2020-11-20;
  LISTINGS_SEARCH, PROFILES_SEARCH 2021-08-11; MESSAGE_CREATED 2022-02-20. Check `min(day)` per
  action before drawing a long series. Rows of `listings` start 2015-08-31, `conversations` and
  `messages` 2022-06-02 (older chats are purged: use the event counts for their history),
  `viewport_daily_stats` 2024-03-07, `search_console_queries` 2024-03-13,
  `viewport_plausible_daily_stats` 2026-02-19, `city_figures_snapshots` 2026-09-28.
- **Amounts** are monthly and in `currency_code` (EUR for France; CHF, CAD elsewhere).
  `cost_total_rent` is the price shown (charges included). Payments: `transactions` with
  `lower(payment_status) = 'completed'` (both `completed` and `COMPLETED` exist) and
  `credit_note_mode = false`; `canonical_*` amounts are converted to EUR. `monthly_revenue` is
  gross (refunds not deducted); its `payment_type` is option, subscription or donation.
- **Median rent per m²** (city pages, `city_figures`): per room (room rent / room surface ≥ 5 m²;
  whole-flat rentals without rooms: rent / surface) over the organic listings (`from_xml_feed =
  false`, not blacklisted, monthly rent) **created** in the last 365 days, online or not;
  recomputed monthly; published from 30 samples. Quote it rather than recomputing it; for a
  monthly history, `rent_trend` applies the same method to the listings created each month.
- **Timestamps are UTC** in `metrics` (`enqueued_at`, `day`) and in `created_at`-style columns;
  French calendar days use Europe/Paris: local midnight is 23:00/22:00 UTC on the previous day.
  Dates (`date`, `day`, `snapshot_on`, `computed_on`) are calendar days.
- **Brands.** `canonical_domain` (events and their counts): `lacartedescolocs.fr` (France,
  historic), `lacartedescolocs.com`, and the `tipis.*` domains (international, launched 2026, a
  handful of events so far). `''` = before 2023-08-09 the domain was not recorded (La Carte des
  Colocs): never filter on the domain for totals. One malformed value (`["lacartedescolocs.fr",
  "€"]`, ≈ 1 000 events) is noise. `published_brand` (listings) holds brand keys (`lacartedescolocs`).
- **Geography.** `viewports` are the areas of the site (country, region = `administrative`,
  county, city, district) keyed by `canonical_path` (e.g. `/fr/auvergne-rhone-alpes/lyon`).
  `viewport_ancestries` links a city to its parents. `listings.utm_geotag` is the city's
  `canonical_path` without the first `/` and with `/` → `_` (`fr_auvergne-rhone-alpes_lyon`).
  Listing locations are rounded to ≈ 100 m (`coordinates_lat/lon`) and carry H3 cells (`h3_r7`,
  `h3_r8`) for hexagon maps.
- **City figures.** `city_figures` (latest day) and `city_figures_snapshots` (history) copy what
  the guides' city pages show, for the city hubs and every city with at least 30 listings online
  (≈ 70 cities): listings and tenant profiles online, median rent per m² with its sample size,
  tenant budget, members, and a 12-month trend. Quote these in articles; they cannot contradict
  the site.
- **Events (`metrics.events`).** `action` is one of 52 codes; the common ones: LISTING_VISITED,
  PROFILE_VISITED, LISTINGS_SEARCH, PROFILES_SEARCH, LISTING_CONTACTED, PROFILE_CONTACTED,
  CONVERSATION_CREATED, MESSAGE_CREATED, LISTING_CREATED, LISTING_VALIDATED,
  LISTING_AUTO_VALIDATED, LISTING_DESACTIVATED, LISTING_DESTROYED, LISTING_NUMBER_SHOWN,
  PROFILE_CREATED, PROFILE_ACTIVATED, USER_CREATED, USER_SIGNED_IN, USER_CONFIRMED_EMAIL,
  USER_BLACKLISTED, EMAIL_ALERT_CREATED, EMAIL_ALERT_ACTIVATED, TRANSACTION_CREATED,
  XML_FEED_CREATED, PRO_PROFILE_VISITED, PRO_PROFILE_CONTACTED. `resource_data` keeps only
  explicitly typed pseudonymous attributes of the resource at that time. The table is 200 M
  rows: always filter `enqueued_at` on a range, and prefer `daily_event_counts` /
  `monthly_event_counts` for series.
- **Web analytics.** Page-level visitor and visit counts are not additive unique people or
  site-wide sessions. The traffic tool covers tracked viewport pages only.
  `viewport_plausible_daily_stats` (visitors per area page per day, by source),
  `viewport_plausible_origin_daily_stats` (where visitors come from), `search_console_queries` /
  `search_console_pages` (Google Search; a query row carries no page), `google_ads_*` (paid),
  `competitor_daily_stats` (competitors' public rankings).
- **Partners.** `xml_feeds` are agencies' feeds importing listings (`from_xml_feed = true`);
  `xml_feed_daily_stats` / `monthly_feed_volumes` their volumes.
- **Moderation and fraud** tables carry scores, decisions and costs, never the texts.
- **Methodology line for every article:** « Méthodologie : chiffres issus de la base de données
  de La Carte des Colocs (annonces en ligne, profils, loyers déclarés dans les annonces), au
  <date>. Une médiane de loyer n'est publiée qu'à partir de 30 annonces sur 365 jours. »
