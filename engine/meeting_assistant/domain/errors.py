from __future__ import annotations

import uuid
from typing import Any


class AppError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        retryable: bool = False,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retryable = retryable
        self.details = details
        self.trace_id = str(uuid.uuid4())

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
            "trace_id": self.trace_id,
        }


def require(
    condition: bool,
    code: str,
    message: str,
    *,
    status: int = 409,
    retryable: bool = False,
    details: Any = None,
) -> None:
    if not condition:
        raise AppError(code, message, status=status, retryable=retryable, details=details)
