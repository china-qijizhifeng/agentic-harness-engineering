from __future__ import annotations

import argparse
import json
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional, Sequence

from agent_debugger_core.cli.config_store import apply_config_patch, ADBError
from agent_debugger_core.runtime.runner import run_agent, RunnerResult, RunnerError
from agent_debugger_core.trace_io import normalize_trace, TraceIOError
from agent_debugger_core.download import download_langfuse_trace, DownloadError

DEFAULT_ASK_QUESTION = "Why is this trace so slow?"


def _flatten_nested_list_attrs(args: argparse.Namespace, attrs: Sequence[str]) -> None:
    for attr in attrs:
        vals = getattr(args, attr, None)
        if isinstance(vals, list) and vals and isinstance(vals[0], list):
            setattr(args, attr, [x for group in vals for x in group])


def _emit_failure(command: str, exc: Exception, output_format: str) -> int:
    payload = {"status": "failed", "command": command, "error": str(exc)}
    if output_format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"[adb] {exc}", file=sys.stderr)
    return 1


def _cmd_config(args) -> int:
    try:
        merged = apply_config_patch(" ".join(args.payload))
    except ADBError as e:
        return _emit_failure("config", e, args.output_format)
    if args.output_format == "json":
        print(json.dumps({"status": "success", "command": "config", "config": merged}, ensure_ascii=False, indent=2))
    else:
        print(f"[adb] config updated → {merged}")
    return 0


def _emit_ask(trace_path: Path, trace_id: str, question: str,
              result: RunnerResult, output_format: str) -> None:
    if output_format == "json":
        payload = {
            "status": "success",
            "command": "ask",
            "trace_path": str(trace_path),
            "trace_id": trace_id,
            "question": question,
            "request_id": "trace",
            "response": result.answer or "",
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(result.answer or "")


def _cmd_ask(args) -> int:
    if args.records_file:
        return _cmd_ask_records(args)
    trace_paths = list(args.trace_path or [])
    questions = list(args.question or []) or [DEFAULT_ASK_QUESTION]
    if not trace_paths:
        return _emit_failure("ask", ADBError("-t/--trace-path required"), args.output_format)
    try:
        normalized_entries = [
            normalize_trace(Path(p), trace_type=args.trace_type)
            for p in trace_paths
        ]
    except TraceIOError as e:
        return _emit_failure("ask", ADBError(f"trace: {e}"), args.output_format)

    primary_path = Path(trace_paths[0])
    primary_trace_id = normalized_entries[0][1]
    try:
        result = run_agent(
            trace_paths=[p for p, _ in normalized_entries],
            mode="ask",
            question=questions[0],
        )
    except RunnerError as e:
        return _emit_failure("ask", ADBError(str(e)), args.output_format)
    _emit_ask(primary_path, primary_trace_id, questions[0], result, args.output_format)
    return 0


def _cmd_ask_records(args) -> int:
    path = Path(args.records_file)
    if not path.exists():
        return _emit_failure("ask", ADBError(f"records file not found: {path}"), args.output_format)

    lines = [line for line in path.read_text().splitlines() if line.strip()]

    def _process(line_text: str) -> dict:
        try:
            rec = json.loads(line_text)
            queries = rec.get("queries") or [DEFAULT_ASK_QUESTION]
            trace_obj = rec.get("traces") or {}
            if not (isinstance(trace_obj, dict) and trace_obj.get("messages")):
                return {"status": "failed", "command": "ask",
                        "error": "records line missing traces.messages"}
            tmp = tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False, prefix="adb_rec_",
            )
            json.dump(trace_obj, tmp, ensure_ascii=False)
            tmp.close()
            normalized_path, trace_id = normalize_trace(
                Path(tmp.name), trace_type="openai_messages",
            )
            result = run_agent(
                trace_paths=[normalized_path],
                mode="ask",
                question=queries[0],
            )
            return {
                "status": "success", "command": "ask",
                "trace_path": tmp.name, "trace_id": trace_id,
                "question": queries[0], "request_id": "trace",
                "response": result.answer or "",
            }
        except (TraceIOError, RunnerError, ADBError) as e:
            return {"status": "failed", "command": "ask", "error": str(e)}
        except Exception as e:
            return {"status": "failed", "command": "ask", "error": f"unexpected: {e}"}

    parallelism = max(1, int(args.parallelism or 1))
    results = []
    with ThreadPoolExecutor(max_workers=parallelism) as pool:
        futures = [pool.submit(_process, ln) for ln in lines]
        for fut in as_completed(futures):
            results.append(fut.result())

    for r in results:
        if args.output_format == "json":
            print(json.dumps(r, ensure_ascii=False))
        else:
            if r["status"] == "success":
                print(r.get("response", ""))
            else:
                print(f"[adb] {r['error']}")
    return 0


def _render_check_text(trace_path: Path, trace_id: str, result: RunnerResult) -> str:
    lines = [
        "# ADB Check Result",
        "",
        f"- Trace ID: `{trace_id}`",
        f"- Trace Path: `{trace_path}`",
        f"- Issues Count: `{len(result.issues)}`",
    ]
    if result.issues:
        lines.extend(["", "## Issues", ""])
        for i, issue in enumerate(result.issues, 1):
            lines.extend([
                f"### {i}. {issue['issue_type']}",
                f"- Message Index: `{issue['message_index']}`",
                "",
                "**Summary**", "", str(issue["summary"]), "",
                "**Evidence**", "", str(issue["evidence"]), "",
            ])
    if result.response:
        lines.extend(["", result.response])
    return "\n".join(lines)


def _cmd_check(args) -> int:
    trace_path = Path(args.trace_path)
    try:
        normalized_path, trace_id = normalize_trace(trace_path, trace_type=args.trace_type)
    except TraceIOError as e:
        return _emit_failure("check", ADBError(f"trace: {e}"), args.output_format)
    try:
        result = run_agent(trace_paths=[normalized_path], mode="check")
    except RunnerError as e:
        return _emit_failure("check", ADBError(str(e)), args.output_format)

    if args.output_format == "json":
        payload = {
            "status": "success", "command": "check",
            "trace_path": str(trace_path), "trace_id": trace_id,
            "request_id": "trace",
            "issues_count": len(result.issues),
            "issues": result.issues,
            "response": result.response or "",
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(_render_check_text(trace_path, trace_id, result))
    return 0


def _cmd_download(args) -> int:
    if args.download_type != "langfuse":
        return _emit_failure("download", ADBError(f"unsupported --type: {args.download_type}"), args.output_format)
    try:
        path = download_langfuse_trace(url=args.langfuse_url, ak=args.ak, sk=args.sk)
    except DownloadError as e:
        return _emit_failure("download", ADBError(str(e)), args.output_format)
    if args.output_format == "json":
        print(json.dumps({"status": "success", "command": "download",
                          "trace_path": str(path)}, ensure_ascii=False, indent=2))
    else:
        print(str(path))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="adb", description="agent debugger CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_config = sub.add_parser("config", help="Update local adb config JSON.")
    p_config.add_argument("payload", nargs="+")
    p_config.add_argument("--format", dest="output_format", choices=("text", "json"), default="text")
    p_config.set_defaults(func=_cmd_config)

    p_ask = sub.add_parser("ask", help="Run QA agent against one or more traces.")
    p_ask.add_argument("-t", "--trace-path", action="append", default=[], nargs="+")
    p_ask.add_argument("-q", "--question", action="append", default=[], nargs="+")
    p_ask.add_argument("-f", "--records-file")
    p_ask.add_argument("-j", "--parallelism", type=int, default=10)
    p_ask.add_argument("--format", dest="output_format", choices=("text", "json"), default="text")
    p_ask.add_argument(
        "--trace-type", dest="trace_type", default="openai_messages",
        choices=("openai_messages", "langfuse", "in_memory_tracer"),
    )
    p_ask.set_defaults(func=_cmd_ask)

    p_check = sub.add_parser("check", help="Run QC pipeline on a trace.")
    p_check.add_argument("-t", "--trace-path", required=True)
    p_check.add_argument("--format", dest="output_format", choices=("text", "json"), default="text")
    p_check.add_argument(
        "--trace-type", dest="trace_type", default="openai_messages",
        choices=("openai_messages", "langfuse", "in_memory_tracer"),
    )
    p_check.set_defaults(func=_cmd_check)

    p_download = sub.add_parser("download", help="Download and clean a Langfuse trace.")
    p_download.add_argument("--type", dest="download_type", default="langfuse")
    p_download.add_argument("--ak", default="")
    p_download.add_argument("--sk", default="")
    p_download.add_argument("--format", dest="output_format", choices=("text", "json"), default="text")
    p_download.add_argument("langfuse_url")
    p_download.set_defaults(func=_cmd_download)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _flatten_nested_list_attrs(args, ("trace_path", "question"))
    output_format = getattr(args, "output_format", "text") or "text"
    try:
        return int(args.func(args))
    except ADBError as e:
        return _emit_failure(getattr(args, "command", ""), e, output_format)


if __name__ == "__main__":
    raise SystemExit(main())
