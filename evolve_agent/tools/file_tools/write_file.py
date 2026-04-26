# Copyright 2025 Google LLC
# SPDX-License-Identifier: Apache-2.0
"""
write_file tool - Writes content to a specified file.

Based on gemini-cli's write-file.ts implementation.
"""

import difflib
from pathlib import Path
from typing import Any

from nexau.archs.main_sub.agent_state import AgentState
from nexau.archs.sandbox import BaseSandbox, SandboxStatus
from nexau.archs.tool.builtin._sandbox_utils import get_sandbox, resolve_path


def _detect_line_ending(content: str) -> str:
    """Detect the line ending used in content."""
    if "\r\n" in content:
        return "\r\n"
    elif "\n" in content:
        return "\n"
    return "\n"


def _generate_diff(
    file_path: str,
    original_content: str,
    new_content: str,
) -> str:
    """Generate a unified diff between original and new content."""
    original_lines = original_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)

    diff = difflib.unified_diff(
        original_lines,
        new_lines,
        fromfile=f"Original: {file_path}",
        tofile=f"Written: {file_path}",
        lineterm="",
    )

    return "".join(diff)


def _get_diff_stat(original_content: str, new_content: str) -> dict[str, int]:
    """Calculate diff statistics."""
    original_lines = set(original_content.splitlines())
    new_lines = set(new_content.splitlines())

    added = len(new_lines - original_lines)
    removed = len(original_lines - new_lines)

    return {"added": added, "removed": removed}


def write_file(
    file_path: str,
    content: str,
    modified_by_user: bool = False,
    ai_proposed_content: str | None = None,
    agent_state: AgentState | None = None,
) -> dict[str, Any]:
    """
    Writes content to a specified file in the local filesystem.

    The user has the ability to modify `content`. If modified, this will be
    stated in the response.

    Args:
        file_path: The path to the file to write to
        content: The content to write to the file
        modified_by_user: Whether the proposed content was modified by the user
        ai_proposed_content: Initially proposed content (if modified_by_user is True)

    Returns:
        Dict with content and returnDisplay matching gemini-cli format
    """
    try:
        sandbox: BaseSandbox = get_sandbox(agent_state)

        # Validate file_path
        if not file_path or not file_path.strip():
            error_msg = 'Missing or empty "file_path"'
            return {
                "content": error_msg,
                "returnDisplay": "Error: Missing file path.",
                "error": {
                    "message": error_msg,
                    "type": "INVALID_FILE_PATH",
                },
            }

        # Resolve path (relative -> sandbox work_dir)
        resolved_path = resolve_path(file_path, sandbox)

        # Check if it's a directory
        if sandbox.file_exists(resolved_path) and sandbox.get_file_info(resolved_path).is_directory:
            error_msg = f"Path is a directory, not a file: {resolved_path}"
            return {
                "content": error_msg,
                "returnDisplay": "Error: Path is a directory.",
                "error": {
                    "message": error_msg,
                    "type": "TARGET_IS_DIRECTORY",
                },
            }

        # Check if file exists for diff
        file_exists = sandbox.file_exists(resolved_path)
        is_new_file = not file_exists
        original_content = ""

        if file_exists:
            read_res = sandbox.read_file(resolved_path, encoding="utf-8", binary=False)
            if read_res.status == SandboxStatus.SUCCESS and isinstance(read_res.content, str):
                original_content = read_res.content
            else:
                # Best-effort fallback; keep empty original_content if unreadable.
                original_content = ""

        # Determine line ending to use
        final_content = content
        if not is_new_file and original_content:
            line_ending = _detect_line_ending(original_content)
            if line_ending == "\r\n":
                # Normalize to CRLF if original file used it
                final_content = final_content.replace("\r\n", "\n").replace("\n", "\r\n")

        # Write the file through sandbox (creates parent dirs)
        write_res = sandbox.write_file(
            resolved_path,
            final_content,
            encoding="utf-8",
            binary=False,
            create_directories=True,
        )
        if write_res.status != SandboxStatus.SUCCESS:
            raise RuntimeError(write_res.error or "Failed to write file")

        # Generate diff for display
        file_diff = _generate_diff(resolved_path, original_content, final_content)
        diff_stat = _get_diff_stat(original_content, final_content)

        # Build success message (matching gemini-cli format)
        if is_new_file:
            llm_message = f"Successfully created and wrote to new file: {resolved_path}."
        else:
            llm_message = f"Successfully overwrote file: {resolved_path}."

        if modified_by_user:
            llm_message += f" User modified the `content` to be: {content}"

        return {
            "content": llm_message,
            "returnDisplay": {
                "fileDiff": file_diff,
                "fileName": Path(resolved_path).name,
                "filePath": resolved_path,
                "originalContent": original_content,
                "newContent": final_content,
                "diffStat": diff_stat,
                "isNewFile": is_new_file,
            },
        }

    except PermissionError:
        error_msg = f"Permission denied writing to file: {file_path}"
        return {
            "content": error_msg,
            "returnDisplay": error_msg,
            "error": {
                "message": error_msg,
                "type": "PERMISSION_DENIED",
            },
        }
    except OSError as e:
        error_msg = f"Error writing to file '{file_path}': {str(e)}"
        return {
            "content": error_msg,
            "returnDisplay": error_msg,
            "error": {"message": error_msg, "type": "FILE_WRITE_FAILURE"},
        }
    except Exception as e:
        error_msg = f"Error writing to file: {str(e)}"
        return {
            "content": error_msg,
            "returnDisplay": error_msg,
            "error": {
                "message": error_msg,
                "type": "FILE_WRITE_FAILURE",
            },
        }
