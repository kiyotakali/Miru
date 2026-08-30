package com.miru.companion;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.RemoteInput;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.NetworkRequest;
import android.os.Build;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.IBinder;
import android.util.Log;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.HttpURLConnection;
import java.net.URL;

/**
 * Persistent foreground service that maintains the VPS connection while the app
 * is in the background.  Responsibilities:
 *
 *   1. Native SSE connection to /api/events — receives chat_message events and
 *      shows Android notifications so the user never misses a message.
 *   2. Periodic screenshot-queue drain — uploads any queued screenshots that
 *      failed to upload while ScreenCaptureService was disconnected.
 *   3. Auto-reconnect with exponential backoff on network failures.
 *
 * Lifecycle: started on successful login, stopped on logout.  Independent of
 * ScreenCaptureService (runs even when screen capture is disabled).
 */
public class MiruConnectionService extends Service {

    private static final String TAG = "MiruConnection";
    private static final String CHANNEL_CONN = "miru_connection";    // low-priority ongoing
    private static final String CHANNEL_MSG  = "miru_messages";      // default, shared with NotificationPollWorker
    private static final int NOTIF_ONGOING = 3001;
    private static final int NOTIF_MESSAGE_BASE = 3100;
    private static final int NOTIFICATION_COLOR = 0xFF6B5448;

    private static final long BACKOFF_INIT  = 3_000;   // 3 s
    private static final long BACKOFF_MAX   = 60_000;  // 60 s

    static volatile boolean isRunning = false;
    static volatile boolean networkAlive = false;

    // Static reference to the live instance so MainActivity can synchronously
    // tear down the SSE socket on credential clear (admin-side delete or
    // logout). Without this, STOP via Intent is async — there's a 1-2s
    // window during which the service still holds the previous user's
    // SSE stream open. See multi-tenant audit 2026-05-08.
    private static volatile MiruConnectionService sLiveInstance = null;

    private String serverUrl;
    private String authToken;
    private String deviceId;

    private HandlerThread workerThread;
    private Handler workerHandler;
    private volatile boolean stopped = false;
    private volatile boolean appInForeground = true; // suppress notifications when foreground
    private long backoff = BACKOFF_INIT;
    private volatile boolean networkJustChanged = false;
    private ConnectivityManager.NetworkCallback networkCallback;

    // SSE state
    private HttpURLConnection sseConnection;
    private int messageNotifCounter = 0;

    // ---- Lifecycle ----

    @Override
    public void onCreate() {
        super.onCreate();
        createChannels();
        sLiveInstance = this;
    }

    /**
     * Synchronously tear down any in-flight SSE connection. Used by
     * MainActivity's clearCredentials() so the previous user's stream is
     * killed before the relaunch / token swap (no 1-2s window where the
     * service still receives the old user's pushes).
     *
     * Safe to call from any thread; closeSseConnection itself is the same.
     * Service self-stops via STOP intent independently.
     */
    public static void disconnectNow() {
        MiruConnectionService inst = sLiveInstance;
        if (inst == null) return;
        Log.i(TAG, "disconnectNow: force-closing SSE for credential clear");
        inst.stopped = true;
        try { inst.closeSseConnection(); } catch (Exception e) {
            Log.w(TAG, "disconnectNow: close failed: " + e);
        }
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent == null) {
            stopSelf();
            return START_NOT_STICKY;
        }

        String action = intent.getAction();
        if ("STOP".equals(action)) {
            Log.i(TAG, "Stop requested");
            stopSelf();
            return START_NOT_STICKY;
        }
        if ("FOREGROUND".equals(action)) {
            appInForeground = true;
            Log.i(TAG, "App in foreground — suppressing notifications");
            return START_STICKY;
        }
        if ("BACKGROUND".equals(action)) {
            appInForeground = false;
            Log.i(TAG, "App in background — notifications enabled");
            return START_STICKY;
        }

        // Read credentials
        SharedPreferences prefs = MiruProfileStore.globals(this);
        serverUrl = intent.getStringExtra("serverUrl");
        authToken = intent.getStringExtra("authToken");
        deviceId  = intent.getStringExtra("deviceId");
        if (serverUrl == null) serverUrl = prefs.getString("capture_server_url", "");
        if (authToken == null) authToken = prefs.getString("capture_auth_token", "");
        if (deviceId  == null) deviceId  = prefs.getString("capture_device_id", "");

        if (serverUrl.isEmpty()) {
            Log.w(TAG, "No server URL, cannot start");
            stopSelf();
            return START_NOT_STICKY;
        }

        // A user-visible companion connection is intentionally long-lived.
        // dataSync has a six-hour background budget on Android 15+, so it is
        // not a valid type for Miru's persistent SSE channel.
        if (Build.VERSION.SDK_INT >= 29) {
            startForeground(NOTIF_ONGOING, buildOngoingNotification("连接中..."),
                    android.content.pm.ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        } else {
            startForeground(NOTIF_ONGOING, buildOngoingNotification("连接中..."));
        }

        if (workerThread == null || !workerThread.isAlive()) {
            stopped = false;
            isRunning = true;
            backoff = BACKOFF_INIT;
            workerThread = new HandlerThread("MiruConn");
            workerThread.start();
            workerHandler = new Handler(workerThread.getLooper());
            workerHandler.post(this::sseLoop);
            registerNetworkCallback();
            Log.i(TAG, "Connection service started → " + serverUrl);
        }

        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        stopped = true;
        isRunning = false;
        networkAlive = false;
        closeSseConnection();
        if (networkCallback != null) {
            try {
                ConnectivityManager cm = (ConnectivityManager) getSystemService(CONNECTIVITY_SERVICE);
                cm.unregisterNetworkCallback(networkCallback);
            } catch (Exception ignored) {}
        }
        if (workerThread != null) {
            workerThread.quitSafely();
            workerThread = null;
        }
        if (sLiveInstance == this) sLiveInstance = null;
        Log.i(TAG, "Connection service destroyed");
        super.onDestroy();
    }

    @Override
    public void onTimeout(int startId, int fgsType) {
        Log.w(TAG, "Foreground-service timeout; closing SSE before system stop");
        stopped = true;
        closeSseConnection();
        stopSelf(startId);
    }

    @Override
    public IBinder onBind(Intent intent) { return null; }

    // ---- SSE Loop ----

    private void sseLoop() {
        while (!stopped) {
            try {
                connectSSE();
            } catch (Exception e) {
                if (stopped) break;
                Log.w(TAG, "SSE error: " + e.getMessage());
            }
            if (stopped) break;
            networkAlive = false;
            Log.i(TAG, "SSE reconnecting in " + (backoff / 1000) + "s");
            updateOngoingNotification("重连中...");
            long sleepEnd = System.currentTimeMillis() + backoff;
            while (!stopped && !networkJustChanged && System.currentTimeMillis() < sleepEnd) {
                try { Thread.sleep(Math.min(1000, sleepEnd - System.currentTimeMillis())); }
                catch (InterruptedException ignored) { break; }
            }
            if (networkJustChanged) {
                networkJustChanged = false;
                backoff = BACKOFF_INIT;
                Log.i(TAG, "Network changed during backoff, reconnecting now");
            } else {
                backoff = Math.min(backoff * 2, BACKOFF_MAX);
            }
        }
    }

    private void connectSSE() throws Exception {
        String urlStr = serverUrl + "/api/events?device_id="
                + java.net.URLEncoder.encode(deviceId, "UTF-8")
                + "&token=" + java.net.URLEncoder.encode(authToken, "UTF-8");

        URL url = new URL(urlStr);
        sseConnection = (HttpURLConnection) url.openConnection();
        sseConnection.setRequestMethod("GET");
        sseConnection.setRequestProperty("Accept", "text/event-stream");
        sseConnection.setConnectTimeout(15_000);
        sseConnection.setReadTimeout(90_000); // 3 missed heartbeats (30s each) = dead
        sseConnection.setDoInput(true);

        int code = sseConnection.getResponseCode();
        if (code != 200) {
            Log.w(TAG, "SSE HTTP " + code);
            closeSseConnection();
            return;
        }

        // Connected
        backoff = BACKOFF_INIT;
        networkAlive = true;
        updateOngoingNotification("已连接");
        Log.i(TAG, "SSE connected");

        BufferedReader reader = new BufferedReader(
                new InputStreamReader(sseConnection.getInputStream(), "UTF-8"));

        String eventType = "";
        StringBuilder dataBuffer = new StringBuilder();

        String line;
        while (!stopped && (line = reader.readLine()) != null) {
            if (line.startsWith("event: ")) {
                eventType = line.substring(7).trim();
            } else if (line.startsWith("data: ")) {
                dataBuffer.append(line.substring(6));
            } else if (line.isEmpty() && dataBuffer.length() > 0) {
                // End of event — dispatch
                handleSSEEvent(eventType, dataBuffer.toString());
                eventType = "";
                dataBuffer.setLength(0);
            }
            // ": heartbeat" comment lines are ignored (keep-alive)
        }

        reader.close();
        closeSseConnection();
    }

    private void closeSseConnection() {
        try {
            if (sseConnection != null) {
                sseConnection.disconnect();
                sseConnection = null;
            }
        } catch (Exception ignored) {}
    }

    private void registerNetworkCallback() {
        ConnectivityManager cm = (ConnectivityManager) getSystemService(CONNECTIVITY_SERVICE);
        networkCallback = new ConnectivityManager.NetworkCallback() {
            @Override
            public void onAvailable(Network network) {
                Log.i(TAG, "Network available — triggering SSE reconnect");
                networkJustChanged = true;
                closeSseConnection();
            }

            @Override
            public void onLost(Network network) {
                ConnectivityManager cm = (ConnectivityManager) getSystemService(CONNECTIVITY_SERVICE);
                if (cm.getActiveNetwork() == null) {
                    Log.i(TAG, "All networks lost");
                    networkAlive = false;
                }
            }
        };
        NetworkRequest request = new NetworkRequest.Builder()
                .addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
                .build();
        cm.registerNetworkCallback(request, networkCallback);
    }

    private void handleSSEEvent(String type, String data) {
        if ("connected".equals(type)) {
            Log.i(TAG, "SSE: connected event received");
            return;
        }
        if ("chat_message".equals(type)) {
            try {
                org.json.JSONObject msg = new org.json.JSONObject(data);
                String role = msg.optString("role", "");
                Log.i(TAG, "SSE: chat_message role=" + role + " fg=" + appInForeground);
                // Only notify for assistant messages (not user's own messages)
                if ("assistant".equals(role) && !appInForeground) {
                    String text = msg.optString("text", "");
                    if (!text.isEmpty()) {
                        Log.i(TAG, "SSE: showing notification for: " + text.substring(0, Math.min(40, text.length())));
                        showMessageNotification(text);
                    }
                }
            } catch (Exception e) {
                Log.w(TAG, "SSE chat_message parse error: " + e.getMessage());
            }
        }
        // Other event types (typing_start, data_changed, etc.) are ignored —
        // they only matter to the WebView which handles them when foregrounded.
    }

    // ---- Notifications ----

    private void createChannels() {
        NotificationManager nm = getSystemService(NotificationManager.class);

        // Low-priority ongoing channel for connection status
        NotificationChannel conn = new NotificationChannel(
                CHANNEL_CONN, "Miru 连接", NotificationManager.IMPORTANCE_LOW);
        conn.setDescription("保持与 Miru 服务器的后台连接");
        conn.setShowBadge(false);
        nm.createNotificationChannel(conn);

        // Message channel (shared with NotificationPollWorker)
        NotificationChannel msg = new NotificationChannel(
                CHANNEL_MSG, "Miru 消息", NotificationManager.IMPORTANCE_DEFAULT);
        msg.setDescription("来自 Miru 的消息和关怀");
        msg.enableVibration(true);
        nm.createNotificationChannel(msg);
    }

    private Notification buildOngoingNotification(String status) {
        Intent tapIntent = new Intent(this, MainActivity.class);
        tapIntent.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent tapPending = PendingIntent.getActivity(
                this, 0, tapIntent,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);

        return new Notification.Builder(this, CHANNEL_CONN)
                .setContentTitle("Miru")
                .setContentText(status)
                .setSmallIcon(R.mipmap.miru_launcher)
                .setLargeIcon(getLauncherLargeIcon())
                .setColor(NOTIFICATION_COLOR)
                .setOngoing(true)
                .setContentIntent(tapPending)
                .build();
    }

    private Bitmap getLauncherLargeIcon() {
        return BitmapFactory.decodeResource(getResources(), R.mipmap.miru_launcher_foreground);
    }

    private void updateOngoingNotification(String status) {
        NotificationManager nm = getSystemService(NotificationManager.class);
        nm.notify(NOTIF_ONGOING, buildOngoingNotification(status));
    }

    private void showMessageNotification(String text) {
        Context ctx = this;
        int notifId = NOTIF_MESSAGE_BASE + (messageNotifCounter++ % 20);

        Intent tapIntent = new Intent(ctx, MainActivity.class);
        tapIntent.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        PendingIntent tapPending = PendingIntent.getActivity(
                ctx, notifId, tapIntent,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);

        // Inline reply
        RemoteInput remoteInput = new RemoteInput.Builder(NotificationReplyReceiver.KEY_REPLY)
                .setLabel("回复 Miru...")
                .build();
        Intent replyIntent = new Intent(ctx, NotificationReplyReceiver.class);
        replyIntent.putExtra(NotificationReplyReceiver.EXTRA_NOTIF_ID, notifId);
        PendingIntent replyPending = PendingIntent.getBroadcast(
                ctx, notifId + 1000, replyIntent,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_MUTABLE);
        Notification.Action replyAction = new Notification.Action.Builder(
                R.mipmap.miru_launcher, "回复", replyPending)
                .addRemoteInput(remoteInput)
                .setAllowGeneratedReplies(true)
                .build();

        String body = text.length() > 200 ? text.substring(0, 200) + "..." : text;

        Notification notification = new Notification.Builder(ctx, CHANNEL_MSG)
                .setContentTitle("Miru")
                .setContentText(body)
                .setStyle(new Notification.BigTextStyle().bigText(body))
                .setSmallIcon(R.mipmap.miru_launcher)
                .setLargeIcon(getLauncherLargeIcon())
                .setColor(NOTIFICATION_COLOR)
                .setAutoCancel(true)
                .setContentIntent(tapPending)
                .addAction(replyAction)
                .build();

        NotificationManager nm = getSystemService(NotificationManager.class);
        nm.notify(notifId, notification);

        // Also set bubble for wallpaper
        Live2DBridge.setPendingBubble(body);
    }
}
