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

"""Configuration schema for context compaction middleware."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, model_validator


class CompactionConfig(BaseModel):
    """Parses and validates the flat YAML configuration."""

    # General Settings
    max_context_tokens: int = 128000
    auto_compact: bool = True
    emergency_compact_enabled: bool = True
    threshold: float = 0.75

    # Strategy Selection
    compaction_strategy: Literal["sliding_window", "tool_result_compaction"] = "tool_result_compaction"

    # Shared Settings (used by both strategies)
    keep_iterations: int = 3  # Number of recent iterations to keep uncompacted
    keep_user_rounds: int = 0  # Number of recent user rounds to keep uncompacted (0 = disabled)

    # Sliding Window Specifics (Optional in YAML, handled by validator)
    summary_model: str | None = None
    summary_base_url: str | None = None
    summary_api_key: str | None = None
    summary_api_type: str = "openai_chat_completion"
    compact_prompt_path: str | None = None
    retry_attempts: int = 3

    @model_validator(mode="after")
    def validate_and_resolve_paths(self) -> "CompactionConfig":
        """Validate strategy dependencies and resolve file paths."""
        # Ensure sliding window has required LLM creds
        if self.compaction_strategy == "sliding_window":
            missing: list[str] = []
            if not self.summary_model:
                missing.append("summary_model")
            if not self.summary_base_url:
                missing.append("summary_base_url")
            if not self.summary_api_key:
                missing.append("summary_api_key")
            if not self.summary_api_type:
                missing.append("summary_api_type")
            if missing:
                raise ValueError(f"Strategy 'sliding_window' requires the following params: {', '.join(missing)}")

        # Resolve compact prompt path
        if self.compact_prompt_path:
            # User provided custom path, use it as-is
            resolved_path = Path(self.compact_prompt_path)
        else:
            # Use default built-in template path
            # This path is relative to the compact_stratigies module
            config_dir = Path(__file__).parent
            prompts_dir = config_dir / "prompts"
            resolved_path = prompts_dir / "compact_prompt.md"

        # Convert resolved path back to string and store it
        self.compact_prompt_path = str(resolved_path)

        return self
