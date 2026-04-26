import json
from pathlib import Path
from unittest.mock import patch


FIX = Path(__file__).parent / "fixtures"


def _setup_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LLM_MODEL", "m")
    monkeypatch.setenv("LLM_BASE_URL", "u")
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("AHE_HOME", str(tmp_path))
    (tmp_path / "evolve_agent" / "tools").mkdir(parents=True)
    (tmp_path / "evolve_agent" / "tools" / "__init__.py").write_text("")


def test_cli_ask_json_format(tmp_path, monkeypatch, capsys):
    _setup_env(tmp_path, monkeypatch)
    trace = FIX / "openai_messages_sample.json"

    from agent_debugger_core.runtime.runner import RunnerResult
    from agent_debugger_core.cli import adb as adb_cli

    fake_result = RunnerResult(mode="ask", answer="because", iterations=3)
    with patch("agent_debugger_core.cli.adb.run_agent", return_value=fake_result):
        rc = adb_cli.main(["ask", "-t", str(trace), "-q", "why?", "--format", "json"])
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["status"] == "success"
    assert payload["command"] == "ask"
    assert payload["response"] == "because"
    assert payload["question"] == "why?"


def test_cli_ask_text_format(tmp_path, monkeypatch, capsys):
    _setup_env(tmp_path, monkeypatch)
    trace = FIX / "openai_messages_sample.json"

    from agent_debugger_core.runtime.runner import RunnerResult
    from agent_debugger_core.cli import adb as adb_cli

    fake_result = RunnerResult(mode="ask", answer="because", iterations=3)
    with patch("agent_debugger_core.cli.adb.run_agent", return_value=fake_result):
        rc = adb_cli.main(["ask", "-t", str(trace), "-q", "why?"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "because" in captured.out
