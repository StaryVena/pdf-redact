"""Conversion between the API frame and PyMuPDF's page frame.

The API frame is the VISIBLE page as a viewer shows it: after CropBox and /Rotate, origin top-left,
x right, y down, relative 0..1. That is exactly the frame of the `/v1/render` image.

PyMuPDF has two frames for one page:
  * the "visible" frame: `page.rect`, `page.rotation_matrix` maps the other frame into it;
  * the "unrotated" frame: origin at the top-left of the CropBox, /Rotate not applied. Text
    extraction (`get_text`), redaction annotations and the drawing helpers all work in this one.
So a box from the API goes through `page.derotation_matrix`, and anything read from the text layer
goes through `page.rotation_matrix` before it is divided by the page size.

Trap: `page.transformation_matrix` (used internally by the drawing helpers) is wrong on rotated pages
that also have a CropBox offset, so shapes drawn on such a page land outside it. `unrotated()` sets
/Rotate to 0 for the duration of the drawing, where the helpers are correct, and puts it back.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pymupdf


def visible_size(page: pymupdf.Page) -> tuple[float, float]:
    rect = page.rect
    return rect.width, rect.height


def rel_to_unrotated(rel: tuple[float, float, float, float], page: pymupdf.Page) -> pymupdf.Rect:
    """Relative visible rectangle -> rectangle in the unrotated frame (for redaction and drawing)."""
    width, height = visible_size(page)
    visible = pymupdf.Rect(rel[0] * width, rel[1] * height, rel[2] * width, rel[3] * height)
    rect: pymupdf.Rect = visible * page.derotation_matrix
    return rect


def rel_to_visible_pt(rel: tuple[float, float, float, float], page: pymupdf.Page) -> pymupdf.Rect:
    width, height = visible_size(page)
    return pymupdf.Rect(rel[0] * width, rel[1] * height, rel[2] * width, rel[3] * height)


def unrotated_to_rel(rect: pymupdf.Rect | tuple[float, ...], page: pymupdf.Page) -> list[float]:
    """Rectangle from the text layer (unrotated frame) -> relative visible rectangle, clamped to 0..1."""
    width, height = visible_size(page)
    visible = pymupdf.Rect(rect) * page.rotation_matrix
    return [
        min(1.0, max(0.0, visible.x0 / width)),
        min(1.0, max(0.0, visible.y0 / height)),
        min(1.0, max(0.0, visible.x1 / width)),
        min(1.0, max(0.0, visible.y1 / height)),
    ]


def point_to_unrotated(x: float, y: float, page: pymupdf.Page) -> pymupdf.Point:
    point: pymupdf.Point = pymupdf.Point(x, y) * page.derotation_matrix
    return point


@contextmanager
def unrotated(page: pymupdf.Page) -> Iterator[int]:
    """Draw in the unrotated frame; yields the page's /Rotate (the angle upright text must be turned by)."""
    rotation = page.rotation
    page.set_rotation(0)
    try:
        yield rotation
    finally:
        page.set_rotation(rotation)
