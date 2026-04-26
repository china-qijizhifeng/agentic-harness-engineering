import json
import os
from pathlib import Path

import pytest

from agent_debugger_core.cli.llm_resolver import resolve_llm_settings, LLMSettingsError


def _prep_home(tmp_path, cfg):
    (tmp_path / ".adb").mkdir()
    (tmp_path / ".adb" / "adb_cli_config.json").write_text(json.dumps(cfg))


def test_config_llm_wins_over_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _prep_home(tmp_path, {"llm": {"model": "m-cfg", "base_url": "u-cfg", "api_key": "k-cfg"}})
    monkeypatch.setenv("QA_MODEL_NAME", "m-qa")
    monkeypatch.setenv("LLM_MODEL", "m-llm")
    s = resolve_llm_settings()
    assert s["model"] == "m-cfg"
    assert s["base_url"] == "u-cfg"
    assert s["api_key"] == "k-cfg"


def test_qa_env_beats_llm_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for k in ("QA_MODEL_NAME", "QA_BASE_URL", "QA_API_KEY"):
        monkeypatch.setenv(k, f"qa-{k}")
    for k in ("LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY"):
        monkeypatch.setenv(k, f"llm-{k}")
    s = resolve_llm_settings()
    assert s["model"] == "qa-QA_MODEL_NAME"
    assert s["base_url"] == "qa-QA_BASE_URL"
    assert s["api_key"] == "qa-QA_API_KEY"


def test_missing_model_or_base_url_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for k in ("QA_MODEL_NAME", "QA_BASE_URL", "QA_API_KEY",
              "LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(LLMSettingsError):
        resolve_llm_settings()
