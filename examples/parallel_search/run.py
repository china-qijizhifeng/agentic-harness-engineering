"""Search and fetch through NexAU's YAML loader and native MCP tool dispatcher."""

import argparse
import json
from pathlib import Path
import uuid

from nexau.archs.main_sub.config import AgentConfig
from nexau.archs.tool.builtin.mcp_client import sync_initialize_mcp_tools


def call_tool(tools, name, **arguments):
    if name not in tools:
        raise RuntimeError(f"MCP server did not expose {name}")
    result = tools[name].execute(**arguments)
    if "error" in result:
        raise RuntimeError(f"{name}: {result['error']}")
    content = result.get("result", "")
    if not content:
        raise RuntimeError(f"{name} returned no content")
    # NexAU returns MCP text content as a string, including server error JSON.
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return content
    if isinstance(payload, dict) and payload.get("error"):
        raise RuntimeError(f"{name}: {payload['error']}")
    return content


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", default="NexAU HTTP MCP configuration")
    parser.add_argument("--url", default="https://docs.parallel.ai/integrations/mcp/search-mcp")
    args = parser.parse_args()

    config = AgentConfig.from_yaml(Path(__file__).with_name("agent.yaml"))
    tools = {tool.name: tool for tool in sync_initialize_mcp_tools(config.mcp_servers)}
    session_id = str(uuid.uuid4())
    print(call_tool(tools, "web_search", objective=args.query,
                    search_queries=[args.query], session_id=session_id))
    print(call_tool(tools, "web_fetch", urls=[args.url], session_id=session_id))


if __name__ == "__main__":
    main()
