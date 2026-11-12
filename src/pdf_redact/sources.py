"""Turn the uploaded files into PyMuPDF documents.

Every upload is sniffed by magic bytes (names and declared types are not trusted) and becomes a
`Source`: one PDF document with one or more pages. Sources are kept separate instead of being merged
right away, because merging with `insert_pdf` drops document-level state (notably the optional content
configuration, which decides what a viewer shows), and the page-by-page results must reflect what the
owner saw. The merge happens once, at the end, on already processed pages.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

import pymupdf
from PIL import Image, ImageOps, UnidentifiedImageError

from .errors import ApiError

# A decompression bomb should fail fast; a 100 MP scan is still far beyond any real document.
Image.MAX_IMAGE_PIXELS = 100_000_000

_DEFAULT_IMAGE_DPI = 300.0
_EXIF_ORIENTATION = 0x0112


@dataclass
class Source:
    index: int  # 0-based index of the input file
    doc: pymupdf.Document
    first_page: int  # 0-based global number of the first page
    encrypted: bool  # PDF encryption without a user password (opened with the empty password)
    has_ocg: bool

    @property
    def page_count(self) -> int:
        return int(self.doc.page_count)


def sniff(data: bytes) -> str | None:
    if b"%PDF-" in data[:1024]:  # the spec allows junk before the header
        return "pdf"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    return None


def _open_pdf(data: bytes) -> pymupdf.Document:
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise ApiError(422, "cannot read PDF") from exc
    if doc.needs_pass:
        doc.close()
        raise ApiError(422, "encrypted PDF (password required)")
    if not doc.is_pdf or doc.page_count == 0:
        doc.close()
        raise ApiError(422, "cannot read PDF")
    return doc


def _flatten(img: Image.Image) -> Image.Image:
    """Reduce to a mode PNG and PyMuPDF handle without surprises."""
    if img.mode in ("1", "L", "RGB"):
        return img
    if img.mode.startswith("I;16") or img.mode in ("I", "F"):
        return img.point(lambda v: v / 256).convert("L")
    if img.mode in ("LA", "PA", "RGBA") or "transparency" in img.info:
        background = Image.new("RGB", img.size, "white")
        rgba = img.convert("RGBA")
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return img.convert("RGB")


def _dpi(img: Image.Image) -> float:
    raw = img.info.get("dpi")
    try:
        value = float(raw[0]) if raw else 0.0
    except (TypeError, ValueError, IndexError):
        value = 0.0
    return value if 50.0 <= value <= 1200.0 else _DEFAULT_IMAGE_DPI


def _open_image(data: bytes, kind: str) -> pymupdf.Document:
    doc = pymupdf.open()
    try:
        with Image.open(io.BytesIO(data)) as img:
            frames = getattr(img, "n_frames", 1) if kind == "tiff" else 1
            for number in range(frames):
                img.seek(number)
                dpi = _dpi(img)
                oriented = img.getexif().get(_EXIF_ORIENTATION, 1) in (0, 1)
                if kind == "jpeg" and oriented and img.mode in ("L", "RGB"):
                    # Untouched JPEG: embed the original bytes, no recompression of a photo or scan.
                    stream, size = data, img.size
                else:
                    frame = _flatten(ImageOps.exif_transpose(img))  # honours the orientation tag
                    buf = io.BytesIO()
                    if kind == "jpeg" and frame.mode in ("L", "RGB"):
                        frame.save(buf, format="JPEG", quality=92)
                    else:
                        frame.save(buf, format="PNG")
                    stream, size = buf.getvalue(), frame.size
                page = doc.new_page(width=size[0] * 72.0 / dpi, height=size[1] * 72.0 / dpi)
                page.insert_image(page.rect, stream=stream)
    except ApiError:
        doc.close()
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError, EOFError) as exc:
        doc.close()
        raise ApiError(422, "cannot read image") from exc
    return doc


def load_sources(files: list[bytes], max_pages: int) -> list[Source]:
    if not files:
        raise ApiError(400, "no file part")
    sources: list[Source] = []
    total = 0
    try:
        for index, data in enumerate(files):
            kind = sniff(data)
            if kind is None:
                raise ApiError(415, "unsupported file type")
            doc = _open_pdf(data) if kind == "pdf" else _open_image(data, kind)
            total += doc.page_count
            if total > max_pages:
                doc.close()
                raise ApiError(413, "too many pages")
            # Authenticated with the empty password, so `is_encrypted` is already False; the metadata knows.
            encrypted = bool(doc.is_encrypted or (doc.metadata or {}).get("encryption"))
            has_ocg = bool(doc.get_ocgs())
            sources.append(Source(index, doc, total - doc.page_count, encrypted, has_ocg))
    except BaseException:
        close_all(sources)
        raise
    return sources


def close_all(sources: list[Source]) -> None:
    for source in sources:
        if not source.doc.is_closed:
            source.doc.close()


def locate(sources: list[Source], page: int) -> tuple[Source, int]:
    """1-based global page number -> (source, 0-based page number inside it)."""
    for source in sources:
        if source.first_page < page <= source.first_page + source.page_count:
            return source, page - source.first_page - 1
    raise ApiError(400, "page out of range")
