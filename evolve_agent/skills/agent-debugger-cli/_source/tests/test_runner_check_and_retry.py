import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_debugger_core.runtime.runner import run_agent, RunnerResult, RunnerError


def _prep_env(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "m"); monkeypatch.setenv("LLM_BASE_URL", "u")
    monkeypatch.setenv("LLM_API_KEY", "k"); monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AHE_HOME", str(tmp_path))
    (tmp_path / "evolve_agent" / "tools").mkdir(parents=True)
    (tmp_path / "evolve_agent" / "tools" / "__init__.py").write_text("")


def _wrap(payload):
    inner = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return json.dumps({
        "success": True,
        "message": "Result submitted and task completed.",
        "status": "TASK_COMPLETED",
        "task_completed": True,
        "output": {"result": inner},
    }, ensure_ascii=False)


def _scripted(sequence):
    fake = MagicMock()
    fake.run.side_effect = [_wrap(p) for p in sequence]
    return fake


def test_check_mode_happy_path(tmp_path, monkeypatch):
    _prep_env(tmp_path, monkeypatch)
    payload = {"mode": "check",
               "issues": [{"issue_type": "工具错误", "summary": "s", "evidence": "e", "message_index": 3}],
               "response": "r"}
    with patch("agent_debugger_core.runtime.runner._build_agent", return_value=_scripted([payload])):
        result = run_agent(trace_paths=[Path("/f")], mode="check")
    assert result.mode == "check"
    assert result.issues[0]["issue_type"] == "工具错误"
    assert result.response == "r"


def test_check_mode_retries_once_on_invalid_schema(tmp_path, monkeypatch):
    _prep_env(tmp_path, monkeypatch)
    bad = {"mode": "check", "issues": [{"issue_type": "nope", "summary": "s", "evidence": "e", "message_index": 0}]}
    good = {"mode": "check", "issues": [], "response": "r"}
    with patch("agent_debugger_core.runtime.runner._build_agent", return_value=_scripted([bad, good])):
        result = run_agent(trace_paths=[Path("/f")], mode="check")
    assert result.issues == []


def test_check_mode_fails_after_two_bad(tmp_path, monkeypatch):
    _prep_env(tmp_path, monkeypatch)
    bad = {"mode": "check", "issues": [{"issue_type": "nope", "summary": "s", "evidence": "e", "message_index": 0}]}
    with patch("agent_debugger_core.runtime.runner._build_agent", return_value=_scripted([bad, bad])):
        with pytest.raises(RunnerError):
            run_agent(trace_paths=[Path("/f")], mode="check")


def test_budget_exceeded_fallback(tmp_path, monkeypatch):
    _prep_env(tmp_path, monkeypatch)
    fake = MagicMock()
    fake.run.return_value = "partial thoughts\n[Note: Maximum iteration limit reached]"
    with patch("agent_debugger_core.runtime.runner._build_agent", return_value=fake):
        result = run_agent(trace_paths=[Path("/f")], mode="ask", question="q")
    assert result.budget_exceeded is True
    assert result.answer.startswith("[budget-exceeded]")
    assert "partial thoughts" in result.answer
