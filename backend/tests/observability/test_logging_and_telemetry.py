"""测试 2：结构化日志强制脱敏、8 个关联字段注入、标准指标与 /metrics ASGI 端点测试。"""

from __future__ import annotations

import asyncio
import json
import logging

from backend.app.observability.logging import (
    CORRELATION_FIELDS,
    REDACTED_PLACEHOLDER,
    StructuredJsonFormatter,
    correlation_context,
    redact_secrets,
)
from backend.app.observability.telemetry import (
    STANDARD_METRIC_NAMES,
    MetricsRegistry,
    init_telemetry,
    metrics_asgi_app,
    set_span_correlation_attributes,
)


def test_structured_logger_redacts_all_secret_patterns() -> None:
    """密钥键名、带密码 DSN、Bearer Token 与 PEM 私钥必须 100% 脱敏为 [REDACTED]。"""
    formatter = StructuredJsonFormatter(service_name="etl-api-test")
    pem_key = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEA1234567890SECRETKEYDATA\n"
        "-----END RSA PRIVATE KEY-----"
    )
    msg = (
        "Connecting mysql://admin:SuperSecret123@db.internal:3306/prod "
        "and postgresql://pguser:PgPass!99@pg.internal:5432/etl "
        "with header Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig "
        f"and key {pem_key}"
    )

    record = logging.LogRecord(
        name="test.security",
        level=logging.INFO,
        pathname=__file__,
        lineno=42,
        msg=msg,
        args=(),
        exc_info=None,
    )
    record.password = "raw-password-value"
    record.api_key = "ak-live-998877"
    record.connection_uri = "mysql://root:rootpass@localhost:3306/app"
    record.nested_payload = {
        "secret_key": "top-secret",
        "Authorization": "Bearer abc.def.ghi",
        "safe_field": "visible-ok",
    }

    with correlation_context(
        trace_id="4bf92f3577b34da6a3ce929d0e0e4736",
        organization_id="org-001",
        project_id="proj-001",
        pipeline_id="pipe-001",
        pipeline_version_id="pv-001",
        preparation_id="prep-001",
        execution_id="exec-001",
        external_job_id="zeta-999",
    ):
        rendered = formatter.format(record)

    parsed = json.loads(rendered)

    # 验证 8 个关联字段全部注入
    for field in CORRELATION_FIELDS:
        assert parsed[field] is not None, f"缺少关联字段: {field}"

    # 验证没有任何明文凭据残留
    for forbidden in (
        "SuperSecret123",
        "PgPass!99",
        "eyJhbGciOiJIUzI1NiJ9.payload.sig",
        "MIIEowIBAAKCAQEA1234567890SECRETKEYDATA",
        "raw-password-value",
        "ak-live-998877",
        "rootpass",
        "top-secret",
        "abc.def.ghi",
    ):
        assert forbidden not in rendered, f"检测到未脱敏明文泄漏: {forbidden}"

    assert parsed["extra"]["password"] == REDACTED_PLACEHOLDER
    assert parsed["extra"]["api_key"] == REDACTED_PLACEHOLDER
    assert parsed["extra"]["connection_uri"] == REDACTED_PLACEHOLDER
    assert parsed["extra"]["nested_payload"]["secret_key"] == REDACTED_PLACEHOLDER
    assert parsed["extra"]["nested_payload"]["Authorization"] == REDACTED_PLACEHOLDER
    assert parsed["extra"]["nested_payload"]["safe_field"] == "visible-ok"


def test_redact_secrets_handles_lists_and_nested_dicts() -> None:
    """递归脱敏必须覆盖列表与多层嵌套字典。"""
    sample = [
        {"token": "tok-123", "dsn": "postgresql://u:p@host/db"},
        {"normal": "Bearer xyz.123.456"},
    ]
    cleaned = redact_secrets(sample)
    assert cleaned[0]["token"] == REDACTED_PLACEHOLDER
    assert cleaned[0]["dsn"] == REDACTED_PLACEHOLDER
    assert cleaned[1]["normal"] == f"Bearer {REDACTED_PLACEHOLDER}"


def test_metrics_registry_and_asgi_endpoint() -> None:
    """验证 12 个标准 Prometheus 指标均可正常打点并由 /metrics ASGI 端点输出。"""
    reg = MetricsRegistry()
    reg.etl_execution_read_rows_total.labels("batch", "mysql").inc(100)
    reg.etl_execution_write_rows_total.labels("batch", "doris").inc(80)
    reg.etl_execution_write_bytes_total.labels("batch", "doris").inc(4096)
    reg.etl_execution_error_rows_total.labels("batch", "DQ_NOT_NULL_VIOLATION").inc(20)
    reg.etl_execution_reject_rate.labels("exec-1", "total").set(0.20)
    reg.etl_execution_amplification_ratio.labels("exec-1").set(1.0)
    reg.etl_execution_duration_seconds.labels("batch", "SUCCEEDED").observe(2.5)
    reg.etl_execution_budget_utilization_ratio.labels("exec-1", "read_rows").set(0.1)
    reg.etl_cdc_lag_milliseconds.labels("mysql_cdc", "exec-1").set(150.0)
    reg.etl_harness_pdp_decisions_total.labels("P0", "DENY").inc(1)
    reg.etl_harness_replay_blocked_total.labels("tool_broker").inc(1)
    reg.etl_outbox_pending_events.set(3)

    app = metrics_asgi_app(registry=reg)
    sent: list[dict] = []

    async def _receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _send(msg: dict) -> None:
        sent.append(msg)

    asyncio.run(app({"type": "http", "method": "GET", "path": "/metrics"}, _receive, _send))

    assert sent[0]["status"] == 200
    body_text = sent[1]["body"].decode("utf-8")
    for metric_name in STANDARD_METRIC_NAMES:
        assert metric_name in body_text, f"/metrics 输出缺少标准指标: {metric_name}"


def test_telemetry_span_correlation_attributes() -> None:
    """验证 TelemetryManager 与 Span 关联属性注入在精简及完整环境均可正常工作。"""
    mgr = init_telemetry(service_name="etl-test")
    tracer = mgr.get_tracer()
    with tracer.start_as_current_span("test_span") as span:
        set_span_correlation_attributes(
            span,
            trace_id="trace-abc",
            execution_id="exec-xyz",
        )
        if hasattr(span, "attributes"):
            assert span.attributes["trace_id"] == "trace-abc"
            assert span.attributes["execution_id"] == "exec-xyz"
