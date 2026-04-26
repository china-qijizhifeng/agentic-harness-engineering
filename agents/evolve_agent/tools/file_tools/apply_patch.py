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

"""Codex-style apply_patch builtin implemented on top of NexAU sandbox APIs."""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import PurePath, PureWindowsPath
from typing import Any

from nexau.archs.main_sub.agent_state import AgentState
from nexau.archs.sandbox import BaseSandbox, SandboxStatus
from nexau.archs.tool.builtin._sandbox_utils import get_sandbox, resolve_path

BEGIN_PATCH_MARKER = "*** Begin Patch"
END_PATCH_MARKER = "*** End Patch"
ADD_FILE_MARKER = "*** Add File: "
DELETE_FILE_MARKER = "*** Delete File: "
UPDATE_FILE_MARKER = "*** Update File: "
MOVE_TO_MARKER = "*** Move to: "
EOF_MARKER = "*** End of File"
CHANGE_CONTEXT_MARKER = "@@ "
EMPTY_CHANGE_CONTEXT_MARKER = "@@"


class ApplyPatchError(Exception):
    """Base error for apply_patch failures."""


class InvalidPatchError(ApplyPatchError):
    """Raised when the patch envelope is malformed."""


class InvalidHunkError(ApplyPatchError):
    """Raised when a patch hunk is malformed."""

    def __init__(self, message: str, line_number: int):
        super().__init__(message)
        self.message = message
        self.line_number = line_number


class PatchApplicationError(ApplyPatchError):
    """Raised when a parsed patch cannot be applied."""


@dataclass(slots=True)
class AddFileHunk:
    path: str
    contents: str


@dataclass(slots=True)
class DeleteFileHunk:
    path: str


@dataclass(slots=True)
class UpdateFileChunk:
    change_context: str | None
    old_lines: list[str]
    new_lines: list[str]
    is_end_of_file: bool


@dataclass(slots=True)
class UpdateFileHunk:
    path: str
    move_path: str | None
    chunks: list[UpdateFileChunk]


PatchHunk = AddFileHunk | DeleteFileHunk | UpdateFileHunk


@dataclass(slots=True)
class TextFileContents:
    content: str
    encoding: str


UNICODE_NORMALIZATION_MAP = {
    "\u2010": "-",
    "\u2011": "-",
    "\u2012": "-",
    "\u2013": "-",
    "\u2014": "-",
    "\u2015": "-",
    "\u2212": "-",
    "\u2018": "'",
    "\u2019": "'",
    "\u201a": "'",
    "\u201b": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u201e": '"',
    "\u201f": '"',
    "\u00a0": " ",
    "\u2002": " ",
    "\u2003": " ",
    "\u2004": " ",
    "\u2005": " ",
    "\u2006": " ",
    "\u2007": " ",
    "\u2008": " ",
    "\u2009": " ",
    "\u200a": " ",
    "\u202f": " ",
    "\u205f": " ",
    "\u3000": " ",
}


def _error_result(message: str, error_type: str) -> dict[str, Any]:
    """Return a standardized tool error payload."""
    return {
        "content": message,
        "returnDisplay": f"Error: {message}",
        "error": {
            "message": message,
            "type": error_type,
        },
    }


def _normalize_unicode_line(value: str) -> str:
    """Normalize common unicode punctuation to ASCII equivalents."""
    return "".join(UNICODE_NORMALIZATION_MAP.get(ch, ch) for ch in value.strip())


def _seek_sequence(lines: list[str], pattern: list[str], start: int, eof: bool) -> int | None:
    """Find a sequence of lines with progressively more permissive matching."""
    if not pattern:
        return start
    if len(pattern) > len(lines):
        return None

    search_start = len(lines) - len(pattern) if eof and len(lines) >= len(pattern) else start
    max_index = len(lines) - len(pattern)

    for index in range(search_start, max_index + 1):
        if lines[index : index + len(pattern)] == pattern:
            return index

    for index in range(search_start, max_index + 1):
        if all(lines[index + offset].rstrip() == item.rstrip() for offset, item in enumerate(pattern)):
            return index

    for index in range(search_start, max_index + 1):
        if all(lines[index + offset].strip() == item.strip() for offset, item in enumerate(pattern)):
            return index

    for index in range(search_start, max_index + 1):
        if all(_normalize_unicode_line(lines[index + offset]) == _normalize_unicode_line(item) for offset, item in enumerate(pattern)):
            return index

    return None


def _strip_lenient_heredoc(patch: str) -> str:
    """Support a Codex-compatible heredoc wrapper as a best-effort fallback."""
    lines = patch.strip().splitlines()
    if len(lines) >= 4 and lines[0] in {"<<EOF", "<<'EOF'", '<<"EOF"'} and lines[-1].endswith("EOF"):
        return "\n".join(lines[1:-1])
    return patch


def _check_patch_boundaries(lines: list[str]) -> None:
    """Validate the patch begin/end markers."""
    first_line = lines[0].strip() if lines else None
    last_line = lines[-1].strip() if lines else None

    if first_line != BEGIN_PATCH_MARKER:
        raise InvalidPatchError("The first line of the patch must be '*** Begin Patch'")
    if last_line != END_PATCH_MARKER:
        raise InvalidPatchError("The last line of the patch must be '*** End Patch'")


def _parse_update_file_chunk(lines: list[str], line_number: int, allow_missing_context: bool) -> tuple[UpdateFileChunk, int]:
    """Parse a single update hunk chunk."""
    if not lines:
        raise InvalidHunkError("Update hunk does not contain any lines", line_number)

    if lines[0] == EMPTY_CHANGE_CONTEXT_MARKER:
        change_context: str | None = None
        start_index = 1
    elif lines[0].startswith(CHANGE_CONTEXT_MARKER):
        change_context = lines[0][len(CHANGE_CONTEXT_MARKER) :]
        start_index = 1
    else:
        if not allow_missing_context:
            raise InvalidHunkError(
                f"Expected update hunk to start with a @@ context marker, got: '{lines[0]}'",
                line_number,
            )
        change_context = None
        start_index = 0

    if start_index >= len(lines):
        raise InvalidHunkError("Update hunk does not contain any lines", line_number + 1)

    chunk = UpdateFileChunk(
        change_context=change_context,
        old_lines=[],
        new_lines=[],
        is_end_of_file=False,
    )
    parsed_lines = 0

    for line in lines[start_index:]:
        if line == EOF_MARKER:
            if parsed_lines == 0:
                raise InvalidHunkError("Update hunk does not contain any lines", line_number + 1)
            chunk.is_end_of_file = True
            parsed_lines += 1
            break

        first_char = line[:1]
        if line == "":
            chunk.old_lines.append("")
            chunk.new_lines.append("")
        elif first_char == " ":
            chunk.old_lines.append(line[1:])
            chunk.new_lines.append(line[1:])
        elif first_char == "+":
            chunk.new_lines.append(line[1:])
        elif first_char == "-":
            chunk.old_lines.append(line[1:])
        else:
            if parsed_lines == 0:
                raise InvalidHunkError(
                    (
                        "Unexpected line found in update hunk: "
                        f"'{line}'. Every line should start with ' ' (context line), '+' "
                        "(added line), or '-' (removed line)"
                    ),
                    line_number + 1,
                )
            break
        parsed_lines += 1

    return chunk, parsed_lines + start_index


def _parse_one_hunk(lines: list[str], line_number: int) -> tuple[PatchHunk, int]:
    """Parse a single file operation hunk."""
    first_line = lines[0].strip()

    if first_line.startswith(ADD_FILE_MARKER):
        path = first_line[len(ADD_FILE_MARKER) :]
        contents: list[str] = []
        parsed_lines = 1
        for add_line in lines[1:]:
            if add_line.startswith("+"):
                contents.append(add_line[1:])
                parsed_lines += 1
            else:
                break
        content = "".join(f"{line}\n" for line in contents)
        return AddFileHunk(path=path, contents=content), parsed_lines

    if first_line.startswith(DELETE_FILE_MARKER):
        path = first_line[len(DELETE_FILE_MARKER) :]
        return DeleteFileHunk(path=path), 1

    if first_line.startswith(UPDATE_FILE_MARKER):
        path = first_line[len(UPDATE_FILE_MARKER) :]
        remaining_lines = lines[1:]
        parsed_lines = 1

        move_path = None
        if remaining_lines and remaining_lines[0].startswith(MOVE_TO_MARKER):
            move_path = remaining_lines[0][len(MOVE_TO_MARKER) :]
            remaining_lines = remaining_lines[1:]
            parsed_lines += 1

        chunks: list[UpdateFileChunk] = []
        while remaining_lines:
            if remaining_lines[0].strip() == "":
                parsed_lines += 1
                remaining_lines = remaining_lines[1:]
                continue
            if remaining_lines[0].startswith("***"):
                break

            chunk, chunk_lines = _parse_update_file_chunk(
                remaining_lines,
                line_number + parsed_lines,
                allow_missing_context=not chunks,
            )
            chunks.append(chunk)
            parsed_lines += chunk_lines
            remaining_lines = remaining_lines[chunk_lines:]

        if not chunks:
            raise InvalidHunkError(f"Update file hunk for path '{path}' is empty", line_number)

        return UpdateFileHunk(path=path, move_path=move_path, chunks=chunks), parsed_lines

    raise InvalidHunkError(
        (
            f"'{first_line}' is not a valid hunk header. Valid hunk headers: "
            "'*** Add File: {path}', '*** Delete File: {path}', '*** Update File: {path}'"
        ),
        line_number,
    )


def _parse_patch_text(patch: str) -> list[PatchHunk]:
    """Parse the apply_patch body into hunks."""
    patch = _strip_lenient_heredoc(patch)
    lines = patch.strip().splitlines()
    _check_patch_boundaries(lines)

    hunks: list[PatchHunk] = []
    remaining_lines = lines[1:-1]
    line_number = 2

    while remaining_lines:
        hunk, parsed_lines = _parse_one_hunk(remaining_lines, line_number)
        hunks.append(hunk)
        line_number += parsed_lines
        remaining_lines = remaining_lines[parsed_lines:]

    return hunks


def _is_absolute_patch_path(path: str) -> bool:
    """Detect POSIX or Windows absolute paths."""
    return PurePath(path).is_absolute() or PureWindowsPath(path).is_absolute()


def _validate_patch_path(path: str) -> None:
    """Restrict patch paths to relative sandbox-local paths."""
    if not path or not path.strip():
        raise InvalidPatchError("Patch file paths must be non-empty")
    stripped = path.strip()
    if _is_absolute_patch_path(stripped):
        raise InvalidPatchError(f"File references must be relative, never absolute: {path}")

    posix_parts = PurePath(stripped).parts
    windows_parts = PureWindowsPath(stripped).parts
    if any(part == ".." for part in (*posix_parts, *windows_parts)):
        raise InvalidPatchError(f"File references must stay within the sandbox working directory: {path}")


def _resolve_patch_path(path: str, sandbox: BaseSandbox) -> str:
    """Resolve a validated patch path inside the sandbox."""
    _validate_patch_path(path)
    return resolve_path(path, sandbox)


def _decode_file_content(content: str | bytes | bytearray | None, display_path: str) -> TextFileContents:
    """Decode sandbox file content while preserving non-UTF-8 text as latin-1."""
    if isinstance(content, str):
        return TextFileContents(content=content, encoding="utf-8")
    if content is None:
        raise PatchApplicationError(f"Failed to read {display_path}: empty file content")

    raw_bytes = bytes(content)
    for encoding in ("utf-8", "latin-1"):
        try:
            return TextFileContents(content=raw_bytes.decode(encoding), encoding=encoding)
        except UnicodeDecodeError:
            continue

    raise PatchApplicationError(f"Failed to decode {display_path} as text")


def _read_text_file(sandbox: BaseSandbox, resolved_path: str, display_path: str, purpose: str) -> TextFileContents:
    """Read a full text file from the sandbox without sandbox text truncation."""
    read_res = sandbox.read_file(resolved_path, binary=True)
    if read_res.status != SandboxStatus.SUCCESS:
        raise PatchApplicationError(f"Failed to read {purpose} {display_path}: {read_res.error or 'unknown error'}")
    return _decode_file_content(read_res.content, display_path)


def _write_text_file(sandbox: BaseSandbox, resolved_path: str, display_path: str, content: str, encoding: str = "utf-8") -> None:
    """Write a text file through the sandbox API."""
    write_res = sandbox.write_file(
        resolved_path,
        content,
        encoding=encoding,
        binary=False,
        create_directories=True,
    )
    if write_res.status != SandboxStatus.SUCCESS:
        raise PatchApplicationError(f"Failed to write file {display_path}: {write_res.error or 'unknown error'}")


def _delete_file(sandbox: BaseSandbox, resolved_path: str, display_path: str) -> None:
    """Delete a file through the sandbox API, but reject directories."""
    if not sandbox.file_exists(resolved_path):
        raise PatchApplicationError(f"Failed to delete file {display_path}")

    info = sandbox.get_file_info(resolved_path)
    if info.is_directory:
        raise PatchApplicationError(f"Failed to delete file {display_path}")

    delete_res = sandbox.delete_file(resolved_path)
    if delete_res.status != SandboxStatus.SUCCESS:
        raise PatchApplicationError(f"Failed to delete file {display_path}: {delete_res.error or 'unknown error'}")


def _generate_diff(display_path: str, original_content: str, new_content: str) -> str:
    """Generate a unified diff for UI display."""
    diff = difflib.unified_diff(
        original_content.splitlines(keepends=True),
        new_content.splitlines(keepends=True),
        fromfile=f"Original: {display_path}",
        tofile=f"Patched: {display_path}",
        lineterm="",
    )
    return "".join(diff)


def _compute_replacements(
    original_lines: list[str],
    display_path: str,
    chunks: list[UpdateFileChunk],
) -> list[tuple[int, int, list[str]]]:
    """Compute the replacements needed to apply update chunks."""
    replacements: list[tuple[int, int, list[str]]] = []
    line_index = 0

    for chunk in chunks:
        if chunk.change_context is not None:
            context_index = _seek_sequence(original_lines, [chunk.change_context], line_index, False)
            if context_index is None:
                raise PatchApplicationError(f"Failed to find context '{chunk.change_context}' in {display_path}")
            line_index = context_index + 1

        if not chunk.old_lines:
            insertion_index = len(original_lines) - 1 if original_lines and original_lines[-1] == "" else len(original_lines)
            replacements.append((insertion_index, 0, list(chunk.new_lines)))
            continue

        pattern = list(chunk.old_lines)
        new_slice = list(chunk.new_lines)
        found_index = _seek_sequence(original_lines, pattern, line_index, chunk.is_end_of_file)

        if found_index is None and pattern and pattern[-1] == "":
            pattern = pattern[:-1]
            if new_slice and new_slice[-1] == "":
                new_slice = new_slice[:-1]
            found_index = _seek_sequence(original_lines, pattern, line_index, chunk.is_end_of_file)

        if found_index is None:
            raise PatchApplicationError(f"Failed to find expected lines in {display_path}:\n{'\n'.join(chunk.old_lines)}")

        replacements.append((found_index, len(pattern), list(new_slice)))
        line_index = found_index + len(pattern)

    replacements.sort(key=lambda item: item[0])
    return replacements


def _apply_replacements(lines: list[str], replacements: list[tuple[int, int, list[str]]]) -> list[str]:
    """Apply replacements in reverse order to preserve indices."""
    updated_lines = list(lines)
    for start_index, old_length, new_segment in reversed(replacements):
        for _ in range(old_length):
            if start_index < len(updated_lines):
                updated_lines.pop(start_index)
        for offset, new_line in enumerate(new_segment):
            updated_lines.insert(start_index + offset, new_line)
    return updated_lines


def _derive_new_contents(display_path: str, original_content: str, chunks: list[UpdateFileChunk]) -> str:
    """Derive the new file content after applying update chunks."""
    original_lines = original_content.split("\n")
    if original_lines and original_lines[-1] == "":
        original_lines.pop()

    replacements = _compute_replacements(original_lines, display_path, chunks)
    new_lines = _apply_replacements(original_lines, replacements)
    if not new_lines or new_lines[-1] != "":
        new_lines.append("")
    return "\n".join(new_lines)


def _build_summary(added: list[str], modified: list[str], deleted: list[str]) -> str:
    """Format a Codex-compatible success summary."""
    lines = ["Success. Updated the following files:"]
    lines.extend(f"A {path}" for path in added)
    lines.extend(f"M {path}" for path in modified)
    lines.extend(f"D {path}" for path in deleted)
    return "\n".join(lines) + "\n"


def apply_patch(input: str, agent_state: AgentState | None = None) -> dict[str, Any]:
    """
    Apply a Codex-style multi-file patch inside the active NexAU sandbox.

    Args:
        input: Full patch body in apply_patch format
        agent_state: Agent runtime state containing the sandbox

    Returns:
        Dict with content and returnDisplay following NexAU builtin conventions
    """
    try:
        sandbox = get_sandbox(agent_state)
        hunks = _parse_patch_text(input)

        if not hunks:
            return _error_result("No files were modified.", "EMPTY_PATCH")

        added: list[str] = []
        modified: list[str] = []
        deleted: list[str] = []
        changes: list[dict[str, Any]] = []

        for hunk in hunks:
            if isinstance(hunk, AddFileHunk):
                resolved_path = _resolve_patch_path(hunk.path, sandbox)
                original_content = ""
                if sandbox.file_exists(resolved_path) and not sandbox.get_file_info(resolved_path).is_directory:
                    original_content = _read_text_file(sandbox, resolved_path, hunk.path, "file").content

                _write_text_file(sandbox, resolved_path, hunk.path, hunk.contents, encoding="utf-8")
                added.append(hunk.path)
                changes.append(
                    {
                        "action": "add",
                        "path": hunk.path,
                        "originalContent": original_content,
                        "newContent": hunk.contents,
                        "fileDiff": _generate_diff(hunk.path, original_content, hunk.contents),
                    }
                )
                continue

            if isinstance(hunk, DeleteFileHunk):
                resolved_path = _resolve_patch_path(hunk.path, sandbox)
                if not sandbox.file_exists(resolved_path) or sandbox.get_file_info(resolved_path).is_directory:
                    raise PatchApplicationError(f"Failed to delete file {hunk.path}")
                original = _read_text_file(sandbox, resolved_path, hunk.path, "file to delete")
                _delete_file(sandbox, resolved_path, hunk.path)
                deleted.append(hunk.path)
                changes.append(
                    {
                        "action": "delete",
                        "path": hunk.path,
                        "originalContent": original.content,
                        "newContent": "",
                        "fileDiff": _generate_diff(hunk.path, original.content, ""),
                    }
                )
                continue

            resolved_source = _resolve_patch_path(hunk.path, sandbox)
            original = _read_text_file(sandbox, resolved_source, hunk.path, "file to update")
            new_content = _derive_new_contents(hunk.path, original.content, hunk.chunks)

            target_display_path = hunk.move_path or hunk.path
            resolved_target = _resolve_patch_path(target_display_path, sandbox)

            if hunk.move_path and resolved_target != resolved_source:
                _write_text_file(sandbox, resolved_target, target_display_path, new_content, encoding=original.encoding)

                delete_res = sandbox.delete_file(resolved_source)
                if delete_res.status != SandboxStatus.SUCCESS:
                    raise PatchApplicationError(f"Failed to remove original {hunk.path}: {delete_res.error or 'unknown error'}")
            else:
                _write_text_file(sandbox, resolved_source, hunk.path, new_content, encoding=original.encoding)

            modified.append(target_display_path)
            changes.append(
                {
                    "action": "update",
                    "path": target_display_path,
                    "moveFrom": hunk.path if hunk.move_path else None,
                    "originalContent": original.content,
                    "newContent": new_content,
                    "fileDiff": _generate_diff(target_display_path, original.content, new_content),
                }
            )

        summary = _build_summary(added, modified, deleted)
        return {
            "content": summary,
            "returnDisplay": {
                "summary": summary,
                "changes": changes,
            },
        }

    except InvalidHunkError as exc:
        return _error_result(
            f"Invalid patch hunk on line {exc.line_number}: {exc.message}",
            "INVALID_PATCH_HUNK",
        )
    except InvalidPatchError as exc:
        return _error_result(f"Invalid patch: {exc}", "INVALID_PATCH")
    except PatchApplicationError as exc:
        return _error_result(str(exc), "APPLY_PATCH_FAILED")
    except Exception as exc:
        return _error_result(f"Failed to apply patch: {exc}", "APPLY_PATCH_FAILED")
