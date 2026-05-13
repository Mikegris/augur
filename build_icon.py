"""
Generate icon.icns for AUGUR.app.

Draws a 1024x1024 master matching the in-app brand mark (green letter on
a dark navy field), exports the iconset PNGs at every macOS-required
size, and runs `iconutil` to produce the final .icns.

macOS-only: `iconutil` doesn't exist on other platforms.

Run:
    python build_icon.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).parent
OUT_ICNS = HERE / "icon.icns"

# Matches the in-app theme (see static/css/terminal.css).
BG       = (2, 11, 18)       # --bg-primary
PANEL    = (10, 32, 48)      # --bg-elevated
GREEN    = (0, 255, 159)     # --green
BORDER   = (26, 64, 96)      # --border-bright

MASTER_SIZE = 1024  # macOS expects up to 1024x1024 (@2x of 512)

# Sizes required for a complete .icns (name → pixels)
ICONSET = {
    "icon_16x16.png":       16,
    "icon_16x16@2x.png":    32,
    "icon_32x32.png":       32,
    "icon_32x32@2x.png":    64,
    "icon_128x128.png":    128,
    "icon_128x128@2x.png": 256,
    "icon_256x256.png":    256,
    "icon_256x256@2x.png": 512,
    "icon_512x512.png":    512,
    "icon_512x512@2x.png": 1024,
}


def _load_font(size: int) -> ImageFont.FreeTypeFont:
    """Try Menlo first (matches the app's terminal vibe), fall back to
    PIL's default if it's not available."""
    for candidate in [
        "/System/Library/Fonts/Menlo.ttc",
        "/System/Library/Fonts/Monaco.ttf",
        "/Library/Fonts/Menlo.ttc",
    ]:
        if Path(candidate).exists():
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
    return ImageFont.load_default()


def draw_master() -> Image.Image:
    s = MASTER_SIZE
    img = Image.new("RGBA", (s, s), BG + (255,))
    d = ImageDraw.Draw(img)

    # Rounded-rect frame, inset from the edges. The macOS icon-mask system
    # squircle-clips us anyway, but a visible inner border gives the icon
    # the same "terminal panel" feel as the in-app brand-logo.
    pad      = int(s * 0.09)
    border_w = max(8, int(s * 0.012))
    radius   = int(s * 0.22)
    d.rounded_rectangle(
        (pad, pad, s - pad, s - pad),
        radius=radius,
        outline=GREEN,
        width=border_w,
        fill=PANEL + (255,),
    )

    # Subtle scanline pattern across the panel — barely visible but ties
    # back to the terminal aesthetic.
    line_alpha = 18
    for y in range(pad + border_w, s - pad - border_w, 6):
        d.line([(pad + border_w, y), (s - pad - border_w, y)],
               fill=GREEN + (line_alpha,), width=1)

    # The letter — "A" for AUGUR. Big, monospace, dead-centred.
    font = _load_font(int(s * 0.58))
    text = "A"
    bbox = d.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    # textbbox can include leading whitespace — compensate with bbox[0]/bbox[1]
    tx = (s - tw) // 2 - bbox[0]
    ty = (s - th) // 2 - bbox[1]
    d.text((tx, ty), text, fill=GREEN, font=font)

    return img


def main() -> int:
    if sys.platform != "darwin":
        sys.stderr.write("✗ build_icon.py is macOS-only (requires iconutil).\n")
        return 1
    if not shutil.which("iconutil"):
        sys.stderr.write("✗ iconutil not found — install the Xcode CLT.\n")
        return 1

    master = draw_master()

    iconset = HERE / "icon.iconset"
    if iconset.exists():
        shutil.rmtree(iconset)
    iconset.mkdir()

    for name, px in ICONSET.items():
        master.resize((px, px), Image.LANCZOS).save(iconset / name, "PNG")
        print(f"  ✓ {name}  ({px}x{px})")

    subprocess.run(
        ["iconutil", "-c", "icns", str(iconset), "-o", str(OUT_ICNS)],
        check=True,
    )
    shutil.rmtree(iconset)
    print(f"\n✓ Wrote {OUT_ICNS}  ({OUT_ICNS.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
