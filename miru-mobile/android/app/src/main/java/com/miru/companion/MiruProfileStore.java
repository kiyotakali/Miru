package com.miru.companion;

import android.content.Context;
import android.content.SharedPreferences;

import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.SecureRandom;
import android.util.Base64;
import java.util.Locale;
import java.util.UUID;

/** Android-owned session and per-account device settings. */
final class MiruProfileStore {
    static final String GLOBAL_PREFS = "miru_prefs";
    private static final String PROFILE_PREFS = "miru_profile_prefs";
    private static final String ACTIVE_PROFILE = "active_profile_key";

    private MiruProfileStore() {}

    static SharedPreferences globals(Context context) {
        return context.getSharedPreferences(GLOBAL_PREFS, Context.MODE_PRIVATE);
    }

    private static SharedPreferences profiles(Context context) {
        return context.getSharedPreferences(PROFILE_PREFS, Context.MODE_PRIVATE);
    }

    static String getOrCreateInstallDeviceId(Context context) {
        SharedPreferences prefs = globals(context);
        String existing = prefs.getString("install_device_id", "");
        if (!existing.isEmpty()) return existing;
        String created = "dev_" + UUID.randomUUID().toString().replace("-", "").substring(0, 12);
        if (!prefs.edit().putString("install_device_id", created).commit()) {
            throw new IllegalStateException("Unable to persist Android device id");
        }
        return created;
    }

    static String getOrCreateClientSecret(Context context) {
        SharedPreferences prefs = globals(context);
        String existing = prefs.getString("android_client_secret", "");
        if (!existing.isEmpty()) return existing;
        byte[] bytes = new byte[32];
        new SecureRandom().nextBytes(bytes);
        String created = Base64.encodeToString(bytes, Base64.NO_WRAP | Base64.URL_SAFE);
        if (!prefs.edit().putString("android_client_secret", created).commit()) {
            throw new IllegalStateException("Unable to persist Android client secret");
        }
        return created;
    }

    static String canonicalOrigin(String serverUrl) {
        String value = serverUrl == null ? "" : serverUrl.trim();
        if (value.isEmpty()) return "";
        try {
            URI uri = new URI(value);
            String scheme = uri.getScheme() == null ? "http" : uri.getScheme().toLowerCase(Locale.ROOT);
            String host = uri.getHost();
            if (host == null || host.isEmpty()) {
                return value.replaceAll("/+$", "").toLowerCase(Locale.ROOT);
            }
            host = host.toLowerCase(Locale.ROOT);
            int port = uri.getPort();
            boolean defaultPort = (port == 80 && "http".equals(scheme))
                    || (port == 443 && "https".equals(scheme));
            return scheme + "://" + host + ((port >= 0 && !defaultPort) ? ":" + port : "");
        } catch (Exception ignored) {
            return value.replaceAll("/+$", "").toLowerCase(Locale.ROOT);
        }
    }

    static String profileKey(String mode, String serverUrl, String userId) {
        String normalizedMode = "local".equalsIgnoreCase(mode) ? "local" : "remote";
        String identity = normalizedMode + "|" + canonicalOrigin(serverUrl) + "|"
                + (userId == null ? "" : userId.trim());
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256")
                    .digest(identity.getBytes(StandardCharsets.UTF_8));
            StringBuilder out = new StringBuilder("profile_");
            for (int i = 0; i < 12; i++) {
                out.append(String.format(Locale.ROOT, "%02x", digest[i]));
            }
            return out.toString();
        } catch (Exception ignored) {
            return "profile_" + Integer.toUnsignedString(identity.hashCode(), 16);
        }
    }

    static String activeProfileKey(Context context) {
        return globals(context).getString(ACTIVE_PROFILE, "");
    }

    static void activateProfile(Context context, String mode, String serverUrl,
                                String userId, String token, String invitationCode) {
        String key = profileKey(mode, serverUrl, userId);
        SharedPreferences global = globals(context);
        SharedPreferences perProfile = profiles(context);

        // Preserve existing users' settings on the first profile-aware run.
        if (!perProfile.contains(key + ".capture_interval_s")) {
            perProfile.edit()
                    .putInt(key + ".capture_interval_s", global.getInt("capture_interval_s", 30))
                    .putBoolean(key + ".screen_capture_enabled",
                            global.getBoolean("screen_capture_enabled", false))
                    .apply();
        }

        boolean ok = global.edit()
                .putString(ACTIVE_PROFILE, key)
                .putString("active_profile_mode", "local".equalsIgnoreCase(mode) ? "local" : "remote")
                .putString("active_profile_user_id", userId == null ? "" : userId)
                .putString("miru_server_url", serverUrl == null ? "" : serverUrl)
                .putString("miru_auth_token", token == null ? "" : token)
                .putString("miru_invitation_code", invitationCode == null ? "" : invitationCode)
                .commit();
        if (!ok) throw new IllegalStateException("Unable to persist active Miru profile");
    }

    static void updateActiveTransport(Context context, String serverUrl, String token, String deviceId) {
        globals(context).edit()
                .putString("capture_server_url", serverUrl == null ? "" : serverUrl)
                .putString("capture_auth_token", token == null ? "" : token)
                .putString("capture_device_id", deviceId == null ? "" : deviceId)
                .apply();
    }

    static boolean isCaptureEnabled(Context context) {
        String key = activeProfileKey(context);
        return !key.isEmpty()
                && profiles(context).getBoolean(key + ".screen_capture_enabled", false);
    }

    static void setCaptureEnabled(Context context, boolean enabled) {
        String key = activeProfileKey(context);
        if (!key.isEmpty()) {
            profiles(context).edit().putBoolean(key + ".screen_capture_enabled", enabled).apply();
        }
    }

    static int getCaptureInterval(Context context) {
        String key = activeProfileKey(context);
        return key.isEmpty() ? 30 : profiles(context).getInt(key + ".capture_interval_s", 30);
    }

    static void setCaptureInterval(Context context, int seconds) {
        String key = activeProfileKey(context);
        if (!key.isEmpty()) {
            profiles(context).edit().putInt(key + ".capture_interval_s", seconds).apply();
        }
    }

    static boolean clearActiveSession(Context context) {
        return globals(context).edit()
                .remove(ACTIVE_PROFILE)
                .remove("active_profile_mode")
                .remove("active_profile_user_id")
                .remove("miru_server_url")
                .remove("miru_auth_token")
                .remove("miru_invitation_code")
                .remove("capture_server_url")
                .remove("capture_auth_token")
                .remove("capture_device_id")
                .remove("screen_capture_enabled")
                .remove("capture_interval_s")
                .commit();
    }
}
