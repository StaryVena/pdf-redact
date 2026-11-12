"""Redaction: geometry on rotated and cropped pages, labels, scans, vector graphics."""

from __future__ import annotations

import io
from collections.abc import Callable
from typing import Any

import pymupdf
import pytest
from conftest import Api
from docs import box, dump, offset_mediabox_pdf, scan_pdf, spec, text_pdf
from PIL import Image
from pix import all_black, changed_outside, extrema, ink, px

from pdf_redact.geometry import unrotated_to_rel


def pad(rect: list[float], d: float = 0.004) -> list[float]:
    return [max(0.0, rect[0] - d), max(0.0, rect[1] - d), min(1.0, rect[2] + d), min(1.0, rect[3] + d)]


def text_of(pdf: bytes) -> str:
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    return "".join(page.get_text() for page in doc.pages())


MakeDoc = Callable[[int], bytes]
CASES: list[tuple[str, MakeDoc]] = [
    ("plain", lambda r: text_pdf(r)),
    ("cropbox", lambda r: text_pdf(r, size=(600, 800), cropbox=(30, 60, 400, 500))),
    ("offset-mediabox", offset_mediabox_pdf),
]


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize(("name", "make"), CASES, ids=[c[0] for c in CASES])
def test_box_lands_on_the_word_on_every_rotation_and_crop(
    api: Api, name: str, make: MakeDoc, rotation: int
) -> None:
    pdf = make(rotation)
    target, other = ("Novák", "Veřejný")
    rect = pad(api.word(pdf, target))
    before = api.render(pdf, dpi=100)
    # The box sits on ink in the original render: the frames of inspect, render and redact agree.
    assert ink(before, rect)

    resp = api.redact(pdf, spec([box(1, rect)]))
    assert resp.status_code == 200
    out = resp.content
    after = api.render(out, dpi=100)
    assert after.size == before.size
    assert all_black(after, rect)
    assert not changed_outside(before, after, [rect])
    text = text_of(out)
    assert target not in text
    assert other in text  # the text layer outside the box survives
    assert resp.headers["X-Redact-Boxes"] == "1"
    assert resp.headers["X-Redact-Rasterized"] == "none"
    # same page geometry after the merge: size, rotation
    page = pymupdf.open(stream=out, filetype="pdf")[0]
    assert page.rotation == rotation


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_word_rects_follow_the_render(api: Api, rotation: int) -> None:
    pdf = text_pdf(rotation, size=(600, 800), cropbox=(30, 60, 400, 500))
    img = api.render(pdf, dpi=100)
    words = api.inspect(pdf)["pages"][0]["words"]
    assert len(words) >= 8
    for word in words:
        assert ink(img, word["rect"]), "the word rectangle must cover dark pixels of the render"
    # the empty gap between the lines (unrotated y 135..215 of the cropped page) has no ink
    page = pymupdf.open(stream=pdf, filetype="pdf")[0]
    assert not ink(img, unrotated_to_rel((40, 135, 300, 215), page))


def label_box(info: dict[str, Any], rects: list[list[float]]) -> list[float]:
    """Union of word rects, grown to at least 160 x 30 pt (a rotated page shows a line as a tall sliver)."""
    x0, y0 = min(r[0] for r in rects), min(r[1] for r in rects)
    x1, y1 = max(r[2] for r in rects), max(r[3] for r in rects)
    w, h = info["width"], info["height"]
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    half_w, half_h = max((x1 - x0) / 2, 80 / w), max((y1 - y0) / 2, 15 / h)
    return [max(0.0, cx - half_w), max(0.0, cy - half_h), min(1.0, cx + half_w), min(1.0, cy + half_h)]


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_labels_are_upright_inside_the_box_with_czech_glyphs(api: Api, rotation: int) -> None:
    pdf = text_pdf(rotation)
    info = api.inspect(pdf)["pages"][0]
    line = label_box(info, [api.word(pdf, "Jan"), api.word(pdf, "Novák")])
    resp = api.redact(pdf, spec([box(1, line, "[datová schránka]")]))
    assert resp.status_code == 200
    assert resp.headers["X-Redact-Labels-Skipped"] == "0"
    page = api.inspect(resp.content)["pages"][0]
    words = {w["text"]: w["rect"] for w in page["words"]}
    assert "[datová" in words
    assert "schránka]" in words
    for text in ("[datová", "schránka]"):
        r = words[text]
        assert line[0] <= r[0]
        assert r[2] <= line[2]
        assert line[1] <= r[1]
        assert r[3] <= line[3]
        # upright: wider than tall in the visible frame (turned text would be a tall sliver)
        assert (r[2] - r[0]) * page["width"] > (r[3] - r[1]) * page["height"]
    img = api.render(resp.content, dpi=150)
    lo, hi = extrema(img.crop(px(img, line, -2)))
    assert lo == 0  # black fill
    assert hi > 200  # white glyphs


def test_second_czech_label_and_text_extraction(api: Api) -> None:
    pdf = text_pdf()
    rect = [0.1, 0.55, 0.9, 0.62]
    resp = api.redact(pdf, spec([box(1, rect, "[údaj o zdraví]")]))
    assert resp.status_code == 200
    assert "[údaj o zdraví]" in " ".join(text_of(resp.content).split())


def test_tiny_box_skips_the_label_and_counts_it(api: Api) -> None:
    pdf = text_pdf()
    resp = api.redact(
        pdf, spec([box(1, [0.1, 0.5, 0.9, 0.5 + 3 / 500], "[Osoba 1]"), box(1, [0.1, 0.6, 0.5, 0.65], "")])
    )
    assert resp.status_code == 200
    assert resp.headers["X-Redact-Labels-Skipped"] == "1"
    assert resp.headers["X-Redact-Boxes"] == "2"
    assert "Osoba" not in text_of(resp.content)


def test_label_size_is_capped_at_11_pt(api: Api) -> None:
    pdf = text_pdf()
    resp = api.redact(pdf, spec([box(1, [0.05, 0.5, 0.95, 0.9], "[Osoba 1]")]))
    page = api.inspect(resp.content)["pages"][0]
    rect = next(w["rect"] for w in page["words"] if w["text"] == "[Osoba")
    assert (rect[3] - rect[1]) * page["height"] < 11 * 1.4


def test_long_label_shrinks_to_fit(api: Api) -> None:
    pdf = text_pdf()
    rect = [0.1, 0.5, 0.35, 0.56]  # 100 x 30 pt
    resp = api.redact(pdf, spec([box(1, rect, "[Marie Dvořáková]")]))
    assert resp.headers["X-Redact-Labels-Skipped"] == "0"
    page = api.inspect(resp.content)["pages"][0]
    for word in page["words"]:
        if word["text"].startswith(("[Marie", "Dvořáková]")):
            r = word["rect"]
            assert rect[0] <= r[0] and r[2] <= rect[2]


@pytest.mark.parametrize("rotation", [0, 90])
def test_scanned_page_pixels_are_blanked(api: Api, rotation: int) -> None:
    pdf = scan_pdf(rotation)
    before = api.render(pdf, dpi=72)
    rect = [0.04, 0.35, 0.6, 0.6] if rotation == 0 else [0.35, 0.04, 0.6, 0.6]
    # find the ink first: the box must cover the text of the scan
    if rotation == 90:
        rect = _rotate_rect_cw(rect)
    assert ink(before, rect)
    resp = api.redact(pdf, spec([box(1, rect)]))
    assert resp.status_code == 200
    after = api.render(resp.content, dpi=72)
    assert all_black(after, rect)
    assert not changed_outside(before, after, [rect])
    # the image itself is blanked, not just covered
    doc = pymupdf.open(stream=resp.content, filetype="pdf")
    page = doc[0]
    xref = page.get_images()[0][0]
    raw = Image.open(io.BytesIO(doc.extract_image(xref)["image"])).convert("L")
    assert extrema(raw)[0] == 0
    assert doc[0].get_text().strip() == ""


def _rotate_rect_cw(rect: list[float]) -> list[float]:
    """The scan rect in the unrotated page, as seen after a 90 degree clockwise /Rotate (visible frame)."""
    x0, y0, x1, y1 = rect
    return [1 - y1, x0, 1 - y0, x1]


def test_vector_graphics_only_removed_when_fully_covered(api: Api) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=500)
    page.insert_font(fontname="dj", fontfile="/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    page.insert_text((100, 120), "Jan Novák", fontname="dj", fontsize=14)
    page.draw_line((20, 200), (380, 200), color=(1, 0, 0), width=2)  # table rule, crosses the box
    page.draw_rect(pymupdf.Rect(110, 300, 130, 310), color=(0, 0, 1), fill=(0, 0, 1))  # inside the box
    pdf = dump(doc)
    rect = [0.2, 0.36, 0.5, 0.66]  # x 80..200, y 180..330
    resp = api.redact(pdf, spec([box(1, rect)]))
    before = api.render(pdf, dpi=72, gray=False)
    after = api.render(resp.content, dpi=72, gray=False)
    # outside the box the red rule is intact on both sides
    assert after.getpixel((40, 200))[:3] == before.getpixel((40, 200))[:3] == (255, 0, 0)  # type: ignore[index]
    assert after.getpixel((300, 200))[:3] == (255, 0, 0)  # type: ignore[index]
    assert after.getpixel((150, 250))[:3] == (0, 0, 0)  # type: ignore[index]
    result = pymupdf.open(stream=resp.content, filetype="pdf")[0]
    # the contained blue rectangle is gone, the crossing rule is still a drawing
    fills = [d for d in result.get_drawings() if d.get("fill") == (0.0, 0.0, 1.0)]
    assert not fills


def test_multi_file_page_numbers_are_global(api: Api) -> None:
    a = text_pdf(lines=[("Jan Novák", 60, 100), ("první strana", 60, 200)])
    b = text_pdf(lines=[("Marie Dvořáková", 60, 100), ("druhý soubor", 60, 200)])
    both = pymupdf.open(stream=a, filetype="pdf")
    both.insert_pdf(pymupdf.open(stream=a, filetype="pdf"))
    two_pages = dump(both)
    info = api.inspect([two_pages, b])
    assert [(p["page"], p["file"]) for p in info["pages"]] == [(1, 0), (2, 0), (3, 1)]
    rect = pad(api.word([two_pages, b], "Dvořáková", page=3))
    resp = api.redact([two_pages, b], spec([box(3, rect, "[Osoba]")]))
    assert resp.status_code == 200
    assert resp.headers["X-Redact-Pages"] == "3"
    text = text_of(resp.content)
    assert "Dvořáková" not in text
    assert text.count("Novák") == 2  # pages 1 and 2 untouched
    assert "druhý soubor" in text


def test_redacting_a_page_that_has_no_text_layer_at_all(api: Api) -> None:
    doc = pymupdf.open()
    doc.new_page(width=300, height=300)
    resp = api.redact(dump(doc), spec([box(1, [0.1, 0.1, 0.5, 0.5], "[x]")]))
    assert resp.status_code == 200
