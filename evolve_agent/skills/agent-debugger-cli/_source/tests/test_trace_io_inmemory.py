import json
from pathlib import Path

from agent_debugger_core.trace_io import normalize_trace


FIX = Path(__file__).parent / "fixtures"


def test_inmemory_tracer_flattens_to_messages(tmp_path):
    normalized_path, trace_id = normalize_trace(
        FIX / "in_memory_tracer_sample.json",
        trace_type="in_memory_tracer",
        runtime_dir=tmp_path,
    )
    data = json.loads(Path(normalized_path).read_text())
    assert trace_id == "imt_trace_01"
    roles = [m["role"] for m in data["messages"]]
    # System + user + assistant (ans 1) + user (followup) + assistant (ans 2).
    assert roles == ["system", "user", "assistant", "user", "assistant"]
