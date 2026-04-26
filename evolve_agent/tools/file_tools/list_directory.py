# Copyright 2025 Google LLC
# SPDX-License-Identifier: Apache-2.0
"""
list_directory tool - Lists files and subdirectories in a directory.

Based on gemini-cli's ls.ts implementation.
"""

import fnmatch
from pathlib import Path
from typing import Any

from nexau.archs.main_sub.agent_state import AgentState
from nexau.archs.sandbox import SandboxStatus
from nexau.archs.tool.builtin._sandbox_utils import get_sandbox, resolve_path


def _should_ignore(filename: str, patterns: list[str] | None) -> bool:
    """Check if filename matches any ignore pattern."""
    if not patterns:
        return False

    for pattern in patterns:
        # Convert glob pattern to fnmatch pattern
        if fnmatch.fnmatch(filename, pattern):
            return True
    return False


def _is_direct_child(entry_path: str, parent_path: str) -> bool:
    """Return True when entry_path is an immediate child of parent_path."""
    entry = Path(entry_path)
    parent = Path(parent_path)
    return entry.parent == parent


def list_directory(
    dir_path: str,
    ignore: list[str] | None = None,
    file_filtering_options: dict[str, bool] | None = None,
    show_hidden: bool = True,
    agent_state: AgentState | None = None,
) -> dict[str, Any]:
    """
    Lists files and subdirectories in a directory.

    Results are sorted with directories first, then alphabetically.
    Can optionally ignore entries matching provided glob patterns.

    Args:
        dir_path: Path to the directory to list
        ignore: List of glob patterns to ignore
        file_filtering_options: Options for respecting .gitignore/.geminiignore
        show_hidden: Whether to include hidden files/directories (starting with '.')

    Returns:
        Dict with content and returnDisplay matching gemini-cli format
    """
    try:
        sandbox = get_sandbox(agent_state)

        # Resolve path (relative -> sandbox work_dir)
        resolved_path = resolve_path(dir_path, sandbox)

        # Check if path exists
        if not sandbox.file_exists(resolved_path):
            error_msg = f"Error: Directory not found or inaccessible: {resolved_path}"
            return {
                "content": error_msg,
                "returnDisplay": "Directory not found or inaccessible.",
                "error": {
                    "message": error_msg,
                    "type": "FILE_NOT_FOUND",
                },
            }

        # Check if it's a directory
        info = sandbox.get_file_info(resolved_path)
        if not info.is_directory:
            error_msg = f"Error: Path is not a directory: {resolved_path}"
            return {
                "content": error_msg,
                "returnDisplay": "Path is not a directory.",
                "error": {
                    "message": error_msg,
                    "type": "PATH_IS_NOT_A_DIRECTORY",
                },
            }

        # Build ignore patterns
        ignore_patterns = list(ignore) if ignore else []

        # Parse file_filtering_options
        respect_git_ignore = True
        if file_filtering_options:
            respect_git_ignore = file_filtering_options.get("respect_git_ignore", True)

        # Read .gitignore if requested
        if respect_git_ignore:
            gitignore_path = str(Path(resolved_path) / ".gitignore")
            if sandbox.file_exists(gitignore_path):
                res = sandbox.read_file(gitignore_path, encoding="utf-8", binary=False)
                if res.status == SandboxStatus.SUCCESS and isinstance(res.content, str):
                    for line in res.content.splitlines():
                        line = line.strip()
                        if line and not line.startswith("#"):
                            ignore_patterns.append(line.rstrip("/"))

        # List directory contents
        entries_info = sandbox.list_files(resolved_path, recursive=False)
        if not entries_info:
            return {
                "content": f"Directory {resolved_path} is empty.",
                "returnDisplay": "Directory is empty.",
            }

        # Process entries
        directories: list[str] = []
        files: list[str] = []
        ignored_count = 0

        for item in entries_info:
            # E2B filesystem listing may include nested descendants in some cases.
            # Keep only immediate children so output matches the requested directory.
            if not _is_direct_child(item.path, resolved_path):
                continue

            entry = Path(item.path).name

            # Skip hidden entries when show_hidden is False
            if not show_hidden and entry.startswith("."):
                ignored_count += 1
                continue

            # Check ignore patterns
            if _should_ignore(entry, ignore_patterns):
                ignored_count += 1
                continue

            try:
                if item.is_directory:
                    directories.append(entry)
                else:
                    files.append(entry)
            except Exception:
                continue

        # Sort: directories first, then files, both alphabetically
        directories.sort(key=str.lower)
        files.sort(key=str.lower)

        # Build formatted list
        formatted_entries: list[str] = []
        for d in directories:
            formatted_entries.append(f"[DIR] {d}")
        for f in files:
            formatted_entries.append(f)

        # Create formatted content for LLM (matching gemini-cli format)
        directory_content = "\n".join(formatted_entries)

        result_message = f"Directory listing for {resolved_path}:\n{directory_content}"
        if ignored_count > 0:
            result_message += f"\n\n({ignored_count} ignored)"

        display_message = f"Listed {len(formatted_entries)} item(s)."
        if ignored_count > 0:
            display_message += f" ({ignored_count} ignored)"

        return {
            "content": result_message,
            "returnDisplay": display_message,
        }

    except Exception as e:
        error_msg = f"Error listing directory: {str(e)}"
        return {
            "content": error_msg,
            "returnDisplay": "Failed to list directory.",
            "error": {
                "message": error_msg,
                "type": "LS_EXECUTION_ERROR",
            },
        }
