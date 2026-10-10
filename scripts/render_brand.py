"""Draw the CaYaDraft avatar and banner with the CaYaDev palette.

Runtime does not need Pillow. Regenerate with: python scripts/render_brand.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets"
BOLD = Path(r"C:\Windows\Fonts\segoeuib.ttf")
REGULAR = Path(r"C:\Windows\Fonts\segoeui.ttf")

BG = (17, 24, 38, 255)
RED = (220, 38, 38, 255)
CARD = (23, 34, 52, 255)
PAGE = (34, 49, 73, 255)
TEXT = (248, 250, 252, 255)
MUTED = (148, 163, 184, 255)
LINE = (203, 213, 225, 255)


def load(path: Path, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(path), size)


def glow(base: Image.Image, center: tuple[int, int], radius: int) -> None:
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    pixels = layer.load()
    cx, cy = center
    x0, x1 = max(0, cx - radius), min(base.size[0], cx + radius)
    y0, y1 = max(0, cy - radius), min(base.size[1], cy + radius)
    for y in range(y0, y1):
        for x in range(x0, x1):
            distance = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
            if distance >= radius:
                continue
            fade = (1 - distance / radius) ** 2
            pixels[x, y] = (220, 38, 38, int(78 * fade))
    base.alpha_composite(layer)


def document(draw: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int) -> None:
    draw.rounded_rectangle((x + 12, y + 16, x + w + 12, y + h + 16), 36, fill=(0, 0, 0, 90))
    draw.rounded_rectangle((x, y, x + w, y + h), 36, fill=PAGE, outline=LINE, width=5)
    fold = 78
    draw.polygon(
        [(x + w - fold, y + 2), (x + w - 2, y + fold), (x + w - fold, y + fold)],
        fill=RED,
    )
    draw.line((x + w - fold, y + 2, x + w - fold, y + fold, x + w - 2, y + fold), fill=(248, 250, 252, 180), width=3)
    left = x + 42
    inner_right = x + w - fold - 28
    span = max(40, inner_right - left)
    colors = [MUTED, MUTED, RED, MUTED]
    scales = (0.92, 0.74, 0.62, 0.8)
    for index, (color, scale) in enumerate(zip(colors, scales)):
        top = y + int(h * 0.30) + index * max(28, int(h * 0.13))
        draw.rounded_rectangle((left, top, left + int(span * scale), top + max(10, int(h * 0.045))), 8, fill=color)


def wordmark(draw: ImageDraw.ImageDraw, center_x: int, y: int, size: int) -> None:
    font = load(BOLD, size)
    left, right = "CaYa", "Draft"
    left_w = draw.textlength(left, font=font)
    right_w = draw.textlength(right, font=font)
    x = center_x - (left_w + right_w) / 2
    draw.text((x, y), left, font=font, fill=TEXT)
    draw.text((x + left_w, y), right, font=font, fill=RED)


def avatar() -> None:
    """Flat mark, no words. The page sits on the optical center."""
    size = 1024
    image = Image.new("RGBA", (size, size), BG)
    draw = ImageDraw.Draw(image)
    width, height = 400, 500
    x = (size - width) // 2
    y = (size - height) // 2
    draw.rounded_rectangle((x, y, x + width, y + height), 56, fill=PAGE, outline=LINE, width=6)
    fold = 112
    draw.polygon(
        [(x + width - fold, y + 6), (x + width - 6, y + fold), (x + width - fold, y + fold)],
        fill=RED,
    )
    bar_h = 22
    gap = 46
    scales = (0.62, 0.40, 0.52)
    colors = (MUTED, RED, MUTED)
    block = len(scales) * bar_h + (len(scales) - 1) * gap
    top = y + (height - block) // 2 + 8
    inner = width - 160
    for index, (scale, color) in enumerate(zip(scales, colors)):
        bar_w = int(inner * scale)
        left = x + (width - bar_w) // 2
        bar_y = top + index * (bar_h + gap)
        draw.rounded_rectangle((left, bar_y, left + bar_w, bar_y + bar_h), 11, fill=color)
    image.save(OUT / "avatar.png", "PNG")


def banner() -> None:
    image = Image.new("RGBA", (1280, 640), BG)
    glow(image, (280, 300), 340)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 1280, 8), fill=RED)
    draw.ellipse((78, 108, 502, 532), outline=RED, width=8)
    document(draw, 168, 150, 230, 300)
    wordmark(draw, 860, 150, 78)
    regular = load(REGULAR, 32)
    draw.text((560, 280), "Reads the issue. Writes a draft. Waits for you.", font=regular, fill=LINE)
    small = load(REGULAR, 24)
    draw.text((560, 500), "cayadev.com", font=small, fill=RED)
    image.save(OUT / "banner.png", "PNG")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    avatar()
    banner()
    print(OUT / "avatar.png")
    print(OUT / "banner.png")
