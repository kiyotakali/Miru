"""BaseTool abstract base class for ContextLife tools."""

from abc import ABC, abstractmethod


class BaseTool(ABC):
    """Abstract base class for all tools.

    Each tool must define:
      - name: str           — tool name (e.g. "add_commitment")
      - description: str    — tool description for LLM
      - input_schema: dict  — JSON Schema for input parameters
      - tags: set           — e.g. {"write"} or {"read", "query"}

    And implement:
      - execute(args: dict) -> Any
    """

    name: str = ""
    description: str = ""
    input_schema: dict = {}
    tags: set = set()

    @abstractmethod
    def execute(self, args: dict):
        """Execute the tool with the given arguments. Returns a JSON-serializable result."""
        ...

    def to_anthropic_tool(self) -> dict:
        """Convert to Anthropic API tool format."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }

    def to_openai_tool(self) -> dict:
        """Convert to OpenAI API tool format."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }

    def to_tool(self, provider: str = "openai") -> dict:
        """Convert to the specified provider's tool format."""
        if provider == "anthropic":
            return self.to_anthropic_tool()
        return self.to_openai_tool()

    # Legacy alias
    def to_claude_tool(self) -> dict:
        return self.to_anthropic_tool()
