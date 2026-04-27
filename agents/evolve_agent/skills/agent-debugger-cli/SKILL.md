---
name: agent-debugger-cli
description: Install and use the bundled agent debugger CLI source to configure adb LLM settings, download Langfuse traces, ask QA questions about traces, and run adb check quality analysis. Use when the task is about agent-debugger CLI workflows, trace debugging, or explaining how adb is configured.
compatibility: Requires Python 3.9+, pip, local shell access, and optional network access for Langfuse trace download or model calls.
metadata:
  author: agent-debugger
  bundled-source: _source/
---

# Agent Debugger CLI

Use this skill when you need the standalone `adb` CLI that ships inside the bundled `_source/` package in this directory.

## Files in this skill

- `_source/`: bundled open-source package that exposes the `adb` command.

## Install

The package is declared as a path source in the project root `pyproject.toml`, so a top-level `uv sync` already installs it. To install standalone from this skill directory:

```bash
python -m pip install ./_source
adb --help
```

If you are upgrading an existing install, use:

```bash
python -m pip install --upgrade --force-reinstall ./_source
```

## LLM configuration

Configure the LLM once with `adb config`:

```bash
adb config '{"llm":{"model":"gpt-4.1","base_url":"https://api.openai.com/v1","api_key":"<your-key>"}}'
```

If you need reasoning-capable OpenAI responses models such as `gpt-5.4` or `codex`, keep the reasoning hint nested under `llm.reasoning.effort`:

```bash
adb config '{"llm":{"model":"gpt-5-codex","base_url":"https://api.openai.com/v1","api_key":"<your-key>","reasoning":{"effort":"high"}}}'
```

This writes local config to `~/.adb/adb_cli_config.json`.

Resolution order for LLM settings is:

1. `adb config` saved values in `config.llm`
2. `QA_MODEL_NAME` / `QA_BASE_URL` / `QA_API_KEY`
3. `LLM_MODEL` / `LLM_BASE_URL` / `LLM_API_KEY`

If `llm.model` or `llm.base_url` is missing after resolution, `adb ask/check` will fail fast.

ADB also supports local-runner config:

```bash
adb config '{"adb":{"runner":"local","venv_dir":"/abs/path/to/adb_local_venv"}}'
```

Or override at runtime:

```bash
export ADB_RUNNER_MODE=local
export ADB_VENV_DIR=/abs/path/to/adb_local_venv
```

`adb config '{"adb":{"runner":"api"}}'` exists, but current `ask/check` support is implemented for `local` runner.

You can also persist QA agent overrides in `config.qa`. Reserved keys are:

- `agent_config`
- `multi_trace_agent_config`
- `artifact_root`
- `agent_override`
- `multi_trace_agent_override`

Example:

```bash
adb config '{
  "qa": {
    "agent_config": "/abs/path/to/agent_debugger_qa.yaml",
    "multi_trace_agent_config": "/abs/path/to/multi_trace_qa_debugger.yaml",
    "artifact_root": "/abs/path/to/adb_artifacts",
    "agent_override": {
      "tools": [{"name":"trace_read"}]
    },
    "multi_trace_agent_override": {
      "tools": [{"name":"trace_ls"}]
    }
  }
}'
```

Model routing notes for local `adb ask`:

- models containing `claude` are routed to `anthropic_chat_completion`
- `gpt-5.4` is routed to `openai_responses`
- models containing `codex` are routed to `openai_responses`
- `codex` defaults to `reasoning.effort=medium` unless you override it

## Trace workflow

`adb ask` and `adb check` accept standard cleaned traces, and they also accept local JSON files that only contain an OpenAI-style `messages` field, for example:

```json
{
  "messages": [
    {"role": "system", "content": "You are helpful."},
    {"role": "user", "content": "Please analyze this conversation."}
  ]
}
```

`messages` must be a non-empty array.

When the input is not already a cleaned/messages-style trace, pass `--trace-type` explicitly on `adb ask` or `adb check`.

- Default: `--trace-type openai_messages`
- Use `--trace-type langfuse` for raw Langfuse trace JSON
- Use `--trace-type in_memory_tracer` for NexAU `InMemoryTracer` dumps

Examples:

```bash
adb ask -t /abs/path/to/langfuse_raw_trace.json --trace-type langfuse \
  -q "What is the main issue in this trace?"

adb check -t /abs/path/to/inmemory_dump.json --trace-type in_memory_tracer --format json
```

If the file is already a cleaned trace or a JSON object with a usable `messages` field, keep the default `openai_messages`.
`--trace-type` is only for `--trace-path`; `adb ask --records-file` does not support it, so records should already contain normalized messages traces.

### 1. Download a Langfuse trace

```bash
adb download --type langfuse --ak <public-key> --sk <secret-key> \
  https://<langfuse-host>/project/<projectId>/traces/<traceId>
```

The command writes cleaned traces under `~/.adb/traces/<projectId>/` and prints the cleaned trace path by default.

### 2. Ask a question about a trace

```bash
adb ask -t ~/.adb/traces/<projectId>/<traceId>.cleaned.json \
  -q "What is the main issue in this trace?"
```

One trace can also take multiple questions in one call:

```bash
adb ask -t ~/.adb/traces/<projectId>/<traceId>.cleaned.json \
  -q "What is the main issue?" "What evidence supports it?"
```

Multiple trace files can be passed after `-t` to run multi-trace QA:

```bash
adb ask \
  -t ~/.adb/traces/<projectId>/trace_a.cleaned.json ~/.adb/traces/<projectId>/trace_b.cleaned.json \
  -q "Compare these traces and explain the main differences."
```

For batch execution, use a JSONL records file where each line contains `{queries, traces}`:

```bash
adb ask -f /abs/path/to/records.jsonl -j 3 --format json
```

Example record:

```json
{"queries":["What is the main issue?"],"traces":{"trace_id":"trace_123","messages":[{"role":"user","content":"hello"}]}}
```

If `-q/--question` is omitted, the current built-in default question text is `Why is this trace so slow?`.

Constraints:

- multiple queries plus multiple traces are not supported in a single `adb ask -t ... -q ...` call; use `adb ask -f /abs/path/to/records.jsonl`
- `adb ask --records-file` does not support `--trace-type`; records should already contain normalized `messages` traces

Default stdout is only the answer text. Use `--format json` when you need stable fields:

```json
{
  "status": "success",
  "command": "ask",
  "trace_path": "/abs/path/to/trace.cleaned.json",
  "trace_id": "trace_123",
  "question": "What is the main issue in this trace?",
  "request_id": "trace_123",
  "response": "The main issue is ..."
}
```

### 3. Run quality check

```bash
adb check -t ~/.adb/traces/<projectId>/<traceId>.cleaned.json
```

This runs the QC pipeline and returns issue summaries. Use `--format json` when you want machine-readable output:

```bash
adb check -t ~/.adb/traces/<projectId>/<traceId>.cleaned.json --format json
```

Default stdout is a Markdown report that includes trace metadata, `issues_count`, and one section per issue with fields such as `issue_type`, `summary`, `evidence`, and `message_index`. A typical text output looks like:

```md
# ADB Check Result

- Trace ID: `trace_123`
- Trace Path: `/abs/path/to/trace.cleaned.json`
- Issues Count: `1`

## Issues

### 1. 工具错误
- Message Index: `7`

**Summary**

The agent called the wrong tool.

**Evidence**

Message 7 calls weather instead of search.
```

JSON mode includes the full issue list:

```json
{
  "status": "success",
  "command": "check",
  "trace_path": "/abs/path/to/trace.cleaned.json",
  "trace_id": "trace_123",
  "request_id": "trace_123",
  "issues_count": 2,
  "issues": [
    {
      "issue_type": "工具错误",
      "summary": "The agent called the wrong tool.",
      "evidence": "Message 7 calls weather instead of search.",
      "message_index": 7
    }
  ],
  "response": "Overall, the trace has tool-use issues."
}
```

The stable wrapper fields are `status/command/trace_path/trace_id/request_id/issues_count/issues/response`. Each item in `issues` is a QC pipeline issue object. The stable core fields are `issue_type`, `summary`, `evidence`, and `message_index`; extra fields such as `issue_category`, `issue_name`, `start_idx`, `end_idx`, or `chunk_summary` may also appear.

If a command fails and `--format json` is set, stdout is:

```json
{
  "status": "failed",
  "command": "ask or check",
  "error": "..."
}
```

## Runtime notes

- `adb ask/check` default to local runner mode.
- The local runner auto-creates a dedicated venv under `~/.adb/venvs/adb_local_venv`.
- Downloaded traces are stored under `~/.adb/traces/`.
- Local runner temp files are stored under `~/.adb/runtime/`.
- `adb config '{"adb":{"runner":"api"}}'` exists, but current `ask/check` support is implemented for `local` runner.

## Minimal end-to-end example

```bash
python -m pip install ./_source
adb config '{"llm":{"model":"gpt-4.1","base_url":"https://api.openai.com/v1","api_key":"<your-key>"}}'
TRACE_PATH="$(adb download --type langfuse --ak <public-key> --sk <secret-key> https://<langfuse-host>/project/<projectId>/traces/<traceId>)"
adb ask -t "$TRACE_PATH" -q "Summarize the main issue in this trace."
adb check -t "$TRACE_PATH" --format json
```
