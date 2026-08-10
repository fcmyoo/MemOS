import logging
import os

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.staticfiles import StaticFiles

from memos.api.access_control import AccessForbiddenError
from memos.api.exceptions import APIExceptionHandler
from memos.api.lifecycle import shutdown_components
from memos.api.middleware.auth import AUTH_ENABLED, verify_api_key
from memos.api.middleware.rate_limit import RateLimitMiddleware
from memos.api.middleware.request_context import RequestContextMiddleware
from memos.api.middleware.security import SecurityHeadersMiddleware
from memos.api.routers import server_router as server_router_module
from memos.api.routers.admin_router import router as admin_router
from memos.plugins.manager import plugin_manager


load_dotenv()
plugin_manager.discover()

RATE_LIMIT_ENABLED = os.getenv("RATE_LIMIT_ENABLED", "true").lower() == "true"
CORS_ORIGINS = [
    origin.strip()
    for origin in os.getenv("CORS_ORIGINS", "").split(",")
    if origin.strip()
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)
logger.info(
    "[SERVER_API] load_dotenv completed. env_MEMSCHEDULER_STREAM_KEY_PREFIX=%s, "
    "env_MEMSCHEDULER_REDIS_STREAM_KEY_PREFIX=%s",
    os.getenv("MEMSCHEDULER_STREAM_KEY_PREFIX"),
    os.getenv("MEMSCHEDULER_REDIS_STREAM_KEY_PREFIX"),
)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    yield
    shutdown_components(server_router_module.components)


app = FastAPI(
    title="MemOS Server REST APIs",
    description="A REST API for managing multiple users with MemOS Server.",
    version="1.0.1",
    lifespan=lifespan,
)

app.mount("/download", StaticFiles(directory=os.getenv("FILE_LOCAL_PATH")), name="static_mapping")

app.add_middleware(RequestContextMiddleware, source="server_api")
if RATE_LIMIT_ENABLED:
    app.add_middleware(RateLimitMiddleware)
    logger.info("Rate limiting enabled")
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key", "X-User-Name"],
)

app.include_router(server_router_module.router, dependencies=[Depends(verify_api_key)])
if AUTH_ENABLED:
    app.include_router(admin_router)


@app.get("/health")
def health_check() -> dict[str, str]:
    """Public, fingerprint-free container and load-balancer health endpoint."""
    return {"status": "healthy"}


app.exception_handler(RequestValidationError)(APIExceptionHandler.validation_error_handler)
app.exception_handler(ValueError)(APIExceptionHandler.value_error_handler)
app.exception_handler(HTTPException)(APIExceptionHandler.http_error_handler)
app.exception_handler(Exception)(APIExceptionHandler.global_exception_handler)
app.exception_handler(AccessForbiddenError)(APIExceptionHandler.access_forbidden_handler)

plugin_manager.init_app(app)


if __name__ == "__main__":
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()
    uvicorn.run("memos.api.server_api:app", host="0.0.0.0", port=args.port, workers=args.workers)
