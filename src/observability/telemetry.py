"""OpenTelemetry **metrics** setup for the coding gateway.

The gateway pushes OTLP metrics to the in-cluster ``alloy-gateway`` (an ambient,
non-hostNetwork Alloy Deployment that converts OTLP → Prometheus ``remote_write``).
The push target is ``settings.otel_endpoint`` (e.g.
``http://alloy-gateway.alloy.svc.cluster.local:4318``); the HTTP metrics path is
``{otel_endpoint}/v1/metrics``.

Scope is metrics only. We deliberately do **not** wire a ``TracerProvider`` at
``otel_endpoint`` — alloy-gateway's pipeline only forwards metrics, so traces sent
there would be dropped. (Traces, if ever added, go to ``tempo`` instead.)
``FastAPIInstrumentor`` is attached with our ``MeterProvider`` only, so it emits
HTTP-server metrics (request count / duration / active requests) and its span path
is a no-op against the global default (no-op) tracer.

``service.name`` / resource attributes are taken from the standard OTel env vars
(``OTEL_SERVICE_NAME`` / ``OTEL_RESOURCE_ATTRIBUTES``) which the deployment already
sets to ``coding-gateway`` — ``Resource.create()`` picks them up automatically.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from opentelemetry import metrics
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource

logger = logging.getLogger(__name__)

# Mirror the 30s push cadence used by the other OTLP-push services
# (procedures / workers) so dashboards see a consistent resolution.
_EXPORT_INTERVAL_MS = 30_000


def setup_metrics(otel_endpoint: str | None) -> MeterProvider | None:
    """Build and globally register an OTLP-push ``MeterProvider``.

    Endpoint-driven and side-effect-light: with no endpoint it is a no-op and
    returns ``None`` (so test / local runs without a collector do nothing). With
    an endpoint it constructs the exporter + reader + provider, sets it as the
    global meter provider, and returns it. The exporter does not open a socket on
    construction — it connects lazily on the first periodic export.

    Args:
        otel_endpoint: OTLP base URL (no ``/v1/metrics`` suffix), or empty/None.

    Returns:
        The registered ``MeterProvider``, or ``None`` when no endpoint was given.
    """
    if not otel_endpoint:
        logger.info("OTEL endpoint not set — skipping metrics setup (no-op).")
        return None

    endpoint = f"{otel_endpoint.rstrip('/')}/v1/metrics"
    exporter = OTLPMetricExporter(endpoint=endpoint)
    reader = PeriodicExportingMetricReader(exporter, export_interval_millis=_EXPORT_INTERVAL_MS)
    # Resource.create() merges the env-based detector → service.name from
    # OTEL_SERVICE_NAME / OTEL_RESOURCE_ATTRIBUTES (set by the deployment).
    provider = MeterProvider(metric_readers=[reader], resource=Resource.create())
    metrics.set_meter_provider(provider)
    logger.info(
        "OTel metrics enabled — pushing to %s every %ds", endpoint, _EXPORT_INTERVAL_MS // 1000
    )
    return provider


def instrument_app(app: FastAPI, provider: MeterProvider | None) -> None:
    """Attach ``FastAPIInstrumentor`` for automatic HTTP-server metrics.

    No-op when ``provider`` is ``None``. Passing the provider explicitly keeps the
    instrumentation metrics-only (no tracer wiring).
    """
    if provider is None:
        return
    FastAPIInstrumentor.instrument_app(app, meter_provider=provider)
    logger.info("FastAPIInstrumentor attached (HTTP-server metrics).")


def shutdown_metrics(provider: MeterProvider | None) -> None:
    """Flush and shut down the meter provider on application teardown."""
    if provider is None:
        return
    try:
        provider.shutdown()
    except Exception:  # pragma: no cover - best-effort drain
        logger.warning("Failed to shut down metrics provider cleanly", exc_info=True)
