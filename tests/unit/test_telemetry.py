"""Tests for observability.telemetry — OTel metrics setup is endpoint-driven,
a no-op without an endpoint, and instrumentation is applied only when a provider
exists. The OTLP exporter is swapped for the SDK's ConsoleMetricExporter so no
socket is opened."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import ConsoleMetricExporter

from observability import telemetry


@pytest.fixture(autouse=True)
def _offline_exporter(monkeypatch):
    """Replace the OTLP HTTP exporter with an offline console exporter."""
    monkeypatch.setattr(telemetry, "OTLPMetricExporter", lambda *a, **k: ConsoleMetricExporter())


class TestSetupMetrics:
    def test_no_endpoint_is_noop(self):
        assert telemetry.setup_metrics("") is None
        assert telemetry.setup_metrics(None) is None

    def test_endpoint_returns_provider(self):
        provider = telemetry.setup_metrics("http://alloy-gateway.alloy.svc.cluster.local:4318")
        try:
            assert isinstance(provider, MeterProvider)
        finally:
            telemetry.shutdown_metrics(provider)

    def test_trailing_slash_endpoint_ok(self):
        provider = telemetry.setup_metrics("http://collector:4318/")
        try:
            assert isinstance(provider, MeterProvider)
        finally:
            telemetry.shutdown_metrics(provider)


class TestInstrumentApp:
    def test_instrument_none_is_noop(self):
        app = FastAPI()
        telemetry.instrument_app(app, None)  # must not raise

    def test_instrument_with_provider(self):
        app = FastAPI()
        provider = telemetry.setup_metrics("http://collector:4318")
        try:
            telemetry.instrument_app(app, provider)  # must not raise
        finally:
            telemetry.shutdown_metrics(provider)


class TestShutdownMetrics:
    def test_shutdown_none_is_noop(self):
        telemetry.shutdown_metrics(None)  # must not raise
