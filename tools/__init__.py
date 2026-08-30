"""ToolRegistry with auto-discovery for ContextLife tools."""

from __future__ import annotations

import importlib
import pkgutil
from typing import Dict, List, Optional

from tools.base import BaseTool


class ToolRegistry:
    """Registry that holds all BaseTool instances."""

    def __init__(self):
        self._tools: Dict[str, BaseTool] = {}

    def register(self, tool: BaseTool):
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[BaseTool]:
        return self._tools.get(name)

    def all(self) -> List[BaseTool]:
        return list(self._tools.values())

    def to_tools(self, provider: str = "openai") -> List[dict]:
        """Return all tools in the specified provider's format."""
        return [t.to_tool(provider) for t in self._tools.values()]

    def to_claude_tools(self) -> List[dict]:
        """Legacy alias: Return all tools in Anthropic format."""
        return self.to_tools("anthropic")

    def get_handlers(self) -> Dict[str, callable]:
        """Return {name: tool.execute} mapping for the agent loop."""
        return {name: tool.execute for name, tool in self._tools.items()}

    @classmethod
    def auto_discover(cls) -> "ToolRegistry":
        """Scan tools/ directory, collect BaseTool subclasses, and instantiate them."""
        registry = cls()
        package = importlib.import_module("tools")
        for importer, modname, ispkg in pkgutil.iter_modules(package.__path__):
            if modname in ("base", "mcp_server", "mcp_proxy", "__init__"):
                continue
            module = importlib.import_module(f"tools.{modname}")
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if (isinstance(attr, type)
                        and issubclass(attr, BaseTool)
                        and attr is not BaseTool):
                    registry.register(attr())
        return registry


_registry: Optional[ToolRegistry] = None


def get_registry() -> ToolRegistry:
    """Singleton: first call triggers auto_discover."""
    global _registry
    if _registry is None:
        _registry = ToolRegistry.auto_discover()
    return _registry
