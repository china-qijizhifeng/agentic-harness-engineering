# Copyright (c) Nex-AGI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Background task management tool for checking status, getting output, and killing background tasks."""

import logging
from typing import (
    Literal,
    NotRequired,
    TypedDict,
)

from nexau.archs.main_sub.agent_state import AgentState
from nexau.archs.sandbox import BaseSandbox, SandboxStatus

logger = logging.getLogger(__name__)


class BackgroundTaskManageResult(TypedDict):
    status: Literal["running", "success", "error", "not_found"]
    pid: NotRequired[int | None]
    action: str
    command: NotRequired[str]
    duration_ms: NotRequired[int]
    exit_code: NotRequired[int | None]
    stdout: NotRequired[str]
    stdout_truncated: NotRequired[bool]
    stderr: NotRequired[str]
    stderr_truncated: NotRequired[bool]
    error: NotRequired[str]
    tasks: NotRequired[list[dict[str, object]]]


def background_task_manage_tool(
    action: Literal["status", "kill", "list"],
    pid: int | None = None,
    agent_state: AgentState | None = None,
) -> BackgroundTaskManageResult:
    """
    Manage background tasks started by run_shell_command with background=True.

    Args:
        action: The action to perform:
            - "status": Get the current status and output of a background task (requires pid)
            - "kill": Kill a running background task (requires pid)
            - "list": List all background tasks and their statuses
        pid: The process ID of the background task (required for "status" and "kill" actions)
        agent_state: AgentState containing agent context and global storage

    Returns:
        Dict containing the action result
    """
    assert agent_state is not None, "background_task_tool invoked, but agent_state is not passed."
    sandbox: BaseSandbox | None = agent_state.get_sandbox()
    assert sandbox is not None, "background_task_tool invoked, but sandbox is not initialized."

    bg_tasks = sandbox.list_background_tasks()
    logger.info(f"background_task_tool: action={action}, pid={pid}, sandbox={id(sandbox)}, tasks={list(bg_tasks.keys())}")

    if action == "list":
        task_list: list[dict[str, object]] = []
        for task_pid, task_info in bg_tasks.items():
            cmd_result = sandbox.get_background_task_status(task_pid)
            task_status: str
            if cmd_result.status == SandboxStatus.RUNNING:
                task_status = "running"
            elif cmd_result.status == SandboxStatus.SUCCESS:
                task_status = "success"
            else:
                task_status = "error"
            task_list.append(
                {
                    "pid": task_pid,
                    "command": task_info.get("command", ""),
                    "status": task_status,
                    "duration_ms": cmd_result.duration_ms,
                }
            )
        return {
            "status": "success",
            "action": "list",
            "tasks": task_list,
        }

    if pid is None:
        return {
            "status": "error",
            "action": action,
            "error": f"pid is required for action '{action}'",
        }

    # Normalize pid to int (LLM may send float e.g. 12345.0)
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return {
            "status": "error",
            "action": action,
            "error": f"pid must be an integer, got {type(pid).__name__}",
        }

    if action == "status":
        cmd_result = sandbox.get_background_task_status(pid)

        if cmd_result.status == SandboxStatus.ERROR and cmd_result.error and "not found" in cmd_result.error.lower():
            return {
                "status": "not_found",
                "pid": pid,
                "action": "status",
                "error": cmd_result.error,
            }

        result_status: Literal["running", "success", "error", "not_found"]
        if cmd_result.status == SandboxStatus.RUNNING:
            result_status = "running"
        elif cmd_result.status == SandboxStatus.SUCCESS:
            result_status = "success"
        else:
            result_status = "error"

        task_info = bg_tasks.get(pid, {})
        result: BackgroundTaskManageResult = {
            "status": result_status,
            "pid": pid,
            "action": "status",
            "command": str(task_info.get("command", "")),
            "duration_ms": cmd_result.duration_ms,
            "exit_code": cmd_result.exit_code,
        }

        if cmd_result.stdout:
            result["stdout"] = cmd_result.stdout
            result["stdout_truncated"] = cmd_result.truncated
        else:
            result["stdout"] = ""
            result["stdout_truncated"] = False

        if cmd_result.stderr:
            result["stderr"] = cmd_result.stderr
            result["stderr_truncated"] = cmd_result.truncated
        else:
            result["stderr"] = ""
            result["stderr_truncated"] = False

        if cmd_result.error:
            result["error"] = cmd_result.error

        logger.info(f"Background task status: pid={pid} -> {result_status}")
        return result

    elif action == "kill":
        cmd_result = sandbox.kill_background_task(pid)

        if cmd_result.status == SandboxStatus.SUCCESS:
            logger.info(f"Background task killed: pid={pid}")
            return {
                "status": "success",
                "pid": pid,
                "action": "kill",
                "stdout": cmd_result.stdout,
                "duration_ms": cmd_result.duration_ms,
            }
        else:
            return {
                "status": "error",
                "pid": pid,
                "action": "kill",
                "error": cmd_result.error or "Failed to kill background task",
            }

    return {
        "status": "error",
        "pid": pid,
        "action": action,
        "error": f"Unknown action: {action}. Use 'status', 'kill', or 'list'.",
    }
