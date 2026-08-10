"""
Class-level regression for the shared ``SecurityHeadersMiddleware``.

Stage A of the unified-entrypoint plan: the middleware must move out of
``server_api_ext`` into the public middleware package so removing the shim
later cannot silently drop the security headers. Importing it from
``memos.api.middleware`` fails until stage B exports it there (expected red).
"""

from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from memos.api.middleware import SecurityHeadersMiddleware


EXPECTED_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-XSS-Protection": "1; mode=block",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
}


def _bare_app():
    """Minimal ASGI app wearing only the middleware under test."""

    async def ok(_request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/", ok)])
    app.add_middleware(SecurityHeadersMiddleware)
    return app


def test_security_headers_middleware_sets_all_five_headers():
    response = TestClient(_bare_app()).get("/")

    assert response.status_code == 200
    for name, expected in EXPECTED_SECURITY_HEADERS.items():
        assert response.headers.get(name) == expected, f"{name} missing or wrong"
