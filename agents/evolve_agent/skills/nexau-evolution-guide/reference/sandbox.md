# Sandbox System

The NexAU framework provides a powerful sandbox system for secure code execution and file operations. Sandboxes isolate tool execution from the host system, improving security and enabling safe deployment in production environments.

## Overview

The sandbox system consists of three main components:

1. **BaseSandbox**: Abstract interface defining sandbox operations
2. **Sandbox Implementations**: Concrete implementations (LocalSandbox, E2BSandbox)
3. **SandboxManager**: Manages sandbox lifecycle (start, stop, pause, restore) and state persistence

### Available Sandbox Backends

- **LocalSandbox**: Executes commands directly on the local system (development/testing only)
- **E2BSandbox**: Cloud-based sandboxes using [E2B](https://e2b.dev) (production-ready)

## Bash Output Truncation

When executing bash commands, the sandbox automatically redirects stdout and stderr to temporary files on disk and applies smart truncation to prevent oversized outputs from consuming excessive LLM context tokens.

### How It Works

1. **Output redirection**: Every `execute_bash` call creates a unique output directory under `/tmp/nexau_bash_tool_results/{uuid}/`, containing `command.txt`, `stdout.txt`, and `stderr.txt`.
2. **Smart truncation**: After command execution, `smart_truncate_output()` checks whether the combined stdout + stderr exceeds a character threshold. If so, each stream is independently truncated — keeping the first N and last M characters — with an omitted marker and a hint pointing to the full file.
3. **Full output preserved**: The untruncated content is always available in the temp files, so the agent can use `read_file` to access the complete output when needed.

**Truncated output example:**

```
[first 5,000 chars of stdout]

... [15,000 characters omitted] ...
(Full output: /tmp/nexau_bash_tool_results/a1b2c3d4/stdout.txt)

[last 5,000 chars of stdout]
```

### CommandResult Fields

The `CommandResult` dataclass returned by `execute_bash` includes truncation metadata:

| Field | Type | Description |
|-------|------|-------------|
| `stdout` | `str` | Truncated stdout (if truncation applied) |
| `stderr` | `str` | Truncated stderr (if truncation applied) |
| `truncated` | `bool` | Whether the output was truncated |
| `original_stdout_length` | `int \| None` | Full stdout length before truncation |
| `original_stderr_length` | `int \| None` | Full stderr length before truncation |
| `output_dir` | `str \| None` | Directory containing full output files |
| `stdout_file` | `str \| None` | Full path to `stdout.txt` |
| `stderr_file` | `str \| None` | Full path to `stderr.txt` |

### Configuring Truncation Parameters

Truncation thresholds are configurable via `sandbox_config`:

```yaml
sandbox_config:
  type: local  # or e2b
  output_char_threshold: 10000   # Total char threshold for truncation (default: 10,000)
  truncate_head_chars: 5000      # Characters to keep from the start (default: 5,000)
  truncate_tail_chars: 5000      # Characters to keep from the end (default: 5,000)
```

Programmatic configuration:

```python
config = AgentConfig(
    name="my_agent",
    sandbox_config={
        "type": "local",
        "output_char_threshold": 20000,
        "truncate_head_chars": 8000,
        "truncate_tail_chars": 8000,
    }
)
```

### Implementation Differences

| Aspect | LocalSandbox | E2BSandbox |
|--------|-------------|------------|
| Redirection | Python-level (`Popen(stdout=fout)`) | Shell-level (`{ cmd; } > stdout.txt 2> stderr.txt`) |
| File location | Local filesystem | Remote sandbox filesystem |
| Reading output | `Path.read_text()` | `sandbox._filesystem.read()` |
| Truncation | Same `smart_truncate_output()` logic | Same `smart_truncate_output()` logic |

> **Tip**: Because `execute_bash` already handles its own truncation, you should add `execute_bash` to the `bypass_tool_names` list when using `LongToolOutputMiddleware` to avoid double truncation. See [Middleware Hooks — LongToolOutputMiddleware](./hooks.md#longtooloutputmiddleware) for details.

## How to Configure Sandbox

Sandboxes are configured through the agent's YAML configuration file or programmatically via `AgentConfig`.

### YAML Configuration

Add a `sandbox_config` section to your agent configuration:

```yaml
name: my_agent
llm_config:
  model: gpt-4

sandbox_config:
  type: local
  work_dir: /path/to/working/directory # Optional, default to current directory
  persist_sandbox: true  # Optional, default to true. If false, sandbox is destroyed after agent.run
```

### E2B Sandbox Configuration

For production use with E2B sandboxes:

```yaml
sandbox_config:
  type: e2b
  work_dir: /home/user # Optional, default to `/home/user` in E2B
  template: base  # E2B template name, default to "base"
  timeout: 300    # Timeout in seconds, default to 300
  api_key: ${env.E2B_API_KEY}  # Use environment variable
  persist_sandbox: true  # Optional, default to true. If false, sandbox is destroyed after agent.run
  metadata:
    project: my_project
    environment: production
  envs:
    CUSTOM_VAR: value
```

**E2B Configuration Options:**

- `template`: E2B template/image to use (default: "base")
- `timeout`: Sandbox timeout in seconds (default: 300)
- `api_key`: E2B API key (can use environment variable `E2B_API_KEY`)
- `api_url`: Custom E2B API URL (optional)
- `persist_sandbox`: Whether to persist sandbox state after agent.run (default: `true`). If `false`, sandbox is destroyed after each agent run
- `metadata`: Custom metadata dictionary for the sandbox
- `envs`: Environment variables to set in the sandbox

### Programmatic Configuration

Same as YAML configuration, but using `AgentConfig`.

```python
from nexau.archs.main_sub.config import AgentConfig

config = AgentConfig(
    name="my_agent",
    llm_config={"model": "gpt-4"},
    sandbox_config={
        "type": "local"
    }
)
```

## Sandbox State Persistence

Sandbox state is automatically persisted to the session storage, allowing sandboxes to be restored across agent restarts.

### How State Persistence Works

1. **Automatic Persistence**: When a sandbox is created or modified, its state is saved to the session
2. **State Restoration**: On agent restart, the sandbox manager attempts to restore from saved state
3. **Reconnection**: For E2B sandboxes, the system reconnects to existing cloud instances

### Example: State Lifecycle

```python
# Create agent with sandbox
agent = Agent.from_config("config.yaml")
# Sandbox created with ID: e2b_abc123
# State automatically saved to session

# Pause the sandbox (preserves state in session)
agent.sandbox_manager.pause()
# Sandbox paused, state persisted to session_manager

# Later, restart the sandbox
agent.sandbox_manager.start(
    session_manager=agent._session_manager,
    user_id=agent._user_id,
    session_id=agent._session_id,
    sandbox_config=agent.config.sandbox_config or {}
)
# Sandbox state loaded from session_manager
# Automatically restored and reconnected to E2B sandbox: e2b_abc123
```

### How Pause and Start Work Internally

The `BaseSandboxManager` implements a sophisticated state management system:

#### Pause Mechanism

When `pause()` is called:

1. **Backend-Specific Pause**: Each sandbox backend implements its own pause logic:
   - **E2BSandbox**: Calls `sandbox.beta_pause()` to pause the cloud container
   - **LocalSandbox**: Simply returns `True` (no actual pause needed for local execution)

2. **State Preservation**: The sandbox state remains in `_session_context`, which stores:
   - `session_manager`: Reference to the session manager
   - `user_id`: Current user ID
   - `session_id`: Current session ID
   - `sandbox_config`: Configuration used to create the sandbox
   - `upload_assets`: List of uploaded assets

3. **Persistent Storage**: The sandbox state is already persisted to the session via `persist_sandbox_state()`:
   ```python
   sandbox_state = sandbox.dict()  # Serialize sandbox attributes
   sandbox_state["sandbox_type"] = sandbox.__class__.__name__
   await session_manager.update_session_sandbox(
       user_id=user_id,
       session_id=session_id,
       sandbox_state=sandbox_state
   )
   ```

#### Start Mechanism

When `start()` is called after a pause:

1. **Load Saved State**: The manager calls `load_sandbox_state()`:
   ```python
   session = await session_manager.get_session(user_id, session_id)
   if session and session.sandbox_state:
       return session.sandbox_state  # Contains sandbox_id, work_dir, etc.
   ```

2. **Attempt Restoration**: If state exists, try to restore the sandbox:
   - **E2BSandbox**: Reconnects to existing cloud instance using `Sandbox.connect(sandbox_id=...)`
   - **LocalSandbox**: Recreates sandbox instance with saved configuration

3. **Fallback to New Creation**: If restoration fails, creates a new sandbox instance

4. **Asset Re-upload**: Re-uploads any assets that were previously uploaded

#### Auto-Resume on Access

The `instance` property implements automatic resumption:

```python
@property
def instance(self) -> TSandbox | None:
    if self.start_future is not None:
        self.start_future.result()  # Wait for async start to complete

    if not self.is_running():
        if self._session_context:
            # Auto-resume using saved context
            logger.warning("Sandbox is not running. Try resuming it...")
            self.start_no_wait(**self._session_context)
            if self.start_future is not None:
                self.start_future.result()
        else:
            raise SandboxError("Sandbox is not running. Please run `start_no_wait` first.")

    return self._instance
```

This means when tools try to access the sandbox after a pause, it automatically resumes without manual intervention.

#### Asynchronous Operations

- **`start_no_wait()`**: Starts sandbox in background thread using `ThreadPoolExecutor`
- **`pause_no_wait()`**: Pauses sandbox in background thread
- **`start_future`**: Tracks async start operation, allows waiting for completion

This design ensures:
- **Non-blocking initialization**: Agent can continue while sandbox starts
- **Automatic recovery**: Sandbox resumes automatically when accessed
- **State continuity**: All sandbox state persists across pause/resume cycles
- **Session integration**: State is tied to session for multi-session support

## Sandbox and Session Relationship

### Are Sandbox and Session Required Together?

**No, they are independent but complementary:**

- **Sandbox without Session**: You can create standalone sandboxes without sessions
- **Session without Sandbox**: Sessions can exist without sandboxes
- **Together**: When used together, sandbox state is persisted to the session for continuity

### Using Sandbox with Sessions

You can manually integrate sandbox with session manager for state persistence:

```python
from nexau.archs.sandbox import LocalSandboxManager, E2BSandboxManager
from nexau.archs.session import SessionManager
from nexau.archs.session.orm import InMemoryDatabaseEngine

# Initialize session manager
session_manager = SessionManager(
    engine=InMemoryDatabaseEngine.get_shared_instance()
)

# Create sandbox manager
sandbox_manager = E2BSandboxManager(
    template="base",
    timeout=300
)

# Start sandbox with session integration
user_id = "user_123"
session_id = "session_456"
sandbox_config = {
    "type": "e2b",
}

sandbox = sandbox_manager.start(
    session_manager=session_manager,
    user_id=user_id,
    session_id=session_id,
    sandbox_config=sandbox_config
)

# Sandbox state is automatically persisted to session
# On next start() call with same session_id, sandbox will be restored

# Use the sandbox
result = sandbox.execute_bash("echo 'Hello from sandbox'")
print(result.stdout)

# Pause to save state
sandbox_manager.pause()

# Later, restart with same session - state will be restored
sandbox = sandbox_manager.start(
    session_manager=session_manager,
    user_id=user_id,
    session_id=session_id,
    sandbox_config=sandbox_config
)
# Sandbox reconnected to existing instance
```

### Using Sandbox without Sessions

You can use sandbox manager without session persistence by passing `session_manager=None`:

```python
from nexau.archs.sandbox import LocalSandboxManager, E2BSandboxManager

# Create sandbox manager
sandbox_manager = LocalSandboxManager()

# Start sandbox without session manager (no state persistence)
sandbox = sandbox_manager.start(
    session_manager=None,  # Disables state persistence
    user_id="user_123",
    session_id="session_456",
    sandbox_config={"type": "local"}
)

# Use sandbox operations
result = sandbox.execute_bash("ls -la")
print(result.stdout)

# Pause the sandbox
sandbox_manager.pause()

# Start again - creates a NEW sandbox (state not persisted)
sandbox = sandbox_manager.start(
    session_manager=None,
    user_id="user_123",
    session_id="session_456",
    sandbox_config={"type": "local"}
)
# This is a fresh sandbox, not restored from previous state
```

**Important**: When `session_manager=None`:
- `persist_sandbox_state()` does nothing (state is not saved)
- `load_sandbox_state()` returns `None` (no state to restore)
- Each `start()` call creates a completely new sandbox instance
- Useful for stateless, ephemeral sandbox usage

## How to Connect to an Existing E2B Sandbox

For E2B sandboxes, you can reconnect to an existing sandbox instance by configuring the `sandbox_id` in your configuration. The sandbox manager will automatically restore the connection.

### Configure sandbox_id in YAML

```yaml
sandbox_config:
  type: e2b
  sandbox_id: e2b_abc123xyz  # ID of existing E2B sandbox
  template: base
  timeout: 300
```

### Programmatic Configuration

```python
from nexau.archs.sandbox import E2BSandboxManager
from nexau.archs.session import SessionManager
from nexau.archs.session.orm import InMemoryDatabaseEngine

# Initialize session manager
session_manager = SessionManager(
    engine=InMemoryDatabaseEngine.get_shared_instance()
)

# Create sandbox manager
sandbox_manager = E2BSandboxManager(
    template="base",
    timeout=300
)

# Start with existing sandbox_id - will reconnect instead of creating new
sandbox_config = {
    "type": "e2b",
    "sandbox_id": "e2b_abc123xyz"  # Existing E2B sandbox ID
}

sandbox = sandbox_manager.start(
    session_manager=session_manager,
    user_id="user_123",
    session_id="session_456",
    sandbox_config=sandbox_config
)
# Sandbox manager detects sandbox_id and reconnects to existing instance
```

### How It Works

When `sandbox_id` is provided in the configuration:

1. **E2B Reconnection**: The `E2BSandboxManager` calls `Sandbox.connect(sandbox_id=...)` to reconnect to the existing cloud instance
2. **State Preservation**: All files, environment variables, and running processes in the sandbox are preserved
3. **No New Creation**: A new sandbox is not created; the existing one is reused
4. **Automatic Fallback**: If the sandbox_id is invalid or the sandbox no longer exists, an error is raised

This is useful for:
- **Long-running tasks**: Reconnect to sandboxes with ongoing processes
- **Shared sandboxes**: Multiple agents/sessions can connect to the same sandbox
- **Cost optimization**: Reuse existing sandboxes instead of creating new ones

## Configuring Sandbox Templates (E2B)

E2B sandboxes use templates (Docker images) to define the execution environment.

### Using Built-in Templates

E2B provides several pre-built templates:

```yaml
sandbox_config:
  type: e2b
  template: base  # Basic Ubuntu environment
```

### Using Custom Templates

Build custom E2B templates from Docker images using the E2B Python SDK:

```python
import asyncio
from e2b import AsyncTemplate
from dotenv import load_dotenv

load_dotenv()

# Create template from Docker image
template = AsyncTemplate().from_image(
    image="mp-bp-cn-shanghai.cr.volces.com/north-prod-images/sandbox_fusion/bp_sandbox:2.0",
    username="xxxxx", # harbor username
    password="xxxxx" # harbor password
)

async def main():
    # Build the template with custom configuration
    await AsyncTemplate.build(
        template,
        alias="north_test",  # Template name/alias
        memory_mb=1024,      # Memory allocation
        skip_cache=True,     # Skip Docker cache
        on_build_logs=lambda log: print(str(log)),  # Build progress logs
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"An error occurred: {e}")
```

After building, reference the template in your configuration:

```yaml
sandbox_config:
  type: e2b
  template: north_test  # Use the alias from build
  api_key: ${env.E2B_API_KEY}
```

## Using Sandbox in Custom Tools

Tools can access the sandbox instance to perform secure file operations and code execution.

### Basic Tool with Sandbox

```python
from nexau.archs.sandbox import BaseSandbox
from nexau.archs.main_sub.agent_state import AgentState

def file_analyzer(file_path: str, agent_state: AgentState) -> dict:
    """Analyze a file in the sandbox."""

    # Get sandbox instance from agent_state
    sandbox: BaseSandbox | None = agent_state.get_sandbox()

    # Check if file exists
    if not sandbox.file_exists(file_path):
        return {"error": f"File not found: {file_path}"}

    # Read file content
    result = sandbox.read_file(file_path)
    if result.status != SandboxStatus.SUCCESS:
        return {"error": result.error}

    # Get file info
    info = sandbox.get_file_info(file_path)

    return {
        "path": file_path,
        "size": info.size,
        "encoding": info.encoding,
        "lines": len(result.content.split("\n")) if result.content else 0
    }
```

### File Operations in Tools

```python
from nexau.archs.sandbox import BaseSandbox, SandboxStatus
from nexau.archs.main_sub.agent_state import AgentState

def process_data(input_file: str, output_file: str, agent_state: AgentState) -> str:
    """Process data file in sandbox."""

    # Get sandbox instance from agent_state
    sandbox: BaseSandbox | None = agent_state.get_sandbox()

    # Read input file
    read_result = sandbox.read_file(input_file)
    if read_result.status != SandboxStatus.SUCCESS:
        return f"Error reading file: {read_result.error}"

    # Process content
    content = read_result.content
    processed = content.upper()  # Example processing

    # Write output file
    write_result = sandbox.write_file(
        output_file,
        processed,
        create_directories=True
    )

    if write_result.status != SandboxStatus.SUCCESS:
        return f"Error writing file: {write_result.error}"

    return f"Processed {input_file} -> {output_file}"
```

### Code Execution in Tools

```python
from nexau.archs.sandbox import BaseSandbox, CodeLanguage
from nexau.archs.main_sub.agent_state import AgentState

def run_analysis(data: dict, agent_state: AgentState) -> dict:
    """Run Python analysis in sandbox."""

    # Get sandbox instance from agent_state
    sandbox: BaseSandbox | None = agent_state.get_sandbox()

    # Generate Python code
    code = f"""
import json
data = {data}
result = {{
    'count': len(data),
    'keys': list(data.keys())
}}
print(json.dumps(result))
"""

    # Execute in sandbox
    result = sandbox.execute_code(
        code=code,
        language=CodeLanguage.PYTHON,
        timeout=30000  # 30 seconds
    )

    if result.status == SandboxStatus.SUCCESS:
        # Parse output
        import json
        output = result.outputs[0]['text'] if result.outputs else "{}"
        return json.loads(output)
    else:
        return {"error": result.error_value}
```

### Advanced Tool Example

```python
from nexau.archs.sandbox import BaseSandbox, SandboxStatus
from pathlib import Path
from nexau.archs.main_sub.agent_state import AgentState

def project_builder(
    project_name: str,
    files: dict[str, str],
    agent_state: AgentState
) -> dict:
    """Build a project structure in sandbox."""

    # Get sandbox instance from agent_state
    sandbox: BaseSandbox | None = agent_state.get_sandbox()

    project_dir = f"/home/user/projects/{project_name}"

    # Create project directory
    try:
        sandbox.create_directory(project_dir, parents=True)
    except Exception as e:
        return {"error": f"Failed to create directory: {e}"}

    # Create all files
    created_files = []
    for file_path, content in files.items():
        full_path = f"{project_dir}/{file_path}"

        result = sandbox.write_file(
            full_path,
            content,
            create_directories=True
        )

        if result.status == SandboxStatus.SUCCESS:
            created_files.append(file_path)
        else:
            return {
                "error": f"Failed to create {file_path}: {result.error}",
                "created": created_files
            }

    # List project files
    files_list = sandbox.list_files(project_dir, recursive=True)

    return {
        "project": project_name,
        "location": project_dir,
        "files_created": len(created_files),
        "total_files": len(files_list),
        "structure": [f.path for f in files_list if f.is_file]
    }
```

## Integrating New Sandbox Backends

You can create custom sandbox implementations by extending the base classes.

### Step 1: Implement BaseSandbox

Create a new sandbox class that implements all abstract methods:

```python
from dataclasses import dataclass
from nexau.archs.sandbox import (
    BaseSandbox,
    CommandResult,
    CodeExecutionResult,
    FileOperationResult,
    FileInfo,
    SandboxStatus,
    CodeLanguage
)

@dataclass(kw_only=True)
class CustomSandbox(BaseSandbox):
    """Custom sandbox implementation."""

    # Add custom configuration fields
    custom_param: str = "default"

    def execute_bash(
        self,
        command: str,
        timeout: int | None = None,
    ) -> CommandResult:
        """Execute bash command in custom environment."""
        # Implement command execution
        # Return CommandResult with status, stdout, stderr, etc.
        pass

    def execute_code(
        self,
        code: str,
        language: CodeLanguage | str,
        timeout: int | None = None,
    ) -> CodeExecutionResult:
        """Execute code in custom environment."""
        # Implement code execution
        pass

    def read_file(
        self,
        file_path: str,
        encoding: str = "utf-8",
        binary: bool = False,
    ) -> FileOperationResult:
        """Read file from custom storage."""
        # Implement file reading
        pass

    def write_file(
        self,
        file_path: str,
        content: str | bytes,
        encoding: str = "utf-8",
        binary: bool = False,
        create_directories: bool = True,
    ) -> FileOperationResult:
        """Write file to custom storage."""
        # Implement file writing
        pass

    # Implement all other abstract methods:
    # - delete_file
    # - list_files
    # - file_exists
    # - get_file_info
    # - create_directory
    # - edit_file
    # - glob
    # - upload_file
    # - download_file
    # - upload_directory
    # - download_directory
```

### Step 2: Implement BaseSandboxManager

Create a manager to handle sandbox lifecycle:

```python
from dataclasses import dataclass, field
from typing import Any
from nexau.archs.sandbox import BaseSandboxManager
from nexau.archs.session import SessionManager

@dataclass(kw_only=True)
class CustomSandboxManager(BaseSandboxManager[CustomSandbox]):
    """Manager for custom sandbox lifecycle."""

    # Configuration fields
    custom_param: str = "default"

    def start(
        self,
        session_manager: SessionManager,
        user_id: str,
        session_id: str,
        sandbox_config: dict[str, Any]
    ) -> CustomSandbox:
        """Start a custom sandbox."""

        # Try to restore from saved state
        sandbox_state = self.load_sandbox_state(
            session_manager, user_id, session_id
        )

        if sandbox_state and sandbox_state.get("sandbox_id"):
            # Restore existing sandbox
            sandbox = CustomSandbox(**sandbox_state)
            # Reconnect to existing instance if needed
            return sandbox

        # Create new sandbox
        sandbox = CustomSandbox(
            sandbox_id=f"custom_{session_id}",
            custom_param=self.custom_param,
            **sandbox_config
        )

        # Initialize sandbox resources
        # ...

        # Persist state
        self.persist_sandbox_state(
            session_manager, user_id, session_id, sandbox
        )

        return sandbox

    def stop(self) -> bool:
        """Stop the sandbox."""
        if self._instance:
            # Cleanup resources
            return True
        return False

    def pause(self) -> bool:
        """Pause the sandbox."""
        # Implement pause logic
        return True

    def is_running(self) -> bool:
        """Check if sandbox is running."""
        return self._instance is not None
```

### Step 3: Register Sandbox Type

Add your sandbox to the sandbox alias mapping:

```python
# In your initialization code
from nexau.archs.sandbox.base_sandbox import SandboxAlias

SandboxAlias["custom"] = "CustomSandbox"
```

### Step 4: Use Custom Sandbox

Configure your agent to use the custom sandbox:

```yaml
sandbox_config:
  type: custom
  custom_param: my_value
  work_dir: /path/to/workdir
```

Or programmatically:

```python
from nexau import Agent

config = AgentConfig.from_yaml("config.yaml")
config.sandbox_config = {
    "type": "custom",
    "custom_param": "my_value"
}
agent = Agent(config=config)
```

### Complete Example: Docker Sandbox

Here's a simplified example of a Docker-based sandbox:

```python
import docker
from dataclasses import dataclass, field

@dataclass(kw_only=True)
class DockerSandbox(BaseSandbox):
    """Docker container sandbox."""

    image: str = "python:3.11"
    container_id: str | None = None
    _client: Any = field(default=None, init=False, repr=False)

    def __post_init__(self):
        self._client = docker.from_env()
        if not self.container_id:
            # Create new container
            container = self._client.containers.run(
                self.image,
                detach=True,
                tty=True,
                working_dir=str(self.work_dir)
            )
            self.container_id = container.id

    def execute_bash(self, command: str, timeout: int | None = None) -> CommandResult:
        container = self._client.containers.get(self.container_id)
        exit_code, output = container.exec_run(
            f"bash -c '{command}'",
            workdir=str(self.work_dir)
        )

        return CommandResult(
            status=SandboxStatus.SUCCESS if exit_code == 0 else SandboxStatus.ERROR,
            stdout=output.decode('utf-8'),
            exit_code=exit_code
        )

    # Implement other methods...
```

## API Reference

### BaseSandbox Interface

The `BaseSandbox` abstract class defines the core interface that all sandbox implementations must follow.

#### Attributes

- **`sandbox_id`** (`str | None`): Unique identifier for the sandbox instance
- **`work_dir`** (`str`): Working directory path in the sandbox (default: current directory)
- **`work_dir`** (`Path`): Property that returns `work_dir` as a `Path` object

#### Command Execution Methods

##### `execute_bash(command: str, timeout: int | None = None) -> CommandResult`

Execute a bash command in the sandbox.

**Parameters:**
- `command` (`str`): The bash command to execute
- `timeout` (`int | None`): Optional timeout in milliseconds (overrides default)

**Returns:** `CommandResult` containing execution results

**Raises:** `SandboxError` if command execution fails

##### `execute_code(code: str, language: CodeLanguage | str, timeout: int | None = None) -> CodeExecutionResult`

Execute code in the specified programming language.

**Parameters:**
- `code` (`str`): The code to execute
- `language` (`CodeLanguage | str`): Programming language (CodeLanguage enum or string)
- `timeout` (`int | None`): Optional timeout in milliseconds (overrides default)

**Returns:** `CodeExecutionResult` containing execution results and outputs

**Raises:**
- `SandboxError` if code execution fails
- `ValueError` if language is not supported

#### File Operation Methods

##### `read_file(file_path: str, encoding: str = "utf-8", binary: bool = False) -> FileOperationResult`

Read a file from the sandbox.

**Parameters:**
- `file_path` (`str`): Path to the file in the sandbox
- `encoding` (`str`): File encoding (default: "utf-8")
- `binary` (`bool`): Whether to read file in binary mode

**Returns:** `FileOperationResult` containing file content

**Raises:** `SandboxError` if file read fails

##### `write_file(file_path: str, content: str | bytes, encoding: str = "utf-8", binary: bool = False, create_directories: bool = True) -> FileOperationResult`

Write content to a file in the sandbox.

**Parameters:**
- `file_path` (`str`): Path to the file in the sandbox
- `content` (`str | bytes`): Content to write (string or bytes)
- `encoding` (`str`): File encoding (default: "utf-8")
- `binary` (`bool`): Whether to write file in binary mode
- `create_directories` (`bool`): Whether to create parent directories if they don't exist

**Returns:** `FileOperationResult` containing operation status

**Raises:** `SandboxError` if file write fails

##### `delete_file(file_path: str) -> FileOperationResult`

Delete a file from the sandbox.

**Parameters:**
- `file_path` (`str`): Path to the file in the sandbox

**Returns:** `FileOperationResult` containing operation status

**Raises:** `SandboxError` if file deletion fails

##### `list_files(directory_path: str, recursive: bool = False, pattern: str | None = None) -> list[FileInfo]`

List files in a directory in the sandbox.

**Parameters:**
- `directory_path` (`str`): Path to the directory in the sandbox
- `recursive` (`bool`): Whether to list files recursively
- `pattern` (`str | None`): Optional glob pattern to filter files

**Returns:** List of `FileInfo` objects for matching files

**Raises:** `SandboxError` if directory listing fails

##### `file_exists(file_path: str) -> bool`

Check if a file exists in the sandbox.

**Parameters:**
- `file_path` (`str`): Path to the file in the sandbox

**Returns:** `True` if file exists, `False` otherwise

##### `get_file_info(file_path: str) -> FileInfo`

Get information about a file in the sandbox.

**Parameters:**
- `file_path` (`str`): Path to the file in the sandbox

**Returns:** `FileInfo` object containing file metadata

**Raises:** `SandboxError` if file info retrieval fails

##### `create_directory(directory_path: str, parents: bool = True) -> bool`

Create a directory in the sandbox.

**Parameters:**
- `directory_path` (`str`): Path to the directory to create
- `parents` (`bool`): Whether to create parent directories if they don't exist

**Returns:** `True` if directory created successfully, `False` otherwise

**Raises:** `SandboxFileError` if directory creation fails

##### `edit_file(file_path: str, old_string: str, new_string: str) -> FileOperationResult`

Edit a file by replacing old_string with new_string.

Supports three operations:
1. **CREATE**: Set `old_string` to empty string to create a new file
2. **UPDATE**: Provide both `old_string` and `new_string` to update existing content
3. **REMOVE_CONTENT**: Set `new_string` to empty string to remove the `old_string` content

**Parameters:**
- `file_path` (`str`): Path to the file to edit
- `old_string` (`str`): String to replace (empty for file creation)
- `new_string` (`str`): Replacement string (empty for content removal)

**Returns:** `FileOperationResult` containing operation status and details

**Raises:** `SandboxFileError` if edit operation fails

##### `glob(pattern: str, recursive: bool = True) -> list[str]`

Find files matching a glob pattern.

**Parameters:**
- `pattern` (`str`): Glob pattern (e.g., '*.py', '**/*.txt')
- `recursive` (`bool`): Whether to search recursively (default: True)

**Returns:** List of file paths matching the pattern

**Raises:** `SandboxFileError` if glob operation fails

#### File Upload/Download Methods

##### `upload_file(local_path: str, sandbox_path: str, create_directories: bool = True) -> FileOperationResult`

Upload a file from the local filesystem to the sandbox.

**Parameters:**
- `local_path` (`str`): Path to the file on the local filesystem
- `sandbox_path` (`str`): Destination path in the sandbox
- `create_directories` (`bool`): Whether to create parent directories if they don't exist

**Returns:** `FileOperationResult` containing operation status

**Raises:** `SandboxError` if file upload fails

##### `download_file(sandbox_path: str, local_path: str, create_directories: bool = True) -> FileOperationResult`

Download a file from the sandbox to the local filesystem.

**Parameters:**
- `sandbox_path` (`str`): Path to the file in the sandbox
- `local_path` (`str`): Destination path on the local filesystem
- `create_directories` (`bool`): Whether to create parent directories if they don't exist

**Returns:** `FileOperationResult` containing operation status

**Raises:** `SandboxError` if file download fails

##### `upload_directory(local_path: str, sandbox_path: str) -> bool`

Upload a directory from the local filesystem to the sandbox.

**Parameters:**
- `local_path` (`str`): Path to the directory on the local filesystem
- `sandbox_path` (`str`): Destination path in the sandbox

**Returns:** `True` if directory uploaded successfully, `False` otherwise

**Raises:** `SandboxError` if directory upload fails

##### `download_directory(sandbox_path: str, local_path: str) -> bool`

Download a directory from the sandbox to the local filesystem.

**Parameters:**
- `sandbox_path` (`str`): Path to the directory in the sandbox
- `local_path` (`str`): Destination path on the local filesystem

**Returns:** `True` if directory downloaded successfully, `False` otherwise

**Raises:** `SandboxError` if directory download fails

---

### BaseSandboxManager Interface

The `BaseSandboxManager` abstract class manages sandbox lifecycle and state persistence.

#### Abstract Methods

##### `start(session_manager: SessionManager | None, user_id: str, session_id: str, sandbox_config: dict[str, Any]) -> TSandbox`

Start a sandbox for a session.

**Parameters:**
- `session_manager` (`SessionManager | None`): Session manager for state persistence (None disables persistence)
- `user_id` (`str`): User identifier
- `session_id` (`str`): Session identifier
- `sandbox_config` (`dict[str, Any]`): Configuration dictionary for sandbox initialization

**Returns:** Started sandbox instance

**Behavior:**
- Attempts to load and restore sandbox state from session if `session_manager` is not `None`
- Creates new sandbox if no saved state exists
- Persists sandbox state to session after creation

##### `stop() -> bool`

Stop the sandbox.

**Returns:** `True` if sandbox stopped successfully, `False` otherwise

##### `pause() -> bool`

Pause the sandbox for a session.

**Returns:** `True` if sandbox paused successfully, `False` otherwise

**Behavior:**
- Backend-specific pause implementation (E2B pauses cloud instance, Local does nothing)
- Sandbox state remains in `_session_context` for auto-resume

##### `is_running() -> bool`

Check if the sandbox is running.

**Returns:** `True` if sandbox is running, `False` otherwise

## Summary

The NexAU sandbox system provides:

- **Flexible configuration** through YAML or programmatic API
- **Automatic state persistence** when used with sessions
- **Multiple backends** (Local for development, E2B for production)
- **Easy tool integration** with automatic sandbox injection
- **Extensible architecture** for custom implementations

For production deployments, use E2B sandboxes with proper templates and resource limits. For development and testing, LocalSandbox provides a simple, fast alternative.
