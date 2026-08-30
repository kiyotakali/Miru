"""MCPProxyTool — wraps remote MCP Server tools as local BaseTool instances.

Used by ToolRegistry.load_mcp_servers() to consume external MCP tools
(e.g. GitHub, filesystem, etc.) and make them available to the agent loop.
"""

from tools.base import BaseTool


class MCPProxyTool(BaseTool):
    """Wraps a remote MCP Server tool as a local BaseTool."""

    tags = {"remote", "mcp"}

    def __init__(self, mcp_client, remote_tool):
        self.name = remote_tool["name"]
        self.description = remote_tool.get("description", "")
        self.input_schema = remote_tool.get("inputSchema", {"type": "object", "properties": {}})
        self._client = mcp_client

    def execute(self, args: dict):
        return self._client.call_tool(self.name, args)
