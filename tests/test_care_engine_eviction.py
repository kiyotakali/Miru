"""Legacy CareEngine cleanup registry tests.

Eviction of live proactive engines now belongs to AttentionEngine.  The old
care_engine registry remains only so logout/delete cleanup can safely remove
stale objects left by older builds.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def test_legacy_registry_can_be_cleaned_without_instantiating_engine():
    import care_engine

    saved = dict(care_engine._instances)
    try:
        class FakeStoppedEngine:
            def __init__(self):
                self.stop_count = 0

            def stop(self):
                self.stop_count += 1

        keep = FakeStoppedEngine()
        evict = FakeStoppedEngine()
        care_engine._instances.clear()
        care_engine._instances["u_keep"] = keep
        care_engine._instances["u_evict"] = evict

        engaged = {"u_keep"}
        for uid in [u for u in care_engine.get_all_instances() if u not in engaged]:
            inst = care_engine._instances[uid]
            if hasattr(inst, "stop"):
                inst.stop()
            care_engine._instances.pop(uid, None)

        assert "u_evict" not in care_engine._instances
        assert "u_keep" in care_engine._instances
        assert evict.stop_count == 1
        assert keep.stop_count == 0
    finally:
        care_engine._instances.clear()
        care_engine._instances.update(saved)


def test_care_engine_no_loop_surface_left():
    import care_engine

    assert not hasattr(care_engine.CareEngine, "_loop")
    assert not hasattr(care_engine.CareEngine, "_deliver")
