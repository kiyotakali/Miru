/**
 * Wallpaper visual effects for Miru Live2D wallpaper.
 *
 * Layers (draw order):
 *   Pre-model:  Background gradient → Stars → Light rays → Glow
 *   [Live2D model]
 *   Post-model: Petals/Fireflies → Dust motes → Vignette → Bubble
 *
 * Time-aware: gradient, petal/firefly swap, star visibility, light rays at dawn/dusk.
 * Emotion-reactive: color temperature, glow brightness, particle speed from Miru backend.
 */
#pragma once
#include <GLES2/gl2.h>

class WallpaperEffects {
public:
    WallpaperEffects();
    ~WallpaperEffects();

    void Init(int width, int height);
    void Update(float deltaTime);
    void Cleanup();

    // --- Pre-model layers (behind character) ---
    void DrawBackground();
    void DrawStars();
    void DrawLightRays();
    void DrawGlow();

    // --- Post-model layers (in front of character) ---
    void DrawParticles();   // petals (day) + fireflies (night)
    void DrawDustMotes();
    void DrawVignette();

    // --- Bubble overlay ---
    void SetBubbleTexture(const unsigned char* rgba, int w, int h);
    void ClearBubble();
    void DrawBubble();

    // --- Emotion state from backend ---
    void SetEmotionState(float valence, float arousal);

private:
    int _width, _height;
    bool _inited;
    float _time;

    // Emotion (smoothed)
    float _valence;   // -1..1 (negative=sad, positive=happy)
    float _arousal;   // 0..1  (calm..excited)

    // ======== Shaders ========
    GLuint _gradProg;               // vertex-color (background)
    GLint  _gradPosLoc, _gradColLoc;

    GLuint _texProg;                // texture * color uniform (everything else)
    GLint  _texPosLoc, _texUvLoc, _texColLoc, _texSamplerLoc;

    // ======== Textures ========
    static const int NUM_PETAL_VARIANTS = 4;
    GLuint _petalTextures[NUM_PETAL_VARIANTS];  // Loaded from assets (128x128 RGBA)
    GLuint _glowTex;        // Radial gradient 64x64
    GLuint _dotTex;         // Gaussian dot 16x16 (dust, stars, fireflies)
    GLuint _vignetteTex;    // Radial vignette 64x64
    GLuint _bubbleTex;      // Chat bubble (uploaded from Java)

    // ======== Cherry blossom petals (day) ========
    static const int NUM_PETALS = 7;
    struct Petal {
        float x, y, vx, vy;
        float rot, rotV;
        float alpha, size;
        float aspect;   // width/height ratio (0.5~0.8 = elongated petal)
        float swayPhase, swayFreq, swayAmp;
        float flipPhase, flipFreq;  // 3D flip simulation
        int texVariant;  // which petal texture (0..3)
    };
    Petal _petals[NUM_PETALS];

    // ======== Fireflies (night) ========
    static const int NUM_FIREFLIES = 7;
    struct Firefly {
        float x, y, vx, vy;
        float glowPhase, glowFreq;
        float baseAlpha, size;
    };
    Firefly _fireflies[NUM_FIREFLIES];

    // ======== Dust motes (ambient, always) ========
    static const int NUM_DUST = 30;
    struct DustMote {
        float x, y, vx, vy;
        float alpha, size;
        float driftPhase;
    };
    DustMote _dust[NUM_DUST];

    // ======== Stars (night) — multi-layer for depth ========
    static const int NUM_STARS = 160;
    struct Star {
        float x, y;
        float twinklePhase, twinkleFreq;
        float baseAlpha, size;
        int layer;  // 0=distant dim, 1=mid, 2=bright feature stars
    };
    Star _stars[NUM_STARS];

    // ======== Light rays (dawn/dusk) ========
    static const int NUM_RAYS = 3;
    struct LightRay {
        float x, angle, width, length, alpha;
        float drift;
    };
    LightRay _rays[NUM_RAYS];

    // ======== Bubble state ========
    int   _bubbleW, _bubbleH;
    float _bubbleAlpha, _bubbleTimer;
    bool  _bubbleVisible;
    static constexpr float BUBBLE_FADE_IN  = 0.3f;
    static constexpr float BUBBLE_SHOW     = 8.0f;
    static constexpr float BUBBLE_FADE_OUT = 0.5f;

    // ======== Init helpers ========
    void initGradientShader();
    void initTextureShader();
    void loadPetalTextures();
    void initGlowTexture();
    void initDotTexture();
    void initVignetteTexture();

    // ======== Particle reset ========
    void resetPetal(Petal& p, bool anywhere);
    void resetFirefly(Firefly& f);
    void resetDustMote(DustMote& d, bool anywhere);
    void initStar(Star& s);
    void initLightRay(LightRay& r, int index);

    // ======== Time/color helpers ========
    float currentHour();
    void  getGradientColors(float top[4], float bot[4]);
    void  getPetalColor(float rgba[4]);
    float getNightFactor();     // 0=day, 1=night, smooth transition
    float getDawnDuskFactor();  // 0=off, 1=golden hour

    // ======== Draw helper ========
    void drawTexturedQuad(float cx, float cy, float hw, float hh,
                          float rot, const float color[4]);

    // ======== GL helpers ========
    GLuint compileShader(GLenum type, const char* src);
    GLuint linkProgram(GLuint vs, GLuint fs);
};
