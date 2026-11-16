#!/usr/bin/env python3
"""Lance une exécution du workflow `change-to-mr` (étape 1 : la demande du
développeur) contre le déploiement managé.

Usage :
    export mistral_api_key=...   # ou MISTRAL_API_KEY
    python scripts/run.py "Ajouter un filtre par date sur le dashboard"
    python scripts/run.py "Corriger le tri de la liste" --dry-run
    python scripts/run.py "..." --repo https://gitlab.../toollive-c8y.git \
        --base-branch develop --max-iterations 5
"""

import argparse
import json
import os
import sys
import urllib.request

API = "https://api.mistral.ai/v1"
WORKFLOW = "change-to-mr"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("request", help="demande du développeur (étape 1)")
    p.add_argument("--repo", default="", help="URL git du repo cible (défaut : front toollive-c8y)")
    p.add_argument("--base-branch", default="")
    p.add_argument("--test-cmd", default="")
    p.add_argument("--max-iterations", type=int, default=5)
    p.add_argument("--dry-run", action="store_true",
                   help="exécute tout sauf push + MR (pas d'effet de bord sur le forge)")
    args = p.parse_args()

    api_key = os.environ.get("mistral_api_key") or os.environ.get("MISTRAL_API_KEY")
    if not api_key:
        sys.exit("définir mistral_api_key (ou MISTRAL_API_KEY) dans l'environnement")

    payload = {"input": {
        "request": args.request,
        "max_iterations": args.max_iterations,
        "dry_run": args.dry_run,
    }}
    if args.repo:
        payload["input"]["repo"] = args.repo
    if args.base_branch:
        payload["input"]["base_branch"] = args.base_branch
    if args.test_cmd:
        payload["input"]["test_cmd"] = args.test_cmd

    req = urllib.request.Request(
        f"{API}/workflows/{WORKFLOW}/execute",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        out = json.loads(resp.read().decode())
    print(json.dumps(out, indent=2, ensure_ascii=False))
    exec_id = out.get("execution_id")
    if exec_id:
        print(f"\nexecution_id: {exec_id}\n"
              f"suivi : console Mistral > Workflows > change-to-mr > exécutions, "
              f"ou query `state` sur l'exécution {exec_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
