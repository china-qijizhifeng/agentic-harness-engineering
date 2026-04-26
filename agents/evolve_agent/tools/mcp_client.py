# Copyright (c) Nex-AGI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""MCP client implementation for NexAU framework."""

import asyncio
import logging
from asyncio.subprocess import Process
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypedDict, cast

from mcp import ClientSession, StdioServerParameters
from mcp.types import Tool as MCPToolType

from nexau.archs.tool import Tool

if TYPE_CHECKING:
    import httpx

logger = logging.getLogger(__name__)
JSONDict = dict[str, Any]


class _HTTPSessionParams(TypedDict):
    """Stored parameters needed to recreate an HTTP MCP session."""

    config: "MCPServerConfig"
    headers: dict[str, str]
    timeout: float


STREAMABLE_HTTP_ACCEPT = "application/json, text/event-stream"
SSE_ACCEPT = "text/event-stream"
DEFAULT_CONTENT_TYPE = "application/json"


class HTTPMCPSession:
    """HTTP MCP session that handles MCP streamable HTTP with session management."""

    def __init__(
        self,
        config: "MCPServerConfig",
        headers: dict[str, str],
        timeout: float,
    ):
        self.config = config
        self.headers = headers
        self.timeout = timeout
        self._request_id = 0
        self._session_id: str | None = None
        self._initialized = False
        self._transport: str | None = None
        self._pending_requests: dict[str, asyncio.Future[JSONDict]] = {}
        self._sse_client: httpx.AsyncClient | None = None
        self._sse_stream_cm: AbstractAsyncContextManager[httpx.Response] | None = None
        self._sse_stream: httpx.Response | None = None
        self._sse_listener_task: asyncio.Task[None] | None = None
        self._sse_endpoint_url: str | None = None
        self._sse_endpoint_headers: dict[str, str] = {}
        self._sse_endpoint_ready: asyncio.Event | None = None

    async def initialize(self) -> None:
        """Public initializer for MCP sessions."""
        await self._initialize_session()

    def _get_next_id(self) -> int:
        """Get the next request ID."""
        self._request_id += 1
        return self._request_id

    async def _initialize_session(self) -> None:
        """Initialize the MCP session, with fallback to HTTP+SSE transport."""
        if self._initialized:
            return

        import httpx

        try:
            await self._initialize_streamable_http()
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            if 400 <= status_code < 500:
                logger.info(
                    "Streamable HTTP transport not available (status %s); attempting HTTP+SSE fallback.",
                    status_code,
                )
                await self._initialize_http_sse()
            else:
                raise

    def _build_initialize_request(self) -> dict[str, Any]:
        """Construct the MCP initialize request payload."""
        return {
            "jsonrpc": "2.0",
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "roots": {"listChanged": True},
                    "sampling": {},
                },
                "clientInfo": {
                    "name": "nexau-mcp-client",
                    "version": "1.0.0",
                },
            },
            "id": self._get_next_id(),
        }

    def _validate_initialize_payload(
        self,
        payload: dict[str, Any] | None,
        raw_text: str | None = None,
    ) -> None:
        """Ensure the initialize response conforms to expectations."""
        if not payload:
            raise Exception(
                f"No valid response data found in initialization response: {raw_text}",
            )

        if "error" in payload:
            raise Exception(f"MCP initialization error: {payload['error']}")

        if "result" not in payload:
            raw_text = raw_text or str(payload)
            raise Exception(
                f"Unexpected initialization response format - missing 'result' field: {raw_text}",
            )

        logger.debug(
            "Initialization successful, server info: %s",
            payload.get("result", {}).get("serverInfo", "Unknown"),
        )

    def _parse_streamable_http_payload(
        self,
        response_text: str,
        expected_id: int | None = None,
    ) -> dict[str, Any]:
        """Parse responses that may include SSE-formatted messages."""
        import json

        if not response_text:
            raise Exception("Empty response from MCP server")

        if "event:" in response_text and "data:" in response_text:
            lines = response_text.strip().split("\n")
            messages: list[dict[str, Any]] = []
            for line in lines:
                if line.startswith("data: "):
                    data_segment = line[6:].strip()
                    if not data_segment:
                        continue
                    try:
                        messages.append(json.loads(data_segment))
                    except json.JSONDecodeError:
                        logger.debug(
                            "Failed to parse SSE line as JSON: %s...",
                            data_segment[:100],
                        )
                        continue

            if not messages:
                raise Exception(f"Could not parse SSE response: {response_text}")

            if expected_id is None:
                return messages[-1]

            for message in messages:
                if str(message.get("id")) == str(expected_id):
                    logger.debug(
                        "Found matching response for request ID %s",
                        expected_id,
                    )
                    return message

            logger.warning(
                "No matching response found for request ID %s in SSE stream",
                expected_id,
            )
            for message in reversed(messages):
                if "result" in message or "error" in message:
                    return message
            return messages[-1]

        try:
            return json.loads(response_text)
        except Exception as exc:  # pragma: no cover - re-raise with context
            raise Exception(f"Could not parse response: {response_text}") from exc

    async def _initialize_streamable_http(self) -> None:
        """Attempt MCP initialization using Streamable HTTP transport."""
        import httpx

        if not self.config.url:
            raise ValueError("Server URL is required")

        init_request = self._build_initialize_request()

        request_headers = self.headers.copy()
        request_headers.setdefault("Accept", STREAMABLE_HTTP_ACCEPT)
        request_headers.setdefault("Content-Type", DEFAULT_CONTENT_TYPE)

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                self.config.url,
                json=init_request,
                headers=request_headers,
            )
            response.raise_for_status()

        session_id = response.headers.get("mcp-session-id")
        if session_id:
            self._session_id = session_id
            logger.debug("Captured session ID: %s", session_id)

        response_text = response.text
        logger.debug("Initialize response: %s", response_text)

        parsed = self._parse_streamable_http_payload(
            response_text,
            expected_id=init_request.get("id"),
        )
        self._validate_initialize_payload(parsed, response_text)

        self._transport = "streamable_http"
        await self._send_initialized_notification()
        self._initialized = True
        logger.info(
            "MCP HTTP session initialized successfully with session ID: %s",
            self._session_id,
        )

    async def _initialize_http_sse(self) -> None:
        """Fallback initialization for servers speaking HTTP+SSE transport."""
        import httpx

        if not self.config.url:
            raise ValueError("Server URL is required")

        timeout = httpx.Timeout(
            timeout=self.timeout,
            connect=self.timeout,
            read=None,
            write=self.timeout,
        )

        base_headers = self.headers.copy()
        base_headers.setdefault("Accept", SSE_ACCEPT)

        self._pending_requests = {}
        self._sse_endpoint_headers = {}
        self._sse_endpoint_ready = asyncio.Event()

        self._sse_client = httpx.AsyncClient(timeout=timeout)
        self._sse_stream_cm = self._sse_client.stream(
            "GET",
            self.config.url,
            headers=base_headers,
        )
        self._sse_stream = await self._sse_stream_cm.__aenter__()
        self._sse_stream.raise_for_status()

        self._sse_listener_task = asyncio.create_task(self._consume_sse_stream())

        try:
            await asyncio.wait_for(self._sse_endpoint_ready.wait(), timeout=self.timeout)
        except TimeoutError as exc:
            raise TimeoutError("Timed out waiting for SSE endpoint event") from exc

        if not self._sse_endpoint_url:
            raise RuntimeError("Did not receive endpoint information from SSE stream")

        init_request = self._build_initialize_request()
        init_response = await self._send_json_rpc_via_sse(init_request, expect_response=True)
        self._validate_initialize_payload(init_response)

        self._transport = "http_sse"
        await self._send_initialized_notification()
        self._initialized = True
        logger.info("MCP HTTP session initialized successfully using HTTP+SSE transport")

    async def _send_initialized_notification(self) -> None:
        """Send the initialized notification as per MCP protocol."""
        import httpx

        notification_data = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        }

        if self._transport == "http_sse":
            try:
                await self._send_json_rpc_via_sse(notification_data, expect_response=False)
                logger.debug("Initialized notification sent successfully via SSE transport")
            except Exception as exc:
                logger.warning("Initialized notification via SSE transport failed: %s", exc)
            return

        request_headers = self.headers.copy()
        request_headers.setdefault("Content-Type", DEFAULT_CONTENT_TYPE)
        if self._session_id:
            request_headers["mcp-session-id"] = self._session_id

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            if not self.config.url:
                raise ValueError("Server URL is required")
            response = await client.post(
                self.config.url,
                json=notification_data,
                headers=request_headers,
            )
            if response.status_code >= 400:
                logger.warning(
                    "Initialized notification returned status %s",
                    response.status_code,
                )
            else:
                logger.debug("Initialized notification sent successfully")

    async def _make_request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make a direct HTTP request to the MCP server."""
        if not self._initialized:
            await self._initialize_session()

        request_data: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
            "id": self._get_next_id(),
        }

        if params:
            request_data["params"] = params

        if self._transport == "http_sse":
            return await self._send_json_rpc_via_sse(request_data, expect_response=True)

        return await self._make_streamable_http_request(request_data)

    async def _make_streamable_http_request(self, request_data: dict[str, Any]) -> dict[str, Any]:
        """Send a JSON-RPC request over Streamable HTTP."""
        import httpx

        if not self.config.url:
            raise ValueError("Server URL is required")

        request_headers = self.headers.copy()
        request_headers.setdefault("Accept", STREAMABLE_HTTP_ACCEPT)
        request_headers.setdefault("Content-Type", DEFAULT_CONTENT_TYPE)
        if self._session_id:
            request_headers["mcp-session-id"] = self._session_id
            logger.debug("Including session ID in request: %s", self._session_id)

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                self.config.url,
                json=request_data,
                headers=request_headers,
            )
            response.raise_for_status()

        response_text = response.text
        return self._parse_streamable_http_payload(
            response_text,
            expected_id=request_data.get("id"),
        )

    async def _consume_sse_stream(self) -> None:
        """Continuously consume SSE messages from the fallback transport."""
        if not self._sse_stream:
            return

        event_type = "message"
        data_lines: list[str] = []

        try:
            async for raw_line in self._sse_stream.aiter_lines():
                line = raw_line.rstrip("\r")

                if line == "":
                    if data_lines:
                        await self._handle_sse_event(event_type, data_lines)
                    event_type = "message"
                    data_lines = []
                    continue

                if line.startswith(":"):
                    continue

                if line.startswith("event:"):
                    event_type = line[6:].strip() or "message"
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())

            if data_lines:
                await self._handle_sse_event(event_type, data_lines)
        except Exception as exc:  # pragma: no cover - background task
            logger.error("SSE listener terminated with error: %s", exc, exc_info=True)
            self._fail_pending_requests(exc)
        finally:
            if self._sse_endpoint_ready and not self._sse_endpoint_ready.is_set():
                self._sse_endpoint_ready.set()

            if self._sse_stream_cm is not None:
                await self._sse_stream_cm.__aexit__(None, None, None)

            self._sse_stream_cm = None
            self._sse_stream = None

    async def _handle_sse_event(self, event_type: str, data_lines: list[str]) -> None:
        """Process individual SSE events."""
        import json
        from urllib.parse import urljoin

        if not data_lines:
            return

        payload_text = "\n".join(data_lines).strip()

        if event_type == "endpoint":
            endpoint: str | None = None
            headers_payload: dict[str, Any] | None = None

            if payload_text:
                try:
                    payload: Any = json.loads(payload_text)
                except json.JSONDecodeError:
                    endpoint = payload_text
                else:
                    if isinstance(payload, dict):
                        payload_dict: dict[str, Any] = cast(dict[str, Any], payload)
                        endpoint_candidate: Any = payload_dict.get("endpoint") or payload_dict.get("url")
                        endpoint = str(endpoint_candidate) if endpoint_candidate is not None else None
                        headers_payload = cast(dict[str, Any] | None, payload_dict.get("headers"))
                    elif isinstance(payload, str):
                        endpoint = payload

            if endpoint:
                base_url = self.config.url or ""
                self._sse_endpoint_url = urljoin(base_url, endpoint)
                logger.debug("SSE endpoint resolved to %s", self._sse_endpoint_url)
            else:
                logger.error("Endpoint event missing usable endpoint information: %s", payload_text or "<empty>")

            combined_headers = self.headers.copy()
            if isinstance(headers_payload, dict):
                for key, value in headers_payload.items():
                    combined_headers[str(key)] = str(value)
            combined_headers.setdefault("Content-Type", DEFAULT_CONTENT_TYPE)
            self._sse_endpoint_headers = combined_headers

            if self._sse_endpoint_ready and not self._sse_endpoint_ready.is_set():
                self._sse_endpoint_ready.set()
            return

        try:
            message_obj: Any = json.loads(payload_text)
        except json.JSONDecodeError:
            logger.debug("Failed to parse SSE data as JSON: %s", payload_text[:200])
            return

        if not isinstance(message_obj, dict):
            return

        message = cast(dict[str, Any], message_obj)
        request_id = cast(str | int | None, message.get("id"))
        if request_id is not None:
            key = str(request_id)
            future = self._pending_requests.pop(key, None)
            if future and not future.done():
                future.set_result(message)
            else:
                logger.debug("Received SSE response for unknown request ID %s: %s", key, message)
        else:
            logger.debug("Received SSE notification: %s", message)

    def _fail_pending_requests(self, exc: Exception) -> None:
        """Fail any pending SSE requests if the stream is closed."""
        if not self._pending_requests:
            return

        for key, future in list(self._pending_requests.items()):
            if future and not future.done():
                future.set_exception(exc)
            self._pending_requests.pop(key, None)

    async def _send_json_rpc_via_sse(
        self,
        payload: dict[str, Any],
        *,
        expect_response: bool,
    ) -> dict[str, Any]:
        """Send JSON-RPC payload over HTTP+SSE transport."""

        if not self._sse_client or not self._sse_endpoint_url:
            raise RuntimeError("SSE transport is not initialized")

        request_headers = self._sse_endpoint_headers.copy()
        if self._session_id:
            request_headers.setdefault("mcp-session-id", self._session_id)

        request_id = payload.get("id")
        future: asyncio.Future[JSONDict] | None = None
        key: str | None = None

        if expect_response:
            if request_id is None:
                raise ValueError("Expected request ID for response tracking")
            key = str(request_id)
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            self._pending_requests[key] = future

        try:
            response = await self._sse_client.post(
                self._sse_endpoint_url,
                json=payload,
                headers=request_headers,
            )
            response.raise_for_status()
        except Exception as exc:
            if key is not None:
                pending = self._pending_requests.pop(key, None)
                if pending and not pending.done():
                    pending.set_exception(exc)
            raise

        if not expect_response:
            return {}

        assert future is not None  # for type checkers

        try:
            result = await asyncio.wait_for(future, timeout=self.timeout)
            return result
        except TimeoutError as exc:
            if key is not None:
                pending = self._pending_requests.pop(key, None)
                if pending and not pending.done():
                    pending.set_exception(exc)
            raise TimeoutError(
                f"SSE response timed out for request {request_id}",
            ) from exc

    async def list_tools(self):
        """List tools by making a direct HTTP request."""
        # Ensure session is initialized
        if not self._initialized:
            await self._initialize_session()

        response = await self._make_request("tools/list")
        logger.debug(f"Tools list response: {response}")

        if "error" in response:
            raise Exception(f"MCP error: {response['error']}")

        # Convert the response to match the expected format
        tools_data = response.get("result", {}).get("tools", [])
        logger.debug(f"Extracted tools data: {tools_data}")

        # Create a simple object structure that matches what MCPTool expects
        class SimpleToolList:
            def __init__(self, tools_data: Sequence[dict[str, Any]]) -> None:
                self.tools = [SimpleTool(tool_data) for tool_data in tools_data]

        class SimpleTool:
            def __init__(self, tool_data: dict[str, Any]) -> None:
                self.name = tool_data["name"]
                self.description = tool_data.get("description", "")
                self.inputSchema = tool_data.get("inputSchema", {})

        result = SimpleToolList(tools_data)
        logger.debug(f"Created tool list with {len(result.tools)} tools")
        return result

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]):
        """Call a tool by making a direct HTTP request."""
        # Ensure session is initialized
        if not self._initialized:
            await self._initialize_session()

        params: JSONDict = {
            "name": tool_name,
            "arguments": arguments,
        }

        response = await self._make_request("tools/call", params)

        if "error" in response:
            raise Exception(f"MCP error: {response['error']}")

        # Create a simple result object that matches what MCPTool expects
        class SimpleResult:
            def __init__(self, result_data: dict[str, Any]) -> None:
                self.content = result_data.get("content", [])

        return SimpleResult(response.get("result", {}))


@dataclass
class MCPServerConfig:
    """Configuration for an MCP server."""

    name: str
    type: str = "stdio"  # "stdio" or "http"
    # For stdio servers
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    # For HTTP servers
    url: str | None = None
    headers: dict[str, str] | None = None
    timeout: float | None = 30
    # disable parallel
    disable_parallel: bool = False


class MCPTool(Tool):
    """Wrapper for MCP tools to conform to NexAU Tool interface."""

    def __init__(
        self,
        mcp_tool: MCPToolType,
        client_session: ClientSession | HTTPMCPSession,
        server_config: MCPServerConfig | None = None,
    ):
        self.mcp_tool = mcp_tool
        self.client_session = client_session
        self.server_config = server_config  # Store config for recreating sessions
        self._session_type = type(client_session).__name__
        self._session_params: _HTTPSessionParams | MCPServerConfig | None = None
        self._sync_executor: Callable[..., dict[str, Any]] = self._execute_sync

        # Store session parameters for thread-safe recreation
        if isinstance(client_session, HTTPMCPSession):
            self._session_params = {
                "config": client_session.config,
                "headers": client_session.headers,
                "timeout": float(client_session.timeout),
            }
        elif server_config is not None:
            # For stdio sessions, store the server config for recreation
            self._session_params = server_config

        # Convert MCP tool to NexAU tool format
        super().__init__(
            name=mcp_tool.name,
            description=mcp_tool.description or "",
            input_schema=mcp_tool.inputSchema,
            implementation=self._sync_executor,
            disable_parallel=server_config.disable_parallel if server_config else False,
        )

    async def _get_thread_local_session(self) -> Any:
        """Get or create a thread-local session to avoid event loop conflicts."""
        # For HTTPMCPSession, we can safely create a new instance in each thread
        if self._session_type == "HTTPMCPSession" and isinstance(
            self._session_params,
            dict,
        ):
            params: _HTTPSessionParams = self._session_params
            config = params["config"]
            headers = params["headers"]
            timeout = params["timeout"]
            return HTTPMCPSession(config, headers, timeout)

        # For stdio sessions, we need to create a new DirectMCPSession
        elif self._session_type == "DirectMCPSession" and isinstance(
            self._session_params,
            MCPServerConfig,
        ):
            config = self._session_params
            if config.type == "stdio" and config.command:
                # Create a new DirectMCPSession
                import os

                merged_env = os.environ.copy()
                if config.env:
                    merged_env.update(config.env)

                # Use the DirectMCPSession class definition from the original code
                class DirectMCPSession:
                    def __init__(
                        self,
                        command: str,
                        args: list[str],
                        env: dict[str, str],
                    ) -> None:
                        self.command = command
                        self.args = args
                        self.env = env
                        self.process: Process | None = None
                        self._request_id = 0

                    async def initialize(self) -> "DirectMCPSession":
                        import subprocess

                        self.process = await asyncio.create_subprocess_exec(
                            self.command,
                            *self.args,
                            limit=1024 * 128,  # set a large buffer size
                            env=self.env,
                            stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                        )

                        if not self.process.stdout or not self.process.stdin:
                            raise RuntimeError("Failed to get process streams")

                        # Initialize the MCP connection properly
                        await self._initialize_connection()
                        return self

                    def _get_next_id(self) -> int:
                        self._request_id += 1
                        return self._request_id

                    async def _initialize_connection(self) -> None:
                        """Initialize the MCP connection with proper handshake."""
                        import json

                        # Send initialize request
                        init_request: JSONDict = {
                            "jsonrpc": "2.0",
                            "id": self._get_next_id(),
                            "method": "initialize",
                            "params": {
                                "protocolVersion": "2024-11-05",
                                "capabilities": {
                                    "roots": {
                                        "listChanged": True,
                                    },
                                    "sampling": {},
                                },
                                "clientInfo": {
                                    "name": "nexau-mcp-client",
                                    "version": "1.0.0",
                                },
                            },
                        }

                        # Send the initialize request
                        init_str = json.dumps(init_request) + "\n"
                        process = self.process
                        if process is None or process.stdin is None or process.stdout is None:
                            raise RuntimeError("Process streams not initialized")
                        stdin = process.stdin
                        stdout = process.stdout
                        stdin.write(init_str.encode())
                        await stdin.drain()

                        # Read the initialize response
                        while True:
                            try:
                                response_line = await asyncio.wait_for(
                                    stdout.readline(),
                                    timeout=60,
                                )
                                response = json.loads(
                                    response_line.decode().strip(),
                                )
                                break
                            except TimeoutError:
                                raise RuntimeError(
                                    "MCP initialization timeout",
                                )
                            except json.JSONDecodeError:
                                continue

                        if "error" in response:
                            raise Exception(
                                f"MCP initialization error: {response['error']}",
                            )

                        # Send initialized notification
                        initialized_notification: JSONDict = {
                            "jsonrpc": "2.0",
                            "method": "notifications/initialized",
                            "params": {},
                        }

                        notif_str = json.dumps(initialized_notification) + "\n"
                        stdin.write(notif_str.encode())
                        await stdin.drain()

                        # Test with tools/list to verify connection
                        await self._make_request("tools/list")

                    async def _make_request(
                        self,
                        method: str,
                        params: dict[str, Any] | None = None,
                    ) -> dict[str, Any]:
                        if not self.process or not self.process.stdin or not self.process.stdout:
                            raise RuntimeError("Process not initialized")

                        import json

                        request_id = self._get_next_id()
                        request: JSONDict = {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "method": method,
                            "params": params or {},
                        }

                        # Send request
                        request_str = json.dumps(request) + "\n"
                        self.process.stdin.write(request_str.encode())
                        await self.process.stdin.drain()

                        # Read responses until we get the one matching our request ID
                        max_attempts = 10  # Prevent infinite loops
                        attempts = 0

                        while attempts < max_attempts:
                            response_line = await self.process.stdout.readline()

                            if not response_line:
                                raise Exception("MCP server closed connection")

                            try:
                                response = json.loads(
                                    response_line.decode().strip(),
                                )

                                # Check if this is a response to our request
                                if "id" in response and response["id"] == request_id:
                                    if "error" in response:
                                        raise Exception(
                                            f"MCP error: {response['error']}",
                                        )
                                    return response
                                # Ignore notifications and responses to other requests

                            except json.JSONDecodeError:
                                # Ignore malformed JSON
                                pass

                            attempts += 1

                        raise Exception(
                            f"No matching response received for request ID {request_id} after {max_attempts} attempts",
                        )

                    async def call_tool(
                        self,
                        name: str,
                        arguments: dict[str, Any],
                    ) -> Any:
                        try:
                            response = await self._make_request(
                                "tools/call",
                                {
                                    "name": name,
                                    "arguments": arguments,
                                },
                            )
                            result_data = response.get("result", {})

                            class ToolCallResult:
                                def __init__(self, data: dict[str, Any]) -> None:
                                    self.content = data.get("content", [])

                            return ToolCallResult(result_data)
                        except Exception as e:
                            logger.error(f"MCP error: {e}")
                            logger.error(f"Name: {name}")
                            logger.error(f"Request: {arguments}")
                            raise Exception(f"MCP error: {e}")

                # Create and initialize new session
                session = DirectMCPSession(
                    config.command,
                    config.args or [],
                    merged_env,
                )
                await session.initialize()
                return session

        # For other session types, try to use the original session
        # This is a fallback - ideally all MCP sessions should be recreatable
        return self.client_session

    def _execute_sync(self, **kwargs: Any) -> dict[str, Any]:
        """Execute the MCP tool synchronously."""
        # Always create a new event loop to avoid "Future attached to a different loop" errors
        # This is necessary when running in multi-threaded environments like ThreadPoolExecutor
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(self._execute_async(**kwargs))
        finally:
            # Clean up the event loop
            try:
                loop.close()
            except Exception:
                pass
            # Reset to previous loop if it exists
            try:
                asyncio.get_event_loop()
            except RuntimeError:
                # No existing loop, that's fine
                pass

    def execute(self, **kwargs: Any) -> dict[str, Any]:
        """Execute the MCP tool synchronously (for backward compatibility)."""
        # Filter out agent_state and global_storage parameters as they're not needed for MCP tools
        # and cause JSON serialization errors
        filtered_kwargs = {k: v for k, v in kwargs.items() if k not in ("agent_state", "global_storage")}

        def _sort_key(item: tuple[str, Any]) -> str:
            return item[0]

        filtered_kwargs = dict(
            sorted(filtered_kwargs.items(), key=_sort_key),
        )
        return self._sync_executor(**filtered_kwargs)

    async def _execute_async(self, **kwargs: Any) -> dict[str, Any]:
        """Execute the MCP tool asynchronously."""
        try:
            # Create a thread-local session to avoid event loop conflicts
            session = await self._get_thread_local_session()
            result = await session.call_tool(self.name, kwargs)

            if hasattr(result, "content"):
                result_content = getattr(result, "content")
                if isinstance(result_content, list):
                    # Handle list of content items
                    content_items: list[str] = []
                    for item in cast(list[Any], result_content):
                        if hasattr(item, "text"):
                            text_value = getattr(item, "text")
                            content_items.append(str(text_value))
                            continue

                        if isinstance(item, dict):
                            item_dict = cast(dict[str, Any], item)
                            if "text" in item_dict:
                                content_items.append(str(item_dict["text"]))
                                continue
                            content_items.append(str(item_dict))
                            continue

                        else:
                            content_items.append(str(item))
                    return {"result": "\n".join(content_items)}
                else:
                    return {"result": str(result_content)}
            else:
                return {"result": str(result)}

        except Exception as e:
            logger.error(f"Error executing MCP tool '{self.name}': {e}")
            return {"error": str(e)}


class MCPClient:
    """Client for connecting to and managing MCP servers."""

    def __init__(self):
        self.servers: dict[str, MCPServerConfig] = {}
        self.sessions: dict[str, Any] = {}
        self.tools: dict[str, MCPTool] = {}

    def add_server(self, config: MCPServerConfig) -> None:
        """Add an MCP server configuration."""
        self.servers[config.name] = config
        logger.info(f"Added MCP server configuration: {config.name}")

    async def connect_to_server(self, server_name: str) -> bool:
        """Connect to an MCP server and initialize the session."""
        if server_name not in self.servers:
            logger.error(f"Server '{server_name}' not found in configurations")
            return False

        config = self.servers[server_name]

        try:
            if config.type == "stdio":
                # Stdio server
                if not config.command:
                    logger.error(
                        f"Command required for stdio server '{server_name}'",
                    )
                    return False

                server_params = StdioServerParameters(
                    command=config.command,
                    args=config.args or [],
                    env=config.env or {},
                )

                # Use asyncio.wait_for for timeout protection
                timeout_duration = config.timeout or 30

                # Use direct subprocess approach with proper JSON-RPC protocol
                logger.info(
                    f"Starting stdio MCP server: {config.command} {' '.join(config.args or [])}",
                )

                # Override the environment to preserve PATH
                import os

                merged_env = os.environ.copy()
                if server_params.env:
                    merged_env.update(server_params.env)

                # Create a direct subprocess-based MCP session
                class DirectMCPSession:
                    def __init__(
                        self,
                        command: str,
                        args: list[str],
                        env: dict[str, str],
                    ) -> None:
                        self.command = command
                        self.args = args
                        self.env = env
                        self.process: Process | None = None
                        self._request_id = 0

                    async def initialize(self) -> "DirectMCPSession":
                        import subprocess

                        self.process = await asyncio.create_subprocess_exec(
                            self.command,
                            *self.args,
                            limit=1024 * 128,  # set a large buffer size
                            env=self.env,
                            stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                        )

                        if not self.process.stdout or not self.process.stdin:
                            raise RuntimeError("Failed to get process streams")

                        # Initialize the MCP connection properly
                        await self._initialize_connection()
                        return self

                    def _get_next_id(self) -> int:
                        self._request_id += 1
                        return self._request_id

                    async def _initialize_connection(self) -> None:
                        """Initialize the MCP connection with proper handshake."""
                        import json

                        # Send initialize request
                        init_request: JSONDict = {
                            "jsonrpc": "2.0",
                            "id": self._get_next_id(),
                            "method": "initialize",
                            "params": {
                                "protocolVersion": "2024-11-05",
                                "capabilities": {
                                    "roots": {
                                        "listChanged": True,
                                    },
                                    "sampling": {},
                                },
                                "clientInfo": {
                                    "name": "nexau-mcp-client",
                                    "version": "1.0.0",
                                },
                            },
                        }

                        # Send the initialize request
                        init_str = json.dumps(init_request) + "\n"
                        process = self.process
                        if process is None or process.stdin is None or process.stdout is None:
                            raise RuntimeError("Process streams not initialized")
                        stdin = process.stdin
                        stdout = process.stdout
                        stdin.write(init_str.encode())
                        await stdin.drain()

                        # Read the initialize response
                        while True:
                            try:
                                response_line = await asyncio.wait_for(
                                    stdout.readline(),
                                    timeout=60,
                                )
                                response = json.loads(
                                    response_line.decode().strip(),
                                )
                                break
                            except TimeoutError:
                                raise RuntimeError(
                                    "MCP initialization timeout",
                                )
                            except json.JSONDecodeError:
                                continue

                        if "error" in response:
                            raise Exception(
                                f"MCP initialization error: {response['error']}",
                            )

                        # Send initialized notification
                        initialized_notification: JSONDict = {
                            "jsonrpc": "2.0",
                            "method": "notifications/initialized",
                            "params": {},
                        }

                        notif_str = json.dumps(initialized_notification) + "\n"
                        stdin.write(notif_str.encode())
                        await stdin.drain()

                        # Test with tools/list to verify connection
                        await self._make_request("tools/list")

                    async def _make_request(
                        self,
                        method: str,
                        params: dict[str, Any] | None = None,
                    ) -> dict[str, Any]:
                        if not self.process or not self.process.stdin or not self.process.stdout:
                            raise RuntimeError("Process not initialized")

                        import json

                        request: JSONDict = {
                            "jsonrpc": "2.0",
                            "id": self._get_next_id(),
                            "method": method,
                            "params": params or {},
                        }

                        # Send request
                        request_str = json.dumps(request) + "\n"
                        self.process.stdin.write(request_str.encode())
                        await self.process.stdin.drain()

                        # Read response
                        response_line = await self.process.stdout.readline()
                        response = json.loads(response_line.decode().strip())

                        if "error" in response:
                            raise Exception(f"MCP error: {response['error']}")

                        return response

                    async def list_tools(self) -> Any:
                        response = await self._make_request("tools/list")
                        tools_data = response.get(
                            "result",
                            {},
                        ).get("tools", [])

                        # Create tool objects
                        class SimpleTool:
                            def __init__(self, data: dict[str, Any]) -> None:
                                self.name = data["name"]
                                self.description = data.get("description", "")
                                self.inputSchema = data.get("inputSchema", {})

                        class ToolResult:
                            def __init__(self, tools: Sequence[dict[str, Any]]) -> None:
                                self.tools = [SimpleTool(tool) for tool in tools]

                        return ToolResult(tools_data)

                    async def call_tool(
                        self,
                        name: str,
                        arguments: dict[str, Any],
                    ) -> Any:
                        response = await self._make_request(
                            "tools/call",
                            {
                                "name": name,
                                "arguments": arguments,
                            },
                        )
                        result_data = response.get("result", {})

                        class ToolCallResult:
                            def __init__(self, data: dict[str, Any]) -> None:
                                self.content = data.get("content", [])

                        return ToolCallResult(result_data)

                # Create and initialize the session
                session = DirectMCPSession(
                    server_params.command,
                    server_params.args,
                    merged_env,
                )
                await asyncio.wait_for(session.initialize(), timeout=timeout_duration)
                self.sessions[server_name] = session
                logger.info(
                    f"Successfully connected to stdio MCP server: {server_name}",
                )
                return True

            elif config.type == "http":
                # HTTP server with FastMCP protocol
                if not config.url:
                    logger.error(
                        f"URL required for HTTP server '{server_name}'",
                    )
                    return False

                # Use reasonable timeout for HTTP connections
                connection_timeout = config.timeout or 30

                try:
                    logger.info(
                        f"Connecting to HTTP MCP server '{server_name}'...",
                    )

                    # Ensure proper headers for FastMCP streamable HTTP protocol
                    headers = config.headers or {}
                    if "Accept" not in headers:
                        headers["Accept"] = STREAMABLE_HTTP_ACCEPT
                    if "Content-Type" not in headers:
                        headers["Content-Type"] = DEFAULT_CONTENT_TYPE

                    # Test connection with a simple session
                    test_session = HTTPMCPSession(
                        config,
                        headers,
                        connection_timeout,
                    )

                    # Test basic connectivity by trying to initialize
                    await test_session.initialize()
                    logger.info(
                        f"Successfully tested HTTP MCP server connection: {server_name}",
                    )

                    # Store the session configuration for later use
                    self.sessions[server_name] = HTTPMCPSession(
                        config,
                        headers,
                        connection_timeout,
                    )
                    logger.info(
                        f"Successfully connected to HTTP MCP server: {server_name}",
                    )
                    return True

                except TimeoutError:
                    logger.warning(
                        f"Connection to HTTP MCP server '{server_name}' timed out after {connection_timeout}s",
                    )
                    logger.warning(f"  URL: {config.url}")
                    return False
                except Exception as e:
                    logger.error(
                        f"Failed to connect to HTTP MCP server '{server_name}': {e}",
                    )
                    logger.debug(f"Error details: {e}")
                    return False

            else:
                logger.error(
                    f"Unknown server type '{config.type}' for server '{server_name}'",
                )
                return False

        except TimeoutError:
            logger.warning(
                f"Connection to MCP server '{server_name}' timed out",
            )
            return False
        except Exception as e:
            logger.error(
                f"Failed to connect to MCP server '{server_name}': {e}",
            )
            # Log more detailed error information for debugging
            import traceback

            logger.debug(
                f"Detailed error for '{server_name}': {traceback.format_exc()}",
            )
            return False

    async def discover_tools(self, server_name: str) -> list[MCPTool]:
        """Discover tools from an MCP server."""
        if server_name not in self.sessions:
            logger.error(f"No active session for server '{server_name}'")
            return []

        session = self.sessions[server_name]

        try:
            # List available tools
            tools_result = await session.list_tools()

            # Convert MCP tools to NexAU tools
            discovered_tools: list[MCPTool] = []
            server_config = self.servers.get(server_name)

            for mcp_tool in tools_result.tools:
                # For HTTPMCPSession, we need to pass the session directly
                # For regular ClientSession, we also pass it directly
                # Convert SimpleTool to MCPToolType-like object for compatibility
                if hasattr(mcp_tool, "inputSchema"):
                    tool = MCPTool(mcp_tool, session, server_config)
                else:
                    # Handle SimpleTool case by converting to proper format
                    tool = MCPTool(mcp_tool, session, server_config)
                discovered_tools.append(tool)

                # Store in registry with server prefix to avoid conflicts
                tool_key = f"{server_name}.{mcp_tool.name}"
                self.tools[tool_key] = tool

            logger.info(
                f"Discovered {len(discovered_tools)} tools from server '{server_name}'",
            )
            return discovered_tools

        except Exception as e:
            logger.error(
                f"Failed to discover tools from server '{server_name}': {e}",
            )
            # Log more details for debugging
            import traceback

            logger.debug(f"Detailed error: {traceback.format_exc()}")
            return []

    def get_tool(self, tool_name: str) -> MCPTool | None:
        """Get a tool by name."""
        return self.tools.get(tool_name)

    def get_all_tools(self) -> list[MCPTool]:
        """Get all discovered tools."""
        return list(self.tools.values())

    async def disconnect_server(self, server_name: str) -> None:
        """Disconnect from an MCP server."""
        if server_name in self.sessions:
            try:
                # session = self.sessions[server_name]
                # The session should handle cleanup automatically
                # when the context manager exits
                del self.sessions[server_name]

                # Remove tools from this server
                tools_to_remove = [k for k in self.tools.keys() if k.startswith(f"{server_name}.")]
                for tool_key in tools_to_remove:
                    del self.tools[tool_key]

                logger.info(f"Disconnected from MCP server: {server_name}")
            except Exception as e:
                logger.error(
                    f"Error disconnecting from server '{server_name}': {e}",
                )

    async def disconnect_all(self) -> None:
        """Disconnect from all MCP servers."""
        for server_name in list(self.sessions.keys()):
            await self.disconnect_server(server_name)


class MCPManager:
    """High-level manager for MCP operations."""

    def __init__(self):
        self.client = MCPClient()
        self.auto_connect = True

    def add_server(
        self,
        name: str,
        server_type: str = "stdio",
        command: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        url: str | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        disable_parallel: bool = False,
    ) -> None:
        """Add an MCP server configuration."""
        config = MCPServerConfig(
            name=name,
            type=server_type,
            command=command,
            args=args,
            env=env,
            url=url,
            headers=headers,
            timeout=timeout,
            disable_parallel=disable_parallel,
        )
        self.client.add_server(config)

    async def initialize_servers(self) -> dict[str, list[MCPTool]]:
        """Initialize all configured servers and discover their tools concurrently."""
        all_tools: dict[str, list[MCPTool]] = {}
        server_names = list(self.client.servers.keys())

        if not server_names:
            logger.info("No MCP servers configured, skipping initialization")
            return all_tools

        logger.info(f"Initializing {len(server_names)} MCP servers in parallel: {server_names}")

        async def init_server(server_name: str) -> tuple[str, list[MCPTool]]:
            """Initialize a single server and discover its tools."""
            if await self.client.connect_to_server(server_name):
                tools = await self.client.discover_tools(server_name)
                return server_name, tools
            return server_name, []

        # Run all server initializations concurrently
        results = await asyncio.gather(
            *[init_server(name) for name in server_names],
            return_exceptions=True,
        )

        # Process results
        success_count = 0
        failed_servers: list[str] = []

        for i, result in enumerate(results):
            server_name = server_names[i]
            if isinstance(result, BaseException):
                failed_servers.append(server_name)
                logger.error(
                    f"Failed to initialize MCP server '{server_name}': {result}",
                )
                logger.debug(f"Detailed error for server '{server_name}': {result!r}")
                continue
            server_name, tools = result
            all_tools[server_name] = tools
            success_count += 1

        # Summary log
        total_tools = sum(len(tools) for tools in all_tools.values())
        if failed_servers:
            logger.warning(
                f"MCP initialization completed: {success_count}/{len(server_names)} servers succeeded, "
                f"{len(failed_servers)} failed ({failed_servers}), {total_tools} tools discovered"
            )
        else:
            logger.info(
                f"MCP initialization completed: {success_count}/{len(server_names)} servers succeeded, {total_tools} tools discovered"
            )

        return all_tools

    def get_available_tools(self) -> Sequence[Tool]:
        """Get all available MCP tools."""
        return self.client.get_all_tools()

    async def shutdown(self) -> None:
        """Shutdown the MCP manager and disconnect all servers."""
        await self.client.disconnect_all()


# Global MCP manager instance
_mcp_manager: MCPManager | None = None


def get_mcp_manager() -> MCPManager:
    """Get the global MCP manager instance."""
    global _mcp_manager
    if _mcp_manager is None:
        _mcp_manager = MCPManager()
    return _mcp_manager


async def initialize_mcp_tools(server_configs: list[dict[str, Any]]) -> Sequence[Tool]:
    """Initialize MCP tools from server configurations.

    Args:
        server_configs: List of server configuration dictionaries with keys:
            - name: Server name
            - type: Server type ("stdio" or "http")
            For stdio servers:
            - command: Command to run
            - args: Command arguments
            - env: Optional environment variables
            For HTTP servers:
            - url: Server URL
            - headers: Optional HTTP headers
            - timeout: Optional timeout in seconds

    Returns:
        List of initialized MCP tools
    """
    manager = get_mcp_manager()

    # Add server configurations
    for config in server_configs:
        manager.add_server(
            name=config["name"],
            server_type=config.get("type", "stdio"),
            command=config.get("command"),
            args=config.get("args"),
            env=config.get("env"),
            url=config.get("url"),
            headers=config.get("headers"),
            timeout=config.get("timeout"),
            disable_parallel=config.get("disable_parallel", False),
        )

    # Initialize servers and discover tools
    await manager.initialize_servers()

    # Return all available tools
    return manager.get_available_tools()


def sync_initialize_mcp_tools(server_configs: list[dict[str, Any]]) -> Sequence[Tool]:
    """Synchronous wrapper for initialize_mcp_tools."""
    # Always create a new event loop to avoid "Future attached to a different loop" errors
    # This matches the pattern used in MCPTool._execute_sync
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        return loop.run_until_complete(initialize_mcp_tools(server_configs))
    finally:
        # Clean up the event loop
        try:
            loop.close()
        except Exception:
            pass
        # Reset to previous loop if it exists
        try:
            asyncio.get_event_loop()
        except RuntimeError:
            # No existing loop, that's fine
            pass
