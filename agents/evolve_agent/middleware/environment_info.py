"""EnvironmentInfo Middleware — auto-detect sandbox environment on first LLM call.

Hooks into the before_model phase (one-shot).  On the very first model call
it runs a set of lightweight shell probes through the sandbox, formats the
results into a compact text block, and injects it as a FRAMEWORK message so
the agent can skip manual environment discovery.

Configuration example (YAML)::

    middlewares:
      - import: middleware.environment_info:EnvironmentInfoMiddleware
        params:
          timeout_per_command_ms: 5000
          total_timeout_ms: 30000
"""

from __future__ import annotations

import logging
import time
from typing import Any

from nexau.archs.main_sub.execution.hooks import (
    BeforeModelHookInput,
    HookResult,
    Middleware,
)
from nexau.core.messages import Message, Role, TextBlock

logger = logging.getLogger(__name__)

_PROBE_COMMANDS: list[tuple[str, str]] = [
    (
        "os",
        "cat /etc/os-release 2>/dev/null | head -3 || uname -a",
    ),
    (
        "python",
        "(python3 --version 2>/dev/null && which python3) || "
        "(python --version 2>/dev/null && which python) || echo 'not found'",
    ),
    (
        "node",
        "node --version 2>/dev/null || echo 'not found'",
    ),
    (
        "gcc",
        "gcc --version 2>/dev/null | head -1 || echo 'not found'",
    ),
    (
        "go",
        "go version 2>/dev/null || echo 'not found'",
    ),
    (
        "rust",
        "rustc --version 2>/dev/null || echo 'not found'",
    ),
    (
        "java",
        "java -version 2>&1 | head -1 || echo 'not found'",
    ),
    (
        "git",
        "git --version 2>/dev/null || echo 'not found'",
    ),
    (
        "make",
        "which make 2>/dev/null && echo 'available' || echo 'not found'",
    ),
    (
        "pkg_managers",
        "echo 'pip:' $(pip3 --version 2>/dev/null | awk '{print $2}' || echo 'N/A'); "
        "echo 'npm:' $(npm --version 2>/dev/null || echo 'N/A'); "
        "echo 'apt:' $(which apt 2>/dev/null && echo 'yes' || echo 'N/A')",
    ),
    (
        "workdir",
        "echo '=== pwd ===' && pwd && echo '=== ls ===' && ls -la 2>/dev/null | head -25",
    ),
]


class EnvironmentInfoMiddleware(Middleware):
    """Inject a one-shot environment summary before the first model call.

    Args:
        timeout_per_command_ms: Per-probe command timeout in milliseconds.
        total_timeout_ms: Total budget for all probes combined.
    """

    def __init__(
        self,
        *,
        timeout_per_command_ms: int = 5000,
        total_timeout_ms: int = 30000,
    ) -> None:
        self.timeout_per_command_ms = timeout_per_command_ms
        self.total_timeout_ms = total_timeout_ms
        self._injected = False

    def before_model(self, hook_input: BeforeModelHookInput) -> HookResult:
        if self._injected:
            return HookResult.no_changes()

        self._injected = True

        sandbox = hook_input.agent_state.get_sandbox()
        if sandbox is None:
            logger.warning(
                "[EnvironmentInfoMiddleware] No sandbox available — skipping env probe",
            )
            return HookResult.no_changes()

        probe_results = self._run_probes(sandbox)
        if not probe_results:
            return HookResult.no_changes()

        env_text = self._format_env_info(probe_results)
        logger.info(
            "[EnvironmentInfoMiddleware] Environment probed (%d results), injecting info message",
            len(probe_results),
        )

        messages = list(hook_input.messages)

        insert_idx = 1
        for i, msg in enumerate(messages):
            if msg.role in (Role.USER, Role.FRAMEWORK):
                insert_idx = i
                break

        env_message = Message(
            role=Role.FRAMEWORK,
            content=[TextBlock(text=env_text)],
        )
        messages.insert(insert_idx, env_message)

        return HookResult.with_modifications(messages=messages)

    # ------------------------------------------------------------------
    # Probing
    # ------------------------------------------------------------------

    def _run_probes(
        self,
        sandbox: Any,
    ) -> dict[str, str]:
        """Execute probe commands via sandbox, returning label→output mapping."""

        results: dict[str, str] = {}
        deadline = time.monotonic() + self.total_timeout_ms / 1000.0

        for label, cmd in _PROBE_COMMANDS:
            if time.monotonic() >= deadline:
                logger.warning(
                    "[EnvironmentInfoMiddleware] Total timeout reached after %d probes",
                    len(results),
                )
                break

            try:
                exec_result = sandbox.execute_bash(
                    cmd, timeout=self.timeout_per_command_ms,
                )
                stdout = getattr(exec_result, "stdout", "") or ""
                stderr = getattr(exec_result, "stderr", "") or ""
                output = (stdout.strip() or stderr.strip()) or "unknown"
                results[label] = output
            except Exception as exc:
                logger.debug(
                    "[EnvironmentInfoMiddleware] Probe %r failed: %s", label, exc,
                )
                results[label] = "unknown"

        return results

    # ------------------------------------------------------------------
    # Formatting
    # ------------------------------------------------------------------

    @staticmethod
    def _format_env_info(probes: dict[str, str]) -> str:
        lines = ["Environment Info (auto-detected, read-only reference):"]

        def _get(key: str) -> str:
            return probes.get(key, "unknown")

        os_info = _get("os")
        for line in os_info.splitlines():
            if "PRETTY_NAME" in line:
                os_info = line.split("=", 1)[-1].strip().strip('"')
                break

        lines.append(f"OS: {os_info}")

        py = _get("python")
        py_lines = py.strip().splitlines()
        if len(py_lines) >= 2:
            version_line = py_lines[0].strip()
            path_line = py_lines[1].strip()
            cmd_hint = "python3" if "python3" in path_line else "python"
            lines.append(f"Python: {version_line} ({path_line})  ← use '{cmd_hint}'")
        else:
            lines.append(f"Python: {py}")

        for label, display in [
            ("node", "Node"),
            ("gcc", "GCC"),
            ("go", "Go"),
            ("rust", "Rust"),
            ("java", "Java"),
            ("git", "Git"),
        ]:
            val = _get(label)
            first_line = val.splitlines()[0].strip() if val else "unknown"
            lines.append(f"{display}: {first_line}")

        make_val = _get("make")
        if "available" in make_val.lower():
            lines.append("Make: available")
        else:
            lines.append("Make: not found")

        pkg = _get("pkg_managers")
        lines.append(f"Pkg managers: {pkg}")

        workdir = _get("workdir")
        wd_lines = workdir.splitlines()
        pwd_val = ""
        file_lines: list[str] = []
        in_ls = False
        for wl in wd_lines:
            if wl.startswith("=== pwd ==="):
                continue
            if wl.startswith("=== ls ==="):
                in_ls = True
                continue
            if not in_ls:
                pwd_val = wl.strip()
            else:
                file_lines.append(wl.strip())

        if pwd_val:
            lines.append(f"Working dir: {pwd_val}")
        if file_lines:
            preview = "  " + "\n  ".join(file_lines[:15])
            lines.append(preview)

        return "\n".join(lines)
