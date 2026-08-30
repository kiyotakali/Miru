import importlib


def _reload_self_profile(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import self_profile

    importlib.reload(self_profile)
    return self_profile


def test_self_profile_persist_and_normalize(monkeypatch, tmp_path):
    sp = _reload_self_profile(monkeypatch, tmp_path)

    result = sp.update_profile(
        {
            "canonical_name": "陈浩",
            "aliases": "chenhz, @chenhz\n小陈",
            "notes": "测试",
        }
    )
    assert result["ok"] is True

    profile = sp.get_profile()
    assert profile["canonical_name"] == "陈浩"
    assert "chenhz" in profile["aliases"]
    assert "@chenhz" in profile["aliases"]
    assert "小陈" in profile["aliases"]

    aliases = sp.alias_set(profile)
    assert "陈浩" in aliases
    assert "@chenhz" in aliases
    assert "chenhz" in aliases


def test_self_profile_legacy_migration(monkeypatch, tmp_path):
    """Old format with display_name + platform_handles gets migrated."""
    sp = _reload_self_profile(monkeypatch, tmp_path)

    # Simulate old-format save
    import json, os
    old_data = {
        "display_name": "晨曦",
        "canonical_name": "",
        "aliases": ["小陈"],
        "platform_handles": [
            {"platform": "微信", "handle": "chenhz"},
            {"platform": "小红书", "handle": "@chenhz"},
        ],
        "auto_resolve_aliases": True,
        "ask_when_uncertain": True,
    }
    with open(os.path.join(str(tmp_path), "self_profile.json"), "w") as f:
        json.dump(old_data, f)

    profile = sp.get_profile()
    # display_name migrated to canonical_name
    assert profile["canonical_name"] == "晨曦"
    # platform handles merged into aliases
    assert "chenhz" in profile["aliases"]
    assert "@chenhz" in profile["aliases"]
    assert "小陈" in profile["aliases"]
    # deprecated fields removed
    assert "display_name" not in profile
    assert "platform_handles" not in profile
    assert "auto_resolve_aliases" not in profile


def test_self_profile_add_alias_dedup(monkeypatch, tmp_path):
    sp = _reload_self_profile(monkeypatch, tmp_path)
    sp.update_profile({"aliases": ["chenhz"]})

    ok1 = sp.add_alias("ChenHz")
    ok2 = sp.add_alias("chenhz")
    assert ok1["ok"] is True
    assert ok2["ok"] is True

    profile = sp.get_profile()
    lowered = [a.lower() for a in profile["aliases"]]
    assert lowered.count("chenhz") == 1
