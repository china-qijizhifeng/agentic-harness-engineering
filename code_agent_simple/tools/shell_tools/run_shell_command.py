# Copyright 2025 Google LLC
# SPDX-License-Identifier: Apache-2.0
"""
run_shell_command tool (shell) - Executes shell commands.

Based on gemini-cli's shell.ts implementation.
Supports foreground and background execution, timeout handling, and process management.
"""

import shlex
import time
from collections.abc import Callable
from typing import Any

from nexau.archs.main_sub.agent_state import AgentState
from nexau.archs.sandbox import BaseSandbox, SandboxStatus
from nexau.archs.tool.builtin._sandbox_utils import get_sandbox, resolve_path

# Configuration constants (matching gemini-cli)
DEFAULT_TIMEOUT_MS = 300000  # 5 minutes default timeout
TRUNCATE_OUTPUT_THRESHOLD = 4_000_000  # Truncate when output exceeds this many chars
TRUNCATE_OUTPUT_LINES = 1000  # Keep last N lines when truncating
MAX_TRUNCATED_LINE_WIDTH = 1000  # Max chars per line in truncated output
MAX_TRUNCATED_CHARS = 4000  # Keep last N chars for single massive line


def _truncate_shell_output(content: str) -> str:
    """
    Truncate large shell output, keeping last N lines (matching gemini-cli).
    Applied when content exceeds TRUNCATE_OUTPUT_THRESHOLD.
    """
    if len(content) <= TRUNCATE_OUTPUT_THRESHOLD:
        return content

    lines = content.split("\n")
    total_lines = len(lines)

    if total_lines > 1:
        # Multi-line: show last N lines, truncate long lines
        last_lines = lines[-TRUNCATE_OUTPUT_LINES:]
        processed: list[str] = []
        for line in last_lines:
            if len(line) > MAX_TRUNCATED_LINE_WIDTH:
                processed.append(line[:MAX_TRUNCATED_LINE_WIDTH] + "... [LINE WIDTH TRUNCATED]")
            else:
                processed.append(line)
        return f"Output too large. Showing the last {len(processed)} of {total_lines} lines.\n...\n" + "\n".join(processed)
    else:
        # Single massive line: keep last N chars
        snippet = content[-MAX_TRUNCATED_CHARS:]
        return f"Output too large. Showing the last {MAX_TRUNCATED_CHARS:,} characters of the output.\n...{snippet}"


def run_shell_command(
    command: str,
    description: str | None = None,
    is_background: bool = False,
    dir_path: str | None = None,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    update_output: Callable[[str], None] | None = None,
    agent_state: AgentState | None = None,
) -> dict[str, Any]:
    """
    Executes a shell command.

    On Unix/Linux/macOS: Executes as `bash -c <command>`
    On Windows: Executes as `powershell.exe -NoProfile -Command <command>`

    The following information is returned:
    - Output: Combined stdout/stderr. Can be `(empty)` or partial on error.
    - Exit Code: Only included if non-zero (command failed).
    - Error: Only included if a process-level error occurred.
    - Signal: Only included if process was terminated by a signal.
    - Background PIDs: Only included if background processes were started.
    - Process Group PGID: Only included if available.

    Args:
        command: The exact command to execute
        description: Brief description of the command for the user
        dir_path: Directory to run the command in (optional)
        is_background: Whether to run in background
        timeout_ms: Timeout in milliseconds (0 for no timeout)
        update_output: Callback for streaming output updates

    Returns:
        Dict with content and returnDisplay matching gemini-cli format
    """
    try:
        # Validate command
        if not command or not command.strip():
            return {
                "content": "Command cannot be empty.",
                "returnDisplay": "Error: Empty command.",
                "error": {
                    "message": "Command cannot be empty.",
                    "type": "INVALID_COMMAND",
                },
            }

        sandbox: BaseSandbox = get_sandbox(agent_state)

        # Determine working directory (string resolution only; checks via sandbox)
        if dir_path:
            cwd = resolve_path(dir_path, sandbox)
            if not sandbox.file_exists(cwd):
                error_msg = f"Directory not found: {dir_path}"
                return {
                    "content": error_msg,
                    "returnDisplay": "Error: Directory not found.",
                    "error": {"message": error_msg, "type": "DIRECTORY_NOT_FOUND"},
                }
            info = sandbox.get_file_info(cwd)
            if not info.is_directory:
                error_msg = f"Path is not a directory: {dir_path}"
                return {
                    "content": error_msg,
                    "returnDisplay": "Error: Path is not a directory.",
                    "error": {"message": error_msg, "type": "NOT_A_DIRECTORY"},
                }
        else:
            cwd = str(sandbox.work_dir)

        timeout_arg = timeout_ms if timeout_ms and timeout_ms > 0 else None

        if is_background:
            # Background mode: sandbox.execute_bash supports cwd and background params
            start = time.time()
            cmd_result = sandbox.execute_bash(
                command,
                timeout=timeout_arg,
                cwd=cwd,
                background=True,
            )
            duration_ms = int((time.time() - start) * 1000)
            bg_pid = cmd_result.background_pid
            if bg_pid is not None:
                llm_content = (
                    f"Background task started (pid: {bg_pid}). "
                    f"Use BackgroundTaskManage with action='status' and pid={bg_pid} to check output."
                )
                bg_result: dict[str, Any] = {
                    "content": llm_content,
                    "returnDisplay": f"Background task started (pid: {bg_pid})",
                    "duration_ms": duration_ms,
                    "backgroundPids": [bg_pid],
                }
                if cmd_result.output_dir:
                    bg_result["output_dir"] = cmd_result.output_dir
                    bg_result["stdout_file"] = cmd_result.stdout_file
                    bg_result["stderr_file"] = cmd_result.stderr_file
                return bg_result
            # Fallback if sandbox didn't return pid
            fallback_result: dict[str, Any] = {
                "content": cmd_result.stdout or "Background task started.",
                "returnDisplay": cmd_result.stdout or "Background task started.",
                "duration_ms": duration_ms,
            }
            if cmd_result.error:
                fallback_result["error"] = {
                    "message": cmd_result.error,
                    "type": "SHELL_EXECUTE_ERROR",
                }
            return fallback_result

        # Foreground mode
        # Build description for display
        cmd_description = command
        if dir_path:
            cmd_description += f" [in {dir_path}]"
        else:
            cmd_description += f" [current working directory {cwd}]"
        if description:
            cmd_description += f" ({description.replace(chr(10), ' ')})"
        # Streaming output is not supported by execute_bash; ignore update_output.
        _ = update_output

        # Execute command through sandbox, optionally scoping to directory via `cd`.
        cmd_to_run = command
        if cwd:
            cmd_to_run = f"cd {shlex.quote(cwd)} && {command}"

        start = time.time()
        cmd_result = sandbox.execute_bash(cmd_to_run, timeout=timeout_arg)
        duration_ms = int((time.time() - start) * 1000)

        stdout = cmd_result.stdout or ""
        stderr = cmd_result.stderr or ""
        output = stdout
        if stderr:
            output = f"{stdout}\n{stderr}" if stdout else stderr

        # Truncate large output (matching gemini-cli: keep last N lines)
        output = _truncate_shell_output(output)

        exit_code = cmd_result.exit_code
        error_message = cmd_result.error

        # Build result
        llm_parts: list[str] = []
        if cmd_result.status == SandboxStatus.TIMEOUT:
            timeout_minutes = (timeout_ms / 60000) if timeout_ms else 0
            llm_parts.append(f"Timeout: command timed out after {timeout_minutes:.1f} minutes.")
        else:
            llm_parts.append(f"Output: {output if output else '(empty)'}")

        if error_message:
            llm_parts.append(f"Error: {error_message}")

        if exit_code != 0:
            llm_parts.append(f"Exit Code: {exit_code}")

        llm_content = "\n".join(llm_parts)

        # Build return display
        if output and output.strip():
            return_display = output
        elif cmd_result.status == SandboxStatus.TIMEOUT:
            return_display = f"Command timed out after {timeout_ms / 60000:.1f} minutes."
        elif error_message:
            return_display = f"Command failed: {error_message}"
        elif exit_code != 0:
            return_display = f"Command exited with code: {exit_code}"
        else:
            return_display = "(empty)"

        result: dict[str, Any] = {
            "content": llm_content,
            "returnDisplay": return_display,
            "duration_ms": duration_ms,
            "exit_code": exit_code,
        }

        # Include CommandResult truncation metadata and file paths
        if cmd_result.output_dir:
            result["output_dir"] = cmd_result.output_dir
            result["stdout_file"] = cmd_result.stdout_file
            result["stderr_file"] = cmd_result.stderr_file
        if cmd_result.truncated:
            result["truncated"] = True
            result["original_stdout_length"] = cmd_result.original_stdout_length
            result["original_stderr_length"] = cmd_result.original_stderr_length

        if error_message or cmd_result.status in (
            SandboxStatus.ERROR,
            SandboxStatus.TIMEOUT,
        ):
            result["error"] = {
                "message": error_message or "Command failed",
                "type": "SHELL_EXECUTE_ERROR",
            }

        return result

    except Exception as e:
        error_msg = f"Error executing shell command: {str(e)}"
        return {
            "content": error_msg,
            "returnDisplay": error_msg,
            "error": {
                "message": error_msg,
                "type": "SHELL_EXECUTE_ERROR",
            },
        }
