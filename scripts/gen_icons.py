#!/usr/bin/env python3
"""Generate PWA icons deterministically — stdlib only (Pillow lands in Prompt 06).

WHY stdlib: no image dependencies yet, and regeneration must be reproducible.
Output: green tile + white ring + centre dot (matatu-green, fare-circle).
"""
import struct
import zlib
from pathlib import Path

BG = (0x0B, 0x7A, 0x3B, 0xFF)   # matatu green
FG = (0xFF, 0xFF, 0xFF, 0xFF)   # fare white


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def write_png(path: Path, size: int) -> None:
    cx = cy = size / 2.0
    ring_r, ring_w, dot_r = size * 0.34, size * 0.12, size * 0.15
    rows = []
    for y in range(size):
        row = bytearray(b"\x00")  # PNG filter type 0 per scanline
        for x in range(size):
            hits = 0
            for dx in (0.25, 0.75):       # 2x2 supersampling
                for dy in (0.25, 0.75):
                    px, py = x + dx, y + dy
                    d = ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5
                    if abs(d - ring_r) <= ring_w / 2 or d <= dot_r:
                        hits += 1
            row += bytes(FG if hits >= 2 else BG)
        rows.append(bytes(row))
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    payload = (b"\x89PNG\r\n\x1a\n"
               + _chunk(b"IHDR", ihdr)
               + _chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
               + _chunk(b"IEND", b""))
    path.write_bytes(payload)
    print(f"wrote {path} ({len(payload)} bytes)")


def main() -> None:
    icons = Path(__file__).resolve().parent.parent / "static" / "icons"
    icons.mkdir(parents=True, exist_ok=True)
    for size in (192, 512):
        write_png(icons / f"icon-{size}.png", size)


if __name__ == "__main__":
    main()
