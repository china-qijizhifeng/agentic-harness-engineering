from __future__ import annotations

import os
from typing import Dict

from agent_debugger_core.cli.config_store import load_config


class LLMSettingsError(Exception):
    pass


def resolve_llm_settings() -> Dict[str, str]:
    cfg_llm = (load_config().get("llm") or {}) if isinstance(load_config().get("llm"), dict) else {}

    def pick(cfg_key: str, qa_env: str, llm_env: str) -> str:
        return (
            cfg_llm.get(cfg_key)
            or os.environ.get(qa_env)
            or os.environ.get(llm_env)
            or ""
        )

    settings = {
        "model": pick("model", "QA_MODEL_NAME", "LLM_MODEL"),
        "base_url": pick("base_url", "QA_BASE_URL", "LLM_BASE_URL"),
        "api_key": pick("api_key", "QA_API_KEY", "LLM_API_KEY"),
    }
    if "reasoning" in cfg_llm and isinstance(cfg_llm["reasoning"], dict):
        settings["reasoning"] = cfg_llm["reasoning"]

    if not settings["model"] or not settings["base_url"]:
        raise LLMSettingsError(
            "llm.model and llm.base_url must be set via `adb config`, QA_*, or LLM_* env vars."
        )
    return settings
