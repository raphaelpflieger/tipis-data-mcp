# Données LCDC — guide pour les coéquipiers (une page)

Vous utilisez votre compte Claude ou ChatGPT autorisé ; le connecteur « Données LCDC » lui donne accès, en lecture
seule, aux données pseudonymisées du site pour vos analyses internes. Les résultats transmis
au fournisseur restent des données personnelles lorsqu’ils concernent des membres. Claude écrit et exécute les requêtes ; vous posez les questions.

## Comment demander

- Dites ce que vous voulez **produire** (un tableau, un graphique, un paragraphe d'article) et
  sur quoi (une ville, une période, un type de logement). Pour les villes, Claude a des outils
  directs (`top_cities`, `city_figures`) ; sinon il liste les jeux de données (`list_datasets`, qui
  donne aussi les règles de calcul), lit la description du bon jeu (`describe_dataset`) puis
  interroge (`query`). Laissez-le faire ces étapes.
- Précisez la période (« sur les 12 derniers mois », « en septembre 2026 ») et le périmètre
  (« France », « Lyon », « annonces organiques hors flux partenaires »).
- Demandez toujours **la date des données** (« au 24 septembre 2026 ») et **l'échantillon** (« sur
  2 606 annonces »). Claude doit les citer ; sinon, demandez-les.
- Pour une ville, commencez par « donne-moi les chiffres clés de Lyon » : ce sont exactement ceux
  de la page ville du site (outil `city_figures`), ils ne peuvent pas contredire le site.
- Le mois en cours est toujours partiel, et les compteurs d'activité ont un jour ou deux de
  retard : Claude doit dire jusqu'à quelle date il compte.
- Trois formats prêts à l'emploi existent : **Rapport ville**, **Baromètre des loyers**, **Fiche
  chiffres pour un article** (menu « + » → Données LCDC, quand claude.ai les affiche ; sinon
  demandez-les en toutes lettres : « prépare un rapport chiffré sur Lyon pour un article »).

## Dix questions qui marchent

1. Quels sont les chiffres clés de Bordeaux aujourd'hui (annonces en ligne, loyer médian au m², profils) ?
2. Classe les 20 plus grandes villes par loyer médian au m², avec le nombre d'annonces derrière chaque médiane.
3. Comment a évolué le nombre d'annonces en ligne à Paris mois par mois depuis un an ?
4. Quelle part des annonces en ligne vient des flux partenaires, par pays ?
5. Quel est le loyer médian d'une chambre en colocation par département en Île-de-France (annonces en ligne, plus de 30 annonces) ?
6. Combien de nouveaux profils de locataires par mois en 2026, et quelle tranche d'âge domine ?
7. Quelles requêtes Google nous ont amené le plus de clics le mois dernier ? (les requêtes ne disent pas
   sur quelle page elles arrivent)
8. Combien de profils de locataires actifs, par tranche d'âge ?
9. Combien de messages et de contacts par jour sur les 30 derniers jours (`daily_event_counts`) ?
10. Quelles pages du guide ont perdu le plus d'impressions Google entre juillet et septembre ?

## La ligne de méthodologie à coller dans chaque article

> Méthodologie : chiffres issus de la base de données de La Carte des Colocs (annonces en ligne,
> profils, loyers déclarés dans les annonces), au <date>. Une médiane de loyer n'est publiée qu'à
> partir de 30 annonces sur 365 jours.

## Ce qui n'est volontairement pas disponible, et pourquoi

- **Contacts directs et textes privés exclus** : pas de noms, e-mails, téléphones, adresses, photos, textes de messages,
  descriptions d'annonces, adresses IP, dates de naissance (seulement une tranche d'âge),
  identifiants de paiement. Le connecteur ne les voit pas ; ce n'est pas un réglage, c'est la
  construction (RGPD, `docs/rgpd.md`).
- **Pas de ligne individuelle à publier** : les lignes pseudonymisées restent consultables
  pour vos analyses internes. Pour publier, utilisez des agrégats : au moins 30 observations,
  puis vérifiez aussi les petits groupes et les risques d’identification par rapprochement.
- **Pas d'écriture** : rien ne peut être modifié dans les bases par ce canal.
- **Des limites de taille** : 60 secondes par requête, 1 000 lignes, 60 000 caractères. Si Claude
  dit « résultat coupé », demandez un agrégat (par mois, par ville) plutôt que le détail.
- **Le texte des guides n'est pas dans le connecteur** (seulement leur longueur et leurs
  statistiques) : les pages sont publiques, collez-les si besoin.

## Si ça ne marche pas

« Couldn't reach the MCP server » ou une demande de reconnexion : reconnectez le connecteur
(Paramètres → Connecteurs → Données LCDC → Connect) avec le compte Google de l'organisation. Si
cela persiste, prévenez Raphaël : le service, sa liste d'accès ou le certificat sont peut-être
en cause.
