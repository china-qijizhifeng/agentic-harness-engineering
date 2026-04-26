from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping


ADB_CONFIG_PATH = Path.home() / ".adb" / "adb_cli_config.json"


class ADBError(Exception):
    """Structured error for CLI failure envelopes."""


def config_path() -> Path:
    # HOME re-evaluated at call time so tests can monkeypatch
    return Path(os.environ.get("HOME", str(Path.home()))) / ".adb" / "adb_cli_config.json"


def load_config() -> dict:
    p = config_path()
    if not p.exists():
        return {}
    return json.loads(p.read_text())


def _deep_merge(base: Mapping[str, Any], patch: Mapping[str, Any]) -> dict:
    merged = dict(base)
    for k, v in patch.items():
        if isinstance(v, Mapping) and isinstance(merged.get(k), Mapping):
            merged[k] = _deep_merge(merged[k], v)
        else:
            merged[k] = v
    return merged


def _validate_patch(patch: Mapping[str, Any]) -> None:
    llm = patch.get("llm")
    if llm is not None:
        if not isinstance(llm, Mapping):
            raise ADBError("config.llm must be a JSON object.")
        for k in ("model", "base_url", "api_key"):
            if k in llm and not isinstance(llm[k], str):
                raise ADBError(f"config.llm.{k} must be a string.")
        if "reasoning" in llm and not isinstance(llm["reasoning"], Mapping):
            raise ADBError("config.llm.reasoning must be a JSON object.")


def apply_config_patch(payload: str) -> dict:
    try:
        patch = json.loads(payload)
    except json.JSONDecodeError as e:
        raise ADBError(f"config payload not valid JSON: {e}") from e
    if not isinstance(patch, Mapping):
        raise ADBError("config payload must be a JSON object.")
    _validate_patch(patch)
    merged = _deep_merge(load_config(), patch)
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(merged, ensure_ascii=False, indent=2))
    return merged
