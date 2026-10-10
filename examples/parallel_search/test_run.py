"""Exercise the documented runner through NexAU and an observed HTTP transport."""

import contextlib
import io
import json
import unittest
from unittest.mock import patch

import httpx

import run
from nexau.archs.tool.builtin import mcp_client


class RunnerTests(unittest.TestCase):
    def test_loader_discovery_and_dispatch(self):
        requests = []

        def respond(request):
            message = json.loads(request.content)
            requests.append((request, message))
            method = message["method"]
            if method.startswith("notifications/"):
                return httpx.Response(202)
            if method == "initialize":
                result = {"protocolVersion": "2024-11-05", "capabilities": {},
                          "serverInfo": {"name": "fixture", "version": "1"}}
            elif method == "tools/list":
                result = {"tools": [
                    {"name": name, "description": name,
                     "inputSchema": {"type": "object", "properties": {}}}
                    for name in ("web_search", "web_fetch")
                ]}
            else:
                self.assertEqual(method, "tools/call")
                result = {"content": [{"type": "text", "text": "Useful source excerpt"}]}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"],
                                            "result": result})

        real_client = httpx.AsyncClient

        def client(**kwargs):
            return real_client(transport=httpx.MockTransport(respond), **kwargs)

        output = io.StringIO()
        with patch.object(mcp_client, "_mcp_manager", None), \
                patch.object(httpx, "AsyncClient", client), \
                patch("sys.argv", ["run.py", "--query", "NexAU HTTP MCP", "--url",
                                   "https://example.org/docs"]), \
                contextlib.redirect_stdout(output):
            run.main()

        self.assertEqual(output.getvalue().count("Useful source excerpt"), 2)
        for request, _ in requests:
            self.assertEqual(str(request.url), "https://search.parallel.ai/mcp")
            self.assertEqual(request.headers["User-Agent"],
                             "agentic-harness-engineering/0.1.0 (Parallel Search MCP example)")
            self.assertNotIn("authorization", request.headers)
        calls = [message["params"] for _, message in requests
                 if message["method"] == "tools/call"]
        self.assertEqual([call["name"] for call in calls], ["web_search", "web_fetch"])
        self.assertEqual(calls[0]["arguments"]["search_queries"], ["NexAU HTTP MCP"])
        self.assertEqual(calls[1]["arguments"]["urls"], ["https://example.org/docs"])
        self.assertEqual(calls[0]["arguments"]["session_id"], calls[1]["arguments"]["session_id"])

    def test_missing_tool_is_failure(self):
        with self.assertRaisesRegex(RuntimeError, "did not expose web_search"):
            run.call_tool({}, "web_search")

    def test_tool_error_is_failure(self):
        class FailedTool:
            def execute(self, **kwargs):
                return {"error": "Request timed out"}

        with self.assertRaisesRegex(RuntimeError, "Request timed out"):
            run.call_tool({"web_search": FailedTool()}, "web_search")


if __name__ == "__main__":
    unittest.main()
