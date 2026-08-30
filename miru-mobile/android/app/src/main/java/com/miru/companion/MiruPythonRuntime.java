package com.miru.companion;

import android.content.Context;
import android.util.Log;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicBoolean;

final class MiruPythonRuntime {
    private static final String TAG = "MiruPythonRuntime";
    private static final ExecutorService EXECUTOR = Executors.newSingleThreadExecutor();
    private static final AtomicBoolean STARTING = new AtomicBoolean(false);
    private static volatile boolean ready = false;
    private static volatile String launchUrl = "";
    private static volatile String lastError = "";

    interface StartCallback {
        void onReady(String url);
        void onError(String message);
    }

    private MiruPythonRuntime() {}

    static void start(Context context, StartCallback callback) {
        Context appContext = context.getApplicationContext();
        if (ready && !launchUrl.isEmpty()) {
            // The embedded backend survives Activity recreation (for example
            // while granting MediaProjection). Its active account may have
            // changed since the initial /login URL was cached, so always ask
            // the live Python runtime for the current destination.
            callback.onReady(getLaunchUrl());
            return;
        }
        if (!STARTING.compareAndSet(false, true)) {
            EXECUTOR.execute(() -> waitForStart(callback));
            return;
        }
        EXECUTOR.execute(() -> {
            try {
                if (!Python.isStarted()) {
                    Python.start(new AndroidPlatform(appContext));
                }
                PyObject module = Python.getInstance().getModule("miru_android_runtime");
                String clientSecret = MiruProfileStore.getOrCreateClientSecret(appContext);
                PyObject result = module.callAttr(
                    "start", appContext.getFilesDir().getAbsolutePath(), 5001, clientSecret);
                org.json.JSONObject state = new org.json.JSONObject(result.toString());
                launchUrl = state.optString("launch_url", "http://127.0.0.1:5001/login?android=1");
                lastError = "";
                ready = true;
                Log.i(TAG, "Embedded backend ready at " + state.optString("base_url"));
                callback.onReady(launchUrl);
            } catch (Throwable error) {
                ready = false;
                lastError = error.getMessage() == null ? error.toString() : error.getMessage();
                Log.e(TAG, "Embedded Python backend failed", error);
                callback.onError(lastError);
            } finally {
                STARTING.set(false);
            }
        });
    }

    private static void waitForStart(StartCallback callback) {
        for (int i = 0; i < 300; i++) {
            if (ready && !launchUrl.isEmpty()) {
                callback.onReady(getLaunchUrl());
                return;
            }
            if (!STARTING.get()) break;
            try { Thread.sleep(50); } catch (InterruptedException ignored) {
                Thread.currentThread().interrupt();
                break;
            }
        }
        callback.onError(lastError.isEmpty() ? "Miru local backend did not start" : lastError);
    }

    static boolean isReady() {
        return ready;
    }

    static String getLaunchUrl() {
        if (!ready || !Python.isStarted()) return launchUrl;
        try {
            PyObject module = Python.getInstance().getModule("miru_android_runtime");
            org.json.JSONObject state = new org.json.JSONObject(
                module.callAttr("get_state").toString());
            launchUrl = state.optString("launch_url", launchUrl);
        } catch (Throwable error) {
            Log.w(TAG, "Unable to refresh embedded launch URL", error);
        }
        return launchUrl;
    }

    static void clearClientSession() {
        if (!Python.isStarted()) return;
        try {
            PyObject module = Python.getInstance().getModule("miru_android_runtime");
            module.callAttr("clear_client_session");
            launchUrl = "http://127.0.0.1:5001/login?android=1";
            Log.i(TAG, "Embedded client session cleared");
        } catch (Throwable error) {
            Log.e(TAG, "Unable to clear embedded client session", error);
        }
    }
}
