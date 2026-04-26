import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_debugger_core.runtime.runner import run_agent, RunnerResult, RunnerError


def _wrap(payload):
    inner = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return json.dumps({
        "success": True,
        "message": "Result submitted and task completed.",
        "status": "TASK_COMPLETED",
        "task_completed": True,
        "output": {"result": inner},
    }, ensure_ascii=False)


def _scripted_agent(payload: dict):
    fake_agent = MagicMock()
    fake_agent.run.return_value = _wrap(payload)
    return fake_agent


def test_ask_mode_happy_path(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "m")
    monkeypatch.setenv("LLM_BASE_URL", "u")
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AHE_HOME", str(tmp_path))
    (tmp_path / "evolve_agent" / "tools").mkdir(parents=True)
    (tmp_path / "evolve_agent" / "tools" / "__init__.py").write_text("")

    fake_agent = _scripted_agent({"mode": "ask", "answer": "42"})

    with patch("agent_debugger_core.runtime.runner._build_agent", return_value=fake_agent):
        result = run_agent(
            trace_paths=[Path("/fake/trace.json")],
            mode="ask",
            question="why?",
        )

    assert isinstance(result, RunnerResult)
    assert result.mode == "ask"
    assert result.answer == "42"
    assert result.budget_exceeded is False
