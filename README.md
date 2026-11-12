# pdf-redact

A small HTTP service that does the PDF half of redaction: it removes the content under rectangles you
give it (true redaction, not a black rectangle laid over text that stays selectable), draws a label on
each box, strips metadata and other hidden carriers, and returns a new PDF. It is built on
[PyMuPDF](https://pymupdf.readthedocs.io/) and therefore licensed under the AGPL-3.0 (see below).

What it is: a stateless converter. Files and rectangles go in, a PDF or an image or JSON comes out.

What it is not: it does not decide what to redact (finding personal data is the caller's job, for
example with OCR on the output of `/v1/render`), it does not OCR, it does not store anything, and it
does not verify its own output. See [Limits](#limits).

## Endpoints

Every `/v1/*` endpoint takes `multipart/form-data` with one or more `file` parts, in order. The files
are concatenated into one document and pages are numbered 1..N across all of them. Accepted: PDF, JPEG,
PNG, TIFF (a multi-page TIFF is several pages), detected by magic bytes (name and declared type are
ignored). JPEG/TIFF EXIF orientation is applied. Errors are JSON, `{"error": "<short reason>"}`; they
never repeat document content.

| Status | When |
| --- | --- |
| 400 | bad or missing form field, invalid `spec`, page out of range |
| 401 | `PDF_REDACT_TOKEN` is set and the bearer token is missing or wrong |
| 413 | more than `PDF_REDACT_MAX_BYTES` uploaded (cut off while reading) or more than `PDF_REDACT_MAX_PAGES` pages |
| 415 | a file is not PDF, JPEG, PNG or TIFF |
| 422 | unreadable file, or a PDF that needs a password |

### `GET /healthz`

```
{"status":"ok","version":"0.1.0","pymupdf":"2.1"}
```

### `GET /metrics`

Prometheus text format: `pdf_redact_requests_total{endpoint,status}` and
`pdf_redact_request_duration_seconds{endpoint}` (histogram).

### `POST /v1/inspect`

```sh
curl -sF file=@a.pdf -F file=@scan.jpg http://localhost:8080/v1/inspect
```

```json
{
  "pages": [
    {"page": 1, "file": 0, "width": 595.3, "height": 841.9, "rotation": 0, "text_chars": 1234,
     "words": [{"text": "Novák", "rect": [0.21, 0.17, 0.33, 0.21]}]}
  ],
  "encrypted": 0, "optional_content": 0, "annotations": 0, "attachments": 0
}
```

`file` is the 0-based index of the input file. `width`/`height` are points of the visible page.
`text_chars` counts non-space characters of the text layer, hidden text included. `words` come from the
text layer. The four document counts are summed over all input files (`encrypted` counts PDFs that
were encrypted but open without a password; `optional_content` counts files with layers).

### `POST /v1/render`

```sh
curl -sF file=@a.pdf -F page=1 -F dpi=200 -F gray=true http://localhost:8080/v1/render -o page1.png
```

`image/png` of one visible page, annotations drawn. `page` is required, `dpi` defaults to 300 and is
capped at 300, `gray` defaults to true. Very large pages are rendered at a lower scale so that the
bitmap stays under 35,000,000 pixels. Rectangles are relative, so the scale does not matter.

### `POST /v1/redact`

```sh
curl -sD - -F file=@a.pdf \
  -F 'spec={"boxes":[{"page":1,"rect":[0.21,0.17,0.33,0.21],"label":"[Osoba 1]"}],"raster_pages":[]}' \
  http://localhost:8080/v1/redact -o redacted.pdf
```

`spec` is JSON: `boxes` (at most 5000; `page` 1-based, `rect` `[x0,y0,x1,y1]` with
`0 <= x0 < x1 <= 1` and `0 <= y0 < y1 <= 1`, `label` at most 64 characters, may be empty) and an
optional `raster_pages` list. Response: `application/pdf` with the headers `X-Redact-Pages`,
`X-Redact-Boxes`, `X-Redact-Rasterized` (comma-separated page numbers, or `none`),
`X-Redact-Labels-Skipped` (labels that did not fit even at 4 pt) and `X-Redact-Version`.

What it does, in this order:

1. Bakes annotations and form fields into the page content, so what a viewer shows is what is
   redacted (a stamp or free-text note under a box goes with the rest).
2. Applies one redaction per box, filled black: text under it is removed from the content stream,
   image pixels under it are blanked in the image itself (scanned pages are images), and vector
   graphics are removed only when they lie entirely inside the box (table rules that merely touch or
   cross a box stay).
3. Writes the label in white, centred, upright as seen on the visible page (also on rotated pages),
   in an embedded DejaVu Sans, so Czech and other Latin diacritics work. The font size is the largest
   of at most `min(0.75 x box height, 11 pt)` that fits the width, down to 4 pt; below that the label is
   skipped and counted.
4. Replaces pages with an image of the redacted page (300 dpi, same size, no text layer) when they are
   listed in `raster_pages`, when partly covered curved or slanted vector art would survive under a
   box (see Limits), and every page of an input file that has optional content (layers):
   hidden layers can keep content that no box covers.
5. Sanitises the result: metadata and XMP, outline, embedded files and file attachments, JavaScript
   and actions, links, thumbnails, form fields, all remaining annotations, hidden text (render mode 3,
   e.g. a scanner's invisible OCR layer), page labels, the structure tree and the producer string are
   removed. The file is written new (full garbage collection, no incremental history).

### `POST /v1/preview`

Same `spec`. Returns the original, concatenated pages unchanged, with every box drawn as a red outline
(about 1.5 pt) and a small red tag `<n> <label>` (`n` is the 1-based index in `spec.boxes`) above the
box, or inside its top edge at the top of the page. Drawn into the page content, so every viewer shows
it. Not sanitised (it is for the person reviewing the boxes). Pages of files with layers are replaced
by a 150 dpi image first.

## Coordinates

All rectangles, in and out, are relative (0..1) to the visible page as a viewer displays it: after the
CropBox and /Rotate, origin at the top left, x to the right, y down, `[x0, y0, x1, y1]`. This is exactly
the frame of the `/v1/render` image, so a box found on the rendered image (OCR, say) is
`[left / image_width, top / image_height, right / image_width, bottom / image_height]`.

Inside, PyMuPDF has a second frame (unrotated, origin at the CropBox corner) for text extraction and
annotations; the conversion goes through the page's rotation matrix. PyMuPDF's own shape and annotation
helpers misplace things on pages that are rotated and have a CropBox offset, so all drawing and
redaction runs with /Rotate temporarily set to 0 (see `geometry.py`). The tests cover rotations
0/90/180/270, a CropBox offset from the MediaBox, and a MediaBox that does not start at the origin.

## Limits

- Only what is under a box is removed. Text elsewhere stays, as text. The caller must verify the
  result: extract the text of the output and OCR the rendering of it, and check that the replaced
  strings are gone. This service does not check.
- A box covers the visible page. Content outside the CropBox is not visible, not reported by `inspect`
  and not touched.
- When glyphs are removed from the middle of one text string of the content stream (for example a
  whole line written by a single `Tj`), the glyphs after the removed span in that same string move
  left, by a few points (roughly the width of one removed glyph in our probes), because MuPDF only
  partly compensates for the removed advance. Removing from the start of a string, or text that the
  producer split into separate strings (per word, per segment), keeps everything in place. Nothing
  leaks; the line just looks slightly different next to the box. Rasterize the page (`raster_pages`)
  if the exact layout matters.
- A vector path that crosses a box edge is not removed by MuPDF (only paths fully inside the box are).
  If such a path is a curve, a slanted line or a slanted quad (baked ink signatures, text converted
  to outlines), the page is rasterized automatically and listed in `X-Redact-Rasterized`; axis-aligned
  lines and rectangles (table borders, cell backgrounds) never trigger this.
- Pages of files with optional content are rasterized, which makes them bigger and not searchable.
- PDFs with an owner password only (no user password) are opened and written out unencrypted; PDFs
  that need a password are rejected with 422. XFA forms and 3D/rich media are not understood.
- Cost grows quickly with many boxes on one page (about quadratic; 800 boxes on one page take a few
  seconds), and pages are rendered in memory (up to 35 megapixels per bitmap): budget a few times the upload size
  plus a few hundred MB.
- One job at a time per process (PyMuPDF is not thread-safe); other requests wait for the lock. Run a
  second container if you need concurrency.

## Deployment

```sh
docker run --rm --read-only --cap-drop ALL -p 127.0.0.1:8080:8080 \
  -e PDF_REDACT_TOKEN=change-me --memory 2g pdf-redact:dev
```

- Internal network only. The service has no accounts and no rate limiting; put it where only the
  calling application can reach it. If the network is not enough, set `PDF_REDACT_TOKEN`: `/v1/*`
  then require `Authorization: Bearer <token>` (constant-time compare); `/healthz` and `/metrics`
  stay open.
- It stores nothing: documents are processed in memory and nothing is written to disk, so the root
  filesystem can be read-only. The image runs as uid 10001.
- It never logs document content, file names, labels or coordinates, only counts, sizes, durations
  and status codes. Keep access logs of any proxy in front of it equally quiet.
- Set a memory limit; large scans at 300 dpi are big. `PDF_REDACT_MAX_BYTES` is enforced while the
  body is read.
- Run a single worker (the image does).

### Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `PDF_REDACT_MAX_BYTES` | `104857600` | total size of all uploaded files; 413 above |
| `PDF_REDACT_MAX_PAGES` | `500` | pages over all files; 413 above |
| `PDF_REDACT_FONT` | `/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf` | TTF for labels; startup fails if missing |
| `PDF_REDACT_TOKEN` | unset | bearer token for `/v1/*` |

## Development

```sh
uv sync
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest -q
uv run uvicorn --factory pdf_redact.app:create_app --port 8080
```

The tests need `fonts-dejavu-core` (or `PDF_REDACT_FONT` pointing at another TTF with the glyphs used)
and build all documents themselves with fictional names.

## Licence

AGPL-3.0-or-later, see `LICENSE`. PyMuPDF is AGPL, so this service is too: if you run a modified
version for users over a network, you must offer them its source. A program that merely calls the HTTP
API (sending files and rectangles, receiving files) is a separate work and is not affected.
