package com.miru.companion;

import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.app.WallpaperInfo;
import android.app.WallpaperManager;
import android.content.ComponentName;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.media.projection.MediaProjectionManager;
import android.os.Build;
import android.os.Bundle;
import android.util.Log;
import android.webkit.JavascriptInterface;
import android.webkit.WebView;

import androidx.core.app.ActivityCompat;
import androidx.core.content.ContextCompat;
import androidx.core.content.pm.PackageInfoCompat;
import androidx.work.WorkManager;

import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {

    private static final String TAG = "MiruMain";
    private static final String TAG_CAPTURE = "MiruScreenCapture";
    private static final int REQUEST_MEDIA_PROJECTION = 9999;
    private static final int REQUEST_NOTIFICATION_PERMISSION = 9998;

    // Screen capture pending params (set before startActivityForResult)
    private String pendingServerUrl;
    private String pendingAuthToken;
    private String pendingDeviceId;

    // Prevent double-prompting within same resume cycle
    private boolean autoResumeAttempted = false;
    // Prevent multiple concurrent MediaProjection permission requests
    private boolean pendingProjectionRequest = false;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        // Always pass null to prevent WebView from restoring an old VPS URL.
        // The local entry page (www/index.html) checks saved credentials in
        // its own localStorage and re-navigates to the VPS with ?token=...
        super.onCreate(null);

        // Register JS interface so the web page can pull pending shared images
        // and control screen capture
        WebView webView = getBridge().getWebView();
        if (webView != null) {
            webView.addJavascriptInterface(new MiruJSInterface(), "MiruAndroid");
        }

        // Check if launched from "paused" notification
        checkAutoResumeIntent(getIntent());

        // Kill any previously-scheduled NotificationPollWorker from older APK installs.
        // MiruConnectionService is now the sole message channel on Android.
        WorkManager.getInstance(this).cancelUniqueWork("miru_notif_poll");

        // Every Android session starts through the private loopback bridge.
        // The bridge reuses the same login/local/self-server flows as desktop,
        // then sends the WebView either to local /app or the selected VPS.
        MiruPythonRuntime.start(this, new MiruPythonRuntime.StartCallback() {
            @Override
            public void onReady(String url) {
                runOnUiThread(() -> {
                    WebView current = getBridge().getWebView();
                    if (current != null && !isFinishing()) current.loadUrl(url);
                });
            }

            @Override
            public void onError(String message) {
                Log.e(TAG, "Local runtime unavailable: " + message);
                runOnUiThread(() -> {
                    WebView current = getBridge().getWebView();
                    if (current != null) {
                        String safe = org.json.JSONObject.quote(
                            message == null ? "Miru 本地服务启动失败" : message);
                        current.evaluateJavascript(
                            "if(typeof window._onEmbeddedBackendError==='function')"
                            + "window._onEmbeddedBackendError(" + safe + ");", null);
                    }
                });
            }
        });
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        checkAutoResumeIntent(intent);
    }

    @Override
    public void onResume() {
        super.onResume();
        // Auto-resume screen capture if preference is ON but service died
        if (!autoResumeAttempted) {
            autoResumeAttempted = true;
            tryAutoResumeCapture();
        }
        // Force SSE reconnect — WebView JS is frozen while backgrounded so the
        // client-side heartbeat checker cannot detect a dead connection.
        // Also sync all screen-capture UI since the service status may have
        // changed (e.g. system killed it, user granted/revoked permission).
        WebView webView = getBridge().getWebView();
        if (webView != null) {
            webView.post(() -> webView.evaluateJavascript(
                "if(typeof _forceReconnectSSE==='function') _forceReconnectSSE();"
                + "if(typeof _syncAllScreenCaptureUI==='function') _syncAllScreenCaptureUI();",
                null));
        }
        // Tell MiruConnectionService to suppress notifications (WebView is active)
        notifyConnectionService("FOREGROUND");
    }

    @Override
    public void onPause() {
        super.onPause();
        // Reset so next onResume can try again
        autoResumeAttempted = false;
        // Tell MiruConnectionService to enable notifications (WebView is frozen)
        notifyConnectionService("BACKGROUND");
    }

    /** Send a lifecycle action to MiruConnectionService if running. */
    private void notifyConnectionService(String action) {
        if (MiruConnectionService.isRunning) {
            Intent intent = new Intent(this, MiruConnectionService.class);
            intent.setAction(action);
            startService(intent);
        }
    }

    private void checkAutoResumeIntent(Intent intent) {
        if (intent != null && intent.getBooleanExtra("auto_resume_capture", false)) {
            Log.i(TAG_CAPTURE, "Launched from paused notification, auto-resuming");
            // Clear the flag to prevent re-triggering
            intent.removeExtra("auto_resume_capture");
            // Prevent onResume from also triggering
            autoResumeAttempted = true;
            tryAutoResumeCapture();
        }
    }

    private void tryAutoResumeCapture() {
        // Guard: prevent multiple concurrent permission requests
        if (pendingProjectionRequest) {
            Log.d(TAG_CAPTURE, "Auto-resume: permission request already pending, skipping");
            return;
        }

        SharedPreferences prefs = getPrefs();
        boolean prefEnabled = MiruProfileStore.isCaptureEnabled(this);
        if (!prefEnabled || ScreenCaptureService.isRunning) return;

        // Load saved capture params
        String savedServerUrl = prefs.getString("capture_server_url", null);
        String savedAuthToken = prefs.getString("capture_auth_token", "");
        String savedDeviceId = prefs.getString("capture_device_id", "");

        if (savedServerUrl == null || savedServerUrl.isEmpty()) {
            Log.w(TAG_CAPTURE, "Auto-resume: no saved server URL, skipping");
            return;
        }

        Log.i(TAG_CAPTURE, "Auto-resuming screen capture");
        pendingProjectionRequest = true;
        pendingServerUrl = savedServerUrl;
        pendingAuthToken = savedAuthToken;
        pendingDeviceId = savedDeviceId;

        // Dismiss the "paused" notification if any
        android.app.NotificationManager nm = getSystemService(android.app.NotificationManager.class);
        nm.cancel(1002); // NOTIFICATION_ID + 1

        // Small delay to let the activity finish resuming
        new android.os.Handler(getMainLooper()).postDelayed(() -> {
            if (!ScreenCaptureService.isRunning && pendingProjectionRequest) {
                MediaProjectionManager mpm = (MediaProjectionManager)
                        getSystemService(Context.MEDIA_PROJECTION_SERVICE);
                startActivityForResult(mpm.createScreenCaptureIntent(), REQUEST_MEDIA_PROJECTION);
            } else {
                pendingProjectionRequest = false;
            }
        }, 800);
    }

    // ===== Screen Capture (MediaProjection) =====

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        if (requestCode == REQUEST_MEDIA_PROJECTION) {
            // Clear pending flag — permission flow completed (granted or denied)
            pendingProjectionRequest = false;
            if (resultCode == Activity.RESULT_OK && data != null) {
                Log.i(TAG_CAPTURE, "MediaProjection permission granted");
                startCaptureService(resultCode, data);
            } else {
                Log.i(TAG_CAPTURE, "MediaProjection permission denied by user");
                MiruProfileStore.setCaptureEnabled(this, false);
                // Notify frontend
                WebView webView = getBridge().getWebView();
                if (webView != null) {
                    webView.post(() -> webView.evaluateJavascript(
                            "if(typeof window._onScreenCaptureResult==='function'){window._onScreenCaptureResult(false)}",
                            null));
                }
            }
            return;
        }
        super.onActivityResult(requestCode, resultCode, data);
    }

    private SharedPreferences getPrefs() {
        return MiruProfileStore.globals(this);
    }

    private void startCaptureService(int resultCode, Intent projectionData) {
        Intent serviceIntent = new Intent(this, ScreenCaptureService.class);
        serviceIntent.putExtra("resultCode", resultCode);
        serviceIntent.putExtra("projectionData", projectionData);
        serviceIntent.putExtra("serverUrl", pendingServerUrl);
        serviceIntent.putExtra("authToken", pendingAuthToken);
        serviceIntent.putExtra("deviceId", pendingDeviceId);

        // Save user preference so we can auto-prompt on next app start
        MiruProfileStore.setCaptureEnabled(this, true);
        MiruProfileStore.updateActiveTransport(
                this, pendingServerUrl, pendingAuthToken, pendingDeviceId);

        ContextCompat.startForegroundService(this, serviceIntent);

        // Notify frontend of success
        WebView webView = getBridge().getWebView();
        if (webView != null) {
            webView.post(() -> webView.evaluateJavascript(
                    "if(typeof window._onScreenCaptureResult==='function'){window._onScreenCaptureResult(true)}",
                    null));
        }
    }

    // ===== JavaScript Interface =====

    private class MiruJSInterface {

        // --- Screen Capture ---

        @JavascriptInterface
        public void requestScreenCapture(String serverUrl, String authToken, String deviceId) {
            Log.i(TAG_CAPTURE, "requestScreenCapture called");

            // If service is already running, no need to re-auth — UNLESS the
            // running instance has a stale token (account switch / token
            // rotation didn't go through clearCredentials). In that case we
            // stop the service first; the user re-grants MediaProjection and
            // the new token takes effect. Without this guard the stale-token
            // service keeps 401-ing every upload.
            if (ScreenCaptureService.isRunning) {
                String currentTok = ScreenCaptureService.currentAuthToken;
                boolean stale = currentTok != null && authToken != null
                        && !currentTok.equals(authToken);
                if (!stale) {
                    Log.i(TAG_CAPTURE, "Service already running with current token, skipping auth");
                    return;
                }
                Log.i(TAG_CAPTURE, "Service running with stale token, stopping before re-auth");
                try {
                    Intent stopCapture = new Intent(MainActivity.this, ScreenCaptureService.class);
                    stopCapture.setAction("STOP");
                    startService(stopCapture);
                } catch (Exception e) {
                    Log.w(TAG_CAPTURE, "stop for token refresh failed: " + e);
                }
                // Fall through to the permission-grant flow below.
            }

            pendingServerUrl = serverUrl;
            pendingAuthToken = authToken;
            pendingDeviceId = deviceId;

            runOnUiThread(() -> {
                // Request notification permission on Android 13+
                if (Build.VERSION.SDK_INT >= 33) {
                    if (ContextCompat.checkSelfPermission(MainActivity.this,
                            "android.permission.POST_NOTIFICATIONS") != PackageManager.PERMISSION_GRANTED) {
                        ActivityCompat.requestPermissions(MainActivity.this,
                                new String[]{"android.permission.POST_NOTIFICATIONS"},
                                REQUEST_NOTIFICATION_PERMISSION);
                    }
                }

                // Request MediaProjection permission
                MediaProjectionManager mpm = (MediaProjectionManager)
                        getSystemService(Context.MEDIA_PROJECTION_SERVICE);
                startActivityForResult(mpm.createScreenCaptureIntent(), REQUEST_MEDIA_PROJECTION);
            });
        }

        @JavascriptInterface
        public void stopScreenCapture() {
            Log.i(TAG_CAPTURE, "stopScreenCapture called");
            MiruProfileStore.setCaptureEnabled(MainActivity.this, false);
            runOnUiThread(() -> {
                Intent stopIntent = new Intent(MainActivity.this, ScreenCaptureService.class);
                stopIntent.setAction("STOP");
                startService(stopIntent);
            });
        }

        @JavascriptInterface
        public boolean isScreenCaptureActive() {
            return ScreenCaptureService.isRunning;
        }

        @JavascriptInterface
        public boolean isScreenCapturePreferenceEnabled() {
            return MiruProfileStore.isCaptureEnabled(MainActivity.this);
        }

        // --- Credential Persistence (SharedPreferences, survives WebView clears) ---

        @JavascriptInterface
        public void saveCredentials(String serverUrl, String token, String invitationCode) {
            String mode = (invitationCode == null || invitationCode.isEmpty()) ? "local" : "remote";
            saveCredentialsForProfile(serverUrl, token, invitationCode, mode, "");
        }

        @JavascriptInterface
        public void saveCredentialsForProfile(String serverUrl, String token,
                                              String invitationCode, String mode,
                                              String userId) {
            String oldProfile = MiruProfileStore.activeProfileKey(MainActivity.this);
            String newProfile = MiruProfileStore.profileKey(mode, serverUrl, userId);
            if (!oldProfile.isEmpty() && !oldProfile.equals(newProfile)) {
                try { MiruConnectionService.disconnectNow(); } catch (Exception ignored) {}
                stopConnectionService();
                if (ScreenCaptureService.isRunning) {
                    Intent stopCapture = new Intent(MainActivity.this, ScreenCaptureService.class);
                    stopCapture.setAction("STOP");
                    startService(stopCapture);
                }
            }
            MiruProfileStore.activateProfile(
                    MainActivity.this, mode, serverUrl, userId, token, invitationCode);
            Log.i(TAG, "Active profile saved (mode=" + mode + ", url=" + serverUrl
                    + ", user=" + userId + ", token_len="
                    + (token == null ? 0 : token.length()) + ")");
        }

        @JavascriptInterface
        public String getCredentials() {
            SharedPreferences prefs = getPrefs();
            String url = prefs.getString("miru_server_url", "");
            String token = prefs.getString("miru_auth_token", "");
            String code = prefs.getString("miru_invitation_code", "");
            // Return as JSON string (avoid org.json dependency)
            return "{\"serverUrl\":\"" + url.replace("\"", "\\\"")
                + "\",\"token\":\"" + token.replace("\"", "\\\"")
                + "\",\"code\":\"" + code.replace("\"", "\\\"") + "\"}";
        }

        @JavascriptInterface
        public void clearCredentials() {
            // Use commit() (synchronous) like saveCredentials() does — the
            // caller often follows up with relaunchToEntry()/finish() which
            // would race apply()'s async write and lose it, causing the
            // entry page to read stale creds and bounce back to VPS in a loop.
            //
            // screen_capture_enabled is also cleared so ScreenCaptureService.onDestroy
            // doesn't post the misleading "请点击恢复" notification when we stop it
            // below (that notification is meant for system-killed services on the
            // same account, not for an account switch).
            boolean ok = MiruProfileStore.clearActiveSession(MainActivity.this);
            Log.i(TAG, "Credentials cleared from SharedPreferences (commit=" + ok + ")");
            // Keep the embedded client-mode config in the same state as native
            // credentials. Local account data is intentionally preserved.
            MiruPythonRuntime.clearClientSession();

            // Synchronously kill the SSE socket BEFORE asking the service to
            // stop. The STOP intent is async and could leave the previous
            // user's stream open for 1-2s; that window matters because the
            // server may push messages routed to the now-deleted user there.
            try { MiruConnectionService.disconnectNow(); } catch (Exception e) {
                Log.w(TAG, "disconnectNow failed (non-fatal): " + e);
            }
            stopConnectionService();

            // Stop ScreenCaptureService too. It caches authToken in memory at
            // service start; without an explicit stop here the previous user's
            // token would keep uploading screenshots and 401-ing forever (the
            // user has no UI signal — they just see "已分析截图: 0").
            if (ScreenCaptureService.isRunning) {
                Log.i(TAG, "clearCredentials: stopping ScreenCaptureService to drop cached token");
                try {
                    Intent stopCapture = new Intent(MainActivity.this, ScreenCaptureService.class);
                    stopCapture.setAction("STOP");
                    startService(stopCapture);
                } catch (Exception e) {
                    Log.w(TAG, "stop ScreenCaptureService in clearCredentials failed: " + e);
                }
            }
        }

        /**
         * Restart MainActivity so the WebView re-loads Capacitor's bundled
         * www/index.html (the full-code entry page).
         *
         * Called from the VPS-side SPA when token becomes invalid (admin deleted
         * the user, invitation revoked, etc). Without this, the SPA would just
         * show its short-code overlay on the VPS domain — but admin-revoked
         * invitations make even short-code login fail, so we need to bounce the
         * user all the way back to full-code entry to enter a fresh invitation.
         *
         * Caller MUST call clearCredentials() first; otherwise www/index.html's
         * loadCredentials() will see saved server_url+token and immediately
         * redirect back to the VPS, defeating the purpose.
         */
        @JavascriptInterface
        public void relaunchToEntry() {
            Log.i(TAG, "relaunchToEntry: restarting Activity to load Capacitor entry");
            runOnUiThread(() -> {
                Intent intent = new Intent(MainActivity.this, MainActivity.class);
                intent.setFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP
                              | Intent.FLAG_ACTIVITY_NEW_TASK
                              | Intent.FLAG_ACTIVITY_CLEAR_TASK);
                startActivity(intent);
                finish();
            });
        }

        @JavascriptInterface
        public boolean usesEmbeddedBackend() {
            return true;
        }

        @JavascriptInterface
        public boolean isEmbeddedBackendReady() {
            return MiruPythonRuntime.isReady();
        }

        @JavascriptInterface
        public String getEmbeddedLaunchUrl() {
            return MiruPythonRuntime.getLaunchUrl();
        }

        // --- Background Connection Service ---

        @JavascriptInterface
        public void startConnectionService(String serverUrl, String authToken, String deviceId) {
            Log.i(TAG, "startConnectionService called");
            MiruProfileStore.updateActiveTransport(
                    MainActivity.this, serverUrl, authToken, deviceId);
            runOnUiThread(() -> {
                Intent intent = new Intent(MainActivity.this, MiruConnectionService.class);
                intent.putExtra("serverUrl", serverUrl);
                intent.putExtra("authToken", authToken);
                intent.putExtra("deviceId", deviceId);
                startForegroundService(intent);
            });
        }

        @JavascriptInterface
        public void stopConnectionService() {
            Log.i(TAG, "stopConnectionService called");
            runOnUiThread(() -> {
                Intent intent = new Intent(MainActivity.this, MiruConnectionService.class);
                intent.setAction("STOP");
                startService(intent);
            });
        }

        @JavascriptInterface
        public boolean isConnectionServiceRunning() {
            return MiruConnectionService.isRunning;
        }

        // --- Device Info ---

        @JavascriptInterface
        public String getDeviceName() {
            String manufacturer = Build.MANUFACTURER;
            String model = Build.MODEL;
            // If model already starts with manufacturer, don't repeat
            if (model.toLowerCase().startsWith(manufacturer.toLowerCase())) {
                return model;
            }
            // Capitalize manufacturer first letter
            manufacturer = manufacturer.substring(0, 1).toUpperCase() + manufacturer.substring(1);
            return manufacturer + " " + model;
        }

        @JavascriptInterface
        public String getInstallDeviceId() {
            return MiruProfileStore.getOrCreateInstallDeviceId(MainActivity.this);
        }

        @JavascriptInterface
        public String getClientSecret() {
            return MiruProfileStore.getOrCreateClientSecret(MainActivity.this);
        }

        @JavascriptInterface
        public String getAppVersionName() {
            try {
                return getPackageManager().getPackageInfo(getPackageName(), 0).versionName;
            } catch (Exception e) {
                return "unknown";
            }
        }

        @JavascriptInterface
        public int getAppVersionCode() {
            try {
                return (int) PackageInfoCompat.getLongVersionCode(
                        getPackageManager().getPackageInfo(getPackageName(), 0));
            } catch (Exception e) {
                return -1;
            }
        }

        @JavascriptInterface
        public void setScreenCaptureInterval(int seconds) {
            int clamped = Math.max(5, Math.min(3600, seconds));
            MiruProfileStore.setCaptureInterval(MainActivity.this, clamped);
            Log.i(TAG_CAPTURE, "Capture interval set to " + clamped + "s");
        }

        @JavascriptInterface
        public int getScreenCaptureInterval() {
            return MiruProfileStore.getCaptureInterval(MainActivity.this);
        }

        @JavascriptInterface
        public String getScreenCaptureStatus() {
            boolean prefEnabled = MiruProfileStore.isCaptureEnabled(MainActivity.this);
            return "{\"active\":" + ScreenCaptureService.isRunning
                    + ",\"preferenceEnabled\":" + prefEnabled
                    + ",\"uploadCount\":" + ScreenCaptureService.uploadCount
                    + ",\"droppedCount\":" + ScreenCaptureService.droppedCount
                    + ",\"lastUploadTime\":" + ScreenCaptureService.lastUploadTime
                    + ",\"error\":" + (ScreenCaptureService.lastError != null
                        ? "\"" + ScreenCaptureService.lastError.replace("\"", "'") + "\""
                        : "null")
                    + "}";
        }

        // --- Live Wallpaper ---

        @JavascriptInterface
        public void setLiveWallpaper() {
            Log.i(TAG, "setLiveWallpaper called");
            runOnUiThread(() -> {
                try {
                    // Standard approach: open preview for our specific wallpaper
                    Intent intent = new Intent(WallpaperManager.ACTION_CHANGE_LIVE_WALLPAPER);
                    intent.putExtra(WallpaperManager.EXTRA_LIVE_WALLPAPER_COMPONENT,
                            new ComponentName(MainActivity.this, MiruWallpaperService.class));
                    startActivity(intent);
                } catch (Exception e) {
                    Log.w(TAG, "ACTION_CHANGE_LIVE_WALLPAPER failed, trying chooser", e);
                    try {
                        // Fallback: open general live wallpaper list
                        Intent fallback = new Intent("android.service.wallpaper.LIVE_WALLPAPER_CHOOSER");
                        startActivity(fallback);
                    } catch (Exception e2) {
                        Log.e(TAG, "Live wallpaper chooser also failed", e2);
                        // Last resort: open system wallpaper settings
                        try {
                            Intent settings = new Intent(android.provider.Settings.ACTION_DISPLAY_SETTINGS);
                            startActivity(settings);
                        } catch (Exception e3) {
                            Log.e(TAG, "All wallpaper intents failed", e3);
                        }
                    }
                }
            });
        }

        @JavascriptInterface
        public boolean isLiveWallpaperActive() {
            WallpaperManager wm = WallpaperManager.getInstance(MainActivity.this);
            WallpaperInfo info = wm.getWallpaperInfo();
            if (info == null) return false;
            return info.getComponent().equals(
                    new ComponentName(MainActivity.this, MiruWallpaperService.class));
        }

        // --- APK Auto-Update (DownloadManager + FileProvider install) ---

        /**
         * Download the APK in the background and launch the system installer
         * when finished. Progress is reported back to JS via
         * window._onApkUpdateProgress({state, pct, downloaded, total}).
         *
         * @param downloadUrl Absolute HTTPS URL to the APK file.
         * @param versionName Version label shown in the notification.
         */
        @JavascriptInterface
        public void startApkUpdate(String downloadUrl, String versionName) {
            Log.i(TAG, "startApkUpdate: url=" + downloadUrl + " v=" + versionName);
            // Use activity context — ApkUpdateManager needs it for WebView callbacks
            ApkUpdateManager.startUpdate(MainActivity.this, downloadUrl, versionName);
        }
    }
}
