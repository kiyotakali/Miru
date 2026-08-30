// Cubism SDK sprite vertex shader (GLSL ES 2.0)
attribute vec2 position;
attribute vec2 uv;
varying vec2 vuv;
void main() {
    gl_Position = vec4(position, 0.0, 1.0);
    vuv = uv;
}
