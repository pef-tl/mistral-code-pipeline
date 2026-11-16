"""Activity d'exécution des agents LLM (planner / dev / reviewer).

Un agent ne s'exécute JAMAIS dans le workflow : le sandbox Temporal interdit tout
appel réseau. Le workflow ne passe que des paramètres JSON (nom de l'agent,
message, cible) ; l'agent et ses tools sont reconstruits ICI, dans l'activity,
puis la boucle `run_agent` tourne hors sandbox.
"""

from __future__ import annotations

from datetime import timedelta

import mistralai.workflows as workflows

from worker.agents import definitions as defs
from worker.agents.run_agent import run_agent
from worker.agents.tools import build_dev_tools

# start_to_close doit rester très inférieur au timeout d'exécution de la
# plateforme : une activité bloquée échoue alors explicitement. Retries bornés.
AGENT_TIMEOUT = timedelta(minutes=20)
AGENT_MAX_ATTEMPTS = 3


@workflows.activity(
    start_to_close_timeout=AGENT_TIMEOUT,
    retry_policy_max_attempts=AGENT_MAX_ATTEMPTS,
    retry_policy_backoff_coefficient=2.0,
)
async def run_agent_activity(agent_name: str, user_message: str, target: str) -> str:
    """Exécute l'agent `agent_name` (voir defs.AGENT_FACTORIES) avec les tools
    confinés à `target` (le workspace), et retourne sa réponse finale."""
    try:
        factory = defs.AGENT_FACTORIES[agent_name]
    except KeyError:
        raise ValueError(
            f"agent inconnu: {agent_name!r} (attendu: {sorted(defs.AGENT_FACTORIES)})"
        )
    tool_impls = build_dev_tools(target)
    agent = factory(tools=[impl for _, impl in tool_impls.values()])
    return await run_agent(agent, user_message, tool_impls)
