# DQA Kobo → Neon — exécution planifiée (gratuite, sans Colab Pro)

Ce dossier contient une version "script" du notebook, pensée pour tourner
toute seule sur GitHub Actions (gratuit pour les dépôts publics, et généreux
en quota pour les dépôts privés) — sans avoir à cliquer sur "Exécuter" dans Colab.

## Fichiers

- `dqa_kobo_to_neon.py` — le pipeline complet (étapes 3 à 7 du notebook),
  piloté uniquement par variables d'environnement.
- `requirements.txt` — dépendances Python.
- `.github/workflows/dqa_scheduled.yml` — planifie l'exécution (par défaut : toutes les heures).

## Mise en place (5 minutes)

1. Crée un dépôt GitHub (public ou privé, peu importe) et mets-y ces 3 fichiers/dossiers.
2. Dans le dépôt : **Settings → Secrets and variables → Actions → New repository secret**,
   ajoute 4 secrets :
   - `KOBO_SERVER` (ex: `kf.kobotoolbox.org`)
   - `KOBO_ASSET_UID`
   - `KOBO_API_TOKEN`
   - `NEON_DATABASE_URL` (utilise l'URI **directe**, pas `-pooler` — GitHub Actions
     ouvre une seule connexion courte par run, la connexion directe convient très bien)
3. Va dans l'onglet **Actions** du dépôt → le workflow "DQA Kobo -> Neon (planifié)"
   apparaît automatiquement (GitHub Actions lit `.github/workflows/*.yml`).
4. Clique sur **Run workflow** pour tester une première exécution manuelle.
5. Vérifie les logs (icône ✅/❌ à côté du run) — tu dois voir le même résumé
   que dans le notebook (`SCORE GLOBAL`, `✅ Run #... envoyé vers Neon`).
6. Si tout est vert, laisse faire : le workflow se relance ensuite tout seul
   selon le `cron` défini dans le fichier `.yml` (toutes les heures par défaut).

## Ajuster la fréquence

Dans `.github/workflows/dqa_scheduled.yml`, modifie la ligne `cron`.
Quelques exemples (heure UTC) :
- `"*/30 * * * *"` → toutes les 30 minutes
- `"0 */6 * * *"` → toutes les 6 heures
- `"0 6,18 * * *"` → 2 fois par jour, à 6h et 18h UTC

⚠️ GitHub Actions ne garantit pas la minute exacte sur les crons très fréquents
(`* * * * *`) — un léger décalage de quelques minutes est normal, sans
conséquence ici.

## Notes

- Aucun secret n'est jamais écrit en clair dans le code : ils sont injectés
  comme variables d'environnement au moment du run, via `secrets.*` dans le
  YAML (visibles seulement par le workflow, jamais dans les logs).
- Le dashboard visuel (matplotlib) du notebook est volontairement absent du
  script : il n'y a pas d'écran en exécution planifiée. Le score reste
  consultable dans Power BI (table `dqa_runs`) ou dans les logs du run GitHub Actions.
- Pour du vrai temps réel côté Power BI, combine ceci avec le mode
  **DirectQuery** ou une **actualisation planifiée** alignée sur la même fréquence.
