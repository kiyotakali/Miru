"""Legacy CareEngine retirement guards.

The old CareEngine class used to own proactive presence.  That responsibility
now belongs to AttentionEngine.  These tests intentionally keep the old module
small: cleanup code may still touch ``care_engine._instances``, but production
must not be able to instantiate the retired sender.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def test_care_engine_constructor_is_retired():
    import care_engine

    with pytest.raises(RuntimeError, match="CareEngine 已退场"):
        care_engine.CareEngine(user_id="u_old")


def test_get_care_engine_is_retired():
    import care_engine

    with pytest.raises(RuntimeError, match="CareEngine 已退场"):
        care_engine.get_care_engine()


def test_legacy_registry_still_exists_for_cleanup():
    import care_engine

    saved = dict(care_engine._instances)
    try:
        class Fake:
            def __init__(self):
                self.stopped = False

            def stop(self):
                self.stopped = True

        inst = Fake()
        care_engine._instances.clear()
        care_engine._instances["u_cleanup"] = inst
        assert care_engine.get_all_instances()["u_cleanup"] is inst
        care_engine.stop_for_user("u_cleanup")
        assert inst.stopped is True
        assert "u_cleanup" not in care_engine._instances
    finally:
        care_engine._instances.clear()
        care_engine._instances.update(saved)


def test_old_spawner_name_delegates_to_attention_engine_source():
    root = os.path.dirname(os.path.dirname(__file__))
    with open(os.path.join(root, "app.py"), "r", encoding="utf-8") as f:
        source = f.read()

    idx = source.index("def _start_care_engine_spawner")
    block = source[idx:idx + 500]
    assert "_start_attention_engine_spawner" in block
    assert "get_care_engine" not in block


def test_daily_patterns_lives_outside_care_engine():
    import care_engine
    import daily_patterns

    assert care_engine.update_daily_patterns is daily_patterns.update_daily_patterns
