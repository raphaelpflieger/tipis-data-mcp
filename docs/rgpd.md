# Note RGPD — connecteur privé « Données LCDC »

Version du 25 septembre 2026. Responsable : l’éditeur de La Carte des Colocs (Raphaël
Pflieger). Décision du responsable : utiliser ses propres comptes Claude et ChatGPT pour
l’analyse interne de ses bases, avec accès nominatif aux seules identités Google autorisées.

## Nature des données et finalité

Le schéma `analytics` contient des **données personnelles pseudonymisées**, ainsi que des
statistiques agrégées. Il n’est pas anonyme : les identifiants stables, relations entre membres,
horodatages, montants, scores et localisations arrondies permettent des rapprochements et
l’individualisation. L’absence de noms ne supprime pas ce caractère personnel.
Voir la [distinction de la CNIL](https://www.cnil.fr/fr/technologies/lanonymisation-de-donnees-personnelles).

Les comptes autorisés peuvent exécuter des SELECT et consulter des lignes individuelles pour
leurs analyses internes. Les résultats sont transmis au fournisseur du compte utilisé et
peuvent être conservés dans la conversation : les données ne restent donc pas uniquement
sur nos serveurs. Le connecteur ne prend aucune décision et ne modifie aucune donnée source.
Les usages prévus sont le pilotage de l’activité et la préparation de contenus éditoriaux.

La base envisagée est l’intérêt légitime (article 6.1.f). Le responsable doit documenter le
besoin, la minimisation et la mise en balance pour ce traitement de données personnelles ;
cette note ne considère pas ces conditions satisfaites du seul fait de la pseudonymisation
ou de l’utilisation de comptes appartenant à l’éditeur.

## Périmètre et minimisation

`tipis-web/db/analytics_allowlist.yml` classe chaque table et colonne. Une nouvelle colonne
reste exclue jusqu’à classification. Les champs directs de contact, noms, secrets, adresses
IP, empreintes, adresses postales et de facturation, identifiants de paiement, médias et textes
libres d’annonces, profils et messages sont exclus. La date de naissance devient une tranche
d’âge ; les coordonnées sont arrondies. Ces mesures réduisent l’exposition sans anonymiser
les personnes. Les données d’activité, de modération et les relations entre membres restent
personnelles même sans le texte des échanges.

Les filtres sauvegardés et attributs JSON des événements sont reconstruits à partir de
champs typés et d’énumérations explicites, y compris dans les objets imbriqués ; les noms de
lieux, segments d’URL et fuseaux horaires passent par un motif (longueur bornée, aucun
caractère d’adresse électronique, de balisage ou de requête). Les clés inconnues et valeurs
d’un autre type sont écartées. Les rapports marketing agrégés provenant
de Google restent exposés selon l’allowlist et nécessitent la même prudence à la publication.

Une publication externe ne doit contenir aucune ligne individuelle ni permettre d’identifier
un membre. Le seuil éditorial de 30 observations est une règle de publication, pas une
restriction SQL ni une preuve suffisante d’anonymisation : il faut aussi examiner les petits
groupes, attributs rares et rapprochements possibles.

## Comptes, fournisseurs et conservation

L’accès est réservé aux comptes Google nommément autorisés par l’éditeur. Une connexion
Claude/ChatGPT ne doit pas être partagée avec une personne extérieure à ce périmètre. Retirer
une identité de l’allowlist révoque ses prochains appels et rafraîchissements de jeton ; cela
n’efface pas les résultats déjà présents dans une conversation.

Les conditions du fournisseur dépendent du plan réellement utilisé. Posséder un abonnement
Pro/Max n’implique ni contrat professionnel de sous-traitance, ni résidence européenne, ni
absence de conservation. Le responsable conserve la référence du plan et de ses conditions,
vérifie les paramètres d’amélioration des modèles sur chaque compte, les destinataires et
transferts applicables, et utilise uniquement l’arrangement approprié à son traitement.
Les paramètres des comptes ne sont pas configurés ou vérifiés par ce dépôt.

Pour Claude Free/Pro/Max, désactiver l’amélioration du modèle n’instaure **pas** une suppression
automatique des conversations après 30 jours. Les conversations restent dans l’historique
jusqu’à suppression ; Anthropic décrit une suppression des systèmes backend sous 30 jours
après cette action, avec exceptions. Voir sa [politique de suppression](https://privacy.claude.com/en/articles/7996878-can-you-delete-data-sent-via-claude).
Pour ChatGPT, vérifier les [contrôles des données](https://help.openai.com/en/articles/7730893-data-controls-faq)
et les conditions du plan avant activation ; ne pas transposer les délais de Claude.

Les conversations contenant des extraits individuels doivent être supprimées lorsque le
besoin d’analyse est terminé. Les exports et publications constituent des copies distinctes.
Un effacement dans la base source actualise immédiatement les vues, mais pas ces copies ni
les historiques chez les fournisseurs ; les demandes de droits doivent en tenir compte.
Le journal du connecteur contient l’identité de l’opérateur et la requête SQL (potentiellement
ses littéraux), jamais les lignes retournées, avec 12 rotations hebdomadaires.

## Contrôles techniques

- OAuth Google avec PKCE et liste d’identités vérifiée à la délivrance, au rafraîchissement et
  à chaque appel. Les redirections de clients sont explicitement autorisées.
- Routes OAuth et métadonnées accessibles aux navigateurs ; `/mcp` restreint aux réseaux
  Anthropic/OpenAI et administrateurs ; `/healthz` réservé aux administrateurs.
- Rôle PostgreSQL limité en lecture au schéma `analytics`, TLS avec `verify-full`, garde SQL
  fondée sur le parseur PostgreSQL. Aucun accès direct aux tables métier `public`.
- `analytics._contract` marque toute synchronisation en attente. Les appels de données et la
  santé vérifient le contrat, les relations et colonnes exposées et les privilèges effectifs.
  Une synchronisation échouée ou une dérive interrompt l’accès aux données.
- Une opération à la fois par identité, plafond global, échéance par opération et requête,
  résultats limités en lignes et taille, transfert incrémental et limites mémoire du conteneur.
  Le SQL arbitraire reste coûteux côté PostgreSQL ; utiliser des plages de dates et agrégats.
- Audit, révocation par l’allowlist, interrupteur applicatif, arrêt du conteneur et refus nginx.

## Registre

**Traitement :** analyse interne de données pseudonymisées via assistants IA, et préparation
de statistiques éditoriales. **Personnes concernées :** membres du site et opérateurs.
**Destinataires :** identités internes autorisées, fournisseurs des comptes Claude/ChatGPT
selon les contrats applicables. **Base envisagée :** intérêt légitime, mise en balance documentée.
**Conservation :** bases sources selon leur politique ; audit 12 rotations hebdomadaires ;
conversations et exports selon le besoin documenté et les conditions effectives des fournisseurs.
