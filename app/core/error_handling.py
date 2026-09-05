"""Consistent JSON error envelope for every API surface.

Every error response — expected (HTTPException) or unexpected (any other
uncaught exception) — is shaped the same way:

    {"success": false, "error": {"code": "...", "message": "..."}}

Two problems this fixes:
  1. Uncaught exceptions previously fell through to Starlette's bare default
     handler, returning unstructured plain text with no "success" flag and
     no machine-readable code — this raised that as unexpected/confusing.
  2. HTTPExceptions previously returned FastAPI's default {"detail": "..."}
     shape, inconsistent with the above, and gave callers no stable `code`
     to branch on (only a human-readable string, and the same wording
     ("Invalid or expired token") was reused for two different situations:
     an expired login session vs. an expired OTP/verification code).

Registered on each mounted sub-app individually (public_app, internal_app,
company_app) since Starlette dispatches exceptions per-ASGI-app — a mounted
sub-app does NOT inherit handlers from the outer app it's mounted under.
"""
import json
import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware

log = logging.getLogger("errors")

_CODE_BY_STATUS = {
    400: "bad_request",
    401: "unauthenticated",
    402: "payment_required",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    422: "validation_error",
    429: "rate_limited",
}


def _envelope(code: str, message: str, ref: str | None = None) -> dict:
    error = {"code": code, "message": message}
    if ref:
        error["ref"] = ref
    return {"success": False, "error": error}


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
        code = _CODE_BY_STATUS.get(exc.status_code, "error")
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(code, str(exc.detail)),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception):
        # Never leak raw exception text/stack traces to the client — log the
        # real error server-side with a correlation ref, return a safe,
        # generic message plus that same ref so a report can be matched back
        # to the corresponding server-side log entry.
        ref = uuid.uuid4().hex[:12]
        log.error("Unhandled exception [ref=%s] on %s %s", ref, request.method, request.url.path, exc_info=exc)
        return JSONResponse(
            status_code=500,
            content=_envelope(
                "internal_error",
                "Something went wrong on our end. Please try again, and include "
                f"this reference if you contact support: {ref}",
                ref=ref,
            ),
        )


class SuccessEnvelopeMiddleware(BaseHTTPMiddleware):
    """Wraps every successful (2xx) JSON response as {"success": true, "data": ...}
    — the mirror image of the error envelope above. Without this, error responses
    are consistently shaped but success responses are whatever each endpoint
    happened to return (a bare Pydantic model, a bare list, or an ad-hoc dict),
    which is exactly the kind of inconsistency an external integrator notices
    immediately. Non-JSON responses and non-2xx responses pass through untouched
    — error responses are already correctly shaped by the handlers above and
    must not be re-wrapped.
    """

    # Paths FastAPI/Swagger tooling itself serves — these must stay in their
    # raw, unwrapped shape, or Swagger UI/ReDoc/codegen tools that fetch the
    # spec directly from /openapi.json will break. Checked by suffix, not
    # exact match, since a mounted sub-app may see the full original request
    # path (e.g. "/api/public/openapi.json") rather than one relative to its
    # own mount point, depending on how the ASGI scope was rewritten.
    _EXCLUDED_SUFFIXES = ("/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc")

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if request.url.path.rstrip("/").endswith(self._EXCLUDED_SUFFIXES):
            return response
        if not (200 <= response.status_code < 300):
            return response
        content_type = response.headers.get("content-type", "")
        if "application/json" not in content_type:
            return response

        body = b"".join([chunk async for chunk in response.body_iterator])
        try:
            original = json.loads(body)
        except ValueError:
            # Not actually JSON despite the header — pass through unchanged.
            return JSONResponse(
                status_code=response.status_code,
                content=body.decode("utf-8", errors="replace"),
                headers=dict(response.headers),
            )
        return JSONResponse(
            status_code=response.status_code,
            content={"success": True, "data": original},
            headers={k: v for k, v in response.headers.items() if k.lower() != "content-length"},
        )