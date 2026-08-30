"""#161 regression: sse.broadcast must require explicit user_id.

Background threads (CareEngine, screen_analyzer, summarizer, ...) used to rely
on a Flask `g.user_id` fallback inside `sse.broadcast()`. That fallback masked
multi-tenant bugs. After #161 the API splits into:

- `sse.broadcast(event, data, *, user_id)` — keyword-only, required, per-user
- `sse.broadcast_all(event, data)` — explicit global broadcast
"""
import os
import sys
import importlib

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def _fresh_sse():
    import sse
    importlib.reload(sse)
    return sse


def test_broadcast_requires_user_id_keyword():
    """Calling sse.broadcast() without user_id must raise TypeError."""
    sse = _fresh_sse()
    with pytest.raises(TypeError):
        sse.broadcast("test_event", {"x": 1})  # noqa — intentional missing kwarg


def test_broadcast_rejects_empty_user_id():
    """Empty / None user_id must raise ValueError to surface the bug."""
    sse = _fresh_sse()
    with pytest.raises(ValueError):
        sse.broadcast("test_event", {"x": 1}, user_id="")
    with pytest.raises(ValueError):
        sse.broadcast("test_event", {"x": 1}, user_id=None)


def test_broadcast_per_user_isolation():
    """A broadcast to user_a only fills user_a's queue, not user_b's."""
    sse = _fresh_sse()
    qa = sse.add_client("dev_a", user_id="user_a")
    qb = sse.add_client("dev_b", user_id="user_b")

    sse.broadcast("hello", {"to": "a"}, user_id="user_a")

    assert not qa.empty()
    assert qb.empty()

    msg = qa.get_nowait()
    assert "to" in msg and '"a"' in msg


def test_broadcast_all_reaches_every_user():
    """broadcast_all delivers to every connected client regardless of user."""
    sse = _fresh_sse()
    qa = sse.add_client("dev_a", user_id="user_a")
    qb = sse.add_client("dev_b", user_id="user_b")

    sse.broadcast_all("global", {"kind": "model_changed"})

    assert not qa.empty()
    assert not qb.empty()


def test_broadcast_unknown_user_silently_drops():
    """Targeting a user with no connected clients is a no-op (no error)."""
    sse = _fresh_sse()
    qa = sse.add_client("dev_a", user_id="user_a")
    sse.broadcast("nothing", {}, user_id="user_who_doesnt_exist")
    assert qa.empty()


if __name__ == "__main__":
    test_broadcast_requires_user_id_keyword()
    test_broadcast_rejects_empty_user_id()
    test_broadcast_per_user_isolation()
    test_broadcast_all_reaches_every_user()
    test_broadcast_unknown_user_silently_drops()
    print("ALL PASS")
