# pdf-redact: PDF redaction service. Single worker: PyMuPDF is not thread-safe and the service runs
# one job at a time anyway.
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /opt/venv /opt/venv
COPY LICENSE /licenses/pdf-redact-AGPL-3.0.txt
# Bytecode is compiled at build time, so the read-only root filesystem needs no writable path.
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PDF_REDACT_FONT=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf
RUN useradd --system --uid 10001 --no-create-home --home-dir /nonexistent pdfredact
USER 10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=4).status==200 else 1)"
ENTRYPOINT ["uvicorn", "--factory", "pdf_redact.app:create_app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--no-access-log"]
