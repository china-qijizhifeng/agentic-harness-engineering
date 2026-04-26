import json
from pathlib import Path

import pytest

from agent_debugger_core.trace_io import normalize_trace, TraceIOError


FIX = Path(__file__).parent / "fixtures"


def test_openai_messages_passthrough(tmp_path):
    normalized_path, trace_id = normalize_trace(
        FIX / "openai_messages_sample.json",
        trace_type="openai_messages",
        runtime_dir=tmp_path,
    )
    data = json.loads(Path(normalized_path).read_text())
    assert data["messages"][0]["role"] == "system"
    assert len(data["messages"]) == 3
    assert trace_id == "trace_sample"


def test_openai_messages_missing_messages_raises(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"trace_id": "x", "messages": []}))
    with pytest.raises(TraceIOError):
        normalize_trace(bad, trace_type="openai_messages", runtime_dir=tmp_path)


def test_openai_messages_missing_file_raises(tmp_path):
    with pytest.raises(TraceIOError):
        normalize_trace(tmp_path / "nope.json", trace_type="openai_messages", runtime_dir=tmp_path)
