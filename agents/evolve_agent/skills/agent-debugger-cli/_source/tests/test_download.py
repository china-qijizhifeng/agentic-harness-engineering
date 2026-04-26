import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_debugger_core.download import download_langfuse_trace, DownloadError


def test_download_writes_cleaned_json(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    fake_client = MagicMock()
    fake_client.get_trace.return_value = MagicMock(
        id="trace_abc",
        project_id="proj_01",
        observations=[
            MagicMock(type="GENERATION", start_time="2026-04-23T10:00:00Z",
                      input=[{"role": "user", "content": "hi"}],
                      output={"role": "assistant", "content": "hello"}),
        ],
    )
    with patch("agent_debugger_core.download._make_client", return_value=fake_client):
        out_path = download_langfuse_trace(
            url="https://lf.example/project/proj_01/traces/trace_abc",
            ak="pk", sk="sk",
        )
    data = json.loads(Path(out_path).read_text())
    assert data["trace_id"] == "trace_abc"
    assert data["messages"][-1]["role"] == "assistant"


def test_download_bad_url(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(DownloadError):
        download_langfuse_trace(url="https://nope", ak="", sk="")
