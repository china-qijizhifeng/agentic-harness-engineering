from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional

from agent_debugger_core.cli.llm_resolver import resolve_llm_settings
from agent_debugger_core.runtime.bootstrap import ensure_tools_importable


RUNTIME_DIR = Path(__file__).parent
AGENT_CONFIG_PATH = RUNTIME_DIR / "agent_config.yaml"

ALLOWED_ISSUE_TYPES = {"工具错误", "幻觉", "循环", "不合规", "截断"}
BUDGET_MARKER = "[Note: Maximum iteration limit reached]"


class RunnerError(Exception):
    pass


class BudgetExceeded(Exception):
    def __init__(self, fallback_text: str):
        super().__init__(fallback_text)
        self.fallback_text = fallback_text


@dataclass
class RunnerResult:
    mode: str
    answer: Optional[str] = None
    issues: List[dict] = field(default_factory=list)
    response: Optional[str] = None
    iterations: int = 0
    budget_exceeded: bool = False


def _build_user_message(trace_paths: List[Path], mode: str, question: Optional[str]) -> str:
    lines = ["Analyze the following normalized trace file(s):"]
    for p in trace_paths:
        lines.append(f"- {p}")
    lines.append("")
    if mode == "ask":
        lines.append(f"Question: {question or 'Why is this trace so slow?'}")
    else:
        lines.append(
            "Task: produce a QC report. Return a JSON payload with "
            '`mode="check"`, `issues=[...]`, and `response="..."`.'
        )
    return "\n".join(lines)


def _build_agent(llm_settings: dict) -> Any:
    """Construct nexau.Agent from agent_config.yaml with LLM settings patched in."""
    import os as _os
    ensure_tools_importable()
    from nexau import Agent, AgentConfig  # noqa: WPS433 — deferred import

    # Populate env vars that agent_config.yaml references via ${env.LLM_*}.
    # AgentConfig.from_yaml substitutes env at load time, so we must set these
    # *before* the load. Caller-supplied llm_settings win; we still overwrite
    # the config fields below for belt-and-suspenders.
    _os.environ["LLM_MODEL"] = llm_settings["model"]
    _os.environ["LLM_BASE_URL"] = llm_settings["base_url"]
    _os.environ["LLM_API_KEY"] = llm_settings["api_key"]
    _os.environ.setdefault("LLM_API_TYPE", "openai_chat_completion")

    config = AgentConfig.from_yaml(config_path=AGENT_CONFIG_PATH)
    config.llm_config.model = llm_settings["model"]
    config.llm_config.base_url = llm_settings["base_url"]
    config.llm_config.api_key = llm_settings["api_key"]
    if "reasoning" in llm_settings:
        config.llm_config.reasoning = llm_settings["reasoning"]
    return Agent(config=config)


def _parse_inner_payload(raw: Any) -> dict:
    if not isinstance(raw, str):
        raise RunnerError("complete_task `result` is not a string")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not m:
            raise RunnerError(f"complete_task result is not JSON: {raw[:200]}")
        return json.loads(m.group(0))


def _parse_run_output(run_output: Any) -> dict:
    """Parse nexau.Agent.run() string output into the inner JSON payload.

    Raises BudgetExceeded when the agent hit max_iterations without calling
    complete_task; the exception carries the last-assistant text so callers
    can surface it as a [budget-exceeded] fallback.
    """
    s = str(run_output or "").strip()
    if not s:
        raise BudgetExceeded("")

    if BUDGET_MARKER in s:
        raise BudgetExceeded(s.replace(BUDGET_MARKER, "").strip())

    # nexau.Agent.run() may return a plain JSON string OR a JSON-encoded
    # JSON string (double-encoded). Unwrap up to 2 times until we see an object.
    outer = s
    for _ in range(2):
        try:
            outer = json.loads(outer)
        except json.JSONDecodeError:
            raise RunnerError(f"agent.run() output is not JSON: {s[:200]}")
        if isinstance(outer, dict):
            break
        if not isinstance(outer, str):
            raise RunnerError(f"agent.run() output is not a JSON object: {s[:200]}")
    else:
        raise RunnerError(f"agent.run() output is not a JSON object: {s[:200]}")

    status = outer.get("status")
    if status != "TASK_COMPLETED" or not outer.get("task_completed"):
        raise RunnerError(
            f"agent ended without completing task (status={status!r}): {s[:200]}"
        )

    output = outer.get("output") or {}
    raw_result = output.get("result") if isinstance(output, dict) else None
    return _parse_inner_payload(raw_result)


def _validate_check_payload(payload: dict) -> None:
    if payload.get("mode") != "check":
        raise RunnerError(f"expected mode=check, got {payload.get('mode')!r}")
    issues = payload.get("issues")
    if not isinstance(issues, list):
        raise RunnerError("check payload missing `issues` list")
    for i, it in enumerate(issues):
        if not isinstance(it, dict):
            raise RunnerError(f"issue #{i} is not an object")
        for k in ("issue_type", "summary", "evidence", "message_index"):
            if k not in it:
                raise RunnerError(f"issue #{i} missing `{k}`")
        if it["issue_type"] not in ALLOWED_ISSUE_TYPES:
            raise RunnerError(
                f"issue #{i} has invalid issue_type={it['issue_type']!r}; "
                f"must be one of {sorted(ALLOWED_ISSUE_TYPES)}"
            )
        if not isinstance(it["message_index"], int):
            raise RunnerError(f"issue #{i}.message_index must be int")


def _run_with_retry(agent, user_message: str, *, attempts: int = 3):
    last = None
    for i in range(attempts):
        try:
            return agent.run(message=user_message)
        except Exception as e:
            last = e
            if i < attempts - 1:
                time.sleep(2 ** i)
    raise RunnerError(f"llm: {last}")


def run_agent(
    *,
    trace_paths: List[Path],
    mode: str,
    question: Optional[str] = None,
) -> RunnerResult:
    if mode not in ("ask", "check"):
        raise RunnerError(f"unknown mode: {mode}")
    llm_settings = resolve_llm_settings()
    agent = _build_agent(llm_settings)
    user_message = _build_user_message(trace_paths, mode, question)

    run_output = _run_with_retry(agent, user_message)

    try:
        payload = _parse_run_output(run_output)
    except BudgetExceeded as be:
        fallback_text = be.fallback_text
        if mode == "ask":
            return RunnerResult(
                mode="ask",
                answer=f"[budget-exceeded] {fallback_text}".strip(),
                budget_exceeded=True,
            )
        return RunnerResult(
            mode="check",
            issues=[],
            response=f"[budget-exceeded] {fallback_text}".strip(),
            budget_exceeded=True,
        )

    if mode == "ask":
        answer = payload.get("answer")
        if not isinstance(answer, str):
            raise RunnerError("ask payload missing string `answer`")
        return RunnerResult(mode="ask", answer=answer)

    try:
        _validate_check_payload(payload)
    except RunnerError as first_err:
        retry_msg = (
            user_message
            + "\n\nYour last complete_task payload was rejected: "
            + str(first_err)
            + "\nRe-emit a valid `check` payload that matches the schema exactly."
        )
        run_output = _run_with_retry(agent, retry_msg)
        try:
            payload = _parse_run_output(run_output)
        except BudgetExceeded as be:
            return RunnerResult(
                mode="check",
                issues=[],
                response=f"[budget-exceeded] {be.fallback_text}".strip(),
                budget_exceeded=True,
            )
        _validate_check_payload(payload)

    return RunnerResult(
        mode="check",
        issues=payload["issues"],
        response=str(payload.get("response", "") or ""),
    )
