# Free web search with Parallel Search MCP

This example loads an HTTP MCP server with the same NexAU v0.3.9 runtime used by
AHE, then dispatches `web_search` and `web_fetch`. [Parallel Search MCP](https://docs.parallel.ai/integrations/mcp/search-mcp)
provides free web search and page fetching without an account or API key. Anonymous
search uses Fast mode and has lower rate limits than authenticated access.

From the AHE repository root, with Python 3.13 and uv installed:

```bash
cd examples/parallel_search
uv sync
uv run python run.py --query "NexAU HTTP MCP configuration" \
  --url https://docs.parallel.ai/integrations/mcp/search-mcp
```

The command prints search excerpts and the fetched page's excerpts. It dispatches
tools directly without calling a model, so no LLM credentials, E2B sandbox or
Serper key are needed. The example installs its own dependencies independently
of the full evaluation stack. `agent.yaml` sets the project User-Agent and a
60-second MCP request timeout. One generated session ID is reused for both calls.

## Add the server to a coding-agent experiment

To expose these tools to the simple coding agent, copy the `mcp_servers` block
from `agent.yaml` into the `code_agent_patch` section of your experiment YAML:

```yaml
code_agent_patch:
  mcp_servers:
    - name: parallel
      type: http
      url: https://search.parallel.ai/mcp
      headers:
        User-Agent: "agentic-harness-engineering/0.1.0 (Parallel Search MCP example)"
      timeout: 60
```

AHE applies this section as a patch to the experiment's copied `code_agent.yaml`.
NexAU discovers `web_search` and `web_fetch` at agent initialization alongside
the simple coding agent's `run_shell_command`. The sandbox needs outbound HTTPS
access to the MCP endpoint. A full evolution run still needs its usual LLM,
Serper and evaluation configuration. If you already configure MCP servers,
append Parallel to that list: AHE's list patches replace the existing list.
Avoid adding this block to an agent that already has a tool named `web_search`
or `web_fetch`, such as the evolution agent, without resolving that name collision.
