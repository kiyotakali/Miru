package com.miru.companion;

import android.opengl.EGL14;
import android.opengl.EGLConfig;
import android.opengl.EGLContext;
import android.opengl.EGLDisplay;
import android.opengl.EGLSurface;
import android.opengl.GLES20;
import android.service.wallpaper.WallpaperService;
import android.util.Log;
import android.view.Choreographer;
import android.view.MotionEvent;
import android.view.SurfaceHolder;

/**
 * Live2D wallpaper service for lock screen and home screen.
 *
 * Uses OpenGL ES 2.0 via EGL to render Cubism models through the native
 * JNI bridge. Choreographer-based frame scheduling at 60 FPS.
 *
 * EGL display is shared process-wide and never terminated to avoid
 * invalidating other engines' contexts during preview->actual transitions.
 */
public class MiruWallpaperService extends WallpaperService {
    private static final String TAG = "MiruWallpaper";

    // Shared EGL display and config — initialized once, never terminated.
    private static EGLDisplay sDisplay = EGL14.EGL_NO_DISPLAY;
    private static EGLConfig sConfig;
    private static int sEngineCount = 0;

    // Track which engine currently owns the native Cubism state.
    private static Live2DEngine sActiveEngine = null;

    private static synchronized EGLDisplay getSharedDisplay() {
        if (sDisplay == EGL14.EGL_NO_DISPLAY) {
            sDisplay = EGL14.eglGetDisplay(EGL14.EGL_DEFAULT_DISPLAY);
            if (sDisplay == EGL14.EGL_NO_DISPLAY) {
                Log.e(TAG, "eglGetDisplay failed");
                return EGL14.EGL_NO_DISPLAY;
            }
            int[] version = new int[2];
            if (!EGL14.eglInitialize(sDisplay, version, 0, version, 1)) {
                Log.e(TAG, "eglInitialize failed");
                sDisplay = EGL14.EGL_NO_DISPLAY;
                return EGL14.EGL_NO_DISPLAY;
            }

            int[] configAttribs = {
                EGL14.EGL_RENDERABLE_TYPE, EGL14.EGL_OPENGL_ES2_BIT,
                EGL14.EGL_RED_SIZE, 8,
                EGL14.EGL_GREEN_SIZE, 8,
                EGL14.EGL_BLUE_SIZE, 8,
                EGL14.EGL_ALPHA_SIZE, 0,
                EGL14.EGL_DEPTH_SIZE, 0,
                EGL14.EGL_STENCIL_SIZE, 0,
                EGL14.EGL_NONE
            };
            EGLConfig[] configs = new EGLConfig[1];
            int[] numConfigs = new int[1];
            EGL14.eglChooseConfig(sDisplay, configAttribs, 0, configs, 0, 1, numConfigs, 0);
            if (numConfigs[0] == 0) {
                Log.e(TAG, "eglChooseConfig found no configs");
                sDisplay = EGL14.EGL_NO_DISPLAY;
                return EGL14.EGL_NO_DISPLAY;
            }
            sConfig = configs[0];
            Log.i(TAG, "Shared EGL display initialized");
        }
        return sDisplay;
    }

    @Override
    public Engine onCreateEngine() {
        return new Live2DEngine();
    }

    class Live2DEngine extends Engine implements Choreographer.FrameCallback {
        private EGLContext eglContext = EGL14.EGL_NO_CONTEXT;
        private EGLSurface eglSurface = EGL14.EGL_NO_SURFACE;

        private Choreographer choreographer;
        private boolean visible = false;
        private boolean initialized = false;
        private boolean eglReady = false;
        private int surfaceWidth = 0;
        private int surfaceHeight = 0;

        // 60 FPS (~16ms per frame)
        private static final long FRAME_INTERVAL_NS = 16_666_666L;
        private long lastFrameTimeNs = 0;

        @Override
        public void onCreate(SurfaceHolder surfaceHolder) {
            super.onCreate(surfaceHolder);
            setTouchEventsEnabled(true);
            choreographer = Choreographer.getInstance();
            synchronized (MiruWallpaperService.class) {
                sEngineCount++;
            }
            Live2DBridge.setAssetManager(getAssets());
        }

        @Override
        public void onSurfaceCreated(SurfaceHolder holder) {
            super.onSurfaceCreated(holder);
        }

        @Override
        public void onSurfaceChanged(SurfaceHolder holder, int format, int width, int height) {
            super.onSurfaceChanged(holder, format, width, height);
            surfaceWidth = width;
            surfaceHeight = height;

            if (!eglReady) {
                if (!initEGL(holder)) return;
                eglReady = true;
            }

            if (!initialized) {
                if (!makeCurrent()) return;

                // Tear down previous engine's native state — GL resources
                // from another EGL context are invalid in ours.
                if (sActiveEngine != null && sActiveEngine != this) {
                    try {
                        Live2DBridge.nativeOnStop();
                        Live2DBridge.nativeOnDestroy();
                    } catch (Exception e) {
                        Log.e(TAG, "Error tearing down previous native", e);
                    }
                }

                sActiveEngine = this;
                Live2DBridge.nativeOnStart();
                Live2DBridge.nativeOnSurfaceCreated();
                Live2DBridge.nativeOnSurfaceChanged(width, height);
                initialized = true;

                if (visible) {
                    lastFrameTimeNs = System.nanoTime();
                    choreographer.postFrameCallback(this);
                }
            } else {
                if (makeCurrent()) {
                    Live2DBridge.nativeOnSurfaceChanged(width, height);
                }
            }
        }

        /**
         * Called when sActiveEngine becomes null (previous engine destroyed).
         * If this engine has a surface but never initialized native, do it now.
         */
        private void tryDeferredInit() {
            if (initialized || !eglReady || surfaceWidth == 0) return;
            if (sActiveEngine != null) return;
            if (!makeCurrent()) return;

            sActiveEngine = this;
            Live2DBridge.nativeOnStart();
            Live2DBridge.nativeOnSurfaceCreated();
            Live2DBridge.nativeOnSurfaceChanged(surfaceWidth, surfaceHeight);
            initialized = true;

            if (visible) {
                lastFrameTimeNs = System.nanoTime();
                choreographer.postFrameCallback(this);
            }
        }

        @Override
        public void onVisibilityChanged(boolean visible) {
            super.onVisibilityChanged(visible);
            this.visible = visible;

            if (visible) {
                // If we were waiting for the previous engine to die, try now.
                if (!initialized) tryDeferredInit();
                if (initialized) {
                    lastFrameTimeNs = System.nanoTime();
                    choreographer.postFrameCallback(this);
                }
            }
        }

        @Override
        public void doFrame(long frameTimeNanos) {
            if (!visible) return;
            choreographer.postFrameCallback(this);

            // Detect ownership loss: another engine took over native state
            // (e.g. preview engine). Reset so we can re-acquire later.
            if (initialized && sActiveEngine != this) {
                initialized = false;
            }

            if (!initialized) {
                tryDeferredInit();
                if (!initialized) return;
            }

            if (frameTimeNanos - lastFrameTimeNs < FRAME_INTERVAL_NS) {
                return;
            }
            lastFrameTimeNs = frameTimeNanos;

            if (makeCurrent()) {
                Live2DBridge.nativeOnDrawFrame();
                if (!EGL14.eglSwapBuffers(sDisplay, eglSurface)) {
                    Log.e(TAG, "eglSwapBuffers failed: 0x" + Integer.toHexString(EGL14.eglGetError()));
                }
            }
        }

        @Override
        public void onTouchEvent(MotionEvent event) {
            if (!initialized || sActiveEngine != this) return;
            float x = event.getX();
            float y = event.getY();

            switch (event.getAction()) {
                case MotionEvent.ACTION_DOWN:
                    Live2DBridge.nativeOnTouchesBegan(x, y);
                    break;
                case MotionEvent.ACTION_MOVE:
                    Live2DBridge.nativeOnTouchesMoved(x, y);
                    break;
                case MotionEvent.ACTION_UP:
                case MotionEvent.ACTION_CANCEL:
                    Live2DBridge.nativeOnTouchesEnded(x, y);
                    break;
            }
        }

        @Override
        public void onSurfaceDestroyed(SurfaceHolder holder) {
            super.onSurfaceDestroyed(holder);
            visible = false;

            if (initialized && sActiveEngine == this) {
                if (makeCurrent()) {
                    Live2DBridge.nativeOnStop();
                    Live2DBridge.nativeOnDestroy();
                }
                sActiveEngine = null;
            }
            initialized = false;
            eglReady = false;
            destroyEGL();

            synchronized (MiruWallpaperService.class) {
                sEngineCount--;
            }
        }

        @Override
        public void onDestroy() {
            super.onDestroy();
        }

        // ===== EGL (per-engine context + surface, shared display) =====

        private boolean initEGL(SurfaceHolder holder) {
            EGLDisplay display = getSharedDisplay();
            if (display == EGL14.EGL_NO_DISPLAY) return false;

            int[] contextAttribs = {
                EGL14.EGL_CONTEXT_CLIENT_VERSION, 2,
                EGL14.EGL_NONE
            };
            eglContext = EGL14.eglCreateContext(display, sConfig, EGL14.EGL_NO_CONTEXT, contextAttribs, 0);
            if (eglContext == EGL14.EGL_NO_CONTEXT) {
                Log.e(TAG, "eglCreateContext failed: 0x" + Integer.toHexString(EGL14.eglGetError()));
                return false;
            }

            eglSurface = EGL14.eglCreateWindowSurface(display, sConfig, holder.getSurface(), new int[]{EGL14.EGL_NONE}, 0);
            if (eglSurface == EGL14.EGL_NO_SURFACE) {
                Log.e(TAG, "eglCreateWindowSurface failed: 0x" + Integer.toHexString(EGL14.eglGetError()));
                return false;
            }

            Log.e(TAG, "EGL context+surface created OK: ctx=" + eglContext + " srf=" + eglSurface);
            return true;
        }

        private boolean makeCurrent() {
            if (sDisplay == EGL14.EGL_NO_DISPLAY || eglSurface == EGL14.EGL_NO_SURFACE) {
                return false;
            }
            return EGL14.eglMakeCurrent(sDisplay, eglSurface, eglSurface, eglContext);
        }

        private void destroyEGL() {
            if (sDisplay != EGL14.EGL_NO_DISPLAY) {
                if (EGL14.eglGetCurrentContext() == eglContext) {
                    EGL14.eglMakeCurrent(sDisplay, EGL14.EGL_NO_SURFACE, EGL14.EGL_NO_SURFACE, EGL14.EGL_NO_CONTEXT);
                }
                if (eglSurface != EGL14.EGL_NO_SURFACE) {
                    EGL14.eglDestroySurface(sDisplay, eglSurface);
                }
                if (eglContext != EGL14.EGL_NO_CONTEXT) {
                    EGL14.eglDestroyContext(sDisplay, eglContext);
                }
            }
            eglContext = EGL14.EGL_NO_CONTEXT;
            eglSurface = EGL14.EGL_NO_SURFACE;
        }
    }
}
