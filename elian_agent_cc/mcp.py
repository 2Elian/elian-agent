"""
MCP (Model Context Protocol) client implementation.

Ported from src/services/mcp/ (23 files).
Supports stdio and SSE transport for connecting MCP servers.
Tools are namespaced as mcp__<server>__<tool>.
"""
import asyncio
import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class MCPTransportType(str, Enum):
    STDIO = "stdio"
    SSE = "sse"
    HTTP = "http"
    WEBSOCKET = "websocket"


class ConfigScope(str, Enum):
    LOCAL = "local"
    USER = "user"
    PROJECT = "project"
    DYNAMIC = "dynamic"
    ENTERPRISE = "enterprise"


@dataclass
class MCPServerConfig:
    """Configuration for a single MCP server."""
    name: str
    transport: MCPTransportType = MCPTransportType.STDIO
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str = ""
    scope: ConfigScope = ConfigScope.USER
    disabled: bool = False


@dataclass
class MCPToolDef:
    """Tool definition received from an MCP server."""
    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    server_name: str = ""

    @property
    def full_name(self) -> str:
        return f"mcp__{self.server_name}__{self.name}"


@dataclass
class MCPResourceDef:
    """Resource definition from an MCP server."""
    uri: str
    name: str = ""
    description: str = ""
    mime_type: str = ""


@dataclass
class MCPPromptDef:
    """Prompt definition from an MCP server."""
    name: str
    description: str = ""
    arguments: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class MCPConnection:
    """Runtime connection to an MCP server."""
    server_name: str
    config: MCPServerConfig
    is_connected: bool = False
    tools: list[MCPToolDef] = field(default_factory=list)
    prompts: list[MCPPromptDef] = field(default_factory=list)
    resources: list[MCPResourceDef] = field(default_factory=list)
    _process: asyncio.subprocess.Process | None = None


class MCPClient:
    """Manages MCP server connections and tool/resource discovery."""

    def __init__(self):
        self._configs: list[MCPServerConfig] = []
        self._connections: dict[str, MCPConnection] = {}
        self._tool_registry: dict[str, MCPToolDef] = {}

    def add_config(self, config: MCPServerConfig) -> None:
        self._configs.append(config)

    def set_configs(self, configs: list[MCPServerConfig]) -> None:
        self._configs = configs

    def get_configs(self) -> list[MCPServerConfig]:
        return list(self._configs)

    async def connect_all(self) -> dict[str, bool]:
        """Connect to all configured MCP servers. Returns {server_name: success}."""
        results = {}
        for config in self._configs:
            if config.disabled:
                continue
            results[config.name] = await self.connect(config.name)
        return results

    async def connect(self, server_name: str) -> bool:
        """Connect to a specific MCP server."""
        config = next((c for c in self._configs if c.name == server_name), None)
        if not config:
            return False

        conn = MCPConnection(server_name=server_name, config=config)

        try:
            if config.transport == MCPTransportType.STDIO:
                conn = await self._connect_stdio(config, conn)
            elif config.transport == MCPTransportType.SSE:
                conn = await self._connect_sse(config, conn)

            # Discover capabilities
            if conn.is_connected:
                await self._discover(conn)
                self._connections[server_name] = conn
                self._register_tools(conn)
                return True
        except Exception as e:
            conn.is_connected = False

        return False

    async def _connect_stdio(self, config: MCPServerConfig, conn: MCPConnection) -> MCPConnection:
        """Connect via stdio: spawn subprocess, communicate via JSON-RPC on stdin/stdout."""
        try:
            cmd = [config.command] + config.args
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**__import__('os').environ, **config.env},
            )
            conn._process = proc

            # Send initialize request
            init_request = json.dumps({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "claude-code-python", "version": "0.2.0"},
                },
            })
            if proc.stdin:
                proc.stdin.write((init_request + "\n").encode())
                await proc.stdin.drain()

                # Read response
                if proc.stdout:
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=10)
                    response = json.loads(line.decode())
                    if "result" in response:
                        conn.is_connected = True
        except Exception:
            conn.is_connected = False

        return conn

    async def _connect_sse(self, config: MCPServerConfig, conn: MCPConnection) -> MCPConnection:
        """Connect via SSE transport."""
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(config.url, ssl=False, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        conn.is_connected = True
        except Exception:
            conn.is_connected = False
        return conn

    async def _discover(self, conn: MCPConnection) -> None:
        """Discover tools, prompts, and resources from a connected server."""
        if not conn._process or not conn._process.stdin:
            return

        # Discover tools
        tools = await self._send_request(conn._process, "tools/list", {})
        if tools and "tools" in tools:
            conn.tools = [
                MCPToolDef(
                    name=t.get("name", ""),
                    description=t.get("description", ""),
                    input_schema=t.get("inputSchema", {}),
                    server_name=conn.server_name,
                )
                for t in tools["tools"]
            ]

        # Discover resources
        resources = await self._send_request(conn._process, "resources/list", {})
        if resources and "resources" in resources:
            conn.resources = [
                MCPResourceDef(
                    uri=r.get("uri", ""),
                    name=r.get("name", ""),
                    description=r.get("description", ""),
                    mime_type=r.get("mimeType", ""),
                )
                for r in resources["resources"]
            ]

    async def _send_request(self, proc, method: str, params: dict) -> dict | None:
        """Send a JSON-RPC request and get response."""
        try:
            request = json.dumps({
                "jsonrpc": "2.0",
                "id": __import__('random').randint(1, 10000),
                "method": method,
                "params": params,
            })
            if proc.stdin:
                proc.stdin.write((request + "\n").encode())
                await proc.stdin.drain()
            if proc.stdout:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=10)
                return json.loads(line.decode()).get("result")
        except Exception:
            pass
        return None

    def _register_tools(self, conn: MCPConnection) -> None:
        """Register MCP tools in the global tool registry."""
        from tools.base import tool_registry, Tool, ToolResult
        from models import ToolUseContext

        for mcp_tool in conn.tools:
            full_name = mcp_tool.full_name

            class MCPBridgeTool(Tool):
                name = full_name
                description = mcp_tool.description
                input_schema = mcp_tool.input_schema
                is_mcp = True

                async def call(self, params, context):
                    if conn._process and conn._process.stdin:
                        try:
                            result = await self._mcp_call(conn._process, mcp_tool.name, params)
                            return ToolResult(content=result)
                        except Exception as e:
                            return ToolResult(content=str(e), is_error=True)
                    return ToolResult(content="MCP server not connected", is_error=True)

                async def _mcp_call(self, proc, tool_name, args):
                    req = json.dumps({
                        "jsonrpc": "2.0", "id": __import__('random').randint(1, 10000),
                        "method": "tools/call",
                        "params": {"name": tool_name, "arguments": args},
                    })
                    if proc.stdin:
                        proc.stdin.write((req + "\n").encode())
                        await proc.stdin.drain()
                    if proc.stdout:
                        line = await asyncio.wait_for(proc.stdout.readline(), timeout=30)
                        resp = json.loads(line.decode())
                        if "result" in resp:
                            content = resp["result"].get("content", [])
                            if isinstance(content, list):
                                return "\n".join(
                                    c.get("text", str(c))
                                    for c in content
                                    if isinstance(c, dict)
                                )
                            return str(content)
                    return "No response from MCP server"

            tool_registry.register(MCPBridgeTool())
            self._tool_registry[full_name] = mcp_tool

    def list_all_tools(self) -> list[dict[str, Any]]:
        """List all MCP tools across all connected servers."""
        result = []
        for name, conn in self._connections.items():
            for tool in conn.tools:
                result.append({
                    "name": tool.full_name,
                    "description": tool.description,
                    "server": name,
                    "input_schema": tool.input_schema,
                })
        return result

    def get_mcp_instructions(self) -> str:
        """Generate MCP instructions for the system prompt."""
        lines = []
        for name, conn in self._connections.items():
            if conn.is_connected:
                lines.append(f"- **{name}**: {len(conn.tools)} tools, {len(conn.resources)} resources")
            else:
                lines.append(f"- **{name}**: disconnected")
        return "\n".join(lines) if lines else ""

    async def disconnect(self, server_name: str) -> None:
        conn = self._connections.pop(server_name, None)
        if conn and conn._process:
            conn._process.kill()
            await conn._process.wait()
        # Unregister tools
        to_remove = [k for k in self._tool_registry if k.startswith(f"mcp__{server_name}__")]
        from tools.base import tool_registry
        for k in to_remove:
            tool_registry.unregister(k)
            del self._tool_registry[k]

    async def disconnect_all(self) -> None:
        for name in list(self._connections.keys()):
            await self.disconnect(name)


# Global MCP client
mcp_client = MCPClient()
