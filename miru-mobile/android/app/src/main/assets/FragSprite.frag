// Cubism SDK sprite fragment shader (GLSL ES 2.0)
precision mediump float;
varying vec2 vuv;
uniform sampler2D texture;
uniform vec4 baseColor;
void main() {
    gl_FragColor = texture2D(texture, vuv) * baseColor;
}
