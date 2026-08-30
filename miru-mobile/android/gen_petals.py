#!/usr/bin/env python3
"""
Generate sakura petal RGBA textures matching the desktop CSS aesthetic.

Desktop CSS reference:
  border-radius: 50% 0 50% 0;
  radial-gradient(ellipse at 35% 30%, rgba(255,140,170,0.85), rgba(255,183,197,0.3) 60%, transparent);
  filter: drop-shadow(0 0 2px rgba(255,160,180,0.3));

Output: 4 raw RGBA files (128x128) in app/src/main/assets/effects/
"""
import struct, math, os

S = 128  # texture size

def clamp(v, lo=0.0, hi=1.0):
    return max(lo, min(hi, v))

def smoothstep(e0, e1, x):
    t = clamp((x - e0) / (e1 - e0))
    return t * t * (3 - 2 * t)

def lerp(a, b, t):
    return a + (b - a) * t

def generate_petal(variant=0):
    """
    Generate a single sakura petal texture.

    The shape is CSS border-radius: 50% 0 50% 0, which creates a leaf shape:
    - Top-left corner: rounded (quarter circle)
    - Top-right corner: sharp point
    - Bottom-right corner: rounded (quarter circle)
    - Bottom-left corner: sharp point

    The fill is a radial gradient from opaque pink at center to transparent at edges,
    clipped by the shape. This gives the soft, glowing quality of the desktop version.

    Variants differ in:
    - Aspect ratio (how elongated)
    - Gradient center offset
    - Color warmth
    - Shape stretching
    """
    pixels = bytearray(S * S * 4)

    # Variant parameters
    if variant == 0:
        # Standard petal - matches CSS .petal-1 (strongest pink)
        stretch_x, stretch_y = 1.0, 0.82
        grad_cx, grad_cy = 0.35, 0.30
        color_center = (255, 120, 155)     # deep warm pink
        color_mid = (255, 165, 185)        # medium pink
        center_alpha = 0.92
        mid_alpha = 0.55
    elif variant == 1:
        # Rounder, more face-on petal
        stretch_x, stretch_y = 0.9, 0.9
        grad_cx, grad_cy = 0.38, 0.32
        color_center = (255, 130, 165)
        color_mid = (255, 175, 195)
        center_alpha = 0.88
        mid_alpha = 0.50
    elif variant == 2:
        # More elongated, side view
        stretch_x, stretch_y = 0.75, 1.0
        grad_cx, grad_cy = 0.33, 0.28
        color_center = (255, 110, 150)     # deepest pink
        color_mid = (255, 160, 180)
        center_alpha = 0.88
        mid_alpha = 0.50
    else:
        # Delicate, lighter variant (still clearly pink)
        stretch_x, stretch_y = 0.85, 0.95
        grad_cx, grad_cy = 0.40, 0.35
        color_center = (255, 140, 170)
        color_mid = (255, 180, 200)
        center_alpha = 0.90
        mid_alpha = 0.52

    for y in range(S):
        for x in range(S):
            # Normalize to [0, 1]
            ux = x / (S - 1)
            uy = y / (S - 1)

            # Apply stretch for variant shape
            # Center the stretch around 0.5
            sx = 0.5 + (ux - 0.5) / stretch_x
            sy = 0.5 + (uy - 0.5) / stretch_y

            # === SHAPE: CSS border-radius: 50% 0 50% 0 ===
            # Sharp corners at top-right (1,0) and bottom-left (0,1)
            # Rounded corners at top-left (0,0) and bottom-right (1,1)

            # Check if inside the unit square
            if sx < 0 or sx > 1 or sy < 0 or sy > 1:
                idx = (y * S + x) * 4
                pixels[idx:idx+4] = b'\x00\x00\x00\x00'
                continue

            # Distance from center (0.5, 0.5)
            dx = sx - 0.5
            dy = sy - 0.5
            dist_center = math.sqrt(dx * dx + dy * dy)

            # Determine if in rounded quadrant
            in_rounded = (sx < 0.5 and sy < 0.5) or (sx > 0.5 and sy > 0.5)

            if in_rounded:
                # Rounded corner: use circle SDF (radius = 0.5)
                sdf = dist_center - 0.5
            else:
                # Sharp corner: use box boundary
                sdf = max(max(sx - 1, -sx), max(sy - 1, -sy))

            # Shape mask with soft edge
            shape_mask = 1.0 - smoothstep(-0.02, 0.04, sdf)

            if shape_mask < 0.001:
                idx = (y * S + x) * 4
                pixels[idx:idx+4] = b'\x00\x00\x00\x00'
                continue

            # === GRADIENT: radial-gradient(ellipse at grad_cx grad_cy, ...) ===
            # This is the key: the gradient provides BOTH color and alpha
            gx = ux - grad_cx
            gy = uy - grad_cy
            # Elliptical distance (wider horizontally)
            grad_dist = math.sqrt(gx * gx / 1.3 + gy * gy)

            # Normalize gradient distance to [0, 1] range
            # Slower falloff than CSS to keep pink visible across more of the petal
            grad_norm = grad_dist / 0.7

            # Alpha from gradient — hold strong pink longer, then fade
            if grad_norm < 0.5:
                # Inner half: strong, nearly uniform alpha
                t = grad_norm / 0.5
                g_alpha = lerp(center_alpha, mid_alpha, t * t)  # ease-in: stays bright longer
            else:
                # Outer half: fade to transparent
                t = (grad_norm - 0.5) / 0.5
                g_alpha = lerp(mid_alpha, 0.0, clamp(t * t))  # ease-in: fades gently

            # Color from gradient
            if grad_norm < 0.6:
                t = grad_norm / 0.6
                r = lerp(color_center[0], color_mid[0], t)
                g = lerp(color_center[1], color_mid[1], t)
                b = lerp(color_center[2], color_mid[2], t)
            else:
                r, g, b = color_mid

            # === DROP SHADOW: subtle pink glow ===
            # Add a faint glow that extends slightly beyond the shape
            glow_alpha = 0.0
            if sdf > -0.06 and sdf < 0.08:
                glow_alpha = (1.0 - smoothstep(-0.06, 0.08, sdf)) * 0.15

            # Final alpha = shape_mask * gradient_alpha + glow
            final_alpha = clamp(shape_mask * g_alpha + glow_alpha)

            idx = (y * S + x) * 4
            pixels[idx + 0] = int(clamp(r / 255) * 255)
            pixels[idx + 1] = int(clamp(g / 255) * 255)
            pixels[idx + 2] = int(clamp(b / 255) * 255)
            pixels[idx + 3] = int(final_alpha * 255)

    return bytes(pixels)

def main():
    out_dir = os.path.join(os.path.dirname(__file__),
                           "app/src/main/assets/effects")
    os.makedirs(out_dir, exist_ok=True)

    for i in range(4):
        data = generate_petal(variant=i)
        path = os.path.join(out_dir, f"petal_{i}.rgba")
        with open(path, 'wb') as f:
            f.write(data)
        print(f"Generated {path} ({len(data)} bytes)")

    print(f"\nAll 4 petal textures generated at {S}x{S} RGBA")
    print(f"Total size: {S*S*4*4 / 1024:.0f} KB")

if __name__ == "__main__":
    main()
