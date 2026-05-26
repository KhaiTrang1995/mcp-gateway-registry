"""OpenTelemetry meter and instrument declarations for the auth server.

Mirrors the pattern in ``registry/observability/meters.py``: declare a single
global meter and the instruments it owns at module scope, import where
needed, increment via ``counter.add(value, attributes)``.

All Path-2 events that auth_server previously POSTed to metrics-service
(``auth_request``, ``tool_execution``, ``protocol_latency``) are migrated
here. Cardinality-risky dimensions (``user_hash``, ``request_id``,
``server_path``, ``session_key``) have been removed from the canonical
attribute set; see issue #1122 for the rationale.
"""

from __future__ import annotations

import logging
import os

from opentelemetry import metrics

logger = logging.getLogger(__name__)


_meter = metrics.get_meter("mcp-auth-server")


# =============================================================================
# Authentication-request metrics
# =============================================================================

auth_request_total = _meter.create_counter(
    name="auth_request_total",
    description="Authentication request count, labeled by outcome and method",
    unit="1",
)

auth_request_duration_ms = _meter.create_histogram(
    name="auth_request_duration_ms",
    description="Authentication request duration in milliseconds",
    unit="ms",
)


# =============================================================================
# Tool-execution metrics (auth-side, with full client info from headers)
# =============================================================================

tool_execution_total = _meter.create_counter(
    name="tool_execution_total",
    description="Tool execution count detected at the auth layer",
    unit="1",
)

tool_execution_duration_ms = _meter.create_histogram(
    name="tool_execution_duration_ms",
    description="Tool execution duration in milliseconds",
    unit="ms",
)


# =============================================================================
# Protocol-latency metrics (time between MCP protocol stages)
# =============================================================================

protocol_latency_ms = _meter.create_histogram(
    name="protocol_latency_ms",
    description=(
        "Time between MCP protocol stages: initialize -> tools/list, "
        "tools/list -> tools/call, initialize -> tools/call"
    ),
    unit="ms",
)


# =============================================================================
# Self-observability of the migration itself
# =============================================================================

_metrics_emission_path_counter = _meter.create_counter(
    name="metrics_emission_path_total",
    description=(
        "Counts which emission path produced an auth-server metric. Helps "
        "operators verify the OTel migration: when METRICS_LEGACY_HTTP_POST="
        "false (the default after issue #1122), the legacy count should be zero."
    ),
    unit="1",
)


def record_emission_path(path: str) -> None:
    """Record that a metric was emitted via the given path.

    Args:
        path: Either ``"otel"`` or ``"legacy"``.
    """
    _metrics_emission_path_counter.add(1, {"path": path})


# =============================================================================
# Public helpers
# =============================================================================


def is_otel_enabled() -> bool:
    """Return True when OTel SDK is configured to export metrics."""
    return bool(os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip())
