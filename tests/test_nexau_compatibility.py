import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("relative_path", [
    "agents/code_agent_simple/code_agent.yaml",
    "agents/evolve_agent/evolve_agent.yaml",
    "agents/explore_agent/source_agent/agent.yaml",
    "agents/explore_agent/web_agent/agent.yaml",
    "agents/evolve_agent/skills/agent-debugger-cli/_source/agent_debugger_core/runtime/agent_config.yaml",
])
def test_agent_configuration_loads_with_pinned_nexau(tmp_path, relative_path):
    env = dict(os.environ, LLM_MODEL="compat-test", LLM_API_KEY="test-only",
               LLM_BASE_URL="https://llm.example.invalid/v1", LLM_API_TYPE="openai_chat_completion",
               EVOLVE_WORK_DIR=str(tmp_path), AHE_HOME=str(ROOT),
               EXPLORE_AGENT_MODEL="compat-test", EXPLORE_AGENT_WORK_DIR=str(tmp_path))
    script = """
import sys
from pathlib import Path
import yaml
path = Path(sys.argv[1])
sys.path.insert(0, str(Path(sys.argv[2]) / 'agents/evolve_agent'))
sys.path.insert(0, str(path.parent))
from nexau.archs.config.config_loader import load_agent_config
agent = load_agent_config(str(path))
expected = {tool['name'] for tool in yaml.safe_load(path.read_text())['tools']}
assert expected <= {tool.name for tool in agent.config.tools}
agent.sync_cleanup()
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(ROOT / relative_path), str(ROOT)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_debugger_completion_and_trace_with_real_nexau(tmp_path, monkeypatch, caplog):
    import httpx
    from openai import OpenAI
    from nexau import Agent, AgentConfig
    from agent_debugger_core.runtime.runner import _parse_run_output
    from trace_converter import extract_trace_data_from_inmemory_dump

    monkeypatch.syspath_prepend(str(ROOT / "agents/evolve_agent"))
    for key, value in {
        "LLM_MODEL": "compat-test", "LLM_API_KEY": "test-only",
        "LLM_BASE_URL": "https://llm.example.invalid/v1",
        "LLM_API_TYPE": "openai_chat_completion", "EVOLVE_WORK_DIR": str(tmp_path),
    }.items():
        monkeypatch.setenv(key, value)
    payload = {"mode": "ask", "answer": "compatible"}
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "compat-completion", "object": "chat.completion", "created": 0,
            "model": "compat-test", "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "tool_calls": [{
                    "id": "complete-1", "type": "function", "function": {
                        "name": "complete_task", "arguments": json.dumps({"result": json.dumps(payload)}),
                    },
                }],
            }}],
        })

    config = AgentConfig.from_yaml(ROOT / "agents/evolve_agent/evolve_agent.yaml")
    config.llm_config.stream = False
    config.max_iterations = 2
    client = OpenAI(
        api_key="test-only", base_url="https://llm.example.invalid/v1",
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    agent = Agent(config=config, openai_client=client)
    try:
        output = agent.run(message="Finish the compatibility check.", context={
            "date": "2026-10-30", "username": "compat-test", "iteration": 1,
            "working_directory": str(tmp_path), "workspace_path": "workspace", "adb_llm": None,
        })
        assert _parse_run_output(output) == payload
        assert len(requests) == 1
        spans = agent.config.tracers[0].dump_traces()
        cleaned = extract_trace_data_from_inmemory_dump(spans, jsonable_output=True)
        assert "complete_task" in json.dumps(cleaned)
        assert not any("middleware" in record.message and "failed" in record.message for record in caplog.records)
    finally:
        agent.sync_cleanup()
