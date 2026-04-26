import json
from unittest.mock import MagicMock, patch

import pytest

from agent_debugger_core.runtime.runner import run_agent, RunnerResult, RunnerError


def _prep(tmp_path, monkeypatch):
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


class _ApiError(RuntimeError):
    pass


def test_llm_retries_twice_on_transient_error(tmp_path, monkeypatch):
    _prep(tmp_path, monkeypatch)
    fake = MagicMock()
    good = _wrap({"mode": "ask", "answer": "ok"})
    fake.run.side_effect = [_ApiError("429"), _ApiError("500"), good]
    with patch("agent_debugger_core.runtime.runner._build_agent", return_value=fake), \
         patch("agent_debugger_core.runtime.runner.time.sleep"):
        result = run_agent(trace_paths=["/f"], mode="ask", question="q")
    assert result.answer == "ok"
    assert fake.run.call_count == 3


def test_llm_fails_after_three_attempts(tmp_path, monkeypatch):
    _prep(tmp_path, monkeypatch)
    fake = MagicMock()
    fake.run.side_effect = _ApiError("429")
    with patch("agent_debugger_core.runtime.runner._build_agent", return_value=fake), \
         patch("agent_debugger_core.runtime.runner.time.sleep"):
        with pytest.raises(RunnerError) as exc:
            run_agent(trace_paths=["/f"], mode="ask", question="q")
    assert "llm:" in str(exc.value)
    assert fake.run.call_count == 3
