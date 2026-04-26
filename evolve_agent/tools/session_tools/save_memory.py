# Copyright 2025 Google LLC
# SPDX-License-Identifier: Apache-2.0
"""
save_memory tool - Saves facts to long-term memory.

Based on gemini-cli's memoryTool.ts implementation.
Stores facts in a GEMINI.md file with proper section management.
"""

import json
from pathlib import Path
from typing import Any

from nexau.archs.main_sub.agent_state import AgentState
from nexau.archs.sandbox import SandboxStatus
from nexau.archs.tool.builtin._sandbox_utils import get_sandbox, resolve_path

# Configuration constants
DEFAULT_CONTEXT_FILENAME = "GEMINI.md"
MEMORY_SECTION_HEADER = "## Gemini Added Memories"


def _ensure_newline_separation(current_content: str) -> str:
    """Ensure proper newline separation before appending content."""
    if len(current_content) == 0:
        return ""
    if current_content.endswith("\n\n") or current_content.endswith("\r\n\r\n"):
        return ""
    if current_content.endswith("\n") or current_content.endswith("\r\n"):
        return "\n"
    return "\n\n"


def _read_memory_file_content(file_path: str, sandbox: Any) -> str:
    """Read the current content of the memory file via sandbox."""
    try:
        if not sandbox.file_exists(file_path):
            return ""
        res = sandbox.read_file(file_path, encoding="utf-8", binary=False)
        if res.status == SandboxStatus.SUCCESS and isinstance(res.content, str):
            return res.content
        return ""
    except Exception:
        return ""


def _compute_new_content(current_content: str, fact: str) -> str:
    """Compute the new content that would result from adding a memory entry."""
    # Process the fact
    processed_text = fact.strip()
    # Remove leading dashes
    import re

    processed_text = re.sub(r"^(-+\s*)+", "", processed_text).strip()
    new_memory_item = f"- {processed_text}"

    header_index = current_content.find(MEMORY_SECTION_HEADER)

    if header_index == -1:
        # Header not found, append header and then the entry
        separator = _ensure_newline_separation(current_content)
        return f"{current_content}{separator}{MEMORY_SECTION_HEADER}\n{new_memory_item}\n"
    else:
        # Header found, find where to insert the new memory entry
        start_of_section = header_index + len(MEMORY_SECTION_HEADER)

        # Find end of section (next ## header or end of file)
        end_of_section_index = current_content.find("\n## ", start_of_section)
        if end_of_section_index == -1:
            end_of_section_index = len(current_content)

        before_section = current_content[:start_of_section].rstrip()
        section_content = current_content[start_of_section:end_of_section_index].rstrip()
        after_section = current_content[end_of_section_index:]

        section_content += f"\n{new_memory_item}"

        result = f"{before_section}\n{section_content.lstrip()}\n{after_section}".rstrip() + "\n"
        return result


def save_memory(
    fact: str,
    modified_by_user: bool = False,
    modified_content: str | None = None,
    memory_file_path: str | None = None,
    agent_state: AgentState | None = None,
) -> dict[str, Any]:
    """
    Saves a specific piece of information or fact to long-term memory.

    Use this tool when the user explicitly asks you to remember something,
    or when they state a clear, concise fact that seems important to retain.

    Args:
        fact: The specific fact or piece of information to remember
        modified_by_user: Whether the content was modified by user
        modified_content: User-modified content (if modified_by_user is True)
        memory_file_path: Custom path for memory file (optional)

    Returns:
        Dict with content and returnDisplay matching gemini-cli format
    """
    file_path = ""
    try:
        sandbox = get_sandbox(agent_state)

        # Validate fact
        if not fact or not fact.strip():
            return {
                "content": json.dumps(
                    {
                        "success": False,
                        "error": 'Parameter "fact" must be a non-empty string.',
                    }
                ),
                "returnDisplay": "Error: Fact cannot be empty.",
                "error": {
                    "message": 'Parameter "fact" must be a non-empty string.',
                    "type": "INVALID_PARAMETER",
                },
            }

        # Determine memory file path
        if memory_file_path:
            file_path = resolve_path(memory_file_path, sandbox)
        else:
            # Default to sandbox work_dir (no host home-dir access).
            file_path = str(Path(str(sandbox.work_dir)) / DEFAULT_CONTEXT_FILENAME)

        if modified_by_user and modified_content is not None:
            # User modified the content, write it directly
            write_res = sandbox.write_file(
                file_path,
                modified_content,
                encoding="utf-8",
                binary=False,
                create_directories=True,
            )
            if write_res.status != SandboxStatus.SUCCESS:
                raise RuntimeError(write_res.error or "Failed to write memory file")

            success_message = "Okay, I've updated the memory file with your modifications."
            return {
                "content": json.dumps(
                    {
                        "success": True,
                        "message": success_message,
                    }
                ),
                "returnDisplay": success_message,
            }
        else:
            # Normal memory entry logic
            current_content = _read_memory_file_content(file_path, sandbox)
            new_content = _compute_new_content(current_content, fact)

            # Write the new content
            write_res = sandbox.write_file(
                file_path,
                new_content,
                encoding="utf-8",
                binary=False,
                create_directories=True,
            )
            if write_res.status != SandboxStatus.SUCCESS:
                raise RuntimeError(write_res.error or "Failed to write memory file")

            success_message = f'Okay, I\'ve remembered that: "{fact}"'
            return {
                "content": json.dumps(
                    {
                        "success": True,
                        "message": success_message,
                    }
                ),
                "returnDisplay": success_message,
            }

    except PermissionError:
        error_msg = f"Permission denied writing to memory file: {file_path}"
        return {
            "content": json.dumps(
                {
                    "success": False,
                    "error": f"Failed to save memory. Detail: {error_msg}",
                }
            ),
            "returnDisplay": f"Error saving memory: {error_msg}",
            "error": {
                "message": error_msg,
                "type": "MEMORY_TOOL_EXECUTION_ERROR",
            },
        }
    except Exception as e:
        error_msg = str(e)
        return {
            "content": json.dumps(
                {
                    "success": False,
                    "error": f"Failed to save memory. Detail: {error_msg}",
                }
            ),
            "returnDisplay": f"Error saving memory: {error_msg}",
            "error": {
                "message": error_msg,
                "type": "MEMORY_TOOL_EXECUTION_ERROR",
            },
        }
