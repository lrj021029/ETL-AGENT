# 可观测性模块：统一导出结构化脱敏日志与 OpenTelemetry / Prometheus 遥测接口

from backend.app.observability.logging import (
    CORRELATION_FIELDS,
    REDACTED_PLACEHOLDER,
    StructuredJsonFormatter,
    bind_correlation_context,
    clear_correlation_context,
    correlation_context,
    get_correlation_context,
    get_logger,
    redact_secrets,
    redact_string,
)
from backend.app.observability.telemetry import (
    CORRELATION_ATTRIBUTE_KEYS,
    STANDARD_METRIC_NAMES,
    MetricsRegistry,
    TelemetryManager,
    get_metrics_registry,
    init_telemetry,
    metrics_asgi_app,
    mount_metrics_endpoint,
    render_metrics_latest,
    set_span_correlation_attributes,
)

__all__ = [
    "CORRELATION_FIELDS",
    "CORRELATION_ATTRIBUTE_KEYS",
    "REDACTED_PLACEHOLDER",
    "STANDARD_METRIC_NAMES",
    "StructuredJsonFormatter",
    "MetricsRegistry",
    "TelemetryManager",
    "bind_correlation_context",
    "clear_correlation_context",
    "correlation_context",
    "get_correlation_context",
    "get_logger",
    "redact_secrets",
    "redact_string",
    "get_metrics_registry",
    "init_telemetry",
    "metrics_asgi_app",
    "mount_metrics_endpoint",
    "render_metrics_latest",
    "set_span_correlation_attributes",
]
