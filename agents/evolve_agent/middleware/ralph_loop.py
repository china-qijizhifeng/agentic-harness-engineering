"""RALPH Loop Middleware — intercept complete_task and enforce verification.

Hooks into the after_model phase.  When the agent emits a ``complete_task``
tool call, this middleware checks the recent conversation history for a passing
verification command (exit_code == 0).  If none is found the call is blocked,
a FRAMEWORK message is injected asking the agent to write and run tests, and
the execution loop continues.

After *max_blocks* consecutive interceptions the middleware force-allows the
call to avoid infinite loops.

Configuration example (YAML)::

    middlewares:
      - import: middleware.ralph_loop:RalphLoopMiddleware
        params:
          max_blocks: 3
          lookback_iterations: 8
          require_exit_code_zero: true
          skip_for_non_code_tasks: true
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace as dc_replace
from typing import Any

from nexau.archs.main_sub.execution.hooks import (
    AfterModelHookInput,
    HookResult,
    Middleware,
)
from nexau.archs.main_sub.execution.parse_structures import ParsedResponse
from nexau.core.messages import Message, Role, TextBlock, ToolResultBlock, ToolUseBlock

logger = logging.getLogger(__name__)

_BLOCK_COUNT_KEY = "__ralph_block_count__"
_COMPLETE_TASK = "complete_task"
_SHELL_TOOL = "run_shell_command"

_VERIFICATION_INDICATORS = (
    "pytest", "python -m pytest", "python3 -m pytest",
    "unittest", "python -m unittest", "python3 -m unittest",
    "jest ", "mocha ", "vitest",
    "make check", "make test",
    "npm test", "npm run test", "yarn test",
    "cargo test", "go test ./", "go test .",
    "rspec", "phpunit",
    "python3 -c", "python -c",
    "python3 test_", "python test_",
    "python3 -m test", "python -m test",
    "bash test_", "./test_", "sh test_",
    "diff ", "cmp ",
    "curl ", "wget ",
    "node -e", "ruby -e",
    "gradle test", "mvn test",
)

_CODE_MODIFICATION_TOOLS = frozenset({
    "write_file", "replace", "apply_patch",
})


class RalphLoopMiddleware(Middleware):
    """Intercept ``complete_task`` and require passing verification first.

    Args:
        max_blocks: Maximum consecutive interceptions before force-allowing.
        lookback_iterations: How many recent assistant/tool rounds to scan
            for a passing verification command.
        require_exit_code_zero: If True, the verification command must have
            exit_code == 0 to count as passing.
        skip_for_non_code_tasks: If True, tasks with no code-modification
            tool calls in history are allowed through without verification.
    """

    def __init__(
        self,
        *,
        max_blocks: int = 3,
        lookback_iterations: int = 8,
        require_exit_code_zero: bool = True,
        skip_for_non_code_tasks: bool = True,
    ) -> None:
        if max_blocks < 1:
            raise ValueError("max_blocks must be >= 1")
        self.max_blocks = max_blocks
        self.lookback_iterations = lookback_iterations
        self.require_exit_code_zero = require_exit_code_zero
        self.skip_for_non_code_tasks = skip_for_non_code_tasks

    def after_model(self, hook_input: AfterModelHookInput) -> HookResult:
        """Check for complete_task calls and enforce verification gate."""

        parsed = hook_input.parsed_response
        if not parsed:
            return HookResult.no_changes()

        complete_calls = [
            tc for tc in parsed.tool_calls if tc.tool_name == _COMPLETE_TASK
        ]
        if not complete_calls:
            return HookResult.no_changes()

        block_count: int = hook_input.agent_state.get_context_value(
            _BLOCK_COUNT_KEY, 0,
        )

        if block_count >= self.max_blocks:
            logger.info(
                "[RalphLoopMiddleware] max_blocks (%d) reached — force-allowing complete_task",
                self.max_blocks,
            )
            return HookResult.no_changes()

        if self.skip_for_non_code_tasks and not self._has_code_modifications(
            hook_input.messages,
        ):
            logger.info(
                "[RalphLoopMiddleware] No code modifications detected — skipping verification gate",
            )
            return HookResult.no_changes()

        has_verification, reason = self._has_recent_verification(
            hook_input.messages, self.lookback_iterations,
        )
        if has_verification:
            logger.info(
                "[RalphLoopMiddleware] Verification found (%s) — allowing complete_task",
                reason,
            )
            return HookResult.no_changes()

        block_count += 1
        hook_input.agent_state.set_context_value(_BLOCK_COUNT_KEY, block_count)

        logger.info(
            "[RalphLoopMiddleware] Blocking complete_task (block %d/%d): %s",
            block_count,
            self.max_blocks,
            reason,
        )

        new_parsed = self._remove_complete_task(parsed)
        messages = self._patch_messages(
            hook_input.messages,
            block_count,
            self.max_blocks,
        )

        return HookResult.with_modifications(
            parsed_response=new_parsed,
            messages=messages,
            force_continue=True,
        )

    # ------------------------------------------------------------------
    # Verification detection
    # ------------------------------------------------------------------

    def _has_recent_verification(
        self,
        messages: list[Message],
        lookback: int,
    ) -> tuple[bool, str]:
        """Scan recent messages for a shell verification command with passing exit code.

        Correlates ToolUseBlock (contains the command string) in ASSISTANT messages
        with ToolResultBlock (contains exit_code) in TOOL messages via tool_use_id.

        Returns ``(found, reason_string)``.
        """

        start_idx = self._find_lookback_start(messages, lookback)

        shell_commands: dict[str, str] = {}

        for msg in messages[start_idx:]:
            if msg.role == Role.ASSISTANT:
                for block in msg.content:
                    if isinstance(block, ToolUseBlock) and block.name == _SHELL_TOOL:
                        command = block.input.get("command", "")
                        shell_commands[block.id] = command

            elif msg.role == Role.TOOL:
                for block in msg.content:
                    if not isinstance(block, ToolResultBlock):
                        continue

                    command = shell_commands.get(block.tool_use_id, "")
                    if not command or not self._is_verification_command(command):
                        continue

                    exit_code = self._extract_exit_code(block.content)
                    if exit_code is None:
                        continue

                    if self.require_exit_code_zero:
                        if exit_code == 0:
                            return True, f"Passing verification: {command!r}"
                    else:
                        return True, f"Verification command found: {command!r}"

        return False, "No passing verification found in recent history"

    @staticmethod
    def _find_lookback_start(messages: list[Message], lookback: int) -> int:
        """Find the message index where the lookback window begins."""
        assistant_count = 0
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].role == Role.ASSISTANT:
                assistant_count += 1
                if assistant_count >= lookback:
                    return i
        return 0

    @staticmethod
    def _is_verification_command(command: str) -> bool:
        cmd_lower = command.lower()
        return any(ind in cmd_lower for ind in _VERIFICATION_INDICATORS)

    @staticmethod
    def _extract_exit_code(content: str | list[Any]) -> int | None:
        """Extract exit_code from a ToolResultBlock content field."""
        raw = content if isinstance(content, str) else str(content)
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                ec = obj.get("exit_code")
                if ec is not None:
                    return int(ec)
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

        if "exit_code" in raw:
            try:
                idx = raw.index('"exit_code"')
                snippet = raw[idx:]
                colon = snippet.index(":")
                rest = snippet[colon + 1 :].strip().split()[0].strip(",}")
                return int(rest)
            except (ValueError, IndexError):
                pass
        return None

    # ------------------------------------------------------------------
    # Code modification detection
    # ------------------------------------------------------------------

    @staticmethod
    def _has_code_modifications(messages: list[Message]) -> bool:
        """Check if the conversation contains any code-modification tool calls."""
        for msg in messages:
            if msg.role != Role.ASSISTANT:
                continue
            for block in msg.content:
                if isinstance(block, ToolUseBlock) and block.name in _CODE_MODIFICATION_TOOLS:
                    return True
        return False

    # ------------------------------------------------------------------
    # Response patching helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _remove_complete_task(parsed: ParsedResponse) -> ParsedResponse:
        """Return a new ParsedResponse with complete_task calls removed."""
        new_tool_calls = [
            tc for tc in parsed.tool_calls if tc.tool_name != _COMPLETE_TASK
        ]

        new_model_response = parsed.model_response
        if new_model_response and new_model_response.tool_calls:
            filtered_model_tcs = [
                tc for tc in new_model_response.tool_calls
                if tc.name != _COMPLETE_TASK
            ]
            new_model_response = dc_replace(
                new_model_response, tool_calls=filtered_model_tcs,
            )

        return ParsedResponse(
            original_response=parsed.original_response,
            tool_calls=new_tool_calls,
            sub_agent_calls=parsed.sub_agent_calls,
            batch_agent_calls=parsed.batch_agent_calls,
            is_parallel_tools=parsed.is_parallel_tools,
            is_parallel_sub_agents=parsed.is_parallel_sub_agents,
            model_response=new_model_response,
        )

    @staticmethod
    def _patch_messages(
        messages: list[Message],
        block_count: int,
        max_blocks: int,
    ) -> list[Message]:
        """Remove complete_task ToolUseBlock from assistant msg and add FRAMEWORK hint.

        Also strips the corresponding ``function_call`` items from the
        ``response_items`` metadata so that the Responses API input stays
        consistent (every ``function_call`` must have a matching
        ``function_call_output``; the intercepted call will never be executed).
        """
        patched = list(messages)

        for i in range(len(patched) - 1, -1, -1):
            if patched[i].role == Role.ASSISTANT:
                removed_call_ids: set[str] = {
                    b.id for b in patched[i].content
                    if isinstance(b, ToolUseBlock) and b.name == _COMPLETE_TASK
                }
                new_blocks = [
                    b for b in patched[i].content
                    if not (isinstance(b, ToolUseBlock) and b.name == _COMPLETE_TASK)
                ]
                updates: dict[str, Any] = {"content": new_blocks}

                if removed_call_ids and "response_items" in patched[i].metadata:
                    new_metadata = dict(patched[i].metadata)
                    new_metadata["response_items"] = [
                        item for item in new_metadata["response_items"]
                        if not (
                            isinstance(item, dict)
                            and item.get("type") == "function_call"
                            and item.get("call_id") in removed_call_ids
                        )
                    ]
                    updates["metadata"] = new_metadata

                patched[i] = patched[i].model_copy(update=updates)
                break

        instruction = _build_verification_instruction(block_count, max_blocks)
        patched.append(
            Message(role=Role.FRAMEWORK, content=[TextBlock(text=instruction)]),
        )
        return patched


def _build_verification_instruction(block_n: int, max_blocks: int) -> str:
    return (
        f"RALPH Verification Gate (block {block_n}/{max_blocks}):\n"
        f"\n"
        f"Your complete_task call has been intercepted because no passing\n"
        f"verification was detected in your recent actions.\n"
        f"\n"
        f"You MUST verify your work before completing. Follow these steps:\n"
        f"\n"
        f"1. Write a verification test that checks your changes work correctly:\n"
        f"   - If the project has an existing test framework, write tests using it\n"
        f"   - Otherwise, write a small script that exercises the key functionality\n"
        f"   - Cover the main requirements from the original task\n"
        f"\n"
        f"2. Run the test and check the output:\n"
        f"   - All tests must pass (exit code 0)\n"
        f"   - If tests fail, fix your code and re-run\n"
        f"\n"
        f"3. Call complete_task again after tests pass.\n"
        f"\n"
        f"If the task does not involve code changes (e.g., answering a question,\n"
        f"reading files), you can call complete_task directly with your findings."
    )
