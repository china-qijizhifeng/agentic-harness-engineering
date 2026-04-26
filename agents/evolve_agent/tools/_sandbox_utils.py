"""
Sandbox helpers for gemini-cli style builtin tools.

These tool implementations route all filesystem and shell operations through
NexAU's BaseSandbox abstraction (e.g. LocalSandbox/E2B).
"""

from __future__ import annotations

from pathlib import Path

from nexau.archs.main_sub.agent_state import AgentState
from nexau.archs.sandbox import BaseSandbox
from nexau.archs.sandbox.base_sandbox import SandboxError


def get_sandbox(agent_state: AgentState | None) -> BaseSandbox:
    """Get sandbox from agent_state, or raise SandboxError."""
    if agent_state is not None:
        sandbox = agent_state.get_sandbox()
        if sandbox is not None:
            return sandbox
    raise SandboxError("Sandbox not found")


def resolve_path(path: str, sandbox: BaseSandbox) -> str:
    """Resolve relative paths against sandbox work_dir (string resolution only)."""
    if Path(path).is_absolute():
        return path
    return str(Path(str(sandbox.work_dir)) / path)
