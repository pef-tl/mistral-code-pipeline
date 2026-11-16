# AGENTS.md — Conventions du projet mistral-code-pipeline

Ce fichier est lu automatiquement par les agents (Vibe Code CLI, VS Code,
agents du pipeline). Il condense les décisions de conception — respecte-les.

## Objet

Workflow **Mistral Workflows** (orchestration durable Temporal), déployé en
**déploiement managé Mistral Cloud** : demande développeur → plan → itérations
dev/tests entre agents (bornées) → **Merge Request GitLab** pour validation
humaine. Jamais de merge automatique.

## Règles d'architecture (non négociables)

1. **Orchestration déterministe** : le workflow ne fait qu'appeler des activités
   et boucler ; tout effet de bord (LLM, git réseau, tests, API GitLab) est dans
   une activité. Ne jamais appeler le réseau depuis le workflow.
2. **Agents dans des activités** : un objet agent ne traverse pas Temporal, seul
   son NOM passe (`agent_runner.run_agent_activity`). Ne PAS utiliser les durable
   agents (`Agent`/`Runner` du plugin workflows) : dépréciés. Client LLM :
   `get_mistral_client()` (jamais `Mistral(api_key=...)` à la main).
3. **Boucle dev/tests sans plafond** : `max_iterations=None` par défaut (illimité,
   bornée par le timeout d'exécution du workflow, 8h) ; `MAX_AGENT_TURNS = 20`,
   `MAX_AGENT_SECONDS = 15 min` bornent chaque boucle d'agent.
4. **Tools confinés** : shell/fichiers des agents bornés au workspace (pas de
   path traversal, pas de `sudo`, pas de `git push`).
5. **MR = gate humaine** : le workflow pousse `feat/...` et ouvre la MR GitLab,
   mais ne merge JAMAIS. `dry_run=true` saute push + MR.
6. **Pas de secret en dur** : PAT GitLab lu depuis l'env (`gitlab_token`, toute
   casse tolérée — ex. binding `GITLAB_TOKEN`), jamais loggué (`_scrub`), jamais
   persisté dans `.git/config` (URL authentifiée passée uniquement en argument
   des commandes réseau). Ne jamais demander la valeur d'un secret dans une
   conversation : le binder comme secret workspace du déploiement.
7. **Repo cible par défaut** : front `toollive-c8y` sur
   `gitlab.toollive.bearstech.com` (branche de base auto-détectée, convention
   `develop`).

## Stack

- Python 3.12, `mistralai-workflows>=3.19`, `mistralai>=3.4`, pydantic.
- Image worker : `deploy/docker/Dockerfile.worker` (python 3.12-slim + git +
  Node 20 + uv). Le `CMD` lance le worker ; la plateforme injecte
  `MISTRAL_SA_TOKEN_PATH` (aucune clé API dans l'image).
- Tests front (Jest) et Python (pytest) tournent DANS le worker.
- Modèles : `mistral-large-latest` (planner/reviewer), `mistral-medium-latest`
  (dev).

## Variables d'env (convention MINUSCULES, fallback majuscules toléré)

`gitlab_token` (PAT, scopes read_repository/write_repository/api),
`pipeline_workspace`, `pipeline_base_branch`, `pipeline_test_cmd`,
`gitlab_username`. Voir `.env.example`.

## Déploiement managé

Le déploiement build **ce repo GitHub** à la révision poussée
(`deploy_workflow_deployment` : `github_url`, `revision=<sha>`,
`build_directory="."`, `dockerfile_path="deploy/docker/Dockerfile.worker"`,
secret `gitlab_token` → `GITLAB_TOKEN`). Nom de déploiement stable :
`mistral-code-pipeline`.
