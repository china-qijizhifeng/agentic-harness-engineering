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

"""Factory functions for creating compaction and trigger strategies."""

from typing import Any

from .compact_stratigies.base import CompactionStrategy
from .compact_stratigies.compact_tool_result import ToolResultCompaction
from .compact_stratigies.sliding_window import SlidingWindowCompaction
from .compact_stratigies.user_model_full_trace_adaptive import UserModelFullTraceAdaptiveCompaction
from .config import CompactionConfig
from .trigger_strategies.base import TriggerStrategy
from .trigger_strategies.token_threshold import TokenThresholdTrigger


def create_compaction_strategy(config: CompactionConfig) -> CompactionStrategy:
    """Builds the specific compaction strategy based on config.

    Args:
        config: Validated configuration object (with resolved paths)

    Returns:
        Initialized compaction strategy instance

    Raises:
        ValueError: If strategy type is unknown
    """
    if config.compaction_strategy == "sliding_window":
        return SlidingWindowCompaction(
            keep_iterations=config.keep_iterations,
            keep_user_rounds=config.keep_user_rounds,
            summary_model=config.summary_model,
            summary_base_url=config.summary_base_url,
            summary_api_key=config.summary_api_key,
            summary_api_type=config.summary_api_type,
            retry_attempts=config.retry_attempts,
            max_context_tokens=config.max_context_tokens,
            compact_prompt_path=config.compact_prompt_path,  # Already resolved by config
        )

    elif config.compaction_strategy == "tool_result_compaction":
        return ToolResultCompaction(
            keep_iterations=config.keep_iterations,
            keep_user_rounds=config.keep_user_rounds,
        )

    raise ValueError(f"Unknown compaction strategy: {config.compaction_strategy}")


def create_trigger_strategy(config: CompactionConfig) -> TriggerStrategy:
    """Builds the trigger strategy based on config.

    Args:
        config: Validated configuration object

    Returns:
        Initialized trigger strategy instance
    """
    return TokenThresholdTrigger(threshold=config.threshold)


def create_emergency_compaction_strategy(
    *,
    token_counter: Any,
    max_context_tokens: int,
) -> UserModelFullTraceAdaptiveCompaction:
    """Build emergency overflow compaction strategy for wrap fallback."""
    return UserModelFullTraceAdaptiveCompaction(
        token_counter=token_counter,
        max_context_tokens=max_context_tokens,
    )
