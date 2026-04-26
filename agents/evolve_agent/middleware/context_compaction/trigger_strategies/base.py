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

"""Base protocol for trigger strategies."""

from typing import Protocol

from nexau.core.messages import Message


class TriggerStrategy(Protocol):
    """Protocol for trigger strategies that determine when to compact."""

    def should_compact(
        self,
        messages: list[Message],
        current_tokens: int,
        max_context_tokens: int,
    ) -> tuple[bool, str]:
        """Check if compaction should be triggered.

        Args:
            messages: Current message history
            current_tokens: Current token count
            max_context_tokens: Maximum context window size

        Returns:
            Tuple of (should_compact, reason_string)
        """
        ...
