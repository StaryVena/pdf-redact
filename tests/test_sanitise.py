"""What redact strips from the document besides the boxes, and when it rasterizes."""

from __future__ import annotations

import pymupdf
import pytest
from conftest import Api
from docs import FONT, box, dump, spec, text_pdf
from pix import extrema

NAME_AT = (60.0, 400.0)


def dirty_pdf() -> bytes:
    """A document carrying everything that must not survive."""
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=500)
    page.insert_font(fontname="dj", fontfile=FONT)
    page.insert_text((60, 100), "Veřejný text zůstává", fontname="dj", fontsize=14)
    page.insert_text((60, 200), "Skrytý text Adam Černý", fontname="dj", fontsize=14, render_mode=3)
    page.insert_link(
        {"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(10, 10, 80, 40), "uri": "https://example.org/x"}
    )
    page.add_freetext_annot(pymupdf.Rect(*NAME_AT, 260, 430), "Jan Novák", fontsize=12, text_color=(0, 0, 0))
    widget = pymupdf.Widget()
    widget.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
    widget.field_name = "jmeno"
    widget.rect = pymupdf.Rect(60, 440, 260, 460)
    widget.field_value = "Marie Dvořáková"
    page.add_widget(widget)
    doc.set_metadata({"author": "Autor Fiktivní", "title": "Důvěrné", "subject": "s", "keywords": "k"})
    doc.set_xml_metadata("<x:xmpmeta xmlns:x='adobe:ns:meta/'><rdf>Fiktivní XMP</rdf></x:xmpmeta>")
    doc.set_toc([[1, "Záložka Novák", 1]])
    doc.embfile_add("priloha.txt", b"tajna priloha Novak")
    root = doc.pdf_catalog()
    doc.xref_set_key(root, "OpenAction", "<</S/JavaScript/JS(app.alert\\('tajny skript'\\))>>")
    doc.xref_set_key(root, "PageLabels", "<</Nums[0<</S/D>>]>>")
    return dump(doc)


def test_the_fixture_really_carries_everything() -> None:
    doc = pymupdf.open(stream=dirty_pdf(), filetype="pdf")
    assert (doc.metadata or {})["author"]
    assert doc.get_xml_metadata()
    assert doc.get_toc()
    assert doc.embfile_count() == 1
    assert doc.xref_get_key(doc.pdf_catalog(), "OpenAction")[0] != "null"
    page = doc[0]
    assert len(list(page.annots())) == 1
    assert len(list(page.widgets())) == 1
    assert page.get_links()
    assert "Skrytý" in page.get_text()


def test_everything_but_the_page_content_is_gone(api: Api) -> None:
    pdf = dirty_pdf()
    # The name annotation and the widget value sit under this box.
    rect = [0.1, 0.78, 0.7, 0.94]
    resp = api.redact(pdf, spec([box(1, rect, "[Osoba]")]))
    assert resp.status_code == 200
    out = resp.content
    doc = pymupdf.open(stream=out, filetype="pdf")
    assert not any(v for k, v in (doc.metadata or {}).items() if k != "format")
    assert not doc.get_xml_metadata()
    assert doc.get_toc() == []
    assert doc.embfile_count() == 0
    page = doc[0]
    assert list(page.annots()) == []
    assert list(page.widgets()) == []
    assert page.get_links() == []
    assert doc.xref_get_key(doc.pdf_catalog(), "OpenAction")[0] == "null"
    assert doc.xref_get_key(doc.pdf_catalog(), "PageLabels")[0] == "null"
    text = page.get_text()
    assert "Skrytý" not in text
    assert "Adam" not in text
    assert "Novák" not in text
    assert "Dvořáková" not in text
    assert "Veřejný text zůstává" in text
    assert "[Osoba]" in text
    for needle in (b"JavaScript", b"tajny", b"priloha", b"Fikti", b"EmbeddedFile"):
        assert needle not in out
    assert b"Producer" not in out
    assert out.count(b"%%EOF") == 1  # no incremental history


def test_an_annotation_under_a_box_is_redacted_like_page_content(api: Api) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=500)
    page.add_freetext_annot(pymupdf.Rect(60, 100, 260, 130), "Jan Novák", fontsize=14, text_color=(0, 0, 0))
    pdf = dump(doc)
    before = api.render(pdf, dpi=72)  # annotations are drawn
    assert extrema(before)[0] < 100
    rect = [0.1, 0.18, 0.7, 0.28]
    resp = api.redact(pdf, spec([box(1, rect)]))
    out = pymupdf.open(stream=resp.content, filetype="pdf")
    assert "Novák" not in out[0].get_text()
    after = api.render(resp.content, dpi=72)
    assert extrema(after.crop((50, 100, 150, 130)))[1] < 40


def ocg_pdf() -> bytes:
    doc = pymupdf.open()
    ocg = doc.add_ocg("Skrytá vrstva", on=False)
    page = doc.new_page(width=400, height=500)
    page.insert_font(fontname="dj", fontfile=FONT)
    page.insert_text((60, 100), "Viditelný text", fontname="dj", fontsize=14)
    page.insert_text((60, 200), "Skrytá Marie Dvořáková", fontname="dj", fontsize=14, oc=ocg)
    doc.new_page(width=400, height=500).insert_text((60, 100), "Druhá strana", fontname="helv", fontsize=14)
    return dump(doc)


def test_optional_content_rasterizes_every_page(api: Api) -> None:
    pdf = ocg_pdf()
    assert api.inspect(pdf)["optional_content"] == 1
    before = api.render(pdf, dpi=72)
    resp = api.redact(pdf, spec([box(1, [0.1, 0.18, 0.5, 0.23], "[x]")]))
    assert resp.status_code == 200
    assert resp.headers["X-Redact-Rasterized"] == "1,2"
    doc = pymupdf.open(stream=resp.content, filetype="pdf")
    assert doc.page_count == 2
    assert all(page.get_text().strip() == "" for page in doc.pages())
    assert all(page.get_images() for page in doc.pages())
    assert doc[0].rect == pymupdf.Rect(0, 0, 400, 500)
    after = api.render(resp.content, dpi=72)
    assert after.size == before.size
    assert not doc.get_ocgs()


def test_raster_pages_are_honoured(api: Api) -> None:
    doc = pymupdf.open()
    doc.insert_pdf(pymupdf.open(stream=text_pdf(), filetype="pdf"))
    doc.insert_pdf(pymupdf.open(stream=text_pdf(), filetype="pdf"))
    pdf = dump(doc)
    resp = api.redact(pdf, spec([box(1, [0.1, 0.1, 0.2, 0.15])], raster_pages=[2]))
    assert resp.headers["X-Redact-Rasterized"] == "2"
    out = pymupdf.open(stream=resp.content, filetype="pdf")
    assert "Veřejný" in out[0].get_text()
    assert out[1].get_text().strip() == ""
    # the redacted page is not rasterized by default, and the header says "none"
    assert api.redact(pdf, spec([box(1, [0.1, 0.1, 0.2, 0.15])])).headers["X-Redact-Rasterized"] == "none"


def test_rasterized_rotated_page_keeps_the_visible_size(api: Api) -> None:
    pdf = text_pdf(90, size=(600, 800), cropbox=(30, 60, 400, 500))
    resp = api.redact(pdf, spec([box(1, [0.1, 0.1, 0.2, 0.2])], raster_pages=[1]))
    page = pymupdf.open(stream=resp.content, filetype="pdf")[0]
    assert page.rect == pymupdf.Rect(0, 0, 440, 370)
    before, after = api.render(pdf, dpi=72), api.render(resp.content, dpi=72)
    assert before.size == after.size


def test_the_label_font_is_embedded_once_per_document(api: Api) -> None:
    doc = pymupdf.open()
    for _ in range(5):
        doc.insert_pdf(pymupdf.open(stream=text_pdf(), filetype="pdf"))
    boxes = [box(p, [0.1, 0.1, 0.6, 0.2], "[Osoba]") for p in range(1, 6)]
    resp = api.redact(dump(doc), spec(boxes))
    assert len(resp.content) < 1_500_000


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_hidden_text_goes_and_visible_text_stays_on_rotated_cropped_pages(api: Api, rotation: int) -> None:
    doc = pymupdf.open()
    page = doc.new_page(width=600, height=800)
    page.insert_font(fontname="dj", fontfile=FONT)
    page.insert_text((100, 200), "viditelný řádek jedna", fontname="dj", fontsize=14)
    page.insert_text((100, 300), "skrytá vrstva Adam Černý", fontname="dj", fontsize=14, render_mode=3)
    page.insert_text((100, 400), "viditelný řádek dvě", fontname="dj", fontsize=14)
    page.insert_text(
        (100, 202), "skrytá druhá vrstva", fontname="dj", fontsize=14, render_mode=3
    )  # over line one
    page.set_cropbox(pymupdf.Rect(30, 60, 500, 700))
    page.set_rotation(rotation)
    pdf = dump(doc)
    assert "skrytá" in pymupdf.open(stream=pdf, filetype="pdf")[0].get_text()
    resp = api.redact(pdf, spec([box(1, [0.01, 0.01, 0.05, 0.05])]))
    text = pymupdf.open(stream=resp.content, filetype="pdf")[0].get_text()
    assert "skrytá" not in text
    assert "Adam" not in text
    assert "viditelný řádek dvě" in text
