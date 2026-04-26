# 🛠️ Core Concepts: Tools

Tools are the functions that an agent can execute to interact with the outside world, such as searching the web, reading files, or running code.

## Built-in Tools

NexAU comes with a variety of pre-built tools for common tasks.

#### File Tools (`nexau.archs.tool.builtin.file_tools`)
- **read_file**: Read text files (with pagination). For images/videos, use `read_visual_file`.
- **read_visual_file**: Read image and video files for multimodal LLMs. Requires a model with vision support.
- **write_file**, **replace**, **apply_patch**: Write and edit files.
- **glob**, **list_directory**, **read_many_files**, **search_file_content**: Find and search files.

#### Web Tools (`nexau.archs.tool.builtin.web_tools`)
- **google_web_search**: Search the web.
- **web_fetch**: Fetch and parse content from a URL.

#### Shell Tools (`nexau.archs.tool.builtin.shell_tools`)
- **run_shell_command**: Execute shell commands.

#### Session Tools (`nexau.archs.tool.builtin.session_tools`)
- **write_todos**, **complete_task**, **save_memory**, **ask_user**: Task management, persistence, user interaction.

### How to use Use these built-in tools
If you use python to create Agent:

```python
from nexau import Agent, AgentConfig, Tool
from nexau.archs.tool.builtin.session_tools import write_todos
from nexau.archs.tool.builtin.web_tools import google_web_search, web_fetch

# Create the tool instance
# Note: you need to create the yaml files for tool config, you may refer to examples/deep_research/tools for examples

web_search_tool = Tool.from_yaml(
    str(script_dir / 'tools/WebSearch.yaml'),
    binding=google_web_search,
)
web_read_tool = Tool.from_yaml(
    str(script_dir / 'tools/WebRead.yaml'),
    binding=web_fetch,
)
todo_write_tool = Tool.from_yaml(
    str(script_dir / 'tools/TodoWrite.tool.yaml'),
    binding=write_todos,
)

# Add it to your agent's tool list
agent_config = AgentConfig(
    name="web_agent",
    tools=[web_search_tool, web_read_tool, todo_write_tool],
    # ... other agent config (llm_config, system_prompt, etc.)
)
agent = Agent(config=agent_config)
```

If you use agent yaml config, you can add these lines to the config file:

```yaml
tools:
  - name: web_search
    yaml_path: ./tools/WebSearch.yaml
    binding: nexau.archs.tool.builtin.web_tools:google_web_search
  - name: web_read
    yaml_path: ./tools/WebRead.yaml
    binding: nexau.archs.tool.builtin.web_tools:web_fetch
  - name: todo_write
    yaml_path: ./tools/TodoWrite.tool.yaml
    binding: nexau.archs.tool.builtin.session_tools:write_todos
```

## Creating Custom Tools

You can easily extend an agent's capabilities by creating your own custom tools.

### extra_kwargs (preset parameters)
- Use `Tool.from_yaml(..., extra_kwargs=...)` or a YAML `extra_kwargs` block to preset fixed arguments (e.g., `base_url`/`api_key`/`model`) so callers can omit them.
- Call-time arguments with the same name override preset values. Reserved keys `agent_state` and `global_storage` are not allowed in `extra_kwargs`.
- Extra fields are not blocked by schema validation and are passed to the tool function; if the function signature does not accept them, a `TypeError` will be raised. To reject unknown fields up front, add `additionalProperties: false` in the tool’s `input_schema`.

**Step 1: Define the Tool's Python Function**

Create a standard Python function with type hints. The docstring will be used to inform the agent how the tool works.

```python
# my_tools/calculator.py

def simple_calculator(expression: str) -> str:
    """
    Evaluates a simple mathematical expression.
    Supports addition (+), subtraction (-), multiplication (*), and division (/).

    Args:
        expression: The mathematical expression to evaluate (e.g., "10 + 5*2").

    Returns:
        The result of the calculation as a string, or an error message.
    """
    try:
        # Note: Using eval() is unsafe for untrusted input. This is for demonstration.
        result = eval(expression, {"__builtins__": None}, {})
        return str(result)
    except Exception as e:
        return f"Error: {e}"
```

**Step 2: Create a YAML Configuration**

The YAML file defines the tool's name, description, and input schema for the agent.

**File: `tools/SimpleCalculator.tool.yaml`**
```yaml
type: tool
name: SimpleCalculator
description: >-
  A tool to evaluate simple mathematical expressions like "10 + 5*2".
  It supports addition, subtraction, multiplication, and division.

input_schema:
  type: object
  properties:
    expression:
      type: string
      description: The mathematical string to evaluate.
  required:
    - expression
  additionalProperties: false
  $schema: [http://json-schema.org/draft-07/schema#](http://json-schema.org/draft-07/schema#)
```

**Step 3: Use the Tool in Your Agent**

Load the tool from its YAML file and bind it to the Python function you created.

```python
from nexau import Agent, AgentConfig, Tool
from my_tools.calculator import simple_calculator

# Create the tool instance
calculator_tool = Tool.from_yaml(
    "tools/SimpleCalculator.tool.yaml",
    binding=simple_calculator
)

# Add it to your agent's tool list
agent_config = AgentConfig(
    name="calculator_agent",
    tools=[calculator_tool],
    # ... other agent config (llm_config, system_prompt, etc.)
)
agent = Agent(config=agent_config)
```

If you use agent yaml config, you can add these lines to the config file:

```yaml
tools:
  - name: simple_calculator
    yaml_path: ./tools/SimpleCalculator.tool.yaml
    binding: my_tools.calculator.simple_calculator:simple_calculator
```

## Tool Return Values: `returnDisplay`

When a tool returns a dictionary, you can include a `returnDisplay` field to provide a short, human-readable summary for the frontend UI. The framework automatically strips `returnDisplay` before sending the result to the LLM, so it won't consume extra tokens.

```python
def my_search_tool(query: str, agent_state=None) -> dict:
    results = perform_search(query)
    return {
        "content": json.dumps(results),                    # Sent to LLM
        "returnDisplay": f"Found {len(results)} results",  # Shown in frontend only
    }
```

**How it works:**
- `content` — the full tool output, forwarded to the LLM as the tool result.
- `returnDisplay` — a concise summary streamed to the frontend for display, then stripped before the LLM sees it.

This is the same pattern used by all NexAU built-in tools (file tools, web tools, shell tools, etc.). Use it in your custom tools whenever the full output is verbose but you want a clean one-liner in the UI.

## Tool Output Truncation

Tools like file search or code analysis can produce very large outputs that waste LLM context tokens. NexAU provides two complementary truncation mechanisms:

### 1. Sandbox-level truncation (bash commands)

The `execute_bash` command automatically redirects stdout/stderr to temporary files and truncates the output when it exceeds a configurable threshold. This is handled at the sandbox level — see [Sandbox — Bash Output Truncation](../advanced-guides/sandbox.md#bash-output-truncation) for configuration details.

### 2. Middleware-level truncation (all tools)

For tools that don't handle their own truncation, `LongToolOutputMiddleware` can be added to the agent's middleware stack. It truncates any tool output exceeding `max_output_chars`, saves the full content to a temp file, and provides a hint so the model can read the full output if needed.

```yaml
middlewares:
  - import: nexau.archs.main_sub.execution.middleware.long_tool_output:LongToolOutputMiddleware
    params:
      max_output_chars: 10000
      head_lines: 50
      tail_lines: 30
      bypass_tool_names:
        - execute_bash  # Already truncated at sandbox level
```

See [Middleware Hooks — LongToolOutputMiddleware](../advanced-guides/hooks.md#longtooloutputmiddleware) for full configuration reference.


## Lazy loading long tool descriptions via Skills

When a tool is marked with `as_skill: true`, NexAU exposes it progressively:
- In `xml` mode, the prompt only includes the brief `skill_description`, and the agent uses `LoadSkill` to fetch the full detail.
- In `openai` and `anthropic` modes, the model gets the brief `skill_description` plus the JSON Schema up front, then calls `LoadSkill` only if it needs the full description.

Refer to [Skills](../advanced-guides/skills.md) for details about the Skill mechanism.
