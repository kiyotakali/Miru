"""MCP Server adapter — exposes all ContextLife tools via FastMCP.

Usage:
    python -m tools.mcp_server          # stdio mode (Claude Desktop / NanoClaw)
    python -m tools.mcp_server --sse    # SSE mode (HTTP accessible)
"""

import json
import sys


def create_mcp_server():
    from fastmcp import FastMCP
    from tools import get_registry

    registry = get_registry()
    server = FastMCP(name="ContextLife")

    for tool in registry.all():
        # Capture tool in closure
        def _make_handler(t=tool):
            def handler(**kwargs):
                result = t.execute(kwargs)
                # FastMCP expects string or dict return
                if isinstance(result, (dict, list)):
                    return json.dumps(result, ensure_ascii=False)
                return str(result)
            handler.__name__ = t.name
            handler.__doc__ = t.description
            return handler

        server.add_tool(
            _make_handler(),
            name=tool.name,
            description=tool.description,
        )

    return server


def main():
    server = create_mcp_server()
    if "--sse" in sys.argv:
        server.run(transport="sse")
    else:
        server.run()


if __name__ == "__main__":
    main()
