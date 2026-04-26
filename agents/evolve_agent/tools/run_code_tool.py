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

"""Python code execution tool implementation using sandbox adaptor."""

from __future__ import annotations

import logging
import time
from typing import Literal, TypedDict

from nexau.archs.main_sub.agent_state import AgentState
from nexau.archs.sandbox import BaseSandbox, SandboxStatus

logger = logging.getLogger(__name__)

# Maximum output length to prevent overwhelming responses
MAX_OUTPUT_LENGTH = 30000
DEFAULT_TIMEOUT = 120000  # 2 minutes in milliseconds
MAX_TIMEOUT = 600000  # 10 minutes in milliseconds


class ExecutionResult(TypedDict, total=False):
    status: Literal["success", "error", "timeout"]
    duration_ms: int
    description: str
    stdout: str
    stderr: str
    exit_code: int
    error: str
    error_type: str


def run_code_tool(
    code_block: str,
    timeout: int | None = None,
    description: str | None = None,
    agent_state: AgentState | None = None,
) -> ExecutionResult:
    """
    Execute Python code using sandbox adaptor.

    Args:
        code_block: The Python code to execute (required)
        timeout: Optional timeout in milliseconds (max 600000ms / 10 minutes)
        description: Clear, concise description of what this code does
        agent_state: AgentState containing agent context and global storage

    Returns:
        Dict containing execution results including stdout, stderr, and exit code

    Examples:
        result = run_code_tool(
            code_block="import sys\\nprint('Hello from Python', sys.version)",
            timeout=30000,
            agent_state=agent_state
        )
    """
    start_time = time.time()

    # Get sandbox instance
    assert agent_state is not None, "File operation tool invoked, but agent_state is not passed. We need sandbox instance in agent_state."
    sandbox: BaseSandbox | None = agent_state.get_sandbox()
    assert sandbox is not None, "File operation tool invoked, but sandbox is not initialized."

    # Validate timeout
    if timeout is None:
        timeout = DEFAULT_TIMEOUT
    if timeout > MAX_TIMEOUT:
        return {
            "status": "error",
            "error": f"Timeout cannot exceed {MAX_TIMEOUT}ms (10 minutes)",
            "duration_ms": 0,
        }

    # Validate code
    if not code_block or not code_block.strip():
        return {
            "status": "error",
            "error": "Code block cannot be empty",
            "duration_ms": int((time.time() - start_time) * 1000),
        }

    try:
        # Execute Python code through sandbox
        exec_result = sandbox.execute_code(code_block, language="python", timeout=timeout)

        duration_ms = exec_result.duration_ms

        # Map sandbox status to tool status
        if exec_result.status == SandboxStatus.TIMEOUT:
            status = "timeout"
        elif exec_result.status == SandboxStatus.SUCCESS:
            status = "success"
        else:
            status = "error"

        # Extract stdout and stderr from outputs
        stdout = ""
        stderr = ""
        if exec_result.outputs:
            for output in exec_result.outputs:
                if output.get("type") == "stdout":
                    stdout += output.get("text", "")
                elif output.get("type") == "stderr":
                    stderr += output.get("text", "")

        # Prepare result
        exec_status: Literal["success", "error", "timeout"] = status  # type: ignore[assignment]
        result: ExecutionResult = {
            "status": exec_status,
            "duration_ms": duration_ms,
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": 0 if status == "success" else 1,
        }

        # Add description if provided
        if description:
            result["description"] = description

        # Add error message if present
        if exec_result.error_value:
            result["error"] = exec_result.error_value
            result["error_type"] = exec_result.error_type or "ExecutionError"

        # Log execution
        logger.info(
            f"Python code executed (status={status}, duration={duration_ms}ms)",
        )

        return result

    except Exception as e:
        duration_ms = int((time.time() - start_time) * 1000)
        return {
            "status": "error",
            "error": f"Unexpected error: {str(e)}",
            "error_type": type(e).__name__,
            "duration_ms": duration_ms,
            "stdout": "",
            "stderr": "",
            "exit_code": -1,
        }
