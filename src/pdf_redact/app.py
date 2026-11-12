"""Starlette application: routing, auth, limits, metrics.

Run with `uvicorn --factory pdf_redact.app:create_app`. Request handlers only parse and validate; the
PDF work runs in a worker thread under one global lock (PyMuPDF is not thread-safe).
"""

from __future__ import annotations

import hmac
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import pymupdf
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Histogram, generate_latest
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import __version__, ops
from .config import Settings
from .errors import ApiError
from .formdata import Form, read_form
from .sources import Source, close_all, load_sources
from .spec import parse_spec

log = logging.getLogger("pdf_redact")


def _configure_logging() -> None:
    """uvicorn only configures its own loggers; ours would be silent without a handler."""
    if not log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        log.propagate = False


MAX_DPI = 300.0


def _flag(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    lowered = value.strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise ApiError(400, "gray must be a boolean")


def create_app(settings: Settings | None = None) -> Starlette:
    cfg = settings or Settings.from_env()
    cfg.check()
    _configure_logging()
    pymupdf.TOOLS.mupdf_display_errors(False)  # MuPDF warnings can quote document content

    registry = CollectorRegistry()
    requests_total = Counter(
        "pdf_redact_requests_total", "Requests", ["endpoint", "status"], registry=registry
    )
    duration = Histogram(
        "pdf_redact_request_duration_seconds", "Request duration", ["endpoint"], registry=registry
    )

    def authorised(request: Request) -> bool:
        if cfg.token is None:
            return True
        header = request.headers.get("authorization", "")
        scheme, _, supplied = header.partition(" ")
        return scheme.lower() == "bearer" and hmac.compare_digest(supplied.encode(), cfg.token.encode())

    async def run(endpoint: str, request: Request, handler: Callable[[], Awaitable[Response]]) -> Response:
        started = time.monotonic()
        status = 500
        try:
            if endpoint.startswith("/v1/") and not authorised(request):
                raise ApiError(401, "unauthorized")
            response = await handler()
            status = response.status_code
            return response
        except ApiError as exc:
            status = exc.status
            return JSONResponse({"error": exc.message}, status_code=exc.status)
        except Exception as exc:
            # Only the exception type: messages from PDF libraries can quote document content.
            log.error("%s failed: %s", endpoint, type(exc).__name__)
            return JSONResponse({"error": "internal error"}, status_code=500)
        finally:
            elapsed = time.monotonic() - started
            requests_total.labels(endpoint, str(status)).inc()
            duration.labels(endpoint).observe(elapsed)
            log.info("%s status=%d duration_ms=%d", endpoint, status, elapsed * 1000)

    async def load(request: Request) -> tuple[Form, list[Source]]:
        form = await read_form(
            request.headers.get("content-type", ""),
            request.headers.get("content-length"),
            request.stream(),
            cfg.max_bytes,
        )

        def work() -> list[Source]:
            with ops.JOB_LOCK:
                return load_sources(form.files, cfg.max_pages)

        sources = await run_in_threadpool(work)
        log.info(
            "input files=%d bytes=%d pages=%d",
            len(form.files),
            form.file_bytes,
            sum(s.page_count for s in sources),
        )
        return form, sources

    async def job(sources: list[Source], fn: Callable[[], Any]) -> Any:
        def locked() -> Any:
            with ops.JOB_LOCK:
                return fn()

        try:
            return await run_in_threadpool(locked)
        finally:
            # Page counts are read before the job closes the documents; closing twice is harmless.
            close_all(sources)

    async def healthz(request: Request) -> Response:
        return await run("/healthz", request, _ok_health)

    async def _ok_health() -> Response:
        return JSONResponse({"status": "ok", "version": __version__, "pymupdf": pymupdf.__version__})

    async def metrics(request: Request) -> Response:
        async def handler() -> Response:
            return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

        return await run("/metrics", request, handler)

    async def inspect_endpoint(request: Request) -> Response:
        async def handler() -> Response:
            _, sources = await load(request)
            result = await job(sources, lambda: ops.inspect(sources))
            return JSONResponse(result)

        return await run("/v1/inspect", request, handler)

    async def render_endpoint(request: Request) -> Response:
        async def handler() -> Response:
            form, sources = await load(request)
            total = sum(s.page_count for s in sources)
            try:
                page = int(form.fields.get("page", ""))
            except ValueError as exc:
                raise ApiError(400, "page is required (integer)") from exc
            if not 1 <= page <= total:
                close_all(sources)
                raise ApiError(400, "page out of range")
            try:
                dpi = float(form.fields.get("dpi") or MAX_DPI)
            except ValueError as exc:
                close_all(sources)
                raise ApiError(400, "dpi must be a number") from exc
            if not dpi > 0:
                close_all(sources)
                raise ApiError(400, "dpi must be positive")
            try:
                gray = _flag(form.fields.get("gray"), True)
            except ApiError:
                close_all(sources)
                raise
            png = await job(sources, lambda: ops.render(sources, page, min(dpi, MAX_DPI), gray))
            return Response(png, media_type="image/png")

        return await run("/v1/render", request, handler)

    async def redact_endpoint(request: Request) -> Response:
        async def handler() -> Response:
            form, sources = await load(request)
            try:
                spec = parse_spec(form.fields.get("spec"), sum(s.page_count for s in sources))
            except ApiError:
                close_all(sources)
                raise
            result = await job(sources, lambda: ops.redact(sources, spec, cfg.font_path))
            log.info(
                "redact pages=%d boxes=%d rasterized=%d", result.pages, result.boxes, len(result.rasterized)
            )
            return Response(
                result.pdf,
                media_type="application/pdf",
                headers={
                    "X-Redact-Pages": str(result.pages),
                    "X-Redact-Boxes": str(result.boxes),
                    "X-Redact-Rasterized": ",".join(map(str, result.rasterized)) or "none",
                    "X-Redact-Labels-Skipped": str(result.labels_skipped),
                    "X-Redact-Version": __version__,
                },
            )

        return await run("/v1/redact", request, handler)

    async def preview_endpoint(request: Request) -> Response:
        async def handler() -> Response:
            form, sources = await load(request)
            try:
                spec = parse_spec(form.fields.get("spec"), sum(s.page_count for s in sources))
            except ApiError:
                close_all(sources)
                raise
            pdf = await job(sources, lambda: ops.preview(sources, spec, cfg.font_path))
            return Response(pdf, media_type="application/pdf", headers={"X-Redact-Version": __version__})

        return await run("/v1/preview", request, handler)

    async def not_found(request: Request, exc: Exception) -> Response:
        return JSONResponse({"error": "not found"}, status_code=404)

    async def method_not_allowed(request: Request, exc: Exception) -> Response:
        return JSONResponse({"error": "method not allowed"}, status_code=405)

    routes = [
        Route("/healthz", healthz, methods=["GET"]),
        Route("/metrics", metrics, methods=["GET"]),
        Route("/v1/inspect", inspect_endpoint, methods=["POST"]),
        Route("/v1/render", render_endpoint, methods=["POST"]),
        Route("/v1/redact", redact_endpoint, methods=["POST"]),
        Route("/v1/preview", preview_endpoint, methods=["POST"]),
    ]
    return Starlette(routes=routes, exception_handlers={404: not_found, 405: method_not_allowed})
