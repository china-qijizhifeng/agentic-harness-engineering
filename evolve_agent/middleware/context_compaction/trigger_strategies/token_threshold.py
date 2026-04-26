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

"""Trigger compaction when token usage exceeds a percentage threshold."""

import logging

from nexau.core.messages import Message

logger = logging.getLogger(__name__)


class TokenThresholdTrigger:
    """Trigger compaction when token usage exceeds a percentage threshold.

    This is similar to Claude Code's auto-compact behavior.
    """

    def __init__(self, threshold: float = 0.75):
        """Initialize token threshold trigger.

        Args:
            threshold: Trigger when usage exceeds this ratio (e.g., 0.75 = 75%). Default: 0.75.
        """
        self.threshold = threshold
        logger.info(f"[TokenThresholdTrigger] Initialized with threshold: {self.threshold:.1%}")

    def should_compact(
        self,
        messages: list[Message],
        current_tokens: int,
        max_context_tokens: int,
    ) -> tuple[bool, str]:
        """Check if compaction should be triggered based on token usage."""
        usage_ratio = current_tokens / max_context_tokens

        if usage_ratio >= self.threshold:
            remaining_ratio = 1.0 - usage_ratio
            return (
                True,
                f"Token usage {usage_ratio:.1%} >= threshold {self.threshold:.1%} "
                f"({current_tokens}/{max_context_tokens} tokens, {remaining_ratio:.1%} remaining)",
            )

        return False, ""
