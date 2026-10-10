"""The recurring formats, in French, as MCP prompts (the coworkers pick them in claude.ai)."""

from __future__ import annotations

METHODOLOGY = (
    "Méthodologie : chiffres issus de la base de données de La Carte des Colocs "
    "(annonces en ligne, profils, loyers déclarés dans les annonces), au {date}. "
    "Une médiane de loyer n'est publiée qu'à partir de 30 annonces sur 365 jours."
)


def rapport_ville(ville: str) -> str:
    return f"""Prépare un rapport chiffré sur le marché de la colocation à {ville}, en français, pour un article du guide.

Étapes :
1. Appelle `city_figures` avec « {ville} » : ce sont les chiffres affichés sur la page ville du site, ils font foi.
2. Complète avec `rent_trend` (évolution) et, si utile, `query` sur `analytics.monthly_city_listings` (annonces en ligne par mois) et `analytics.viewport_plausible_daily_stats` (audience de la page).
3. Rédige : un paragraphe d'introduction, un tableau des chiffres clés (avec la date « au » et le nombre d'annonces qui fondent la médiane), 2 à 3 constats sourcés, une phrase de méthodologie.
4. Ne cite jamais une annonce, un membre ou une adresse : uniquement des agrégats.

Termine par la ligne de méthodologie à coller dans l'article :
{METHODOLOGY.replace('{date}', '<date du jour>')}"""


def barometre_loyers(zone: str = "France") -> str:
    return f"""Construis un baromètre des loyers en colocation pour « {zone} », en français.

Données :
- `top_cities` (metric=median_rent_per_m2) pour le classement des villes : la médiane affichée sur la page ville, sa date de calcul et le nombre de loyers derrière chaque médiane ;
- `query` pour une médiane par type de logement ou par département si besoin, avec la méthode du site : loyer de chaque chambre (`analytics.rooms`) divisé par sa surface (≥ 5 m²), annonces organiques (`from_xml_feed = false`, non blacklistées, loyer mensuel) créées sur les 365 derniers jours, `percentile_cont(0.5)`, et au moins 30 loyers par groupe.

Format : un tableau trié, une lecture en 3 points, les limites (échantillon, loyers déclarés charges comprises, annonces créées sur un an et pas seulement en ligne), puis la ligne de méthodologie :
{METHODOLOGY.replace('{date}', '<date du jour>')}"""


def fiche_chiffres_article(sujet: str) -> str:
    return f"""Prépare une fiche de chiffres pour un article sur « {sujet} », en français.

1. Si un outil dédié répond (`city_figures`, `top_cities`, `rent_trend`, `traffic_series`, `feed_volumes`), utilise-le ; sinon commence par `list_datasets` (ses règles : « en ligne », mois en cours partiel, dates de début des séries, fuseau UTC, marques) puis `describe_dataset` sur les jeux de données pertinents.
2. Formule 3 à 6 questions chiffrables, écris pour chacune une requête SQL (agrégats uniquement, jamais de ligne individuelle), exécute-la avec `query`.
3. Présente : question → chiffre → requête (en bloc de code) → date des données. Signale les seuils (moins de 30 observations = à ne pas publier).
4. Propose un graphique par chiffre (type et axes).

Ligne de méthodologie à coller dans l'article :
{METHODOLOGY.replace('{date}', '<date du jour>')}"""
