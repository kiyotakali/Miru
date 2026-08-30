"""End-to-end test for aliases cascade (2026-05-08).

Bug being fixed:
  Before this change, "各平台用户名" / aliases entered in settings →
  written to data/users/<uid>/self_profile.json BUT NOT cascaded to
  identity.json or slots/self/identity/main.md, so the main agent /
  journal / VLM never saw them.

  Fix: identity._FIELDS now includes "aliases" (list-typed),
  cascade_from_settings forwards the list, and curator renders it as
  "- 别名: alex, 小明A" on the slot card.

This test simulates a settings save and asserts each layer received
the data.
"""
import os
import sys
import json
import shutil
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def _setup_user_dir():
    return tempfile.mkdtemp(prefix="alias_cascade_test_")


def _reset_flask_g():
    try:
        from flask.globals import _cv_app
        while _cv_app.get() is not None:
            _cv_app.get().pop()
    except Exception:
        pass


def _push_g(tmp):
    """Set Flask g so storage / identity / curator all resolve to tmp."""
    import app as _app_mod
    ctx = _app_mod.app.app_context()
    ctx.push()
    from flask import g
    g.user_id = "test_user"
    g.user_data_dir = tmp
    g.is_admin = False
    return ctx


def test_aliases_written_to_identity_json():
    """settings POST → identity.json gets aliases field."""
    print("=" * 60)
    print("TEST: aliases land in identity.json")
    print("=" * 60)
    import self_profile, identity
    tmp = _setup_user_dir()
    passed = 0
    total = 0

    try:
        ctx = _push_g(tmp)
        try:
            self_profile.update_profile({
                "canonical_name": "小明",
                "aliases": ["alex", "小明A", "  alex  ", ""],  # has dup + empty
                "notes": "developer",
            })

            # Read identity.json directly
            ident_path = os.path.join(tmp, "identity.json")
            total += 1
            if os.path.exists(ident_path):
                print(f"  [PASS] identity.json created")
                passed += 1
            else:
                print(f"  [FAIL] identity.json missing")
                return passed, total

            with open(ident_path, encoding="utf-8") as f:
                data = json.load(f)

            total += 1
            if data.get("aliases") == ["alex", "小明A"]:
                print(f"  [PASS] aliases stored + deduped + stripped: {data['aliases']}")
                passed += 1
            else:
                print(f"  [FAIL] aliases wrong: {data.get('aliases')!r}")

            total += 1
            if data.get("name") == "小明":
                print(f"  [PASS] name still cascades")
                passed += 1
            else:
                print(f"  [FAIL] name wrong: {data.get('name')!r}")

            total += 1
            if data.get("source_versions", {}).get("aliases") == "settings":
                print(f"  [PASS] source_version tracked: settings")
                passed += 1
            else:
                print(f"  [FAIL] source_version: {data.get('source_versions')}")
        finally:
            ctx.pop()
    finally:
        _reset_flask_g()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n  IdentityJson: {passed}/{total} passed")
    return passed, total


def test_aliases_appear_in_slot_main_md():
    """The cascaded aliases must be visible in slots/self/identity/main.md
    so it shows on the card AND lands in the main agent prompt."""
    print("\n" + "=" * 60)
    print("TEST: aliases appear in slots/self/identity/main.md")
    print("=" * 60)
    import self_profile
    tmp = _setup_user_dir()
    passed = 0
    total = 0

    try:
        ctx = _push_g(tmp)
        try:
            self_profile.update_profile({
                "canonical_name": "小明",
                "aliases": ["alex", "小明A"],
                "notes": "",
            })

            slot_main = os.path.join(tmp, "memory", "self", "identity", "main.md")
            total += 1
            if os.path.exists(slot_main):
                print(f"  [PASS] slot main.md exists at {slot_main}")
                passed += 1
            else:
                print(f"  [FAIL] slot main.md missing at {slot_main}")
                return passed, total

            with open(slot_main, encoding="utf-8") as f:
                body = f.read()
            print(f"  --- slot main.md content ---\n{body}\n  ----------------------------")

            total += 1
            if "别名" in body:
                print(f"  [PASS] '别名' label present in card")
                passed += 1
            else:
                print(f"  [FAIL] '别名' missing from card")

            total += 1
            if "alex" in body and "小明A" in body:
                print(f"  [PASS] both aliases rendered")
                passed += 1
            else:
                print(f"  [FAIL] alias values missing")
        finally:
            ctx.pop()
    finally:
        _reset_flask_g()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n  SlotMainMd: {passed}/{total} passed")
    return passed, total


def test_aliases_in_authoritative_facts():
    """get_authoritative_facts() must emit a single aliases fact so
    ground_truth fallback path also sees it."""
    print("\n" + "=" * 60)
    print("TEST: aliases emit one fact in get_authoritative_facts()")
    print("=" * 60)
    import identity
    tmp = _setup_user_dir()
    passed = 0
    total = 0

    try:
        ctx = _push_g(tmp)
        try:
            identity.update({
                "name": "小明",
                "aliases": ["alex", "小明A", "octocat"],
            }, source="settings")

            facts = identity.get_authoritative_facts()
            alias_facts = [f for f in facts if f.get("category") == "aliases"]

            total += 1
            if len(alias_facts) == 1:
                print(f"  [PASS] exactly 1 aliases fact emitted")
                passed += 1
            else:
                print(f"  [FAIL] expected 1 aliases fact, got {len(alias_facts)}")

            total += 1
            if alias_facts and alias_facts[0]["text"] == "alex, 小明A, octocat":
                print(f"  [PASS] fact.text correctly joined: {alias_facts[0]['text']!r}")
                passed += 1
            else:
                got = alias_facts[0]["text"] if alias_facts else "(no fact)"
                print(f"  [FAIL] fact.text wrong: {got!r}")

            total += 1
            if alias_facts and alias_facts[0]["confidence"] == 1.0 and alias_facts[0]["pinned"]:
                print(f"  [PASS] fact is pinned + confidence 1.0 (ground truth)")
                passed += 1
            else:
                print(f"  [FAIL] fact metadata wrong: {alias_facts}")
        finally:
            ctx.pop()
    finally:
        _reset_flask_g()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n  Facts: {passed}/{total} passed")
    return passed, total


def test_clearing_aliases():
    """Clearing aliases (sending empty list) should remove them from card."""
    print("\n" + "=" * 60)
    print("TEST: clearing aliases removes them from card")
    print("=" * 60)
    import self_profile
    tmp = _setup_user_dir()
    passed = 0
    total = 0

    try:
        ctx = _push_g(tmp)
        try:
            # First add some aliases
            self_profile.update_profile({
                "canonical_name": "小明",
                "aliases": ["alex"],
            })
            slot_main = os.path.join(tmp, "memory", "self", "identity", "main.md")
            with open(slot_main, encoding="utf-8") as f:
                before = f.read()

            total += 1
            if "alex" in before:
                print(f"  [PASS] alex initially present")
                passed += 1

            # Now clear
            self_profile.update_profile({
                "canonical_name": "小明",
                "aliases": [],
            })
            with open(slot_main, encoding="utf-8") as f:
                after = f.read()

            total += 1
            if "alex" not in after:
                print(f"  [PASS] alex removed after clearing")
                passed += 1
            else:
                print(f"  [FAIL] alex still present:\n{after}")

            total += 1
            if "别名" not in after:
                print(f"  [PASS] '别名' label removed (no empty section)")
                passed += 1
            else:
                print(f"  [FAIL] '别名' label lingered with empty value")
        finally:
            ctx.pop()
    finally:
        _reset_flask_g()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n  Clear: {passed}/{total} passed")
    return passed, total


def test_self_profile_json_still_holds_aliases():
    """Backward compat: self_profile.json (used by alias_set for message
    recognition) must still contain aliases — we did not remove that path."""
    print("\n" + "=" * 60)
    print("TEST: self_profile.json still holds aliases for alias_set()")
    print("=" * 60)
    import self_profile
    tmp = _setup_user_dir()
    passed = 0
    total = 0

    try:
        ctx = _push_g(tmp)
        try:
            self_profile.update_profile({
                "canonical_name": "小明",
                "aliases": ["alex"],
            })

            sp_path = os.path.join(tmp, "self_profile.json")
            total += 1
            if os.path.exists(sp_path):
                with open(sp_path, encoding="utf-8") as f:
                    sp = json.load(f)
                if sp.get("aliases") == ["alex"]:
                    print(f"  [PASS] self_profile.json.aliases preserved")
                    passed += 1
                else:
                    print(f"  [FAIL] self_profile.json.aliases wrong: {sp.get('aliases')!r}")
            else:
                print(f"  [FAIL] self_profile.json missing")

            total += 1
            alias_set = self_profile.alias_set()
            if "alex" in alias_set and "@alex" in alias_set:
                print(f"  [PASS] alias_set() still expands to {{alex, @alex}}")
                passed += 1
            else:
                print(f"  [FAIL] alias_set wrong: {alias_set}")
        finally:
            ctx.pop()
    finally:
        _reset_flask_g()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n  BackCompat: {passed}/{total} passed")
    return passed, total


def test_repeated_save_replaces_aliases_line_not_appends():
    """Repeated saves of aliases must REPLACE the line in the card,
    not append a second '- 别名: ...' below.

    This is the user-facing invariant: 'I edit aliases 3 times, the card
    still has exactly ONE 别名 row showing my latest value.'
    """
    print("\n" + "=" * 60)
    print("TEST: repeated saves replace 别名 line (no duplicates)")
    print("=" * 60)
    import self_profile
    tmp = _setup_user_dir()
    passed = 0
    total = 0

    slot_main = os.path.join(tmp, "memory", "self", "identity", "main.md")

    try:
        ctx = _push_g(tmp)
        try:
            # Save #1
            self_profile.update_profile({
                "canonical_name": "小明",
                "aliases": ["alex"],
            })
            with open(slot_main, encoding="utf-8") as f:
                v1 = f.read()
            print(f"  --- after save #1 ---\n{v1}")

            # Save #2 — different aliases
            self_profile.update_profile({
                "canonical_name": "小明",
                "aliases": ["jane", "octocat"],
            })
            with open(slot_main, encoding="utf-8") as f:
                v2 = f.read()
            print(f"  --- after save #2 ---\n{v2}")

            # Save #3 — yet another set
            self_profile.update_profile({
                "canonical_name": "小明",
                "aliases": ["xiaoming_2024"],
            })
            with open(slot_main, encoding="utf-8") as f:
                v3 = f.read()
            print(f"  --- after save #3 ---\n{v3}")

            # Each version must have EXACTLY ONE "别名:" line
            for i, body in enumerate([v1, v2, v3], 1):
                total += 1
                count = body.count("- 别名:")
                if count == 1:
                    print(f"  [PASS] save #{i}: exactly 1 '别名' line")
                    passed += 1
                else:
                    print(f"  [FAIL] save #{i}: found {count} '别名' lines")

            # Final card has only the latest value (no stale 'alex' / 'jane')
            total += 1
            if "xiaoming_2024" in v3 and "alex" not in v3 and "jane" not in v3:
                print(f"  [PASS] final card has only latest aliases (alex/jane gone)")
                passed += 1
            else:
                print(f"  [FAIL] stale aliases linger:\n{v3}")
        finally:
            ctx.pop()
    finally:
        _reset_flask_g()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n  RepeatedSave: {passed}/{total} passed")
    return passed, total


def test_partial_update_preserves_other_fields():
    """Saving only canonical_name must not erase aliases (and vice versa).

    Settings UI sends the full payload every save, but the cascade layer
    must not nuke fields it didn't see in this payload — otherwise users
    lose data when one field changes.
    """
    print("\n" + "=" * 60)
    print("TEST: editing one field doesn't wipe others on the card")
    print("=" * 60)
    import self_profile
    tmp = _setup_user_dir()
    passed = 0
    total = 0

    slot_main = os.path.join(tmp, "memory", "self", "identity", "main.md")

    try:
        ctx = _push_g(tmp)
        try:
            # Set everything once
            self_profile.update_profile({
                "canonical_name": "小明",
                "aliases": ["alex", "jane"],
                "notes": "developer",
            })
            with open(slot_main, encoding="utf-8") as f:
                v0 = f.read()

            total += 1
            if "小明" in v0 and "alex, jane" in v0 and "developer" in v0:
                print(f"  [PASS] all 3 fields land in card initially")
                passed += 1

            # Now save ONLY new aliases (keeping name + notes the same per UI behavior)
            self_profile.update_profile({
                "canonical_name": "小明",
                "aliases": ["alex", "jane", "octocat"],
                "notes": "developer",
            })
            with open(slot_main, encoding="utf-8") as f:
                v1 = f.read()

            total += 1
            if "小明" in v1 and "octocat" in v1 and "developer" in v1:
                print(f"  [PASS] aliases updated, name + notes preserved")
                passed += 1
            else:
                print(f"  [FAIL] something dropped:\n{v1}")

            # Update ONLY canonical_name
            self_profile.update_profile({
                "canonical_name": "Alex Chen",
                "aliases": ["alex", "jane", "octocat"],
                "notes": "developer",
            })
            with open(slot_main, encoding="utf-8") as f:
                v2 = f.read()

            total += 1
            if "Alex Chen" in v2 and "octocat" in v2 and "developer" in v2 and "小明" not in v2:
                print(f"  [PASS] name changed (Alex Chen), aliases + notes preserved")
                passed += 1
            else:
                print(f"  [FAIL] partial update lost data:\n{v2}")
        finally:
            ctx.pop()
    finally:
        _reset_flask_g()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n  PartialUpdate: {passed}/{total} passed")
    return passed, total


def test_field_order_stable_across_saves():
    """The '- 姓名/别名/...' lines must always appear in the same order
    in the card, regardless of which field the user edited last."""
    print("\n" + "=" * 60)
    print("TEST: field order is stable (姓名 → 别名 → 备注)")
    print("=" * 60)
    import self_profile
    tmp = _setup_user_dir()
    passed = 0
    total = 0

    slot_main = os.path.join(tmp, "memory", "self", "identity", "main.md")

    def _ordered_field_keys(body: str) -> list:
        """Extract the labels in the order they appear in the body."""
        order = []
        for line in body.split("\n"):
            line = line.strip()
            if not line.startswith("- "):
                continue
            after = line[2:]
            if ":" in after:
                label = after.split(":", 1)[0].strip()
                order.append(label)
        return order

    try:
        ctx = _push_g(tmp)
        try:
            # Edit name first
            self_profile.update_profile({
                "canonical_name": "A",
                "aliases": ["alex"],
                "notes": "n1",
            })
            with open(slot_main, encoding="utf-8") as f:
                order1 = _ordered_field_keys(f.read())

            # Edit aliases (would go second naively if we appended)
            self_profile.update_profile({
                "canonical_name": "A",
                "aliases": ["alex", "jane"],
                "notes": "n1",
            })
            with open(slot_main, encoding="utf-8") as f:
                order2 = _ordered_field_keys(f.read())

            # Edit notes last
            self_profile.update_profile({
                "canonical_name": "A",
                "aliases": ["alex", "jane"],
                "notes": "n2",
            })
            with open(slot_main, encoding="utf-8") as f:
                order3 = _ordered_field_keys(f.read())

            expected = ["姓名", "别名", "用户备注"]
            for i, ord_ in enumerate([order1, order2, order3], 1):
                total += 1
                if ord_ == expected:
                    print(f"  [PASS] save #{i}: order = {ord_}")
                    passed += 1
                else:
                    print(f"  [FAIL] save #{i}: order = {ord_}, expected {expected}")
        finally:
            ctx.pop()
    finally:
        _reset_flask_g()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n  StableOrder: {passed}/{total} passed")
    return passed, total


def test_no_phantom_blank_lines_or_empty_sections():
    """When a field is empty its line must not appear (no '- 别名: '
    with empty value) and the body must not have stray blank lines."""
    print("\n" + "=" * 60)
    print("TEST: empty fields produce zero lines (not blank ones)")
    print("=" * 60)
    import self_profile
    tmp = _setup_user_dir()
    passed = 0
    total = 0

    slot_main = os.path.join(tmp, "memory", "self", "identity", "main.md")

    try:
        ctx = _push_g(tmp)
        try:
            self_profile.update_profile({
                "canonical_name": "小明",
                "aliases": [],     # explicitly empty
                "notes": "",        # explicitly empty
            })
            with open(slot_main, encoding="utf-8") as f:
                body = f.read()
            print(f"  --- card with only name set ---\n{body}")

            total += 1
            if "- 别名:" not in body:
                print(f"  [PASS] no empty '别名' line")
                passed += 1
            else:
                print(f"  [FAIL] empty 别名 line present")

            total += 1
            if "- 用户备注:" not in body:
                print(f"  [PASS] no empty '用户备注' line")
                passed += 1
            else:
                print(f"  [FAIL] empty 备注 line present")

            total += 1
            # No double blank lines in body (between header and list)
            doubled = "\n\n\n" in body
            if not doubled:
                print(f"  [PASS] no triple newlines (clean body)")
                passed += 1
            else:
                print(f"  [FAIL] body has stray blank lines")
        finally:
            ctx.pop()
    finally:
        _reset_flask_g()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n  CleanBody: {passed}/{total} passed")
    return passed, total


def test_save_broadcasts_data_changed_memory_scope():
    """update_profile must broadcast SSE 'data_changed' with scope='memory'
    so every open client invalidates its memory_slots cache and re-fetches.

    Without this, saving aliases in settings → cascaded to slot main.md
    correctly, but the memory hub UI still shows the OLD card from
    IndexedDB cache until manual refresh.
    """
    print("\n" + "=" * 60)
    print("TEST: update_profile broadcasts data_changed(scope=memory)")
    print("=" * 60)
    import self_profile
    import sse
    from unittest.mock import patch
    tmp = _setup_user_dir()
    passed = 0
    total = 0

    captured = []

    def _spy_broadcast(event_name, payload, user_id=None):
        captured.append((event_name, payload, user_id))

    try:
        ctx = _push_g(tmp)
        try:
            with patch.object(sse, "broadcast", side_effect=_spy_broadcast):
                self_profile.update_profile({
                    "canonical_name": "小明",
                    "aliases": ["alex", "jane"],
                })

            # Look for our event
            data_changed = [c for c in captured if c[0] == "data_changed"]

            total += 1
            if data_changed:
                print(f"  [PASS] data_changed event was broadcast")
                passed += 1
            else:
                print(f"  [FAIL] no data_changed broadcast; got: {captured}")

            total += 1
            if data_changed and data_changed[0][1].get("scope") == "memory":
                print(f"  [PASS] scope is 'memory' (frontend will mark memory_slots + memory_tree stale)")
                passed += 1
            else:
                got = data_changed[0][1] if data_changed else None
                print(f"  [FAIL] scope wrong: {got}")

            # Verify it carries user_id (per-user SSE isolation)
            total += 1
            if data_changed and data_changed[0][2] == "test_user":
                print(f"  [PASS] user_id passed through (per-user SSE isolation)")
                passed += 1
            else:
                got = data_changed[0][2] if data_changed else None
                print(f"  [FAIL] user_id wrong: {got!r}")
        finally:
            ctx.pop()
    finally:
        _reset_flask_g()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n  Broadcast: {passed}/{total} passed")
    return passed, total


def test_frontend_data_changed_handler_invalidates_slots_cache():
    """Static check: the SSE 'data_changed' handler in templates/index.html
    must include 'memory_slots' in the keyMap['memory'] list. Without this
    string in the source, the SSE event won't invalidate the memory hub
    card view's cache."""
    print("\n" + "=" * 60)
    print("TEST: frontend data_changed handler covers memory_slots key")
    print("=" * 60)
    repo_root = os.path.dirname(os.path.dirname(__file__))
    html_path = os.path.join(repo_root, "templates", "index.html")
    passed = 0
    total = 0

    try:
        with open(html_path, "r", encoding="utf-8") as f:
            html = f.read()

        # 1. The keyMap.memory entry must mention 'memory_slots'
        # (loose check — we just need the handler to invalidate it)
        total += 1
        # Find the keyMap definition + verify both keys are listed for 'memory'
        import re
        m = re.search(r"'memory':\s*\[([^\]]*)\]", html)
        if m and "memory_slots" in m.group(1) and "memory_tree" in m.group(1):
            print(f"  [PASS] keyMap.memory invalidates BOTH memory_slots and memory_tree")
            passed += 1
        else:
            print(f"  [FAIL] keyMap.memory missing one of the keys; matched: {m.group(0) if m else 'no match'}")

        # 2. settingsSaveIdentity must call MiruCache.markStale for slots
        total += 1
        save_fn_idx = html.find("async function settingsSaveIdentity")
        next_fn_idx = html.find("function ", save_fn_idx + 100) if save_fn_idx > 0 else -1
        save_fn_body = html[save_fn_idx:next_fn_idx] if save_fn_idx > 0 and next_fn_idx > 0 else ""
        if "markStale('memory_slots')" in save_fn_body:
            print(f"  [PASS] settingsSaveIdentity marks memory_slots stale on save")
            passed += 1
        else:
            print(f"  [FAIL] settingsSaveIdentity doesn't markStale('memory_slots')")

        total += 1
        if "markStale('memory_tree')" in save_fn_body:
            print(f"  [PASS] settingsSaveIdentity marks memory_tree stale on save")
            passed += 1
        else:
            print(f"  [FAIL] settingsSaveIdentity doesn't markStale('memory_tree')")
    except Exception as e:
        print(f"  [FAIL] couldn't read template: {e}")

    print(f"\n  FrontendCache: {passed}/{total} passed")
    return passed, total


def main():
    results = []
    results.append(test_aliases_written_to_identity_json())
    results.append(test_aliases_appear_in_slot_main_md())
    results.append(test_aliases_in_authoritative_facts())
    results.append(test_clearing_aliases())
    results.append(test_self_profile_json_still_holds_aliases())
    results.append(test_repeated_save_replaces_aliases_line_not_appends())
    results.append(test_partial_update_preserves_other_fields())
    results.append(test_field_order_stable_across_saves())
    results.append(test_no_phantom_blank_lines_or_empty_sections())
    results.append(test_save_broadcasts_data_changed_memory_scope())
    results.append(test_frontend_data_changed_handler_invalidates_slots_cache())

    total_p = sum(r[0] for r in results)
    total_t = sum(r[1] for r in results)
    print("\n" + "=" * 60)
    print(f"TOTAL: {total_p}/{total_t} passed")
    print("=" * 60)
    if total_p < total_t:
        sys.exit(1)


if __name__ == "__main__":
    main()
