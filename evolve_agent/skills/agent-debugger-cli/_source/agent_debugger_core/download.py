from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


class DownloadError(Exception):
    pass


_URL_RE = re.compile(
    r"^https?://[^/]+/project/(?P<project>[^/]+)/traces/(?P<trace>[^/?#]+)"
)


def _parse_trace_url(url: str) -> tuple[str, str]:
    m = _URL_RE.match(url)
    if not m:
        raise DownloadError(f"not a recognized Langfuse trace URL: {url}")
    return m.group("project"), m.group("trace")


def _make_client(ak: str, sk: str, url: str):
    try:
        from langfuse import Langfuse  # type: ignore
    except ImportError as e:
        raise DownloadError(
            "langfuse SDK not installed; `pip install langfuse>=2.0.0`"
        ) from e
    host = url.split("/project/")[0]
    return Langfuse(public_key=ak or None, secret_key=sk or None, host=host)


def _observation_to_message_pair(obs: Any) -> list[dict]:
    msgs: list[dict] = []
    inp = getattr(obs, "input", None)
    if isinstance(inp, list):
        msgs.extend(inp)
    out = getattr(obs, "output", None)
    if isinstance(out, dict) and "role" in out:
        msgs.append({"role": out["role"], "content": out.get("content", "")})
    elif isinstance(out, str):
        msgs.append({"role": "assistant", "content": out})
    return msgs


def download_langfuse_trace(*, url: str, ak: str, sk: str) -> Path:
    project_id, trace_id = _parse_trace_url(url)
    client = _make_client(ak, sk, url)
    trace = client.get_trace(trace_id)
    generations = [
        o for o in (getattr(trace, "observations", None) or [])
        if str(getattr(o, "type", "")).upper() == "GENERATION"
    ]
    generations.sort(key=lambda o: getattr(o, "start_time", "") or "")

    messages: list[dict] = []
    if generations:
        longest = max(
            (getattr(o, "input", None) or [] for o in generations if isinstance(getattr(o, "input", None), list)),
            key=lambda m: len(m),
            default=[],
        )
        messages.extend(list(longest))
        for o in generations:
            for m in _observation_to_message_pair(o):
                if not messages or messages[-1] != m:
                    messages.append(m)

    out_dir = Path.home() / ".adb" / "traces" / project_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{trace_id}.cleaned.json"
    out_path.write_text(json.dumps({"trace_id": trace_id, "messages": messages},
                                   ensure_ascii=False, indent=2))
    return out_path
