"""Tool: look_at_screen — Look at the most recent screen observation.

In VPS-hosted deployments the chat agent runs server-side and has no local
desktop, so we cannot literally screencapture. Instead we read the most
recent vision-tier observation from any of the user's online devices —
that observation is at most ~10 minutes old (the screenshot tick interval),
which is fresh enough for "what am I doing" queries.

When called from a desktop client mode (DMG launcher) where the local
sensor *is* available, we still try a fresh screencapture first — fresher
data wins.
"""

from tools.base import BaseTool


class LookAtScreenTool(BaseTool):
    name = "look_at_screen"
    description = (
        "查看用户当前屏幕。返回最近一次屏幕观察的文字描述（最多 10 分钟内）。"
        "当用户问'我在干啥/我现在在做什么'或你需要了解他屏幕在显示什么时使用。"
        "无需参数。无最近观察时返回 status=stale。"
    )
    input_schema = {
        "type": "object",
        "properties": {},
        "required": [],
    }
    tags = {"read", "screen"}

    def execute(self, args: dict):
        # Fresh capture path (only works in client mode w/ desktop sensor)
        try:
            from sensor import get_sensor
            jpeg_bytes = get_sensor().capture_now()
        except Exception:
            jpeg_bytes = None

        if jpeg_bytes is not None:
            try:
                from screen_analyzer import get_analyzer
                result = get_analyzer().analyze_for_tool(jpeg_bytes)
                if result and result.get("observation"):
                    return {"status": "ok",
                            "observation": result["observation"],
                            "freshness": "live"}
            except Exception:
                pass

        # Fallback: most recent observation from any online device,
        # already vision-described upstream by the screenshot tick.
        try:
            from screen_analyzer import get_analyzer
            recents = get_analyzer().get_all_recent_observations(
                max_age_minutes=10
            )
        except Exception:
            recents = []

        if not recents:
            return {"status": "stale",
                    "message": "最近 10 分钟没有屏幕观察记录（可能截屏功能关闭，或没有活跃设备）"}

        top = recents[0]
        from datetime import datetime
        age = (datetime.now() - top["time"]).total_seconds() / 60
        return {
            "status": "ok",
            "observation": top["observation"],
            "freshness": f"{age:.1f}分钟前",
            "device": top.get("device_name", top.get("device_id", "")),
        }
