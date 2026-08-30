"""Retired CareEngine compatibility shim.

The old CareEngine proactive sender has been removed from production.  Miru's
continuous attention now lives in ``attention_engine.py``:

    AttentionEngine -> attention_intent_queue.json -> proactive main agent

This module intentionally keeps only tiny compatibility symbols so old cleanup
paths/tests that refer to ``care_engine._instances`` do not crash.  New code
must not instantiate CareEngine or route proactive delivery through it.
"""

from __future__ import annotations

from daily_patterns import update_daily_patterns


_instances: dict[str, object] = {}


class CareEngine:
    """Retired placeholder.

    Instantiating this class is a bug.  Use ``attention_engine.get_attention_engine``
    for live attention, or ``daily_patterns.update_daily_patterns`` for nightly
    pattern maintenance.
    """

    def __init__(self, *args, **kwargs):
        raise RuntimeError(
            "CareEngine 已退场：主动陪伴请使用 attention_engine；"
            "每日 pattern 维护请使用 daily_patterns.update_daily_patterns。"
        )


def get_care_engine(*args, **kwargs):
    raise RuntimeError(
        "CareEngine 已退场：不要再创建旧 proactive sender；"
        "请使用 attention_engine.get_attention_engine。"
    )


def get_all_instances() -> dict[str, object]:
    """Return the legacy registry for cleanup code only."""
    return _instances


def stop_for_user(user_id: str) -> None:
    inst = _instances.pop(user_id, None)
    if inst is not None and hasattr(inst, "stop"):
        inst.stop()
