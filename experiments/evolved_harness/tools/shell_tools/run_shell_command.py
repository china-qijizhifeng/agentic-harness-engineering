# Copyright 2025 Google LLC
# SPDX-License-Identifier: Apache-2.0
"""
run_shell_command tool (shell) - Executes shell commands.

Based on gemini-cli's shell.ts implementation.
Supports foreground and background execution, timeout handling, and process management.
"""

import posixpath
import re
import shlex
import time
from collections.abc import Callable
from typing import Any

from nexau.archs.main_sub.agent_state import AgentState
from nexau.archs.sandbox import BaseSandbox, SandboxStatus
from nexau.archs.tool.builtin._sandbox_utils import get_sandbox, resolve_path

# Configuration constants (matching gemini-cli)
DEFAULT_TIMEOUT_MS = 300000  # 5 minutes default timeout
TRUNCATE_OUTPUT_THRESHOLD = 4_000_000  # Truncate when output exceeds this many chars
TRUNCATE_OUTPUT_LINES = 1000  # Keep last N lines when truncating
MAX_TRUNCATED_LINE_WIDTH = 1000  # Max chars per line in truncated output
MAX_TRUNCATED_CHARS = 4000  # Keep last N chars for single massive line
LONG_STEP_NOTE_THRESHOLD_MS = 90_000
PUBLISH_GUARD_KEY = "run_shell_command_publish_guard"
POST_SUCCESS_OVERRIDE_TOKEN = "ALLOW_POST_SUCCESS_RESET"

SUCCESS_SIGNAL_RE = re.compile(
    r"(VALIDATION_OK|acceptance check passed|exact_match\s*[:=]\s*True|"
    r"matches_expected(?:_design)?\s*[:=]\s*True|diff_exit=0|similarity\s+100(?:\.0+)?|"
    r"EVAL_[A-Z_]+=|HTTP body:\s*<<|curl body =>\s*<<)",
    re.IGNORECASE,
)
SCAN_CLEAN_SIGNAL_RE = re.compile(
    r"(REMAINING_[A-Z_]*HITS=0|WORKTREE_[A-Z_]*HITS=0|\b0 hits\b|\bno hits\b|\bno matches\b|\bclean\b)",
    re.IGNORECASE,
)
BENCHMARK_SIGNAL_RE = re.compile(
    r"\b(candidate\d+|baseline|golden|speedup|median_s|elapsed_ms|QUERY PLAN|runtime|benchmark)\b",
    re.IGNORECASE,
)
STRUCTURED_SOURCE_SIGNAL_RE = re.compile(
    r"\b(Wayback|speedrun|SOLUTION|benchmark page)\b",
    re.IGNORECASE,
)
FINAL_CHECK_RE = re.compile(
    r"\b(final|evaluator(?:-style)?|acceptance|end-to-end|verified workflow|final sweep|final layout|deliverable)\b",
    re.IGNORECASE,
)
TEST_FILE_RE = re.compile(r"\btest\s+-f\s+([^\s;&|]+)")
CMP_FILE_RE = re.compile(r"\bcmp(?:\s+-s)?\s+([^\s;&|]+)\s+([^\s;&|]+)")
CURL_PATH_RE = re.compile(r"\bcurl\b[^\n;|&]*https?://[^\s\"']+/([A-Za-z0-9_.-]+)")
ROOT_URL_ONLY_CURL_RE = re.compile(r"\bcurl\b[^\n;|&]*https?://[^/\s\"']+/?(?:\s|$)", re.IGNORECASE)
FIND_ROOT_RE = re.compile(r"\bfind\s+([^\s;&|]+)\s+[^\n;|&]*\s+-type\s+f\b")
FIND_FILES_ONLY_RE = re.compile(r"\bfind\s+([^\s;&|]+)\s+[^\n;|&]*\s+-type\s+f\b", re.IGNORECASE)
GIT_DIR_RE = re.compile(r"--git-dir=([^\s;&|]+)")
WORK_TREE_RE = re.compile(r"--work-tree=([^\s;&|]+)")
GIT_CLONE_SOURCE_RE = re.compile(r"\bgit\s+clone\s+([^\s;&|]+)")
DESTRUCTIVE_FILE_RM_RE = re.compile(r"\brm\s+-[A-Za-z-]*[fr][A-Za-z-]*", re.IGNORECASE)
DESTRUCTIVE_ROOT_RM_RE = re.compile(r"\brm\s+-[A-Za-z-]*r[A-Za-z-]*", re.IGNORECASE)
DESTRUCTIVE_FIND_DELETE_RE = re.compile(r"\bfind\s+([^\s;&|]+)\s+[^\n;|&]*\s+-delete\b", re.IGNORECASE)
DESTRUCTIVE_GIT_RE = re.compile(
    r"\bgit\b[^\n;|&]*(?:\breset\s+--hard\b|\bclean\b[^\n;|&]*\bf\b|\bcheckout\s+-f\b|\bupdate-ref\s+-d\b|\bbranch\s+-D\b|\binit\b)",
    re.IGNORECASE,
)
POST_SUCCESS_GIT_META_RE = re.compile(
    r"\bgit\b[^\n;|&]*(?:\bcommit\b|\bfilter-branch\b|\brebase\b|\bcherry-pick\b|\bmerge\b|\bamend\b|\bgc\b|\breflog\b|\brepack\b|\bprune\b|\bfsck\b|\bupdate-ref\b)",
    re.IGNORECASE,
)
SCRIPT_ENTRY_RE = re.compile(
    r"\b(?:python\d*(?:\s+-u)?|bash|sh|Rscript|node|perl|ruby)\s+((?:/|\./|\.\./)[^\s;&|]+|[A-Za-z0-9_.-]+\.[A-Za-z0-9_.-]+)",
    re.IGNORECASE,
)
GENERIC_FILE_PATH_RE = re.compile(
    r"(?<![\w./-])((?:/app|/srv|/git|\./|\.\./)[^\s\"'`;|&]+\.[A-Za-z0-9._-]+)"
)


def _truncate_shell_output(content: str) -> str:
    """
    Truncate large shell output, keeping last N lines (matching gemini-cli).
    Applied when content exceeds TRUNCATE_OUTPUT_THRESHOLD.
    """
    if len(content) <= TRUNCATE_OUTPUT_THRESHOLD:
        return content

    lines = content.split("\n")
    total_lines = len(lines)

    if total_lines > 1:
        # Multi-line: show last N lines, truncate long lines
        last_lines = lines[-TRUNCATE_OUTPUT_LINES:]
        processed: list[str] = []
        for line in last_lines:
            if len(line) > MAX_TRUNCATED_LINE_WIDTH:
                processed.append(line[:MAX_TRUNCATED_LINE_WIDTH] + "... [LINE WIDTH TRUNCATED]")
            else:
                processed.append(line)
        return f"Output too large. Showing the last {len(processed)} of {total_lines} lines.\n...\n" + "\n".join(processed)
    else:
        # Single massive line: keep last N chars
        snippet = content[-MAX_TRUNCATED_CHARS:]
        return f"Output too large. Showing the last {MAX_TRUNCATED_CHARS:,} characters of the output.\n...{snippet}"


def _collect_execution_notes(
    *,
    command: str,
    description: str | None,
    output: str,
    exit_code: int | None,
    duration_ms: int,
    timed_out: bool,
) -> list[str]:
    if timed_out:
        return []

    combined_text = "\n".join(part for part in (description or "", command, output) if part)
    validation_text = "\n".join(part for part in (description or "", command) if part)
    final_check_like = bool(FINAL_CHECK_RE.search(validation_text))
    notes: list[str] = []

    def add_note(note: str) -> None:
        if note not in notes:
            notes.append(note)

    if duration_ms >= LONG_STEP_NOTE_THRESHOLD_MS:
        add_note(
            f"This step already consumed about {duration_ms / 1000:.0f}s. Reassess now: keep the cheapest viable path, cap further expensive experiments, and if a candidate already meets the contract, save it to the required target path before doing more exploration."
        )

    if final_check_like and ROOT_URL_ONLY_CURL_RE.search(command):
        add_note(
            "This only checks that the service root responds. If the contract names a specific public path/resource such as `/hello.html`, rerun the final check against that exact path and confirm its final content/status code."
        )

    if final_check_like and FIND_FILES_ONLY_RE.search(command):
        add_note(
            "This layout check only inspects regular files. Hidden directories or cache folders can still pollute the deliverable. Recheck the whole target tree including file and directory entries before publishing."
        )

    if BENCHMARK_SIGNAL_RE.search(combined_text):
        add_note(
            "Performance evidence is noisy. Compare candidate and baseline under the same setup with repeated alternating runs, decide by median/threshold, and stop once one candidate clearly clears the requirement."
        )

    if exit_code == 0 and SUCCESS_SIGNAL_RE.search(output):
        add_note(
            "This looks like acceptance-style or self-check success. Do not edit the deliverable again unless you have new failing evidence. If you do make further changes, rerun the exact final check from the canonical path/public entry point and reread the final artifact literally."
        )

    if exit_code == 0 and SCAN_CLEAN_SIGNAL_RE.search(output):
        add_note(
            "A zero-hit or 'clean' scan only proves that particular scan. Before finishing, independently reread the exact target files/output or compare against canonical placeholders/expected lines instead of validating with the same regex assumptions used to produce the change."
        )

    if STRUCTURED_SOURCE_SIGNAL_RE.search(combined_text):
        add_note(
            "If this structured source already gives you a plausible answer, prefer minimal verification plus delivery over open-ended OCR, reverse engineering, or further expensive reconstruction."
        )

    return notes[:2]


def _clean_shell_token(token: str) -> str:
    cleaned = token.strip().strip("\"'`[](){}")
    while cleaned.endswith((";", ",", ":")):
        cleaned = cleaned[:-1]
    return cleaned


def _extract_publish_guard_targets(command: str) -> tuple[set[str], set[str]]:
    protected_files: set[str] = set()
    protected_roots: set[str] = set()

    for regex in (TEST_FILE_RE,):
        for match in regex.finditer(command):
            candidate = _clean_shell_token(match.group(1))
            if candidate:
                protected_files.add(candidate)

    for match in CMP_FILE_RE.finditer(command):
        for group in match.groups():
            candidate = _clean_shell_token(group)
            if candidate:
                protected_files.add(candidate)

    for match in CURL_PATH_RE.finditer(command):
        candidate = _clean_shell_token(match.group(1))
        if candidate:
            protected_files.add(candidate)

    for regex in (SCRIPT_ENTRY_RE, GENERIC_FILE_PATH_RE):
        for match in regex.finditer(command):
            candidate = _clean_shell_token(match.group(1))
            if candidate and not candidate.startswith("/tmp/"):
                protected_files.add(candidate)

    for regex in (FIND_ROOT_RE, GIT_DIR_RE, WORK_TREE_RE, GIT_CLONE_SOURCE_RE):
        for match in regex.finditer(command):
            candidate = _clean_shell_token(match.group(1))
            if candidate:
                protected_roots.add(candidate)

    return protected_files, protected_roots


def _get_publish_guard(agent_state: AgentState | None) -> dict[str, list[str]]:
    default_guard = {"files": [], "roots": []}
    if agent_state is None:
        return default_guard

    stored = agent_state.get_global_value(PUBLISH_GUARD_KEY, default_guard)
    if not isinstance(stored, dict):
        return default_guard

    files = stored.get("files")
    roots = stored.get("roots")
    return {
        "files": [str(item) for item in files] if isinstance(files, list) else [],
        "roots": [str(item) for item in roots] if isinstance(roots, list) else [],
    }


def _save_publish_guard(
    agent_state: AgentState | None,
    *,
    protected_files: set[str],
    protected_roots: set[str],
) -> dict[str, list[str]]:
    existing = _get_publish_guard(agent_state)
    merged = {
        "files": sorted({*existing["files"], *protected_files})[:32],
        "roots": sorted({*existing["roots"], *protected_roots})[:32],
    }
    if agent_state is not None:
        agent_state.set_global_value(PUBLISH_GUARD_KEY, merged)
    return merged


def _command_mentions_target(command: str, target: str) -> bool:
    target = _clean_shell_token(target)
    if not target:
        return False
    if target in command:
        return True

    basename = posixpath.basename(target)
    if basename and basename != target:
        return re.search(rf"(?<![\w.-]){re.escape(basename)}(?![\w.-])", command) is not None
    return False


def _command_resets_root(command: str, root: str) -> bool:
    root = _clean_shell_token(root)
    if not root:
        return False

    if re.search(rf"\bfind\s+{re.escape(root)}\b[^\n;|&]*\s+-delete\b", command, re.IGNORECASE):
        return True

    if DESTRUCTIVE_ROOT_RM_RE.search(command) and re.search(
        rf"{re.escape(root)}(?:\s|$|/\*|/\.\*|/\.$)",
        command,
    ):
        return True

    if DESTRUCTIVE_GIT_RE.search(command) and (
        re.search(rf"--git-dir={re.escape(root)}(?:\b|/)", command)
        or re.search(rf"\bgit\s+init(?:\s+--bare)?\s+{re.escape(root)}(?:\b|/)", command)
    ):
        return True

    return False


def _target_variants(target: str) -> list[str]:
    cleaned = _clean_shell_token(target)
    if not cleaned:
        return []

    variants = [cleaned]
    basename = posixpath.basename(cleaned)
    if basename and basename != cleaned:
        variants.append(basename)
    return list(dict.fromkeys(variants))


def _command_writes_protected_file(command: str, target: str) -> bool:
    for variant in _target_variants(target):
        escaped = re.escape(variant)
        if re.search(rf"(?<!<)(?:>>?|1>>?|2>>?)\s*{escaped}(?:\b|$)", command):
            return True
        if re.search(rf"\btee\b[^\n;|&]*\s+{escaped}(?:\b|$)", command):
            return True
        if re.search(rf"\b(?:sed|perl)\b[^\n;|&]*\s-i(?:\S*)?[^\n;|&]*\s+{escaped}(?:\b|$)", command):
            return True
        if re.search(rf"\b(?:touch|truncate)\b[^\n;|&]*\s+{escaped}(?:\b|$)", command):
            return True
        if re.search(
            rf"\b(?:cp|mv|install|ln)\b[^\n;|&]*\s+{escaped}(?:\s*(?:;|&&|\|\||$))",
            command,
            re.IGNORECASE,
        ):
            return True
    return False


def _command_runs_protected_script(command: str, target: str) -> bool:
    for variant in _target_variants(target):
        escaped = re.escape(variant)
        if re.search(
            rf"\b(?:python\d*(?:\s+-u)?|bash|sh|Rscript|node|perl|ruby)\b[^\n;|&]*\s+{escaped}(?:\b|$)",
            command,
            re.IGNORECASE,
        ):
            return True
    return False


def _command_is_dangerous_git_meta_op(command: str) -> bool:
    return POST_SUCCESS_GIT_META_RE.search(command) is not None


def _maybe_block_post_success_reset(
    *,
    command: str,
    description: str | None,
    agent_state: AgentState | None,
) -> dict[str, Any] | None:
    override_requested = POST_SUCCESS_OVERRIDE_TOKEN in command or POST_SUCCESS_OVERRIDE_TOKEN in (description or "")

    guard = _get_publish_guard(agent_state)
    if not guard["files"] and not guard["roots"]:
        return None

    reasons: list[str] = []
    hard_reasons: list[str] = []
    for protected_file in guard["files"]:
        if (DESTRUCTIVE_FILE_RM_RE.search(command) or DESTRUCTIVE_FIND_DELETE_RE.search(command)) and _command_mentions_target(
            command,
            protected_file,
        ):
            reason = f"delete protected output {protected_file}"
            reasons.append(reason)
            if not _clean_shell_token(protected_file).startswith("/tmp/"):
                hard_reasons.append(reason)
        if _command_writes_protected_file(command, protected_file):
            reason = f"rewrite protected file {protected_file}"
            reasons.append(reason)
            hard_reasons.append(reason)
        if _command_runs_protected_script(command, protected_file):
            reason = f"rerun protected generator script {protected_file}"
            reasons.append(reason)
            hard_reasons.append(reason)

    for protected_root in guard["roots"]:
        if _command_resets_root(command, protected_root):
            reason = f"reset protected root {protected_root}"
            reasons.append(reason)
            if not _clean_shell_token(protected_root).startswith("/tmp/"):
                hard_reasons.append(reason)

    if _command_is_dangerous_git_meta_op(command):
        reason = "mutate git history or repository metadata after publish state"
        reasons.append(reason)
        hard_reasons.append(reason)

    if not reasons:
        return None

    if override_requested and not hard_reasons:
        return None

    detail = ", ".join(dict.fromkeys(reasons))
    hard_detail = ", ".join(dict.fromkeys(hard_reasons))
    if override_requested and hard_reasons:
        warning = (
            "Blocked a post-validation command even though the override token was provided because this command would "
            f"{hard_detail}. Split cleanup from validation: remove explicit extras with a separate bounded command, "
            "do any reruns in /tmp or a copied scratch directory, and keep git history/meta operations off the live "
            "deliverable state. If the published state truly needs a code change, make that change first, then rerun "
            "the full acceptance sweep before any further cleanup."
        )
    else:
        warning = (
            "Blocked a potentially destructive or state-mutating post-validation command because a prior "
            f"final/evaluator-style check succeeded and this command would {detail}. Use /tmp or a copied "
            "scratch directory for further experiments. If you truly have new failing evidence and only need a "
            f"bounded cleanup/reset on the live deliverable state, include {POST_SUCCESS_OVERRIDE_TOKEN} in the "
            "description with the reason, then rerun the full acceptance sweep after the change."
        )
    return {
        "content": warning,
        "returnDisplay": warning,
        "duration_ms": 0,
        "exit_code": 90,
        "error": {
            "message": warning,
            "type": "POST_SUCCESS_STATE_GUARD",
        },
    }


def _maybe_activate_publish_guard(
    *,
    command: str,
    description: str | None,
    exit_code: int | None,
    agent_state: AgentState | None,
    cwd: str | None,
) -> str | None:
    if exit_code != 0 or not description or not FINAL_CHECK_RE.search(description):
        return None

    protected_files, protected_roots = _extract_publish_guard_targets(command)
    if cwd and not cwd.startswith("/tmp/"):
        protected_roots.add(cwd)
    guard = _save_publish_guard(
        agent_state,
        protected_files=protected_files,
        protected_roots=protected_roots,
    )

    protected_targets = [*guard["files"], *guard["roots"]]
    if protected_targets:
        target_preview = ", ".join(protected_targets[:4])
        if len(protected_targets) > 4:
            target_preview += ", ..."
        return (
            "This final/evaluator-style check passed, so the current state is now your publish state. "
            f"Do not reset repos/web roots, rewrite checked files, or rerun stateful generator scripts touching protected targets such as {target_preview}. If cleanup "
            "is required, remove only explicit forbidden extras and rerun this same acceptance check after cleanup."
        )

    return (
        "This final/evaluator-style check passed, so the current filesystem/service state is now your publish "
        "state. Do any extra experiments in /tmp or a copied scratch directory instead of resetting, rewriting, "
        "or rerunning stateful generators against the live deliverable state."
    )


def run_shell_command(
    command: str,
    description: str | None = None,
    is_background: bool = False,
    dir_path: str | None = None,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    update_output: Callable[[str], None] | None = None,
    agent_state: AgentState | None = None,
) -> dict[str, Any]:
    """
    Executes a shell command.

    On Unix/Linux/macOS: Executes as `bash -c <command>`
    On Windows: Executes as `powershell.exe -NoProfile -Command <command>`

    The following information is returned:
    - Output: Combined stdout/stderr. Can be `(empty)` or partial on error.
    - Exit Code: Only included if non-zero (command failed).
    - Error: Only included if a process-level error occurred.
    - Signal: Only included if process was terminated by a signal.
    - Background PIDs: Only included if background processes were started.
    - Process Group PGID: Only included if available.

    Args:
        command: The exact command to execute
        description: Brief description of the command for the user
        dir_path: Directory to run the command in (optional)
        is_background: Whether to run in background
        timeout_ms: Timeout in milliseconds (0 for no timeout)
        update_output: Callback for streaming output updates

    Returns:
        Dict with content and returnDisplay matching gemini-cli format
    """
    try:
        # Validate command
        if not command or not command.strip():
            return {
                "content": "Command cannot be empty.",
                "returnDisplay": "Error: Empty command.",
                "error": {
                    "message": "Command cannot be empty.",
                    "type": "INVALID_COMMAND",
                },
            }

        sandbox: BaseSandbox = get_sandbox(agent_state)

        # Determine working directory (string resolution only; checks via sandbox)
        if dir_path:
            cwd = resolve_path(dir_path, sandbox)
            if not sandbox.file_exists(cwd):
                error_msg = f"Directory not found: {dir_path}"
                return {
                    "content": error_msg,
                    "returnDisplay": "Error: Directory not found.",
                    "error": {"message": error_msg, "type": "DIRECTORY_NOT_FOUND"},
                }
            info = sandbox.get_file_info(cwd)
            if not info.is_directory:
                error_msg = f"Path is not a directory: {dir_path}"
                return {
                    "content": error_msg,
                    "returnDisplay": "Error: Path is not a directory.",
                    "error": {"message": error_msg, "type": "NOT_A_DIRECTORY"},
                }
        else:
            cwd = str(sandbox.work_dir)

        timeout_arg = timeout_ms if timeout_ms and timeout_ms > 0 else None

        guard_block = _maybe_block_post_success_reset(
            command=command,
            description=description,
            agent_state=agent_state,
        )
        if guard_block is not None:
            return guard_block

        if is_background:
            # Background mode: sandbox.execute_bash supports cwd and background params
            start = time.time()
            cmd_result = sandbox.execute_bash(
                command,
                timeout=timeout_arg,
                cwd=cwd,
                background=True,
            )
            duration_ms = int((time.time() - start) * 1000)
            bg_pid = cmd_result.background_pid
            if bg_pid is not None:
                llm_content = (
                    f"Background task started (pid: {bg_pid}). "
                    f"Use BackgroundTaskManage with action='status' and pid={bg_pid} to check output."
                )
                bg_result: dict[str, Any] = {
                    "content": llm_content,
                    "returnDisplay": f"Background task started (pid: {bg_pid})",
                    "duration_ms": duration_ms,
                    "backgroundPids": [bg_pid],
                }
                if cmd_result.output_dir:
                    bg_result["output_dir"] = cmd_result.output_dir
                    bg_result["stdout_file"] = cmd_result.stdout_file
                    bg_result["stderr_file"] = cmd_result.stderr_file
                return bg_result
            # Fallback if sandbox didn't return pid
            fallback_result: dict[str, Any] = {
                "content": cmd_result.stdout or "Background task started.",
                "returnDisplay": cmd_result.stdout or "Background task started.",
                "duration_ms": duration_ms,
            }
            if cmd_result.error:
                fallback_result["error"] = {
                    "message": cmd_result.error,
                    "type": "SHELL_EXECUTE_ERROR",
                }
            return fallback_result

        # Foreground mode
        # Build description for display
        cmd_description = command
        if dir_path:
            cmd_description += f" [in {dir_path}]"
        else:
            cmd_description += f" [current working directory {cwd}]"
        if description:
            cmd_description += f" ({description.replace(chr(10), ' ')})"
        # Streaming output is not supported by execute_bash; ignore update_output.
        _ = update_output

        # Execute command through sandbox, optionally scoping to directory via `cd`.
        cmd_to_run = command
        if cwd:
            cmd_to_run = f"cd {shlex.quote(cwd)} && {command}"

        start = time.time()
        cmd_result = sandbox.execute_bash(cmd_to_run, timeout=timeout_arg)
        duration_ms = int((time.time() - start) * 1000)

        stdout = cmd_result.stdout or ""
        stderr = cmd_result.stderr or ""
        output = stdout
        if stderr:
            output = f"{stdout}\n{stderr}" if stdout else stderr

        # Truncate large output (matching gemini-cli: keep last N lines)
        output = _truncate_shell_output(output)

        exit_code = cmd_result.exit_code
        error_message = cmd_result.error

        # Build result
        llm_parts: list[str] = []
        if cmd_result.status == SandboxStatus.TIMEOUT:
            timeout_minutes = (timeout_ms / 60000) if timeout_ms else 0
            llm_parts.append(f"Timeout: command timed out after {timeout_minutes:.1f} minutes.")
            llm_parts.append(
                "Hint: inspect partial progress via stdout_file/stderr_file. For exploratory probes, use a smaller timeout_ms; for truly long-running installs, servers, training, or builds, prefer is_background=true and then follow up with short status/log checks."
            )
        else:
            llm_parts.append(f"Output: {output if output else '(empty)'}")

        if error_message:
            llm_parts.append(f"Error: {error_message}")

        if exit_code != 0:
            llm_parts.append(f"Exit Code: {exit_code}")

        execution_notes = _collect_execution_notes(
            command=command,
            description=description,
            output=output,
            exit_code=exit_code,
            duration_ms=duration_ms,
            timed_out=cmd_result.status == SandboxStatus.TIMEOUT,
        )
        for note in execution_notes:
            llm_parts.append(f"Execution note: {note}")

        publish_guard_note = _maybe_activate_publish_guard(
            command=command,
            description=description,
            exit_code=exit_code,
            agent_state=agent_state,
            cwd=cwd,
        )
        if publish_guard_note:
            llm_parts.append(f"Execution note: {publish_guard_note}")

        llm_content = "\n".join(llm_parts)

        # Build return display
        if output and output.strip():
            return_display = output
        elif cmd_result.status == SandboxStatus.TIMEOUT:
            return_display = f"Command timed out after {timeout_ms / 60000:.1f} minutes."
        elif error_message:
            return_display = f"Command failed: {error_message}"
        elif exit_code != 0:
            return_display = f"Command exited with code: {exit_code}"
        else:
            return_display = "(empty)"

        result: dict[str, Any] = {
            "content": llm_content,
            "returnDisplay": return_display,
            "duration_ms": duration_ms,
            "exit_code": exit_code,
        }

        # Include CommandResult truncation metadata and file paths
        if cmd_result.output_dir:
            result["output_dir"] = cmd_result.output_dir
            result["stdout_file"] = cmd_result.stdout_file
            result["stderr_file"] = cmd_result.stderr_file
        if cmd_result.truncated:
            result["truncated"] = True
            result["original_stdout_length"] = cmd_result.original_stdout_length
            result["original_stderr_length"] = cmd_result.original_stderr_length

        if error_message or cmd_result.status in (
            SandboxStatus.ERROR,
            SandboxStatus.TIMEOUT,
        ):
            result["error"] = {
                "message": error_message or "Command failed",
                "type": "SHELL_EXECUTE_ERROR",
            }

        return result

    except Exception as e:
        error_msg = f"Error executing shell command: {str(e)}"
        return {
            "content": error_msg,
            "returnDisplay": error_msg,
            "error": {
                "message": error_msg,
                "type": "SHELL_EXECUTE_ERROR",
            },
        }
