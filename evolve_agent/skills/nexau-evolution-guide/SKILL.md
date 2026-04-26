---
name: nexau-evolution-guide
description: NexAU agent evolution reference for a simple starting agent. Use when adding tools, middleware, sub-agents, or skills during evolution. Covers all available components, YAML config schema, and creation guides. Reference docs in reference/ for deep dives.
---

# NexAU Evolution Guide — Simple Agent Starting Point

The code agent starts **simple**: a single `run_shell_command` tool, no middleware, no skills, no sub-agents. Evolution progressively adds components to improve performance. This guide covers **what you can add** and **how to add it**.

## Current Agent Baseline

```yaml
# workspace/code_agent.yaml (starting state)
type: agent
tools:
  - name: run_shell_command
    yaml_path: ./tool_descriptions/run_shell_command.tool.yaml
    binding: tools.shell_tools:run_shell_command
# No middleware, no skills, no sub_agents
```

Everything below is **available to add** during evolution.

## Validation

After any change, **always validate**:

```bash
python evolve_agent/skills/nexau-evolution-guide/scripts/validate_agent.py workspace/code_agent.yaml
```

---

## Adding Tools

The agent only starts with `run_shell_command`. Add tools to give the agent more capabilities.

### Available Built-in Tools

These tool implementations already exist in `workspace/tools/` and can be registered by adding entries to `code_agent.yaml`:

| Tool | Binding | Purpose |
|------|---------|---------|
| `read_file` | `tools.file_tools:read_file` | Read file with line numbers, offset/limit pagination |
| `write_file` | `tools.file_tools:write_file` | Write/overwrite a file |
| `replace` | `tools.file_tools:replace` | Find-and-replace in files (exact → flexible → regex fallback) |
| `search_file_content` | `tools.file_tools:search_file_content` | Search file contents (ripgrep-style) |
| `glob` | `tools.file_tools:glob` | Find files by glob pattern |
| `list_directory` | `tools.file_tools:list_directory` | List directory contents |
| `run_shell_command` | `tools.shell_tools:run_shell_command` | Execute shell commands (already registered) |
| `BackgroundTaskManage` | `tools:background_task_manage_tool` | Manage background processes |
| `web_search` | `tools.web_tools:google_web_search` | Google web search |
| `web_read` | `tools.web_tools:web_fetch` | Fetch web page content |
| `save_memory` | `tools.session_tools:save_memory` | Save notes to memory file |
| `write_todos` | `tools.session_tools:write_todos` | Write TODO list |
| `complete_task` | `tools.session_tools:complete_task` | Signal task completion (stop tool) |

### How to Register a Tool

Add to `tools:` in `workspace/code_agent.yaml`:

```yaml
tools:
  - name: read_file
    yaml_path: ./tool_descriptions/read_file.tool.yaml
    binding: tools.file_tools:read_file
```

Each tool needs:
1. **Tool YAML** (`tool_descriptions/*.tool.yaml`) — schema + description for the LLM
2. **Python binding** (`tools/*.py`) — the implementation
3. **Registration** in `code_agent.yaml` under `tools:`

If adding `complete_task`, also set `stop_tools: [complete_task]`.

### Creating a New Tool

**Step 1 — YAML definition** (`workspace/tool_descriptions/my_tool.tool.yaml`):

```yaml
type: tool
name: my_tool
description: >-
  What the tool does, when to use it, and any caveats.
input_schema:
  type: object
  properties:
    param_name:
      type: string
      description: Parameter description
  required:
    - param_name
  additionalProperties: false
  $schema: http://json-schema.org/draft-07/schema#
```

**Step 2 — Python implementation** (`workspace/tools/my_module.py`):

```python
from typing import Any

def my_tool(*, param_name: str, agent_state=None) -> dict[str, Any]:
    # agent_state.get_sandbox() — sandbox for file/shell operations
    # agent_state.global_storage — GlobalStorage object (NOT a dict, use .get()/.set())
    # agent_state.get_global_value(key, default) / .set_global_value(key, value)
    # Omit agent_state/sandbox from signature if not needed
    result = f"Processed {param_name}"
    return {
        "content": result,           # Sent to LLM as tool result
        "returnDisplay": result,     # Shown to user
    }
```

**Step 3 — Register** in `code_agent.yaml`:

```yaml
tools:
  - name: my_tool
    yaml_path: ./tool_descriptions/my_tool.tool.yaml
    binding: tools.my_module:my_tool
```

> **⚠️ CRITICAL**: All three steps required. Missing any one = tool unavailable or agent crashes.

### Tool Modification Ideas

| Problem | Action |
|---------|--------|
| `replace` OLD_STRING_NOT_FOUND | Add fuzzy match with `difflib.get_close_matches` |
| Shell output too large | Edit `_truncate_shell_output()` to keep head+tail |
| Agent uses `python` not `python3` | Add shim in `run_shell_command.py` |
| Tool description unclear | Edit `tool_descriptions/*.tool.yaml` (high-impact, low-risk) |

---

## Adding Middleware

The agent starts with **no middleware**. Middleware hooks into the execution pipeline to modify behavior at various points. Register middleware in `code_agent.yaml` under `middlewares:`.

Order matters: `before_*` hooks run top-to-bottom; `after_*` hooks run bottom-to-top.


### AgentState & GlobalStorage API

Middleware and tools access runtime state via `agent_state`. **`global_storage` is NOT a dict** — it is a thread-safe `GlobalStorage` object. Do NOT use dict methods like `[]`, `setdefault()`, `in`, or `pop()`.

**AgentState API:**

| Method | Description |
|--------|-------------|
| `agent_state.get_global_value(key, default)` | Read from global storage |
| `agent_state.set_global_value(key, value)` | Write to global storage |
| `agent_state.get_context_value(key, default)` | Read from context |
| `agent_state.set_context_value(key, value)` | Write to context |
| `agent_state.get_sandbox()` | Get sandbox instance |
| `agent_state.agent_name` | Agent name (str) |
| `agent_state.agent_id` | Agent ID (str) |

**GlobalStorage API** (`agent_state.global_storage`):

| Method | Description |
|--------|-------------|
| `.get(key, default=None)` | Read a value |
| `.set(key, value)` | Write a value |
| `.update(dict)` | Batch write |
| `.delete(key)` | Delete a key |
| `.keys()` | List all keys |
| `.items()` | List all key-value pairs |
| `.lock_key(key)` | Context manager for exclusive key access |

**Common mistakes:**
```python
# ❌ WRONG — GlobalStorage is not a dict
storage = agent_state.global_storage
storage.setdefault("key", [])     # AttributeError
storage["key"] = value            # TypeError
if "key" in storage:              # TypeError

# ✅ CORRECT
storage = agent_state.global_storage
val = storage.get("key", [])      # Read with default
storage.set("key", val)           # Write
```

### Creating Custom Middleware

**Step 1 — Create** `workspace/middleware/my_middleware.py`:

```python
from nexau.archs.main_sub.execution.hooks import (
    Middleware, HookResult,
    BeforeModelHookInput, AfterModelHookInput,
    AfterToolHookInput, ModelCallParams, ModelCallFn,
)
from nexau.core.messages import Message, Role, TextBlock

class MyMiddleware(Middleware):
    def __init__(self, *, param_a: str = "default"):
        self.param_a = param_a

    def before_model(self, hook_input: BeforeModelHookInput) -> HookResult:
        # hook_input.messages, hook_input.current_iteration, hook_input.max_iterations
        return HookResult.no_changes()

    def after_model(self, hook_input: AfterModelHookInput) -> HookResult:
        # hook_input.parsed_response, hook_input.messages
        # Can modify: messages, parsed_response, force_continue
        return HookResult.no_changes()

    def after_tool(self, hook_input: AfterToolHookInput) -> HookResult:
        # hook_input.tool_name, hook_input.tool_output, hook_input.sandbox
        return HookResult.no_changes()

    def wrap_model_call(self, params: ModelCallParams, call_next: ModelCallFn):
        # MUST call call_next(params) to proceed
        return call_next(params)
```

**Step 2 — Register** in `code_agent.yaml`:

```yaml
middlewares:
  - import: middleware.my_middleware:MyMiddleware
    params:
      param_a: "value"
```

### Hook Points Reference

| Hook | When | Can modify | Use case |
|------|------|-----------|----------|
| `before_agent` | Before execution loop | messages | One-time setup |
| `after_agent` | After execution loop | — | Cleanup |
| `before_model` | Before each LLM call | messages | Inject reminders, compact context |
| `after_model` | After LLM response | messages, parsed_response, force_continue | Post-process, force retry |
| `before_tool` | Before tool execution | tool inputs | Validate/transform arguments |
| `after_tool` | After tool execution | tool_output | Truncate/enrich output |
| `wrap_model_call` | Wraps LLM call | params | Error handling, failover |
| `wrap_tool_call` | Wraps tool call | params | Timeout, retry |

### Middleware LLM Access

Middleware can make its own LLM calls. At runtime, these env vars are always available:

| Variable | Value |
|----------|-------|
| `LLM_API_KEY` | API key for current experiment |
| `LLM_BASE_URL` | LLM API endpoint |
| `LLM_MODEL` | Model identifier |

**In `wrap_model_call`** (preferred — reuse existing client):

```python
from nexau.archs.main_sub.execution.llm_caller import LLMCaller

class MyLLMMiddleware(Middleware):
    def wrap_model_call(self, params: ModelCallParams, call_next: ModelCallFn):
        llm_caller = LLMCaller(
            params.openai_client, params.llm_config,
            retry_attempts=1, middleware_manager=None,  # None to avoid recursion
        )
        # Make side call, then: return call_next(params)
```

**In other hooks** (create standalone client):

```python
import os, openai
from nexau.archs.llm.llm_config import LLMConfig

client = openai.OpenAI(api_key=os.environ["LLM_API_KEY"], base_url=os.environ["LLM_BASE_URL"])
llm_config = LLMConfig(model=os.environ["LLM_MODEL"], ...)
caller = LLMCaller(client, llm_config, retry_attempts=2, middleware_manager=None)
```

> **Always set `middleware_manager=None`** to avoid infinite recursion.

---

## Adding Sub-Agents

Sub-agents are child agents with their own prompt, tools, middleware, and isolated context window.

### When to Use

| Use sub-agent | Don't use sub-agent |
|--------------|-------------------|
| Task needs deep, focused context | Simple one-shot operation |
| Subtask needs different tools/prompt | Parent's tools are sufficient |
| Isolate failure from parent | Overhead not justified |

### Creating a Sub-Agent

**Step 1 — Create config** (`workspace/sub_agents/verifier/agent.yaml`):

```yaml
type: agent
name: verifier
max_iterations: 50
max_context_tokens: 128000
system_prompt: ./prompt.md
system_prompt_type: jinja
tool_call_mode: openai

llm_config:
  model: ${env.LLM_MODEL}
  base_url: ${env.LLM_BASE_URL}
  api_key: ${env.LLM_API_KEY}
  max_tokens: 16000
  temperature: 0.3
  stream: false
  api_type: openai_chat_completion

tools:
  - name: run_shell_command
    yaml_path: ../tool_descriptions/run_shell_command.tool.yaml
    binding: tools.shell_tools:run_shell_command
  - name: complete_task
    yaml_path: ../tool_descriptions/complete_task.tool.yaml
    binding: tools.session_tools:complete_task

stop_tools: [complete_task]
```

**Step 2 — Create prompt** (`workspace/sub_agents/verifier/prompt.md`)

**Step 3 — Register** in `code_agent.yaml`:

```yaml
sub_agents:
  - name: verifier
    config_path: ./sub_agents/verifier/agent.yaml
```

> **⚠️ CRITICAL**: Framework auto-injects `RecallSubAgent` tool when `sub_agents` is non-empty. Do NOT add it manually — causes duplicate tool error.

---

## Adding Skills

Skills are modular knowledge packages loaded at runtime via `LoadSkill`.

### Structure

```
workspace/skills/my-skill/
├── SKILL.md              ← Required: YAML frontmatter + content
├── scripts/              ← Optional
└── references/           ← Optional
```

### SKILL.md Format

```yaml
---
name: my-skill-name
description: When to use this skill and what it provides.
---

# Skill content loaded when agent calls LoadSkill...
```

### Registration

```yaml
skills:
  - ./skills/my-skill      # Each folder listed individually
  - ./skills/another-skill  # Does NOT recursively scan!
```

> **⚠️ CRITICAL**: Without registration in `skills:`, the agent cannot discover or load the skill.

---

## YAML Config Reference (`code_agent.yaml`)

```yaml
type: agent
name: string
max_iterations: 300
max_context_tokens: 200000
tool_call_mode: "openai"             # "openai" | "xml" | "anthropic"
system_prompt: ./systemprompt.md
system_prompt_type: jinja            # "string" | "file" | "jinja"

llm_config:                          # 🚫 DO NOT MODIFY
  model: ${env.LLM_MODEL}
  # ... (hands-off)

tools: [...]
stop_tools: [complete_task]
middlewares: [...]
sub_agents: [...]
skills: [...]

tracers:
  - import: nexau.archs.tracer.adapters.in_memory:InMemoryTracer
```

| Field | Type | Default | Safe to modify |
|-------|------|---------|---------------|
| `name` | str | — | ✅ Safe (cosmetic) |
| `max_iterations` | int | 100 | ✅ Safe |
| `max_context_tokens` | int | 128000 | ⚠️ Only increase |
| `tool_call_mode` | str | `"openai"` | ⚠️ Risky |
| `system_prompt` | str | — | ✅ Safe |
| `llm_config` | dict | — | 🚫 Hands-off |
| `tools` | list | `[]` | ✅ Safe |
| `stop_tools` | set | `{}` | ✅ Safe |
| `middlewares` | list | `None` | ✅ Safe |
| `sub_agents` | list | `[]` | ✅ Safe |
| `skills` | list | `[]` | ✅ Safe |
| `sandbox_config` | dict | `None` | ❌ Never |
| `tracers` | list | `[]` | ❌ Never |


## Reference Docs

Detailed NexAU v0.3.9 documentation is available in `reference/`:

| File | Content |
|------|---------|
| `hooks.md` | Middleware base classes, hooks API, MiddlewareManager |
| `tools.md` | Tool system architecture |
| `agents.md` | Agent core concepts |
| `llms.md` | LLM configuration |
| `sandbox.md` | Sandbox system |
| `skills.md` | Skill system |
