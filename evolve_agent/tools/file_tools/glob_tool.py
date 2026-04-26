# Copyright 2025 Google LLC
# SPDX-License-Identifier: Apache-2.0
"""
glob tool - Finds files matching glob patterns.

Based on gemini-cli's glob.ts implementation.
Returns absolute paths sorted by modification time (newest first).
"""

import fnmatch
from pathlib import Path
from typing import Any

from nexau.archs.main_sub.agent_state import AgentState
from nexau.archs.sandbox import BaseSandbox, SandboxStatus
from nexau.archs.tool.builtin._sandbox_utils import get_sandbox, resolve_path

# Default exclusions matching gemini-cli
DEFAULT_EXCLUDES = [
    "node_modules",
    ".git",
    "__pycache__",
    "venv",
    ".venv",
    "dist",
    "build",
    ".tox",
    ".eggs",
    "*.egg-info",
]


def _should_exclude(path: str, excludes: list[str]) -> bool:
    """Check if path should be excluded."""
    parts = Path(path).parts
    for part in parts:
        for pattern in excludes:
            if fnmatch.fnmatch(part, pattern):
                return True
    return False


def _sort_paths_by_mtime_desc(paths_with_mtime: list[tuple[str, str | None]]) -> list[str]:
    """Sort by modification time descending (string mtime), then path."""
    # LocalSandbox uses "%Y-%m-%d %H:%M:%S", which sorts lexicographically.
    paths_with_mtime.sort(key=lambda t: (t[1] or "", t[0]))
    # reverse for newest first
    return [p for p, _ in reversed(paths_with_mtime)]


def glob(
    pattern: str,
    dir_path: str | None = None,
    case_sensitive: bool = False,
    respect_git_ignore: bool = True,
    agent_state: AgentState | None = None,
) -> dict[str, Any]:
    """
    Finds files matching a glob pattern.

    Returns absolute paths sorted by modification time (newest first).
    Ideal for quickly locating files based on their name or path structure.

    Args:
        pattern: Glob pattern (e.g., "**/*.py", "docs/*.md")
        dir_path: Directory to search in (optional, defaults to cwd)
        case_sensitive: Whether matching should be case-sensitive
        respect_git_ignore: Whether to respect .gitignore patterns

    Returns:
        Dict with content and returnDisplay matching gemini-cli format
    """
    try:
        sandbox: BaseSandbox = get_sandbox(agent_state)

        # Validate pattern
        if not pattern or not pattern.strip():
            return {
                "content": "The 'pattern' parameter cannot be empty.",
                "returnDisplay": "Error: Empty pattern",
                "error": {
                    "message": "The 'pattern' parameter cannot be empty.",
                    "type": "INVALID_PATTERN",
                },
            }

        # Determine search directory
        search_dir = resolve_path(dir_path, sandbox) if dir_path else str(sandbox.work_dir)

        if not sandbox.file_exists(search_dir):
            error_msg = f"Search path does not exist {search_dir}"
            return {
                "content": error_msg,
                "returnDisplay": "Error: Path not found",
                "error": {
                    "message": error_msg,
                    "type": "DIRECTORY_NOT_FOUND",
                },
            }

        if not sandbox.get_file_info(search_dir).is_directory:
            error_msg = f"Search path is not a directory: {search_dir}"
            return {
                "content": error_msg,
                "returnDisplay": "Error: Not a directory",
                "error": {
                    "message": error_msg,
                    "type": "NOT_A_DIRECTORY",
                },
            }

        # Build exclusion list
        excludes = list(DEFAULT_EXCLUDES)

        # Try to read .gitignore if requested
        if respect_git_ignore:
            gitignore_path = str(Path(search_dir) / ".gitignore")
            if sandbox.file_exists(gitignore_path):
                res = sandbox.read_file(gitignore_path, encoding="utf-8", binary=False)
                if res.status == SandboxStatus.SUCCESS and isinstance(res.content, str):
                    for line in res.content.splitlines():
                        line = line.strip()
                        if line and not line.startswith("#"):
                            excludes.append(line.rstrip("/"))

        # Construct full pattern path (match gemini-cli behavior)
        if Path(pattern).is_absolute():
            full_pattern = pattern
        elif pattern.startswith("**/"):
            full_pattern = f"{search_dir}/{pattern}"
        else:
            full_pattern = f"{search_dir}/**/{pattern}"

        # Find matching paths through sandbox
        matches = sandbox.glob(full_pattern, recursive=True)

        # Filter by excludes and collect modification times (include both files and dirs)
        files_with_mtime: list[tuple[str, str | None]] = []
        for m in matches:
            try:
                info = sandbox.get_file_info(m)

                try:
                    rel = str(Path(m).relative_to(search_dir))
                except Exception:
                    rel = m

                if _should_exclude(rel, excludes):
                    continue

                files_with_mtime.append((m, info.modified_time))
            except Exception:
                continue

        # Sort by modification time (newest first)
        sorted_paths = _sort_paths_by_mtime_desc(files_with_mtime)

        # Count ignored files
        ignored_count = len(matches) - len(files_with_mtime)

        # Build result matching gemini-cli format
        if not sorted_paths:
            message = f'No files found matching pattern "{pattern}" within {search_dir}'
            if ignored_count > 0:
                message += f" ({ignored_count} files were ignored)"
            return {
                "content": message,
                "returnDisplay": "No files found",
            }

        file_count = len(sorted_paths)
        file_list = "\n".join(sorted_paths)

        result_message = f'Found {file_count} match(es) for "{pattern}" within {search_dir}'
        if ignored_count > 0:
            result_message += f" ({ignored_count} additional files were ignored)"
        result_message += f", sorted by modification time (newest first):\n{file_list}"

        return {
            "content": result_message,
            "returnDisplay": f"Found {file_count} matching file(s)",
        }

    except Exception as e:
        error_msg = f"Error during glob search operation: {str(e)}"
        return {
            "content": error_msg,
            "returnDisplay": "Error: An unexpected error occurred.",
            "error": {
                "message": error_msg,
                "type": "GLOB_EXECUTION_ERROR",
            },
        }
