# Copyright 2025 Google LLC (adapted from gemini-cli)
# SPDX-License-Identifier: Apache-2.0
"""
complete_task tool - Signals task completion with final results.

Based on gemini-cli's complete_task implementation in local-executor.ts.
This is a mandatory termination protocol tool - the agent MUST call this
to properly complete a task.
"""

import json
from typing import Any


def complete_task(result: str | None = None, **kwargs: Any) -> str:
    """
    Signals that the task is complete and submits the final result.

    This is the ONLY way to properly finish a task. The agent MUST call
    this tool when the task is done. Failure to call this tool will
    result in a protocol violation (ERROR_NO_COMPLETE_TASK_CALL).

    Args:
        result: The final results or findings to return. This should be
                comprehensive and follow any formatting requested in the
                task instructions. Required unless a custom output schema
                is configured.
        **kwargs: Additional output fields if a custom output schema is
                  configured for the agent.

    Returns:
        JSON string confirming task completion or error details.

    Protocol Notes:
        - This tool should be called exactly once per task
        - Calling other tools after complete_task is not allowed
        - If the agent stops without calling this, it triggers recovery
        - During grace period, ONLY this tool can be called
    """
    try:
        # Check if result is provided (required by default)
        # In gemini-cli, if outputConfig is set, a specific field is required
        # Otherwise, 'result' is required

        # Handle both positional 'result' and any custom output fields
        output_data: dict[str, Any] = {}

        if result is not None:
            output_data["result"] = result

        # Include any additional kwargs (for custom output schemas)
        output_data.update(kwargs)

        # Validate that we have some output
        if not output_data:
            return json.dumps(
                {
                    "success": False,
                    "error": 'Missing required "result" argument. You must provide your findings when calling complete_task.',
                    "type": "MISSING_RESULT",
                    "task_completed": False,
                }
            )

        # Check if 'result' specifically is required and missing
        if "result" not in output_data and not kwargs:
            return json.dumps(
                {
                    "success": False,
                    "error": 'Missing required "result" argument. You must provide your findings when calling complete_task.',
                    "type": "MISSING_RESULT",
                    "task_completed": False,
                }
            )

        # Validate result is not empty if provided
        result_value: Any = output_data.get("result", "")
        if isinstance(result_value, str) and not result_value.strip():
            return json.dumps(
                {
                    "success": False,
                    "error": "The 'result' argument cannot be empty. Please provide your findings.",
                    "type": "EMPTY_RESULT",
                    "task_completed": False,
                }
            )

        # Task completed successfully
        return json.dumps(
            {
                "success": True,
                "message": "Result submitted and task completed.",
                "status": "TASK_COMPLETED",
                "task_completed": True,
                "output": output_data,
            },
            ensure_ascii=False,
        )

    except Exception as e:
        return json.dumps(
            {
                "success": False,
                "error": f"Error completing task: {str(e)}",
                "type": "COMPLETE_TASK_ERROR",
                "task_completed": False,
            }
        )


# Tool metadata for registration
TOOL_METADATA = {
    "name": "complete_task",
    "description": (
        "Call this tool to submit your final answer and complete the task. "
        "This is the ONLY way to finish. You MUST call this tool when your "
        "task is done. Provide comprehensive results in the 'result' argument."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "result": {
                "type": "string",
                "description": (
                    "Your final results or findings to return. "
                    "Ensure this is comprehensive and follows any formatting "
                    "requested in your instructions."
                ),
            },
        },
        "required": ["result"],
    },
}
