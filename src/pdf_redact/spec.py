"""The `spec` form field of /v1/redact and /v1/preview, validated."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

from .errors import ApiError

MAX_BOXES = 5000
MAX_LABEL = 64


@dataclass(frozen=True)
class Box:
    page: int  # 1-based, global
    rect: tuple[float, float, float, float]  # relative to the visible page
    label: str
    number: int  # 1-based index in spec.boxes


@dataclass
class Spec:
    boxes: list[Box] = field(default_factory=list)
    raster_pages: set[int] = field(default_factory=set)


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _is_page(value: object, total: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= total


def parse_spec(raw: str | None, total_pages: int) -> Spec:
    """Messages name the box index only; they never repeat what the caller sent."""
    if raw is None:
        raise ApiError(400, "missing spec")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ApiError(400, "spec is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ApiError(400, "spec must be an object")
    raw_boxes = data.get("boxes", [])
    if not isinstance(raw_boxes, list):
        raise ApiError(400, "spec.boxes must be a list")
    if len(raw_boxes) > MAX_BOXES:
        raise ApiError(400, f"too many boxes (max {MAX_BOXES})")
    spec = Spec()
    for n, item in enumerate(raw_boxes, start=1):
        if not isinstance(item, dict):
            raise ApiError(400, f"box {n}: must be an object")
        page = item.get("page")
        if not _is_page(page, total_pages):
            raise ApiError(400, f"box {n}: page out of range")
        rect = item.get("rect")
        if not (isinstance(rect, list) and len(rect) == 4 and all(_is_number(v) for v in rect)):
            raise ApiError(400, f"box {n}: rect must be four numbers")
        x0, y0, x1, y1 = (float(v) for v in rect)
        if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
            raise ApiError(400, f"box {n}: rect must satisfy 0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1")
        label = item.get("label", "")
        if not isinstance(label, str) or len(label) > MAX_LABEL:
            raise ApiError(400, f"box {n}: label must be a string of at most {MAX_LABEL} characters")
        assert isinstance(page, int)
        spec.boxes.append(Box(page, (x0, y0, x1, y1), label, n))
    raw_raster = data.get("raster_pages", [])
    if not isinstance(raw_raster, list):
        raise ApiError(400, "spec.raster_pages must be a list")
    for value in raw_raster:
        if not _is_page(value, total_pages):
            raise ApiError(400, "raster_pages: page out of range")
        assert isinstance(value, int)
        spec.raster_pages.add(value)
    return spec
