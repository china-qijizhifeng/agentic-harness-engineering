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

"""Sliding window compaction strategy."""

import logging
from pathlib import Path
from typing import Any, cast

import anthropic
import openai
from openai.types.chat import ChatCompletion

from nexau.archs.llm.llm_config import LLMConfig
from nexau.archs.main_sub.execution.llm_caller import LLMCaller
from nexau.core.adapters.legacy import messages_to_legacy_openai_chat
from nexau.core.messages import Message, Role, TextBlock, ToolUseBlock

from nexau.archs.main_sub.utils.token_counter import TokenCounter

logger = logging.getLogger(__name__)

# Backward-compatibility: older tests patch `sliding_window.OpenAI`.
OpenAI = openai.OpenAI


def _load_compact_prompt(prompt_path: str) -> str:
    """Load the compact prompt template from file.

    Args:
        prompt_path: Path to compact prompt file (already resolved by config).

    Returns:
        The compact prompt content as a string.

    Raises:
        FileNotFoundError: If the template file is not found.
    """
    template_file = Path(prompt_path)

    try:
        with open(template_file, encoding="utf-8") as f:
            content = f.read()
            return content
    except FileNotFoundError:
        logger.error(f"Compact prompt template not found at {template_file}")
        raise
    except Exception as e:
        logger.error(f"Failed to load compact prompt template: {e}")
        raise


class SlidingWindowCompaction:
    """Sliding window compaction strategy - keeps recent conversation iterations.

    An "iteration" is bounded by ASSISTANT messages:
    [USER or FRAMEWORK](optional) -> [ASSISTANT] -> [TOOL results](optional)

    - Each ASSISTANT message starts a new iteration
    - USER or FRAMEWORK messages before the ASSISTANT are part of that iteration (optional)
    - TOOL results after the ASSISTANT are part of that iteration (optional)

    This strategy:
    1. Groups messages into conversation iterations
    2. Keeps the most recent N iterations in full
    3. Compresses older iterations using LLM summarization
    4. Handles large inputs safely via chunked summarization
    """

    # Reserved tokens for compact_prompt + LLM output overhead
    _SUMMARY_RESERVED_TOKENS = 4096

    def __init__(
        self,
        keep_system: bool = True,
        keep_iterations: int = 3,
        keep_user_rounds: int = 0,
        summary_model: str | None = None,
        summary_base_url: str | None = None,
        summary_api_key: str | None = None,
        summary_api_type: str = "openai_chat_completion",
        max_context_tokens: int = 128000,
        compact_prompt_path: str | None = None,
        token_counter: TokenCounter | None = None,
        retry_attempts: int = 3,
    ):
        """Initialize sliding window compaction.

        Args:
            keep_system: Whether to preserve the system message.
            keep_iterations: Number of recent iterations to keep. Default: 3.
            keep_user_rounds: Number of recent user rounds to keep. Default: 0 (disabled).
                When > 0, uses user rounds mode instead of iterations mode.
            summary_model: LLM model for summarization. Required.
            summary_base_url: LLM API base URL for summarization. Required.
            summary_api_key: LLM API key for summarization. Required.
            summary_api_type: LLM API type for summarization. Required.
            retry_attempts: Number of retry attempts for LLM calls. Default: 3.
            token_counter: Token counter instance for counting tokens. If None, a default TokenCounter is used.
            max_context_tokens: Context window size of the summary LLM. Default: 128000.
            compact_prompt_path: Path to compact prompt file (already resolved by config). Required.

        Raises:
            ValueError: If both keep_iterations != 3 and keep_user_rounds > 0 are set.
            ValueError: If keep_iterations < 1 or keep_user_rounds < 0.
            ValueError: If LLM configuration is missing.
        """
        if keep_iterations != 3 and keep_user_rounds > 0:
            raise ValueError("Cannot set both keep_iterations and keep_user_rounds")

        if keep_iterations < 1:
            raise ValueError(f"keep_iterations must be >= 1, got {keep_iterations}")
        if keep_user_rounds < 0:
            raise ValueError(f"keep_user_rounds must be >= 0, got {keep_user_rounds}")

        # Validate LLM configuration
        if not summary_model or not summary_base_url or not summary_api_key:
            raise ValueError(
                "LLM configuration is required for SlidingWindowCompaction. "
                "Please provide summary_model, summary_base_url, and summary_api_key."
            )

        # Validate compact prompt path
        if not compact_prompt_path:
            raise ValueError("compact_prompt_path is required for SlidingWindowCompaction.")

        self.keep_system = keep_system
        self.keep_iterations = keep_iterations
        self.keep_user_rounds = keep_user_rounds
        self.max_context_tokens = max_context_tokens

        # Initialize LLM caller for summarization (route API calls through LLMCaller).
        self.summary_model = summary_model
        self.summary_base_url = summary_base_url
        self.summary_api_key = summary_api_key
        self.summary_api_type = summary_api_type
        self.token_counter = token_counter or TokenCounter()
        # Load compact prompt using the resolved path
        self.compact_prompt = _load_compact_prompt(compact_prompt_path)
        logger.info(f"[SlidingWindowCompaction] summary_api_type {summary_api_type}")
        summary_llm_config = LLMConfig(
            model=self.summary_model,
            base_url=self.summary_base_url,
            api_key=self.summary_api_key,
            api_type=summary_api_type,
        )

        summary_client = self._initialize_openai_client(summary_llm_config)
        self._summary_client = summary_client
        logger.info(f"summary_client {summary_client}")
        self._llm_caller = LLMCaller(summary_client, summary_llm_config, retry_attempts=retry_attempts)
        logger.info(
            f"[SlidingWindowCompaction] Initialized: model={self.summary_model}, "
            f"keep_iterations={self.keep_iterations}, keep_user_rounds={self.keep_user_rounds}, "
            f"max_context_tokens={self.max_context_tokens}"
        )

    @property
    def _summary_input_limit(self) -> int:
        """Max input tokens allowed when calling the summary LLM."""
        return self.max_context_tokens - self._SUMMARY_RESERVED_TOKENS

    def _initialize_openai_client(self, llm_config: LLMConfig) -> Any:
        """Initialize OpenAI client from LLM config."""
        # Guard clause

        try:
            if self.summary_api_type == "gemini_rest":
                return None
            if self.summary_api_type == "anthropic_chat_completion":
                client_kwargs = llm_config.to_client_kwargs()
                return anthropic.Anthropic(**client_kwargs)
            if llm_config.api_type in ["openai_responses", "openai_chat_completion"]:
                client_kwargs = llm_config.to_client_kwargs()
                return OpenAI(**client_kwargs)
            raise ValueError(f"Invalid API type: {llm_config.api_type}")
        except Exception as e:
            logger.error(f"❌ Failed to initialize OpenAI client: {e}")
            return None

    def compact(
        self,
        messages: list[Message],
    ) -> list[Message]:
        """Compact messages by keeping recent iterations or user rounds.

        Uses incremental summarization: each compaction only summarizes
        the messages that have slid out of the window since last time.
        Previous summaries are naturally included because they were injected
        into a user message that is now part of the messages to compress.

        For large inputs that exceed the summary LLM's context window,
        falls back to chunked summarization (split → summarize each → merge).

        If all summarization attempts fail, uses hard truncation as a last
        resort to guarantee the output fits within context limits.
        """
        logger.info(f"[SlidingWindowCompaction] Starting compaction on {len(messages)} messages")

        result: list[Message] = []
        start_idx = 0

        # Keep system message if present
        if self.keep_system and messages and messages[0].role == Role.SYSTEM:
            result.append(messages[0])
            start_idx = 1

        # Group messages based on keep_user_rounds or keep_iterations
        if self.keep_user_rounds > 0:
            groups = self._group_into_user_rounds(messages[start_idx:])
            keep_count = self.keep_user_rounds
            group_name = "user_rounds"
        else:
            groups = self._group_into_iterations(messages[start_idx:])
            keep_count = self.keep_iterations
            group_name = "iterations"
        if len(groups) <= keep_count:
            logger.info(f"[SlidingWindowCompaction] Skipping: {len(groups)} {group_name} <= {keep_count}")
            return messages.copy()

        # Calculate how many groups to compress
        groups_to_compress = groups[:-keep_count]
        groups_to_keep = groups[-keep_count:]

        # Collect all messages to compress (include system for context)
        all_compressed_messages: list[Message] = []
        if self.keep_system and messages and messages[0].role == Role.SYSTEM:
            all_compressed_messages.append(messages[0])
        for group_msgs in groups_to_compress:
            all_compressed_messages.extend(group_msgs)

        # Generate summary safely (handles oversized input)
        summary = self._generate_summary_safe(all_compressed_messages)

        # Inject summary into the first USER message of kept groups
        self._inject_summary(result, groups_to_keep, summary)
        input_tokens = self.token_counter.count_tokens(messages)
        summary_tokens = self.token_counter.count_tokens(result)
        logger.info(
            f"[SlidingWindowCompaction] Compaction complete: "
            f"{input_tokens} tokens -> {summary_tokens} tokens "
            f"({len(groups_to_compress)} {group_name} compressed, {len(groups_to_keep)} {group_name} kept)"
        )
        return result

    def _generate_summary_safe(self, messages: list[Message]) -> str:
        """Generate summary with automatic chunking for oversized inputs.

        Strategy:
        1. If input fits within summary LLM's context → direct summarization
        2. If input is too large → split into chunks by iteration boundaries,
           summarize each chunk, then merge summaries
        3. If all LLM calls fail → return a hard truncation placeholder

        Args:
            messages: Messages to summarize.

        Returns:
            Summary text (never raises, always returns something).
        """
        input_tokens = self.token_counter.count_tokens(messages)
        input_limit = self._summary_input_limit

        logger.info(f"[SlidingWindowCompaction] Summary input: {input_tokens} tokens, limit: {input_limit} tokens")
        if input_tokens <= input_limit:
            # Normal path: input fits, summarize directly
            try:
                return self._generate_summary(messages)
            except Exception as e:
                logger.error(f"[SlidingWindowCompaction] Direct summary failed: {e}")
                return self._hard_truncation_fallback(messages)

        # Input too large: chunked summarization
        logger.info(f"[SlidingWindowCompaction] Input exceeds limit ({input_tokens} > {input_limit}), using chunked summarization")
        try:
            return self._chunked_summary(messages, input_limit)
        except Exception as e:
            logger.error(f"[SlidingWindowCompaction] Chunked summary failed: {e}")
            return self._hard_truncation_fallback(messages)

    def _chunked_summary(self, messages: list[Message], chunk_token_limit: int) -> str:
        """Split messages into chunks, summarize each, then merge.

        Args:
            messages: Messages to summarize.
            chunk_token_limit: Max tokens per chunk.

        Returns:
            Merged summary text.

        Raises:
            RuntimeError: If all chunk summaries fail.
        """
        chunks = self._split_into_chunks(messages, chunk_token_limit)
        logger.info(f"[SlidingWindowCompaction] Split into {len(chunks)} chunks")

        # Summarize each chunk independently
        chunk_summaries: list[str] = []
        for i, chunk in enumerate(chunks):
            try:
                summary = self._generate_summary(chunk)
                chunk_summaries.append(summary)
                logger.info(f"[SlidingWindowCompaction] Chunk {i + 1}/{len(chunks)} summarized")
            except Exception as e:
                logger.warning(f"[SlidingWindowCompaction] Chunk {i + 1}/{len(chunks)} failed: {e}, skipping")

        if not chunk_summaries:
            raise RuntimeError("All chunk summaries failed")

        # Merge chunk summaries
        if len(chunk_summaries) == 1:
            return chunk_summaries[0]

        merged_text = "\n\n".join(f"[Part {i + 1}]: {s}" for i, s in enumerate(chunk_summaries))

        # Check if merged summaries need a final consolidation pass
        merged_msg = Message(role=Role.USER, content=[TextBlock(text=merged_text)])
        merged_tokens = self.token_counter.count_tokens([merged_msg])

        if merged_tokens <= chunk_token_limit:
            # Merge is small enough, do one final consolidation
            try:
                return self._generate_summary([merged_msg])
            except Exception:
                logger.warning("[SlidingWindowCompaction] Final merge summary failed, using concatenated summaries")
                return merged_text
        else:
            # Merged summaries still too large, recurse
            logger.info("[SlidingWindowCompaction] Merged summaries still too large, recursing")
            return self._chunked_summary([merged_msg], chunk_token_limit)

    def _split_into_chunks(self, messages: list[Message], chunk_token_limit: int) -> list[list[Message]]:
        """Split messages into chunks respecting iteration boundaries.

        Keeps complete iterations together within each chunk.
        If a single iteration exceeds the limit, it goes alone in its own chunk.

        Args:
            messages: Messages to split.
            chunk_token_limit: Max tokens per chunk.

        Returns:
            List of message chunks.
        """
        # Re-group into iterations to avoid splitting mid-iteration
        iterations = self._group_into_iterations(messages)

        chunks: list[list[Message]] = []
        current_chunk: list[Message] = []
        current_tokens = 0

        for iteration_msgs in iterations:
            iter_tokens = self.token_counter.count_tokens(iteration_msgs)

            if current_tokens + iter_tokens > chunk_token_limit and current_chunk:
                # Current chunk is full, start a new one
                chunks.append(current_chunk)
                current_chunk = []
                current_tokens = 0

            current_chunk.extend(iteration_msgs)
            current_tokens += iter_tokens

        if current_chunk:
            chunks.append(current_chunk)

        return chunks

    def _hard_truncation_fallback(self, messages: list[Message]) -> str:
        """Fallback by keeping the newest messages within token limit."""
        max_tokens = self._summary_input_limit
        tokens_used = 0
        retained_messages: list[Message] = []

        # Keep system message if present
        if self.keep_system and messages and messages[0].role == Role.SYSTEM:
            retained_messages.append(messages[0])
            tokens_used += self.token_counter.count_tokens([messages[0]])

        # Iterate messages from newest to oldest
        for msg in reversed(messages):
            msg_tokens = self.token_counter.count_tokens([msg])
            if tokens_used + msg_tokens > max_tokens:
                break  # stop adding older messages
            retained_messages.insert(1 if self.keep_system else 0, msg)
            tokens_used += msg_tokens

        # Convert retained messages to concatenated text
        context_snippets = [msg.get_text_content().strip() for msg in retained_messages if msg.get_text_content()]
        return "\n".join(context_snippets)

    def _inject_summary(
        self,
        result: list[Message],
        groups_to_keep: list[list[Message]],
        summary: str,
    ) -> None:
        """Inject summary into the first USER message of kept groups.

        Modifies result in place by appending messages from groups_to_keep,
        with the summary prepended to the first USER message found.

        Args:
            result: Result message list to append to (modified in place).
            groups_to_keep: Groups of messages to keep.
            summary: Summary text to inject.
        """
        first_user_modified = False
        for group_msgs in groups_to_keep:
            for msg in group_msgs:
                if msg.role == Role.USER and not first_user_modified:
                    original_content = msg.get_text_content()
                    modified_content = (
                        f"This session is being continued from a previous conversation that ran out of context. "
                        f"The previous conversation is summarized as follows: {summary}. "
                        f"The user request for this round is: {original_content}"
                    )
                    modified_msg = msg.model_copy(update={"content": [TextBlock(text=modified_content)]})
                    modified_msg.metadata["isSummary"] = True
                    result.append(modified_msg)
                    first_user_modified = True
                else:
                    result.append(msg)

    def _group_into_iterations(self, messages: list[Message]) -> list[list[Message]]:
        """Group messages into conversation iterations.

        An iteration is bounded by ASSISTANT messages:
        [USER or FRAMEWORK](optional) -> [ASSISTANT] -> [TOOL results](optional)

        - Each ASSISTANT message starts a new iteration
        - USER or FRAMEWORK messages before the ASSISTANT are part of that iteration (optional)
        - TOOL results after the ASSISTANT are part of that iteration (optional)
        """
        iterations: list[list[Message]] = []
        current_iteration: list[Message] = []

        for msg in messages:
            if msg.role == Role.ASSISTANT:
                # ASSISTANT starts a new iteration
                # Move any preceding USER and FRAMEWORK messages to the new iteration
                prefix_msgs: list[Message] = []
                while current_iteration and current_iteration[-1].role in (Role.USER, Role.FRAMEWORK):
                    prefix_msgs.insert(0, current_iteration.pop())

                if current_iteration:
                    iterations.append(current_iteration)

                current_iteration = prefix_msgs + [msg]
            else:
                # Continue current iteration (user, framework, or tool)
                current_iteration.append(msg)

        # Add the last iteration
        if current_iteration:
            iterations.append(current_iteration)

        return iterations

    def _group_into_user_rounds(self, messages: list[Message]) -> list[list[Message]]:
        """Group messages into user rounds.

        A UserRound starts with a USER message and ends with an ASSISTANT message
        that has no tool calls (final response).
        """
        user_rounds: list[list[Message]] = []
        current_round: list[Message] = []

        for msg in messages:
            current_round.append(msg)

            if msg.role == Role.ASSISTANT:
                # Check if this is a final response (no tool calls)
                has_tool_use = any(isinstance(block, ToolUseBlock) for block in msg.content)
                if not has_tool_use and current_round:
                    user_rounds.append(current_round)
                    current_round = []

        # Handle incomplete round at the end
        if current_round:
            user_rounds.append(current_round)

        return user_rounds

    def _generate_summary_direct_fallback(self, llm_messages: list[Message], *, max_tokens: int) -> str:
        """Fallback summary path for simple OpenAI-compatible mocked clients."""
        if self.summary_api_type != "openai_chat_completion":
            raise RuntimeError("Direct summary fallback only supports openai_chat_completion")
        if self._summary_client is None:
            raise RuntimeError("Summary client is not initialized")

        response = cast(
            ChatCompletion,
            self._summary_client.chat.completions.create(
                model=self.summary_model,
                messages=messages_to_legacy_openai_chat(llm_messages),
                max_tokens=max_tokens,
            ),
        )
        if not response.choices:
            return ""

        content = response.choices[0].message.content
        if isinstance(content, str):
            return content.strip()
        return ""

    def _generate_summary(self, messages: list[Message]) -> str:
        """Generate summary using LLM.

        Raises:
            Exception: If LLM call fails.
        """
        logger.info(f"[SlidingWindowCompaction] Calling LLM to generate summary (model: {self.summary_model})")

        # Prepare messages for LLM
        llm_messages = messages.copy()
        llm_messages.append(Message(role=Role.USER, content=[TextBlock(text=self.compact_prompt)]))

        tool_call_mode = "anthropic" if self.summary_api_type == "anthropic_chat_completion" else "openai"

        try:
            model_response = self._llm_caller.call_llm(
                llm_messages,
                max_tokens=2048,
                tool_call_mode=tool_call_mode,
            )
            summary = (model_response.content or "").strip() if model_response else ""
            logger.info("[SlidingWindowCompaction] LLM summary generated successfully")
            return summary
        except Exception as exc:
            if self.summary_api_type != "openai_chat_completion":
                raise

            logger.warning(
                "[SlidingWindowCompaction] LLMCaller summary generation failed; falling back to direct OpenAI-compatible client call: %s",
                exc,
            )
            summary = self._generate_summary_direct_fallback(llm_messages, max_tokens=2048)
            logger.info("[SlidingWindowCompaction] LLM summary generated successfully via direct client fallback")
            return summary
