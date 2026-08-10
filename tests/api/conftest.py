"""Shared API test configuration.

Module-level environment setup runs during pytest collection, before any
test module imports ``memos.api.server_api`` / ``memos.api.routers.server_router``.
This guarantees ``chat_handler`` is instantiated uniformly in every test file
(module-level ``chat_handler`` in server_router is None unless
ENABLE_CHAT_API=true) — without it, the first file that imports the router
would cache ``chat_handler=None`` and later files would get 503 on chat
endpoints.

``memos/api/config.py`` calls ``load_dotenv(override=True)``, which would
silently clobber these values from ``.env`` the moment the API import chain
runs. We neutralize only the ``override`` semantics for the whole session
(keeping the real loader otherwise intact so the load_dotenv-order unit
tests in test_auth.py keep passing): process-env values then always win, and
each test file controls its own env explicitly (MEMOS_BASE_PATH,
AUTH_ENABLED, ... are set on the command line or patched per-test).
"""
import os
import sys
from unittest.mock import patch

import dotenv

os.environ["ENABLE_CHAT_API"] = "true"

_real_load_dotenv = dotenv.load_dotenv


def _load_dotenv_without_override(*args, **kwargs):
    """Real load_dotenv, but never override existing process env vars."""
    kwargs.pop("override", None)
    return _real_load_dotenv(*args, **kwargs)


# Applies for the whole test session; per-test monkeypatching of
# dotenv.load_dotenv (test_auth.py) stacks on top of this cleanly.
patch("dotenv.load_dotenv", _load_dotenv_without_override).start()

# If a test file imported the API entry chain before this conftest ran, the
# cached modules were built with the old env value. Drop them so the next
# ``from memos.api import server_api`` re-imports with ENABLE_CHAT_API=true.
for _name in (
    "memos.api.server_api",
    "memos.api.server_api_ext",
    "memos.api.routers.server_router",
):
    sys.modules.pop(_name, None)
