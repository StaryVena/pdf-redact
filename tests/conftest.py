from __future__ import annotations

import io
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from PIL import Image
from starlette.testclient import TestClient

from pdf_redact.app import create_app
from pdf_redact.config import Settings

Files = bytes | list[bytes]


class Api:
    def __init__(self, client: TestClient) -> None:
        self.client = client

    def post(
        self,
        endpoint: str,
        files: Files,
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        names: list[str] | None = None,
    ) -> httpx.Response:
        items = [files] if isinstance(files, bytes) else files
        parts = [
            ("file", (names[i] if names else f"f{i}.bin", blob, "application/octet-stream"))
            for i, blob in enumerate(items)
        ]
        resp: httpx.Response = self.client.post(endpoint, files=parts, data=data or {}, headers=headers)
        return resp

    def inspect(self, files: Files) -> dict[str, Any]:
        resp = self.post("/v1/inspect", files)
        assert resp.status_code == 200, resp.text
        return resp.json()  # type: ignore[no-any-return]

    def render(self, files: Files, page: int = 1, dpi: int = 100, gray: bool = True) -> Image.Image:
        resp = self.post("/v1/render", files, {"page": str(page), "dpi": str(dpi), "gray": str(gray).lower()})
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"] == "image/png"
        return Image.open(io.BytesIO(resp.content))

    def redact(self, files: Files, spec: str) -> httpx.Response:
        return self.post("/v1/redact", files, {"spec": spec})

    def word(self, files: Files, text: str, page: int = 1) -> list[float]:
        """Relative visible rect of a word, as the text layer reports it."""
        for entry in self.inspect(files)["pages"]:
            if entry["page"] == page:
                for word in entry["words"]:
                    if word["text"] == text:
                        return word["rect"]  # type: ignore[no-any-return]
        raise AssertionError("word not found")


@pytest.fixture
def make_api() -> Callable[[Settings | None], Api]:
    def factory(settings: Settings | None = None) -> Api:
        return Api(TestClient(create_app(settings or Settings()), raise_server_exceptions=False))

    return factory


@pytest.fixture
def api(make_api: Callable[[Settings | None], Api]) -> Api:
    return make_api(None)
