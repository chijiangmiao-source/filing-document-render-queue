from __future__ import annotations

from fastapi import HTTPException

from . import errors


class APIError(HTTPException):
    def __init__(self, code: str, message: str):
        super().__init__(status_code=errors.HTTP_STATUS[code], detail=code)
        self.code = code
        self.message = message
