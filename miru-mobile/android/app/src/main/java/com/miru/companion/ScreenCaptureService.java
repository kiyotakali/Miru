package com.miru.companion;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.graphics.Bitmap;
import android.graphics.PixelFormat;
import android.hardware.display.DisplayManager;
import android.hardware.display.VirtualDisplay;
import android.media.Image;
import android.media.ImageReader;
import android.media.projection.MediaProjection;
import android.media.projection.MediaProjectionManager;
import android.os.BatteryManager;
import android.os.Build;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.IBinder;
import android.util.DisplayMetrics;
import android.util.Log;
import android.view.WindowManager;

import java.io.ByteArrayOutputStream;
import java.io.DataOutputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.SocketTimeoutException;
import java.net.URL;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

public class ScreenCaptureService extends Service {

    private static final String TAG = "MiruScreenCapture";
    private static final String CHANNEL_ID = "miru_screen_capture";
    private static final int NOTIFICATION_ID = 1001;
    private static final int DEFAULT_INTERVAL_S = 30;
    private static final int NOTIFICATION_COLOR = 0xFF6B5448;
    private static final int MAX_INTERVAL_MS = 3_600_000;
    private static final float CHANGE_THRESHOLD = 0.30f; // 30% pixel diff — match desktop sensor.py.
    private static final int THUMB_W = 200;
    private static final int THUMB_H = 150;
    private static final int MAX_CAPTURE_DIM = 1920;
    private static final int JPEG_QUALITY = 60;
    private static final int UPLOAD_MAX_RETRIES = 2;
    private static final long UPLOAD_RETRY_DELAY_MS = 2_000;

    // Static state accessible from MainActivity
    static volatile boolean isRunning = false;
    static volatile int uploadCount = 0;
    static volatile long lastUploadTime = 0;
    static volatile String lastError = null;
    static volatile int droppedCount = 0;
    // Exposed so MainActivity.requestScreenCapture can detect a stale-token
    // service (account switch / token rotation) and restart it. Without this
    // the service keeps uploading with the previous account's token and
    // every /api/device/screenshot returns 401 silently.
    static volatile String currentAuthToken = null;

    private MediaProjection mediaProjection;
    private VirtualDisplay virtualDisplay;
    private ImageReader imageReader;
    private HandlerThread handlerThread;
    private Handler handler;
    private int captureWidth;
    private int captureHeight;
    private int screenDpi;

    private String serverUrl;
    private String authToken;
    private String deviceId;

    private int[] lastThumbnail;
    private int currentIntervalMs;
    private int noChangeCount = 0;
    private boolean screenOn = true;
    private boolean lowBattery = false;
    private final SimpleDateFormat isoFmt =
            new SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss", Locale.US);

    static boolean timeoutMeansServerAccepted(boolean requestBodySent) {
        return requestBodySent;
    }

    private final BroadcastReceiver screenReceiver = new BroadcastReceiver() {
        @Override
        public void onReceive(Context context, Intent intent) {
            if (Intent.ACTION_SCREEN_OFF.equals(intent.getAction())) {
                screenOn = false;
                Log.i(TAG, "Screen OFF, pausing capture");
            } else if (Intent.ACTION_SCREEN_ON.equals(intent.getAction())) {
                screenOn = true;
                Log.i(TAG, "Screen ON, resuming capture");
            }
        }
    };

    private final BroadcastReceiver batteryReceiver = new BroadcastReceiver() {
        @Override
        public void onReceive(Context context, Intent intent) {
            if (Intent.ACTION_BATTERY_LOW.equals(intent.getAction())) {
                lowBattery = true;
                Log.i(TAG, "Battery LOW, pausing capture");
            } else if (Intent.ACTION_BATTERY_OKAY.equals(intent.getAction())) {
                lowBattery = false;
                Log.i(TAG, "Battery OK, resuming capture");
            }
        }
    };

    private final MediaProjection.Callback projectionCallback = new MediaProjection.Callback() {
        @Override
        public void onStop() {
            Log.i(TAG, "MediaProjection stopped by system");
            stopSelf();
        }
    };

    @Override
    public void onCreate() {
        super.onCreate();
        createNotificationChannel();
    }

    /**
     * Read base capture interval from SharedPreferences (set by JS Bridge).
     * Clamped to 5-3600 seconds. Falls back to DEFAULT_INTERVAL_S.
     */
    private int getBaseIntervalMs() {
        int seconds = MiruProfileStore.getCaptureInterval(this);
        seconds = Math.max(5, Math.min(3600, seconds));
        return seconds * 1000;
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent == null) {
            stopSelf();
            return START_NOT_STICKY;
        }

        String action = intent.getAction();
        if ("STOP".equals(action)) {
            Log.i(TAG, "Stop requested via notification");
            stopSelf();
            return START_NOT_STICKY;
        }

        // Start foreground immediately (must be within 5s)
        startForeground(NOTIFICATION_ID, buildNotification(),
                android.content.pm.ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION);

        int resultCode = intent.getIntExtra("resultCode", -1);
        Intent projectionData = intent.getParcelableExtra("projectionData");
        serverUrl = intent.getStringExtra("serverUrl");
        authToken = intent.getStringExtra("authToken");
        deviceId = intent.getStringExtra("deviceId");
        currentAuthToken = authToken;

        if (projectionData == null || serverUrl == null) {
            Log.e(TAG, "Missing projection data or server URL");
            stopSelf();
            return START_NOT_STICKY;
        }

        // Get screen metrics
        WindowManager wm = (WindowManager) getSystemService(WINDOW_SERVICE);
        DisplayMetrics metrics = new DisplayMetrics();
        wm.getDefaultDisplay().getRealMetrics(metrics);
        screenDpi = metrics.densityDpi;

        // Scale down to max capture dimension
        float scale = Math.min(1.0f,
                Math.min((float) MAX_CAPTURE_DIM / metrics.widthPixels,
                         (float) MAX_CAPTURE_DIM / metrics.heightPixels));
        captureWidth = (int) (metrics.widthPixels * scale);
        captureHeight = (int) (metrics.heightPixels * scale);
        // Ensure even dimensions (required by some encoders)
        captureWidth = (captureWidth / 2) * 2;
        captureHeight = (captureHeight / 2) * 2;

        Log.i(TAG, "Capture size: " + captureWidth + "x" + captureHeight + " (dpi=" + screenDpi + ")");

        // Create MediaProjection
        MediaProjectionManager mpm = (MediaProjectionManager) getSystemService(MEDIA_PROJECTION_SERVICE);
        try {
            mediaProjection = mpm.getMediaProjection(resultCode, projectionData);
        } catch (Exception e) {
            Log.e(TAG, "Failed to create MediaProjection", e);
            lastError = "MediaProjection creation failed";
            stopSelf();
            return START_NOT_STICKY;
        }
        mediaProjection.registerCallback(projectionCallback, null);

        // Create ImageReader + VirtualDisplay
        imageReader = ImageReader.newInstance(captureWidth, captureHeight, PixelFormat.RGBA_8888, 2);
        virtualDisplay = mediaProjection.createVirtualDisplay(
                "MiruScreenCapture",
                captureWidth, captureHeight, screenDpi,
                DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR,
                imageReader.getSurface(),
                null, null);

        // Start capture loop on background thread
        handlerThread = new HandlerThread("MiruCapture");
        handlerThread.start();
        handler = new Handler(handlerThread.getLooper());
        currentIntervalMs = getBaseIntervalMs();
        Log.i(TAG, "Base interval: " + currentIntervalMs + "ms");
        handler.postDelayed(this::captureLoop, currentIntervalMs);

        // Register screen and battery receivers
        IntentFilter screenFilter = new IntentFilter();
        screenFilter.addAction(Intent.ACTION_SCREEN_OFF);
        screenFilter.addAction(Intent.ACTION_SCREEN_ON);
        registerReceiver(screenReceiver, screenFilter);

        IntentFilter batteryFilter = new IntentFilter();
        batteryFilter.addAction(Intent.ACTION_BATTERY_LOW);
        batteryFilter.addAction(Intent.ACTION_BATTERY_OKAY);
        registerReceiver(batteryReceiver, batteryFilter);

        // Check initial battery state
        BatteryManager bm = (BatteryManager) getSystemService(BATTERY_SERVICE);
        int level = bm.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY);
        lowBattery = level > 0 && level < 15;

        isRunning = true;
        uploadCount = 0;
        droppedCount = 0;
        lastError = null;
        Log.i(TAG, "Screen capture service started");

        return START_NOT_STICKY;
    }

    private void captureLoop() {
        if (!isRunning) return;

        // Hot-reload base interval from SharedPreferences (changed via JS Bridge)
        int newBase = getBaseIntervalMs();
        if (noChangeCount == 0 && currentIntervalMs != newBase) {
            Log.i(TAG, "Base interval changed: " + currentIntervalMs + "ms -> " + newBase + "ms");
            currentIntervalMs = newBase;
        }

        try {
            if (screenOn && !lowBattery) {
                doCapture();
            } else {
                Log.d(TAG, "Skipping capture (screen=" + screenOn + ", lowBatt=" + lowBattery + ")");
            }
        } catch (Exception e) {
            Log.e(TAG, "Capture loop error", e);
        }

        // Schedule next capture
        if (isRunning && handler != null) {
            handler.postDelayed(this::captureLoop, currentIntervalMs);
        }
    }

    private void doCapture() {
        Image image = null;
        try {
            image = imageReader.acquireLatestImage();
            if (image == null) return;

            // Convert Image to Bitmap
            Image.Plane plane = image.getPlanes()[0];
            int rowStride = plane.getRowStride();
            int pixelStride = plane.getPixelStride();
            int rowPadding = rowStride - pixelStride * captureWidth;

            Bitmap bitmap = Bitmap.createBitmap(
                    captureWidth + rowPadding / pixelStride, captureHeight,
                    Bitmap.Config.ARGB_8888);
            bitmap.copyPixelsFromBuffer(plane.getBuffer());

            // Crop padding if any
            if (rowPadding > 0) {
                bitmap = Bitmap.createBitmap(bitmap, 0, 0, captureWidth, captureHeight);
            }

            image.close();
            image = null;

            // Check if screen changed (thumbnail comparison)
            boolean changed = screenChanged(bitmap);

            if (changed) {
                noChangeCount = 0;
                currentIntervalMs = getBaseIntervalMs();

                // Compress to JPEG
                ByteArrayOutputStream baos = new ByteArrayOutputStream();
                bitmap.compress(Bitmap.CompressFormat.JPEG, JPEG_QUALITY, baos);
                byte[] jpegBytes = baos.toByteArray();
                String capturedAt = isoFmt.format(new Date());

                Log.i(TAG, "Screen changed, uploading " + jpegBytes.length + " bytes");
                uploadScreenshot(jpegBytes, capturedAt);
            } else {
                noChangeCount++;
                if (noChangeCount >= 3) {
                    currentIntervalMs = Math.min(currentIntervalMs * 2, MAX_INTERVAL_MS);
                    Log.d(TAG, "No change x" + noChangeCount + ", interval=" + currentIntervalMs + "ms");
                }
            }

            bitmap.recycle();

        } catch (OutOfMemoryError e) {
            Log.e(TAG, "OOM during capture", e);
            lastError = "Out of memory";
        } catch (Exception e) {
            Log.e(TAG, "Capture error", e);
        } finally {
            if (image != null) image.close();
        }
    }

    private boolean screenChanged(Bitmap bitmap) {
        // Create thumbnail
        Bitmap thumb = Bitmap.createScaledBitmap(bitmap, THUMB_W, THUMB_H, true);
        int[] pixels = new int[THUMB_W * THUMB_H];
        thumb.getPixels(pixels, 0, THUMB_W, 0, 0, THUMB_W, THUMB_H);
        thumb.recycle();

        if (lastThumbnail == null) {
            lastThumbnail = pixels;
            return true; // First frame always uploads
        }

        // Count changed pixels (any RGB channel diff > 15)
        int changed = 0;
        int total = pixels.length;
        for (int i = 0; i < total; i++) {
            int c1 = pixels[i];
            int c2 = lastThumbnail[i];
            int dr = Math.abs(((c1 >> 16) & 0xFF) - ((c2 >> 16) & 0xFF));
            int dg = Math.abs(((c1 >> 8) & 0xFF) - ((c2 >> 8) & 0xFF));
            int db = Math.abs((c1 & 0xFF) - (c2 & 0xFF));
            if (dr > 15 || dg > 15 || db > 15) {
                changed++;
            }
        }

        lastThumbnail = pixels;
        float ratio = (float) changed / total;
        return ratio >= CHANGE_THRESHOLD;
    }

    private void uploadScreenshot(byte[] jpegBytes, String capturedAt) {
        // Skip entirely if MiruConnectionService reports network is down — no
        // point burning retries on a doomed connection. Drop and move on.
        if (MiruConnectionService.isRunning && !MiruConnectionService.networkAlive) {
            droppedCount++;
            Log.i(TAG, "Network down, dropping screenshot (dropped=" + droppedCount + ")");
            return;
        }
        // Try live upload with bounded retry: attempt, fail → wait 2s → retry.
        for (int attempt = 0; attempt <= UPLOAD_MAX_RETRIES; attempt++) {
            if (!isRunning) return;
            if (doUpload(jpegBytes, capturedAt)) {
                return;
            }
            if (attempt < UPLOAD_MAX_RETRIES) {
                Log.i(TAG, "Upload failed (attempt " + (attempt + 1) + "/"
                        + (UPLOAD_MAX_RETRIES + 1) + "), retrying in "
                        + (UPLOAD_RETRY_DELAY_MS / 1000) + "s");
                try { Thread.sleep(UPLOAD_RETRY_DELAY_MS); }
                catch (InterruptedException e) { return; }
            }
        }
        droppedCount++;
        Log.w(TAG, "Upload gave up after " + (UPLOAD_MAX_RETRIES + 1)
                + " attempts, dropping (dropped=" + droppedCount + ")");
    }

    /**
     * POST screenshot to server. Returns true on HTTP 200.
     */
    private boolean doUpload(byte[] jpegBytes, String capturedAt) {
        HttpURLConnection conn = null;
        boolean requestBodySent = false;
        try {
            String boundary = "----MiruBoundary" + System.currentTimeMillis();
            URL url = new URL(serverUrl + "/api/device/screenshot");
            conn = (HttpURLConnection) url.openConnection();
            conn.setRequestMethod("POST");
            conn.setDoOutput(true);
            boolean fast = MiruConnectionService.networkAlive;
            conn.setConnectTimeout(fast ? 5_000 : 10_000);
            // The response includes the vision-model result. Real providers
            // can occasionally take longer than 15 seconds even though the
            // server has already accepted and is processing the image.
            conn.setReadTimeout(fast ? 60_000 : 90_000);
            conn.setRequestProperty("Content-Type", "multipart/form-data; boundary=" + boundary);
            if (authToken != null && !authToken.isEmpty()) {
                conn.setRequestProperty("Authorization", "Bearer " + authToken);
            }

            OutputStream os = conn.getOutputStream();
            DataOutputStream dos = new DataOutputStream(os);

            // device_id field
            dos.writeBytes("--" + boundary + "\r\n");
            dos.writeBytes("Content-Disposition: form-data; name=\"device_id\"\r\n\r\n");
            dos.writeBytes(deviceId + "\r\n");

            // captured_at field (original capture timestamp)
            dos.writeBytes("--" + boundary + "\r\n");
            dos.writeBytes("Content-Disposition: form-data; name=\"captured_at\"\r\n\r\n");
            dos.writeBytes(capturedAt + "\r\n");

            // image field
            dos.writeBytes("--" + boundary + "\r\n");
            dos.writeBytes("Content-Disposition: form-data; name=\"image\"; filename=\"screenshot.jpg\"\r\n");
            dos.writeBytes("Content-Type: image/jpeg\r\n\r\n");
            dos.write(jpegBytes);
            dos.writeBytes("\r\n");

            dos.writeBytes("--" + boundary + "--\r\n");
            dos.flush();
            dos.close();
            requestBodySent = true;

            int responseCode = conn.getResponseCode();

            if (responseCode == 200) {
                uploadCount++;
                lastUploadTime = System.currentTimeMillis();
                lastError = null;
                Log.i(TAG, "Upload success #" + uploadCount);
                return true;
            } else {
                Log.w(TAG, "Upload failed: HTTP " + responseCode);
                lastError = "HTTP " + responseCode;
                return false;
            }
        } catch (SocketTimeoutException e) {
            if (timeoutMeansServerAccepted(requestBodySent)) {
                // Retrying the same captured_at after the complete multipart
                // body was sent creates duplicate screenshot observations when
                // the server is merely still waiting on the vision provider.
                // Treat delivery as accepted; the server continues processing
                // independently after the client stops waiting.
                uploadCount++;
                lastUploadTime = System.currentTimeMillis();
                lastError = null;
                Log.w(TAG, "Server response timed out after upload; not retrying the same frame");
                return true;
            }
            Log.w(TAG, "Upload timeout before request body completed: " + e.getMessage());
            lastError = e.getMessage();
            return false;
        } catch (Exception e) {
            Log.w(TAG, "Upload error: " + e.getMessage());
            lastError = e.getMessage();
            return false;
        } finally {
            if (conn != null) conn.disconnect();
        }
    }

    @Override
    public void onDestroy() {
        isRunning = false;
        currentAuthToken = null;
        Log.i(TAG, "Screen capture service stopping");

        if (handler != null) {
            handler.removeCallbacksAndMessages(null);
        }
        if (virtualDisplay != null) {
            virtualDisplay.release();
            virtualDisplay = null;
        }
        if (imageReader != null) {
            imageReader.close();
            imageReader = null;
        }
        if (mediaProjection != null) {
            mediaProjection.unregisterCallback(projectionCallback);
            mediaProjection.stop();
            mediaProjection = null;
        }
        if (handlerThread != null) {
            handlerThread.quitSafely();
            handlerThread = null;
        }

        try { unregisterReceiver(screenReceiver); } catch (Exception ignored) {}
        try { unregisterReceiver(batteryReceiver); } catch (Exception ignored) {}

        // If user preference is still ON (system killed us, not user stop),
        // show a "paused" notification so user can tap to restore.
        boolean prefEnabled = MiruProfileStore.isCaptureEnabled(this);
        if (prefEnabled) {
            showPausedNotification();
        }

        super.onDestroy();
    }

    private void showPausedNotification() {
        Intent tapIntent = new Intent(this, MainActivity.class);
        tapIntent.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        tapIntent.putExtra("auto_resume_capture", true);
        PendingIntent tapPending = PendingIntent.getActivity(
                this, 1, tapIntent,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);

        Notification notification = new Notification.Builder(this, CHANNEL_ID)
                .setContentTitle("Miru 观察已暂停")
                .setContentText("点击恢复屏幕观察")
                .setSmallIcon(R.mipmap.miru_launcher)
                .setColor(NOTIFICATION_COLOR)
                .setAutoCancel(true)
                .setContentIntent(tapPending)
                .build();

        NotificationManager nm = getSystemService(NotificationManager.class);
        nm.notify(NOTIFICATION_ID + 1, notification);
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    private void createNotificationChannel() {
        NotificationChannel channel = new NotificationChannel(
                CHANNEL_ID, "屏幕观察", NotificationManager.IMPORTANCE_LOW);
        channel.setDescription("Miru 屏幕观察服务");
        channel.setShowBadge(false);
        NotificationManager nm = getSystemService(NotificationManager.class);
        nm.createNotificationChannel(channel);
    }

    private Notification buildNotification() {
        // Stop action
        Intent stopIntent = new Intent(this, ScreenCaptureService.class);
        stopIntent.setAction("STOP");
        PendingIntent stopPending = PendingIntent.getService(
                this, 0, stopIntent,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);

        // Tap to open app
        Intent tapIntent = new Intent(this, MainActivity.class);
        tapIntent.setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent tapPending = PendingIntent.getActivity(
                this, 0, tapIntent,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);

        return new Notification.Builder(this, CHANNEL_ID)
                .setContentTitle("Miru 守护中")
                .setContentText("正在观察屏幕内容")
                .setSmallIcon(R.mipmap.miru_launcher)
                .setColor(NOTIFICATION_COLOR)
                .setOngoing(true)
                .setContentIntent(tapPending)
                .addAction(new Notification.Action.Builder(
                        null, "停止", stopPending).build())
                .build();
    }
}
