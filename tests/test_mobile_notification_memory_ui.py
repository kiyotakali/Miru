from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_mobile_memory_hides_archived_slot_group():
    html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    assert "showArchived = archived.length && !(typeof _isMobile !== 'undefined' && _isMobile)" in html
    assert "if (showArchived)" in html


def test_slot_detail_actions_expose_delete_not_archive():
    html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    start = html.index("html += '<div class=\"slot-detail-actions\">';")
    end = html.index("// Metadata block", start)
    actions = html[start:end]
    assert "_slotDelete(" in actions
    assert "_slotArchive(" not in actions


def test_cached_slot_detail_sets_memory_back_target():
    html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    start = html.index("if (cachedSlot) {")
    end = html.index("} else {", start)
    cached_branch = html[start:end]
    assert "showDetailView(_renderSlotDetailHtml(domain, cachedSlot, previewMd, []), 'memoryBrowser')" in cached_branch
    assert "detailContent.innerHTML = _renderSlotDetailHtml(domain, cachedSlot" not in cached_branch


def test_android_message_notifications_use_launcher_large_icon():
    service = (
        ROOT
        / "miru-mobile/android/app/src/main/java/com/miru/companion/MiruConnectionService.java"
    ).read_text(encoding="utf-8")
    receiver = (
        ROOT
        / "miru-mobile/android/app/src/main/java/com/miru/companion/NotificationReplyReceiver.java"
    ).read_text(encoding="utf-8")

    assert ".setLargeIcon(getLauncherLargeIcon())" in service
    assert "R.mipmap.miru_launcher_foreground" in service
    assert ".setLargeIcon(getLauncherLargeIcon(context))" in receiver
    assert "R.mipmap.miru_launcher_foreground" in receiver


def test_android_message_notifications_use_miru_small_icon():
    service = (
        ROOT
        / "miru-mobile/android/app/src/main/java/com/miru/companion/MiruConnectionService.java"
    ).read_text(encoding="utf-8")
    receiver = (
        ROOT
        / "miru-mobile/android/app/src/main/java/com/miru/companion/NotificationReplyReceiver.java"
    ).read_text(encoding="utf-8")
    capture_service = (
        ROOT
        / "miru-mobile/android/app/src/main/java/com/miru/companion/ScreenCaptureService.java"
    ).read_text(encoding="utf-8")

    assert "R.mipmap.miru_launcher" in service
    assert "R.mipmap.miru_launcher" in receiver
    assert "R.mipmap.miru_launcher" in capture_service
    assert "R.drawable.miru_status_icon_v2" not in service
    assert "R.drawable.miru_status_icon_v2" not in receiver
    assert "R.drawable.miru_status_icon_v2" not in capture_service
    assert "R.drawable.ic_miru_notification" not in service
    assert "R.drawable.ic_miru_notification" not in receiver
    assert "R.drawable.ic_notification" not in capture_service


def test_android_notifications_set_brand_color():
    service = (
        ROOT
        / "miru-mobile/android/app/src/main/java/com/miru/companion/MiruConnectionService.java"
    ).read_text(encoding="utf-8")
    receiver = (
        ROOT
        / "miru-mobile/android/app/src/main/java/com/miru/companion/NotificationReplyReceiver.java"
    ).read_text(encoding="utf-8")
    capture_service = (
        ROOT
        / "miru-mobile/android/app/src/main/java/com/miru/companion/ScreenCaptureService.java"
    ).read_text(encoding="utf-8")

    assert "0xFF6B5448" in service
    assert ".setColor(NOTIFICATION_COLOR)" in service
    assert ".setColor(NOTIFICATION_COLOR)" in receiver
    assert ".setColor(NOTIFICATION_COLOR)" in capture_service


def test_android_capture_header_active_uses_running_service_only():
    html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    start = html.index("function _androidCaptureIsOn()")
    end = html.index("function _androidCaptureNeedsAuthorization()", start)
    active_fn = html[start:end]

    assert "isScreenCaptureActive" in active_fn
    assert "isScreenCapturePreferenceEnabled" not in active_fn


def test_android_local_ai_settings_use_phone_storage_copy():
    html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    start = html.index("var aiStorageScope = _clientModeMode === 'local'")
    end = html.index(": '当前 Miru 私有服务器中';", start)
    storage_copy = html[start:end]

    assert "window.MiruAndroid" in storage_copy
    assert "'这部手机中'" in storage_copy
    assert "'这台 Windows 电脑中'" in storage_copy
    assert "'这台 Mac 中'" in storage_copy


def test_android_capture_permission_denial_clears_preference():
    main = (
        ROOT
        / "miru-mobile/android/app/src/main/java/com/miru/companion/MainActivity.java"
    ).read_text(encoding="utf-8")
    start = main.index('Log.i(TAG_CAPTURE, "MediaProjection permission denied by user")')
    end = main.index("// Notify frontend", start)
    denied_branch = main[start:end]

    assert "MiruProfileStore.setCaptureEnabled(this, false)" in denied_branch

    profile_store = (
        ROOT
        / "miru-mobile/android/app/src/main/java/com/miru/companion/MiruProfileStore.java"
    ).read_text(encoding="utf-8")
    assert 'putBoolean(key + ".screen_capture_enabled", enabled)' in profile_store


def test_android_default_launcher_vectors_removed():
    assert not (
        ROOT
        / "miru-mobile/android/app/src/main/res/drawable-v24/ic_launcher_foreground.xml"
    ).exists()
    assert not (
        ROOT
        / "miru-mobile/android/app/src/main/res/drawable/ic_launcher_background.xml"
    ).exists()


def test_android_stale_notification_vectors_removed():
    assert not (
        ROOT / "miru-mobile/android/app/src/main/res/drawable/ic_notification.xml"
    ).exists()
    assert not (
        ROOT / "miru-mobile/android/app/src/main/res/drawable/ic_miru_notification.xml"
    ).exists()


def test_android_manifest_uses_uncached_miru_launcher_icon():
    manifest = (
        ROOT / "miru-mobile/android/app/src/main/AndroidManifest.xml"
    ).read_text(encoding="utf-8")

    assert 'android:icon="@mipmap/miru_launcher"' in manifest
    assert 'android:roundIcon="@mipmap/miru_launcher_round"' in manifest
