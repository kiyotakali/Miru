"""Daily sleep/work pattern maintenance.

This used to live inside the old CareEngine module.  AttentionEngine is now the
runtime for Miru's continuous presence, while this file owns the unrelated
nightly maintenance job that updates ``memory/patterns``.
"""

from __future__ import annotations

import user_settings
import memory


def update_daily_patterns():
    """Extract sleep/work patterns from today's data.

    Called during nightly maintenance.  This function is deliberately separate
    from AttentionEngine: it writes coarse patterns for future context, but it
    never decides whether Miru should speak.
    """
    try:
        today = user_settings.user_now().strftime("%Y-%m-%d")

        first_time = None
        last_time = None
        try:
            from sleep_inference import infer_daily_activity
            daily = infer_daily_activity(today)
            first_time = daily.get("earliest")
            last_time = daily.get("latest")
        except Exception as e:
            print(f"[DailyPatterns] sleep_inference failed: {e}")

        if not first_time or not last_time:
            import storage as _st
            history = _st.get_chat_history(limit=200)
            today_times = [
                m.get("time", "") for m in history
                if m.get("time", "").startswith(today)
                and m.get("role") == "user"
            ]
            if not today_times:
                return
            first_time = today_times[0].split(" ")[-1][:5]
            last_time = today_times[-1].split(" ")[-1][:5]

        sleep_content = memory.read_file("patterns/sleep.md") or ""
        lines = [l for l in sleep_content.strip().split("\n") if l.strip()]

        recent_lines = []
        for line in lines:
            if line.startswith("20") and line[:10] != today:
                try:
                    from datetime import datetime as _dt
                    entry_date = _dt.strptime(line[:10], "%Y-%m-%d")
                    age = (user_settings.user_now() - entry_date).days
                    if age <= 7:
                        recent_lines.append(line)
                except Exception:
                    recent_lines.append(line)
            elif not line.startswith("20"):
                pass

        recent_lines.append(f"{today}: 首条消息 ~{first_time}, 末条消息 ~{last_time}")

        if len(recent_lines) >= 3:
            firsts = []
            lasts = []
            for line in recent_lines:
                if "首条消息" in line and "末条消息" in line:
                    try:
                        parts = line.split("首条消息 ~")[1]
                        first = parts.split(",")[0].strip()
                        last = line.split("末条消息 ~")[1].strip()
                        firsts.append(first)
                        lasts.append(last)
                    except Exception:
                        continue
            if firsts:
                def _hhmm_to_minutes(s: str) -> int:
                    h, m = map(int, s.split(":"))
                    return h * 60 + m

                def _minutes_to_hhmm(m: int) -> str:
                    return f"{m // 60:02d}:{m % 60:02d}"

                try:
                    f_mins = sorted(_hhmm_to_minutes(x) for x in firsts)
                    l_mins = sorted(_hhmm_to_minutes(x) for x in lasts)
                    f_med = _minutes_to_hhmm(f_mins[len(f_mins) // 2])
                    l_med = _minutes_to_hhmm(l_mins[len(l_mins) // 2])
                    summary = (
                        f"近{len(firsts)}天平均: 首条消息 ~{f_med}, "
                        f"末条消息 ~{l_med}"
                    )
                    recent_lines.append(summary)
                except (ValueError, AttributeError):
                    pass

        memory.write_file("patterns/sleep.md", "\n".join(recent_lines))
        print(f"[DailyPatterns] Updated patterns/sleep.md ({len(recent_lines)} entries)")

        analyzer_obs = []
        try:
            from screen_analyzer import get_analyzer
            all_obs = get_analyzer().get_all_recent_observations(max_age_minutes=720)
            for obs in all_obs:
                analyzer_obs.append(
                    f"{obs['time'].strftime('%H:%M')}: {obs['observation'][:80]}"
                )
        except Exception:
            pass

        if analyzer_obs:
            work_content = memory.read_file("patterns/work.md") or ""
            work_lines = [l for l in work_content.strip().split("\n") if l.strip()]
            work_lines = [l for l in work_lines if not l.startswith(today)]
            work_lines = [l for l in work_lines if l.startswith("20")][-6:]
            obs_summary = "; ".join(analyzer_obs[:8])
            work_lines.append(f"{today}: {obs_summary}")
            memory.write_file("patterns/work.md", "\n".join(work_lines))
            print("[DailyPatterns] Updated patterns/work.md")

    except Exception as e:
        print(f"[DailyPatterns] Pattern update failed (non-fatal): {e}")
