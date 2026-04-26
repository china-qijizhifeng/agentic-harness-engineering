import json
from pathlib import Path

from agent_debugger_core.trace_io import normalize_trace


FIX = Path(__file__).parent / "fixtures"


def test_langfuse_projects_to_messages(tmp_path):
    normalized_path, trace_id = normalize_trace(
        FIX / "langfuse_sample.json",
        trace_type="langfuse",
        runtime_dir=tmp_path,
    )
    data = json.loads(Path(normalized_path).read_text())
    assert trace_id == "lf_trace_01"
    roles = [m["role"] for m in data["messages"]]
    assert roles == ["system", "user", "assistant", "user", "assistant"]
    assert data["messages"][-1]["content"] == "a2"
