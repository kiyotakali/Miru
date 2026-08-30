/**
 * Miru wallpaper visual effects — full implementation.
 *
 * A: Breathing glow, vignette, ambient dust motes
 * B: Night stars, fireflies (replace petals at night)
 * C: Emotion-reactive color shift, dawn/dusk light rays
 * + Improved cherry blossom petal texture
 */
#include "WallpaperEffects.hpp"
#include "JniBridgeC.hpp"
#include <cmath>
#include <cstdlib>
#include <ctime>
#include <cstring>
#include <android/log.h>

#define FXTAG "WallpaperFX"
#define LOGE(...) __android_log_print(ANDROID_LOG_ERROR, FXTAG, __VA_ARGS__)

// ================================================================
//  Helpers
// ================================================================

static float randf() { return static_cast<float>(rand()) / static_cast<float>(RAND_MAX); }
static float randf(float lo, float hi) { return lo + randf() * (hi - lo); }

static float smoothstep(float e0, float e1, float x) {
    float t = (x - e0) / (e1 - e0);
    if (t < 0.f) t = 0.f;
    if (t > 1.f) t = 1.f;
    return t * t * (3.f - 2.f * t);
}

static float lerp(float a, float b, float t) { return a + (b - a) * t; }

static float clamp01(float x) { return x < 0.f ? 0.f : (x > 1.f ? 1.f : x); }

static void lerpColor(const float a[4], const float b[4], float t, float out[4]) {
    for (int i = 0; i < 4; i++) out[i] = lerp(a[i], b[i], t);
}

// ================================================================
//  Constructor / Destructor
// ================================================================

WallpaperEffects::WallpaperEffects()
    : _width(0), _height(0), _inited(false), _time(0.f)
    , _valence(0.f), _arousal(0.2f)
    , _gradProg(0), _gradPosLoc(-1), _gradColLoc(-1)
    , _texProg(0), _texPosLoc(-1), _texUvLoc(-1), _texColLoc(-1), _texSamplerLoc(-1)
    , _glowTex(0), _dotTex(0), _vignetteTex(0), _bubbleTex(0)
    , _bubbleW(0), _bubbleH(0), _bubbleAlpha(0.f), _bubbleTimer(0.f), _bubbleVisible(false)
{
    memset(_petalTextures, 0, sizeof(_petalTextures));
}

WallpaperEffects::~WallpaperEffects() {
    Cleanup();
}

// ================================================================
//  Shader helpers
// ================================================================

GLuint WallpaperEffects::compileShader(GLenum type, const char* src) {
    GLuint s = glCreateShader(type);
    glShaderSource(s, 1, &src, nullptr);
    glCompileShader(s);
    GLint ok = 0;
    glGetShaderiv(s, GL_COMPILE_STATUS, &ok);
    if (!ok) {
        char log[512];
        glGetShaderInfoLog(s, sizeof(log), nullptr, log);
        LOGE("Shader compile: %s", log);
    }
    return s;
}

GLuint WallpaperEffects::linkProgram(GLuint vs, GLuint fs) {
    GLuint p = glCreateProgram();
    glAttachShader(p, vs);
    glAttachShader(p, fs);
    glLinkProgram(p);
    glDeleteShader(vs);
    glDeleteShader(fs);
    GLint ok = 0;
    glGetProgramiv(p, GL_LINK_STATUS, &ok);
    if (!ok) {
        char log[512];
        glGetProgramInfoLog(p, sizeof(log), nullptr, log);
        LOGE("Program link: %s", log);
    }
    return p;
}

// ================================================================
//  Init / Cleanup
// ================================================================

void WallpaperEffects::Init(int width, int height) {
    if (_inited) Cleanup();

    _width = width;
    _height = height;
    _time = 0.f;

    // Date-based seed: stars stay in same positions across reinits within a day
    time_t now = time(nullptr);
    struct tm* local = localtime(&now);
    unsigned int dateSeed = local->tm_year * 10000 + local->tm_yday;
    srand(dateSeed);

    // Shaders
    initGradientShader();
    initTextureShader();

    // Textures
    loadPetalTextures();
    initGlowTexture();
    initDotTexture();
    initVignetteTexture();

    // Particles
    for (int i = 0; i < NUM_PETALS; i++)    resetPetal(_petals[i], true);
    for (int i = 0; i < NUM_FIREFLIES; i++) resetFirefly(_fireflies[i]);
    for (int i = 0; i < NUM_DUST; i++)      resetDustMote(_dust[i], true);
    for (int i = 0; i < NUM_STARS; i++)     initStar(_stars[i]);
    for (int i = 0; i < NUM_RAYS; i++)      initLightRay(_rays[i], i);

    _inited = true;
    LOGE("Init %dx%d", width, height);
}

void WallpaperEffects::Cleanup() {
    if (_gradProg)    { glDeleteProgram(_gradProg);    _gradProg = 0; }
    if (_texProg)     { glDeleteProgram(_texProg);     _texProg = 0; }
    for (int i = 0; i < NUM_PETAL_VARIANTS; i++) {
        if (_petalTextures[i]) { glDeleteTextures(1, &_petalTextures[i]); _petalTextures[i] = 0; }
    }
    if (_glowTex)     { glDeleteTextures(1, &_glowTex);     _glowTex = 0; }
    if (_dotTex)      { glDeleteTextures(1, &_dotTex);      _dotTex = 0; }
    if (_vignetteTex) { glDeleteTextures(1, &_vignetteTex); _vignetteTex = 0; }
    if (_bubbleTex)   { glDeleteTextures(1, &_bubbleTex);   _bubbleTex = 0; }
    _bubbleVisible = false;
    _inited = false;
}

// ================================================================
//  Shader init
// ================================================================

void WallpaperEffects::initGradientShader() {
    const char* vs =
        "attribute vec2 aPos;\n"
        "attribute vec4 aCol;\n"
        "varying vec4 vCol;\n"
        "void main() {\n"
        "  gl_Position = vec4(aPos, 0.0, 1.0);\n"
        "  vCol = aCol;\n"
        "}\n";
    const char* fs =
        "precision mediump float;\n"
        "varying vec4 vCol;\n"
        "void main() {\n"
        "  gl_FragColor = vCol;\n"
        "}\n";
    _gradProg = linkProgram(
        compileShader(GL_VERTEX_SHADER, vs),
        compileShader(GL_FRAGMENT_SHADER, fs));
    _gradPosLoc = glGetAttribLocation(_gradProg, "aPos");
    _gradColLoc = glGetAttribLocation(_gradProg, "aCol");
}

void WallpaperEffects::initTextureShader() {
    const char* vs =
        "attribute vec2 aPos;\n"
        "attribute vec2 aUV;\n"
        "varying vec2 vUV;\n"
        "void main() {\n"
        "  gl_Position = vec4(aPos, 0.0, 1.0);\n"
        "  vUV = aUV;\n"
        "}\n";
    const char* fs =
        "precision mediump float;\n"
        "varying vec2 vUV;\n"
        "uniform vec4 uColor;\n"
        "uniform sampler2D uTex;\n"
        "void main() {\n"
        "  gl_FragColor = texture2D(uTex, vUV) * uColor;\n"
        "}\n";
    _texProg = linkProgram(
        compileShader(GL_VERTEX_SHADER, vs),
        compileShader(GL_FRAGMENT_SHADER, fs));
    _texPosLoc     = glGetAttribLocation(_texProg, "aPos");
    _texUvLoc      = glGetAttribLocation(_texProg, "aUV");
    _texColLoc     = glGetUniformLocation(_texProg, "uColor");
    _texSamplerLoc = glGetUniformLocation(_texProg, "uTex");
}

// ================================================================
//  Texture generation
// ================================================================

void WallpaperEffects::loadPetalTextures() {
    // Load 4 petal variant textures from assets (128x128 raw RGBA)
    const int TEX_SIZE = 128;
    const unsigned int EXPECTED = TEX_SIZE * TEX_SIZE * 4;
    const char* files[NUM_PETAL_VARIANTS] = {
        "effects/petal_0.rgba",
        "effects/petal_1.rgba",
        "effects/petal_2.rgba",
        "effects/petal_3.rgba",
    };

    glGenTextures(NUM_PETAL_VARIANTS, _petalTextures);

    for (int i = 0; i < NUM_PETAL_VARIANTS; i++) {
        unsigned int size = 0;
        char* data = JniBridgeC::LoadFileAsBytesFromJava(files[i], &size);

        if (data && size == EXPECTED) {
            glBindTexture(GL_TEXTURE_2D, _petalTextures[i]);
            glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, TEX_SIZE, TEX_SIZE, 0,
                         GL_RGBA, GL_UNSIGNED_BYTE, data);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
            LOGE("Loaded petal texture %d (%u bytes)", i, size);
        } else {
            LOGE("Failed to load petal texture %s (got %u bytes, expected %u)",
                 files[i], size, EXPECTED);
            _petalTextures[i] = 0;
        }

        delete[] data;
    }
}

void WallpaperEffects::initGlowTexture() {
    // 64x64 radial gradient for character glow
    const int S = 64;
    unsigned char pixels[S * S * 4];

    for (int y = 0; y < S; y++) {
        for (int x = 0; x < S; x++) {
            float fx = (static_cast<float>(x) / (S - 1)) * 2.f - 1.f;
            float fy = (static_cast<float>(y) / (S - 1)) * 2.f - 1.f;
            float dist = sqrtf(fx * fx + fy * fy);

            // Gaussian-like falloff
            float alpha = expf(-dist * dist * 3.0f);
            alpha = clamp01(alpha);

            int idx = (y * S + x) * 4;
            pixels[idx + 0] = 255;
            pixels[idx + 1] = 255;
            pixels[idx + 2] = 255;
            pixels[idx + 3] = static_cast<unsigned char>(alpha * 255.f);
        }
    }

    glGenTextures(1, &_glowTex);
    glBindTexture(GL_TEXTURE_2D, _glowTex);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, S, S, 0, GL_RGBA, GL_UNSIGNED_BYTE, pixels);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
}

void WallpaperEffects::initDotTexture() {
    // 16x16 soft gaussian dot for dust motes, stars, fireflies
    const int S = 16;
    unsigned char pixels[S * S * 4];

    for (int y = 0; y < S; y++) {
        for (int x = 0; x < S; x++) {
            float fx = (static_cast<float>(x) / (S - 1)) * 2.f - 1.f;
            float fy = (static_cast<float>(y) / (S - 1)) * 2.f - 1.f;
            float dist = sqrtf(fx * fx + fy * fy);

            float alpha = expf(-dist * dist * 4.5f);
            alpha = clamp01(alpha);

            int idx = (y * S + x) * 4;
            pixels[idx + 0] = 255;
            pixels[idx + 1] = 255;
            pixels[idx + 2] = 255;
            pixels[idx + 3] = static_cast<unsigned char>(alpha * 255.f);
        }
    }

    glGenTextures(1, &_dotTex);
    glBindTexture(GL_TEXTURE_2D, _dotTex);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, S, S, 0, GL_RGBA, GL_UNSIGNED_BYTE, pixels);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
}

void WallpaperEffects::initVignetteTexture() {
    // 64x64 radial: transparent center, opaque edges
    const int S = 64;
    unsigned char pixels[S * S * 4];

    for (int y = 0; y < S; y++) {
        for (int x = 0; x < S; x++) {
            float fx = (static_cast<float>(x) / (S - 1)) * 2.f - 1.f;
            float fy = (static_cast<float>(y) / (S - 1)) * 2.f - 1.f;
            float dist = sqrtf(fx * fx + fy * fy);

            // Vignette: transparent in center, dark at edges
            float alpha = smoothstep(0.5f, 1.35f, dist);
            alpha = clamp01(alpha);

            int idx = (y * S + x) * 4;
            pixels[idx + 0] = 255;
            pixels[idx + 1] = 255;
            pixels[idx + 2] = 255;
            pixels[idx + 3] = static_cast<unsigned char>(alpha * 255.f);
        }
    }

    glGenTextures(1, &_vignetteTex);
    glBindTexture(GL_TEXTURE_2D, _vignetteTex);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, S, S, 0, GL_RGBA, GL_UNSIGNED_BYTE, pixels);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
}

// ================================================================
//  Particle reset
// ================================================================

void WallpaperEffects::resetPetal(Petal& p, bool anywhere) {
    // Exact desktop CSS petal parameters, scaled for mobile (≈3x CSS pixel sizes)
    // Desktop: 7 petals, 11-26px in ~375px window → mobile: 35-75px in ~1080px screen
    // CSS uses fixed positions + keyframe animation, we replicate the same approach
    //
    // swayFreq = animation speed (1/duration), swayPhase = phase offset (0-1)
    // x = fixed horizontal position, y/rot/scale computed from animation progress

    static const struct {
        float leftPct;   // CSS left %
        float w, h;      // CSS size in px (will be scaled 3x)
        float duration;  // CSS animation-duration in seconds
        float delay;     // CSS animation-delay in seconds
    } CSS_PETALS[7] = {
        { 0.08f, 22, 18, 12.f, 0.0f },
        { 0.28f, 16, 14, 15.f, 2.5f },
        { 0.52f, 26, 20, 10.f, 1.0f },
        { 0.72f, 14, 12, 14.f, 4.0f },
        { 0.90f, 20, 16, 11.f, 6.0f },
        { 0.42f, 13, 11, 13.f, 8.0f },
        { 0.65f, 24, 18, 16.f, 3.0f },
    };

    // Use petal index (encoded in 'anywhere' for init, otherwise cycle)
    // For reset after falling off screen, pick a random CSS preset
    int idx = anywhere ? (&p - _petals) % 7 : (rand() % 7);
    const auto& css = CSS_PETALS[idx];

    // Position: CSS left% → GL x coordinate [-1, 1]
    p.x = -1.f + css.leftPct * 2.f;
    p.y = 0.f;  // Not used directly — computed from progress in Draw

    // Size: scale CSS px by 3x for mobile resolution
    p.size = css.w * 3.f;
    p.aspect = css.h / css.w;  // Preserve CSS width/height ratio
    p.alpha = 0.85f;           // CSS peak opacity

    // Animation timing
    p.swayFreq = 1.f / css.duration;   // animation speed
    p.swayPhase = css.delay / css.duration;  // initial phase offset

    // Unused in new parametric system but keep valid
    p.vy = 0; p.vx = 0; p.rot = 0; p.rotV = 0;
    p.swayAmp = 0; p.flipPhase = 0; p.flipFreq = 0;

    p.texVariant = rand() % NUM_PETAL_VARIANTS;
}

void WallpaperEffects::resetFirefly(Firefly& f) {
    f.x = randf(-0.8f, 0.8f);
    f.y = randf(-0.6f, 0.8f);
    f.vx = randf(-0.02f, 0.02f);
    f.vy = randf(-0.015f, 0.015f);
    f.glowPhase = randf(0.f, 6.2832f);
    f.glowFreq = randf(0.4f, 0.9f);
    f.baseAlpha = randf(0.4f, 0.8f);
    f.size = randf(8.f, 16.f);
}

void WallpaperEffects::resetDustMote(DustMote& d, bool anywhere) {
    d.x = randf(-1.1f, 1.1f);
    d.y = anywhere ? randf(-1.1f, 1.1f) : randf(1.05f, 1.3f);
    d.vx = randf(-0.005f, 0.005f);
    d.vy = randf(-0.003f, 0.003f);
    d.alpha = randf(0.06f, 0.2f);
    d.size = randf(3.f, 7.f);
    d.driftPhase = randf(0.f, 6.2832f);
}

void WallpaperEffects::initStar(Star& s) {
    s.x = randf(-0.98f, 0.98f);
    s.y = randf(-0.5f, 0.98f);  // Full screen except very bottom
    s.twinklePhase = randf(0.f, 6.2832f);

    // Determine layer: 55% distant, 30% mid, 15% bright feature
    float roll = randf();
    if (roll < 0.55f) {
        s.layer = 0;  // Distant: small but clearly visible
        s.baseAlpha = randf(0.3f, 0.6f);
        s.size = randf(2.5f, 5.0f);
        s.twinkleFreq = randf(0.15f, 0.5f);
    } else if (roll < 0.85f) {
        s.layer = 1;  // Mid: prominent, moderate twinkle
        s.baseAlpha = randf(0.5f, 0.8f);
        s.size = randf(5.0f, 8.0f);
        s.twinkleFreq = randf(0.3f, 0.8f);
    } else {
        s.layer = 2;  // Feature: bright, eye-catching, slow breathe
        s.baseAlpha = randf(0.8f, 1.0f);
        s.size = randf(8.0f, 14.f);
        s.twinkleFreq = randf(0.1f, 0.35f);
    }
}

void WallpaperEffects::initLightRay(LightRay& r, int index) {
    // Diagonal rays from upper-right corner area
    r.x = randf(0.1f, 0.7f);
    r.angle = randf(-0.6f, -0.3f);  // Slight diagonal
    r.width = randf(0.04f, 0.09f);
    r.length = randf(1.5f, 2.5f);
    r.alpha = randf(0.04f, 0.08f);
    r.drift = randf(0.01f, 0.03f);
}

// ================================================================
//  Update
// ================================================================

void WallpaperEffects::Update(float dt) {
    if (!_inited) return;
    _time += dt;

    float arousalMul = 0.8f + _arousal * 0.4f;  // 0.8-1.2 speed multiplier

    // --- Petals: parametric (no physics, matches CSS keyframe animation) ---
    // Position computed from _time in DrawParticles

    // --- Fireflies ---
    for (int i = 0; i < NUM_FIREFLIES; i++) {
        Firefly& f = _fireflies[i];
        // Gentle random walk
        f.vx += randf(-0.02f, 0.02f) * dt;
        f.vy += randf(-0.02f, 0.02f) * dt;
        f.vx *= 0.98f;  // Damping
        f.vy *= 0.98f;
        f.x += f.vx * dt * 2.f;
        f.y += f.vy * dt * 2.f;
        // Bounce off edges softly
        if (f.x < -0.9f || f.x > 0.9f) f.vx *= -0.5f;
        if (f.y < -0.7f || f.y > 0.9f) f.vy *= -0.5f;
        f.x = lerp(f.x, 0.f, dt * 0.01f); // Gentle pull toward center
        f.y = lerp(f.y, 0.1f, dt * 0.01f);
    }

    // --- Dust motes ---
    for (int i = 0; i < NUM_DUST; i++) {
        DustMote& d = _dust[i];
        float drift = sinf(_time * 0.5f + d.driftPhase) * 0.003f;
        d.x += (d.vx + drift) * dt * arousalMul;
        d.y += d.vy * dt * arousalMul;
        // Gentle random nudge
        d.vx += randf(-0.001f, 0.001f) * dt;
        d.vy += randf(-0.001f, 0.001f) * dt;
        d.vx *= 0.99f;
        d.vy *= 0.99f;
        if (d.x < -1.2f || d.x > 1.2f || d.y < -1.2f || d.y > 1.2f) {
            resetDustMote(d, false);
        }
    }

    // --- Light rays drift ---
    for (int i = 0; i < NUM_RAYS; i++) {
        _rays[i].x += _rays[i].drift * dt * 0.1f;
        if (_rays[i].x > 1.0f) _rays[i].x -= 1.5f;
    }

    // --- Bubble timer ---
    if (_bubbleVisible) {
        _bubbleTimer += dt;
    }
}

// ================================================================
//  Time / color helpers
// ================================================================

float WallpaperEffects::currentHour() {
    time_t now = time(nullptr);
    struct tm* local = localtime(&now);
    return static_cast<float>(local->tm_hour) + static_cast<float>(local->tm_min) / 60.f;
}

float WallpaperEffects::getNightFactor() {
    float h = currentHour();
    // Night: 19:30 - 5:00 full, transitions at edges
    if (h >= 19.5f || h < 5.f)  return 1.f;
    if (h >= 18.f && h < 19.5f) return smoothstep(18.f, 19.5f, h);
    if (h >= 5.f  && h < 6.5f)  return 1.f - smoothstep(5.f, 6.5f, h);
    return 0.f;
}

float WallpaperEffects::getDawnDuskFactor() {
    float h = currentHour();
    // Dawn: 6:00 - 8:30, Dusk: 17:00 - 19:00
    if (h >= 6.f  && h < 7.f)   return smoothstep(6.f, 7.f, h);
    if (h >= 7.f  && h < 8.f)   return 1.f;
    if (h >= 8.f  && h < 9.f)   return 1.f - smoothstep(8.f, 9.f, h);
    if (h >= 17.f && h < 17.5f) return smoothstep(17.f, 17.5f, h);
    if (h >= 17.5f && h < 18.5f) return 1.f;
    if (h >= 18.5f && h < 19.f) return 1.f - smoothstep(18.5f, 19.f, h);
    return 0.f;
}

void WallpaperEffects::getGradientColors(float top[4], float bot[4]) {
    float hour = currentHour();

    struct Stop { float h; float t[4]; float b[4]; };
    static const Stop S[] = {
        {  0.f, {0.04f,0.03f,0.14f,1.f}, {0.02f,0.02f,0.08f,1.f} },  // midnight: deep indigo-purple
        {  5.f, {0.04f,0.03f,0.14f,1.f}, {0.02f,0.02f,0.08f,1.f} },
        {  6.5f,{0.95f,0.72f,0.65f,1.f}, {0.98f,0.85f,0.78f,1.f} },  // dawn: warm orange-pink
        {  9.f, {0.92f,0.78f,0.72f,1.f}, {0.96f,0.88f,0.82f,1.f} },
        { 11.f, {0.55f,0.78f,0.92f,1.f}, {0.80f,0.90f,0.96f,1.f} },  // day: sky blue
        { 16.f, {0.55f,0.78f,0.92f,1.f}, {0.80f,0.90f,0.96f,1.f} },
        { 17.5f,{0.95f,0.55f,0.30f,1.f}, {0.68f,0.32f,0.55f,1.f} },  // dusk: orange-purple
        { 19.f, {0.08f,0.06f,0.22f,1.f}, {0.04f,0.03f,0.14f,1.f} },  // early night: purple-indigo
        { 22.f, {0.05f,0.04f,0.16f,1.f}, {0.02f,0.02f,0.10f,1.f} },  // deep night
        { 24.f, {0.04f,0.03f,0.14f,1.f}, {0.02f,0.02f,0.08f,1.f} },
    };
    const int N = sizeof(S) / sizeof(S[0]);

    for (int i = 0; i < N - 1; i++) {
        if (hour >= S[i].h && hour < S[i + 1].h) {
            float t = (hour - S[i].h) / (S[i + 1].h - S[i].h);
            lerpColor(S[i].t, S[i + 1].t, t, top);
            lerpColor(S[i].b, S[i + 1].b, t, bot);

            // C1: Emotion color temperature shift
            float warmShift = _valence * 0.03f;  // ±0.03 max
            top[0] = clamp01(top[0] + warmShift);
            bot[0] = clamp01(bot[0] + warmShift);
            top[2] = clamp01(top[2] - warmShift);
            bot[2] = clamp01(bot[2] - warmShift);
            return;
        }
    }
    for (int i = 0; i < 4; i++) { top[i] = S[0].t[i]; bot[i] = S[0].b[i]; }
}

void WallpaperEffects::getPetalColor(float rgba[4]) {
    // Tint multiplier: texture already has pink baked in (R255 G170 B185 range)
    // So shader color acts as a subtle tint/brightness mod, not the main color
    float hour = currentHour();

    // Day: warm white tint amplifies the baked pink
    static const float dayCol[]   = {1.0f, 0.92f, 0.95f, 1.f};
    // Night: cool blue-white tint over the pink texture
    static const float nightCol[] = {0.90f, 0.90f, 1.0f, 1.f};

    if (hour >= 7.f && hour < 17.f) {
        for (int i = 0; i < 4; i++) rgba[i] = dayCol[i];
    } else if (hour >= 19.f || hour < 5.f) {
        for (int i = 0; i < 4; i++) rgba[i] = nightCol[i];
    } else if (hour >= 17.f && hour < 19.f) {
        float t = (hour - 17.f) / 2.f;
        lerpColor(dayCol, nightCol, t, rgba);
    } else {
        float t = (hour - 5.f) / 2.f;
        lerpColor(nightCol, dayCol, t, rgba);
    }

    rgba[0] = clamp01(rgba[0] + _valence * 0.02f);
    rgba[2] = clamp01(rgba[2] - _valence * 0.02f);
}

// ================================================================
//  Draw helper: textured quad
// ================================================================

void WallpaperEffects::drawTexturedQuad(float cx, float cy, float hw, float hh,
                                         float rot, const float color[4]) {
    float co = cosf(rot), si = sinf(rot);
    float qx[4] = {-hw,  hw,  hw, -hw};
    float qy[4] = {-hh, -hh,  hh,  hh};

    float px[4], py[4];
    for (int j = 0; j < 4; j++) {
        px[j] = cx + qx[j] * co - qy[j] * si;
        py[j] = cy + qx[j] * si + qy[j] * co;
    }

    float pos[] = {
        px[0],py[0], px[1],py[1], px[2],py[2],
        px[0],py[0], px[2],py[2], px[3],py[3],
    };
    float uv[] = {
        0,1, 1,1, 1,0,
        0,1, 1,0, 0,0,
    };

    glUniform4fv(_texColLoc, 1, color);
    glVertexAttribPointer(_texPosLoc, 2, GL_FLOAT, GL_FALSE, 0, pos);
    glVertexAttribPointer(_texUvLoc,  2, GL_FLOAT, GL_FALSE, 0, uv);
    glDrawArrays(GL_TRIANGLES, 0, 6);
}

// ================================================================
//  DrawBackground — time-aware gradient
// ================================================================

void WallpaperEffects::DrawBackground() {
    if (!_inited || !_gradProg) return;

    float top[4], bot[4];
    getGradientColors(top, bot);

    const int STRIDE = 6;
    float v[] = {
        -1.f,  1.f,  top[0], top[1], top[2], top[3],
        -1.f, -1.f,  bot[0], bot[1], bot[2], bot[3],
         1.f, -1.f,  bot[0], bot[1], bot[2], bot[3],
        -1.f,  1.f,  top[0], top[1], top[2], top[3],
         1.f, -1.f,  bot[0], bot[1], bot[2], bot[3],
         1.f,  1.f,  top[0], top[1], top[2], top[3],
    };

    glDisable(GL_BLEND);
    glUseProgram(_gradProg);
    glEnableVertexAttribArray(_gradPosLoc);
    glEnableVertexAttribArray(_gradColLoc);
    glVertexAttribPointer(_gradPosLoc, 2, GL_FLOAT, GL_FALSE, STRIDE * sizeof(float), v);
    glVertexAttribPointer(_gradColLoc, 4, GL_FLOAT, GL_FALSE, STRIDE * sizeof(float), v + 2);
    glDrawArrays(GL_TRIANGLES, 0, 6);
    glDisableVertexAttribArray(_gradPosLoc);
    glDisableVertexAttribArray(_gradColLoc);
}

// ================================================================
//  DrawStars — B1: night sky twinkle
// ================================================================

void WallpaperEffects::DrawStars() {
    if (!_inited || !_texProg || !_dotTex) return;

    float nf = getNightFactor();
    if (nf <= 0.001f) return;

    glUseProgram(_texProg);
    glActiveTexture(GL_TEXTURE0);
    glUniform1i(_texSamplerLoc, 0);

    glEnable(GL_BLEND);
    glBlendFunc(GL_SRC_ALPHA, GL_ONE);  // Additive for celestial glow

    glEnableVertexAttribArray(_texPosLoc);
    glEnableVertexAttribArray(_texUvLoc);

    // --- Moon glow: large soft radial light in upper area ---
    glBindTexture(GL_TEXTURE_2D, _glowTex);
    {
        float moonAlpha = 0.18f * nf;
        float breath = 0.5f + 0.5f * sinf(_time * 0.3f);
        moonAlpha += breath * 0.04f * nf;
        float moonCol[4] = {0.75f, 0.82f, 1.0f, moonAlpha};
        drawTexturedQuad(0.45f, 0.65f, 0.7f, 0.6f, 0.f, moonCol);
    }

    // --- Starfield shimmer: broad glow across upper sky (nebula effect) ---
    {
        float shimmer = 0.07f * nf;
        float shimCol[4] = {0.7f, 0.75f, 0.95f, shimmer};
        drawTexturedQuad(-0.2f, 0.5f, 0.9f, 0.5f, 0.2f, shimCol);
        float shimCol2[4] = {0.8f, 0.78f, 0.95f, shimmer * 0.7f};
        drawTexturedQuad(0.3f, 0.3f, 0.8f, 0.4f, -0.15f, shimCol2);
    }

    // --- Stars: 160 across 3 layers ---
    glBindTexture(GL_TEXTURE_2D, _dotTex);

    for (int i = 0; i < NUM_STARS; i++) {
        const Star& s = _stars[i];

        // Twinkle: distant stars barely flicker, feature stars breathe slowly
        float twinkle;
        if (s.layer == 0) {
            twinkle = 0.6f + 0.4f * sinf(_time * s.twinkleFreq + s.twinklePhase);
        } else if (s.layer == 1) {
            twinkle = 0.45f + 0.55f * sinf(_time * s.twinkleFreq + s.twinklePhase);
        } else {
            // Feature stars: slow deep breathe
            twinkle = 0.4f + 0.6f * sinf(_time * s.twinkleFreq + s.twinklePhase);
        }

        float alpha = s.baseAlpha * twinkle * nf;
        if (alpha < 0.005f) continue;

        float sz = s.size / static_cast<float>(_width);
        float szy = s.size / static_cast<float>(_height);

        // Color varies by layer: distant=cool blue, feature=warm white
        float col[4];
        if (s.layer == 0) {
            col[0] = 0.8f; col[1] = 0.85f; col[2] = 1.0f; col[3] = alpha;
        } else if (s.layer == 1) {
            col[0] = 0.9f; col[1] = 0.92f; col[2] = 1.0f; col[3] = alpha;
        } else {
            // Feature stars: slight warm tint, with a glow halo
            col[0] = 1.0f; col[1] = 0.97f; col[2] = 0.92f; col[3] = alpha;
            // Draw outer glow halo
            float glowCol[4] = {0.85f, 0.88f, 1.0f, alpha * 0.4f};
            drawTexturedQuad(s.x, s.y, sz * 5.f, szy * 5.f, 0.f, glowCol);
        }

        drawTexturedQuad(s.x, s.y, sz, szy, 0.f, col);
    }

    glDisableVertexAttribArray(_texPosLoc);
    glDisableVertexAttribArray(_texUvLoc);
}

// ================================================================
//  DrawLightRays — C2: dawn/dusk golden rays
// ================================================================

void WallpaperEffects::DrawLightRays() {
    if (!_inited || !_texProg || !_glowTex) return;

    float ddf = getDawnDuskFactor();
    if (ddf <= 0.001f) return;

    glUseProgram(_texProg);
    glActiveTexture(GL_TEXTURE0);
    glBindTexture(GL_TEXTURE_2D, _glowTex);
    glUniform1i(_texSamplerLoc, 0);

    glEnable(GL_BLEND);
    glBlendFunc(GL_SRC_ALPHA, GL_ONE);  // Additive for light

    glEnableVertexAttribArray(_texPosLoc);
    glEnableVertexAttribArray(_texUvLoc);

    float hour = currentHour();
    bool isDusk = (hour >= 16.f && hour < 20.f);
    // Dawn: warm gold from right; Dusk: warm orange from left
    float baseAngle = isDusk ? -0.4f : -0.5f;
    float baseCx = isDusk ? -0.3f : 0.3f;

    for (int i = 0; i < NUM_RAYS; i++) {
        const LightRay& r = _rays[i];
        float alpha = r.alpha * ddf;

        // Warm golden color
        float col[4] = {1.0f, 0.9f, 0.7f, alpha};

        float cx = baseCx + r.x;
        float cy = 0.4f;
        float hw = r.length;
        float hh = r.width;
        float angle = baseAngle + r.angle;

        drawTexturedQuad(cx, cy, hw, hh, angle, col);
    }

    glDisableVertexAttribArray(_texPosLoc);
    glDisableVertexAttribArray(_texUvLoc);
}

// ================================================================
//  DrawGlow — A1: character breathing glow
// ================================================================

void WallpaperEffects::DrawGlow() {
    if (!_inited || !_texProg || !_glowTex) return;

    glUseProgram(_texProg);
    glActiveTexture(GL_TEXTURE0);
    glBindTexture(GL_TEXTURE_2D, _glowTex);
    glUniform1i(_texSamplerLoc, 0);

    glEnable(GL_BLEND);
    glBlendFunc(GL_SRC_ALPHA, GL_ONE);  // Additive glow

    glEnableVertexAttribArray(_texPosLoc);
    glEnableVertexAttribArray(_texUvLoc);

    // Breathing pulsation: ~4 second cycle
    float breath = 0.5f + 0.5f * sinf(_time * 1.57f);  // 0..1 over ~4s
    float baseAlpha = 0.12f + _valence * 0.03f;         // Emotion brightness
    float alpha = baseAlpha + breath * 0.06f;
    alpha = clamp01(alpha);

    // Glow color: warm by day, cool by night
    float nf = getNightFactor();
    float r = lerp(1.0f, 0.7f, nf);
    float g = lerp(0.95f, 0.8f, nf);
    float b = lerp(0.85f, 1.0f, nf);
    float col[4] = {r, g, b, alpha};

    // Position: below center, roughly where the model stands
    float cx = 0.f;
    float cy = -0.45f;
    float hw = 0.55f;
    float hh = 0.35f;

    drawTexturedQuad(cx, cy, hw, hh, 0.f, col);

    glDisableVertexAttribArray(_texPosLoc);
    glDisableVertexAttribArray(_texUvLoc);
}

// ================================================================
//  DrawParticles — petals (day) + fireflies (night)
// ================================================================

void WallpaperEffects::DrawParticles() {
    if (!_inited || !_texProg) return;

    float nf = getNightFactor();

    glUseProgram(_texProg);
    glUniform1i(_texSamplerLoc, 0);
    glEnable(GL_BLEND);

    glEnableVertexAttribArray(_texPosLoc);
    glEnableVertexAttribArray(_texUvLoc);

    // --- Petals: CSS keyframe animation replica ---
    // Matches desktop pet.html @keyframes petal-fall exactly:
    //   0%:   y=-10px,   x=0,    rot=0,   scale=1.0,  opacity=0
    //   6%:                                             opacity=0.85
    //  25%:   y=25vh,    x=+35,  rot=80,  scale=0.95, opacity=0.7
    //  50%:   y=50vh,    x=-20,  rot=190, scale=0.85, opacity=0.5
    //  75%:   y=75vh,    x=+50,  rot=300, scale=0.7,  opacity=0.3
    // 100%:   y=105vh,   x=+25,  rot=400, scale=0.55, opacity=0
    if (nf < 0.99f) {
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);
        glActiveTexture(GL_TEXTURE0);

        // CSS sway keyframes (translateX as fraction of screen width)
        // Scaled: desktop 35px/375px ≈ 0.09, etc.
        static const float KF_T[]  = { 0.f,   0.06f, 0.25f,  0.50f,  0.75f,  1.0f  };
        static const float KF_Y[]  = { 1.05f, 1.02f, 0.55f,  0.05f, -0.45f, -1.05f };
        static const float KF_X[]  = { 0.f,   0.f,   0.065f,-0.037f, 0.093f, 0.047f};
        static const float KF_R[]  = { 0.f,   0.f,   1.396f, 3.316f, 5.236f, 6.981f}; // degrees→rad
        static const float KF_S[]  = { 1.0f,  1.0f,  0.95f,  0.85f,  0.70f,  0.55f };
        static const float KF_A[]  = { 0.0f,  0.85f, 0.70f,  0.50f,  0.30f,  0.0f  };
        static const int   KF_N    = 6;

        for (int i = 0; i < NUM_PETALS; i++) {
            const Petal& p = _petals[i];
            if (!_petalTextures[p.texVariant]) continue;

            glBindTexture(GL_TEXTURE_2D, _petalTextures[p.texVariant]);

            // Animation progress: loops with individual speed and phase offset
            float progress = fmodf(_time * p.swayFreq + p.swayPhase, 1.0f);
            if (progress < 0.f) progress += 1.f;

            // Interpolate keyframes
            float y = KF_Y[0], swayX = KF_X[0], rot = KF_R[0];
            float scale = KF_S[0], kfAlpha = KF_A[0];
            for (int k = 0; k < KF_N - 1; k++) {
                if (progress >= KF_T[k] && progress < KF_T[k+1]) {
                    float t = (progress - KF_T[k]) / (KF_T[k+1] - KF_T[k]);
                    y       = lerp(KF_Y[k], KF_Y[k+1], t);
                    swayX   = lerp(KF_X[k], KF_X[k+1], t);
                    rot     = lerp(KF_R[k], KF_R[k+1], t);
                    scale   = lerp(KF_S[k], KF_S[k+1], t);
                    kfAlpha = lerp(KF_A[k], KF_A[k+1], t);
                    break;
                }
            }

            float x = p.x + swayX;
            float sz = p.size * scale;
            float sx = sz / static_cast<float>(_width);
            float sy = sz * p.aspect / static_cast<float>(_height);

            float alpha = kfAlpha * (1.f - nf);
            if (alpha < 0.01f) continue;

            float col[4] = {1.0f, 1.0f, 1.0f, alpha};
            drawTexturedQuad(x, y, sx, sy, rot, col);
        }
    }

    // --- Fireflies (fade in at night) ---
    if (nf > 0.01f && _dotTex) {
        glBlendFunc(GL_SRC_ALPHA, GL_ONE);  // Additive glow
        glActiveTexture(GL_TEXTURE0);
        glBindTexture(GL_TEXTURE_2D, _dotTex);

        for (int i = 0; i < NUM_FIREFLIES; i++) {
            const Firefly& f = _fireflies[i];
            float glow = 0.5f + 0.5f * sinf(_time * f.glowFreq * 6.2832f + f.glowPhase);
            float alpha = f.baseAlpha * glow * nf;
            if (alpha < 0.01f) continue;

            float sz = f.size / static_cast<float>(_width);
            float szy = f.size / static_cast<float>(_height);

            // Warm yellow-green firefly color
            float col[4] = {1.0f, 0.95f, 0.5f, alpha};
            drawTexturedQuad(f.x, f.y, sz * 2.f, szy * 2.f, 0.f, col);

            // Brighter core
            float coreCol[4] = {1.0f, 1.0f, 0.85f, alpha * 0.6f};
            drawTexturedQuad(f.x, f.y, sz * 0.8f, szy * 0.8f, 0.f, coreCol);
        }

        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);  // Restore
    }

    glDisableVertexAttribArray(_texPosLoc);
    glDisableVertexAttribArray(_texUvLoc);
}

// ================================================================
//  DrawDustMotes — A3: ambient floating light particles
// ================================================================

void WallpaperEffects::DrawDustMotes() {
    if (!_inited || !_texProg || !_dotTex) return;

    glUseProgram(_texProg);
    glActiveTexture(GL_TEXTURE0);
    glBindTexture(GL_TEXTURE_2D, _dotTex);
    glUniform1i(_texSamplerLoc, 0);

    glEnable(GL_BLEND);
    glBlendFunc(GL_SRC_ALPHA, GL_ONE);  // Additive for light dust

    glEnableVertexAttribArray(_texPosLoc);
    glEnableVertexAttribArray(_texUvLoc);

    float nf = getNightFactor();
    // Day: warm white; Night: cool blue-white
    float r = lerp(1.0f, 0.8f, nf);
    float g = lerp(0.97f, 0.85f, nf);
    float b = lerp(0.9f, 1.0f, nf);

    for (int i = 0; i < NUM_DUST; i++) {
        const DustMote& d = _dust[i];
        float alpha = d.alpha * (0.8f + _valence * 0.2f);  // Emotion brightness
        if (alpha < 0.005f) continue;

        float sz = d.size / static_cast<float>(_width);
        float szy = d.size / static_cast<float>(_height);
        float col[4] = {r, g, b, alpha};

        drawTexturedQuad(d.x, d.y, sz, szy, 0.f, col);
    }

    glDisableVertexAttribArray(_texPosLoc);
    glDisableVertexAttribArray(_texUvLoc);
}

// ================================================================
//  DrawVignette — A2: darken screen edges
// ================================================================

void WallpaperEffects::DrawVignette() {
    if (!_inited || !_texProg || !_vignetteTex) return;

    glUseProgram(_texProg);
    glActiveTexture(GL_TEXTURE0);
    glBindTexture(GL_TEXTURE_2D, _vignetteTex);
    glUniform1i(_texSamplerLoc, 0);

    glEnable(GL_BLEND);
    glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);

    glEnableVertexAttribArray(_texPosLoc);
    glEnableVertexAttribArray(_texUvLoc);

    // Vignette strength: slightly stronger at night
    float nf = getNightFactor();
    float strength = lerp(0.35f, 0.5f, nf);
    float col[4] = {0.f, 0.f, 0.f, strength};

    // Fullscreen quad, no rotation
    float pos[] = {
        -1.f,  1.f,   1.f,  1.f,   1.f, -1.f,
        -1.f,  1.f,   1.f, -1.f,  -1.f, -1.f,
    };
    float uv[] = {
        0.f, 0.f,  1.f, 0.f,  1.f, 1.f,
        0.f, 0.f,  1.f, 1.f,  0.f, 1.f,
    };

    glUniform4fv(_texColLoc, 1, col);
    glVertexAttribPointer(_texPosLoc, 2, GL_FLOAT, GL_FALSE, 0, pos);
    glVertexAttribPointer(_texUvLoc,  2, GL_FLOAT, GL_FALSE, 0, uv);
    glDrawArrays(GL_TRIANGLES, 0, 6);

    glDisableVertexAttribArray(_texPosLoc);
    glDisableVertexAttribArray(_texUvLoc);
}

// ================================================================
//  Bubble overlay (unchanged, for future use)
// ================================================================

void WallpaperEffects::SetBubbleTexture(const unsigned char* rgba, int w, int h) {
    if (!_inited) return;
    if (!_bubbleTex) {
        glGenTextures(1, &_bubbleTex);
    }
    glBindTexture(GL_TEXTURE_2D, _bubbleTex);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, w, h, 0, GL_RGBA, GL_UNSIGNED_BYTE, rgba);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    _bubbleW = w;
    _bubbleH = h;
    _bubbleTimer = 0.f;
    _bubbleAlpha = 0.f;
    _bubbleVisible = true;
}

void WallpaperEffects::ClearBubble() {
    _bubbleVisible = false;
    _bubbleAlpha = 0.f;
    _bubbleTimer = 0.f;
}

void WallpaperEffects::DrawBubble() {
    if (!_inited || !_bubbleVisible || !_bubbleTex || !_texProg) return;

    float totalDuration = BUBBLE_FADE_IN + BUBBLE_SHOW + BUBBLE_FADE_OUT;
    if (_bubbleTimer < BUBBLE_FADE_IN) {
        _bubbleAlpha = _bubbleTimer / BUBBLE_FADE_IN;
    } else if (_bubbleTimer < BUBBLE_FADE_IN + BUBBLE_SHOW) {
        _bubbleAlpha = 1.f;
    } else if (_bubbleTimer < totalDuration) {
        _bubbleAlpha = 1.f - (_bubbleTimer - BUBBLE_FADE_IN - BUBBLE_SHOW) / BUBBLE_FADE_OUT;
    } else {
        _bubbleVisible = false;
        _bubbleAlpha = 0.f;
        return;
    }

    float bw = static_cast<float>(_bubbleW) / static_cast<float>(_width);
    float bh = static_cast<float>(_bubbleH) / static_cast<float>(_height);
    float col[4] = {1.f, 1.f, 1.f, _bubbleAlpha};

    glUseProgram(_texProg);
    glActiveTexture(GL_TEXTURE0);
    glBindTexture(GL_TEXTURE_2D, _bubbleTex);
    glUniform1i(_texSamplerLoc, 0);

    glEnable(GL_BLEND);
    glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA);

    glEnableVertexAttribArray(_texPosLoc);
    glEnableVertexAttribArray(_texUvLoc);

    drawTexturedQuad(0.f, 0.2f, bw, bh, 0.f, col);

    glDisableVertexAttribArray(_texPosLoc);
    glDisableVertexAttribArray(_texUvLoc);
}

// ================================================================
//  Emotion state
// ================================================================

void WallpaperEffects::SetEmotionState(float valence, float arousal) {
    // Smooth toward target (don't jump)
    _valence = lerp(_valence, valence, 0.1f);
    _arousal = lerp(_arousal, arousal, 0.1f);
}
