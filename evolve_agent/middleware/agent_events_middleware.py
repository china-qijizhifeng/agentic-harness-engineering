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

"""Agent events middleware that bridges llm_aggregators events with agent events."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, TypeGuard

from openai.types.chat import ChatCompletionChunk
from openai.types.responses import ResponseStreamEvent

from nexau.archs.llm.llm_aggregators import (
    AnthropicEventAggregator,
    GeminiRestEventAggregator,
    OpenAIChatCompletionAggregator,
    OpenAIResponsesAggregator,
)
from nexau.archs.llm.llm_aggregators.events import (
    Event,
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    ToolCallResultEvent,
)
from nexau.archs.main_sub.execution.hooks import (
    AfterAgentHookInput,
    AfterToolHookInput,
    BeforeAgentHookInput,
    HookResult,
    Middleware,
    ModelCallParams,
)
from nexau.archs.main_sub.execution.stop_reason import AgentStopReason

if TYPE_CHECKING:
    from anthropic.types import RawMessageStreamEvent


logger = logging.getLogger(__name__)


def _noop_event_handler(_: Event) -> None:
    """No-op event handler for non-first choices to avoid duplicate UI updates."""
    return None


def is_anthropic_event(event: object) -> TypeGuard[RawMessageStreamEvent]:
    return event.__class__.__module__.startswith("anthropic.")


def is_openai_responses_event(event: object) -> TypeGuard[ResponseStreamEvent]:
    event_module = event.__class__.__module__
    return event_module.startswith("openai.types.responses") or event_module.startswith("openai.lib.streaming")


def is_gemini_rest_chunk(chunk: object) -> TypeGuard[dict[str, object]]:
    """Check if a chunk is a Gemini REST API streaming dict."""
    return isinstance(chunk, dict) and "candidates" in chunk


class AgentEventsMiddleware(Middleware):
    """Middleware that connects llm_aggregators events to streaming callbacks.

    This middleware bridges the gap between:
    1. llm_aggregators layer: which processes raw stream chunks (ChatCompletionChunk,
       ResponseStreamEvent, MessageDeltaEvent)
    2. Agent execution layer: which needs streaming callbacks

    The middleware accepts an on_event callback that will receive unified Event objects
    (from nexau.archs.llm.llm_aggregators.events), which are produced by the aggregators
    when they process stream chunks.

    Architecture:
    - In wrap_model_call: LLM caller passes stream chunks to middleware via stream_chunk
    - LLM aggregators: Convert raw chunks to unified Event objects and call on_event
    - This middleware: Provides on_event to llm_aggregators, and proxy to user's callback

    Note: The stream_chunk receives raw chunks from the LLM api (ChatCompletionChunk,
    ResponseStreamEvent, or MessageDeltaEvent), not Event objects.
    """

    def __init__(
        self,
        *,
        session_id: str,
        on_event: Callable[[Event], None] = _noop_event_handler,
    ):
        """Initialize the AgentEventsMiddleware.

        Args:
            on_event: Callback that receives unified Event objects from llm_aggregators.
                      These events are emitted by the aggregator when it processes stream
                      chunks from the LLM.
        """
        self.session_id = session_id
        self.on_event = on_event
        self.openai_chat_completion_aggregators: dict[str, OpenAIChatCompletionAggregator] = {}
        # Use lazy initialization to avoid passing on_event too early
        self._openai_responses_aggregator: OpenAIResponsesAggregator | None = None
        self._gemini_rest_aggregator: GeminiRestEventAggregator | None = None
        self._anthropic_aggregator: AnthropicEventAggregator | None = None
        # Track current run_id per aggregator to detect stream boundaries
        self._current_gemini_run_id: str = ""

    def openai_responses_aggregator(self, *, run_id: str) -> OpenAIResponsesAggregator:
        """Lazy initialization for OpenAIResponsesAggregator."""
        if self._openai_responses_aggregator is None:
            self._openai_responses_aggregator = OpenAIResponsesAggregator(
                on_event=self.on_event,
                run_id=run_id,
            )
        return self._openai_responses_aggregator

    def gemini_rest_aggregator(self, *, run_id: str) -> GeminiRestEventAggregator:
        """Lazy initialization for GeminiRestEventAggregator."""
        if self._gemini_rest_aggregator is None:
            self._gemini_rest_aggregator = GeminiRestEventAggregator(
                on_event=self.on_event,
                run_id=run_id,
            )
            self._current_gemini_run_id = run_id
        elif self._current_gemini_run_id != run_id:
            # New agent run — clear stale state from the previous stream
            self._gemini_rest_aggregator.clear()
            self._current_gemini_run_id = run_id
        return self._gemini_rest_aggregator

    def anthropic_aggregator(self, *, run_id: str) -> AnthropicEventAggregator:
        """Lazy initialization for AnthropicEventAggregator."""
        if self._anthropic_aggregator is None:
            self._anthropic_aggregator = AnthropicEventAggregator(
                on_event=self.on_event,
                run_id=run_id,
            )
        return self._anthropic_aggregator

    def before_agent(self, hook_input: BeforeAgentHookInput) -> HookResult:
        """Hook called before agent execution starts.

        Emits RunStartedEvent to signal the beginning of an agent run.
        For sub-agents, includes parent_run_id to establish hierarchy.

        Args:
            hook_input: Input containing agent state and messages

        Returns:
            HookResult with no changes
        """
        agent_state = hook_input.agent_state

        self.on_event(
            RunStartedEvent(
                thread_id=self.session_id,
                root_run_id=agent_state.root_run_id,
                run_id=agent_state.run_id,
                agent_id=agent_state.agent_id,
                timestamp=int(datetime.now().timestamp() * 1000),
            )
        )
        return HookResult.no_changes()

    def after_agent(self, hook_input: AfterAgentHookInput) -> HookResult:
        """Hook called after agent execution finishes.

        Emits RunFinishedEvent to signal the completion of an agent run.
        If the agent stopped due to an error, emits RunErrorEvent instead.

        Args:
            hook_input: Input containing agent state, messages and response

        Returns:
            HookResult with no changes
        """
        agent_state = hook_input.agent_state

        if hook_input.stop_reason in {
            AgentStopReason.ERROR_OCCURRED,
            AgentStopReason.CONTEXT_TOKEN_LIMIT,
        }:
            self.on_event(
                RunErrorEvent(
                    timestamp=int(datetime.now().timestamp() * 1000),
                    run_id=agent_state.run_id,
                    message=hook_input.agent_response,
                )
            )
        else:
            self.on_event(
                RunFinishedEvent(
                    thread_id=self.session_id,
                    run_id=agent_state.run_id,
                    result=hook_input.agent_response,
                    timestamp=int(datetime.now().timestamp() * 1000),
                )
            )
        return HookResult.no_changes()

    def after_tool(self, hook_input: AfterToolHookInput) -> HookResult:
        """Hook called after tool execution.

        Emits ToolCallResultEvent with the tool execution result.

        Args:
            hook_input: Input containing tool execution information and result

        Returns:
            HookResult with no changes (tool output is preserved)
        """

        # Create tool result content as JSON string
        content = json.dumps(hook_input.tool_output, ensure_ascii=False)

        # Emit ToolCallResultEvent
        self.on_event(
            ToolCallResultEvent(
                tool_call_id=hook_input.tool_call_id,
                content=content,
                timestamp=int(datetime.now().timestamp() * 1000),
            )
        )

        return HookResult.no_changes()

    def stream_chunk(
        self,
        chunk: ChatCompletionChunk | ResponseStreamEvent | RawMessageStreamEvent | dict[str, object],
        params: ModelCallParams,
    ) -> ChatCompletionChunk | ResponseStreamEvent | RawMessageStreamEvent | dict[str, object]:
        """Process raw stream chunks from LLM.

        This hook receives raw chunks from the LLM API streaming response. The chunks
        are one of these types:
        - ChatCompletionChunk (OpenAI Chat Completions API)
        - ResponseStreamEvent (OpenAI Responses API)
        - MessageDeltaEvent (Anthropic Messages API)
        - dict (Gemini REST API)

        Note: The actual Event objects (from llm_aggregators.events) are generated
        internally by the aggregator and passed to the on_event callback. The aggregator
        needs to be initialized with this middleware's on_event callback.

        Args:
            chunk: Raw chunk from LLM streaming API
            params: Current model call parameters

        Returns:
            The chunk (potentially modified) to pass to the next middleware
        """

        # Get run_id from agent_state - agent_state is always present in stream_chunk
        if params.agent_state is None:
            raise RuntimeError("agent_state is required in stream_chunk for run_id")
        run_id = params.agent_state.run_id

        # logger.debug("============stream_chunk============: %s", chunk.model_dump_json())

        if isinstance(chunk, ChatCompletionChunk):
            openai_chat_completion_aggregator = self.openai_chat_completion_aggregators.get(chunk.id)
            if not openai_chat_completion_aggregator:
                openai_chat_completion_aggregator = OpenAIChatCompletionAggregator(
                    on_event=self.on_event,
                    run_id=run_id,
                )
                self.openai_chat_completion_aggregators[chunk.id] = openai_chat_completion_aggregator
            openai_chat_completion_aggregator.aggregate(chunk)

        if is_openai_responses_event(chunk):
            openai_responses_aggregator = self.openai_responses_aggregator(run_id=run_id)
            if chunk.type == "response.created":
                self.openai_responses_aggregator(run_id=run_id).clear()
            openai_responses_aggregator.aggregate(chunk)

        if is_gemini_rest_chunk(chunk):
            gemini_aggregator = self.gemini_rest_aggregator(run_id=run_id)
            gemini_aggregator.aggregate(chunk)

        if is_anthropic_event(chunk):
            anthropic_agg = self.anthropic_aggregator(run_id=run_id)
            if chunk.type == "message_start":
                anthropic_agg.clear()
            anthropic_agg.aggregate(chunk)

        # This middleware doesn't modify chunks, just observes them
        # The actual event emission happens in the aggregator's on_event callback
        # that was passed to the llm_aggregator instance

        return chunk
