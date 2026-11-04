"""Identify an image's real format and pixel size from its first bytes.

Used to check uploaded objects before a photo is published. It reads headers
only: it confirms the bytes start like the claimed format and reports the
dimensions the header declares. It does not decode pixels, so it can't prove
the rest of the file is intact.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

HEADER_BYTES = 64 * 1024
EXTENDED_HEADER_BYTES = 1024 * 1024

_HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1"}
_AVIF_BRANDS = {b"avif", b"avis"}
# SOF markers carry the frame size; C4 (DHT), C8 (JPG ext) and CC (DAC) don't.
_JPEG_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


@dataclass(frozen=True)
class ImageHeader:
    content_type: str
    width: int | None = None
    height: int | None = None


def sniff(data: bytes) -> ImageHeader | None:
    if data[:3] == b"\xff\xd8\xff":
        return _jpeg(data)
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        if data[12:16] == b"IHDR" and len(data) >= 24:
            w, h = struct.unpack(">II", data[16:24])
            return ImageHeader("image/png", w, h)
        return ImageHeader("image/png")
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        w, h = struct.unpack("<HH", data[6:10])
        return ImageHeader("image/gif", w, h)
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _webp(data)
    if data[4:8] == b"ftyp":
        return _isobmff(data)
    return None


def _jpeg(data: bytes) -> ImageHeader:
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            break
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker in (0x01, *range(0xD0, 0xD9)):  # standalone markers
            i += 2
            continue
        (seg_len,) = struct.unpack(">H", data[i + 2 : i + 4])
        if marker in _JPEG_SOF and i + 9 <= len(data):
            h, w = struct.unpack(">HH", data[i + 5 : i + 9])
            return ImageHeader("image/jpeg", w, h)
        i += 2 + seg_len
    return ImageHeader("image/jpeg")


def _webp(data: bytes) -> ImageHeader:
    chunk = data[12:16]
    if chunk == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
        w, h = struct.unpack("<HH", data[26:30])
        return ImageHeader("image/webp", w & 0x3FFF, h & 0x3FFF)
    if chunk == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
        (bits,) = struct.unpack("<I", data[21:25])
        return ImageHeader("image/webp", (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
    if chunk == b"VP8X" and len(data) >= 30:
        w = int.from_bytes(data[24:27], "little") + 1
        h = int.from_bytes(data[27:30], "little") + 1
        return ImageHeader("image/webp", w, h)
    return ImageHeader("image/webp")


def _isobmff(data: bytes) -> ImageHeader | None:
    (box_len,) = struct.unpack(">I", data[0:4])
    brands = {data[8:12]}
    brands.update(data[i : i + 4] for i in range(16, min(box_len, len(data)) - 3, 4))
    if brands & _AVIF_BRANDS:
        return ImageHeader("image/avif")
    if brands & _HEIF_BRANDS:
        return ImageHeader("image/heic")
    return None


def matches(header: ImageHeader | None, content_type: str) -> bool:
    """Whether sniffed bytes agree with a declared Content-Type."""
    if header is None:
        return False
    if content_type in ("image/heic", "image/heif"):
        return header.content_type == "image/heic"
    return header.content_type == content_type
