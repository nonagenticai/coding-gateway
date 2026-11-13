"""Gateway configuration using pydantic-settings.

Provides a single Settings class for the coding-gateway microservice.

Per ``gitops-production/docs/config-externalization-policy.md`` and the
2026-05-09 user directive (memory: ``feedback-no-hardcoded-urls-ports-in-code``):
service addresses MUST come from gitops env. Missing env in production must
crash boot loudly instead of silently routing traffic to a dead in-cluster
service name. Defaults are empty strings; ``_REQUIRED_IN_PRODUCTION`` enforces
presence at startup when ``ENVIRONMENT=production``.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import ClassVar

from coding_client.config import BaseMicroserviceSettings
from pydantic import AliasChoices, Field, field_validator, model_validator

_CALLER_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_CANONICAL_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


def parse_project_id_caller_licences(raw: str) -> dict[str, str]:
    """Parse ``"<license_id>=<name>,..."`` into ``{license_id(lower): name}``.

    Several ids may share one name. Raises ``ValueError`` naming the bad entry
    (or a duplicate id): a typo here would silently leave a caller on its
    ``licence:<id>`` label, which is exactly the misread ("zero missing for
    n8n") this map exists to prevent.
    """
    mapping: dict[str, str] = {}
    for entry in (e.strip() for e in (raw or "").split(",")):
        if not entry:
            continue
        lic, sep, name = (part.strip() for part in entry.partition("="))
        if not sep or not _CANONICAL_UUID_RE.match(lic) or not _CALLER_NAME_RE.match(name):
            raise ValueError(
                f"malformed entry {entry!r}: expected <license_id uuid>=<name>, "
                "name matching ^[a-z0-9][a-z0-9._-]{0,63}$"
            )
        if lic.lower() in mapping:
            raise ValueError(f"license_id {lic!r} mapped twice")
        mapping[lic.lower()] = name
    return mapping


class Settings(BaseMicroserviceSettings):
    """Coding-gateway configuration."""

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
        "extra": "ignore",
    }

    # ── Identity ──────────────────────────────────────────────────────
    service_name: str = Field(default="Code Graph Helper API")
    version: str = Field(default="1.0.0")
    environment: str = Field(default="production")
    debug: bool = Field(default=False)
    port: int = Field(default=8687)

    # ── Auth / tenant ─────────────────────────────────────────────────
    enable_auth: bool = Field(default=True)
    enable_docs: bool = Field(default=False)
    # NB no ``default_tenant_id`` field. It defaulted to the literal
    # ``"n8n-dev"`` — the stale pre-uuid7 label of a tenant that really exists —
    # and fed three outbound call sites that also send this service's own
    # licence, so an unset env var would have been VOUCHED downstream rather
    # than rejected. The tenant now comes from
    # ``clients.outbound_identity.service_tenant()``, which reads the SDK's
    # ``DEFAULT_TENANT_ID`` (``None`` when unset) and refuses.
    secret_key: str = Field(default="change-me-in-production")
    # X-Project-Id gate (middleware/project_id.py). Default OFF = count-only:
    # `coding_gateway_project_id_missing_total{caller,reason}` fires in BOTH
    # states, and gitops flips this once that counter reads zero for the callers
    # that matter. True changes the OUTCOME only (served → 400).
    require_project_id_header: bool = Field(
        default=False,
        validation_alias=AliasChoices(
            "CODING_GATEWAY_REQUIRE_PROJECT_ID_HEADER", "REQUIRE_PROJECT_ID_HEADER"
        ),
    )
    # Bounded `caller` names for licence-authenticated requests on that counter,
    # keyed by the INTROSPECTED license_id — never a forwarded X-License-Id or
    # X-App-Id. Unmapped licences read `licence:<license_id>`. Malformed =
    # startup failure only while enforcing (`_check_project_id_caller_licences`).
    project_id_caller_licences: str = Field(
        default="",
        validation_alias=AliasChoices(
            "CODING_GATEWAY_PROJECT_ID_CALLER_LICENCES", "PROJECT_ID_CALLER_LICENCES"
        ),
    )

    # ── Database ──────────────────────────────────────────────────────
    # Address sourced from the gateway Deployment env in gitops-production.
    database_url: str = Field(default="")
    db_connection_timeout: int = Field(default=30)
    db_statement_timeout: int = Field(default=30000)

    # ── Redis ─────────────────────────────────────────────────────────
    redis_url: str = Field(default="")

    # ── Backend micro-service URLs ────────────────────────────────────
    # Real values come from the coding-gateway Deployment env block in
    # gitops-production. Empty defaults force production boot to fail loudly
    # if any are missing.
    analysis_url: str = Field(default="")
    testing_url: str = Field(default="")
    ai_url: str = Field(default="")
    containers_url: str = Field(default="")
    github_url: str = Field(default="")
    security_url: str = Field(default="")
    realtime_url: str = Field(default="")

    # ── Proxy read timeouts ───────────────────────────────────────────
    # The proxy shares ONE httpx client, so its read timeout used to be a
    # single global budget for every backend. Measured 2026-08-28: the
    # coding-analysis quality-gate route runs a FULL static analysis and
    # answers 200 in 212-222s, while the proxy hung up at exactly 120 015ms
    # and returned `504 Backend service timeout` -- discarding a successful
    # response. Its own budgets are far larger (300-1800s per tool, 1800s
    # per batch), so that route could never have fitted the global 120s.
    #
    # Default stays 120s: every route keeps today's behaviour. Only the
    # blocking-analyzer routes named in proxy._LONG_READ_PATTERNS get the
    # larger budget. Both are env-overridable so tuning is a ConfigMap edit
    # rather than an image rebuild.
    proxy_read_timeout_default: float = Field(default=120.0, gt=0)
    proxy_read_timeout_analysis: float = Field(default=300.0, gt=0)
    # Long WRITE routes, each sized to its backend's own declared ceiling plus a
    # margin (proxy._WRITE_READ_BUDGETS has the per-route derivation). Cutting a
    # write short does not stop it: the backend completes while the caller gets
    # a 504, so these must outlast the backend, never undercut it.
    proxy_read_timeout_exec: float = Field(default=330.0, gt=0)
    proxy_read_timeout_push: float = Field(default=960.0, gt=0)
    proxy_read_timeout_fetch: float = Field(default=960.0, gt=0)
    proxy_read_timeout_import: float = Field(default=660.0, gt=0)
    proxy_read_timeout_upgrade: float = Field(default=1860.0, gt=0)
    # coding-testing workspace runs / workspace openapi-diff (py2rust A2/A5b): several
    # declared exec ceilings in sequence (proxy._WRITE_READ_BUDGETS has the sum).
    proxy_read_timeout_workspace_run: float = Field(default=630.0, gt=0)
    # coding-testing workspace schema-diff (py2rust A5a): 7 sequential execs, each
    # bounded by the request's per-command timeout (le=300) -- see proxy._WRITE_READ_BUDGETS.
    proxy_read_timeout_workspace_schema_diff: float = Field(default=2130.0, gt=0)
    # coding-testing behaviour record/replay/compare (py2rust A5c), sized for the sync
    # variant (driver le=300 + its helper execs) -- see proxy._WRITE_READ_BUDGETS.
    proxy_read_timeout_behaviour_parity: float = Field(default=690.0, gt=0)
    # Published per operation as `x-request-timeout-ms` in the aggregated
    # /openapi.json: the route's read budget above + this margin, so the gateway's
    # own answer (the result, or its 504) always arrives before the client gives up.
    client_request_timeout_margin: float = Field(default=30.0, gt=0)

    # ── Aggregated /openapi.json identity (n8n node generator input) ───
    # info.title is the generated node's displayName; info["x-n8n"] replaces the
    # generator's hand-written node config for coding.
    openapi_title: str = Field(default="Code Graph Helper")
    n8n_node_description: str = Field(
        default=(
            "Code Graph Helper platform: code graphs, static and semantic analysis, "
            "quality and security checks, testing, GitHub and pull request actions, "
            "and workspaces"
        )
    )

    # ── Public-facing gateway URL (used in OpenAPI servers block) ─────
    public_server_url: str = Field(default="")

    # ── Paths ─────────────────────────────────────────────────────────
    temp_dir: str = Field(default="/tmp/code-graph-helper")
    ca_cert_path: str = Field(default="/etc/ssl/certs/ca-certificates.crt")
    # Positive-containment root for the visualization endpoint's local-FS reads.
    # The endpoint accepts a file path ONLY when it resolves to a location under
    # this directory; None (the default) disables ALL local-FS reads → every path
    # is rejected. Sourced from gitops env like the other paths; no hardcoded
    # default because repo source is never present on the gateway pod.
    visualization_workspace_root: str | None = Field(
        default=None,
        description=(
            "Absolute directory the visualization endpoint may read files under; "
            "None (default) disables all local-FS reads."
        ),
    )

    # ── Observability ─────────────────────────────────────────────────
    otel_endpoint: str = Field(default="")
    log_level: str = Field(default="INFO")
    log_format: str = Field(default="json")

    # ── Reserved for future feature flags ─────────────────────────────
    # These fields are defined but not yet consumed by application logic.
    # They are kept so that environment variables can pre-configure them
    # before the corresponding features are implemented.
    enable_caching: bool = Field(default=True)
    enable_background_processing: bool = Field(default=True)
    enable_full_text_search: bool = Field(default=True)

    # ── CORS ──────────────────────────────────────────────────────────
    cors_origins: list[str] = Field(default=["*"])

    # ── Rate limiting ─────────────────────────────────────────────────
    rate_limit_requests: int = Field(default=100)
    rate_limit_window: int = Field(default=60)

    # ── Kubernetes jobs ────────────────────────────────────────────────
    max_concurrent_jobs: int = Field(default=3)
    job_ttl_seconds: int = Field(default=1800)
    job_backoff_limit: int = Field(default=2)
    job_timeout_seconds: int = Field(default=3600)
    # pytest-runner image; sourced from gitops env (was a hardcoded reference
    # to the retired air.local registry).
    pytest_image: str = Field(default="")

    # ── Required-in-production env vars ───────────────────────────────
    # Enforced by BaseMicroserviceSettings.model_post_init; missing any of
    # these when ``ENVIRONMENT=production`` raises a single combined RuntimeError.
    _SERVICE_NAME: ClassVar[str] = "coding-gateway"
    _REQUIRED_IN_PRODUCTION: ClassVar[tuple[str, ...]] = (
        "database_url",
        "redis_url",
        "analysis_url",
        "testing_url",
        "ai_url",
        "containers_url",
        "github_url",
        "security_url",
        "realtime_url",
        "public_server_url",
        "otel_endpoint",
        "pytest_image",
    )

    @field_validator("environment")
    @classmethod
    def validate_environment(cls, v: str) -> str:
        allowed = {"development", "staging", "production", "test", "testing"}
        if v not in allowed:
            raise ValueError(f"environment must be one of {allowed}")
        return v

    @field_validator("database_url")
    @classmethod
    def _normalize_db_driver(cls, v: str) -> str:
        # Fleet standard: psycopg v3. SQLAlchemy defaults to psycopg2 for bare
        # postgresql:// URLs; force the v3 driver since asyncpg/psycopg2 aren't
        # installed.
        if v.startswith("postgresql://"):
            return v.replace("postgresql://", "postgresql+psycopg://", 1)
        return v

    @model_validator(mode="after")
    def _ensure_directories(self) -> Settings:
        """Create temp_dir if it does not exist (ignore read-only FS errors)."""
        try:
            Path(self.temp_dir).mkdir(parents=True, exist_ok=True)
        except OSError:
            logging.getLogger(__name__).warning("Could not create directory %s", self.temp_dir)
        return self

    @model_validator(mode="after")
    def _check_secret_key(self) -> Settings:
        """Reject default secret_key in non-development environments."""
        if self.secret_key == "change-me-in-production" and self.environment not in (
            "development",
            "test",
            "testing",
        ):
            raise ValueError(
                "secret_key must be changed from default in non-development environments"
            )
        return self

    @model_validator(mode="after")
    def _check_project_id_caller_licences(self) -> Settings:
        """Refuse a malformed licence→caller map, but ONLY while enforcing.

        With the flag off the map only names counter series and a typo must
        never take the gateway down: the middleware warns once and treats it as
        empty. With it on, a wrong label hides which caller is being refused.
        """
        if self.require_project_id_header:
            try:
                parse_project_id_caller_licences(self.project_id_caller_licences)
            except ValueError as exc:
                raise ValueError(f"PROJECT_ID_CALLER_LICENCES {exc}") from None
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached singleton of the gateway settings."""
    return Settings()
