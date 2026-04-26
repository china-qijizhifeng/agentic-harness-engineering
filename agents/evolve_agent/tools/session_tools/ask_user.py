# Copyright 2025 Google LLC
# SPDX-License-Identifier: Apache-2.0
"""
ask_user tool - Structured output tool for asking the user questions.

The tool validates questions and returns them as structured output.
The frontend/app renders the questions UI and the user's answer
comes back as a regular user message in the conversation.
No middleware required.
"""

from typing import Any

# Question types
QUESTION_TYPE_CHOICE = "choice"
QUESTION_TYPE_TEXT = "text"
QUESTION_TYPE_YESNO = "yesno"


def _validate_questions(
    questions: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Validate questions list. Returns error dict or None if valid."""
    if not questions:
        return {
            "content": "At least one question is required.",
            "returnDisplay": "Error: No questions provided.",
            "error": {
                "message": "At least one question is required.",
                "type": "INVALID_PARAMETER",
            },
        }

    # if len(questions) > 4:
    #     return {
    #         "content": "Maximum 4 questions allowed.",
    #         "returnDisplay": "Error: Too many questions.",
    #         "error": {
    #             "message": "Maximum 4 questions allowed.",
    #             "type": "INVALID_PARAMETER",
    #         },
    #     }

    for i, q in enumerate(questions):
        question_type = q.get("type", QUESTION_TYPE_CHOICE)

        if not q.get("question"):
            return {
                "content": f"Question {i + 1}: 'question' is required.",
                "returnDisplay": "Error: Missing question text.",
                "error": {
                    "message": f"Question {i + 1}: 'question' is required.",
                    "type": "INVALID_PARAMETER",
                },
            }

        if not q.get("header"):
            return {
                "content": f"Question {i + 1}: 'header' is required.",
                "returnDisplay": "Error: Missing header.",
                "error": {
                    "message": f"Question {i + 1}: 'header' is required.",
                    "type": "INVALID_PARAMETER",
                },
            }

        if len(q.get("header", "")) > 32:
            return {
                "content": f"Question {i + 1}: 'header' must be at most 32 characters.",
                "returnDisplay": "Error: Header too long.",
                "error": {
                    "message": f"Question {i + 1}: 'header' must be at most 32 characters.",
                    "type": "INVALID_PARAMETER",
                },
            }

        if question_type == QUESTION_TYPE_CHOICE:
            err = _validate_choice_options(i, q)
            if err is not None:
                return err

    return None


def _validate_choice_options(index: int, q: dict[str, Any]) -> dict[str, Any] | None:
    """Validate choice-type question options. Returns error dict or None."""
    options = q.get("options", [])
    if not options or len(options) < 2:
        return {
            "content": f"Question {index + 1}: type='choice' requires 'options' array with 2-4 items.",
            "returnDisplay": "Error: Insufficient options.",
            "error": {
                "message": f"Question {index + 1}: type='choice' requires 'options' array with 2-4 items.",
                "type": "INVALID_PARAMETER",
            },
        }
    if len(options) > 4:
        return {
            "content": f"Question {index + 1}: 'options' array must have at most 4 items.",
            "returnDisplay": "Error: Too many options.",
            "error": {
                "message": f"Question {index + 1}: 'options' array must have at most 4 items.",
                "type": "INVALID_PARAMETER",
            },
        }

    for j, opt in enumerate(options):
        if not opt.get("label") or not isinstance(opt.get("label"), str):
            return {
                "content": f"Question {index + 1}, option {j + 1}: 'label' is required and must be a non-empty string.",
                "returnDisplay": "Error: Invalid option label.",
                "error": {
                    "message": f"Question {index + 1}, option {j + 1}: 'label' is required.",
                    "type": "INVALID_PARAMETER",
                },
            }
        if opt.get("description") is None or not isinstance(opt.get("description"), str):
            return {
                "content": f"Question {index + 1}, option {j + 1}: 'description' is required and must be a string.",
                "returnDisplay": "Error: Invalid option description.",
                "error": {
                    "message": f"Question {index + 1}, option {j + 1}: 'description' is required.",
                    "type": "INVALID_PARAMETER",
                },
            }

    return None


def ask_user(
    questions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Ask the user one or more questions via structured output.

    Validates the questions and returns them as structured output for the
    frontend/app to render. The user's answer comes back as a regular
    user message in the conversation — no middleware required.

    Args:
        questions: List of question objects, each containing:
            - question: The complete question to ask
            - header: Short label (max 32 chars) displayed as chip/tag
            - type: Question type (choice/text/yesno), defaults to choice
            - options: For choice type, list of {label, description} objects
            - multiSelect: For choice type, allow multiple selections
            - placeholder: For text type, hint text

    Returns:
        Dict with structured questions for frontend rendering, or error.
    """
    # 1. 验证问题格式
    error = _validate_questions(questions)
    if error is not None:
        return error

    return {"content": "Asking user questions, waiting for user answers..."}
