"""Tests for config/settings.py."""

from __future__ import annotations

import pytest


def test_settings_defaults(settings):
    """Settings have correct defaults."""
    assert settings.service_name == "Code Graph Helper API"
    assert settings.port == 8687
    assert settings.environment == "test"  # set by _test_env fixture


def test_backend_ports(settings):
    """Backend service URLs use correct ports."""
    assert settings.analysis_url == "http://coding-analysis:8688"
    assert settings.testing_url == "http://coding-testing:8689"
    assert settings.ai_url == "http://coding-ai:8690"
    assert settings.containers_url == "http://coding-containers:8691"
    assert settings.github_url == "http://coding-github:8692"
    assert settings.security_url == "http://coding-security:8693"
    assert settings.realtime_url == "http://coding-realtime:8694"


def test_environment_validation(monkeypatch):
    """Settings reject invalid environment values."""
    from config.settings import Settings

    with pytest.raises(Exception):
        Settings(environment="invalid_env")


def test_environment_valid_values():
    """Settings accept all valid environment values."""
    from config.settings import Settings

    for env in ("development", "staging", "production", "test"):
        kwargs = {"environment": env}
        if env not in ("development", "test"):
            kwargs["secret_key"] = "not-default-key"
        s = Settings(**kwargs)
        assert s.environment == env


def test_production_fails_loudly_when_required_url_missing(monkeypatch):
    """Settings(environment='production') must raise when any required URL env var is empty.

    Per gitops-production/docs/config-externalization-policy.md and
    feedback-no-hardcoded-urls-ports-in-code: service addresses must come from
    gitops env; missing env should crash boot rather than silently route.
    """
    from config.settings import Settings

    # Clear every URL env var so Settings has to fall back on the empty default
    for var in (
        "DATABASE_URL",
        "REDIS_URL",
        "ANALYSIS_URL",
        "TESTING_URL",
        "AI_URL",
        "CONTAINERS_URL",
        "GITHUB_URL",
        "SECURITY_URL",
        "REALTIME_URL",
        "PUBLIC_SERVER_URL",
        "OTEL_ENDPOINT",
        "PYTEST_IMAGE",
    ):
        monkeypatch.delenv(var, raising=False)

    with pytest.raises(RuntimeError) as excinfo:
        Settings(environment="production", secret_key="not-default-key")
    msg = str(excinfo.value)
    # Confirm the error names every required field, as a single combined report.
    for field in (
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
    ):
        assert field in msg


def test_production_passes_when_all_required_urls_present(monkeypatch):
    """Settings(environment='production') succeeds when all required URLs are set."""
    from config.settings import Settings

    for var, val in (
        ("DATABASE_URL", "postgresql+psycopg://u:p@host:5432/db"),
        ("REDIS_URL", "redis://host:6379/0"),
        ("ANALYSIS_URL", "http://coding-analysis:8688"),
        ("TESTING_URL", "http://coding-testing:8689"),
        ("AI_URL", "http://coding-ai:8690"),
        ("CONTAINERS_URL", "http://coding-containers:8691"),
        ("GITHUB_URL", "http://coding-github:8692"),
        ("SECURITY_URL", "http://coding-security:8693"),
        ("REALTIME_URL", "http://coding-realtime:8694"),
        ("PUBLIC_SERVER_URL", "https://coding.example.com"),
        ("OTEL_ENDPOINT", "http://otel-collector:4318"),
        ("PYTEST_IMAGE", "ghcr.io/nonagenticai/pytest-runner:latest"),
    ):
        monkeypatch.setenv(var, val)
    s = Settings(environment="production", secret_key="not-default-key")
    assert s.environment == "production"
    assert s.analysis_url == "http://coding-analysis:8688"


def test_auth_defaults(settings):
    """Auth defaults are correct for test environment.

    The ``default_tenant_id`` assertion here used to read
    ``== "n8n-dev"``. It did not merely fail to catch the defect — it **pinned
    the defect as correct**, so it would have gone red on the fix and stayed
    green on the bug. Inverted rather than deleted, so a revert is caught.
    """
    assert settings.enable_auth is False  # overridden by _test_env
    assert "default_tenant_id" not in type(settings).model_fields, (
        "Settings.default_tenant_id is back. The gateway's own routes must take "
        "their tenant from clients.outbound_identity.service_tenant(), which "
        "reads the SDK's DEFAULT_TENANT_ID (None when unset) and refuses — "
        "because those routes also send this service's licence, so a guessed "
        "tenant is vouched for downstream rather than rejected."
    )
