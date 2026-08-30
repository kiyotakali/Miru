package com.miru.companion;

import android.app.DownloadManager;
import android.app.NotificationManager;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.database.Cursor;
import android.net.Uri;
import android.os.Build;
import android.os.Environment;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.util.Log;
import android.widget.Toast;

import androidx.core.content.FileProvider;

import java.io.File;

/**
 * APK auto-update flow (Plan B):
 *   1. Front-end JS calls MiruAndroid.startApkUpdate(url, versionName)
 *   2. Enqueue a DownloadManager request → Downloads/miru-latest.apk
 *   3. Register a one-shot BroadcastReceiver on ACTION_DOWNLOAD_COMPLETE
 *   4. On success: verify file exists, launch Intent.ACTION_VIEW with FileProvider URI
 *   5. System package installer takes over
 *
 * Safety:
 *   - Only one concurrent download (cancels previous ID if still running)
 *   - Android 8+ requires REQUEST_INSTALL_PACKAGES → we deep-link to settings if needed
 *   - Progress piped back to WebView via evaluateJavascript → window._onApkUpdateProgress
 */
public class ApkUpdateManager {
    private static final String TAG = "MiruApkUpdate";
    private static final String APK_FILENAME = "miru-latest.apk";
    private static final long PROGRESS_POLL_MS = 800;

    // Thread-safe static — only one download at a time
    private static volatile long currentDownloadId = -1L;
    private static volatile BroadcastReceiver completionReceiver = null;
    private static volatile Runnable pollProgressRunnable = null;

    private ApkUpdateManager() {}

    /**
     * Entry point from the JS bridge. Starts a fresh download; any in-flight
     * download is cancelled first so the user can retry without killing the app.
     */
    public static void startUpdate(final Context appCtx, final String downloadUrl,
                                   final String versionName) {
        if (downloadUrl == null || downloadUrl.isEmpty()) {
            Log.w(TAG, "startUpdate: empty downloadUrl");
            return;
        }

        // Install-permission gate (Android 8+). Without this the ACTION_VIEW
        // after download will silently fail, so prompt the user up-front.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            if (!appCtx.getPackageManager().canRequestPackageInstalls()) {
                new Handler(Looper.getMainLooper()).post(() -> {
                    Toast.makeText(appCtx,
                            "请允许 Miru 安装应用（一次授权即可）",
                            Toast.LENGTH_LONG).show();
                });
                Intent i = new Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES)
                        .setData(Uri.parse("package:" + appCtx.getPackageName()))
                        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                try {
                    appCtx.startActivity(i);
                } catch (Exception e) {
                    Log.e(TAG, "Could not open install-permission settings", e);
                }
                return;
            }
        }

        DownloadManager dm = (DownloadManager)
                appCtx.getSystemService(Context.DOWNLOAD_SERVICE);
        if (dm == null) {
            Log.e(TAG, "DownloadManager not available");
            return;
        }

        // Cancel any previous download from a failed/retry cycle
        cancelCurrent(appCtx);

        // Clear old file if present (DownloadManager refuses to overwrite)
        File dst = new File(appCtx.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS),
                APK_FILENAME);
        if (dst.exists()) {
            //noinspection ResultOfMethodCallIgnored
            dst.delete();
        }

        DownloadManager.Request req = new DownloadManager.Request(Uri.parse(downloadUrl))
                .setTitle("Miru 更新 " + (versionName != null ? versionName : ""))
                .setDescription("正在下载新版本…")
                .setMimeType("application/vnd.android.package-archive")
                .setNotificationVisibility(
                        DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
                .setDestinationInExternalFilesDir(appCtx,
                        Environment.DIRECTORY_DOWNLOADS, APK_FILENAME)
                .setAllowedOverMetered(true)
                .setAllowedOverRoaming(true);

        try {
            currentDownloadId = dm.enqueue(req);
            Log.i(TAG, "Download enqueued: id=" + currentDownloadId
                    + " url=" + downloadUrl);
        } catch (Exception e) {
            Log.e(TAG, "DownloadManager enqueue failed", e);
            currentDownloadId = -1L;
            return;
        }

        // Register completion receiver (auto-unregisters on fire)
        registerCompletionReceiver(appCtx);
        // Start polling for progress updates → WebView
        startProgressPolling(appCtx);
    }

    private static void registerCompletionReceiver(final Context appCtx) {
        // If a stale receiver is hanging around, clean it up first
        unregisterCompletionReceiver(appCtx);

        completionReceiver = new BroadcastReceiver() {
            @Override
            public void onReceive(Context ctx, Intent intent) {
                long id = intent.getLongExtra(
                        DownloadManager.EXTRA_DOWNLOAD_ID, -1L);
                if (id != currentDownloadId) return;
                stopProgressPolling();
                handleDownloadFinished(appCtx, id);
                unregisterCompletionReceiver(appCtx);
            }
        };

        IntentFilter filter = new IntentFilter(DownloadManager.ACTION_DOWNLOAD_COMPLETE);
        if (Build.VERSION.SDK_INT >= 33) {
            // RECEIVER_EXPORTED needed on 33+ for system-sent broadcasts
            appCtx.registerReceiver(completionReceiver, filter,
                    Context.RECEIVER_EXPORTED);
        } else {
            appCtx.registerReceiver(completionReceiver, filter);
        }
    }

    private static synchronized void unregisterCompletionReceiver(Context appCtx) {
        if (completionReceiver != null) {
            try {
                appCtx.unregisterReceiver(completionReceiver);
            } catch (IllegalArgumentException ignored) { }
            completionReceiver = null;
        }
    }

    private static void handleDownloadFinished(Context appCtx, long id) {
        DownloadManager dm = (DownloadManager)
                appCtx.getSystemService(Context.DOWNLOAD_SERVICE);
        if (dm == null) return;

        DownloadManager.Query q = new DownloadManager.Query().setFilterById(id);
        Cursor c = dm.query(q);
        if (c == null) return;

        try {
            if (!c.moveToFirst()) return;
            int statusIdx = c.getColumnIndex(DownloadManager.COLUMN_STATUS);
            int reasonIdx = c.getColumnIndex(DownloadManager.COLUMN_REASON);
            int uriIdx = c.getColumnIndex(DownloadManager.COLUMN_LOCAL_URI);

            int status = statusIdx >= 0 ? c.getInt(statusIdx) : DownloadManager.STATUS_FAILED;
            int reason = reasonIdx >= 0 ? c.getInt(reasonIdx) : 0;
            String localUri = uriIdx >= 0 ? c.getString(uriIdx) : null;

            if (status == DownloadManager.STATUS_SUCCESSFUL && localUri != null) {
                installApk(appCtx, Uri.parse(localUri));
                notifyWeb(appCtx, "{\"state\":\"done\"}");
            } else {
                Log.w(TAG, "Download failed: status=" + status + " reason=" + reason);
                notifyWeb(appCtx, "{\"state\":\"error\",\"reason\":" + reason + "}");
                new Handler(Looper.getMainLooper()).post(() ->
                        Toast.makeText(appCtx, "下载失败，请稍后重试",
                                Toast.LENGTH_LONG).show());
            }
        } finally {
            c.close();
            currentDownloadId = -1L;
        }
    }

    private static void installApk(Context appCtx, Uri localUri) {
        // localUri is a file:// URI under the app's getExternalFilesDir. We MUST
        // re-wrap it via FileProvider (content://) for ACTION_VIEW on API 24+.
        File apkFile;
        if ("file".equals(localUri.getScheme())) {
            apkFile = new File(localUri.getPath() != null ? localUri.getPath() : "");
        } else {
            // Already content:// — launch directly
            Intent install = new Intent(Intent.ACTION_VIEW)
                    .setDataAndType(localUri, "application/vnd.android.package-archive")
                    .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            try { appCtx.startActivity(install); }
            catch (Exception e) { Log.e(TAG, "ACTION_VIEW failed", e); }
            return;
        }

        if (!apkFile.exists() || apkFile.length() == 0) {
            Log.e(TAG, "Downloaded APK is missing or empty: " + apkFile);
            notifyWeb(appCtx, "{\"state\":\"error\",\"reason\":\"missing_file\"}");
            return;
        }

        Uri contentUri;
        try {
            contentUri = FileProvider.getUriForFile(appCtx,
                    appCtx.getPackageName() + ".fileprovider", apkFile);
        } catch (Exception e) {
            Log.e(TAG, "FileProvider URI failed", e);
            return;
        }

        Intent install = new Intent(Intent.ACTION_VIEW)
                .setDataAndType(contentUri, "application/vnd.android.package-archive")
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        try {
            appCtx.startActivity(install);
            Log.i(TAG, "Install intent launched for " + apkFile.getName());
        } catch (Exception e) {
            Log.e(TAG, "Failed to launch install intent", e);
            notifyWeb(appCtx, "{\"state\":\"error\",\"reason\":\"install_intent_failed\"}");
        }
    }

    private static void cancelCurrent(Context appCtx) {
        if (currentDownloadId < 0) return;
        DownloadManager dm = (DownloadManager)
                appCtx.getSystemService(Context.DOWNLOAD_SERVICE);
        if (dm != null) {
            try { dm.remove(currentDownloadId); } catch (Exception ignored) { }
        }
        currentDownloadId = -1L;
        stopProgressPolling();
        unregisterCompletionReceiver(appCtx);
    }

    // --------------------------------------------------------------------
    // Progress polling (→ WebView)
    // --------------------------------------------------------------------

    private static void startProgressPolling(final Context appCtx) {
        stopProgressPolling();
        final Handler h = new Handler(Looper.getMainLooper());
        pollProgressRunnable = new Runnable() {
            @Override
            public void run() {
                long id = currentDownloadId;
                if (id < 0) return;
                DownloadManager dm = (DownloadManager)
                        appCtx.getSystemService(Context.DOWNLOAD_SERVICE);
                if (dm == null) return;
                DownloadManager.Query q = new DownloadManager.Query().setFilterById(id);
                Cursor c = dm.query(q);
                if (c == null) return;
                try {
                    if (c.moveToFirst()) {
                        int sIdx = c.getColumnIndex(DownloadManager.COLUMN_STATUS);
                        int dIdx = c.getColumnIndex(DownloadManager.COLUMN_BYTES_DOWNLOADED_SO_FAR);
                        int tIdx = c.getColumnIndex(DownloadManager.COLUMN_TOTAL_SIZE_BYTES);
                        int status = sIdx >= 0 ? c.getInt(sIdx) : 0;
                        long downloaded = dIdx >= 0 ? c.getLong(dIdx) : 0;
                        long total = tIdx >= 0 ? c.getLong(tIdx) : -1;
                        int pct = (total > 0)
                                ? (int) (downloaded * 100 / total)
                                : -1;

                        String state = "running";
                        if (status == DownloadManager.STATUS_PAUSED) state = "paused";
                        else if (status == DownloadManager.STATUS_PENDING) state = "pending";
                        else if (status == DownloadManager.STATUS_SUCCESSFUL) state = "done";
                        else if (status == DownloadManager.STATUS_FAILED) state = "error";

                        notifyWeb(appCtx, "{\"state\":\"" + state + "\",\"pct\":" + pct
                                + ",\"downloaded\":" + downloaded
                                + ",\"total\":" + total + "}");

                        if (status == DownloadManager.STATUS_SUCCESSFUL
                                || status == DownloadManager.STATUS_FAILED) {
                            return; // stop polling; receiver handles finalize
                        }
                    }
                } finally {
                    c.close();
                }
                // Schedule next tick
                if (pollProgressRunnable != null) {
                    h.postDelayed(pollProgressRunnable, PROGRESS_POLL_MS);
                }
            }
        };
        h.postDelayed(pollProgressRunnable, PROGRESS_POLL_MS);
    }

    private static void stopProgressPolling() {
        pollProgressRunnable = null;
    }

    /** Fire an event to the WebView (main-thread dispatch). No-op if no WebView. */
    private static void notifyWeb(Context appCtx, final String jsonPayload) {
        if (!(appCtx instanceof MainActivity)) {
            // appCtx may be application context in tests; hunt for live activity
            return;
        }
        final MainActivity act = (MainActivity) appCtx;
        new Handler(Looper.getMainLooper()).post(() -> {
            try {
                android.webkit.WebView wv = act.getBridge().getWebView();
                if (wv != null) {
                    wv.evaluateJavascript(
                            "if(typeof window._onApkUpdateProgress==='function'){"
                                    + "try{window._onApkUpdateProgress(" + jsonPayload + ")}"
                                    + "catch(e){}}",
                            null);
                }
            } catch (Exception e) {
                Log.w(TAG, "notifyWeb failed", e);
            }
        });
    }

    /** Silence the DownloadManager notification after install (optional). */
    public static void clearDownloadNotification(Context appCtx) {
        NotificationManager nm = (NotificationManager)
                appCtx.getSystemService(Context.NOTIFICATION_SERVICE);
        if (nm != null) {
            // DownloadManager uses its own IDs; we don't touch them here.
            // Left as a hook for future refinement.
        }
    }
}
