import json
from unittest.mock import patch

from agent_debugger_core.runtime.runner import RunnerResult, RunnerError
from agent_debugger_core.cli import adb as adb_cli


def test_records_file_two_lines_one_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("AHE_HOME", str(tmp_path))
    (tmp_path / "evolve_agent" / "tools").mkdir(parents=True)
    (tmp_path / "evolve_agent" / "tools" / "__init__.py").write_text("")
    monkeypatch.setenv("LLM_MODEL", "m")
    monkeypatch.setenv("LLM_BASE_URL", "u")
    monkeypatch.setenv("LLM_API_KEY", "k")

    records = tmp_path / "rec.jsonl"
    records.write_text(
        json.dumps({"queries": ["q1"], "traces": {"trace_id": "t1",
            "messages": [{"role": "user", "content": "hi"}]}}) + "\n"
        + json.dumps({"queries": ["q2"], "traces": {"trace_id": "t2",
            "messages": [{"role": "user", "content": "hi"}]}}) + "\n"
    )

    def _side_effect(**kwargs):
        q = kwargs.get("question")
        if q == "q2":
            raise RunnerError("simulated failure for t2")
        return RunnerResult(mode="ask", answer="ok for " + q, iterations=3)

    with patch("agent_debugger_core.cli.adb.run_agent", side_effect=_side_effect):
        rc = adb_cli.main(["ask", "-f", str(records), "-j", "2", "--format", "json"])
    assert rc == 0
    out = capsys.readouterr().out
    parsed = [json.loads(line) for line in out.strip().splitlines()]
    statuses = sorted(p["status"] for p in parsed)
    assert statuses == ["failed", "success"]
