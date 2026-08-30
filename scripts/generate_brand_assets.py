#!/usr/bin/env python3
"""Generate Miru app icons from the maintained brand source.

The logo source of truth is assets/brand/miru-logo.svg. This script redraws the
same geometry with Pillow so the desktop, PWA, and Android raster assets can be
refreshed without relying on a GUI export step.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw


ROOT = Path(__file__).resolve().parents[1]
BRAND_DIR = ROOT / "assets" / "brand"
TAURI_ICON_DIR = ROOT / "src-tauri" / "icons"
ANDROID_RES_DIR = ROOT / "miru-mobile" / "android" / "app" / "src" / "main" / "res"

CANVAS = 1024
OVERSAMPLE = 4

INK = "#4A3A30"
EYE = "#D99A2B"
BG = "#FFF5E9"
BG_EDGE = "#F8E1D4"
ROSE = "#D77F75"
HIGHLIGHT = "#FFF9EF"


def _scale_points(points: list[tuple[float, float]], factor: float) -> list[tuple[int, int]]:
    return [(round(x * factor), round(y * factor)) for x, y in points]


def _cubic(
    p0: tuple[float, float],
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
    steps: int = 24,
) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for i in range(steps + 1):
        t = i / steps
        mt = 1 - t
        x = mt**3 * p0[0] + 3 * mt**2 * t * p1[0] + 3 * mt * t**2 * p2[0] + t**3 * p3[0]
        y = mt**3 * p0[1] + 3 * mt**2 * t * p1[1] + 3 * mt * t**2 * p2[1] + t**3 * p3[1]
        points.append((x, y))
    return points


def _path_from_segments(segments: list[list[tuple[float, float]]]) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for seg in segments:
        if points and seg and points[-1] == seg[0]:
            points.extend(seg[1:])
        else:
            points.extend(seg)
    return points


def _line(draw: ImageDraw.ImageDraw, points: list[tuple[float, float]], fill: str, width: int, factor: float) -> None:
    scaled = _scale_points(points, factor)
    draw.line(scaled, fill=fill, width=round(width * factor), joint="curve")
    radius = round(width * factor / 2)
    for x, y in (scaled[0], scaled[-1]):
        draw.ellipse([x - radius, y - radius, x + radius, y + radius], fill=fill)


def _rounded_rect(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], radius: int, fill: str) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def _background(size: int) -> Image.Image:
    edge = tuple(int(BG_EDGE[i : i + 2], 16) for i in (1, 3, 5))
    img = Image.new("RGBA", (size, size), (*edge, 255))
    # Subtle radial warmth: enough to read as soft, but still clean at 32 px.
    center = (size * 0.50, size * 0.44)
    max_r = size * 0.74
    base = tuple(int(BG[i : i + 2], 16) for i in (1, 3, 5))
    mask = Image.new("L", (size, size), 0)
    md = ImageDraw.Draw(mask)
    for r in range(round(max_r), 0, -8):
        t = r / max_r
        value = round(255 * (1 - t))
        md.ellipse([center[0] - r, center[1] - r, center[0] + r, center[1] + r], fill=value)
    gradient = Image.new("RGBA", (size, size), (*edge, 255))
    inner = Image.new("RGBA", (size, size), (*base, 255))
    gradient = Image.composite(inner, gradient, mask)
    radius = round(size * 0.209)
    bg_mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(bg_mask).rounded_rectangle(
        [round(size * 0.023), round(size * 0.023), round(size * 0.977), round(size * 0.977)],
        radius=radius,
        fill=255,
    )
    return Image.composite(gradient, img, bg_mask)


def _draw_symbol(draw: ImageDraw.ImageDraw, factor: float) -> None:
    window = _path_from_segments(
        [
            [(200, 724), (200, 374)],
            _cubic((200, 374), (200, 292), (260, 232), (342, 232), 18),
            [(342, 232), (682, 232)],
            _cubic((682, 232), (764, 232), (824, 292), (824, 374), 18),
            [(824, 374), (824, 724)],
        ]
    )
    _line(draw, window, INK, 42, factor)

    cat = _path_from_segments(
        [
            _cubic((278, 706), (254, 640), (280, 596), (300, 570), 16),
            _cubic((300, 570), (288, 506), (319, 433), (366, 417), 18),
            _cubic((366, 417), (395, 408), (432, 469), (461, 517), 14),
            _cubic((461, 517), (493, 496), (534, 496), (566, 519), 14),
            _cubic((566, 519), (596, 470), (660, 406), (694, 424), 18),
            _cubic((694, 424), (738, 447), (751, 515), (739, 569), 16),
            _cubic((739, 569), (766, 605), (781, 652), (759, 706), 16),
        ]
    )
    _line(draw, cat, INK, 42, factor)

    # Bookmark accent, drawn before the sill so it appears tucked under the ledge.
    bookmark = _scale_points([(708, 714), (760, 714), (760, 824), (734, 798), (708, 824)], factor)
    draw.polygon(bookmark, fill=ROSE)
    _line(draw, [(708, 714), (760, 714), (760, 824), (734, 798), (708, 824), (708, 714)], INK, 30, factor)

    _line(draw, [(176, 724), (848, 724)], INK, 42, factor)

    for cx in (456, 568):
        glow_radius = round(44 * factor)
        x = round(cx * factor)
        y = round(640 * factor)
        draw.ellipse([x - glow_radius, y - glow_radius, x + glow_radius, y + glow_radius], fill=(217, 154, 43, 52))
        r = round(28 * factor)
        draw.ellipse([x - r, y - r, x + r, y + r], fill=EYE)
        hr = round(9 * factor)
        draw.ellipse([x - round(9 * factor) - hr, y - round(8 * factor) - hr, x - round(9 * factor) + hr, y - round(8 * factor) + hr], fill=HIGHLIGHT)

    for cx in (405, 619):
        x = round(cx * factor)
        y = round(670 * factor)
        rx = round(14 * factor)
        ry = round(9 * factor)
        draw.ellipse([x - rx, y - ry, x + rx, y + ry], fill=ROSE)


def render_logo(size: int, *, background: bool = True, symbol_scale: float = 1.0) -> Image.Image:
    large = size * OVERSAMPLE
    factor = large / CANVAS
    if background:
        img = _background(large)
    else:
        img = Image.new("RGBA", (large, large), (0, 0, 0, 0))
    layer = Image.new("RGBA", (large, large), (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    if symbol_scale != 1.0:
        symbol = Image.new("RGBA", (large, large), (0, 0, 0, 0))
        _draw_symbol(ImageDraw.Draw(symbol), factor)
        scaled = symbol.resize((round(large * symbol_scale), round(large * symbol_scale)), Image.Resampling.LANCZOS)
        layer.alpha_composite(scaled, ((large - scaled.width) // 2, (large - scaled.height) // 2))
    else:
        _draw_symbol(draw, factor)
    img.alpha_composite(layer)
    return img.resize((size, size), Image.Resampling.LANCZOS)


def save_png(path: Path, size: int, *, background: bool = True, symbol_scale: float = 1.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    render_logo(size, background=background, symbol_scale=symbol_scale).save(path, optimize=True)


def make_icns(source_1024: Image.Image, out_path: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "miru.iconset"
        iconset.mkdir()
        specs = [
            ("icon_16x16.png", 16),
            ("icon_16x16@2x.png", 32),
            ("icon_32x32.png", 32),
            ("icon_32x32@2x.png", 64),
            ("icon_128x128.png", 128),
            ("icon_128x128@2x.png", 256),
            ("icon_256x256.png", 256),
            ("icon_256x256@2x.png", 512),
            ("icon_512x512.png", 512),
            ("icon_512x512@2x.png", 1024),
        ]
        for name, size in specs:
            source_1024.resize((size, size), Image.Resampling.LANCZOS).save(iconset / name, optimize=True)
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out_path)], check=True)


def make_ico(source_1024: Image.Image, out_path: Path) -> None:
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    source_1024.save(out_path, format="ICO", sizes=sizes)


def update_android_background_color() -> None:
    path = ANDROID_RES_DIR / "values" / "ic_launcher_background.xml"
    text = path.read_text(encoding="utf-8")
    text = text.replace("#FFFFFF", BG)
    path.write_text(text, encoding="utf-8")


def generate() -> None:
    BRAND_DIR.mkdir(parents=True, exist_ok=True)
    TAURI_ICON_DIR.mkdir(parents=True, exist_ok=True)

    full_1024 = render_logo(1024, background=True)
    full_1024.save(BRAND_DIR / "miru-logo-1024.png", optimize=True)

    for size in (192, 512):
        full_1024.resize((size, size), Image.Resampling.LANCZOS).save(ROOT / f"icon-{size}.png", optimize=True)

    tauri_sizes = {
        "32x32.png": 32,
        "128x128.png": 128,
        "128x128@2x.png": 256,
        "icon.png": 512,
        "Square30x30Logo.png": 30,
        "Square44x44Logo.png": 44,
        "Square71x71Logo.png": 71,
        "Square89x89Logo.png": 89,
        "Square107x107Logo.png": 107,
        "Square142x142Logo.png": 142,
        "Square150x150Logo.png": 150,
        "Square284x284Logo.png": 284,
        "Square310x310Logo.png": 310,
        "StoreLogo.png": 50,
    }
    for name, size in tauri_sizes.items():
        full_1024.resize((size, size), Image.Resampling.LANCZOS).save(TAURI_ICON_DIR / name, optimize=True)
    make_icns(full_1024, TAURI_ICON_DIR / "icon.icns")
    make_ico(full_1024, TAURI_ICON_DIR / "icon.ico")

    densities = {
        "mipmap-mdpi": (48, 108),
        "mipmap-hdpi": (72, 162),
        "mipmap-xhdpi": (96, 216),
        "mipmap-xxhdpi": (144, 324),
        "mipmap-xxxhdpi": (192, 432),
    }
    for density, (launcher_size, foreground_size) in densities.items():
        dir_path = ANDROID_RES_DIR / density
        save_png(dir_path / "ic_launcher.png", launcher_size, background=True)
        save_png(dir_path / "ic_launcher_round.png", launcher_size, background=True)
        save_png(dir_path / "ic_launcher_foreground.png", foreground_size, background=False, symbol_scale=0.74)
    update_android_background_color()

    # Remove older generated iconsets if an interrupted run left one in the tree.
    leftover = TAURI_ICON_DIR / "miru.iconset"
    if leftover.exists():
        shutil.rmtree(leftover)


if __name__ == "__main__":
    os.chdir(ROOT)
    generate()
    print("Generated Miru brand assets.")
