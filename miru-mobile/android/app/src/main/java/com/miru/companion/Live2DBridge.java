package com.miru.companion;

import android.content.res.AssetManager;
import java.io.InputStream;
import java.io.ByteArrayOutputStream;

/**
 * Java-side JNI bridge for Live2D Cubism native rendering.
 * Provides file loading from assets and receives native lifecycle calls.
 */
public class Live2DBridge {
    private static AssetManager sAssetManager;
    private static String sModelBasePath = "";

    static {
        System.loadLibrary("MiruLive2D");
    }

    public static void setAssetManager(AssetManager am) {
        sAssetManager = am;
    }

    public static void setModelBasePath(String path) {
        sModelBasePath = path;
    }

    /**
     * Called from native code to load a file as byte array.
     * First tries the model base path (external storage), then falls back to assets.
     */
    public static byte[] LoadFile(String filePath) {
        // Try loading from external model path first
        if (sModelBasePath != null && !sModelBasePath.isEmpty()) {
            try {
                java.io.File f = new java.io.File(sModelBasePath, filePath);
                if (f.exists()) {
                    java.io.FileInputStream fis = new java.io.FileInputStream(f);
                    byte[] data = readStream(fis);
                    fis.close();
                    return data;
                }
            } catch (Exception e) {
                // Fall through to assets
            }
        }

        // Fallback: load from APK assets
        if (sAssetManager == null) return null;
        try {
            InputStream is = sAssetManager.open(filePath);
            byte[] data = readStream(is);
            is.close();
            return data;
        } catch (Exception e) {
            e.printStackTrace();
            return null;
        }
    }

    private static byte[] readStream(InputStream is) throws Exception {
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        byte[] buf = new byte[8192];
        int len;
        while ((len = is.read(buf)) != -1) {
            bos.write(buf, 0, len);
        }
        return bos.toByteArray();
    }

    // --- Pending bubble text (Java thread-safe, consumed by wallpaper GL thread) ---
    private static volatile String sPendingBubble = null;

    public static void setPendingBubble(String text) {
        sPendingBubble = text;
    }

    public static boolean hasPendingBubble() {
        return sPendingBubble != null;
    }

    /** Returns and clears the pending bubble text. */
    public static String consumePendingBubble() {
        String text = sPendingBubble;
        sPendingBubble = null;
        return text;
    }

    // Native methods
    public static native void nativeOnStart();
    public static native void nativeOnPause();
    public static native void nativeOnStop();
    public static native void nativeOnDestroy();
    public static native void nativeOnSurfaceCreated();
    public static native void nativeOnSurfaceChanged(int width, int height);
    public static native void nativeOnDrawFrame();
    public static native void nativeOnTouchesBegan(float x, float y);
    public static native void nativeOnTouchesEnded(float x, float y);
    public static native void nativeOnTouchesMoved(float x, float y);

    // Bubble texture upload to native GL
    public static native void nativeSetBubbleTexture(byte[] rgba, int width, int height);
    public static native void nativeClearBubble();

    // Emotion state for visual effects (called from notification poll or SSE)
    public static native void nativeSetEmotionState(float valence, float arousal);
}
