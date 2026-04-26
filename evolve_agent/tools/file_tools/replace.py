# Copyright 2025 Google LLC
# SPDX-License-Identifier: Apache-2.0
"""
replace tool (edit) - Replaces text within a file.

Based on gemini-cli's edit.ts implementation.
Supports exact, flexible (whitespace-insensitive), and regex-based replacements.
"""

import difflib
import re
from pathlib import Path
from typing import Any

from nexau.archs.main_sub.agent_state import AgentState
from nexau.archs.sandbox import SandboxStatus
from nexau.archs.tool.builtin._sandbox_utils import get_sandbox, resolve_path


def _detect_line_ending(content: str) -> str:
    """Detect line ending used in content."""
    if "\r\n" in content:
        return "\r\n"
    return "\n"


def _restore_trailing_newline(original: str, modified: str) -> str:
    """Restore original trailing newline state."""
    had_trailing = original.endswith("\n")
    if had_trailing and not modified.endswith("\n"):
        return modified + "\n"
    elif not had_trailing and modified.endswith("\n"):
        return modified.rstrip("\n")
    return modified


def _escape_regex(s: str) -> str:
    """Escape special regex characters."""
    return re.escape(s)


def _safe_literal_replace(content: str, old_string: str, new_string: str) -> str:
    """
    Safe literal string replacement that handles $ sequences correctly.
    Unlike str.replace which might interpret $ in some contexts.
    """
    return content.replace(old_string, new_string)


def _calculate_exact_replacement(
    current_content: str,
    old_string: str,
    new_string: str,
) -> dict[str, Any] | None:
    """
    Try exact string replacement.
    Returns result dict or None if no match.
    """
    normalized_content = current_content
    normalized_search = old_string.replace("\r\n", "\n")
    normalized_replace = new_string.replace("\r\n", "\n")

    occurrences = normalized_content.count(normalized_search)

    if occurrences > 0:
        modified = _safe_literal_replace(
            normalized_content,
            normalized_search,
            normalized_replace,
        )
        modified = _restore_trailing_newline(current_content, modified)
        return {
            "newContent": modified,
            "occurrences": occurrences,
            "finalOldString": normalized_search,
            "finalNewString": normalized_replace,
            "strategy": "exact",
        }

    return None


def _calculate_flexible_replacement(
    current_content: str,
    old_string: str,
    new_string: str,
) -> dict[str, Any] | None:
    """
    Try flexible (whitespace-insensitive) replacement.
    Matches content by stripped/trimmed lines, preserving original indentation.
    """
    normalized_content = current_content
    normalized_search = old_string.replace("\r\n", "\n")
    normalized_replace = new_string.replace("\r\n", "\n")

    # Split into lines, keeping line endings
    source_lines = re.findall(r".*(?:\n|$)", normalized_content)
    if source_lines and source_lines[-1] == "":
        source_lines = source_lines[:-1]

    search_lines_stripped = [line.strip() for line in normalized_search.split("\n")]
    replace_lines = normalized_replace.split("\n")

    flexible_occurrences = 0
    i = 0

    while i <= len(source_lines) - len(search_lines_stripped):
        window = source_lines[i : i + len(search_lines_stripped)]
        window_stripped = [line.strip() for line in window]

        # Check if all lines match
        is_match = all(ws == ss for ws, ss in zip(window_stripped, search_lines_stripped))

        if is_match and len(window_stripped) == len(search_lines_stripped):
            flexible_occurrences += 1

            # Get indentation from first matching line
            first_line = window[0]
            indent_match = re.match(r"^(\s*)", first_line)
            indentation = indent_match.group(1) if indent_match else ""

            # Apply indentation to replacement lines
            new_block = [f"{indentation}{line}" for line in replace_lines]
            new_block_str = "\n".join(new_block)

            # Replace in source_lines
            source_lines[i : i + len(search_lines_stripped)] = [new_block_str]
            i += len(replace_lines)
        else:
            i += 1

    if flexible_occurrences > 0:
        modified = "".join(source_lines)
        modified = _restore_trailing_newline(current_content, modified)
        return {
            "newContent": modified,
            "occurrences": flexible_occurrences,
            "finalOldString": normalized_search,
            "finalNewString": normalized_replace,
            "strategy": "flexible",
        }

    return None


def _calculate_regex_replacement(
    current_content: str,
    old_string: str,
    new_string: str,
) -> dict[str, Any] | None:
    """
    Try regex-based flexible replacement.
    Tokenizes the search string and allows flexible whitespace between tokens.
    """
    normalized_search = old_string.replace("\r\n", "\n")
    normalized_replace = new_string.replace("\r\n", "\n")

    # Add spaces around delimiters for tokenization
    delimiters = ["(", ")", ":", "[", "]", "{", "}", ">", "<", "="]
    processed = normalized_search
    for delim in delimiters:
        processed = processed.replace(delim, f" {delim} ")

    # Split by whitespace and filter empty
    tokens = [t for t in processed.split() if t]

    if not tokens:
        return None

    # Build regex pattern
    escaped_tokens = [_escape_regex(t) for t in tokens]
    pattern = r"\s*".join(escaped_tokens)

    # Final pattern captures leading indentation
    final_pattern = rf"^(\s*){pattern}"

    try:
        match = re.search(final_pattern, current_content, re.MULTILINE)
    except re.error:
        return None

    if not match:
        return None

    indentation = match.group(1) or ""
    new_lines = normalized_replace.split("\n")
    new_block = "\n".join(f"{indentation}{line}" for line in new_lines)

    # Replace only the first occurrence
    modified = re.sub(final_pattern, new_block, current_content, count=1, flags=re.MULTILINE)
    modified = _restore_trailing_newline(current_content, modified)

    return {
        "newContent": modified,
        "occurrences": 1,
        "finalOldString": normalized_search,
        "finalNewString": normalized_replace,
        "strategy": "regex",
    }


def _calculate_replacement(
    current_content: str,
    old_string: str,
    new_string: str,
) -> dict[str, Any]:
    """
    Calculate replacement using multiple strategies.
    Tries: exact -> flexible -> regex
    """
    normalized_search = old_string.replace("\r\n", "\n")
    normalized_replace = new_string.replace("\r\n", "\n")

    # Empty old_string means no replacement
    if normalized_search == "":
        return {
            "newContent": current_content,
            "occurrences": 0,
            "finalOldString": normalized_search,
            "finalNewString": normalized_replace,
            "strategy": "none",
        }

    # Try exact replacement first
    result = _calculate_exact_replacement(current_content, old_string, new_string)
    if result:
        return result

    # Try flexible (whitespace-insensitive) replacement
    result = _calculate_flexible_replacement(current_content, old_string, new_string)
    if result:
        return result

    # Try regex-based replacement
    result = _calculate_regex_replacement(current_content, old_string, new_string)
    if result:
        return result

    # No match found
    return {
        "newContent": current_content,
        "occurrences": 0,
        "finalOldString": normalized_search,
        "finalNewString": normalized_replace,
        "strategy": "none",
    }


def _generate_diff(
    file_path: str,
    original_content: str,
    new_content: str,
) -> str:
    """Generate unified diff."""
    original_lines = original_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)

    diff = difflib.unified_diff(
        original_lines,
        new_lines,
        fromfile=f"Current: {file_path}",
        tofile=f"Proposed: {file_path}",
    )

    return "".join(diff)


def _get_error_result(
    file_path: str,
    occurrences: int,
    expected: int,
    old_string: str,
    new_string: str,
) -> dict[str, Any] | None:
    """Generate error result if replacement failed."""
    if occurrences == 0:
        return {
            "display": "Failed to edit, could not find the string to replace.",
            "raw": f"Failed to edit, 0 occurrences found for old_string in {file_path}. "
            "Ensure you're not escaping content incorrectly and check whitespace, "
            "indentation, and context. Use read_file tool to verify.",
            "type": "EDIT_NO_OCCURRENCE_FOUND",
        }
    elif occurrences != expected:
        term = "occurrence" if expected == 1 else "occurrences"
        return {
            "display": f"Failed to edit, expected {expected} {term} but found {occurrences}.",
            "raw": f"Failed to edit, Expected {expected} {term} but found {occurrences} for old_string in file: {file_path}",
            "type": "EDIT_EXPECTED_OCCURRENCE_MISMATCH",
        }
    elif old_string == new_string:
        return {
            "display": "No changes to apply. The old_string and new_string are identical.",
            "raw": f"No changes to apply. The old_string and new_string are identical in file: {file_path}",
            "type": "EDIT_NO_CHANGE",
        }

    return None


def replace(
    file_path: str,
    old_string: str,
    new_string: str,
    expected_replacements: int = 1,
    instruction: str | None = None,
    modified_by_user: bool = False,
    ai_proposed_content: str | None = None,
    agent_state: AgentState | None = None,
) -> dict[str, Any]:
    """
    Replaces text within a file.

    By default, replaces a single occurrence, but can replace multiple occurrences
    when `expected_replacements` is specified. This tool requires providing
    significant context around the change to ensure precise targeting.

    Supports three replacement strategies:
    1. Exact: Literal string match
    2. Flexible: Whitespace-insensitive line matching
    3. Regex: Token-based flexible matching

    Args:
        file_path: The path to the file to modify
        old_string: The exact literal text to replace
        new_string: The text to replace old_string with
        expected_replacements: Number of replacements expected (default 1)
        instruction: Description of the change being made
        modified_by_user: Whether the edit was modified by the user
        ai_proposed_content: Original AI-proposed content if modified

    Returns:
        Dict with content and returnDisplay matching gemini-cli format
    """
    try:
        sandbox = get_sandbox(agent_state)

        # Resolve path (relative -> sandbox work_dir)
        resolved_path = resolve_path(file_path, sandbox)

        # Check if file exists
        file_exists = sandbox.file_exists(resolved_path)
        is_new_file = old_string == "" and not file_exists

        # Handle new file creation
        if is_new_file:
            write_res = sandbox.write_file(
                resolved_path,
                new_string,
                encoding="utf-8",
                binary=False,
                create_directories=True,
            )
            if write_res.status != SandboxStatus.SUCCESS:
                raise RuntimeError(write_res.error or "Failed to create file")

            return {
                "content": f"Created new file: {resolved_path} with provided content.",
                "returnDisplay": f"Created {file_path}",
            }

        # File must exist for edits (not new file)
        if not file_exists:
            error_msg = f"File not found: {file_path}"
            return {
                "content": error_msg,
                "returnDisplay": "Error: File not found. Use empty old_string to create new file.",
                "error": {
                    "message": error_msg,
                    "type": "FILE_NOT_FOUND",
                },
            }

        # Trying to create file that already exists
        if old_string == "" and file_exists:
            error_msg = f"File already exists, cannot create: {file_path}"
            return {
                "content": error_msg,
                "returnDisplay": "Error: Failed to edit. Attempted to create existing file.",
                "error": {
                    "message": error_msg,
                    "type": "ATTEMPT_TO_CREATE_EXISTING_FILE",
                },
            }

        # Read current content
        read_res = sandbox.read_file(resolved_path, encoding="utf-8", binary=False)
        if read_res.status != SandboxStatus.SUCCESS:
            # Try latin-1 as best-effort fallback
            read_res = sandbox.read_file(resolved_path, encoding="latin-1", binary=False)
        if read_res.status != SandboxStatus.SUCCESS or not isinstance(read_res.content, str):
            raise RuntimeError(read_res.error or "Failed to read file")
        current_content = read_res.content

        # Detect original line ending
        original_line_ending = _detect_line_ending(current_content)

        # Normalize to \n for processing
        normalized_content = current_content.replace("\r\n", "\n")

        # Calculate replacement
        result: dict[str, Any] = _calculate_replacement(normalized_content, old_string, new_string)

        # Check for errors
        error: dict[str, Any] | None = _get_error_result(
            file_path,
            result["occurrences"],
            expected_replacements,
            result["finalOldString"],
            result["finalNewString"],
        )

        if error:
            return {
                "content": error["raw"],
                "returnDisplay": f"Error: {error['display']}",
                "error": {
                    "message": error["raw"],
                    "type": error["type"],
                },
            }

        # Apply replacement and write
        new_content: str = result["newContent"]

        # Restore original line endings if CRLF
        if original_line_ending == "\r\n":
            new_content = new_content.replace("\n", "\r\n")

        # Write the file through sandbox
        write_res = sandbox.write_file(
            resolved_path,
            new_content,
            encoding="utf-8",
            binary=False,
            create_directories=True,
        )
        if write_res.status != SandboxStatus.SUCCESS:
            raise RuntimeError(write_res.error or "Failed to write file")

        # Generate diff for display
        file_diff = _generate_diff(
            resolved_path,
            normalized_content,
            result["newContent"],
        )

        # Build success message
        llm_message = f"Successfully modified file: {resolved_path} ({result['occurrences']} replacements, strategy: {result['strategy']})."

        if modified_by_user:
            llm_message += f" User modified the `new_string` content to be: {new_string}."

        return {
            "content": llm_message,
            "returnDisplay": {
                "fileDiff": file_diff,
                "fileName": Path(resolved_path).name,
                "filePath": resolved_path,
                "originalContent": normalized_content,
                "newContent": result["newContent"],
                "isNewFile": False,
                "occurrences": result["occurrences"],
                "strategy": result["strategy"],
            },
        }

    except PermissionError:
        error_msg = f"Permission denied: {file_path}"
        return {
            "content": error_msg,
            "returnDisplay": error_msg,
            "error": {
                "message": error_msg,
                "type": "PERMISSION_DENIED",
            },
        }
    except Exception as e:
        error_msg = f"Error executing edit: {str(e)}"
        return {
            "content": error_msg,
            "returnDisplay": f"Error: {str(e)}",
            "error": {
                "message": error_msg,
                "type": "EDIT_EXECUTION_ERROR",
            },
        }
