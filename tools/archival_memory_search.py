"""Tool: archival_memory_search — Search + read from archival memory."""

from tools.base import BaseTool


class ArchivalMemorySearchTool(BaseTool):
    name = "archival_memory_search"
    description = (
        "搜索长期记忆（归档记忆）。输入关键词搜索所有记忆文件，"
        "返回匹配的文件路径和内容摘要。也可以指定路径直接读取某个文件。"
        "核心记忆已在上下文中，此工具用于查找更详细的归档信息。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索关键词（自然语言），或 'index' 查看记忆索引",
            },
            "path": {
                "type": "string",
                "description": "直接读取指定文件路径。slot 文件用 <domain>/<slot_id>/main.md 格式（如 projects/contextlife/main.md、people/wei_jiazhe/main.md、topics/<id>/main.md、self/identity/main.md）；commitment/journal/patterns 是扁平文件（如 commitments/active.md、journal/2026-05-10.md）。提供 path 时忽略 query",
            },
            "max_results": {
                "type": "integer",
                "description": "最大返回数量（默认5）",
            },
        },
        "required": [],
    }
    tags = {"read", "query", "memory"}

    def execute(self, args: dict):
        import memory

        # Direct file read mode
        path = args.get("path", "").strip()
        if path:
            if path == "index":
                return {"content": memory.read_index(), "path": "index.md"}
            content = memory.read_file(path)
            if content is None:
                return {"error": f"File not found: {path}"}
            return {"content": content, "path": path}

        # Search mode
        query = args.get("query", "").strip()
        if not query:
            return {"error": "Either query or path is required"}

        if query == "index":
            return {"content": memory.read_index(), "path": "index.md"}

        max_results = args.get("max_results", 5)
        results = memory.search(query, max_results=max_results)

        # For top results, include more content
        enriched = []
        for r in results[:3]:
            content = memory.read_file(r["path"])
            if content and len(content) > 300:
                content = content[:300] + "..."
            r["content"] = content
            enriched.append(r)
        # Remaining results keep only snippets
        enriched.extend(results[3:])

        return {"results": enriched, "total": len(results)}
