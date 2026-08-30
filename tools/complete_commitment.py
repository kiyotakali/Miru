"""Tool: complete_commitment — Mark a commitment as completed (memory files)."""

import re
from datetime import datetime

from tools.base import BaseTool


class CompleteCommitmentTool(BaseTool):
    name = "complete_commitment"
    description = "标记一个承诺/行动项为已完成。当用户说完成了/做好了/搞定了某事时调用。"
    input_schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "要完成的任务标题（模糊匹配）"},
        },
        "required": ["title"],
    }
    tags = {"write"}

    def execute(self, args: dict):
        import memory

        title = args.get("title", "").strip()
        if not title:
            return {"status": "error", "message": "缺少标题"}

        memory.ensure_dirs()
        content = memory.read_file("commitments/active.md")
        if not content:
            return {"status": "not_found", "title": title}

        # Find the line with matching title (fuzzy: title substring match)
        lines = content.split("\n")
        found_idx = None
        found_line = ""
        title_lower = title.lower()
        for i, line in enumerate(lines):
            if line.strip().startswith("- [ ]") and title_lower in line.lower():
                found_idx = i
                found_line = line
                break

        if found_idx is None:
            return {"status": "not_found", "title": title}

        # Mark as done: - [ ] → - [x]
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        completed_line = found_line.replace("- [ ]", "- [x]", 1)
        if "[completed:" not in completed_line:
            completed_line = completed_line.rstrip() + f"  [completed: {now}]"
        lines[found_idx] = completed_line
        memory.write_file("commitments/active.md", "\n".join(lines))

        # Also append to done.md for archive
        done_content = memory.read_file("commitments/done.md")
        if done_content is None:
            done_content = "# Completed Commitments\n"
        if not done_content.endswith("\n"):
            done_content += "\n"
        done_content += completed_line.strip() + "\n"
        memory.write_file("commitments/done.md", done_content)

        try:
            from core import _broadcast_commitment_sync
            _broadcast_commitment_sync()
        except Exception:
            pass

        return {"status": "ok", "title": title}
