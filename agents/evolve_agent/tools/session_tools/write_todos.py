# Copyright 2025 Google LLC
# SPDX-License-Identifier: Apache-2.0
"""
write_todos tool - Manages a list of subtasks for complex queries.

Based on gemini-cli's write-todos.ts implementation.
Helps track progress and organize complex multi-step tasks.
"""

from typing import Any, cast

# Valid todo statuses
TODO_STATUSES = ["pending", "in_progress", "completed", "cancelled"]


def write_todos(
    todos: Any,
) -> dict[str, Any]:
    """
    Lists out subtasks required to complete a user request.

    This tool helps track progress, organize complex queries, and ensure
    no steps are missed. The user can see current progress being made.

    Task state definitions:
    - pending: Work has not begun on a given subtask.
    - in_progress: Marked just prior to beginning work. Only one subtask
      should be in_progress at a time.
    - completed: Subtask was successfully completed with no errors.
    - cancelled: Subtask is no longer needed due to dynamic nature of task.

    Args:
        todos: The complete list of todo items. Each item should have:
               - description: The description of the task
               - status: The current status (pending/in_progress/completed/cancelled)

    Returns:
        Dict with content and returnDisplay matching gemini-cli format
    """
    try:
        # Validate todos parameter (runtime check for JSON/LLM input)
        # Empty list [] is valid (clears todo list)
        if not isinstance(todos, list):
            return {
                "content": "`todos` parameter must be an array",
                "returnDisplay": "Error: Invalid todos parameter.",
                "error": {
                    "message": "`todos` parameter must be an array",
                    "type": "INVALID_PARAMETER",
                },
            }

        # Validate each todo item and build typed list
        valid_todos: list[dict[str, Any]] = []
        todos_list = cast(list[Any], todos)
        for _idx, item in enumerate(todos_list):
            if not isinstance(item, dict):
                return {
                    "content": "Each todo item must be an object",
                    "returnDisplay": "Error: Invalid todo item.",
                    "error": {
                        "message": "Each todo item must be an object",
                        "type": "INVALID_PARAMETER",
                    },
                }
            todo_dict = cast(dict[str, Any], item)
            description = str(todo_dict.get("description") or "")
            if not description.strip():
                return {
                    "content": "Each todo must have a non-empty description string",
                    "returnDisplay": "Error: Missing or empty description.",
                    "error": {
                        "message": "Each todo must have a non-empty description string",
                        "type": "INVALID_PARAMETER",
                    },
                }
            valid_todos.append(todo_dict)
            status = str(todo_dict.get("status") or "")
            if status not in TODO_STATUSES:
                return {
                    "content": f"Each todo must have a valid status ({', '.join(TODO_STATUSES)})",
                    "returnDisplay": "Error: Invalid status.",
                    "error": {
                        "message": f"Each todo must have a valid status ({', '.join(TODO_STATUSES)})",
                        "type": "INVALID_PARAMETER",
                    },
                }

        # Check that only one task is in_progress
        in_progress_count = sum(1 for t in valid_todos if t.get("status") == "in_progress")

        if in_progress_count > 1:
            return {
                "content": 'Invalid parameters: Only one task can be "in_progress" at a time.',
                "returnDisplay": "Error: Multiple in_progress tasks.",
                "error": {
                    "message": 'Only one task can be "in_progress" at a time.',
                    "type": "INVALID_PARAMETER",
                },
            }

        # Build the todo list string
        if valid_todos:
            todo_list_string = "\n".join(f"{i + 1}. [{t['status']}] {t['description']}" for i, t in enumerate(valid_todos))
            llm_content = f"Successfully updated the todo list. The current list is now:\n{todo_list_string}"
        else:
            llm_content = "Successfully cleared the todo list."

        return {
            "content": llm_content,
            "returnDisplay": {"todos": valid_todos},
        }

    except Exception as e:
        error_msg = f"Error updating todos: {str(e)}"
        return {
            "content": error_msg,
            "returnDisplay": error_msg,
            "error": {
                "message": error_msg,
                "type": "WRITE_TODOS_ERROR",
            },
        }
