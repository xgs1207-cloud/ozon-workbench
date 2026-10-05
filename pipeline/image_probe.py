"""图片探测与写入（纯标准库）。

为什么不用 Pillow：核心链路要能在**零第三方依赖**下跑通（也让测试环境更可控）。
这里只需要两件事 —— 读图片的真实尺寸/格式，以及写出尺寸正确的最小 PNG 占位图。

支持探测：PNG / JPEG / WebP / GIF（覆盖我们可能遇到的格式）；写只支持 PNG（Ozon 项目规则也是仅 png）。
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path
from typing import Any

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".avif"}


def _png_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24 or not data.startswith(PNG_SIGNATURE):
        return None
    if data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    index = 2
    while index + 9 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in {0xD8, 0xD9, 0x01} or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        if index + 4 > len(data):
            return None
        length = struct.unpack(">H", data[index + 2 : index + 4])[0]
        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
            if index + 9 > len(data):
                return None
            height, width = struct.unpack(">HH", data[index + 5 : index + 9])
            return int(width), int(height)
        index += 2 + length
    return None


def _webp_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    chunk = data[12:16]
    if chunk == b"VP8X":
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height
    if chunk == b"VP8 ":
        # 关键帧头：3 字节 tag + 3 字节同步码 + 2 字节宽 + 2 字节高
        start = 20
        if data[start + 3 : start + 6] != b"\x9d\x01\x2a":
            return None
        width = struct.unpack("<H", data[start + 6 : start + 8])[0] & 0x3FFF
        height = struct.unpack("<H", data[start + 8 : start + 10])[0] & 0x3FFF
        return int(width), int(height)
    if chunk == b"VP8L":
        bits = int.from_bytes(data[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return int(width), int(height)
    return None


def _gif_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 10 or data[:3] not in {b"GIF"}:
        return None
    width, height = struct.unpack("<HH", data[6:10])
    return int(width), int(height)


def probe_image(path: Path | str) -> dict[str, Any]:
    """返回 ``{ok, format, width, height, error}``；不可读/不认识时 ``ok=False``。"""
    target = Path(path)
    if not target.is_file():
        return {"ok": False, "format": "missing", "width": 0, "height": 0, "error": "文件不存在"}
    try:
        data = target.read_bytes()
    except OSError as error:
        return {"ok": False, "format": "unreadable", "width": 0, "height": 0, "error": str(error)}
    if not data:
        return {"ok": False, "format": "empty", "width": 0, "height": 0, "error": "空文件"}

    for name, probe in (
        ("png", _png_size),
        ("jpeg", _jpeg_size),
        ("webp", _webp_size),
        ("gif", _gif_size),
    ):
        try:
            size = probe(data)
        except (struct.error, ValueError, IndexError) as error:
            return {"ok": False, "format": name, "width": 0, "height": 0, "error": f"解析失败：{error}"}
        if size:
            width, height = size
            if width <= 0 or height <= 0:
                return {"ok": False, "format": name, "width": 0, "height": 0, "error": "尺寸非法"}
            return {"ok": True, "format": name, "width": width, "height": height, "error": None}
    return {"ok": False, "format": "unknown", "width": 0, "height": 0, "error": "无法识别的图片格式"}


def write_solid_png(
    path: Path | str,
    width: int,
    height: int,
    *,
    rgb: tuple[int, int, int] = (242, 240, 236),
    accent_rgb: tuple[int, int, int] = (200, 60, 70),
) -> Path:
    """写一张尺寸正确的 PNG（中间三分之一用强调色），用于打通流程的占位图。"""
    if width <= 0 or height <= 0:
        raise ValueError("宽高必须为正整数")
    accent_start = height // 3
    accent_end = (height * 2) // 3
    accent_row = bytes(accent_rgb) * width
    base_row = bytes(rgb) * width
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type 0
        raw.extend(accent_row if accent_start <= y < accent_end else base_row)
    compressed = zlib.compress(bytes(raw), 6)

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8bit truecolor RGB
    payload = PNG_SIGNATURE + chunk(b"IHDR", ihdr) + chunk(b"IDAT", compressed) + chunk(b"IEND", b"")
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return target


def aspect_ratio_text(width: int, height: int) -> str:
    if not width or not height:
        return "unknown"
    from math import gcd

    divisor = gcd(width, height)
    return f"{width // divisor}:{height // divisor}"


def is_supported_suffix(path: Path | str) -> bool:
    return Path(path).suffix.lower() in _IMAGE_SUFFIXES
