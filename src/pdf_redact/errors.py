"""API errors. Messages are short fixed English reasons and never echo any document content."""

from __future__ import annotations


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
