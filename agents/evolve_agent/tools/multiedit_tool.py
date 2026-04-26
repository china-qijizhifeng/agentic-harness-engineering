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

"""MultiEdit tool implementation for making multiple edits to a single file."""

import logging
import time
from pathlib import Path
from typing import Any, NotRequired, TypedDict

from nexau.archs.main_sub.agent_state import AgentState
from nexau.archs.sandbox import BaseSandbox, SandboxStatus

logger = logging.getLogger(__name__)


class EditOperation:
    """Represents a single edit operation."""

    def __init__(self, old_string: str, new_string: str, replace_all: bool = False):
        self.old_string = old_string
        self.new_string = new_string
        self.replace_all = replace_all

    def __str__(self):
        return f"EditOperation(old='{self.old_string[:50]}...', new='{self.new_string[:50]}...', replace_all={self.replace_all})"


class EditPayload(TypedDict, total=False):
    old_string: str
    new_string: str
    replace_all: NotRequired[bool]


class FileEditPayload(TypedDict):
    file_path: str
    edits: list[EditPayload]


class AppliedEditDetail(TypedDict):
    edit_index: int
    replacements_made: int
    old_string_length: int
    new_string_length: int


def multiedit_tool(
    file_path: str,
    edits: list[EditPayload],
    agent_state: AgentState,
) -> dict[str, Any]:
    """
    Make multiple edits to a single file in one operation.

    This tool performs multiple find-and-replace operations efficiently on a single file.
    All edits are applied in sequence, and the operation is atomic - either all succeed
    or none are applied.

    Args:
        file_path: The absolute path to the file to modify (must be absolute, not relative)
        edits: Array of edit operations, each containing:
            - old_string: The text to replace (must match exactly, including whitespace)
            - new_string: The text to replace it with
            - replace_all: Replace all occurrences (optional, defaults to False)

    Returns:
        Dict containing the result of the operation
    """
    start_time = time.time()

    # Get sandbox instance
    sandbox: BaseSandbox | None = agent_state.get_sandbox()
    assert sandbox is not None, "File operation tool invoked, but sandbox is not initialized."

    # Validate file path is absolute
    if not Path(file_path).is_absolute():
        return {
            "status": "error",
            "error": f"File path must be absolute, got relative path: {file_path}",
            "file_path": file_path,
            "duration_ms": int((time.time() - start_time) * 1000),
        }

    # Validate edits list
    if not edits:
        return {
            "status": "error",
            "error": "No edits provided",
            "file_path": file_path,
            "duration_ms": int((time.time() - start_time) * 1000),
        }

    # Validate and parse edit operations
    edit_operations: list[EditOperation] = []
    for i, edit in enumerate(edits):
        if "old_string" not in edit:
            return {
                "status": "error",
                "error": f"Edit {i} missing required 'old_string' field",
                "file_path": file_path,
                "duration_ms": int((time.time() - start_time) * 1000),
            }

        if "new_string" not in edit:
            return {
                "status": "error",
                "error": f"Edit {i} missing required 'new_string' field",
                "file_path": file_path,
                "duration_ms": int((time.time() - start_time) * 1000),
            }

        old_string: str = edit["old_string"]
        new_string: str = edit["new_string"]
        replace_all: bool = bool(edit.get("replace_all", False))

        # Validate strings are different
        if old_string == new_string:
            return {
                "status": "error",
                "error": f"Edit {i}: old_string and new_string cannot be the same",
                "file_path": file_path,
                "duration_ms": int((time.time() - start_time) * 1000),
            }

        edit_operations.append(
            EditOperation(
                old_string,
                new_string,
                replace_all,
            ),
        )

    # Handle file creation case (first edit has empty old_string)
    is_new_file = False
    initial_content: str = ""
    if edit_operations[0].old_string == "":
        if sandbox.file_exists(file_path):
            return {
                "status": "error",
                "error": f"Cannot create new file - file already exists: {file_path}",
                "file_path": file_path,
                "duration_ms": int((time.time() - start_time) * 1000),
            }
        is_new_file = True
        initial_content = edit_operations[0].new_string
        edit_operations = edit_operations[1:]  # Remove the creation edit
    else:
        # Check if file exists for modification
        if not sandbox.file_exists(file_path):
            return {
                "status": "error",
                "error": f"File does not exist: {file_path}",
                "file_path": file_path,
                "duration_ms": int((time.time() - start_time) * 1000),
            }

        # Check file permissions
        try:
            file_info = sandbox.get_file_info(file_path)
            if not file_info.readable:
                return {
                    "status": "error",
                    "error": f"No read permission for file: {file_path}",
                    "file_path": file_path,
                    "duration_ms": int((time.time() - start_time) * 1000),
                }
            if not file_info.writable:
                return {
                    "status": "error",
                    "error": f"No write permission for file: {file_path}",
                    "file_path": file_path,
                    "duration_ms": int((time.time() - start_time) * 1000),
                }
        except Exception as e:
            return {
                "status": "error",
                "error": f"Failed to check file permissions: {str(e)}",
                "file_path": file_path,
                "duration_ms": int((time.time() - start_time) * 1000),
            }

    try:
        # Read original content or start with initial content for new files
        if is_new_file:
            original_content = ""
            current_content: str = initial_content
        else:
            read_result = sandbox.read_file(file_path, encoding="utf-8")
            if read_result.status != SandboxStatus.SUCCESS:
                return {
                    "status": "error",
                    "error": f"Failed to read file: {read_result.error or 'Unknown error'}",
                    "file_path": file_path,
                    "duration_ms": int((time.time() - start_time) * 1000),
                }
            if isinstance(read_result.content, str):
                original_content = read_result.content
            elif isinstance(read_result.content, bytes):
                original_content = read_result.content.decode("utf-8")
            else:
                original_content = ""
            current_content = original_content

        # Apply edits in sequence
        applied_edits: list[AppliedEditDetail] = []
        for i, edit_op in enumerate(edit_operations):
            if edit_op.old_string not in current_content:
                return {
                    "status": "error",
                    "error": f"Edit {i}: old_string not found in file content",
                    "file_path": file_path,
                    "edit_index": i,
                    "old_string": (edit_op.old_string[:100] + "..." if len(edit_op.old_string) > 100 else edit_op.old_string),
                    "applied_edits": len(applied_edits),
                    "duration_ms": int((time.time() - start_time) * 1000),
                }

            # Perform the replacement
            if edit_op.replace_all:
                new_content: str = current_content.replace(
                    edit_op.old_string,
                    edit_op.new_string,
                )
                replacements_made: int = current_content.count(edit_op.old_string)
            else:
                new_content = current_content.replace(
                    edit_op.old_string,
                    edit_op.new_string,
                    1,
                )
                replacements_made = 1 if edit_op.old_string in current_content else 0

            current_content = new_content
            applied_edits.append(
                {
                    "edit_index": i,
                    "replacements_made": replacements_made,
                    "old_string_length": len(edit_op.old_string),
                    "new_string_length": len(edit_op.new_string),
                },
            )

        # Write the final content to file through sandbox
        write_result = sandbox.write_file(
            file_path,
            current_content,
            encoding="utf-8",
            binary=False,
            create_directories=True,
        )
        if write_result.status != SandboxStatus.SUCCESS:
            return {
                "status": "error",
                "error": f"Failed to write file: {write_result.error or 'Unknown error'}",
                "file_path": file_path,
                "duration_ms": int((time.time() - start_time) * 1000),
            }

        duration_ms = int((time.time() - start_time) * 1000)

        # Calculate statistics
        total_replacements = sum(edit["replacements_made"] for edit in applied_edits)
        content_size_change = len(current_content) - len(original_content)

        result: dict[str, Any] = {
            "status": "success",
            "file_path": file_path,
            "is_new_file": is_new_file,
            "total_edits": len(edit_operations),
            "applied_edits": len(applied_edits),
            "total_replacements": total_replacements,
            "original_size": len(original_content),
            "final_size": len(current_content),
            "size_change": content_size_change,
            "duration_ms": duration_ms,
            "edit_details": applied_edits,
        }

        logger.info(
            f"MultiEdit completed: {len(applied_edits)} edits, {total_replacements} replacements in {duration_ms}ms",
        )

        return result

    except UnicodeDecodeError as e:
        return {
            "status": "error",
            "error": f"File encoding error - cannot read file as UTF-8: {str(e)}",
            "file_path": file_path,
            "duration_ms": int((time.time() - start_time) * 1000),
        }

    except PermissionError as e:
        return {
            "status": "error",
            "error": f"Permission denied: {str(e)}",
            "file_path": file_path,
            "duration_ms": int((time.time() - start_time) * 1000),
        }

    except OSError as e:
        return {
            "status": "error",
            "error": f"OS error: {str(e)}",
            "file_path": file_path,
            "duration_ms": int((time.time() - start_time) * 1000),
        }

    except Exception as e:
        logger.error(f"Unexpected error during multiedit: {e}")
        return {
            "status": "error",
            "error": f"Unexpected error: {str(e)}",
            "error_type": type(e).__name__,
            "file_path": file_path,
            "duration_ms": int((time.time() - start_time) * 1000),
        }
