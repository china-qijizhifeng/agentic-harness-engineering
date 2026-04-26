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

"""Context compaction middleware with customizable trigger and compaction strategies."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, cast

from nexau.archs.llm.llm_aggregators.events import CompactionFinishedEvent, CompactionStartedEvent

from nexau.archs.main_sub.execution.hooks import AfterModelHookInput, BeforeModelHookInput, HookResult, Middleware, ModelCallFn, ModelCallParams
from nexau.archs.main_sub.execution.llm_caller import LLMCaller
from .config import CompactionConfig
from .factory import create_compaction_strategy, create_emergency_compaction_strategy, create_trigger_strategy

if TYPE_CHECKING:
    from nexau.archs.main_sub.utils.token_counter import TokenCounter

from nexau.core.messages import Message, Role, ToolResultBlock, ToolUseBlock

logger = logging.getLogger(__name__)

_FULL_TRACE_MESSAGES_KEY = "__nexau_full_trace_messages__"
_FULL_TRACE_SEEN_IDS_KEY = "__nexau_full_trace_seen_ids__"
_CONTEXT_OVERFLOW_MARKERS = (
    "maximum context length",
    "context length exceeded",
    "context window",
    "context token",
    "context limit",
    "prompt is too long",
    "prompt too long",
    "input is too long",
    "input token count",
    "token limit exceeded",
    "context_length_exceeded",
)
CompactionPhase = Literal["before_model", "after_model", "wrap_model_call"]
CompactionMode = Literal["regular", "emergency"]


class ContextCompactionMiddleware(Middleware):
    """Middleware for automatic context compaction with customizable strategies.

    This middleware monitors token usage and automatically compacts the message
    history when certain thresholds are exceeded. Both the trigger logic and
    compaction logic can be customized.

    Default behavior:
    - Trigger: When token usage reaches 75% (25% remaining)
    - Compaction: Sliding window - keeps only the most recent N messages (default: 20)
    - Simple and efficient: No summarization overhead
    """

    def __init__(
        self,
        *,
        token_counter: TokenCounter | None = None,
        **kwargs: Any,
    ) -> None:
        """Initialize context compaction middleware.

        Args:
            token_counter: Token counter instance for fallback counting
            **kwargs: Configuration parameters validated by CompactionConfig.
                     All configuration including strategies should be passed via kwargs.
        """
        # Initialize statistics
        self._compact_count = 0
        self._total_messages_removed = 0
        self._event_emitter: Callable[[Any], None] | None = None

        # Initialize token counter
        if token_counter is None:
            from nexau.archs.main_sub.utils.token_counter import TokenCounter

            self.token_counter = TokenCounter()
        else:
            self.token_counter = token_counter

        # Configuration parameters are required
        if not kwargs:
            raise ValueError(
                "Configuration parameters (kwargs) are required. Provide at least the required config fields or use default values."
            )

        # Pydantic validates the flat YAML/dict here
        if "overflow_max_tokens_stop_enabled" in kwargs:
            logger.warning(
                "[ContextCompactionMiddleware] 'overflow_max_tokens_stop_enabled' is deprecated in middleware params "
                "and ignored. Configure it at agent/executor level instead."
            )
        config = CompactionConfig(**kwargs)

        self.max_context_tokens = config.max_context_tokens
        self.auto_compact = config.auto_compact
        self.emergency_compact_enabled = config.emergency_compact_enabled

        # Create strategies from config
        self.trigger_strategy = create_trigger_strategy(config)
        self.compaction_strategy = create_compaction_strategy(config)
        self.emergency_compaction_strategy = create_emergency_compaction_strategy(
            token_counter=self.token_counter,
            max_context_tokens=self.max_context_tokens,
        )

        logger.info(
            f"[ContextCompactionMiddleware] Initialized: "
            f"max_context_tokens={self.max_context_tokens}, "
            f"auto_compact={self.auto_compact}, "
            f"emergency_compact_enabled={self.emergency_compact_enabled}, "
            f"trigger={self.trigger_strategy.__class__.__name__}, "
            f"compaction={self.compaction_strategy.__class__.__name__}, "
            f"emergency_compaction={self.emergency_compaction_strategy.__class__.__name__}"
        )

    def set_event_emitter(self, event_emitter: Callable[[Any], None]) -> None:
        """Inject a unified event emitter (typically from AgentEventsMiddleware)."""
        self._event_emitter = event_emitter

    @staticmethod
    def _resolve_run_id(agent_state: Any | None) -> str | None:
        if agent_state is None:
            return None
        run_id = getattr(agent_state, "run_id", None)
        return run_id if isinstance(run_id, str) and run_id else None

    def _emit_compaction_started(
        self,
        *,
        agent_state: Any | None,
        phase: CompactionPhase,
        mode: CompactionMode,
        trigger_reason: str | None,
        original_message_count: int,
        original_token_count: int | None,
    ) -> None:
        if self._event_emitter is None:
            return
        run_id = self._resolve_run_id(agent_state)
        if run_id is None:
            return
        self._event_emitter(
            CompactionStartedEvent(
                run_id=run_id,
                phase=phase,
                mode=mode,
                trigger_reason=trigger_reason,
                original_message_count=original_message_count,
                original_token_count=original_token_count,
                max_context_tokens=self.max_context_tokens,
                timestamp=int(datetime.now().timestamp() * 1000),
            )
        )

    def _emit_compaction_finished(
        self,
        *,
        agent_state: Any | None,
        phase: CompactionPhase,
        mode: CompactionMode,
        success: bool,
        trigger_reason: str | None,
        original_message_count: int,
        compacted_message_count: int | None,
        original_token_count: int | None,
        compacted_token_count: int | None,
        error: str | None = None,
    ) -> None:
        if self._event_emitter is None:
            return
        run_id = self._resolve_run_id(agent_state)
        if run_id is None:
            return
        self._event_emitter(
            CompactionFinishedEvent(
                run_id=run_id,
                phase=phase,
                mode=mode,
                success=success,
                trigger_reason=trigger_reason,
                original_message_count=original_message_count,
                compacted_message_count=compacted_message_count,
                original_token_count=original_token_count,
                compacted_token_count=compacted_token_count,
                max_context_tokens=self.max_context_tokens,
                error=error,
                timestamp=int(datetime.now().timestamp() * 1000),
            )
        )

    def _get_current_tokens(self, hook_input: AfterModelHookInput) -> int | None:
        """Extract token count from model response usage information.

        Args:
            hook_input: After model hook input containing model response

        Returns:
            Total token count if available, None otherwise
        """
        if not hook_input.model_response or not hook_input.model_response.usage:
            return None

        usage = hook_input.model_response.usage

        # Usage is now in normalized format with total_tokens field
        if "total_tokens" in usage:
            return usage["total_tokens"]

        # Try to calculate from components if total is missing
        input_tokens = usage.get("input_tokens") or usage.get("prompt_tokens")
        output_tokens = usage.get("completion_tokens") or usage.get("output_tokens")

        if input_tokens is not None and output_tokens is not None:
            try:
                input_val = int(input_tokens)
                output_val = int(output_tokens)

                # Check for cache tokens (e.g. Anthropic)
                # If input_tokens_uncached exists, input_tokens likely already includes cache (normalized)
                # If not, and we have cache keys, we should add them (raw usage)
                if "input_tokens_uncached" not in usage:
                    cache_creation = int(usage.get("cache_creation_input_tokens") or 0)
                    cache_read = int(usage.get("cache_read_input_tokens") or 0)
                    input_val += cache_creation + cache_read

                return input_val + output_val
            except (ValueError, TypeError):
                pass

        # Unknown format
        logger.warning(f"[ContextCompactionMiddleware] Unknown usage format: {usage}")
        return None

    @staticmethod
    def _is_context_overflow_error(exc: BaseException) -> bool:
        """Best-effort detection for provider errors indicating prompt/context overflow."""
        visited: set[int] = set()
        pending: list[BaseException] = [exc]

        while pending:
            current = pending.pop()
            current_id = id(current)
            if current_id in visited:
                continue
            visited.add(current_id)

            text_parts = [str(current)]
            body = getattr(current, "body", None)
            if body is not None:
                text_parts.append(str(body))

            text = " ".join(text_parts).lower()
            if any(marker in text for marker in _CONTEXT_OVERFLOW_MARKERS):
                return True

            cause = current.__cause__
            if isinstance(cause, BaseException):
                pending.append(cause)

            context = current.__context__
            if isinstance(context, BaseException):
                pending.append(context)

        return False

    @staticmethod
    def _normalize_tools_for_token_count(
        tools: list[Any] | None,
    ) -> list[dict[str, Any]] | None:
        """Convert tool payloads into dicts for token estimation when possible."""
        if not tools:
            return None

        normalized: list[dict[str, Any]] = []
        for tool in tools:
            if isinstance(tool, dict):
                normalized.append(cast(dict[str, Any], tool))
                continue

            model_dump = getattr(tool, "model_dump", None)
            if callable(model_dump):
                dumped = model_dump()
                if isinstance(dumped, dict):
                    normalized.append(cast(dict[str, Any], dumped))
                    continue

            return None

        return normalized

    def _estimate_tokens(
        self,
        messages: list[Message],
        tools: list[Any] | None = None,
    ) -> int | None:
        """Estimate tokens for diagnostics; never raise from middleware paths."""
        normalized_tools = self._normalize_tools_for_token_count(tools)
        try:
            return self.token_counter.count_tokens(messages, tools=normalized_tools)
        except Exception as exc:  # pragma: no cover - diagnostic-only fallback
            logger.debug("[ContextCompactionMiddleware] Token estimation failed: %s", exc)
            return None

    def before_model(self, hook_input: BeforeModelHookInput) -> HookResult:
        """Compaction before the model call when budget is near limit."""
        if not self.auto_compact:
            return HookResult.no_changes()

        messages = hook_input.messages
        current_tokens = self._estimate_tokens(messages)
        if current_tokens is None:
            return HookResult.no_changes()

        usage_ratio = current_tokens / self.max_context_tokens
        logger.info(
            "[ContextCompactionMiddleware] Pre-call compaction check: %d messages, %d/%d tokens (%.1f%%)",
            len(messages),
            current_tokens,
            self.max_context_tokens,
            usage_ratio * 100,
        )

        should_compact, trigger_reason = self.trigger_strategy.should_compact(
            messages,
            current_tokens,
            self.max_context_tokens,
        )
        if not should_compact:
            return HookResult.no_changes()

        try:
            self._record_full_trace_messages(hook_input.agent_state, messages)
        except Exception as exc:  # pragma: no cover - best effort
            logger.debug("[ContextCompactionMiddleware] full-trace capture failed in before_model: %s", exc)

        logger.info("[ContextCompactionMiddleware] Pre-call compaction triggered: %s", trigger_reason)
        original_count = len(messages)
        self._emit_compaction_started(
            agent_state=hook_input.agent_state,
            phase="before_model",
            mode="regular",
            trigger_reason=trigger_reason,
            original_message_count=original_count,
            original_token_count=current_tokens,
        )
        try:
            compacted_messages = self._compact_messages(messages)
        except Exception as exc:
            self._emit_compaction_finished(
                agent_state=hook_input.agent_state,
                phase="before_model",
                mode="regular",
                success=False,
                trigger_reason=trigger_reason,
                original_message_count=original_count,
                compacted_message_count=None,
                original_token_count=current_tokens,
                compacted_token_count=None,
                error=str(exc),
            )
            raise

        self._compact_count += 1
        self._total_messages_removed += original_count - len(compacted_messages)
        after_tokens = self._estimate_tokens(compacted_messages)
        self._emit_compaction_finished(
            agent_state=hook_input.agent_state,
            phase="before_model",
            mode="regular",
            success=True,
            trigger_reason=trigger_reason,
            original_message_count=original_count,
            compacted_message_count=len(compacted_messages),
            original_token_count=current_tokens,
            compacted_token_count=after_tokens,
        )

        logger.info(
            "[ContextCompactionMiddleware] Pre-call compaction complete: %d -> %d messages",
            original_count,
            len(compacted_messages),
        )
        return HookResult.with_modifications(messages=compacted_messages)

    def _build_emergency_summarize_fn(self, params: ModelCallParams) -> Callable[[list[Message], str, int], str]:
        if params.llm_config is None:
            raise RuntimeError("llm_config is required for emergency compaction summarization")

        summary_llm_config = params.llm_config.copy()
        summary_llm_config.temperature = 0
        summary_llm_config.stream = False

        llm_caller = LLMCaller(
            params.openai_client,
            summary_llm_config,
            retry_attempts=1,
            middleware_manager=None,
        )

        def summarize_fn(messages: list[Message], prompt: str, max_tokens: int) -> str:
            summary_messages = list(messages)
            summary_messages.append(Message.user(prompt))
            response = llm_caller.call_llm(
                summary_messages,
                max_tokens=max_tokens,
                force_stop_reason=None,
                agent_state=params.agent_state,
                tool_call_mode="xml",
                tools=None,
                openai_client=params.openai_client,
                shutdown_event=params.shutdown_event,
            )
            text = (response.content or "").strip() if response else ""
            return text if text else "NONE"

        return summarize_fn

    def wrap_model_call(
        self,
        params: ModelCallParams,
        call_next: ModelCallFn,
    ) -> Any:
        """Fallback path: emergency compact and retry once when provider overflows."""
        try:
            return call_next(params)
        except Exception as exc:
            if not self.auto_compact or not self.emergency_compact_enabled or not self._is_context_overflow_error(exc):
                raise

            logger.warning(
                "[ContextCompactionMiddleware] Model call failed due to context overflow; "
                "attempting fixed two-segment emergency compaction retry"
            )

            # Keep full trace uncompacted even if compaction happens during fallback path.
            if params.agent_state is not None:
                try:
                    self._record_full_trace_messages(params.agent_state, params.messages)
                except Exception as trace_exc:  # pragma: no cover - best effort only
                    logger.debug("[ContextCompactionMiddleware] full-trace capture failed in fallback: %s", trace_exc)

            original_messages = params.messages
            summarize_fn = self._build_emergency_summarize_fn(params)
            before_tokens = self._estimate_tokens(original_messages, params.tools)
            self._emit_compaction_started(
                agent_state=params.agent_state,
                phase="wrap_model_call",
                mode="emergency",
                trigger_reason="provider_context_overflow",
                original_message_count=len(original_messages),
                original_token_count=before_tokens,
            )
            try:
                compacted_messages = self.emergency_compaction_strategy.compact(
                    original_messages,
                    summarize_fn=summarize_fn,
                )
            except Exception as compact_exc:
                self._emit_compaction_finished(
                    agent_state=params.agent_state,
                    phase="wrap_model_call",
                    mode="emergency",
                    success=False,
                    trigger_reason="provider_context_overflow",
                    original_message_count=len(original_messages),
                    compacted_message_count=None,
                    original_token_count=before_tokens,
                    compacted_token_count=None,
                    error=str(compact_exc),
                )
                raise

            self._compact_count += 1
            self._total_messages_removed += len(original_messages) - len(compacted_messages)

            after_tokens = self._estimate_tokens(compacted_messages, params.tools)

            if after_tokens is not None and after_tokens >= self.max_context_tokens:
                logger.error(
                    "[ContextCompactionMiddleware] Emergency compaction result still exceeds context limit: %d/%d",
                    after_tokens,
                    self.max_context_tokens,
                )
                self._emit_compaction_finished(
                    agent_state=params.agent_state,
                    phase="wrap_model_call",
                    mode="emergency",
                    success=False,
                    trigger_reason="provider_context_overflow",
                    original_message_count=len(original_messages),
                    compacted_message_count=len(compacted_messages),
                    original_token_count=before_tokens,
                    compacted_token_count=after_tokens,
                    error=f"maximum context length exceeded after emergency compaction ({after_tokens}/{self.max_context_tokens})",
                )
                raise RuntimeError(
                    f"maximum context length exceeded after emergency compaction ({after_tokens}/{self.max_context_tokens})"
                ) from exc

            token_stats = f", tokens {before_tokens} -> {after_tokens}" if before_tokens is not None and after_tokens is not None else ""

            logger.warning(
                "[ContextCompactionMiddleware] Fallback compaction complete: %d -> %d messages%s. Retrying model call once.",
                len(original_messages),
                len(compacted_messages),
                token_stats,
            )
            self._emit_compaction_finished(
                agent_state=params.agent_state,
                phase="wrap_model_call",
                mode="emergency",
                success=True,
                trigger_reason="provider_context_overflow",
                original_message_count=len(original_messages),
                compacted_message_count=len(compacted_messages),
                original_token_count=before_tokens,
                compacted_token_count=after_tokens,
            )

            compacted_params = replace(params, messages=compacted_messages)
            return call_next(compacted_params)

    def after_model(self, hook_input: AfterModelHookInput) -> HookResult:
        """Check and compact messages after each model call if needed."""
        # Best-effort "full trace" capture: context compaction mutates `messages` and therefore
        # the final agent.history loses earlier dialogue. We maintain a separate append-only trace
        # in agent context for debugging/training purposes.
        try:
            self._record_full_trace(hook_input)
        except Exception as exc:  # pragma: no cover
            logger.debug("[ContextCompactionMiddleware] full-trace capture failed: %s", exc)

        if not self.auto_compact:
            return HookResult.no_changes()

        messages = hook_input.messages

        # Get token count from model response usage information
        current_tokens = self._get_current_tokens(hook_input)

        # If we couldn't get token count from model response, fall back to token_counter
        if current_tokens is None:
            logger.warning("[ContextCompactionMiddleware] No usage information from model response, falling back to token_counter")
            current_tokens = self.token_counter.count_tokens(messages)
            logger.info(f"[ContextCompactionMiddleware] Local token estimation: {current_tokens}")
        else:
            logger.info(f"[ContextCompactionMiddleware] Using API-reported token usage: {current_tokens}")

        usage_ratio = current_tokens / self.max_context_tokens

        logger.info(
            f"[ContextCompactionMiddleware] Checking compaction: "
            f"{len(messages)} messages, {current_tokens}/{self.max_context_tokens} tokens ({usage_ratio:.1%})"
        )

        # Check if the last assistant message has tool calls
        # If not, skip compaction to preserve the conversation state
        last_assistant_msg: Message | None = None
        for msg in reversed(messages):
            if msg.role == Role.ASSISTANT:
                last_assistant_msg = msg
                break

        if last_assistant_msg is not None:
            has_tool_calls = any(isinstance(block, ToolUseBlock) for block in last_assistant_msg.content)

            if not has_tool_calls:
                logger.info(
                    "[ContextCompactionMiddleware] Last assistant message has no tool calls, "
                    "skipping compaction to preserve conversation state"
                )
                return HookResult.no_changes()
            else:
                logger.info("[ContextCompactionMiddleware] Last assistant message has tool calls, proceeding with compaction check")

        # Check if compaction should be triggered
        should_compact, trigger_reason = self.trigger_strategy.should_compact(
            messages,
            current_tokens,
            self.max_context_tokens,
        )

        if not should_compact:
            logger.info("[ContextCompactionMiddleware] No compaction needed")
            return HookResult.no_changes()

        logger.info(f"[ContextCompactionMiddleware] Compaction triggered: {trigger_reason}")

        # Perform compaction
        original_message_count = len(messages)
        self._emit_compaction_started(
            agent_state=hook_input.agent_state,
            phase="after_model",
            mode="regular",
            trigger_reason=trigger_reason,
            original_message_count=original_message_count,
            original_token_count=current_tokens,
        )
        try:
            compacted_messages = self._compact_messages(messages)
        except Exception as exc:
            self._emit_compaction_finished(
                agent_state=hook_input.agent_state,
                phase="after_model",
                mode="regular",
                success=False,
                trigger_reason=trigger_reason,
                original_message_count=original_message_count,
                compacted_message_count=None,
                original_token_count=current_tokens,
                compacted_token_count=None,
                error=str(exc),
            )
            raise
        compacted_message_count = len(compacted_messages)
        compacted_tokens = self._estimate_tokens(compacted_messages)

        # Update statistics
        self._compact_count += 1
        self._total_messages_removed += original_message_count - compacted_message_count
        self._emit_compaction_finished(
            agent_state=hook_input.agent_state,
            phase="after_model",
            mode="regular",
            success=True,
            trigger_reason=trigger_reason,
            original_message_count=original_message_count,
            compacted_message_count=compacted_message_count,
            original_token_count=current_tokens,
            compacted_token_count=compacted_tokens,
        )

        logger.info(
            f"[ContextCompactionMiddleware] Compaction complete: "
            f"{original_message_count} -> {compacted_message_count} messages "
            f"({original_message_count - compacted_message_count} removed)"
        )

        return HookResult.with_modifications(messages=compacted_messages)

    def _compact_messages(
        self,
        messages: list[Message],
    ) -> list[Message]:
        """Compact messages using the configured strategy."""
        return self.compaction_strategy.compact(messages)

    @staticmethod
    def _is_compaction_artifact(msg: Message) -> bool:
        """Detect synthetic messages produced by compaction strategies.

        We want full_trace to reflect the uncompacted dialogue.
        """
        try:
            md: dict[str, Any] = getattr(msg, "metadata", None) or {}
            if md.get("isSummary") is True or md.get("is_compacted") is True or md.get("compacted") is True:
                return True
        except Exception:
            pass

        # ToolResultCompaction replaces old tool results with this placeholder string.
        if msg.role == Role.TOOL:
            try:
                for block in msg.content or []:
                    if isinstance(block, ToolResultBlock) and str(block.content) == "Tool call result has been compacted":
                        return True
            except Exception:
                pass
        return False

    def _record_full_trace(self, hook_input: AfterModelHookInput) -> None:
        """Append newly-seen messages to agent context full trace."""
        self._record_full_trace_messages(hook_input.agent_state, hook_input.messages)

    def _record_full_trace_messages(self, agent_state: Any, messages: list[Message]) -> None:
        """Append newly-seen messages to full trace in agent context."""
        full_raw: Any = agent_state.get_context_value(_FULL_TRACE_MESSAGES_KEY, [])
        full: list[Message] = cast(list[Message], full_raw) if isinstance(full_raw, list) else []

        seen: Any = agent_state.get_context_value(_FULL_TRACE_SEEN_IDS_KEY, set())
        seen_ids: set[int] = set()
        if isinstance(seen, set):
            seen_ids = cast(set[int], seen)
        elif isinstance(seen, list):
            seen_ids = {int(x) for x in cast(list[Any], seen)}

        # Use object identity within a single run to avoid duplicates.
        for msg in messages:
            if self._is_compaction_artifact(msg):
                continue
            mid = id(msg)
            if mid in seen_ids:
                continue
            seen_ids.add(mid)
            try:
                full.append(msg.model_copy(deep=True))
            except Exception:
                full.append(msg)

        agent_state.set_context_value(_FULL_TRACE_MESSAGES_KEY, full)
        agent_state.set_context_value(_FULL_TRACE_SEEN_IDS_KEY, seen_ids)
