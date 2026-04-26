from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Tuple


class TraceIOError(Exception):
    pass


def _runtime_dir_default() -> Path:
    return Path.home() / ".adb" / "runtime"


def _hash_path(trace_path: Path) -> str:
    resolved = str(trace_path.resolve())
    return hashlib.sha1(resolved.encode("utf-8")).hexdigest()[:16]


def _normalize_openai_messages(src_path: Path) -> dict:
    try:
        data = json.loads(src_path.read_text())
    except json.JSONDecodeError as e:
        raise TraceIOError(f"{src_path}: not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise TraceIOError(f"{src_path}: top-level must be a JSON object")
    messages = data.get("messages")
    if not isinstance(messages, list) or not messages:
        raise TraceIOError(f"{src_path}: messages must be a non-empty array")
    trace_id = str(data.get("trace_id") or src_path.stem)
    return {"trace_id": trace_id, "messages": messages}


def _normalize_in_memory_tracer(src_path: Path) -> dict:
    try:
        data = json.loads(src_path.read_text())
    except json.JSONDecodeError as e:
        raise TraceIOError(f"{src_path}: not valid JSON: {e}") from e
    spans = data.get("spans")
    if not isinstance(spans, list) or not spans:
        raise TraceIOError(f"{src_path}: spans must be a non-empty array")

    # Strategy: the longest input.messages across all spans is the final
    # conversation *before* the last assistant reply. Append each span's
    # output assistant message in order to reconstruct the full sequence.
    llm_spans = [s for s in spans if isinstance(s, dict) and "input" in s and "output" in s]
    if not llm_spans:
        raise TraceIOError(f"{src_path}: no llm.call spans found")
    base_msgs = max(
        (s["input"].get("messages") or [] for s in llm_spans),
        key=lambda m: len(m),
    )
    messages = list(base_msgs)
    for s in llm_spans:
        for m in (s["output"].get("messages") or []):
            messages.append(m)
    # Dedup consecutive duplicates (base may already contain an assistant).
    deduped = []
    for m in messages:
        if deduped and deduped[-1] == m:
            continue
        deduped.append(m)
    # Also remove non-consecutive duplicates that are already present in base.
    base_set_idx = len(list(base_msgs))
    final = list(base_msgs)
    seen = set(json.dumps(m, sort_keys=True) for m in base_msgs)
    for m in deduped[base_set_idx:]:
        key = json.dumps(m, sort_keys=True)
        if key not in seen:
            final.append(m)
            seen.add(key)
    trace_id = str(data.get("trace_id") or src_path.stem)
    return {"trace_id": trace_id, "messages": final}




def _normalize_langfuse(src_path: Path) -> dict:
    try:
        data = json.loads(src_path.read_text())
    except json.JSONDecodeError as e:
        raise TraceIOError(f"{src_path}: not valid JSON: {e}") from e
    observations = data.get("observations") or []
    generations = [
        o for o in observations
        if isinstance(o, dict) and str(o.get("type", "")).upper() == "GENERATION"
    ]
    if not generations:
        raise TraceIOError(f"{src_path}: no GENERATION observations")
    generations.sort(key=lambda o: o.get("startTime", ""))

    base_msgs = max(
        (o.get("input") or [] for o in generations if isinstance(o.get("input"), list)),
        key=lambda m: len(m),
        default=[],
    )
    messages = list(base_msgs)
    for o in generations:
        out = o.get("output")
        if isinstance(out, dict) and "role" in out:
            messages.append({"role": out["role"], "content": out.get("content", "")})
        elif isinstance(out, str):
            messages.append({"role": "assistant", "content": out})

    # Consecutive dedup.
    consec = []
    for m in messages:
        if consec and consec[-1] == m:
            continue
        consec.append(m)
    # Then remove appended duplicates of base_msgs.
    base_len = len(base_msgs)
    final = list(base_msgs)
    seen = {json.dumps(m, sort_keys=True) for m in base_msgs}
    for m in consec[base_len:]:
        key = json.dumps(m, sort_keys=True)
        if key not in seen:
            final.append(m)
            seen.add(key)

    trace_id = str(data.get("id") or data.get("trace_id") or src_path.stem)
    return {"trace_id": trace_id, "messages": final}

def normalize_trace(
    trace_path: Path | str,
    *,
    trace_type: str = "openai_messages",
    runtime_dir: Path | None = None,
) -> Tuple[Path, str]:
    src = Path(trace_path)
    if not src.exists():
        raise TraceIOError(f"trace file not found: {src}")
    runtime_dir = runtime_dir or _runtime_dir_default()
    runtime_dir.mkdir(parents=True, exist_ok=True)

    if trace_type == "openai_messages":
        normalized = _normalize_openai_messages(src)
    elif trace_type == "in_memory_tracer":
        normalized = _normalize_in_memory_tracer(src)
    elif trace_type == "langfuse":
        normalized = _normalize_langfuse(src)
    else:
        raise TraceIOError(f"unknown trace type: {trace_type}")

    out_path = runtime_dir / f"{_hash_path(src)}.normalized.json"
    out_path.write_text(json.dumps(normalized, ensure_ascii=False))
    return out_path, normalized["trace_id"]
