from __future__ import annotations

import logging
import re
from typing import Any

from nexau.archs.main_sub.execution.hooks import BeforeModelHookInput, AfterToolHookInput, HookResult, Middleware
from nexau.core.messages import Message, Role, TextBlock

STATE_KEY = "execution_risk_hints_state"
logger = logging.getLogger(__name__)

LOCALHOST_CHECK_RE = re.compile(r"https?://(?:127\.0\.0\.1|localhost|0\.0\.0\.0)(?::\d+)?/", re.IGNORECASE)
VALIDATION_DESC_RE = re.compile(
    r"\b(?:validate|validation|verify|verification|acceptance|final|evaluator(?:-style)?|check|smoke test|layout)\b",
    re.IGNORECASE,
)
HELP_OR_SYNTAX_RE = re.compile(r"\b(?:--help|-h|py_compile|compileall)\b", re.IGNORECASE)
EXISTENCE_ONLY_RE = re.compile(
    r"^\s*(?:pwd|ls|stat|file|which|type|readlink|test\s+-[efsd]|wc\s+-[clmw]|head|tail|sed\s+-n|grep|find)\b",
    re.IGNORECASE,
)
ENV_PROBE_RE = re.compile(
    r"\b(?:command\s+-v|which\b|type\b|python\d*\s+-c|python\d*\s+--version|python\d*\s+-V|pip\b|uv\b|apt-get\b|dnf\b|apk\b)\b",
    re.IGNORECASE,
)
MISSING_DEP_RE = re.compile(
    r"(?:\bcommand not found\b|\bNo module named\b|\bModuleNotFoundError\b|\bnot installed\b|\bnot found\b|\bR_MISSING\b)",
    re.IGNORECASE,
)
INLINE_HELPER_RE = re.compile(r"python\d*\s+-\s*<<|cat\s+>\s+/tmp/|/tmp/[^\s;&|]+\.(?:py|sh|rb|pl)", re.IGNORECASE)
BENCHMARK_RE = re.compile(
    r"\b(?:benchmark|runtime|latency|throughput|median|speedup|elapsed|qps|fps|orig_seconds|cand_seconds|query\s+plan)\b",
    re.IGNORECASE,
)
REFERENCE_RE = re.compile(r"\b(?:golden|baseline|reference|threshold|1\.05|ratio|target)\b", re.IGNORECASE)
SPEEDUP_RE = re.compile(r"\bspeedup(?:_solution_vs_golden)?(?:\s*[:=]\s*|\s+)(\d+(?:\.\d+)?)\b", re.IGNORECASE)
THRESHOLD_RE = re.compile(r"\b(?:required_speedup|threshold|target|ratio)(?:\s*[:=]\s*|\s+)(\d+(?:\.\d+)?)\b", re.IGNORECASE)
LOW_LEVEL_MODEL_API_RE = re.compile(
    r"\b(?:SentenceTransformer|AutoModel|forward\.[A-Za-z_]\w*|state_dict\()\b"
)
OFFICIAL_WRAPPER_RE = re.compile(r"\b(?:mteb|PromptType|wrapper|benchmark harness|official)\b", re.IGNORECASE)
SEMANTIC_RUN_RE = re.compile(
    r"\b(?:pytest|curl\s+https?://|Rscript\b|python\d*\s+[^-\s][^;&|]*\.(?:py|R)|cmp\b|diff\b|sqlite3\b|psql\b|cargo\s+test|go\s+test|npm\s+test|make\s+test)\b",
    re.IGNORECASE,
)
CLEAN_LAYOUT_CONTRACT_RE = re.compile(
    r"\b(?:single[- ]file|single file|exactly one file|contained in a single file|must only contain|should contain only|only\s+[A-Za-z0-9_.-]+\s+exists)\b",
    re.IGNORECASE,
)
WRAPPER_CONTRACT_RE = re.compile(
    r"\b(?:mteb|official wrapper|benchmark wrapper|installed package|revision\s+[0-9a-f]{7,}|PromptType)\b",
    re.IGNORECASE,
)
RAW_WRAPPER_BYPASS_RE = re.compile(r"\b(?:SentenceTransformer|AutoModel|AutoTokenizer)\b")
OFFICIAL_WRAPPER_USE_RE = re.compile(
    r"\b(?:mteb|PromptType|use_instructions|query[_ -]?prompt|instruction(?:s)?|task[_ -]?type|encode_queries|wrapper)\b",
    re.IGNORECASE,
)
LIVE_TREE_BUILD_OUTPUT_RE = re.compile(
    r"\b(?:gcc|g\+\+|clang|clang\+\+|cc)\b[^\n;|&]*\s-o\s+((?:/app|/srv|/git|\./|\.\./)[^\s;&|]+)",
    re.IGNORECASE,
)
LIVE_TREE_RUSTC_RE = re.compile(
    r"\brustc\b[^\n;|&]*\s((?:/app|/srv|/git|\./|\.\./)[^\s;&|]+\.rs)(?:\s|$)",
    re.IGNORECASE,
)
POST_SUCCESS_GUARD_RE = re.compile(
    r"(?:POST_SUCCESS_STATE_GUARD|Blocked a potentially destructive or state-mutating post-validation command)",
    re.IGNORECASE,
)
NON_FAILFAST_ASSERT_RE = re.compile(r"\b(?:diff\b|cmp\b|grep\s+-q\b|test\s+-[efsd]\b)", re.IGNORECASE)
FAILFAST_GUARD_RE = re.compile(
    r"(?:^|[\n;])\s*set\s+-e(?:\s|$|u|o|x)|\|\|\s*(?:exit|return)\b|trap\s+['\"][^'\"]*exit",
    re.IGNORECASE,
)
SUCCESS_SENTINEL_RE = re.compile(
    r"\b(?:all (?:diff )?checks passed|acceptance(?:_ok)?|validation passed|final check passed)\b",
    re.IGNORECASE,
)
MULTISTEP_SHELL_RE = re.compile(r"(?:\n|;|\bfor\b|\bwhile\b|\buntil\b)", re.IGNORECASE)
NONCANONICAL_ACCEPTANCE_RE = re.compile(
    r"(?:\._release|/_release/|LD_LIBRARY_PATH=|/lib(?:64)?/ld-linux[^\s]*|ld-linux[^\s]*)",
    re.IGNORECASE,
)


class ExecutionRiskHintsMiddleware(Middleware):
    """Adds sequence-level shell execution hints for recurring failure patterns."""

    def __init__(
        self,
        *,
        long_step_ms: int = 120_000,
        total_long_ms_warning: int = 420_000,
        max_notes_per_call: int = 2,
    ) -> None:
        self.long_step_ms = long_step_ms
        self.total_long_ms_warning = total_long_ms_warning
        self.max_notes_per_call = max_notes_per_call

    def before_model(self, hook_input: BeforeModelHookInput) -> HookResult:
        state = self._load_state(hook_input.agent_state.get_global_value(STATE_KEY, {}))
        self._infer_contract_flags(state, hook_input.messages)

        pending_notes = [note for note in state.get("pending_framework_notes", []) if isinstance(note, str) and note.strip()]
        state["pending_framework_notes"] = []
        hook_input.agent_state.set_global_value(STATE_KEY, state)

        has_assistant = any(getattr(message, "role", None) == Role.ASSISTANT for message in hook_input.messages)
        if not has_assistant or not pending_notes:
            return HookResult.no_changes()

        reminder_lines = ["Active execution risks to resolve before the next step:"]
        for note in pending_notes[: self.max_notes_per_call]:
            reminder_lines.append(f"- {note}")
        reminder = "\n".join(reminder_lines)

        updated_messages = list(hook_input.messages)
        updated_messages.append(Message(role=Role.FRAMEWORK, content=[TextBlock(text=reminder)]))
        logger.info("[ExecutionRiskHintsMiddleware] Surfaced %d pending risk note(s) before model call", len(pending_notes))
        return HookResult.with_modifications(messages=updated_messages)

    def after_tool(self, hook_input: AfterToolHookInput) -> HookResult:
        if hook_input.tool_name != "run_shell_command":
            return HookResult.no_changes()

        tool_input = hook_input.tool_input if isinstance(hook_input.tool_input, dict) else {}
        command = str(tool_input.get("command", ""))
        description = str(tool_input.get("description", "") or "")

        state = self._load_state(hook_input.agent_state.get_global_value(STATE_KEY, {}))
        tool_output = hook_input.tool_output
        content = self._extract_content(tool_output)
        exit_code = self._extract_exit_code(tool_output)
        duration_ms = self._extract_duration_ms(tool_output)
        timed_out = self._looks_timed_out(content, exit_code)

        if duration_ms >= self.long_step_ms or timed_out:
            state["long_steps"] += 1
            state["long_ms"] += max(duration_ms, 0)
        if timed_out:
            state["timeouts"] += 1

        if self._looks_semantic_run(command, description):
            state["semantic_validation_hits"] += 1

        shallow_validation = self._looks_shallow_validation(command, description)
        if shallow_validation:
            state["shallow_validation_hits"] += 1

        if self._looks_like_dependency_probe(command, description, content, exit_code):
            state["dependency_probe_failures"] += 1
        else:
            state["dependency_probe_failures"] = 0

        signature = self._error_signature(content, exit_code)
        if signature:
            if signature == state.get("last_error_signature"):
                state["same_error_repeats"] += 1
            else:
                state["last_error_signature"] = signature
                state["same_error_repeats"] = 1
        else:
            state["last_error_signature"] = ""
            state["same_error_repeats"] = 0

        candidates: list[tuple[int, str, str]] = []

        if state["same_error_repeats"] >= 2:
            candidates.append(
                (
                    60,
                    "retry",
                    "The same error family just repeated. Stop retrying the same command pattern; inspect the contract/path/tool signature once, then switch strategy.",
                )
            )

        if state["dependency_probe_failures"] >= 2:
            candidates.append(
                (
                    58,
                    "missing_dependency",
                    "A key runtime/dependency probe just failed again. Stop re-checking the environment; move to the best direct implementation or the cheapest bounded fallback and keep validation lightweight.",
                )
            )

        if self._looks_like_post_success_guard(content):
            candidates.append(
                (
                    57,
                    "post_success_guard",
                    "The publish-state guard just fired. Treat the last evaluator-style passing state as the live candidate: stop cleanup/replay loops, continue only in /tmp or a copied scratch tree, and come back to the live deliverable only if new failing evidence forces one bounded change.",
                )
            )

        if state["long_steps"] >= 2 and (
            state["timeouts"] >= 1 or state["long_ms"] >= self.total_long_ms_warning
        ):
            seconds = int(round(state["long_ms"] / 1000))
            candidates.append(
                (
                    55,
                    "budget",
                    f"Budget warning: you have already spent about {seconds}s across long foreground runs/timeouts. Stop starting new heavy searches; use background jobs with short polls or finalize the best verified candidate now.",
                )
            )

        if LOCALHOST_CHECK_RE.search(command):
            candidates.append(
                (
                    50,
                    "localhost",
                    "A localhost/127.0.0.1 check only proves local reachability. Mirror the evaluator's exact public host/path/port and final deployed state before publishing.",
                )
            )

        if self._looks_like_non_failfast_validation(command, description):
            candidates.append(
                (
                    46,
                    "failfast_validation",
                    "This validation script chains diff/cmp-style assertions inside a multi-step shell block without an explicit fail-fast guard. One hidden mismatch can still be followed by a misleading 'passed' line and exit 0. Add `set -e` or explicit `|| exit 1`, or run the assertions separately, before trusting the result.",
                )
            )

        if shallow_validation and (
            VALIDATION_DESC_RE.search(description) or state["shallow_validation_hits"] >= 2
        ) and state["semantic_validation_hits"] == 0:
            candidates.append(
                (
                    45,
                    "shallow",
                    "This was only help/syntax/file-existence validation. Do not treat it as final proof; run the real entry point with evaluator-style args and confirm the exact output path/public endpoint/forbidden extras from disk.",
                )
            )

        if self._looks_like_noncanonical_acceptance(command, description):
            candidates.append(
                (
                    44,
                    "noncanonical_acceptance",
                    "This acceptance check depends on a hidden build dir or loader/library-path indirection. Treat that as a debugging signal only; before publishing, rerun from the canonical public binary/script/layout the evaluator will call, with minimal extra environment tweaks.",
                )
            )

        if self._looks_like_low_level_model_check(command, description):
            candidates.append(
                (
                    40,
                    "wrapper",
                    "This check is relying on a lower-level model API or exposed internal state. If the task names a benchmark wrapper, revision, or black-box interface, revalidate through that exact interface before publishing.",
                )
            )

        if self._looks_proxy_validation(command, description):
            candidates.append(
                (
                    35,
                    "proxy_validation",
                    "This validation is using an inline/self-written helper. Generator and validator can share the same wrong assumptions. Before publishing, reread the final artifact from disk and cross-check it against the named official tool/wrapper/baseline or another independent evaluator-style path instead of validating only design-time assumptions.",
                )
            )

        if state["clean_layout_contract"] and self._writes_live_tree_build_output(command):
            candidates.append(
                (
                    43,
                    "clean_layout",
                    "This task's contract says the deliverable tree must stay clean/single-file, but this command built artifacts directly in the live tree. Compile or run in /tmp, or delete explicit extras in the deliverable directory, then rerun the exact layout check before publishing.",
                )
            )

        if state["wrapper_contract"] and self._looks_like_raw_wrapper_bypass(command, description, content):
            candidates.append(
                (
                    42,
                    "wrapper_contract",
                    "The task named an official package/wrapper/revision. Raw SentenceTransformer/AutoModel calls can miss wrapper metadata such as task prompts or normalization. Recompute via the named package/wrapper, or explicitly mirror its documented prompt/task-type settings, before publishing.",
                )
            )

        if BENCHMARK_RE.search("\n".join(part for part in (description, command, content) if part)) and not REFERENCE_RE.search(
            "\n".join(part for part in (description, command, content) if part)
        ):
            candidates.append(
                (
                    30,
                    "benchmark_reference",
                    "A speedup against an arbitrary comparator is not enough. If the task defines a golden/reference baseline or numeric threshold, benchmark against that exact comparator under the same setup and decide by the required median/threshold.",
                )
            )

        if self._has_thin_benchmark_margin(description, command, content):
            candidates.append(
                (
                    28,
                    "benchmark_margin",
                    "This benchmark margin is still thin for a noisy performance gate. Do not publish on one near-threshold win; repeat alternating runs and keep optimizing until the slowest/median result has comfortable headroom over the requirement.",
                )
            )

        notes: list[str] = []
        for _, key, note in sorted(candidates, reverse=True):
            if state["emitted"].get(key):
                continue
            state["emitted"][key] = 1
            notes.append(note)
            if len(notes) >= self.max_notes_per_call:
                break

        if notes:
            pending = [existing for existing in state.get("pending_framework_notes", []) if isinstance(existing, str)]
            for note in notes:
                if note not in pending:
                    pending.append(note)
            state["pending_framework_notes"] = pending[: self.max_notes_per_call]

        hook_input.agent_state.set_global_value(STATE_KEY, state)

        if not notes:
            return HookResult.no_changes()

        return HookResult.with_modifications(tool_output=self._append_notes(tool_output, notes))

    def _load_state(self, raw_state: Any) -> dict[str, Any]:
        state = raw_state if isinstance(raw_state, dict) else {}
        emitted = state.get("emitted")
        pending_framework_notes = state.get("pending_framework_notes")
        return {
            "long_steps": int(state.get("long_steps", 0) or 0),
            "long_ms": int(state.get("long_ms", 0) or 0),
            "timeouts": int(state.get("timeouts", 0) or 0),
            "same_error_repeats": int(state.get("same_error_repeats", 0) or 0),
            "last_error_signature": str(state.get("last_error_signature", "") or ""),
            "dependency_probe_failures": int(state.get("dependency_probe_failures", 0) or 0),
            "shallow_validation_hits": int(state.get("shallow_validation_hits", 0) or 0),
            "semantic_validation_hits": int(state.get("semantic_validation_hits", 0) or 0),
            "contract_flags_inferred": bool(state.get("contract_flags_inferred", False)),
            "clean_layout_contract": bool(state.get("clean_layout_contract", False)),
            "wrapper_contract": bool(state.get("wrapper_contract", False)),
            "pending_framework_notes": [str(item) for item in pending_framework_notes] if isinstance(pending_framework_notes, list) else [],
            "emitted": emitted if isinstance(emitted, dict) else {},
        }

    def _infer_contract_flags(self, state: dict[str, Any], messages: list[Any]) -> None:
        if state.get("contract_flags_inferred"):
            return

        user_text = self._extract_user_text(messages)
        state["clean_layout_contract"] = bool(CLEAN_LAYOUT_CONTRACT_RE.search(user_text))
        state["wrapper_contract"] = bool(WRAPPER_CONTRACT_RE.search(user_text))
        state["contract_flags_inferred"] = True

    def _extract_user_text(self, messages: list[Any]) -> str:
        parts: list[str] = []
        for message in messages:
            if getattr(message, "role", None) != Role.USER:
                continue
            text = self._message_to_text(message)
            if text:
                parts.append(text)
        return "\n".join(parts)

    def _message_to_text(self, message: Any) -> str:
        content = getattr(message, "content", None)
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return str(content or "")

        blocks: list[str] = []
        for block in content:
            if isinstance(block, TextBlock):
                blocks.append(block.text)
                continue
            text = getattr(block, "text", None)
            if isinstance(text, str):
                blocks.append(text)
        return "\n".join(part for part in blocks if part)

    def _writes_live_tree_build_output(self, command: str) -> bool:
        if LIVE_TREE_BUILD_OUTPUT_RE.search(command):
            return True

        rust_match = LIVE_TREE_RUSTC_RE.search(command)
        if not rust_match:
            return False

        source_path = rust_match.group(1)
        return not source_path.startswith("/tmp/")

    def _looks_like_raw_wrapper_bypass(self, command: str, description: str, content: str) -> bool:
        combined = "\n".join(part for part in (description, command, content) if part)
        return bool(RAW_WRAPPER_BYPASS_RE.search(combined) and not OFFICIAL_WRAPPER_USE_RE.search(combined))

    def _extract_content(self, tool_output: Any) -> str:
        if isinstance(tool_output, dict):
            content = tool_output.get("content", "")
            return content if isinstance(content, str) else str(content)
        return tool_output if isinstance(tool_output, str) else str(tool_output)

    def _extract_exit_code(self, tool_output: Any) -> int | None:
        if isinstance(tool_output, dict):
            value = tool_output.get("exit_code")
            if isinstance(value, int):
                return value
        return None

    def _extract_duration_ms(self, tool_output: Any) -> int:
        if isinstance(tool_output, dict):
            value = tool_output.get("duration_ms")
            if isinstance(value, int):
                return value
        return 0

    def _looks_timed_out(self, content: str, exit_code: int | None) -> bool:
        return content.startswith("Timeout:") or "timed out after" in content or exit_code == -15

    def _looks_semantic_run(self, command: str, description: str) -> bool:
        combined = "\n".join(part for part in (description, command) if part)
        return bool(SEMANTIC_RUN_RE.search(combined))

    def _looks_shallow_validation(self, command: str, description: str) -> bool:
        command = command.strip()
        if HELP_OR_SYNTAX_RE.search(command):
            return True
        if VALIDATION_DESC_RE.search(description) and EXISTENCE_ONLY_RE.search(command):
            return True
        return False

    def _looks_like_post_success_guard(self, content: str) -> bool:
        return bool(POST_SUCCESS_GUARD_RE.search(content))

    def _looks_like_non_failfast_validation(self, command: str, description: str) -> bool:
        combined = "\n".join(part for part in (description, command) if part)
        if not VALIDATION_DESC_RE.search(combined):
            return False
        if not NON_FAILFAST_ASSERT_RE.search(command):
            return False
        if FAILFAST_GUARD_RE.search(command):
            return False
        if not MULTISTEP_SHELL_RE.search(command):
            return False
        return bool(SUCCESS_SENTINEL_RE.search(command) or "printf" in command or "echo" in command)

    def _looks_proxy_validation(self, command: str, description: str) -> bool:
        if not VALIDATION_DESC_RE.search(description):
            return False
        return bool(INLINE_HELPER_RE.search(command))

    def _looks_like_noncanonical_acceptance(self, command: str, description: str) -> bool:
        combined = "\n".join(part for part in (description, command) if part)
        if not VALIDATION_DESC_RE.search(combined):
            return False
        return bool(NONCANONICAL_ACCEPTANCE_RE.search(command))

    def _looks_like_low_level_model_check(self, command: str, description: str) -> bool:
        combined = "\n".join(part for part in (description, command) if part)
        if not VALIDATION_DESC_RE.search(combined):
            return False
        return bool(LOW_LEVEL_MODEL_API_RE.search(command) and not OFFICIAL_WRAPPER_RE.search(command))

    def _looks_like_dependency_probe(
        self,
        command: str,
        description: str,
        content: str,
        exit_code: int | None,
    ) -> bool:
        combined = "\n".join(part for part in (description, command) if part)
        if exit_code in (None, 0) and not MISSING_DEP_RE.search(content):
            return False
        if not (ENV_PROBE_RE.search(combined) or EXISTENCE_ONLY_RE.search(command) or HELP_OR_SYNTAX_RE.search(command)):
            return False
        return bool(MISSING_DEP_RE.search(content))

    def _has_thin_benchmark_margin(self, description: str, command: str, content: str) -> bool:
        combined = "\n".join(part for part in (description, command, content) if part)
        if not BENCHMARK_RE.search(combined):
            return False

        speedups = [float(match.group(1)) for match in SPEEDUP_RE.finditer(combined)]
        if not speedups:
            return False

        strongest = max(speedups)
        thresholds = [float(match.group(1)) for match in THRESHOLD_RE.finditer(combined) if float(match.group(1)) > 0]
        if thresholds:
            threshold = max(thresholds)
            return strongest <= max(threshold * 1.1, threshold + 0.15)
        return 1.0 <= strongest <= 1.3

    def _error_signature(self, content: str, exit_code: int | None) -> str:
        if exit_code in (None, 0) and not self._looks_timed_out(content, exit_code):
            return ""
        for line in content.splitlines():
            cleaned = line.strip()
            if not cleaned:
                continue
            if cleaned.startswith(("Error:", "Timeout:", "Traceback", "AssertionError", "FileNotFoundError", "ModuleNotFoundError")):
                return cleaned[:180]
        return f"exit:{exit_code}"

    def _append_notes(self, tool_output: Any, notes: list[str]) -> Any:
        if isinstance(tool_output, dict):
            updated = dict(tool_output)
            existing = self._extract_content(tool_output)
            new_lines = [
                f"Execution note: {note}"
                for note in notes
                if f"Execution note: {note}" not in existing
            ]
            if not new_lines:
                return updated
            updated["content"] = existing + ("\n" if existing else "") + "\n".join(new_lines)
            return updated

        existing = tool_output if isinstance(tool_output, str) else str(tool_output)
        new_lines = [
            f"Execution note: {note}" for note in notes if f"Execution note: {note}" not in existing
        ]
        if not new_lines:
            return tool_output
        return existing + ("\n" if existing else "") + "\n".join(new_lines)
