import json
from pathlib import Path
from unittest.mock import patch

from agent_debugger_core.runtime.runner import RunnerResult
from agent_debugger_core.cli import adb as adb_cli


FIX = Path(__file__).parent / "fixtures"


def _setup_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AHE_HOME", str(tmp_path))
    (tmp_path / "evolve_agent" / "tools").mkdir(parents=True)
    (tmp_path / "evolve_agent" / "tools" / "__init__.py").write_text("")
    monkeypatch.setenv("LLM_MODEL", "m")
    monkeypatch.setenv("LLM_BASE_URL", "u")
    monkeypatch.setenv("LLM_API_KEY", "k")


def test_check_json_schema(capsys, tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    trace = FIX / "openai_messages_sample.json"
    fake = RunnerResult(
        mode="check",
        issues=[{"issue_type": "工具错误", "summary": "s", "evidence": "e", "message_index": 1}],
        response="overall ok",
        iterations=5,
    )
    with patch("agent_debugger_core.cli.adb.run_agent", return_value=fake):
        rc = adb_cli.main(["check", "-t", str(trace), "--format", "json"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "success"
    assert out["command"] == "check"
    assert out["issues_count"] == 1
    assert out["issues"][0]["issue_type"] == "工具错误"
    assert "response" in out


def test_check_text_markdown(capsys, tmp_path, monkeypatch):
    _setup_env(tmp_path, monkeypatch)
    trace = FIX / "openai_messages_sample.json"
    fake = RunnerResult(mode="check", issues=[], response="no issues", iterations=2)
    with patch("agent_debugger_core.cli.adb.run_agent", return_value=fake):
        rc = adb_cli.main(["check", "-t", str(trace)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "# ADB Check Result" in out
    assert "Issues Count" in out
