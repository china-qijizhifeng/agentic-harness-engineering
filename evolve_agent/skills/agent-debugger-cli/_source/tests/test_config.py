import json
import os
import subprocess
import sys
import tempfile


def _run(args, env=None):
    return subprocess.run(
        [sys.executable, "-m", "agent_debugger_core.cli.adb"] + args,
        capture_output=True, text=True, check=False, env=env,
    )


def test_config_writes_llm_block(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    payload = json.dumps({"llm": {"model": "gpt-4.1", "base_url": "https://x", "api_key": "k"}})
    result = _run(["config", payload], env={**os.environ, "HOME": str(tmp_path)})
    assert result.returncode == 0, result.stderr
    cfg = json.loads((tmp_path / ".adb" / "adb_cli_config.json").read_text())
    assert cfg["llm"]["model"] == "gpt-4.1"
    assert cfg["llm"]["api_key"] == "k"


def test_config_deep_merge_preserves_other_keys(tmp_path):
    adb_dir = tmp_path / ".adb"
    adb_dir.mkdir()
    (adb_dir / "adb_cli_config.json").write_text(json.dumps({"llm": {"model": "old"}, "qa": {"agent_config": "/p"}}))
    payload = json.dumps({"llm": {"api_key": "new"}})
    env = {**os.environ, "HOME": str(tmp_path)}
    result = _run(["config", payload], env=env)
    assert result.returncode == 0, result.stderr
    cfg = json.loads((adb_dir / "adb_cli_config.json").read_text())
    assert cfg["llm"] == {"model": "old", "api_key": "new"}
    assert cfg["qa"] == {"agent_config": "/p"}


def test_config_rejects_malformed_llm(tmp_path):
    env = {**os.environ, "HOME": str(tmp_path)}
    result = _run(["config", '{"llm": "not-an-object"}', "--format", "json"], env=env)
    assert result.returncode != 0
    err = json.loads(result.stdout)
    assert err["status"] == "failed"
    assert err["command"] == "config"
