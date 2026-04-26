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

"""Middleware that injects iteration/token reminders before each model call."""

from __future__ import annotations

import logging

from nexau.core.messages import Message, Role, TextBlock

from nexau.archs.main_sub.utils.token_counter import TokenCounter
from nexau.archs.main_sub.execution.hooks import BeforeModelHookInput, HookResult, Middleware

logger = logging.getLogger(__name__)


class RoundAndTokenReminderMiddleware(Middleware):
    """Injects iteration and optional token budget hints before model calls."""

    def __init__(
        self,
        *,
        max_context_tokens: int,
        desired_max_tokens: int = 16384,
    ) -> None:
        """Configure the reminder middleware.

        Args:
            max_context_tokens: Context window size; required when token hint enabled.
            desired_max_tokens: Preferred response size for token hint messaging.
        """
        self.max_context_tokens = max_context_tokens
        self.desired_max_tokens = desired_max_tokens
        # TODO: reuse token counter from AgentConfig
        self.token_counter = TokenCounter()

    def before_model(self, hook_input: BeforeModelHookInput) -> HookResult:  # type: ignore[override]
        """Append iteration (and optional token) hints prior to model invocation."""

        # If no assistant messages yet, skip adding hints to avoid front-loading noise.
        has_assistant = any(msg.role == Role.ASSISTANT for msg in hook_input.messages)
        if not has_assistant:
            return HookResult.no_changes()

        iteration_hint = self._build_iteration_hint(
            hook_input.current_iteration,
            hook_input.max_iterations,
            hook_input.max_iterations - hook_input.current_iteration,
        )

        current_tokens = self._count_tokens(hook_input)
        remaining_tokens = max((self.max_context_tokens or 0) - current_tokens, 0)
        token_hint = self._build_token_limit_hint(
            current_prompt_tokens=current_tokens,
            max_tokens=self.max_context_tokens or 0,
            remaining_tokens=remaining_tokens,
            desired_max_tokens=self.desired_max_tokens,
        )
        hint_content = f"{iteration_hint}\n\n{token_hint}"

        updated_messages = list(hook_input.messages)
        if updated_messages[-1].role not in (Role.USER, Role.FRAMEWORK):
            updated_messages.append(Message(role=Role.FRAMEWORK, content=[TextBlock(text=hint_content)]))
        else:
            last = updated_messages[-1]
            blocks = list(last.content)
            appended = False
            for idx in range(len(blocks) - 1, -1, -1):
                block = blocks[idx]
                if isinstance(block, TextBlock):
                    blocks[idx] = TextBlock(text=f"{block.text}\n\n{hint_content}")
                    appended = True
                    break
            if not appended:
                blocks.append(TextBlock(text=hint_content))
            updated_messages[-1] = last.model_copy(update={"content": blocks})

        logger.info("[RoundAndTokenReminderMiddleware] Added iteration/token hint message")
        return HookResult.with_modifications(messages=updated_messages)

    def _count_tokens(self, hook_input: BeforeModelHookInput) -> int:
        """Count tokens for the current prompt using configured token counter."""

        try:
            return self.token_counter.count_tokens(hook_input.messages)
        except Exception as exc:  # pragma: no cover - defensive logging
            logger.warning("[RoundAndTokenReminderMiddleware] Token counting failed: %s", exc)
            return 0

    def _remaining_tokens(self, hook_input: BeforeModelHookInput) -> int:
        """Compute remaining tokens based on the configured context window."""

        return max(self.max_context_tokens - self._count_tokens(hook_input), 0)

    @staticmethod
    def _build_iteration_hint(
        current_iteration: int,
        max_iterations: int,
        remaining_iterations: int,
    ) -> str:
        """Match the iteration hint semantics used in executor loop."""

        if remaining_iterations <= 1:
            return (
                f"⚠️ WARNING: This is iteration {current_iteration}/{max_iterations}. "
                f"You have only {remaining_iterations} iteration(s) remaining. "
                f"Please provide a conclusive response and avoid making additional tool calls or sub-agent calls "
                f"unless absolutely critical. Focus on summarizing your findings and providing final recommendations."
            )
        if remaining_iterations <= 3:
            return (
                f"🔄 Iteration {current_iteration}/{max_iterations} - {remaining_iterations} iterations remaining. "
                f"Please be mindful of the remaining steps and work towards a conclusion."
            )
        return (
            f"🔄 Iteration {current_iteration}/{max_iterations} - Continue your response if you have more to say, "
            f"or if you need to make additional tool calls or sub-agent calls."
        )

    @staticmethod
    def _build_token_limit_hint(
        current_prompt_tokens: int,
        max_tokens: int,
        remaining_tokens: int,
        desired_max_tokens: int,
    ) -> str:
        """Replicate executor token limit hint messaging."""

        if remaining_tokens < 3 * desired_max_tokens:
            return (
                f"⚠️ WARNING: Token usage is approaching the limit {current_prompt_tokens}/{max_tokens}."
                f" You have only {remaining_tokens} tokens left."
                f" Please be mindful of the token limit and avoid making additional tool calls or sub-agent calls "
                f"unless absolutely critical. Focus on summarizing your findings and providing final recommendations."
            )
        return (
            f"🔄 Token Usage: {current_prompt_tokens}/{max_tokens} in the current prompt - {remaining_tokens} tokens left."
            f" Continue your response if you have more to say, or if you need to make additional tool calls or sub-agent calls."
        )
