# Copyright 2025 Google LLC
# SPDX-License-Identifier: Apache-2.0
"""
read_many_files tool - Reads content from multiple files using glob patterns.

Based on gemini-cli's read-many-files.ts implementation.
Concatenates text file contents with separators and supports binary files.
"""

import base64
import fnmatch
import mimetypes
from pathlib import Path
from typing import Any

from nexau.archs.main_sub.agent_state import AgentState
from nexau.archs.sandbox import BaseSandbox, SandboxStatus
from nexau.archs.tool.builtin._sandbox_utils import get_sandbox

# Configuration constants
REFERENCE_CONTENT_END = "REFERENCE_CONTENT_END"
MAX_LINE_LENGTH = 2000
DEFAULT_OUTPUT_SEPARATOR_FORMAT = "--- {filePath} ---"
DEFAULT_ENCODING = "utf-8"

# Default exclusion patterns
DEFAULT_EXCLUDES = [
    "node_modules/**",
    ".git/**",
    "__pycache__/**",
    "venv/**",
    ".venv/**",
    "dist/**",
    "build/**",
    ".tox/**",
    ".eggs/**",
    "*.egg-info/**",
    "*.pyc",
    "*.pyo",
    "*.so",
    "*.o",
    "*.a",
    "*.lib",
    "*.dll",
    "*.exe",
]

# Supported binary file types
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".aiff", ".aac", ".ogg", ".flac"}
PDF_EXTENSION = ".pdf"


def _detect_file_type(file_path: str) -> str:
    """Detect file type based on extension."""
    ext = Path(file_path).suffix.lower()
    if ext in IMAGE_EXTENSIONS:
        return "image"
    elif ext in AUDIO_EXTENSIONS:
        return "audio"
    elif ext == PDF_EXTENSION:
        return "pdf"
    return "text"


def _should_exclude(path: str, excludes: list[str]) -> bool:
    """Check if path matches any exclude pattern."""
    for pattern in excludes:
        if fnmatch.fnmatch(path, pattern):
            return True
        # Also check just the filename
        if fnmatch.fnmatch(Path(path).name, pattern):
            return True
    return False


def _match_glob_patterns(
    base_dir: str,
    include_patterns: list[str],
    exclude_patterns: list[str],
    sandbox: BaseSandbox,
) -> list[str]:
    """Find files matching glob patterns via sandbox."""
    matched_files: set[str] = set()

    for pattern in include_patterns:
        # Normalize pattern
        normalized = pattern.replace("\\", "/")

        # Make pattern relative to base_dir
        if Path(normalized).is_absolute():
            full_pattern = normalized
        else:
            full_pattern = f"{base_dir}/{normalized}"

        try:
            matches = sandbox.glob(full_pattern, recursive=True)
        except Exception:
            matches = []

        for match in matches:
            try:
                info = sandbox.get_file_info(match)
                if not info.is_file:
                    continue
            except Exception:
                continue

            try:
                rel_path = str(Path(match).relative_to(base_dir)).replace("\\", "/")
            except Exception:
                rel_path = match

            if not _should_exclude(rel_path, exclude_patterns):
                matched_files.add(match)

    return sorted(matched_files)


def _read_binary_file(file_path: str, sandbox: BaseSandbox) -> dict[str, Any]:
    """Read binary file and return as inline data (via sandbox)."""
    ext = Path(file_path).suffix.lower()

    res = sandbox.read_file(file_path, binary=True)
    if res.status != SandboxStatus.SUCCESS:
        raise RuntimeError(res.error or "Failed to read binary file")

    if isinstance(res.content, (bytes, bytearray)):
        content = bytes(res.content)
    elif isinstance(res.content, str):
        content = res.content.encode("utf-8", errors="replace")
    else:
        content = b""

    mime_type, _ = mimetypes.guess_type(file_path)
    if not mime_type:
        if ext in IMAGE_EXTENSIONS:
            mime_type = f"image/{ext[1:]}"
        elif ext in AUDIO_EXTENSIONS:
            mime_type = f"audio/{ext[1:]}"
        elif ext == PDF_EXTENSION:
            mime_type = "application/pdf"
        else:
            mime_type = "application/octet-stream"

    return {
        "inlineData": {
            "mimeType": mime_type,
            "data": base64.b64encode(content).decode("utf-8"),
        }
    }


def _read_text_file(file_path: str, sandbox: BaseSandbox, max_lines: int = 2000) -> dict[str, Any]:
    """Read text file content with optional truncation (via sandbox)."""
    res = sandbox.read_file(file_path, encoding="utf-8", binary=False)
    if res.status != SandboxStatus.SUCCESS:
        res = sandbox.read_file(file_path, encoding="latin-1", binary=False)
    if res.status != SandboxStatus.SUCCESS or not isinstance(res.content, str):
        return {"error": res.error or "Failed to read file"}

    lines = res.content.splitlines(keepends=True)
    is_truncated = len(lines) > max_lines or bool(res.truncated)
    if len(lines) > max_lines:
        lines = lines[:max_lines]

    # Truncate individual lines exceeding MAX_LINE_LENGTH
    processed: list[str] = []
    for line in lines:
        if len(line) > MAX_LINE_LENGTH:
            ends_with_newline = line.endswith("\n")
            processed.append(line[:MAX_LINE_LENGTH] + "... [truncated]" + ("\n" if ends_with_newline else ""))
            is_truncated = True
        else:
            processed.append(line)
    content = "".join(processed)
    return {"content": content, "isTruncated": is_truncated, "totalLines": len(lines)}


def _is_explicitly_requested(
    file_path: str,
    include_patterns: list[str],
) -> bool:
    """Check if file was explicitly requested by name or extension."""
    ext = Path(file_path).suffix.lower()
    filename = Path(file_path).name
    name_without_ext = Path(file_path).stem

    for pattern in include_patterns:
        pattern_lower = pattern.lower()
        # Check if extension is in pattern
        if ext and ext in pattern_lower:
            return True
        # Check if filename is in pattern
        if filename.lower() in pattern_lower or pattern_lower in filename.lower():
            return True
        # Check if name without extension is in pattern
        if name_without_ext.lower() in pattern_lower:
            return True

    return False


def read_many_files(
    include: list[str],
    exclude: list[str] | None = None,
    recursive: bool = True,
    use_default_excludes: bool = True,
    file_filtering_options: dict[str, bool] | None = None,
    agent_state: AgentState | None = None,
) -> dict[str, Any]:
    """
    Reads content from multiple files specified by glob patterns.

    For text files, concatenates their content into a single string with separators.
    For explicitly requested binary files (image/audio/PDF), includes them as inline data.

    Args:
        include: Array of glob patterns or paths to include
        exclude: Optional glob patterns to exclude
        recursive: Whether to search recursively (default True)
        use_default_excludes: Whether to apply default exclusion patterns (default True)
        file_filtering_options: Options for respecting .gitignore/.geminiignore

    Returns:
        Dict with content and returnDisplay matching gemini-cli format
    """
    try:
        sandbox = get_sandbox(agent_state)

        if not include:
            return {
                "content": "No include patterns specified.",
                "returnDisplay": "Error: No include patterns specified.",
                "error": {
                    "message": "No include patterns specified.",
                    "type": "INVALID_PARAMETERS",
                },
            }

        # Determine base directory
        base_dir = str(sandbox.work_dir)

        # Build exclusion patterns
        exclude_patterns = list(exclude) if exclude else []
        if use_default_excludes:
            exclude_patterns = DEFAULT_EXCLUDES + exclude_patterns

        # Try to read .gitignore if requested
        respect_git_ignore = True
        if file_filtering_options:
            respect_git_ignore = file_filtering_options.get("respect_git_ignore", True)

        if respect_git_ignore:
            gitignore_path = str(Path(base_dir) / ".gitignore")
            if sandbox.file_exists(gitignore_path):
                res = sandbox.read_file(gitignore_path, encoding="utf-8", binary=False)
                if res.status == SandboxStatus.SUCCESS and isinstance(res.content, str):
                    for line in res.content.splitlines():
                        line = line.strip()
                        if line and not line.startswith("#"):
                            exclude_patterns.append(line.rstrip("/"))

        # Find matching files
        matched_files = _match_glob_patterns(base_dir, include, exclude_patterns, sandbox)

        if not matched_files:
            return {
                "content": "No files matching the criteria were found or all were skipped.",
                "returnDisplay": "No files found matching patterns.",
            }

        # Process files
        content_parts: list[str | dict[str, Any]] = []
        processed_files: list[str] = []
        skipped_files: list[dict[str, Any]] = []

        for file_path in matched_files:
            try:
                rel_path = str(Path(file_path).relative_to(base_dir)).replace("\\", "/")
            except Exception:
                rel_path = file_path
            file_type = _detect_file_type(file_path)

            # Handle binary files (image/audio/pdf)
            if file_type in ("image", "audio", "pdf"):
                # Only include if explicitly requested
                if not _is_explicitly_requested(file_path, include):
                    skipped_files.append(
                        {
                            "path": rel_path,
                            "reason": f"asset file ({file_type}) was not explicitly requested by name or extension",
                        }
                    )
                    continue

                try:
                    binary_data = _read_binary_file(file_path, sandbox)
                    content_parts.append(binary_data)
                    processed_files.append(rel_path)
                except Exception as e:
                    skipped_files.append(
                        {
                            "path": rel_path,
                            "reason": f"Read error: {str(e)}",
                        }
                    )
            else:
                # Text file
                try:
                    result = _read_text_file(file_path, sandbox)

                    if "error" in result:
                        skipped_files.append(
                            {
                                "path": rel_path,
                                "reason": f"Read error: {result['error']}",
                            }
                        )
                        continue

                    separator = DEFAULT_OUTPUT_SEPARATOR_FORMAT.replace("{filePath}", file_path)

                    file_content = ""
                    if result.get("isTruncated"):
                        file_content += (
                            "[WARNING: This file was truncated. To view the full content, use the "
                            "'read_file' tool on this specific file.]\n\n"
                        )

                    file_content += result["content"]
                    content_parts.append(f"{separator}\n\n{file_content}\n\n")
                    processed_files.append(rel_path)

                except Exception as e:
                    skipped_files.append(
                        {
                            "path": rel_path,
                            "reason": f"Unexpected error: {str(e)}",
                        }
                    )

        # Build display message
        display_message = f"### ReadManyFiles Result (Target Dir: `{base_dir}`)\n\n"

        if processed_files:
            display_message += f"Successfully read and concatenated content from **{len(processed_files)} file(s)**.\n"

            if len(processed_files) <= 10:
                display_message += "\n**Processed Files:**\n"
                for p in processed_files:
                    display_message += f"- `{p}`\n"
            else:
                display_message += "\n**Processed Files (first 10 shown):**\n"
                for p in processed_files[:10]:
                    display_message += f"- `{p}`\n"
                display_message += f"- ...and {len(processed_files) - 10} more.\n"

        if skipped_files:
            if not processed_files:
                display_message += "No files were read and concatenated based on the criteria.\n"

            if len(skipped_files) <= 5:
                display_message += f"\n**Skipped {len(skipped_files)} item(s):**\n"
            else:
                display_message += f"\n**Skipped {len(skipped_files)} item(s) (first 5 shown):**\n"

            for f in skipped_files[:5]:
                display_message += f"- `{f['path']}` (Reason: {f['reason']})\n"

            if len(skipped_files) > 5:
                display_message += f"- ...and {len(skipped_files) - 5} more.\n"
        elif not processed_files:
            display_message += "No files were read and concatenated based on the criteria.\n"

        # Build content
        if content_parts:
            # Combine text parts
            llm_content: list[str | dict[str, Any]] = []
            for part in content_parts:
                if isinstance(part, str):
                    llm_content.append(part)
                else:
                    # Binary data part - keep as-is
                    llm_content.append(part)

            # Add terminator
            llm_content.append(f"\n{REFERENCE_CONTENT_END}")
        else:
            llm_content = ["No files matching the criteria were found or all were skipped."]

        return {
            "content": llm_content,
            "returnDisplay": display_message.strip(),
        }

    except Exception as e:
        error_msg = f"Error during file search: {str(e)}"
        return {
            "content": error_msg,
            "returnDisplay": f"## File Search Error\n\nAn error occurred while searching for files:\n```\n{str(e)}\n```",
            "error": {
                "message": error_msg,
                "type": "READ_MANY_FILES_SEARCH_ERROR",
            },
        }
