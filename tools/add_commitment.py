"""Tool: add_commitment — Create a new commitment (writes to memory files)."""

from datetime import datetime

from tools.base import BaseTool


class AddCommitmentTool(BaseTool):
    name = "add_commitment"
    description = "创建一个新的承诺或行动项。当用户明确表达想做/需要做某事时调用。"
    input_schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "承诺/任务标题"},
            "due": {"type": "string", "description": "截止时间 YYYY-MM-DD 或 YYYY-MM-DD HH:MM（有精确时间时用后者），没有则留空"},
            "detail": {"type": "string", "description": "具体内容（可选）"},
        },
        "required": ["title"],
    }
    tags = {"write"}

    def execute(self, args: dict):
        import memory

        title = args.get("title", "").strip()
        if not title:
            return {"status": "error", "message": "缺少标题"}

        due = args.get("due", "")
        detail = args.get("detail", "")
        # Seconds precision so two commitments in the same minute get distinct IDs.
        # (parse_commitments derives cid from added-timestamp digits; minute-level
        # precision collapses multiple same-response commits into one.)
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Build the commitment line
        line = f"- [ ] {title}"
        if due:
            line += f" (deadline: {due})"
        if detail:
            line += f" -- {detail}"
        line += f"  [added: {now}]"

        # Read or create active.md
        memory.ensure_dirs()
        content = memory.read_file("commitments/active.md")
        if content is None:
            content = "# Active Commitments\n"

        # Append the new commitment
        if not content.endswith("\n"):
            content += "\n"
        content += line + "\n"
        memory.write_file("commitments/active.md", content)

        try:
            from core import _broadcast_commitment_sync
            _broadcast_commitment_sync()
        except Exception:
            pass

        return {"status": "ok", "title": title, "due": due}
