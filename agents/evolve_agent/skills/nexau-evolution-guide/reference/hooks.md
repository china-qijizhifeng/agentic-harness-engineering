## Middleware Hooks

NexAU no longer exposes separate `before_model_hooks`, `after_model_hooks`, or `after_tool_hooks`. Instead, the agent runtime is driven entirely by **middlewares**—Python objects that can plug into every phase of the loop.

### Middleware Interface

A middleware can implement any of these optional methods:

- `before_agent(hook_input)` – run once before the first LLM call to tweak the initial history or seed run-scoped state.
- `after_agent(hook_input)` – run once after execution (success, stop-tool, or error) to finalize the returned response.
- `before_model(hook_input)` – inspect/modify the message list before the LLM call.
- `after_model(hook_input)` – inspect/modify parsed responses and conversation state after the LLM call.
- `before_tool(hook_input)` – adjust tool inputs (or cancel calls) right before execution.
- `after_tool(hook_input)` – inspect/modify tool outputs before they are fed back into the loop.
- `wrap_model_call(params, call_next)` – intercept the low-level LLM invocation (for custom providers, retries, tracing, etc.).
- `wrap_tool_call(params, call_next)` – intercept each tool execution.

Execution order is deterministic:

- `before_agent` / `before_model` / `before_tool`: first → last
- `after_agent` / `after_model` / `after_tool`: last → first
- `wrap_*`: nested, so the first middleware wraps everything else (outermost wins)

### Minimal Example

```python
from nexau.archs.main_sub.execution.hooks import HookResult, Middleware

class AuditMiddleware(Middleware):
    def after_model(self, hook_input):
        print("Model emitted", len(hook_input.parsed_response.tool_calls or []), "tool calls")
        return HookResult.no_changes()

    def after_tool(self, hook_input):
        print("Tool", hook_input.tool_name, "returned", hook_input.tool_output)
        return HookResult.no_changes()

    def wrap_model_call(self, params, call_next):
        print("Calling LLM with", len(params.messages), "messages")
        return call_next(params)
```

### What Can a Middleware Change?

A middleware returns a `HookResult` describing any modifications. Common patterns:

- **Conversation** – supply `messages=[...]` to rewrite the next prompt (add reminders, system notes, or scratchpad content).
- **Parsed response** – supply `parsed_response=...` to add/remove tool calls, toggle parallelism flags, or set `force_continue=True` to keep iterating without new calls.
- **Tool input** – via `before_tool`, return `tool_input=...` to tweak parameters (add defaults, redact secrets) before the tool runs.
- **Tool output** – supply `tool_output=...` to redact/reshape a tool result before it flows back into the conversation.
- **Agent response** – via `after_agent`, return `agent_response="..."` to wrap, redact, or append metadata to the final assistant reply.
- **Agent state** – `hook_input.agent_state` is mutable; you can stash counters, feature flags, or tracing IDs for later iterations.

Returning a `HookResult` makes the intent explicit and lets later middlewares build on your changes without guessing what mutated in-place.

### Examples

```python
from nexau.archs.main_sub.execution.hooks import HookResult, Middleware

class PrefixMiddleware(Middleware):
    def before_model(self, hook_input):
        updated = hook_input.messages + [{
            "role": "system",
            "content": "Reminder: stay within budget.",
        }]
        return HookResult.with_modifications(messages=updated)

class ToolFilter(Middleware):
    def after_model(self, hook_input):
        parsed = hook_input.parsed_response
        if not parsed:
            return HookResult.no_changes()
        parsed.tool_calls = [call for call in parsed.tool_calls if call.tool_name != "system_command"]
        return HookResult.with_modifications(parsed_response=parsed)

class JsonToolNormalizer(Middleware):
    def after_tool(self, hook_input):
        if isinstance(hook_input.tool_output, str):
            return HookResult.with_modifications(tool_output={"text": hook_input.tool_output})
        return HookResult.no_changes()

class ClampInputMiddleware(Middleware):
    def before_tool(self, hook_input):
        updated = dict(hook_input.tool_input)
        updated.setdefault("timeout", 30)
        return HookResult.with_modifications(tool_input=updated)
```

```

### Working with Agent State

`hook_input.agent_state` exposes the live `AgentState` instance. You can read/write custom fields (e.g. `agent_state.context.storage['metrics'] = ...`) to persist values across iterations. Because agent state is shared by every middleware, prefer namespaced keys or dataclasses to avoid collisions.

### Wiring Middlewares

Middlewares are registered through the `middlewares` field on your agent configuration (YAML or code). Example YAML snippet:

```yaml
middlewares:
  - import: my_project.middleware:AuditMiddleware
    params:
      log_file: "/tmp/audit.log"
```

When building agents programmatically, pass actual middleware instances through `Agent(config=AgentConfig(..., middlewares=[...]))`.


## Customizing LLM Calls via Middleware

To customize how NexAU talks to an LLM (swap providers, add caching, manipulate parameters, etc.) you now implement the `wrap_model_call` method on a middleware.

### Why Middleware?

- Works alongside other `before_model` / `after_model` logic.
- Fully nested: the first middleware in your list can wrap every downstream call.
- No special config keys or bespoke plumbing.

### Example: Provider Switch + Metrics

```python
from nexau.archs.main_sub.execution.hooks import Middleware, ModelCallParams
from nexau.archs.main_sub.execution.model_response import ModelResponse

class ProviderSwitchMiddleware(Middleware):
    def __init__(self, fallback_client):
        self.fallback_client = fallback_client

    def wrap_model_call(self, params: ModelCallParams, call_next):
        # Try the default client first
        try:
            return call_next(params)
        except Exception as primary_error:
            print("Primary client failed, falling back:", primary_error)

        # Fallback path – call a completely different provider
        response = self._call_fallback(params)
        print("Fallback response preview:", (response.content or "")[:200])
        return response

    def _call_fallback(self, params: ModelCallParams) -> ModelResponse:
        raw = self.fallback_client.chat.completions.create(
            model="custom-fallback",
            messages=params.messages,
            max_tokens=params.max_tokens,
        )
        return ModelResponse.from_openai_message(raw.choices[0].message)
```

Register the middleware as usual:

```yaml
middlewares:
  - import: my_project.middlewares:ProviderSwitchMiddleware
    params:
      fallback_client: !python/object:my_project.clients:FallbackClient {}
```


### Built-in Middleware

- `LoggingMiddleware`: replaces the old logging hooks and supports both after-model/after-tool logging as well as wrapping model calls to trace custom generators.
- `ContextCompactionMiddleware`: manages conversation context when token limits are approached. See [Context Compaction](./context_compaction.md) for details.
- `LLMFailoverMiddleware`: automatically fails over to backup LLM providers when the primary provider returns matching errors (e.g. 500, 502, 503). Supports multi-level fallback chains and an optional circuit breaker. See below for configuration.
- `LongToolOutputMiddleware`: truncates oversized tool outputs and saves the full content to temporary files via the Sandbox API. Useful for tools that may return very large results (e.g. file search, code analysis). See below for configuration.

You can combine built-in middleware with your own; the manager guarantees the ordering rules described above.

### LLM Failover Middleware

When your primary LLM provider goes down or returns errors, `LLMFailoverMiddleware` intercepts the failure via `wrap_model_call` and retries with backup providers — no changes to `LLMCaller` needed.

**Key features**:

- Trigger on HTTP status codes (e.g. 500, 502, 503, 529) or exception type names (e.g. `RateLimitError`)
- Multiple fallback providers tried in order
- Immutable: creates new `ModelCallParams` per fallback, never mutates the original config
- Optional circuit breaker to skip a failing primary for a cooldown period

**YAML configuration**:

```yaml
middlewares:
  - import: nexau.archs.main_sub.execution.middleware.llm_failover:LLMFailoverMiddleware
    params:
      trigger:
        status_codes: [500, 502, 503, 529]
        exception_types: ["RateLimitError", "InternalServerError"]
      fallback_providers:
        - name: "backup-gateway"
          llm_config:
            base_url: "https://backup.example.com/v1"
            api_key: "sk-backup-xxx"
        - name: "emergency"
          llm_config:
            model: "gpt-4o"
            base_url: "https://emergency.example.com/v1"
            api_key: "sk-emergency-xxx"
            api_type: "openai_chat_completion"
      circuit_breaker:
        failure_threshold: 3
        recovery_timeout_seconds: 60
```

**Python usage**:

```python
from nexau.archs.main_sub.execution.middleware.llm_failover import LLMFailoverMiddleware

failover = LLMFailoverMiddleware(
    trigger={"status_codes": [500, 502, 503], "exception_types": ["RateLimitError"]},
    fallback_providers=[
        {"name": "backup", "llm_config": {"base_url": "https://backup.example.com/v1", "api_key": "sk-xxx"}},
    ],
    circuit_breaker={"failure_threshold": 3, "recovery_timeout_seconds": 60},
)
```

**How it works**:

1. Primary call via `call_next(params)` — if it succeeds, return immediately
2. On failure, check if the exception matches `trigger.status_codes` or `trigger.exception_types`
3. If matched, iterate through `fallback_providers` in order, creating a new `ModelCallParams` with the fallback's config
4. If all providers fail, raise the last exception
5. Circuit breaker (optional): after N consecutive primary failures, skip primary for a cooldown period and go straight to fallbacks

**Notes**:

- Fallback `llm_config` fields are merged on top of the primary config — unspecified fields are inherited
- The `model` field is inherited from primary unless explicitly overridden in the fallback
- See [RFC-0003](../../rfcs/0003-llm-failover-middleware.md) for the full design rationale

### LongToolOutputMiddleware

When a tool returns output whose serialized text exceeds a configurable character threshold, `LongToolOutputMiddleware` automatically:

1. **Truncates** the output, keeping only the first N and last M lines.
2. **Saves** the full output to a temporary file via the Sandbox API (`sandbox.write_file`), so it works transparently with both local and remote (E2B) sandboxes.
3. **Replaces** the original tool output with the truncated version plus a hint pointing to the temp file, so the model can `read_file` it if needed.

**YAML configuration:**

```yaml
middlewares:
  - import: nexau.archs.main_sub.execution.middleware.long_tool_output:LongToolOutputMiddleware
    params:
      max_output_chars: 10000    # Character threshold triggering truncation (default: 10,000)
      head_lines: 50             # Lines to keep from the start (default: 50)
      tail_lines: 30             # Lines to keep from the end (default: 30)
      head_chars: 5000
      tail_chars: 5000
      temp_dir: /tmp/nexau_tool_outputs  # Directory for full outputs (default)
      bypass_tool_names:         # Tools that already handle their own truncation
        - execute_bash
```

**Python configuration:**

```python
from nexau.archs.main_sub.execution.middleware.long_tool_output import LongToolOutputMiddleware

middleware = LongToolOutputMiddleware(
    max_output_chars=10000,
    head_lines=50,
    tail_lines=30,
    temp_dir="/tmp/nexau_tool_outputs",
    bypass_tool_names=["execute_bash"],
)
```

**Configuration parameters:**

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `max_output_chars` | `int` | `10,000` | Character count threshold that triggers truncation |
| `head_lines` | `int` | `50` | Number of leading lines to retain |
| `tail_lines` | `int` | `30` | Number of trailing lines to retain |
| `temp_dir` | `str \| None` | `"/tmp/nexau_tool_outputs"` | Directory for full outputs. Set to `None` to disable file persistence (truncation still applies but no file is saved) |
| `bypass_tool_names` | `list[str] \| None` | `None` | Tool names whose output should never be truncated. Useful for tools that already handle their own truncation |

**How it handles different output types:**

- **String output**: Directly truncated and appended with a hint.
- **Dict with `content` key**: Truncates the `content` field; other keys (e.g. `returnDisplay`) are preserved.
- **Dict with `result` key**: Same as `content`.
- **Dict without known keys**: Serializes the full dict as JSON, truncates, and wraps in `{"content": ...}`.

**Truncated output example:**

```
Line 0000: ...
Line 0001: ...
...
Line 0049: ...

... [150 lines omitted] ...

Line 0190: ...
...
Line 0199: ...

⚠️ [LongToolOutputMiddleware] The full output (17,000 chars, ~200 lines)
has been truncated. The complete output has been saved to:
  /tmp/nexau_tool_outputs/search_files_abc12345_1709568000000.txt
Use the read file tool to view the full content if needed.
```

**Setting `temp_dir` to `None`:**

When `temp_dir` is `None`, truncation still applies but no file is written and no file path appears in the hint. This mode does not require a sandbox to be configured.

```yaml
middlewares:
  - import: nexau.archs.main_sub.execution.middleware.long_tool_output:LongToolOutputMiddleware
    params:
      max_output_chars: 10000
      temp_dir: null  # Truncate only, no file persistence
```

**Bypass list:**

Tools like `execute_bash` already perform their own [output truncation](./sandbox.md#bash-output-truncation) at the sandbox level. Adding them to `bypass_tool_names` avoids double truncation.
