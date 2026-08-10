"""Deprecated compatibility alias; use ``memos.api.server_api:app``."""

from memos.api.server_api import app

__all__ = ["app"]


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("memos.api.server_api:app", host="0.0.0.0", port=8000, workers=1)
