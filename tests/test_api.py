"""HTTP surface: inputs, inspect, render, preview, validation, auth, metrics."""

from __future__ import annotations

import io
import json
from collections.abc import Callable

import pymupdf
import pytest
from conftest import Api
from docs import (
    box,
    dump,
    jpeg_with_orientation,
    png_bytes,
    scan_pdf,
    spec,
    text_pdf,
    tiff_bytes,
)
from PIL import Image
from pix import changed_outside, extrema, ink

from pdf_redact import __version__
from pdf_redact.config import Settings


def red_at(img: Image.Image, x: float, y: float, reach: int = 3) -> bool:
    """Red pixel within a few pixels of the point (a 1.5 pt line is only two or three pixels wide)."""
    rgb = img.convert("RGB")
    cx, cy = round(x * img.width), round(y * img.height)
    for px in range(max(0, cx - reach), min(img.width, cx + reach + 1)):
        for py in range(max(0, cy - reach), min(img.height, cy + reach + 1)):
            r, g, b = rgb.getpixel((px, py))  # type: ignore[misc]
            if r > 200 and g < 80 and b < 80:
                return True
    return False


# --- inputs ------------------------------------------------------------------------------------


def test_jpeg_exif_orientation_is_honoured(api: Api) -> None:
    # Orientation 6: stored 400 x 200, displayed 200 x 400 (rotated 90 degrees clockwise), so the
    # red block of the top-left corner appears in the top-right corner.
    jpeg = jpeg_with_orientation(6)
    page = api.inspect(jpeg)["pages"][0]
    assert page["width"] < page["height"]
    img = api.render(jpeg, dpi=72, gray=False)
    assert img.height > img.width
    assert red_at(img, 0.9, 0.05)
    assert not red_at(img, 0.05, 0.05)
    # and without the tag the block stays top-left
    plain = api.render(jpeg_with_orientation(1), dpi=72, gray=False)
    assert plain.width > plain.height
    assert red_at(plain, 0.05, 0.1)


def test_jpeg_is_detected_by_magic_bytes_not_by_name(api: Api) -> None:
    resp = api.post("/v1/inspect", jpeg_with_orientation(1), names=["report.pdf"])
    assert resp.status_code == 200


def test_multipage_tiff_becomes_pages(api: Api) -> None:
    info = api.inspect(tiff_bytes(3))
    assert [p["page"] for p in info["pages"]] == [1, 2, 3]
    assert info["pages"][1]["width"] > info["pages"][0]["width"]
    img = api.render(tiff_bytes(3), page=3, dpi=36)
    assert img.size[0] > 0


def test_png_scan_can_be_redacted(api: Api) -> None:
    png = png_bytes()
    rect = [0.04, 0.35, 0.6, 0.6]
    before = api.render(png, dpi=72)
    assert ink(before, rect)
    resp = api.redact(png, spec([box(1, rect, "[Osoba]")]))
    assert resp.status_code == 200
    after = api.render(resp.content, dpi=72)
    assert not ink(after, [0.7, 0.35, 0.99, 0.6])  # nothing was there
    assert not changed_outside(before, after, [rect])


def test_pdf_and_image_in_one_upload(api: Api) -> None:
    info = api.inspect([text_pdf(), jpeg_with_orientation(1), tiff_bytes(2)])
    assert [(p["page"], p["file"]) for p in info["pages"]] == [(1, 0), (2, 1), (3, 2), (4, 2)]


# --- inspect -----------------------------------------------------------------------------------


def test_inspect_reports_words_counts_and_geometry(api: Api) -> None:
    info = api.inspect(text_pdf(90))
    page = info["pages"][0]
    assert (page["width"], page["height"], page["rotation"]) == (500, 400, 90)
    assert page["text_chars"] == sum(len(w["text"]) for w in page["words"])
    assert {"Novák", "Lipová", "Veřejný"} <= {w["text"] for w in page["words"]}
    assert all(0 <= v <= 1 for w in page["words"] for v in w["rect"])
    assert info["encrypted"] == 0
    assert info["optional_content"] == 0
    assert info["annotations"] == 0
    assert info["attachments"] == 0


def test_inspect_of_a_scan_has_no_text(api: Api) -> None:
    page = api.inspect(scan_pdf())["pages"][0]
    assert page["text_chars"] == 0
    assert page["words"] == []


def test_inspect_counts_annotations_and_attachments(api: Api) -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    page.add_freetext_annot(pymupdf.Rect(50, 50, 200, 80), "x")
    doc.embfile_add("a.txt", b"abc")
    info = api.inspect(dump(doc))
    assert info["annotations"] == 1
    assert info["attachments"] == 1


def test_owner_encrypted_pdf_opens_and_is_flagged(api: Api) -> None:
    doc = pymupdf.open(stream=text_pdf(), filetype="pdf")
    data = dump(doc, encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="o", user_pw="", permissions=0)
    assert api.inspect(data)["encrypted"] == 1
    resp = api.redact(data, spec([box(1, [0.1, 0.1, 0.5, 0.2])]))
    assert resp.status_code == 200
    assert not pymupdf.open(stream=resp.content, filetype="pdf").is_encrypted


# --- render ------------------------------------------------------------------------------------


def test_render_defaults_and_colour(api: Api) -> None:
    pdf = text_pdf()
    resp = api.post("/v1/render", pdf, {"page": "1"})
    img = Image.open(io.BytesIO(resp.content))
    assert img.mode == "L"
    assert img.size == (1667, 2084)  # 400 x 500 pt at 300 dpi, rounded up
    rgb = Image.open(
        io.BytesIO(api.post("/v1/render", pdf, {"page": "1", "dpi": "72", "gray": "false"}).content)
    )
    assert rgb.mode == "RGB"
    assert rgb.size == (400, 500)


def test_render_dpi_is_clamped_at_300(api: Api) -> None:
    resp = api.post("/v1/render", text_pdf(), {"page": "1", "dpi": "600"})
    assert Image.open(io.BytesIO(resp.content)).size == (1667, 2084)


def test_render_pixel_cap_lowers_the_scale(api: Api) -> None:
    doc = pymupdf.open()
    doc.new_page(width=5000, height=5000).draw_rect(pymupdf.Rect(100, 100, 4900, 4900), fill=(0, 0, 0))
    resp = api.post("/v1/render", dump(doc), {"page": "1"})
    assert resp.status_code == 200
    width, height = Image.open(io.BytesIO(resp.content)).size
    assert width * height <= 35_000_000
    assert width * height > 33_000_000  # reduced to the cap, not further
    assert abs(width - height) <= 1


def test_render_draws_annotations(api: Api) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=200, height=100)
    page.add_freetext_annot(pymupdf.Rect(10, 10, 190, 60), "ANOTACE", fontsize=20, text_color=(0, 0, 0))
    img = api.render(dump(doc), dpi=72)
    assert extrema(img)[0] < 100


# --- preview -----------------------------------------------------------------------------------


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_preview_draws_red_outline_and_keeps_the_text(api: Api, rotation: int) -> None:
    pdf = text_pdf(rotation, size=(600, 800), cropbox=(30, 60, 400, 500))
    rect = [0.3, 0.4, 0.7, 0.6]
    resp = api.post("/v1/preview", pdf, {"spec": spec([box(1, rect, "[údaj o zdraví]")])})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    before = api.render(pdf, dpi=100, gray=False)
    after = api.render(resp.content, dpi=100, gray=False)
    assert after.size == before.size
    # on the outline at the middle of each side, red; inside the box centre, untouched
    assert red_at(after, 0.3, 0.5)
    assert red_at(after, 0.7, 0.5)
    assert red_at(after, 0.5, 0.6)
    assert not red_at(before, 0.3, 0.5)
    assert not red_at(after, 0.5, 0.5)  # nothing is drawn over the content of the box
    cx, cy = 0.5, 0.5
    assert after.convert("RGB").getpixel(
        (round(cx * after.width), round(cy * after.height))
    ) == before.convert("RGB").getpixel((round(cx * after.width), round(cy * after.height)))
    text_in = pymupdf.open(stream=pdf, filetype="pdf")[0].get_text()
    text_out = pymupdf.open(stream=resp.content, filetype="pdf")[0].get_text()
    assert text_in.split() == [w for w in text_out.split() if w not in ("1", "[údaj", "o", "zdraví]")]
    # tag text carries the box number and the label
    words = [w["text"] for w in api.inspect(resp.content)["pages"][0]["words"]]
    assert "[údaj" in words


def test_preview_tag_goes_inside_at_the_page_top(api: Api) -> None:
    pdf = text_pdf()
    resp = api.post("/v1/preview", pdf, {"spec": spec([box(1, [0.1, 0.0, 0.5, 0.1], "[Osoba 7]")])})
    page = api.inspect(resp.content)["pages"][0]
    tag = next(w for w in page["words"] if w["text"] == "[Osoba")
    assert 0 <= tag["rect"][1] < 0.1  # inside the box, not off the page


def test_preview_is_not_sanitised(api: Api) -> None:
    doc = pymupdf.open(stream=text_pdf(), filetype="pdf")
    doc[0].add_freetext_annot(pymupdf.Rect(10, 10, 100, 40), "poznámka")
    resp = api.post("/v1/preview", dump(doc), {"spec": spec([box(1, [0.1, 0.5, 0.5, 0.6])])})
    out = pymupdf.open(stream=resp.content, filetype="pdf")
    assert len(list(out[0].annots())) == 1


# --- validation --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        {"boxes": [{"page": 2, "rect": [0.1, 0.1, 0.2, 0.2]}]},
        {"boxes": [{"page": 0, "rect": [0.1, 0.1, 0.2, 0.2]}]},
        {"boxes": [{"page": "1", "rect": [0.1, 0.1, 0.2, 0.2]}]},
        {"boxes": [{"page": 1, "rect": [0.2, 0.1, 0.1, 0.2]}]},
        {"boxes": [{"page": 1, "rect": [0.1, 0.1, 0.1, 0.2]}]},
        {"boxes": [{"page": 1, "rect": [-0.1, 0.1, 0.2, 0.2]}]},
        {"boxes": [{"page": 1, "rect": [0.1, 0.1, 0.2, 1.2]}]},
        {"boxes": [{"page": 1, "rect": [0.1, 0.1, 0.2]}]},
        {"boxes": [{"page": 1, "rect": ["a", 0.1, 0.2, 0.2]}]},
        {"boxes": [{"page": 1, "rect": [0.1, 0.1, 0.2, 0.2], "label": "x" * 65}]},
        {"boxes": [{"page": 1, "rect": [0.1, 0.1, 0.2, 0.2], "label": 5}]},
        {"boxes": "nope"},
        {"boxes": [], "raster_pages": [3]},
        {"boxes": [], "raster_pages": "1"},
        [],
    ],
)
def test_bad_spec_is_400(api: Api, bad: object) -> None:
    resp = api.post("/v1/redact", text_pdf(), {"spec": json.dumps(bad)})
    assert resp.status_code == 400
    assert set(resp.json()) == {"error"}
    assert "Novák" not in resp.text


def test_spec_missing_or_not_json_is_400(api: Api) -> None:
    assert api.post("/v1/redact", text_pdf()).status_code == 400
    assert api.post("/v1/redact", text_pdf(), {"spec": "{nope"}).status_code == 400


def test_label_of_64_characters_and_empty_label_are_fine(api: Api) -> None:
    ok = spec([box(1, [0.1, 0.1, 0.9, 0.3], "x" * 64), box(1, [0.1, 0.4, 0.3, 0.5], "")])
    assert api.post("/v1/redact", text_pdf(), {"spec": ok}).status_code == 200


def test_too_many_boxes_is_400(api: Api) -> None:
    many = spec([box(1, [0.1, 0.1, 0.2, 0.2])] * 5001)
    assert api.post("/v1/redact", text_pdf(), {"spec": many}).status_code == 400
    limit = spec([box(1, [0.1, 0.1, 0.2, 0.2])] * 5000)
    assert api.post("/v1/preview", text_pdf(), {"spec": limit}).status_code == 200


def test_render_parameter_errors(api: Api) -> None:
    pdf = text_pdf()
    assert api.post("/v1/render", pdf).status_code == 400
    assert api.post("/v1/render", pdf, {"page": "2"}).status_code == 400
    assert api.post("/v1/render", pdf, {"page": "x"}).status_code == 400
    assert api.post("/v1/render", pdf, {"page": "1", "dpi": "abc"}).status_code == 400
    assert api.post("/v1/render", pdf, {"page": "1", "gray": "maybe"}).status_code == 400


def test_unsupported_type_is_415(api: Api) -> None:
    resp = api.post("/v1/inspect", b"PK\x03\x04 not a document", names=["a.pdf"])
    assert resp.status_code == 415
    assert resp.json() == {"error": "unsupported file type"}
    assert api.post("/v1/inspect", [text_pdf(), b"GIF89a.."]).status_code == 415


def test_no_file_and_wrong_content_type(api: Api) -> None:
    assert api.client.post("/v1/inspect", files={"other": ("a", b"x")}).status_code == 400
    assert (
        api.client.post(
            "/v1/inspect", content=b"{}", headers={"content-type": "application/json"}
        ).status_code
        == 400
    )


def test_encrypted_pdf_is_422(api: Api) -> None:
    doc = pymupdf.open(stream=text_pdf(), filetype="pdf")
    data = dump(doc, encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="o", user_pw="geheim")
    resp = api.post("/v1/inspect", data)
    assert resp.status_code == 422
    assert "encrypted" in resp.json()["error"]


def test_broken_files_are_422(api: Api) -> None:
    assert api.post("/v1/inspect", b"%PDF-1.7\nthis is not a pdf").status_code == 422
    assert api.post("/v1/inspect", b"\xff\xd8\xff\xe0 truncated").status_code == 422
    assert api.post("/v1/inspect", b"\x89PNG\r\n\x1a\n").status_code == 422


def test_too_large_is_413_while_reading(make_api: Callable[[Settings | None], Api]) -> None:
    api = make_api(Settings(max_bytes=5000))
    resp = api.post("/v1/inspect", text_pdf() + b"\0" * 6000)
    assert resp.status_code == 413
    assert resp.json() == {"error": "upload too large"}
    # two files that are each small but too large together
    assert api.post("/v1/inspect", [b"%PDF-" + b"0" * 3000, b"%PDF-" + b"0" * 3000]).status_code == 413


def test_too_many_pages_is_413(make_api: Callable[[Settings | None], Api]) -> None:
    api = make_api(Settings(max_pages=2))
    assert api.post("/v1/inspect", tiff_bytes(3)).status_code == 413
    assert api.post("/v1/inspect", [text_pdf(), text_pdf()]).status_code == 200
    assert api.post("/v1/inspect", [text_pdf(), text_pdf(), text_pdf()]).status_code == 413


# --- auth, health, metrics ---------------------------------------------------------------------


def test_auth_is_off_without_a_token(api: Api) -> None:
    assert api.post("/v1/inspect", text_pdf()).status_code == 200


def test_auth_with_a_token(make_api: Callable[[Settings | None], Api]) -> None:
    api = make_api(Settings(token="s3cret"))
    pdf = text_pdf()
    assert api.post("/v1/inspect", pdf).status_code == 401
    assert api.post("/v1/inspect", pdf, headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert api.post("/v1/inspect", pdf, headers={"Authorization": "Basic s3cret"}).status_code == 401
    assert api.post("/v1/inspect", pdf, headers={"Authorization": "Bearer s3cret"}).status_code == 200
    for endpoint in ("/v1/render", "/v1/redact", "/v1/preview"):
        assert api.post(endpoint, pdf).status_code == 401
    assert api.client.get("/healthz").status_code == 200
    assert api.client.get("/metrics").status_code == 200


def test_healthz(api: Api) -> None:
    body = api.client.get("/healthz").json()
    assert body == {"status": "ok", "version": __version__, "pymupdf": pymupdf.__version__}


def test_metrics_count_requests_by_endpoint_and_status(api: Api) -> None:
    api.post("/v1/inspect", text_pdf())
    api.post("/v1/inspect", b"junk")
    text = api.client.get("/metrics").text
    assert 'pdf_redact_requests_total{endpoint="/v1/inspect",status="200"} 1.0' in text
    assert 'pdf_redact_requests_total{endpoint="/v1/inspect",status="415"} 1.0' in text
    assert 'pdf_redact_request_duration_seconds_count{endpoint="/v1/inspect"} 2.0' in text


def test_redact_headers(api: Api) -> None:
    resp = api.redact(text_pdf(), spec([box(1, [0.1, 0.1, 0.5, 0.2], "[A]")]))
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.headers["X-Redact-Pages"] == "1"
    assert resp.headers["X-Redact-Boxes"] == "1"
    assert resp.headers["X-Redact-Rasterized"] == "none"
    assert resp.headers["X-Redact-Labels-Skipped"] == "0"
    assert resp.headers["X-Redact-Version"] == __version__


def test_missing_font_fails_at_startup(tmp_path: object) -> None:
    from pdf_redact.app import create_app

    with pytest.raises(RuntimeError, match="font"):
        create_app(Settings(font_path="/nonexistent/font.ttf"))
