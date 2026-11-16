"""Définitions des agents du pipeline (exécutés DANS des activités).

Un agent ne s'exécute JAMAIS dans le workflow : le sandbox Temporal interdit tout
appel réseau. Le workflow ne passe que le NOM de l'agent à l'activity, qui le
reconstruit ici. Voir :
https://docs.mistral.ai/studio/agents/overview

Ne PAS utiliser les durable agents (`Agent`/`Runner` du plugin workflows) :
dépréciés — la boucle est explicite dans `run_agent.py`.
"""

from dataclasses import dataclass, field
from typing import Any, Callable

# ---------------------------------------------------------------- modèles ---
# Routing : gros modèle pour le raisonnement (plan/review), modèle plus
# rapide/économique pour la boucle dev.
PLANNER_MODEL = "mistral-large-latest"
DEV_MODEL = "mistral-medium-latest"
REVIEWER_MODEL = "mistral-large-latest"

MAX_DEV_ITERATIONS = 5          # borne de la boucle dev -> tests (workflow)
MAX_AGENT_TURNS = 20            # borne de la boucle d'outils d'un agent
MAX_AGENT_SECONDS = 15 * 60     # borne wall-clock d'une boucle d'agent
MAX_REPEATED_TOOL_CALLS = 3     # même appel tool répété -> réponse finale forcée


@dataclass
class AgentSpec:
    name: str
    model: str
    instructions: str
    tools: list[Callable[..., Any]] = field(default_factory=list)


# --------------------------------------------------------------- PLANNER ----
PLANNER_INSTRUCTIONS = """\
You are the planning agent of an autonomous coding pipeline.
You receive a change request from a developer and the file tree of the target
repository (already cloned in your workspace).

Your job: produce a SHORT, concrete implementation plan. Do NOT write code.

Explore the repo with your file/shell tools (a handful of reads is enough),
then answer with EXACTLY this JSON object and nothing else:
{
  "summary": "one-sentence restatement of the change",
  "files": ["path/to/file the change will touch", "..."],
  "approach": ["step 1", "step 2", "..."],
  "verification": "how to verify the change (tests to run/add, manual check)"
}

Rules:
- Follow the repo's existing conventions (AGENTS.md, README, similar code).
- Prefer the smallest change that satisfies the request.
- The agent loop is hard-bounded (~20 turns): after ~10 tool calls, answer NOW
  with the JSON above, even if imperfect.
"""

# -------------------------------------------------------------------- DEV ---
DEV_INSTRUCTIONS = """\
You are a developer agent implementing a change inside a git workspace.
You have shell and file tools, all confined to the workspace.

Rules:
- Implement EXACTLY the requested change. Never refactor unrelated code.
- Follow the repo's conventions (AGENTS.md, README, existing similar code).
- Keep the diff minimal and idiomatic; run the project's linter/formatter on
  every file you touch if one exists.
- Do NOT commit (the pipeline commits for you). Do NOT push. Do NOT modify CI
  configuration or workflow files of the pipeline itself.
- If tests fail, read the log, fix the code, and re-run the relevant command.
- When the acceptance criteria are met, stop calling tools and return a short
  summary of what you changed (files + why).
"""

# ---------------------------------------------------------------- REVIEWER --
REVIEWER_INSTRUCTIONS = """\
You are the review/test agent of an autonomous coding pipeline. You receive the
original change request, the plan, the current diff, and the latest test run
output. You decide whether the change is DONE.

Verify:
1. The diff implements the request and nothing out of scope.
2. No secret, no debug leftover, no weakened test, no unrelated refactor.
3. The test command passed (or, if it failed, whether the failure is genuinely
   caused by this change).

Answer with EXACTLY this JSON object and nothing else:
{ "approve": true|false,
  "findings": ["concrete issue the dev agent must fix", "..."] }

If approve=false, findings must be actionable: name the file and the fix.
"""


def planner_agent(tools: list) -> AgentSpec:
    return AgentSpec("planner-agent", PLANNER_MODEL, PLANNER_INSTRUCTIONS, tools=tools)


def dev_agent(tools: list) -> AgentSpec:
    return AgentSpec("dev-agent", DEV_MODEL, DEV_INSTRUCTIONS, tools=tools)


def reviewer_agent(tools: list) -> AgentSpec:
    return AgentSpec("reviewer-agent", REVIEWER_MODEL, REVIEWER_INSTRUCTIONS, tools=tools)


# Registre nom -> factory : le workflow ne passe que le NOM de l'agent.
AGENT_FACTORIES = {
    "planner": planner_agent,
    "dev": dev_agent,
    "reviewer": reviewer_agent,
}
