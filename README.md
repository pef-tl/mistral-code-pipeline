# mistral-code-pipeline

Workflow **Mistral Cloud (déploiement managé)** qui transforme une demande
développeur en Merge Request prête à valider, sur le front
`toollive-c8y` (GitLab toollive) :

```
demande dev ─▶ plan (agent) ─▶ boucle dev ▶ tests ▶ review (agents, SANS plafond)
                                                          │ (itérations jusqu'à convergence)
                                                          ▼
                                        push branche feat/... ─▶ Merge Request GitLab
                                                                    │
                                                                    ▼
                                                        validation HUMAINE (review MR)
```

Jamais de merge automatique : la MR est la gate humaine finale.

## Déroulement du workflow `change-to-mr`

1. **Demande** — l'entrée `request` est ce que le développeur veut faire
   (évolution ou correction, en langage naturel).
2. **Plan** — l'agent *planner* explore le repo cloné et produit un plan JSON
   (fichiers, approche, vérification).
3. **Itérations dev/tests** — boucle entre trois agents, **sans plafond par
   défaut** (`max_iterations` = `null` → illimité, bornée par le timeout
   d'exécution du workflow) :
   - agent *dev* : implémente / corrige dans le workspace (tools confinés :
     shell, lecture/écriture de fichiers, liste des fichiers — pas de push) ;
   - gate déterministe : `run_tests` (commande auto-détectée selon la stack :
     `package.json` → Jest `npm run test-unit -- --ci --silent`, `pyproject.toml`
     → `uv run pytest -x -q`) ;
   - agent *reviewer* : verdict JSON `{approve, findings}` sur demande + plan +
     diff + log de tests.
   - La boucle s'arrête quand **tests OK + reviewer OK**, sinon le feedback est
     réinjecté dans le contexte du dev à l'itération suivante (plafond optionnel
     `max_iterations` si renseigné).
4. **Merge Request** — push de `feat/<slug>-<exec>` et ouverture d'une MR GitLab
   (titre, description avec demande/plan/itérations/tests/diff stat) pour
   validation humaine.

`dry_run: true` exécute tout **sauf** le push et la MR (vérification de bout en
bout sans effet de bord sur le forge).

## Entrée du workflow

```json
{
  "request": "Ajouter un filtre par date sur le dashboard",
  "repo": "https://gitlab.toollive.bearstech.com/toollive-team/toollive-business/toollive-c8y.git",
  "base_branch": "",
  "test_cmd": "",
  "max_iterations": null,
  "dry_run": false
}
```

- `repo` : URL git du repo cible (défaut : le front toollive-c8y) ou chemin
  local (mode dev/test, sans push ni MR).
- `base_branch` / `test_cmd` : vides = auto-détection (`develop`/`main`/`master`,
  stack du repo). Overrides aussi possibles par env `pipeline_base_branch` /
  `pipeline_test_cmd`.
- `max_iterations` : `null` (défaut) = **aucune limite** d'itérations dev/tests ;
  un entier positif = plafond optionnel.
- Query `state` disponible pendant l'exécution : `{phase, iteration, mr_url}`.

## Lancer une exécution

Depuis la console Mistral (Workflows → `change-to-mr` → Start) ou en API :

```bash
export mistral_api_key=...
python scripts/run.py "Ajouter un filtre par date sur le dashboard"
python scripts/run.py "Corriger le tri de la liste" --dry-run   # sans push/MR
```

## Lancer le worker en local

```bash
uv sync
cp .env.example .env   # renseigner mistral_api_key, gitlab_token (si repo distant)
uv run python -m worker.entrypoints.worker
```

## Déploiement managé (Mistral Cloud)

Le déploiement build ce repo GitHub (app GitHub Mistral installée sur le repo)
et sert le worker sans aucune clé dans l'image :

- image : `deploy/docker/Dockerfile.worker` (python 3.12 + git + Node 20 + uv),
  `CMD` = worker ;
- branche/sha poussé = révision servie ;
- **secret** : binder le secret workspace `gitlab_token` (PAT GitLab, scopes
  `read_repository`, `write_repository`, `api`) en env `GITLAB_TOKEN`.

Création via l'API/console ou `deploy_workflow_deployment` (MCP) :
`deployment_name`, `github_url=https://github.com/pef-tl/mistral-code-pipeline`,
`revision=<sha poussé>`, `build_directory="."`,
`dockerfile_path="deploy/docker/Dockerfile.worker"`,
`secrets=[{"secret_name": "gitlab_token", "env_var_name": "GITLAB_TOKEN"}]`.

## Tests

```bash
uv run pytest          # tests unitaires + e2e du workflow (Temporal time-skipping, effets externes mockés)
```

## Fichiers

```
apps/worker/src/worker/
├── entrypoints/worker.py        # discovery des workflows + run_worker
├── workflows/change_to_mr.py    # orchestrateur durable (change-to-mr)
├── activities/
│   ├── git_tools.py             # clone/branche/commit/push/diff + API GitLab (MR)
│   ├── test_runner.py           # gate de tests déterministe
│   └── agent_runner.py          # exécution des agents LLM dans une activité
└── agents/
    ├── definitions.py           # planner / dev / reviewer + bornes
    ├── run_agent.py             # boucle modèle -> tools -> modèle (bornée en tours)
    └── tools.py                 # tools confinés au workspace
deploy/docker/Dockerfile.worker
scripts/run.py                   # lance une exécution du workflow
tests/                           # pytest (unitaires + e2e workflow)
```

Conventions : voir `AGENTS.md`.
