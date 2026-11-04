import io
import struct

import pytest
from conftest import image_bytes
from PIL import Image

from app.imagesniff import ImageHeader, matches, sniff


@pytest.mark.parametrize("fmt, mime, kwargs", [
    ("JPEG", "image/jpeg", {"quality": 90}),
    ("JPEG", "image/jpeg", {"progressive": True}),
    ("PNG", "image/png", {}),
    ("GIF", "image/gif", {}),
    ("WEBP", "image/webp", {"quality": 85}),          # lossy: VP8
    ("WEBP", "image/webp", {"lossless": True}),       # VP8L
])
def test_reads_format_and_size(fmt, mime, kwargs):
    assert sniff(image_bytes(fmt, 637, 421, **kwargs)) == ImageHeader(mime, 637, 421)


def test_webp_extended_header_with_alpha():
    img = Image.new("RGBA", (300, 200), (0, 0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, "WEBP", exif=b"Exif\x00\x00II*\x00\x08\x00\x00\x00\x00\x00")
    data = buf.getvalue()
    assert data[12:16] == b"VP8X"
    assert sniff(data) == ImageHeader("image/webp", 300, 200)


def test_jpeg_with_large_exif_before_frame_header():
    img = Image.new("RGB", (800, 600))
    exif = img.getexif()
    exif[0x010E] = "x" * 30000  # ImageDescription, pushes SOF ~30 KB in
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    assert sniff(buf.getvalue()[:64 * 1024]) == ImageHeader("image/jpeg", 800, 600)


def test_heic_and_avif_brands():
    def ftyp(major, *compat):
        body = major + b"\x00\x00\x00\x00" + b"".join(compat)
        return struct.pack(">I", 8 + len(body)) + b"ftyp" + body + b"\x00" * 16
    assert sniff(ftyp(b"heic", b"mif1", b"heic")).content_type == "image/heic"
    assert sniff(ftyp(b"mif1", b"miaf", b"MiHB")).content_type == "image/heic"
    assert sniff(ftyp(b"avif", b"mif1", b"miaf")).content_type == "image/avif"
    assert sniff(ftyp(b"isom", b"mp41")) is None  # an MP4 is not an image


def test_garbage_and_html_are_not_images():
    assert sniff(b"<html><body>hi</body></html>") is None
    assert sniff(b"") is None


def test_matches_requires_agreement():
    png = sniff(image_bytes("PNG", 10, 10))
    assert matches(png, "image/png")
    assert not matches(png, "image/webp")
    assert not matches(None, "image/jpeg")
    assert matches(ImageHeader("image/heic"), "image/heif")


def test_jpeg_frame_header_beyond_first_read():
    from app.imagesniff import EXTENDED_HEADER_BYTES, HEADER_BYTES
    jpeg = image_bytes("JPEG", 900, 700)
    # Two maximum-size APP2 segments (like iPhone MPF/ICC data) push SOF past 64 KB.
    filler = (b"\xff\xe2" + struct.pack(">H", 65535) + b"\x00" * 65533) * 2
    data = jpeg[:2] + filler + jpeg[2:]
    assert sniff(data[:HEADER_BYTES]).width is None
    assert sniff(data[:EXTENDED_HEADER_BYTES]) == ImageHeader("image/jpeg", 900, 700)
