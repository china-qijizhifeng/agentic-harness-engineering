import subprocess
import sys


def test_adb_help_lists_all_subcommands():
    result = subprocess.run(
        [sys.executable, "-m", "agent_debugger_core.cli.adb", "--help"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    for sub in ("config", "ask", "check", "download"):
        assert sub in result.stdout, f"missing subcommand in help: {sub}"


def test_adb_unknown_subcommand_errors():
    result = subprocess.run(
        [sys.executable, "-m", "agent_debugger_core.cli.adb", "not-a-cmd"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode != 0


def test_trace_path_flattens_space_separated():
    from agent_debugger_core.cli.adb import _build_parser, _flatten_nested_list_attrs
    parser = _build_parser()
    args = parser.parse_args(["ask", "-t", "a.json", "b.json", "-q", "q1"])
    _flatten_nested_list_attrs(args, ("trace_path", "question"))
    assert args.trace_path == ["a.json", "b.json"]
    assert args.question == ["q1"]


def test_trace_path_flattens_repeated_flag():
    from agent_debugger_core.cli.adb import _build_parser, _flatten_nested_list_attrs
    parser = _build_parser()
    args = parser.parse_args(["ask", "-t", "a.json", "-t", "b.json", "-q", "q1", "-q", "q2"])
    _flatten_nested_list_attrs(args, ("trace_path", "question"))
    assert args.trace_path == ["a.json", "b.json"]
    assert args.question == ["q1", "q2"]


def test_check_trace_path_unaffected():
    from agent_debugger_core.cli.adb import _build_parser, _flatten_nested_list_attrs
    parser = _build_parser()
    args = parser.parse_args(["check", "-t", "single.json"])
    _flatten_nested_list_attrs(args, ("trace_path", "question"))
    assert args.trace_path == "single.json"
