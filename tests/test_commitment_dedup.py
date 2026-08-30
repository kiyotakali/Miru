"""Tests for commitment ID uniqueness — specifically the bug where two
commitments added in the same minute (or second) collapsed into one in the
parsed list because their cids were identical.
"""
import importlib
import os

import pytest
from flask import g


@pytest.fixture
def env(tmp_path, monkeypatch):
    data_dir = str(tmp_path / "data")
    monkeypatch.setenv("DATA_DIR", data_dir)
    os.makedirs(os.path.join(data_dir, "_admin"), exist_ok=True)

    import auth
    importlib.reload(auth)
    import storage
    importlib.reload(storage)
    import memory
    importlib.reload(memory)
    import core
    importlib.reload(core)

    import app as _app
    importlib.reload(_app)
    ctx = _app.app.app_context()
    ctx.push()
    g.user_id = "_admin"
    import auth
    g.user_data_dir = os.path.join(auth._BASE_DATA_DIR, "_admin")
    g.is_admin = True

    yield {"core": core, "memory": memory}
    try:
        ctx.pop()
    except Exception:
        pass


def test_two_commitments_same_minute_both_parsed(env):
    """Regression: two add_commitments in same minute must both show up."""
    memory = env["memory"]
    core = env["core"]
    memory.ensure_dirs()
    # Two entries with IDENTICAL added timestamp (old minute-precision format)
    content = (
        "# Active Commitments\n"
        "- [ ] 看完 fast wam 并整理三/单视角训练区别  [added: 2026-04-18 15:30]\n"
        "- [ ] 复习 IDM 并总结 text 标注问题  [added: 2026-04-18 15:30]\n"
    )
    memory.write_file("commitments/active.md", content)

    items = core.parse_commitments(include_done=False)
    titles = [i["title"] for i in items]
    assert len(items) == 2, f"expected both commitments, got {len(items)}: {titles}"
    assert "看完 fast wam 并整理三/单视角训练区别" in titles
    assert "复习 IDM 并总结 text 标注问题" in titles


def test_same_second_precision_both_parsed(env):
    """Same-second timestamps (new format) must also stay distinct."""
    memory = env["memory"]
    core = env["core"]
    memory.ensure_dirs()
    content = (
        "# Active Commitments\n"
        "- [ ] 任务A  [added: 2026-04-18 15:30:45]\n"
        "- [ ] 任务B  [added: 2026-04-18 15:30:45]\n"
    )
    memory.write_file("commitments/active.md", content)
    items = core.parse_commitments(include_done=False)
    assert len(items) == 2


def test_identical_lines_still_deduped(env):
    """Exact duplicates (same title AND timestamp) should still collapse."""
    memory = env["memory"]
    core = env["core"]
    memory.ensure_dirs()
    content = (
        "# Active Commitments\n"
        "- [ ] 同一件事  [added: 2026-04-18 15:30:45]\n"
        "- [ ] 同一件事  [added: 2026-04-18 15:30:45]\n"
    )
    memory.write_file("commitments/active.md", content)
    items = core.parse_commitments(include_done=False)
    # Both title+timestamp identical → hash collides → dedup keeps 1 (correct)
    assert len(items) == 1
