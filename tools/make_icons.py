#!/usr/bin/env python3
"""Генерирует PNG-иконки PWA (планета с орбитой на тёмном фоне) без внешних
библиотек — только zlib/struct из стандартной библиотеки.

    python tools/make_icons.py        # пишет app/static/icons/*.png
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "app" / "static" / "icons"
BG = (7, 10, 18)
PLANET = (57, 135, 229)
RING = (144, 133, 233)


def png(path: Path, w: int, h: int, pixels: list[tuple[int, int, int, int]]) -> None:
    raw = b"".join(b"\x00" + b"".join(struct.pack("BBBB", *pixels[y * w + x]) for x in range(w)) for y in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def mix(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def icon(size: int, scale: float = 1.0, rounded: bool = True, transparent: bool = False):
    px = []
    c = size / 2
    r_planet = size * 0.2 * scale
    ring_a, ring_b, ring_w = size * 0.36 * scale, size * 0.12 * scale, size * 0.025 * scale
    corner = size * 0.22
    tilt = math.radians(-20)
    ct, st = math.cos(tilt), math.sin(tilt)
    for y in range(size):
        for x in range(size):
            dx, dy = x + 0.5 - c, y + 0.5 - c
            # фон со скруглёнными углами
            qx, qy = max(abs(dx) - (c - corner), 0), max(abs(dy) - (c - corner), 0)
            inside = (math.hypot(qx, qy) <= corner) if rounded else True
            alpha = 0 if (transparent or not inside) else 255
            col = BG
            # орбита: эллипс, повёрнутый на tilt
            ex, ey = dx * ct + dy * st, -dx * st + dy * ct
            e = math.hypot(ex / ring_a, ey / ring_b)
            ring_d = abs(e - 1) * min(ring_a, ring_b)
            d = math.hypot(dx, dy)
            behind = ey < 0 and d < r_planet
            if ring_d < ring_w and not behind:
                col, alpha = RING, 255
            elif d < r_planet:
                light = max(0.0, min(1.0, 0.55 + (-dx - dy) / (2.4 * r_planet)))
                col, alpha = mix((18, 42, 90), PLANET, light), 255
            elif d < r_planet * 1.5 and alpha:
                col = mix(PLANET, BG, (d - r_planet) / (r_planet * 0.5) * 0.15 + 0.85)
            px.append((*col, alpha))
    return px


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for size in (192, 512):
        png(OUT / f"icon-{size}.png", size, size, icon(size))
    png(OUT / "icon-maskable-512.png", 512, 512, icon(512, scale=0.75, rounded=False))
    png(OUT / "badge-72.png", 72, 72, icon(72, scale=1.2, rounded=False, transparent=True))
    print("Иконки записаны в", OUT)


if __name__ == "__main__":
    main()
