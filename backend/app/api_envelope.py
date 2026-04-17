from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class ApiEnvelopeException(HTTPException):
    pass


def error_payload(
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "details": details or {},
    }


def raise_api_error(
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> None:
    raise ApiEnvelopeException(
        status_code=status_code,
        detail=error_payload(code, message, details),
    )


async def api_envelope_exception_handler(
    request: Request,
    exc: StarletteHTTPException,
) -> JSONResponse:
    detail = getattr(exc, "detail", None)
    if not (isinstance(detail, dict) and "code" in detail and "message" in detail):
        return await http_exception_handler(request, exc)
    payload = detail if isinstance(detail, dict) else error_payload(
        "http_error",
        str(detail),
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "ok": False,
            "error": payload,
        },
        headers=exc.headers,
    )
