"""
Centralized cube access control for the MemOS product API.

Every cube-related request must resolve the authenticated actor and validate
cube membership through the single shared :class:`CubeAccessControl` instance
before touching graph/vector storage, the scheduler, LLMs, background tasks,
or returning a StreamingResponse.

All denials raise the same fixed 403 detail so unauthorized callers cannot
distinguish "cube missing" from "cube inactive", "user missing" or "no
permission" (anti-enumeration).
"""

from collections.abc import Iterable
from typing import NoReturn

from fastapi import HTTPException

from memos.api.middleware.auth import AuthContext
from memos.mem_user.user_manager import UserManager


FORBIDDEN_DETAIL = "Insufficient cube access"


class AccessForbiddenError(HTTPException):
    """Raised by the access control gate for unauthorized cube access.

    A dedicated exception type so entry points can register a handler that
    returns the standard ``{"detail": ...}`` body for authorization denials,
    without changing the project-wide HTTPException handler (which wraps
    responses as ``{code, message, data}``).
    """

    def __init__(self) -> None:
        super().__init__(status_code=403, detail=FORBIDDEN_DETAIL)


class CubeAccessControl:
    """Single authorization gate for all cube access in the product API."""

    def __init__(self, user_manager: UserManager) -> None:
        self.user_manager = user_manager

    @staticmethod
    def _deny() -> NoReturn:
        raise AccessForbiddenError()

    @staticmethod
    def is_bypassed(auth: AuthContext) -> bool:
        return bool(auth.get("auth_bypassed"))

    @staticmethod
    def is_privileged(auth: AuthContext) -> bool:
        return bool(auth.get("is_master_key") or auth.get("is_internal"))

    def resolve_actor(
        self,
        auth: AuthContext,
        claimed_user_id: str | None = None,
    ) -> str:
        """Resolve the effective actor user id for this request.

        Bypassed (AUTH_ENABLED=false) and privileged (master/internal)
        requests keep their legacy semantics: an explicitly claimed user id
        wins, otherwise the authenticated identity is used without SQLite
        lookups. Regular keys must map to an active SQLite user, and a
        claimed user id that differs from the key's own user id is rejected
        with the uniform 403.
        """
        if self.is_bypassed(auth):
            return claimed_user_id or auth.get("user_name", "default")
        if self.is_privileged(auth):
            return claimed_user_id or auth["user_name"]

        user = self.user_manager.get_user_by_name(auth["user_name"])
        if user is None or not user.is_active:
            self._deny()
        if claimed_user_id is not None and claimed_user_id != user.user_id:
            self._deny()
        return user.user_id

    def require_cube_access(
        self,
        auth: AuthContext,
        actor_user_id: str,
        cube_ids: Iterable[str],
    ) -> None:
        """Validate that the actor may access every cube in cube_ids.

        Cubes are deduplicated before validation. Any missing, inactive or
        unauthorized cube raises the same fixed 403 (all-or-nothing).

        A cube id equal to the actor's own user id is the actor's implicit
        default namespace (MemOS stores memories under ``user_name=mem_cube_id``
        and cube ids commonly equal user ids), so it always passes.
        """
        if self.is_bypassed(auth) or self.is_privileged(auth):
            return
        for cube_id in dict.fromkeys(cube_ids):
            if not cube_id:
                self._deny()
            if cube_id == actor_user_id:
                continue
            if not self.user_manager.validate_user_cube_access(
                actor_user_id, cube_id
            ):
                self._deny()
