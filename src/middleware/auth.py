"""Authentication middleware for the coding-gateway.

Wraps coding-client's AuthMiddleware to handle ENABLE_AUTH=false by
injecting a default anonymous user with DEFAULT_TENANT_ID, ensuring
request.state.user is always set for downstream dependencies like
require_tenant.
"""

import os

from coding_client.auth import AuthMiddleware as _BaseAuthMiddleware
from coding_client.auth.models import User
from coding_client.auth.tenant import DEFAULT_TENANT_ID
from fastapi import Request
from starlette.middleware.base import RequestResponseEndpoint


class AuthMiddleware(_BaseAuthMiddleware):
    """Auth middleware that injects default user when auth is disabled."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint):
        if os.getenv("ENABLE_AUTH", "true").lower() != "true":
            request.state.user = User(
                sub="anonymous",
                preferred_username="anonymous",
                tenant_id=DEFAULT_TENANT_ID,
                realm_access={"roles": ["user"]},
            )
            request.state.tenant_id = DEFAULT_TENANT_ID
            return await call_next(request)

        return await super().dispatch(request, call_next)


__all__ = ["AuthMiddleware"]
