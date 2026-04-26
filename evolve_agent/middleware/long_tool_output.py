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

"""Middleware that truncates long tool outputs and saves full content to temp files.

When a tool returns output whose serialized text exceeds a configurable character
threshold, this middleware:

1. Truncates the output, keeping the longer of:
   - the first ``head_lines`` and last ``tail_lines`` lines
   - the first ``head_chars`` and last ``tail_chars`` characters
2. Writes the **full** output to a temporary file under ``temp_dir`` via the
   Sandbox API (``sandbox.write_file``).  This ensures the file is written to
   the correct environment regardless of whether the agent runs locally or
   inside a remote sandbox (e.g. E2B).
3. Replaces the original tool output with the truncated version plus a
   human-/LLM-readable note pointing to the temp file so the model can
   ``read_file`` it if needed.

Configuration example (YAML agent config)::

    middlewares:
      - import: nexau.archs.main_sub.execution.middleware.long_tool_output:LongToolOutputMiddleware
        params:
          max_output_chars: 10000
          head_lines: 50
          tail_lines: 30
          temp_dir: /tmp/nexau_tool_outputs
          bypass_tool_names:
            - execute_bash
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Sequence
from typing import cast

from nexau.archs.sandbox.base_sandbox import BaseSandbox, SandboxStatus

from nexau.archs.main_sub.execution.hooks import AfterToolHookInput, HookResult, Middleware

logger = logging.getLogger(__name__)

# Defaults ------------------------------------------------------------------

_DEFAULT_MAX_OUTPUT_CHARS = 10_000
"""Character threshold above which truncation kicks in."""

_DEFAULT_HEAD_LINES = 50
"""Number of lines to keep from the beginning of the output."""

_DEFAULT_TAIL_LINES = 30
"""Number of lines to keep from the end of the output."""

_DEFAULT_HEAD_CHARS = 5000
"""Number of characters to keep from the beginning of the output."""

_DEFAULT_TAIL_CHARS = 5000
"""Number of characters to keep from the end of the output."""

_DEFAULT_TEMP_DIR = "/tmp/nexau_tool_outputs"
"""Default directory for persisted full outputs."""


class LongToolOutputMiddleware(Middleware):
    """Truncates oversized tool outputs and persists the full version to disk.

    The middleware operates in the ``after_tool`` phase.  If the serialized
    tool output (JSON string) is shorter than *max_output_chars* the output
    passes through unmodified.

    File writes go through the abstract :class:`BaseSandbox` API so the
    middleware works transparently with both local and remote (e.g. E2B)
    sandboxes.

    Args:
        max_output_chars: Character count threshold that triggers truncation.
        head_lines: Number of leading lines to retain in the truncated view.
        tail_lines: Number of trailing lines to retain in the truncated view.
        head_chars: Number of leading characters to retain in the truncated
            view.
        tail_chars: Number of trailing characters to retain in the truncated
            view.
        temp_dir: Absolute directory path where full outputs are written.
            Defaults to ``/tmp/nexau_tool_outputs``.  Set to ``None`` to
            disable file persistence (truncation still applies, but no file
            is saved and no ``read_file`` hint is emitted).
        bypass_tool_names: Tool names whose output should never be truncated
            by this middleware.  Useful for tools that already perform their
            own truncation (e.g. ``execute_bash``).
    """

    def __init__(
        self,
        *,
        max_output_chars: int = _DEFAULT_MAX_OUTPUT_CHARS,
        head_lines: int = _DEFAULT_HEAD_LINES,
        tail_lines: int = _DEFAULT_TAIL_LINES,
        head_chars: int = _DEFAULT_HEAD_CHARS,
        tail_chars: int = _DEFAULT_TAIL_CHARS,
        temp_dir: str | None = _DEFAULT_TEMP_DIR,
        bypass_tool_names: Sequence[str] | None = None,
    ) -> None:
        if max_output_chars < 1:
            raise ValueError("max_output_chars must be >= 1")
        if head_lines < 0:
            raise ValueError("head_lines must be >= 0")
        if tail_lines < 0:
            raise ValueError("tail_lines must be >= 0")
        if head_chars < 0:
            raise ValueError("head_chars must be >= 0")
        if tail_chars < 0:
            raise ValueError("tail_chars must be >= 0")
        if head_chars + tail_chars > max_output_chars:
            raise ValueError("head_chars + tail_chars must be <= max_output_chars")

        self.max_output_chars = max_output_chars
        self.head_lines = head_lines
        self.tail_lines = tail_lines
        self.head_chars = head_chars
        self.tail_chars = tail_chars
        self.temp_dir = temp_dir
        self._bypass_tool_names: frozenset[str] = frozenset(bypass_tool_names or ())

    # ------------------------------------------------------------------
    # Middleware hook
    # ------------------------------------------------------------------

    def after_tool(self, hook_input: AfterToolHookInput) -> HookResult:
        """Inspect tool output; truncate and persist if too long."""

        # Bypass — tools that already handle their own truncation
        if hook_input.tool_name in self._bypass_tool_names:
            return HookResult.no_changes()

        output = hook_input.tool_output
        sandbox = hook_input.sandbox

        # For dict outputs with a known text field, measure that field directly
        # so line-based truncation operates on the actual text content rather
        # than the JSON serialization of the whole dict.
        content_key, content_text = self._extract_content_text(output)

        # Exclude returnDisplay from length measurement — it is a display-only
        # field stripped by the tool executor before being sent to the LLM, so
        # it should not inflate the size check or trigger unnecessary truncation.
        output_for_measurement: object = output
        if isinstance(output_for_measurement, dict) and "returnDisplay" in output_for_measurement:
            output_for_measurement = {k: v for k, v in cast("dict[str, object]", output_for_measurement).items() if k != "returnDisplay"}
        output_text = self._serialize(cast("object", output_for_measurement))

        if len(output_text) <= self.max_output_chars:
            return HookResult.no_changes()

        # Decide what text to truncate: the inner content field if available,
        # otherwise the full serialized representation.
        text_to_truncate = content_text if content_text is not None else output_text
        text_for_stats = content_text if content_text is not None else output_text

        # ---- Truncate ------------------------------------------------
        truncated_text = self._truncate(text_to_truncate)

        # ---- Persist full output to temp file via Sandbox API --------
        saved_path: str | None = None
        if self.temp_dir is not None:
            saved_path = self._save_to_temp_file(
                sandbox=sandbox,
                tool_name=hook_input.tool_name,
                tool_call_id=hook_input.tool_call_id,
                full_text=output_text,
            )

        # ---- Build replacement output --------------------------------
        total_chars = len(text_for_stats)
        total_lines = text_for_stats.count("\n") + 1

        if saved_path is not None:
            hint = (
                f"\n\n⚠️ [LongToolOutputMiddleware] The full output ({total_chars:,} chars, ~{total_lines} lines) "
                f"has been truncated. The complete output has been saved to:\n"
                f"  {saved_path}\n"
                f"Use the read file tool to view the full content if needed."
            )
        else:
            hint = f"\n\n⚠️ [LongToolOutputMiddleware] The full output ({total_chars:,} chars, ~{total_lines} lines) has been truncated."

        new_output = self._build_truncated_output(output, truncated_text, hint, content_key)

        logger.info(
            "[LongToolOutputMiddleware] Truncated output for tool '%s' (%d chars -> %d chars). Full output saved to: %s",
            hook_input.tool_name,
            total_chars,
            len(truncated_text) + len(hint),
            saved_path,
        )

        return HookResult.with_modifications(tool_output=new_output)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_content_text(output: object) -> tuple[str | None, str | None]:
        """Extract the primary text field from a dict-style tool output.

        Returns a ``(key, text)`` tuple.  If *output* is not a dict or does
        not contain a known text key (``content`` or ``result``), both values
        are ``None``.
        """

        if not isinstance(output, dict):
            return None, None

        output_dict = cast("dict[str, object]", output)
        for key in ("content", "result"):
            val = output_dict.get(key)
            if val is not None and isinstance(val, str):
                return key, val

        return None, None

    @staticmethod
    def _serialize(output: object) -> str:
        """Convert tool output to a string for length measurement."""

        if isinstance(output, str):
            return output
        try:
            return json.dumps(output, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            return str(output)

    def _truncate(self, text: str) -> str:
        """Keep the longer of the configured line- or char-based truncations."""

        candidates: list[str] = []

        line_candidate = self._truncate_by_lines(text)
        if line_candidate is not None:
            candidates.append(line_candidate)

        char_candidate = self._truncate_by_chars(text)
        if char_candidate is not None:
            candidates.append(char_candidate)

        if not candidates:
            return text

        truncated_candidates = [candidate for candidate in candidates if len(candidate) < len(text)]
        if truncated_candidates:
            candidates = truncated_candidates

        valid_candidates = [candidate for candidate in candidates if len(candidate) < self.max_output_chars]
        if valid_candidates:
            return max(valid_candidates, key=len)

        if len(candidates) == 1:
            return candidates[0]

        return min(candidates, key=len)

    def _truncate_by_lines(self, text: str) -> str | None:
        """Keep the first *head_lines* and last *tail_lines* lines."""

        if self.head_lines == 0 and self.tail_lines == 0:
            return None

        lines = text.splitlines(keepends=True)
        total = len(lines)

        # If lines fit within head + tail, no truncation needed (caller
        # already checked char length, but lines may be very long).
        if total <= self.head_lines + self.tail_lines:
            return text

        head = lines[: self.head_lines]
        tail = lines[-self.tail_lines :] if self.tail_lines > 0 else []
        omitted = total - self.head_lines - self.tail_lines
        separator = f"\n... [{omitted} lines omitted] ...\n"

        return "".join(head) + separator + "".join(tail)

    def _truncate_by_chars(self, text: str) -> str | None:
        """Keep the first *head_chars* and last *tail_chars* characters."""

        if self.head_chars == 0 and self.tail_chars == 0:
            return None

        if len(text) <= self.head_chars + self.tail_chars:
            return text

        head = text[: self.head_chars]
        tail = text[len(text) - self.tail_chars :] if self.tail_chars > 0 else ""
        omitted = len(text) - self.head_chars - self.tail_chars
        separator = f"\n... [{omitted} chars omitted] ...\n"

        return head + separator + tail

    def _save_to_temp_file(
        self,
        *,
        sandbox: BaseSandbox | None,
        tool_name: str,
        tool_call_id: str,
        full_text: str,
    ) -> str:
        """Write *full_text* to a temp file via Sandbox API and return its path.

        Uses ``sandbox.write_file`` (with ``create_directories=True``) so the
        file ends up in the correct environment (local filesystem or remote
        sandbox).  If no sandbox is available, raises a ``RuntimeError``.
        """

        # Build a human-friendly filename
        safe_tool = tool_name.replace(os.sep, "_").replace(" ", "_")
        timestamp = int(time.time() * 1000)
        short_id = tool_call_id[-8:] if len(tool_call_id) > 8 else tool_call_id
        filename = f"{safe_tool}_{short_id}_{timestamp}.txt"
        filepath = f"{self.temp_dir}/{filename}"

        if sandbox is None:
            raise RuntimeError(
                "[LongToolOutputMiddleware] No sandbox available to write temp file. The middleware requires a sandbox to be configured."
            )

        result = sandbox.write_file(
            file_path=filepath,
            content=full_text,
            encoding="utf-8",
            create_directories=True,
        )

        if result.status != SandboxStatus.SUCCESS:
            raise RuntimeError(f"[LongToolOutputMiddleware] Failed to write temp file via sandbox: {result.error or 'unknown error'}")

        return filepath

    @staticmethod
    def _build_truncated_output(
        original_output: object,
        truncated_text: str,
        hint: str,
        content_key: str | None = None,
    ) -> object:
        """Construct the replacement tool output.

        Args:
            original_output: The raw tool output.
            truncated_text: Line-truncated version of the primary text.
            hint: Human-/LLM-readable note about the temp file.
            content_key: If the output is a dict whose text field was
                extracted earlier, this is the key name (``"content"`` or
                ``"result"``).  When provided the replacement targets that
                key directly; other keys are preserved.

        Returns:
            The modified output with truncated text + hint.
        """

        if isinstance(original_output, dict):
            result: dict[str, object] = dict(cast("dict[str, object]", original_output))
            if content_key is not None:
                # We already know which field holds the text
                result[content_key] = truncated_text + hint
            elif "content" in result:
                result["content"] = truncated_text + hint
            elif "result" in result:
                result["result"] = truncated_text + hint
            else:
                # Fallback: serialise all keys, truncate, add hint
                result = {"content": truncated_text + hint}
            return result
        else:
            return truncated_text + hint
