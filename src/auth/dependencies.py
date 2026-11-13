"""Auth dependencies for FastAPI routes.

Thin re-export layer: all implementations now live in coding-client.
This module preserves the public API so existing router imports like
``from auth.dependencies import User, require_tenant, get_current_user``
continue to work unchanged.
"""

from __future__ import annotations

# -- Core models and dependencies from coding-client --
from coding_client.auth import (
    AuthMiddleware,
    TenantMiddleware,
    User,
    check_tenant_access,
    create_tenant_context,
    get_current_user,
    get_current_user_with_tenant,
    get_tenant_id,
    optional_tenant,
    require_access_coding,
    require_role,
    require_same_tenant,
    require_tenant,
    require_tenant_context,
)
from coding_client.auth.dependencies import require_any_role

# Re-export DEFAULT_TENANT_ID so middleware/auth.py's existing import still works
from coding_client.auth.tenant import DEFAULT_TENANT_ID

__all__ = [
    # Models
    "User",
    # Dependencies
    "get_current_user",
    "get_current_user_with_tenant",
    # Middleware (re-exported for convenience)
    "AuthMiddleware",
    "TenantMiddleware",
    # RBAC
    "require_access_coding",
    "require_any_role",
    "require_role",
    # Tenant
    "DEFAULT_TENANT_ID",
    "check_tenant_access",
    "create_tenant_context",
    "get_tenant_id",
    "optional_tenant",
    "require_same_tenant",
    "require_tenant",
    "require_tenant_context",
]
