import os
from pathlib import Path

import pytest

from agent_debugger_core.runtime.bootstrap import ensure_tools_importable, BootstrapError


def test_bootstrap_prefers_ahe_home_env(tmp_path, monkeypatch):
    fake_ahe = tmp_path / "ahe"
    (fake_ahe / "evolve_agent" / "tools").mkdir(parents=True)
    (fake_ahe / "evolve_agent" / "tools" / "__init__.py").write_text("")
    monkeypatch.setenv("AHE_HOME", str(fake_ahe))
    monkeypatch.chdir(tmp_path)

    added = ensure_tools_importable()
    assert str(fake_ahe / "evolve_agent") == added


def test_bootstrap_falls_back_to_cwd(tmp_path, monkeypatch):
    monkeypatch.delenv("AHE_HOME", raising=False)
    fake_ahe = tmp_path / "root"
    (fake_ahe / "evolve_agent" / "tools").mkdir(parents=True)
    (fake_ahe / "evolve_agent" / "tools" / "__init__.py").write_text("")
    monkeypatch.chdir(fake_ahe)

    added = ensure_tools_importable()
    assert str(fake_ahe / "evolve_agent") == added


def test_bootstrap_raises_when_nothing_found(tmp_path, monkeypatch):
    monkeypatch.delenv("AHE_HOME", raising=False)
    monkeypatch.chdir(tmp_path)  # empty dir, no evolve_agent
    with pytest.raises(BootstrapError):
        ensure_tools_importable(_skip_self_search=True)
