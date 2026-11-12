"""The PDF operations: inspect, render, redact, preview.

PyMuPDF is not thread-safe, so every job takes `JOB_LOCK`: one job at a time per process. Everything
happens in memory (documents open from bytes, results come out of `tobytes`). Nothing here logs or
returns file names, labels or coordinates beyond what the caller asked for.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass

import pymupdf

from .geometry import (
    point_to_unrotated,
    rel_to_unrotated,
    rel_to_visible_pt,
    unrotated,
    unrotated_to_rel,
    visible_size,
)
from .sources import Source, close_all, locate
from .spec import Box, Spec

JOB_LOCK = threading.Lock()

MAX_RENDER_PIXELS = 35_000_000
RASTER_DPI = 300
PREVIEW_RASTER_DPI = 150
LABEL_MAX_PT = 11.0
LABEL_MIN_PT = 4.0
LABEL_HEIGHT_FRACTION = 0.75
LABEL_PAD = 1.0  # horizontal breathing room inside the box, in pt
CAP_HEIGHT_HALF = 0.36  # half the cap height of DejaVu Sans in em: centres digits and capitals optically
FONT_NAME = "DejaVuSans"

BLACK = (0.0, 0.0, 0.0)
WHITE = (1.0, 1.0, 1.0)
RED = (1.0, 0.0, 0.0)

# Catalog and page keys that can carry content or identity after redaction. Dropping a key from the
# catalog orphans what it pointed to; the garbage collection on save then removes the objects.
_CATALOG_KEYS = (
    "Metadata", "Outlines", "Names", "OpenAction", "AA", "AcroForm", "PageLabels", "StructTreeRoot",
    "MarkInfo", "Threads", "PieceInfo", "OCProperties", "Perms", "Collection", "URI", "SpiderInfo",
    "Dests", "ViewerPreferences", "Info",
)  # fmt: skip
_PAGE_KEYS = ("Metadata", "PieceInfo", "Thumb", "AA", "StructParents", "B", "Annots", "Tabs")


# --- common ------------------------------------------------------------------------------------


def render_scale(page: pymupdf.Page, dpi: float) -> float:
    """dpi/72, lowered so that the bitmap stays under the pixel cap (large drawings exist)."""
    scale = dpi / 72.0
    width, height = visible_size(page)
    if width * height * scale * scale > MAX_RENDER_PIXELS:
        scale = math.sqrt(MAX_RENDER_PIXELS / (width * height))
        # The bitmap is rounded up to whole pixels; stay under the cap after rounding too.
        while math.ceil(width * scale) * math.ceil(height * scale) > MAX_RENDER_PIXELS:
            scale *= 0.9999
    return scale


def _pixmap(page: pymupdf.Page, dpi: float, gray: bool) -> pymupdf.Pixmap:
    scale = render_scale(page, dpi)
    return page.get_pixmap(
        matrix=pymupdf.Matrix(scale, scale),
        colorspace=pymupdf.csGRAY if gray else pymupdf.csRGB,
        alpha=False,
        annots=True,
    )


def _rasterize_page(doc: pymupdf.Document, pno: int, dpi: float) -> None:
    """Replace a page by an image of its visible state: same visible size, no text layer, no rotation."""
    page = doc[pno]
    width, height = visible_size(page)
    png = _pixmap(page, dpi, gray=False).tobytes("png")
    fresh = doc.new_page(pno=pno, width=width, height=height)
    fresh.insert_image(fresh.rect, stream=png)
    doc.delete_page(pno + 1)


# --- inspect -----------------------------------------------------------------------------------


def inspect(sources: list[Source]) -> dict[str, object]:
    pages: list[dict[str, object]] = []
    annotations = attachments = 0
    try:
        for source in sources:
            doc = source.doc
            attachments += doc.embfile_count()
            for pno in range(doc.page_count):
                page = doc[pno]
                annotations += sum(1 for _ in page.annots()) + sum(1 for _ in page.widgets())
                words = []
                chars = 0
                for x0, y0, x1, y1, text, *_ in page.get_text("words"):
                    rel = unrotated_to_rel((x0, y0, x1, y1), page)
                    chars += len("".join(text.split()))
                    if rel[0] >= rel[2] or rel[1] >= rel[3]:
                        continue  # outside the visible page (CropBox): not shown, so not reported
                    words.append({"text": text, "rect": rel})
                width, height = visible_size(page)
                pages.append(
                    {
                        "page": source.first_page + pno + 1,
                        "file": source.index,
                        "width": width,
                        "height": height,
                        "rotation": page.rotation,
                        "text_chars": chars,
                        "words": words,
                    }
                )
        return {
            "pages": pages,
            "encrypted": sum(1 for s in sources if s.encrypted),
            "optional_content": sum(1 for s in sources if s.has_ocg),
            "annotations": annotations,
            "attachments": attachments,
        }
    finally:
        close_all(sources)


# --- render ------------------------------------------------------------------------------------


def render(sources: list[Source], page_no: int, dpi: float, gray: bool) -> bytes:
    try:
        source, pno = locate(sources, page_no)
        png: bytes = _pixmap(source.doc[pno], dpi, gray).tobytes("png")
        return png
    finally:
        close_all(sources)


# --- labels ------------------------------------------------------------------------------------


@dataclass
class _Label:
    point: pymupdf.Point  # baseline start, unrotated frame
    size: float
    text: str


def _fit_label(font: pymupdf.Font, box: pymupdf.Rect, text: str) -> float | None:
    """Font size for a label centred in a box: up to min(0.75 * height, 11 pt), down to 4 pt, else None."""
    unit = font.text_length(text, fontsize=1.0)
    if unit <= 0:
        return None
    size = min(LABEL_HEIGHT_FRACTION * box.height, LABEL_MAX_PT, (box.width - 2 * LABEL_PAD) / unit)
    return math.floor(size * 10) / 10 if size >= LABEL_MIN_PT else None


def _centred_label(font: pymupdf.Font, page: pymupdf.Page, box: Box) -> tuple[bool, _Label | None]:
    """(fits, label). An empty label is not a failure and yields (True, None)."""
    if not box.label:
        return True, None
    visible = rel_to_visible_pt(box.rect, page)
    size = _fit_label(font, visible, box.label)
    if size is None:
        return False, None
    width = font.text_length(box.label, fontsize=size)
    centre = (visible.x0 + visible.x1) / 2, (visible.y0 + visible.y1) / 2
    start = point_to_unrotated(centre[0] - width / 2, centre[1] + CAP_HEIGHT_HALF * size, page)
    return True, _Label(start, size, box.label)


def _write_labels(
    page: pymupdf.Page,
    labels: list[_Label],
    font_path: str,
    color: tuple[float, float, float],
    rotation: int,
) -> None:
    """Upright text on a possibly rotated page: written in the unrotated frame, turned by /Rotate.

    Must be called inside `unrotated(page)`.
    """
    if not labels:
        return
    page.insert_font(fontname=FONT_NAME, fontfile=font_path)
    # One shape, one commit: committing per label rewrites the content stream every time (quadratic).
    shape = page.new_shape()
    for label in labels:
        shape.insert_text(
            label.point,
            label.text,
            fontsize=label.size,
            fontname=FONT_NAME,
            color=color,
            rotate=rotation,
        )
    shape.commit()


# --- redact ------------------------------------------------------------------------------------


@dataclass
class RedactResult:
    pdf: bytes
    pages: int
    boxes: int
    rasterized: list[int]
    labels_skipped: int


def _axis_aligned(a: pymupdf.Point, b: pymupdf.Point) -> bool:
    return abs(a.x - b.x) < 1e-3 or abs(a.y - b.y) < 1e-3


def _partial_art_survives(page: pymupdf.Page, rects: list[pymupdf.Rect]) -> bool:
    """Does vector art that is only partly inside a box survive under the fill?

    MuPDF removes a path only when it lies entirely inside the box (that keeps table rules). A path
    that merely crosses the box stays in the content stream, and its part under the fill can be
    recovered by deleting the fill. Axis-aligned lines and rectangles (borders, cell backgrounds,
    our own black fills) are ignored: their hidden part reveals nothing. A curve, a slanted line or
    a slanted quad (a baked ink signature, text converted to outlines) triggers a rasterization.
    Call inside `unrotated(page)`: drawings come in the unrotated frame.
    """
    for drawing in page.get_drawings():
        bbox = drawing["rect"]
        if not any(
            bbox.x0 <= r.x1 and bbox.x1 >= r.x0 and bbox.y0 <= r.y1 and bbox.y1 >= r.y0 for r in rects
        ):
            continue
        for item in drawing["items"]:
            kind = item[0]
            if kind == "c":
                return True
            if kind == "l" and not _axis_aligned(item[1], item[2]):
                return True
            if kind == "qu":
                q = item[1]
                if not (_axis_aligned(q.ul, q.ur) and _axis_aligned(q.ur, q.lr)):
                    return True
    return False


def _redact_source(
    source: Source, boxes: dict[int, list[Box]], raster: set[int], font: pymupdf.Font, font_path: str
) -> tuple[int, int]:
    """Redact one input in place. Returns (boxes applied, labels skipped).

    `raster` holds local page numbers and grows by the pages that need a bitmap because partly
    covered vector art survived.
    """
    doc = source.doc
    # Annotations and form fields first: what the owner saw rendered is what gets redacted.
    doc.bake(annots=True, widgets=True)
    skipped = applied = 0
    for pno, page_boxes in sorted(boxes.items()):
        page = doc[pno]
        labels: list[_Label] = []
        rects = [rel_to_unrotated(box.rect, page) for box in page_boxes]
        for box in page_boxes:
            fits, label = _centred_label(font, page, box)
            if not fits:
                skipped += 1
            if label:
                labels.append(label)
        # Annotation placement and applying go through the same faulty matrix as the drawing helpers
        # (see geometry.py), so the whole block runs with /Rotate 0.
        with unrotated(page) as rotation:
            for rect in rects:
                page.add_redact_annot(rect, fill=BLACK, cross_out=False)
            # images=2 blanks the pixels under the box (scans are images); graphics=1 removes a vector
            # shape only when it lies entirely inside the box, so table lines that merely touch survive.
            page.apply_redactions(
                images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
                graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                text=pymupdf.PDF_REDACT_TEXT_REMOVE,
            )
            _write_labels(page, labels, font_path, WHITE, rotation)
            if _partial_art_survives(page, rects):
                raster.add(pno)  # after the labels, so the bitmap carries box and label
        applied += len(page_boxes)
    for pno in sorted(raster):
        _rasterize_page(doc, pno, RASTER_DPI)
    return applied, skipped


def _sanitise(doc: pymupdf.Document) -> None:
    """Strip everything that is not page content. `doc` is the freshly merged output."""
    # Hidden text (render mode 3, e.g. a scanner's invisible OCR layer) is removed by scrub as well
    # as links, thumbnails, form field values and JavaScript; what scrub leaves is dropped below.
    doc.scrub()
    for page in doc.pages():
        while (annot := page.first_annot) is not None:
            page.delete_annot(annot)
        for link in page.get_links():
            page.delete_link(link)
        for key in _PAGE_KEYS:
            doc.xref_set_key(page.xref, key, "null")
    for name in doc.embfile_names():
        doc.embfile_del(name)
    doc.set_metadata({})
    doc.del_xml_metadata()
    doc.set_toc([])
    root = doc.pdf_catalog()
    for key in _CATALOG_KEYS:
        doc.xref_set_key(root, key, "null")
    if doc.xref_get_key(-1, "Info")[0] != "null":
        doc.xref_set_key(-1, "Info", "null")


def redact(sources: list[Source], spec: Spec, font_path: str) -> RedactResult:
    out = pymupdf.open()
    try:
        font = pymupdf.Font(fontfile=font_path)
        rasterized: list[int] = []
        applied = skipped = 0
        for source in sources:
            first, last = source.first_page, source.first_page + source.page_count
            boxes: dict[int, list[Box]] = {}
            for box in spec.boxes:
                if first < box.page <= last:
                    boxes.setdefault(box.page - first - 1, []).append(box)
            raster = {p - first - 1 for p in spec.raster_pages if first < p <= last}
            if source.has_ocg:
                # Hidden layers could keep content the owner never saw; a bitmap cannot.
                raster = set(range(source.page_count))
            done, miss = _redact_source(source, boxes, raster, font, font_path)
            rasterized += [first + pno + 1 for pno in sorted(raster)]
            applied += done
            skipped += miss
            out.insert_pdf(source.doc, links=False, annots=False, widgets=False)
            source.doc.close()
        _sanitise(out)
        pdf = out.tobytes(garbage=4, clean=True, deflate=True)
        return RedactResult(pdf, out.page_count, applied, sorted(set(rasterized)), skipped)
    finally:
        out.close()
        close_all(sources)


# --- preview -----------------------------------------------------------------------------------


def _draw_preview(page: pymupdf.Page, boxes: list[Box], font: pymupdf.Font, font_path: str) -> None:
    width, height = visible_size(page)
    shapes: list[tuple[pymupdf.Rect, pymupdf.Rect, _Label]] = []
    for box in boxes:
        visible = rel_to_visible_pt(box.rect, page)
        text = f"{box.number} {box.label}".rstrip()
        size = 7.0
        tag_w = font.text_length(text, fontsize=size) + 4.0
        tag_h = size * 1.4
        # Above the box so that the content under it stays visible; inside the top edge at the page top.
        top = visible.y0 - tag_h if visible.y0 - tag_h >= 0 else visible.y0
        left = min(visible.x0, max(0.0, width - tag_w))
        tag = pymupdf.Rect(left, top, min(width, left + tag_w), min(height, top + tag_h))
        start = point_to_unrotated(tag.x0 + 2.0, tag.y0 + tag_h / 2 + CAP_HEIGHT_HALF * size, page)
        shapes.append(
            (
                visible * page.derotation_matrix,
                tag * page.derotation_matrix,
                _Label(start, size, text),
            )
        )
    with unrotated(page) as rotation:
        page.insert_font(fontname=FONT_NAME, fontfile=font_path)
        shape = page.new_shape()  # one commit for all boxes: per-call drawing is quadratic
        for outline, tag, label in shapes:
            shape.draw_rect(outline)
            shape.finish(color=RED, fill=None, width=1.5)
            shape.draw_rect(tag)
            shape.finish(color=RED, fill=RED, width=0)
            shape.insert_text(
                label.point, label.text, fontsize=label.size, fontname=FONT_NAME, color=WHITE, rotate=rotation
            )
        shape.commit()


def preview(sources: list[Source], spec: Spec, font_path: str) -> bytes:
    out = pymupdf.open()
    try:
        font = pymupdf.Font(fontfile=font_path)
        for source in sources:
            doc = source.doc
            first, last = source.first_page, source.first_page + source.page_count
            if source.has_ocg:
                # Merging drops the layer configuration; a bitmap shows what the owner saw.
                for pno in range(doc.page_count):
                    _rasterize_page(doc, pno, PREVIEW_RASTER_DPI)
            by_page: dict[int, list[Box]] = {}
            for box in spec.boxes:
                if first < box.page <= last:
                    by_page.setdefault(box.page - first - 1, []).append(box)
            for pno, boxes in by_page.items():
                _draw_preview(doc[pno], boxes, font, font_path)
            out.insert_pdf(doc)
            doc.close()
        data: bytes = out.tobytes(garbage=3, deflate=True)
        return data
    finally:
        out.close()
        close_all(sources)
