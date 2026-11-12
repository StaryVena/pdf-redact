"""Runtime configuration, read from the environment once per app instance."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


@dataclass(frozen=True)
class Settings:
    max_bytes: int = 100 * 1024 * 1024
    max_pages: int = 500
    font_path: str = DEFAULT_FONT
    token: str | None = None

    @classmethod
    def from_env(cls) -> Settings:
        token = os.environ.get("PDF_REDACT_TOKEN") or None
        return cls(
            max_bytes=int(os.environ.get("PDF_REDACT_MAX_BYTES", cls.max_bytes)),
            max_pages=int(os.environ.get("PDF_REDACT_MAX_PAGES", cls.max_pages)),
            font_path=os.environ.get("PDF_REDACT_FONT", DEFAULT_FONT),
            token=token,
        )

    def check(self) -> None:
        """Fail at startup, not on the first request, when the label font is missing."""
        if not Path(self.font_path).is_file():
            raise RuntimeError(f"label font not found: {self.font_path} (set PDF_REDACT_FONT)")
