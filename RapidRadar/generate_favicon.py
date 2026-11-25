"""
generate_favicon.py
====================
Generates:
  assets/favicon.svg  – the canonical SVG source
  assets/favicon.ico  – multi-size ICO (16x16, 32x32, 48x48) for the EXE icon

No third-party packages required – uses only stdlib struct/zlib.
Run before PyInstaller:  python generate_favicon.py
"""

import base64
import os
import struct
import zlib

ASSETS_DIR = os.path.join(os.path.dirname(__file__), "assets")
os.makedirs(ASSETS_DIR, exist_ok=True)

# ── SVG source ─────────────────────────────────────────────────────────────────
SVG = """\
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">
  <!-- Dark background -->
  <circle cx="16" cy="16" r="16" fill="#0d1117"/>
  <!-- Radar rings -->
  <circle cx="16" cy="16" r="12" fill="none" stroke="#58a6ff" stroke-width="1.5" opacity="0.35"/>
  <circle cx="16" cy="16" r="7"  fill="none" stroke="#58a6ff" stroke-width="1.5" opacity="0.6"/>
  <!-- Centre dot -->
  <circle cx="16" cy="16" r="2.2" fill="#58a6ff"/>
  <!-- Sweep arm -->
  <line x1="16" y1="16" x2="27" y2="7" stroke="#3fb950" stroke-width="1.8" stroke-linecap="round"/>
  <!-- Blip on sweep -->
  <circle cx="23.5" cy="9.5" r="2.8" fill="#3fb950" opacity="0.9"/>
  <!-- Faint echo blip -->
  <circle cx="11" cy="22" r="1.8" fill="#58a6ff" opacity="0.45"/>
</svg>"""

svg_path = os.path.join(ASSETS_DIR, "favicon.svg")
with open(svg_path, "w", encoding="utf-8") as f:
    f.write(SVG)
print(f"[favicon] Written {svg_path}")

# Print base64 so callers can embed it as a data URI
b64 = base64.b64encode(SVG.encode()).decode()
print(f"[favicon] Base64 length: {len(b64)} chars")

# ── Minimal ICO writer (no dependencies) ───────────────────────────────────────
# We build a proper ICO file containing three sizes from raw RGBA pixel data.

def _make_rgba(size: int) -> bytes:
    """
    Draw a tiny radar icon at *size*×*size* pixels using pure Python.
    Returns raw RGBA bytes (4 bytes per pixel, row-major, top-to-bottom).
    """
    import math
    cx = cy = size / 2.0
    r = size / 2.0

    def in_circle(px, py, cx, cy, rad):
        return (px - cx) ** 2 + (py - cy) ** 2 <= rad ** 2

    pixels = []
    for y in range(size):
        for x in range(size):
            # Clip to circle boundary
            if not in_circle(x + 0.5, y + 0.5, cx, cy, r):
                pixels.append((0, 0, 0, 0))
                continue

            dx = (x + 0.5) - cx
            dy = (y + 0.5) - cy
            dist = math.sqrt(dx * dx + dy * dy)

            # Background
            pr, pg, pb, pa = 0x0d, 0x11, 0x17, 255

            # Radar rings  (#58a6ff)
            for ring_r, alpha_mult in [(r * 0.75, 0.35), (r * 0.44, 0.60)]:
                if abs(dist - ring_r) < max(1.0, size * 0.04):
                    pr = int(0x58 * alpha_mult + pr * (1 - alpha_mult))
                    pg = int(0xa6 * alpha_mult + pg * (1 - alpha_mult))
                    pb = int(0xff * alpha_mult + pb * (1 - alpha_mult))

            # Centre dot
            if dist < r * 0.14:
                pr, pg, pb = 0x58, 0xa6, 0xff

            # Sweep arm (green, angle ~-45°)
            angle = math.atan2(-dy, dx)  # positive = counter-clockwise
            arm_angle = math.radians(45)  # 45° above positive-x axis
            if (abs(angle - arm_angle) < math.radians(max(4, 360 / size))
                    and dist < r * 0.85 and dist > r * 0.15):
                pr, pg, pb = 0x3f, 0xb9, 0x50

            # Blip on sweep
            if in_circle(x + 0.5, y + 0.5, cx + r * 0.47, cy - r * 0.47, max(2, r * 0.18)):
                pr, pg, pb = 0x3f, 0xb9, 0x50
                pa = 230

            pixels.append((pr, pg, pb, pa))

    return bytes(c for p in pixels for c in p)


def _rgba_to_png(rgba: bytes, size: int) -> bytes:
    """Minimal PNG encoder: 32-bit RGBA, no external libs."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    raw_rows = b""
    row_bytes = size * 4
    for row in range(size):
        raw_rows += b"\x00" + rgba[row * row_bytes: (row + 1) * row_bytes]

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw_rows, 9))
        + chunk(b"IEND", b"")
    )


def _build_ico(sizes=(16, 32, 48)) -> bytes:
    """Build a multi-resolution ICO file (PNG-format images inside ICO wrapper)."""
    images = []
    for s in sizes:
        rgba = _make_rgba(s)
        png  = _rgba_to_png(rgba, s)
        images.append((s, png))

    # ICO header: 6 bytes
    header = struct.pack("<HHH", 0, 1, len(images))
    # Directory: 16 bytes per image
    offset = 6 + 16 * len(images)
    directory = b""
    for s, png in images:
        w = h = 0 if s == 256 else s  # 0 means 256 in ICO format
        directory += struct.pack("<BBBBHHII", w, h, 0, 0, 1, 32, len(png), offset)
        offset += len(png)

    return header + directory + b"".join(png for _, png in images)


ico_path = os.path.join(ASSETS_DIR, "favicon.ico")
with open(ico_path, "wb") as f:
    f.write(_build_ico((16, 32, 48)))
print(f"[favicon] Written {ico_path}  ({os.path.getsize(ico_path)} bytes)")

# ── Print the data URI for embedding in HTML ──────────────────────────────────
print(f"\n[favicon] Embed tag:")
print(f'  <link rel="icon" type="image/svg+xml" href="data:image/svg+xml;base64,{b64}">')
