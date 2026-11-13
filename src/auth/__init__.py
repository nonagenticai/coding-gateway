"""Authentication and authorisation package for coding-gateway.

All implementations come from coding-client; this package re-exports them
so existing imports (``from auth.dependencies import ...``) keep working.
"""

from __future__ import annotations

from .dependencies import (
    User,
    get_current_user,
    get_current_user_with_tenant,
    get_tenant_id,
    require_any_role,
    require_role,
    require_tenant,
)

__all__ = [
    "User",
    "get_current_user",
    "get_current_user_with_tenant",
    "get_tenant_id",
    "require_any_role",
    "require_role",
    "require_tenant",
]
