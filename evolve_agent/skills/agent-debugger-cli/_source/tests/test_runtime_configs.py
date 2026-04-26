from pathlib import Path
import yaml

from agent_debugger_core import runtime as runtime_pkg


RUNTIME_DIR = Path(runtime_pkg.__file__).parent


def test_system_prompt_contains_iteration_budget():
    txt = (RUNTIME_DIR / "system_prompt.md").read_text()
    assert "20 tool-calling iterations" in txt
    assert "complete_task" in txt


def test_agent_config_shape():
    cfg = yaml.safe_load((RUNTIME_DIR / "agent_config.yaml").read_text())
    assert cfg["type"] == "agent"
    assert cfg["max_iterations"] == 25
    assert "complete_task" in cfg["stop_tools"]
    tool_names = {t["name"] for t in cfg["tools"]}
    assert tool_names == {
        "read_file", "write_file", "replace", "search_file_content",
        "glob", "list_directory", "run_shell_command",
        "web_search", "web_read", "complete_task",
    }


def test_tool_descriptions_all_present():
    tool_yamls = set((RUNTIME_DIR / "tool_descriptions").glob("*.tool.yaml"))
    stems = {p.name for p in tool_yamls}
    assert stems == {
        "read_file.tool.yaml", "write_file.tool.yaml", "replace.tool.yaml",
        "search_file_content.tool.yaml", "Glob.tool.yaml",
        "list_directory.tool.yaml", "run_shell_command.tool.yaml",
        "WebSearch.tool.yaml", "WebFetch.tool.yaml", "complete_task.tool.yaml",
    }
