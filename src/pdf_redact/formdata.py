"""Streaming multipart parser with a byte cap and no temporary files.

Starlette's own form parser spools uploads to temp files and has no total size limit. Here the cap is
enforced while the body streams in, so an oversized upload is cut off at the limit instead of being
buffered first. Parts named `file` are collected in order; every other part is a small text field.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import MultipartParser, parse_options_header

from .errors import ApiError

MAX_FIELD_BYTES = 8 * 1024 * 1024
MAX_FIELDS = 32


@dataclass
class Form:
    files: list[bytes] = field(default_factory=list)
    fields: dict[str, str] = field(default_factory=dict)
    file_bytes: int = 0


class _Collector:
    def __init__(self, max_file_bytes: int) -> None:
        self.max_file_bytes = max_file_bytes
        self.form = Form()
        self._name = ""
        self._header_name = b""
        self._header_value = b""
        self._disposition = b""
        self._is_file = False
        self._buffer = bytearray()
        self._part_fields = 0

    def callbacks(self) -> dict[str, object]:
        return {
            "on_part_begin": self.part_begin,
            "on_part_data": self.part_data,
            "on_part_end": self.part_end,
            "on_header_field": self.header_field,
            "on_header_value": self.header_value,
            "on_header_end": self.header_end,
            "on_headers_finished": self.headers_finished,
        }

    def part_begin(self) -> None:
        self._buffer = bytearray()
        self._disposition = b""
        self._name = ""
        self._is_file = False

    def header_field(self, data: bytes, start: int, end: int) -> None:
        self._header_name += data[start:end]

    def header_value(self, data: bytes, start: int, end: int) -> None:
        self._header_value += data[start:end]

    def header_end(self) -> None:
        if self._header_name.lower() == b"content-disposition":
            self._disposition = self._header_value
        self._header_name = b""
        self._header_value = b""

    def headers_finished(self) -> None:
        _, options = parse_options_header(self._disposition)
        self._name = options.get(b"name", b"").decode("utf-8", "replace")
        self._is_file = self._name == "file"
        if not self._is_file:
            self._part_fields += 1
            if self._part_fields > MAX_FIELDS:
                raise ApiError(400, "too many form fields")

    def part_data(self, data: bytes, start: int, end: int) -> None:
        size = end - start
        if self._is_file:
            self.form.file_bytes += size
            if self.form.file_bytes > self.max_file_bytes:
                raise ApiError(413, "upload too large")
        elif len(self._buffer) + size > MAX_FIELD_BYTES:
            raise ApiError(413, "form field too large")
        self._buffer += data[start:end]

    def part_end(self) -> None:
        if self._is_file:
            self.form.files.append(bytes(self._buffer))
        else:
            self.form.fields[self._name] = bytes(self._buffer).decode("utf-8", "replace")
        self._buffer = bytearray()


async def read_form(
    content_type: str, content_length: str | None, stream: AsyncIterator[bytes], max_bytes: int
) -> Form:
    kind, params = parse_options_header(content_type.encode("latin-1", "replace"))
    if kind != b"multipart/form-data" or b"boundary" not in params:
        raise ApiError(400, "expected multipart/form-data")
    if content_length and content_length.isdigit() and int(content_length) > max_bytes + MAX_FIELD_BYTES:
        raise ApiError(413, "upload too large")
    collector = _Collector(max_bytes)
    parser = MultipartParser(params[b"boundary"], collector.callbacks())  # type: ignore[arg-type]
    try:
        async for chunk in stream:
            parser.write(chunk)
        parser.finalize()
    except MultipartParseError as exc:
        raise ApiError(400, "malformed multipart body") from exc
    return collector.form
