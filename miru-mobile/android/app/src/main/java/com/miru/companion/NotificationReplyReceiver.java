package com.miru.companion;

import android.app.Notification;
import android.app.NotificationManager;
import android.app.RemoteInput;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.os.Bundle;
import android.util.Log;

import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;

/**
 * Handles inline reply from Miru notifications.
 * Posts the user's reply to the VPS chat endpoint, then updates
 * the notification and wallpaper bubble with Miru's response.
 */
public class NotificationReplyReceiver extends BroadcastReceiver {

    private static final String TAG = "MiruReply";
    public static final String KEY_REPLY = "reply_text";
    public static final String EXTRA_NOTIF_ID = "notification_id";
    private static final int NOTIFICATION_COLOR = 0xFF6B5448;

    @Override
    public void onReceive(Context context, Intent intent) {
        Bundle results = RemoteInput.getResultsFromIntent(intent);
        if (results == null) return;

        CharSequence replyCS = results.getCharSequence(KEY_REPLY);
        if (replyCS == null || replyCS.length() == 0) return;

        String replyText = replyCS.toString();
        int notifId = intent.getIntExtra(EXTRA_NOTIF_ID, 2001);

        Log.e(TAG, "Reply received: " + replyText);

        // Update notification to "sending..."
        updateNotification(context, notifId, "发送中...", null);

        // Network call in background
        PendingResult pending = goAsync();
        new Thread(() -> {
            try {
                String response = postReply(context, replyText);
                if (response != null) {
                    updateNotification(context, notifId, response, "Miru");
                    // Set wallpaper bubble with response
                    Live2DBridge.setPendingBubble(response);
                } else {
                    updateNotification(context, notifId, "发送失败，请稍后再试", null);
                }
            } catch (Exception e) {
                Log.e(TAG, "Reply error", e);
                updateNotification(context, notifId, "发送失败: " + e.getMessage(), null);
            } finally {
                pending.finish();
            }
        }).start();
    }

    private String postReply(Context context, String message) {
        SharedPreferences prefs = MiruProfileStore.globals(context);
        String serverUrl = prefs.getString("capture_server_url", "");
        String authToken = prefs.getString("capture_auth_token", "");
        String deviceId = prefs.getString("capture_device_id", "");

        if (serverUrl.isEmpty()) return null;

        try {
            JSONObject body = new JSONObject();
            body.put("message", message);
            body.put("device_id", deviceId);
            body.put("source", "notification_reply");

            URL url = new URL(serverUrl + "/api/chat");
            HttpURLConnection conn = (HttpURLConnection) url.openConnection();
            conn.setRequestMethod("POST");
            conn.setRequestProperty("Content-Type", "application/json");
            if (!authToken.isEmpty()) {
                conn.setRequestProperty("Authorization", "Bearer " + authToken);
            }
            conn.setConnectTimeout(15_000);
            conn.setReadTimeout(30_000);
            conn.setDoOutput(true);

            byte[] payload = body.toString().getBytes(StandardCharsets.UTF_8);
            OutputStream os = conn.getOutputStream();
            os.write(payload);
            os.close();

            int code = conn.getResponseCode();
            if (code != 200) {
                Log.e(TAG, "Chat API returned " + code);
                conn.disconnect();
                return null;
            }

            BufferedReader reader = new BufferedReader(
                    new InputStreamReader(conn.getInputStream()));
            StringBuilder sb = new StringBuilder();
            String line;
            while ((line = reader.readLine()) != null) sb.append(line);
            reader.close();
            conn.disconnect();

            JSONObject resp = new JSONObject(sb.toString());
            return resp.optString("reply", resp.optString("text", ""));

        } catch (Exception e) {
            Log.e(TAG, "POST error", e);
            return null;
        }
    }

    private void updateNotification(Context context, int notifId, String text, String title) {
        Notification.Builder builder = new Notification.Builder(context, "miru_messages")
                .setContentTitle(title != null ? title : "Miru")
                .setContentText(text)
                .setStyle(new Notification.BigTextStyle().bigText(text))
                .setSmallIcon(R.mipmap.miru_launcher)
                .setLargeIcon(getLauncherLargeIcon(context))
                .setColor(NOTIFICATION_COLOR)
                .setAutoCancel(true);

        NotificationManager nm = context.getSystemService(NotificationManager.class);
        nm.notify(notifId, builder.build());
    }

    private Bitmap getLauncherLargeIcon(Context context) {
        return BitmapFactory.decodeResource(context.getResources(), R.mipmap.miru_launcher_foreground);
    }
}
