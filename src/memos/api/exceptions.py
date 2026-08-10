import logging

from fastapi.exceptions import HTTPException, RequestValidationError
from fastapi.requests import Request
from fastapi.responses import JSONResponse


logger = logging.getLogger(__name__)


class APIExceptionHandler:
    """Centralized exception handling for MemOS APIs."""

    @staticmethod
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        """Handle request validation errors."""
        errors = exc.errors()
        path = request.url.path
        method = request.method

        readable_errors = []
        for err in errors:
            loc = " -> ".join(str(loc_i) for loc_i in err.get("loc", []))
            readable_errors.append(
                f"[{loc}] {err.get('msg', 'unknown error')} (type: {err.get('type', 'unknown')})"
            )

        logger.error(
            f"Validation error on {method} {path}: {readable_errors}, raw errors: {errors}"
        )
        return JSONResponse(
            status_code=422,
            content={
                "code": 422,
                "message": f"Parameter validation error on {method} {path}: {'; '.join(readable_errors)}",
                "detail": errors,
                "data": None,
            },
        )

    @staticmethod
    async def value_error_handler(request: Request, exc: ValueError):
        """Handle ValueError exceptions globally."""
        logger.error(f"ValueError: {exc}")
        return JSONResponse(
            status_code=400,
            content={"code": 400, "message": str(exc), "data": None},
        )

    @staticmethod
    async def global_exception_handler(request: Request, exc: Exception):
        """Handle all unhandled exceptions globally."""
        logger.error(f"Exception: {exc}")
        return JSONResponse(
            status_code=500,
            content={"code": 500, "message": str(exc), "data": None},
        )

    @staticmethod
    async def http_error_handler(request: Request, exc: HTTPException):
        """Handle HTTP exceptions globally."""
        logger.error(f"HTTP error {exc.status_code}: {exc.detail}")
        return JSONResponse(
            status_code=exc.status_code,
            content={"code": exc.status_code, "message": str(exc.detail), "data": None},
        )

    @staticmethod
    async def access_forbidden_handler(request: Request, exc):
        """Handle cube access denials with the standard FastAPI body.

        Authorization failures keep ``{"detail": "Insufficient cube access"}``
        so clients can distinguish them from generic wrapped HTTP errors.
        """
        from memos.api.access_control import FORBIDDEN_DETAIL

        return JSONResponse(status_code=403, content={"detail": FORBIDDEN_DETAIL})
