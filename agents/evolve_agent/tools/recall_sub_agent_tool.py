"""recall_sub_agent tool implementation."""

from __future__ import annotations

from typing import Any

from nexau.archs.main_sub.agent_state import AgentState
from nexau.archs.main_sub.execution import SubAgentManager


def recall_sub_agent(
    sub_agent_name: str,
    message: str,
    sub_agent_id: str,
    agent_state: AgentState | None = None,
) -> dict[str, Any]:
    """Delegate `message` to a sub-agent and return its result.

    Args:
        sub_agent_name: Name of the sub-agent as configured on the parent agent.
        message: Task/question for the sub-agent.
        context: Optional context override for the sub-agent run.
        agent_state: Injected by the framework; provides access to the executor.

    Returns:
        Dict with `status` and either `result` or `error`.
    """
    if agent_state is None:
        return {"status": "error", "error": "Agent state not available"}

    # AgentState stores a reference to the runtime Executor; it isn't part of the
    # public interface today, so we access it defensively.
    executor = getattr(agent_state, "_executor", None)
    if executor is None:
        return {
            "status": "error",
            "error": "Executor not available on agent_state",
        }

    subagent_manager: SubAgentManager | None = getattr(executor, "subagent_manager", None)
    if subagent_manager is None:
        return {
            "status": "error",
            "error": "Sub-agent manager not available on executor",
        }

    try:
        result = subagent_manager.call_sub_agent(
            sub_agent_name,
            message,
            sub_agent_id,
            parent_agent_state=agent_state,
        )
        return {
            "status": "success",
            "sub_agent": sub_agent_name,
            "sub_agent_id": sub_agent_id,
            "message": message,
            "result": result,
        }
    except Exception as exc:
        return {
            "status": "error",
            "sub_agent": sub_agent_name,
            "sub_agent_id": sub_agent_id,
            "error": str(exc),
            "error_type": type(exc).__name__,
        }


__all__ = ["recall_sub_agent"]
