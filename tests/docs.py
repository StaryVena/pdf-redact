"""Synthetic fixture documents. All names and addresses are fictional."""

from __future__ import annotations

import io
import json
from typing import Any

import pymupdf
from PIL import Image, ImageDraw, ImageFont

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

# (text, x, y) in the unrotated frame of a 400 x 500 pt page, y = baseline. The targets end their
# line: MuPDF does not keep the advance of removed glyphs, so text after a redaction moves left.
LINES = [
    ("Podepsal Jan Novák", 60, 100),
    ("Bydliště Lipová 12", 60, 140),
    ("Marie Dvořáková", 60, 180),
    ("Veřejný text zůstává", 60, 300),
]


def dump(doc: pymupdf.Document, **kwargs: Any) -> bytes:
    data: bytes = doc.tobytes(**kwargs)
    return data


def text_pdf(
    rotation: int = 0,
    cropbox: tuple[float, float, float, float] | None = None,
    lines: list[tuple[str, float, float]] | None = None,
    size: tuple[float, float] = (400, 500),
    fontsize: float = 14,
) -> bytes:
    doc = pymupdf.open()
    page = doc.new_page(width=size[0], height=size[1])
    page.insert_font(fontname="dj", fontfile=FONT)
    for text, x, y in lines or LINES:
        page.insert_text((x, y), text, fontname="dj", fontsize=fontsize)
    if cropbox:
        page.set_cropbox(pymupdf.Rect(*cropbox))
    page.set_rotation(rotation)
    return dump(doc)


def offset_mediabox_pdf(rotation: int) -> bytes:
    """MediaBox not at the origin and a CropBox inside it, set as raw PDF keys."""
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    page.insert_font(fontname="dj", fontfile=FONT)
    for i, word in enumerate(["Jan", "Novák", "Lipová", "Veřejný"]):
        page.insert_text((300, 300 + 40 * i), word, fontname="dj", fontsize=16)
    doc.xref_set_key(page.xref, "MediaBox", "[100 100 700 900]")
    doc.xref_set_key(page.xref, "CropBox", "[200 200 650 650]")
    doc.xref_set_key(page.xref, "Rotate", str(rotation))
    return dump(doc)


def scan_image(text: str = "Jan Novák, Lipová 12", size: tuple[int, int] = (800, 400)) -> Image.Image:
    img = Image.new("L", size, 255)
    ImageDraw.Draw(img).text((40, 150), text, fill=0, font=ImageFont.truetype(FONT, 48))
    return img


def scan_pdf(rotation: int = 0) -> bytes:
    """Image-only page, as a scanner produces it (no text layer)."""
    buf = io.BytesIO()
    scan_image().save(buf, format="PNG")
    doc = pymupdf.open()
    page = doc.new_page(width=800, height=400)
    page.insert_image(page.rect, stream=buf.getvalue())
    page.set_rotation(rotation)
    return dump(doc)


def jpeg_with_orientation(orientation: int) -> bytes:
    """400 x 200 image, red block in the top-left corner, EXIF orientation set."""
    img = Image.new("RGB", (400, 200), "white")
    ImageDraw.Draw(img).rectangle((0, 0, 59, 59), fill=(255, 0, 0))
    exif = Image.Exif()
    exif[0x0112] = orientation
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95, exif=exif)
    return buf.getvalue()


def png_bytes() -> bytes:
    buf = io.BytesIO()
    scan_image().convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def tiff_bytes(frames: int) -> bytes:
    images = [Image.new("L", (300 + 50 * i, 200), 255 - 40 * i) for i in range(frames)]
    buf = io.BytesIO()
    images[0].save(buf, format="TIFF", save_all=True, append_images=images[1:])
    return buf.getvalue()


def spec(boxes: list[dict[str, object]], raster_pages: list[int] | None = None) -> str:
    data: dict[str, object] = {"boxes": boxes}
    if raster_pages is not None:
        data["raster_pages"] = raster_pages
    return json.dumps(data)


def box(page: int, rect: list[float], label: str = "") -> dict[str, object]:
    return {"page": page, "rect": rect, "label": label}
