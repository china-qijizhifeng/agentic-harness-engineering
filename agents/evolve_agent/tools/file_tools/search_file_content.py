# Copyright 2025 Google LLC
# SPDX-License-Identifier: Apache-2.0
"""
search_file_content tool (grep) - Searches for regex patterns in file contents.

Based on gemini-cli's grep.ts implementation.
Uses prioritized strategies: git grep -> system grep -> JavaScript fallback.
"""

import fnmatch
import re
import shlex
from pathlib import Path
from typing import Any

from nexau.archs.main_sub.agent_state import AgentState
from nexau.archs.sandbox import BaseSandbox, SandboxStatus
from nexau.archs.tool.builtin._sandbox_utils import get_sandbox, resolve_path

# Configuration constants
DEFAULT_TOTAL_MAX_MATCHES = 500
DEFAULT_SEARCH_TIMEOUT_MS = 30000
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
]


def _rg_available(sandbox: BaseSandbox) -> bool:
    """Check if ripgrep (rg) is available inside the sandbox."""
    try:
        res = sandbox.execute_bash("rg --version", timeout=5000)
        return res.status == SandboxStatus.SUCCESS and (res.exit_code == 0)
    except Exception:
        return False


def _parse_grep_line(line: str, base_path: str) -> dict[str, Any] | None:
    """
    Parse a single line of grep-like output.
    Expects format: filePath:lineNumber:lineContent
    """
    if not line.strip():
        return None

    # Use regex to locate the first occurrence of :<digits>:
    match = re.match(r"^(.+?):(\d+):(.*)$", line)
    if not match:
        return None

    file_path_raw, line_number_str, line_content = match.groups()

    try:
        line_number = int(line_number_str)
    except ValueError:
        return None

    base = Path(base_path)
    raw = file_path_raw.strip()
    p = Path(raw)
    if p.is_absolute():
        try:
            rel = str(p.relative_to(base))
        except Exception:
            rel = raw
    else:
        rel = raw

    return {
        "filePath": rel,
        "lineNumber": line_number,
        "line": line_content,
    }


def _rg_grep(
    *,
    pattern: str,
    search_path: str,
    include: str | None,
    max_matches: int,
    excludes: list[str],
    sandbox: BaseSandbox,
) -> list[dict[str, Any]]:
    """Run ripgrep in the sandbox and parse results."""

    cmd: list[str] = [
        "rg",
        "-n",
        "-H",
        "-i",
        "--no-heading",
        "--color",
        "never",
        "--max-count",
        str(max_matches),
    ]

    # Excludes: best-effort glob negatives.
    for ex in excludes:
        if ex.startswith("*"):
            cmd.extend(["--glob", f"!{ex}"])
        else:
            cmd.extend(["--glob", f"!{ex}/**"])

    if include:
        cmd.extend(["--glob", include])

    cmd.append(pattern)
    cmd.append(search_path)

    cmd_str = " ".join(shlex.quote(x) for x in cmd)
    res = sandbox.execute_bash(cmd_str, timeout=DEFAULT_SEARCH_TIMEOUT_MS)

    if res.status == SandboxStatus.TIMEOUT:
        raise TimeoutError("search_file_content timed out")

    # `rg` exit codes: 0 matches found, 1 no matches, 2 error
    if res.exit_code == 1:
        return []
    if res.status != SandboxStatus.SUCCESS or res.exit_code not in (0, 1):
        raise RuntimeError(res.stderr or res.error or "ripgrep failed")

    matches: list[dict[str, Any]] = []
    for line in (res.stdout or "").splitlines():
        parsed = _parse_grep_line(line, search_path)
        if parsed:
            matches.append(parsed)
            if len(matches) >= max_matches:
                break
    return matches


def _python_grep(
    pattern: str,
    search_path: str,
    include: str | None,
    max_matches: int,
    excludes: list[str],
    sandbox: BaseSandbox,
) -> list[dict[str, Any]]:
    """
    Pure Python grep implementation as fallback.
    """
    matches: list[dict[str, Any]] = []

    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error:
        return matches

    def should_exclude(path: str) -> bool:
        """Check if path should be excluded."""
        parts = Path(path).parts
        for part in parts:
            for exclude in excludes:
                if fnmatch.fnmatch(part, exclude):
                    return True
        return False

    def matches_include(filename: str) -> bool:
        """Check if filename matches include pattern."""
        if not include:
            return True
        return fnmatch.fnmatch(filename, include)

    # List files via sandbox (recursive) and scan contents.
    try:
        infos = sandbox.list_files(search_path, recursive=True)
    except Exception:
        infos = []

    for info in infos:
        if len(matches) >= max_matches:
            return matches

        if not info.is_file:
            continue

        full_path = info.path
        try:
            rel_path = str(Path(full_path).relative_to(search_path))
        except Exception:
            rel_path = full_path

        if should_exclude(rel_path):
            continue

        if include and not (matches_include(Path(rel_path).name) or fnmatch.fnmatch(rel_path, include)):
            continue

        read_res = sandbox.read_file(full_path, encoding="utf-8", binary=False)
        if read_res.status != SandboxStatus.SUCCESS or not isinstance(read_res.content, str):
            continue

        for line_num, line in enumerate(read_res.content.splitlines(), 1):
            if len(matches) >= max_matches:
                return matches
            if regex.search(line):
                matches.append(
                    {
                        "filePath": rel_path,
                        "lineNumber": line_num,
                        "line": line.rstrip("\n\r"),
                    }
                )

    return matches


def search_file_content(
    pattern: str,
    dir_path: str | None = None,
    include: str | None = None,
    agent_state: AgentState | None = None,
) -> dict[str, Any]:
    """
    Searches for a regular expression pattern within file contents.

    Uses a prioritized strategy:
    1. git grep (if in git repository)
    2. System grep (if available)
    3. Pure Python fallback

    Returns lines containing matches with file paths and line numbers.

    Args:
        pattern: The regular expression pattern to search for
        dir_path: Directory to search in (optional, defaults to cwd)
        include: Glob pattern to filter files (e.g., "*.js", "*.{ts,tsx}")

    Returns:
        Dict with content and returnDisplay matching gemini-cli format
    """
    try:
        sandbox = get_sandbox(agent_state)

        # Validate pattern
        try:
            re.compile(pattern)
        except re.error as e:
            error_msg = f"Invalid regular expression pattern: {pattern}. Error: {str(e)}"
            return {
                "content": error_msg,
                "returnDisplay": "Error: Invalid regex pattern.",
                "error": {
                    "message": error_msg,
                    "type": "INVALID_PATTERN",
                },
            }

        # Determine search directory
        if dir_path:
            search_path = resolve_path(dir_path, sandbox)

            if not sandbox.file_exists(search_path):
                error_msg = f"Path does not exist: {search_path}"
                return {
                    "content": error_msg,
                    "returnDisplay": "Error: Path does not exist.",
                    "error": {
                        "message": error_msg,
                        "type": "FILE_NOT_FOUND",
                    },
                }

            if not sandbox.get_file_info(search_path).is_directory:
                error_msg = f"Path is not a directory: {search_path}"
                return {
                    "content": error_msg,
                    "returnDisplay": "Error: Path is not a directory.",
                    "error": {
                        "message": error_msg,
                        "type": "PATH_IS_NOT_A_DIRECTORY",
                    },
                }
        else:
            search_path = str(sandbox.work_dir)

        search_dir_display = dir_path or "."

        # Try search strategies in order
        max_matches = DEFAULT_TOTAL_MAX_MATCHES
        matches: list[dict[str, Any]]
        strategy_used = "python fallback"

        if _rg_available(sandbox):
            matches = _rg_grep(
                pattern=pattern,
                search_path=search_path,
                include=include,
                max_matches=max_matches,
                excludes=DEFAULT_EXCLUDES,
                sandbox=sandbox,
            )
            strategy_used = "rg"
        else:
            matches = _python_grep(
                pattern,
                search_path,
                include,
                max_matches,
                DEFAULT_EXCLUDES,
                sandbox,
            )

        # Build location description
        search_location = f'in path "{search_dir_display}"'
        filter_desc = f' (filter: "{include}")' if include else ""

        # No matches found
        if not matches:
            no_match_msg = f'No matches found for pattern "{pattern}" {search_location}{filter_desc}.'
            return {
                "content": no_match_msg,
                "returnDisplay": "No matches found",
            }

        # Check if results were truncated
        was_truncated = len(matches) >= max_matches

        # Group matches by file
        matches_by_file: dict[str, list[dict[str, Any]]] = {}
        for match in matches:
            file_key = match["filePath"]
            if file_key not in matches_by_file:
                matches_by_file[file_key] = []
            matches_by_file[file_key].append(match)

        # Sort matches within each file by line number
        for file_matches in matches_by_file.values():
            file_matches.sort(key=lambda m: m["lineNumber"])

        # Build result
        match_count = len(matches)
        match_term = "match" if match_count == 1 else "matches"

        truncation_note = f" (results limited to {max_matches} matches for performance)" if was_truncated else ""

        llm_content = (
            f'Found {match_count} {match_term} for pattern "{pattern}" '
            f"{search_location}{filter_desc}{truncation_note} (strategy: {strategy_used}):\n---\n"
        )

        for file_path, file_matches in matches_by_file.items():
            llm_content += f"File: {file_path}\n"
            for match in file_matches:
                trimmed_line = match["line"].strip()
                llm_content += f"L{match['lineNumber']}: {trimmed_line}\n"
            llm_content += "---\n"

        return {
            "content": llm_content.strip(),
            "returnDisplay": f"Found {match_count} {match_term}{' (limited)' if was_truncated else ''}",
        }

    except Exception as e:
        error_msg = f"Error during grep search operation: {str(e)}"
        return {
            "content": error_msg,
            "returnDisplay": f"Error: {str(e)}",
            "error": {
                "message": error_msg,
                "type": "GREP_EXECUTION_ERROR",
            },
        }
