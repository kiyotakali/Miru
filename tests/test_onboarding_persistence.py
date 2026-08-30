import shutil
import tempfile
from unittest.mock import patch


def test_onboarding_skip_is_persisted_server_side():
    import app as app_mod
    import core
    import storage

    tmp = tempfile.mkdtemp(prefix="miru_onboarding_skip_")
    try:
        with patch.object(storage, "get_data_dir", return_value=tmp), \
             patch.object(core, "_inject_custom_first_greeting_bg", lambda *_args, **_kwargs: None):
            assert not storage.is_onboarding_completed()
            result = core.initialize_from_questionnaire({})

            assert "onboarding: completed" in result["actions"]
            assert storage.is_onboarding_completed()
            assert storage.get_onboarding_meta()["skipped"] is True

            with patch("core_memory.get_block", return_value=""):
                assert app_mod._needs_onboarding() is False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_onboarding_still_needed_for_empty_new_account():
    import app as app_mod
    import storage

    tmp = tempfile.mkdtemp(prefix="miru_onboarding_new_")
    try:
        with patch.object(storage, "get_data_dir", return_value=tmp), \
             patch("core_memory.get_block", return_value=""):
            assert app_mod._needs_onboarding() is True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_onboarding_still_needed_for_default_human_placeholder():
    import app as app_mod
    import storage

    tmp = tempfile.mkdtemp(prefix="miru_onboarding_placeholder_")
    try:
        with patch.object(storage, "get_data_dir", return_value=tmp), \
             patch("core_memory.get_block", return_value="（还不了解用户，等待通过对话了解）\n"):
            assert app_mod._needs_onboarding() is True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_onboarding_legacy_human_facts_suppress_first_meet_flow():
    import app as app_mod
    import storage

    tmp = tempfile.mkdtemp(prefix="miru_onboarding_legacy_human_")
    try:
        with patch.object(storage, "get_data_dir", return_value=tmp), \
             patch("core_memory.get_block", return_value="用户名字: 李垦\n最近在忙: Miru\n"):
            assert app_mod._needs_onboarding() is False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_onboarding_meta_write_failure_is_visible_but_nonfatal():
    import core
    import storage

    tmp = tempfile.mkdtemp(prefix="miru_onboarding_meta_fail_")
    try:
        with patch.object(storage, "get_data_dir", return_value=tmp), \
             patch.object(storage, "mark_onboarding_completed", side_effect=OSError("disk full")), \
             patch.object(core, "_inject_custom_first_greeting_bg", lambda *_args, **_kwargs: None):
            result = core.initialize_from_questionnaire({})

            assert "onboarding: completed" not in result["actions"]
            assert "onboarding: meta write failed" in result["actions"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
