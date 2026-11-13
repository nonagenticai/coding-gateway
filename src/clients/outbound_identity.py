"""Identity headers for coding-gateway's OWN outbound calls.

Not to be confused with the proxy path. `routers/proxy.py` forwards the client's
own ``Authorization`` verbatim and re-injects the tenant it resolved from that
same token — those requests already carry a verifiable credential and need
nothing from here.

These are the gateway's *own* routes (graph queries, metrics, cache stats,
validation), which build a fresh header dict and previously sent nothing but a
bare ``X-Tenant-Id``. That works only while the callee still trusts the header.
Once coding-analysis and coding-containers are flipped to
``TRUST_TENANT_HEADER=false``, a bare tenant proves nothing and every one of
those routes 401s.

So the gateway presents its OWN licence in ``Authorization`` while the customer
tenant continues to ride ``X-Tenant-Id``. coding-analysis and coding-containers
honour that asserted tenant only when ``svc-coding-gateway``'s subject is in
their ``CODING_ONBEHALF_SUBJECTS``.
"""

from __future__ import annotations

import os

from coding_client.auth.outbound import TenantMissingError, optional_tenant_headers
from coding_client.auth.tenant import DEFAULT_TENANT_ID

__all__ = ["identity_headers", "service_tenant", "service_token"]


def service_token() -> str | None:
    """The gateway's own licence, read per call so a rotation needs no restart."""
    return os.getenv("CODING_SERVICE_TOKEN") or None


def service_tenant() -> str:
    """The tenant the gateway acts as on its own routes. Never guessed.

    Read from ``coding_client.auth.tenant.DEFAULT_TENANT_ID``, which is ``None``
    when the env var is unset.

    This used to be ``settings.default_tenant_id``, a pydantic field defaulting
    to the literal ``"n8n-dev"``. That literal is not junk and it is not a real
    tenant_id either, which is exactly what made it dangerous: it *was* a real
    tenant, as a text tenant_id, until the 2026-04-29 Keycloak migration moved
    tenant identity to uuid7 and demoted the label to a ``tenant_name``
    attribute.

    Combined with :func:`service_token` above, an unset env var therefore would
    not have 401'd and would not have crashed. The gateway would have sent its
    own valid licence while asserting a tenant nobody chose — and because
    ``svc-coding-gateway`` is in coding-analysis's ``CODING_ONBEHALF_SUBJECTS``,
    the callee would have *honoured* it. A forged-looking assertion laundered
    through a legitimate credential is precisely what this module exists to
    prevent, so a missing tenant is an error.

    Raises:
        TenantMissingError: ``DEFAULT_TENANT_ID`` is unset. Deliberately not a
            ``ValueError``/``RuntimeError``/``OSError`` — all three call sites
            sit inside broad ``except`` blocks that degrade to a plausible
            fallback (``return None`` / ``return False``), and a
            misconfiguration must not present as "coding-analysis is down".
    """
    if not DEFAULT_TENANT_ID:
        raise TenantMissingError(
            "coding-gateway has no DEFAULT_TENANT_ID configured, so its own "
            "routes have no tenant to act as. Refusing rather than guessing: "
            "this service sends its own licence, so a guessed tenant would be "
            "vouched for downstream, not rejected. Set DEFAULT_TENANT_ID on the "
            "Deployment env block."
        )
    return DEFAULT_TENANT_ID


def identity_headers(tenant_id: str | None) -> dict[str, str]:
    """``X-Tenant-Id`` (when known) plus this service's own credential.

    The tenant stays in its own header — putting the credential there instead
    would make every call resolve to the gateway's own tenant rather than the
    customer's, which is a data-scoping bug, not a fix.
    """
    return optional_tenant_headers(tenant_id, service_token=service_token())
