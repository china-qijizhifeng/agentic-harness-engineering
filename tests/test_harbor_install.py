import asyncio
import importlib
import os
import subprocess
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import yaml

import evolve
from agents.harbor_nexau import PinnedNexAU


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("prebuilt", ["True", "False"])
def test_harbor_uploads_install_script_for_pinned_nexau(tmp_path, monkeypatch, prebuilt):
    monkeypatch.setenv("USE_BP_E2B", prebuilt)
    agent = PinnedNexAU(logs_dir=tmp_path, config_path=str(tmp_path / "agent.yaml"))
    environment = SimpleNamespace(
        exec=AsyncMock(return_value=SimpleNamespace(return_code=0, stdout="", stderr="")),
        upload_dir=AsyncMock(), upload_file=AsyncMock(),
    )
    asyncio.run(agent.setup(environment))
    uploaded = environment.upload_file.call_args.kwargs["source_path"]
    script = uploaded.read_text()
    subprocess.run(["bash", "-n", str(uploaded)], check=True)
    if prebuilt == "False":
        dependencies = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
        nexau = next(dep.split(" @ ", 1)[1] for dep in dependencies if dep.startswith("nexau @ "))
        assert nexau in script
        assert "nex-agi/NexAU" not in script
    else:
        assert 'source $NEXAU_VENV/bin/activate' in script
        assert all(not line.startswith("uv pip install") for line in script.splitlines())


@pytest.mark.parametrize("agent_name", ["nexau", "claude-code"])
def test_harbor_launch_uses_importable_adapter(tmp_path, monkeypatch, agent_name):
    config = yaml.safe_load((ROOT / "configs/base.yaml").read_text())
    config["harbor"]["agent"] = agent_name
    monkeypatch.setenv("PYTHONPATH", "/existing/python/path")
    monkeypatch.setattr(evolve, "get_llm_config", lambda *args, **kwargs: {"model": "compat-test"})
    monkeypatch.setattr(evolve, "find_latest_job_dir", lambda path: None)
    popen = Mock()
    monkeypatch.setattr(evolve.subprocess, "Popen", popen)
    evolve.launch_harbor(config, tmp_path, "agent.yaml", tmp_path / "jobs")
    cmd = popen.call_args.args[0]
    env = popen.call_args.kwargs["env"]
    assert env["PYTHONPATH"].split(os.pathsep) == [str(ROOT), "/existing/python/path"]
    if agent_name == "nexau":
        assert "--agent" not in cmd
        import_path = cmd[cmd.index("--agent-import-path") + 1]
        module, name = import_path.split(":")
        assert getattr(importlib.import_module(module), name) is PinnedNexAU
    else:
        assert "--agent-import-path" not in cmd
        assert cmd[cmd.index("--agent") + 1] == agent_name
