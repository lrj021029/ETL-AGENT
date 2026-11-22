"""OpenTelemetry 分布式追踪与 Prometheus 指标注册表。

严格遵循 contracts/observability/telemetry_spec.md 第 2 节与第 3 节规范：
- 支持 8 个统一 Span 关联属性（trace_id 等）。
- 导出全部 12 个标准低基数 Prometheus 指标。
- 提供 FastAPI / 标准 ASGI `/metrics` 挂载助手。
- 在未安装 prometheus_client / opentelemetry 的精简测试环境中自动降级为兼容的内存实现，保证可测试性与零阻塞。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from backend.app.observability.logging import CORRELATION_FIELDS

# 与 telemetry_spec.md 第 2 节对齐的 Span 关联属性键名
CORRELATION_ATTRIBUTE_KEYS: tuple[str, ...] = CORRELATION_FIELDS

# 标准 Prometheus 指标名称清单（来自 contracts/observability/telemetry_spec.md 第 3 节）
STANDARD_METRIC_NAMES: tuple[str, ...] = (
    "etl_execution_read_rows_total",
    "etl_execution_write_rows_total",
    "etl_execution_write_bytes_total",
    "etl_execution_error_rows_total",
    "etl_execution_reject_rate",
    "etl_execution_amplification_ratio",
    "etl_execution_duration_seconds",
    "etl_execution_budget_utilization_ratio",
    "etl_cdc_lag_milliseconds",
    "etl_harness_pdp_decisions_total",
    "etl_harness_replay_blocked_total",
    "etl_outbox_pending_events",
)

# 尝试加载可选依赖 prometheus_client
try:
    from prometheus_client import (  # type: ignore[import-not-found]
        CONTENT_TYPE_LATEST,
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )

    _HAS_PROMETHEUS_CLIENT = True
except ImportError:  # pragma: no cover - 精简环境降级路径
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"
    _HAS_PROMETHEUS_CLIENT = False

# 尝试加载可选依赖 OpenTelemetry SDK
try:
    from opentelemetry import trace as otel_trace  # type: ignore[import-not-found]
    from opentelemetry.sdk.resources import Resource  # type: ignore[import-not-found]
    from opentelemetry.sdk.trace import TracerProvider  # type: ignore[import-not-found]
    from opentelemetry.sdk.trace.export import (  # type: ignore[import-not-found]
        BatchSpanProcessor,
        ConsoleSpanExporter,
    )

    _HAS_OTEL_SDK = True
except ImportError:  # pragma: no cover - 精简环境降级路径
    otel_trace = None  # type: ignore[assignment]
    _HAS_OTEL_SDK = False


def _format_labels(label_names: tuple[str, ...], label_values: tuple[str, ...]) -> str:
    """将标签名与标签值格式化为 Prometheus 文本exposition 格式。"""
    if not label_names:
        return ""
    pairs = []
    for k, v in zip(label_names, label_values):
        escaped = str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        pairs.append(f'{k}="{escaped}"')
    return "{" + ",".join(pairs) + "}"


class _FallbackMetricChild:
    """精简环境下的指标标签子实例（兼容 Counter/Gauge/Histogram 常用方法）。"""

    def __init__(self, parent: "_FallbackMetric", label_values: tuple[str, ...]) -> None:
        self._parent = parent
        self._label_values = label_values

    def inc(self, amount: float = 1.0) -> None:
        self._parent._inc(self._label_values, amount)

    def dec(self, amount: float = 1.0) -> None:
        self._parent._inc(self._label_values, -amount)

    def set(self, value: float) -> None:
        self._parent._set(self._label_values, value)

    def observe(self, value: float) -> None:
        self._parent._observe(self._label_values, value)


class _FallbackMetric:
    """未安装 prometheus_client 时的线程安全内存指标实现。"""

    def __init__(
        self,
        name: str,
        documentation: str,
        labelnames: tuple[str, ...] = (),
        metric_type: str = "counter",
        buckets: tuple[float, ...] = (0.1, 0.5, 1.0, 5.0, 15.0, 60.0, 300.0, 1800.0),
    ) -> None:
        self.name = name
        self.documentation = documentation
        self.labelnames = tuple(labelnames)
        self.metric_type = metric_type
        self.buckets = buckets
        self._lock = threading.Lock()
        self._values: dict[tuple[str, ...], float] = {}
        self._hist_counts: dict[tuple[str, ...], dict[float, int]] = {}
        self._hist_sums: dict[tuple[str, ...], float] = {}
        self._hist_totals: dict[tuple[str, ...], int] = {}

    def labels(self, *args: str, **kwargs: str) -> _FallbackMetricChild:
        if args:
            if len(args) != len(self.labelnames):
                raise ValueError(f"标签数量不匹配: 期望 {len(self.labelnames)}，实际 {len(args)}")
            key = tuple(str(a) for a in args)
        else:
            key = tuple(str(kwargs[k]) for k in self.labelnames)
        return _FallbackMetricChild(self, key)

    def inc(self, amount: float = 1.0) -> None:
        if self.labelnames:
            raise ValueError(f"指标 {self.name} 需要指定标签 {self.labelnames}")
        self._inc((), amount)

    def set(self, value: float) -> None:
        if self.labelnames:
            raise ValueError(f"指标 {self.name} 需要指定标签 {self.labelnames}")
        self._set((), value)

    def observe(self, value: float) -> None:
        if self.labelnames:
            raise ValueError(f"指标 {self.name} 需要指定标签 {self.labelnames}")
        self._observe((), value)

    def _inc(self, key: tuple[str, ...], amount: float) -> None:
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + float(amount)

    def _set(self, key: tuple[str, ...], value: float) -> None:
        with self._lock:
            self._values[key] = float(value)

    def _observe(self, key: tuple[str, ...], value: float) -> None:
        val = float(value)
        with self._lock:
            if key not in self._hist_counts:
                self._hist_counts[key] = {b: 0 for b in self.buckets}
                self._hist_sums[key] = 0.0
                self._hist_totals[key] = 0
            for b in self.buckets:
                if val <= b:
                    self._hist_counts[key][b] += 1
            self._hist_sums[key] += val
            self._hist_totals[key] += 1

    def render_lines(self) -> list[str]:
        """渲染为 Prometheus 文本exposition 格式行列表。"""
        lines = [
            f"# HELP {self.name} {self.documentation}",
            f"# TYPE {self.name} {self.metric_type}",
        ]
        with self._lock:
            if self.metric_type == "histogram":
                for key, bucket_map in self._hist_counts.items():
                    base_labels = dict(zip(self.labelnames, key))
                    for b in self.buckets:
                        lbl = _format_labels(
                            self.labelnames + ("le",),
                            tuple(base_labels[k] for k in self.labelnames) + (str(b),),
                        )
                        lines.append(f"{self.name}_bucket{lbl} {bucket_map[b]}")
                    inf_lbl = _format_labels(
                        self.labelnames + ("le",),
                        tuple(base_labels[k] for k in self.labelnames) + ("+Inf",),
                    )
                    lines.append(f"{self.name}_bucket{inf_lbl} {self._hist_totals[key]}")
                    plain_lbl = _format_labels(self.labelnames, key)
                    lines.append(f"{self.name}_sum{plain_lbl} {self._hist_sums[key]}")
                    lines.append(f"{self.name}_count{plain_lbl} {self._hist_totals[key]}")
            else:
                if not self._values and not self.labelnames:
                    lines.append(f"{self.name} 0.0")
                for key, val in self._values.items():
                    lbl = _format_labels(self.labelnames, key)
                    lines.append(f"{self.name}{lbl} {val}")
        return lines


class MetricsRegistry:
    """ETL-Agent 标准 Prometheus 指标注册表，封装全部 12 个契约指标。"""

    def __init__(self, use_prometheus_client: bool | None = None) -> None:
        self._use_prom = _HAS_PROMETHEUS_CLIENT if use_prometheus_client is None else (
            use_prometheus_client and _HAS_PROMETHEUS_CLIENT
        )
        self._fallback_metrics: list[_FallbackMetric] = []

        if self._use_prom:
            self.registry: Any = CollectorRegistry()
            self.etl_execution_read_rows_total = Counter(
                "etl_execution_read_rows_total",
                "执行读取总行数",
                ["execution_mode", "connector_source"],
                registry=self.registry,
            )
            self.etl_execution_write_rows_total = Counter(
                "etl_execution_write_rows_total",
                "执行写入总行数",
                ["execution_mode", "connector_sink"],
                registry=self.registry,
            )
            self.etl_execution_write_bytes_total = Counter(
                "etl_execution_write_bytes_total",
                "执行写入总字节数",
                ["execution_mode", "connector_sink"],
                registry=self.registry,
            )
            self.etl_execution_error_rows_total = Counter(
                "etl_execution_error_rows_total",
                "质量校验错误总行数",
                ["execution_mode", "error_code"],
                registry=self.registry,
            )
            self.etl_execution_reject_rate = Gauge(
                "etl_execution_reject_rate",
                "执行拒绝率 (error_rows / validated_rows)",
                ["execution_id", "window_type"],
                registry=self.registry,
            )
            self.etl_execution_amplification_ratio = Gauge(
                "etl_execution_amplification_ratio",
                "输出放大比",
                ["execution_id"],
                registry=self.registry,
            )
            self.etl_execution_duration_seconds = Histogram(
                "etl_execution_duration_seconds",
                "执行耗时分布（秒）",
                ["execution_mode", "status"],
                buckets=(0.1, 0.5, 1.0, 5.0, 15.0, 60.0, 300.0, 1800.0),
                registry=self.registry,
            )
            self.etl_execution_budget_utilization_ratio = Gauge(
                "etl_execution_budget_utilization_ratio",
                "资源预算使用率",
                ["execution_id", "budget_type"],
                registry=self.registry,
            )
            self.etl_cdc_lag_milliseconds = Gauge(
                "etl_cdc_lag_milliseconds",
                "CDC 复制延迟（毫秒）",
                ["connector_source", "execution_id"],
                registry=self.registry,
            )
            self.etl_harness_pdp_decisions_total = Counter(
                "etl_harness_pdp_decisions_total",
                "Harness PDP 策略决策总次数",
                ["risk_level", "decision"],
                registry=self.registry,
            )
            self.etl_harness_replay_blocked_total = Counter(
                "etl_harness_replay_blocked_total",
                "Capability 重放拦截总次数",
                ["source_component"],
                registry=self.registry,
            )
            self.etl_outbox_pending_events = Gauge(
                "etl_outbox_pending_events",
                "Outbox 待投递事件数",
                registry=self.registry,
            )
        else:
            self.registry = None
            self.etl_execution_read_rows_total = self._reg_fallback(
                "etl_execution_read_rows_total", "执行读取总行数", ("execution_mode", "connector_source"), "counter"
            )
            self.etl_execution_write_rows_total = self._reg_fallback(
                "etl_execution_write_rows_total", "执行写入总行数", ("execution_mode", "connector_sink"), "counter"
            )
            self.etl_execution_write_bytes_total = self._reg_fallback(
                "etl_execution_write_bytes_total", "执行写入总字节数", ("execution_mode", "connector_sink"), "counter"
            )
            self.etl_execution_error_rows_total = self._reg_fallback(
                "etl_execution_error_rows_total", "质量校验错误总行数", ("execution_mode", "error_code"), "counter"
            )
            self.etl_execution_reject_rate = self._reg_fallback(
                "etl_execution_reject_rate", "执行拒绝率 (error_rows / validated_rows)", ("execution_id", "window_type"), "gauge"
            )
            self.etl_execution_amplification_ratio = self._reg_fallback(
                "etl_execution_amplification_ratio", "输出放大比", ("execution_id",), "gauge"
            )
            self.etl_execution_duration_seconds = self._reg_fallback(
                "etl_execution_duration_seconds", "执行耗时分布（秒）", ("execution_mode", "status"), "histogram"
            )
            self.etl_execution_budget_utilization_ratio = self._reg_fallback(
                "etl_execution_budget_utilization_ratio", "资源预算使用率", ("execution_id", "budget_type"), "gauge"
            )
            self.etl_cdc_lag_milliseconds = self._reg_fallback(
                "etl_cdc_lag_milliseconds", "CDC 复制延迟（毫秒）", ("connector_source", "execution_id"), "gauge"
            )
            self.etl_harness_pdp_decisions_total = self._reg_fallback(
                "etl_harness_pdp_decisions_total", "Harness PDP 策略决策总次数", ("risk_level", "decision"), "counter"
            )
            self.etl_harness_replay_blocked_total = self._reg_fallback(
                "etl_harness_replay_blocked_total", "Capability 重放拦截总次数", ("source_component",), "counter"
            )
            self.etl_outbox_pending_events = self._reg_fallback(
                "etl_outbox_pending_events", "Outbox 待投递事件数", (), "gauge"
            )

    def _reg_fallback(
        self,
        name: str,
        doc: str,
        labelnames: tuple[str, ...],
        metric_type: str,
    ) -> _FallbackMetric:
        metric = _FallbackMetric(name=name, documentation=doc, labelnames=labelnames, metric_type=metric_type)
        self._fallback_metrics.append(metric)
        return metric

    def render_latest(self) -> bytes:
        """生成标准 Prometheus 文本格式指标快照字节流。"""
        if self._use_prom:
            return generate_latest(self.registry)
        lines: list[str] = []
        for metric in self._fallback_metrics:
            lines.extend(metric.render_lines())
        return ("\n".join(lines) + "\n").encode("utf-8")


_default_registry: MetricsRegistry | None = None
_registry_lock = threading.Lock()


def get_metrics_registry() -> MetricsRegistry:
    """获取进程级单例 MetricsRegistry。"""
    global _default_registry
    if _default_registry is None:
        with _registry_lock:
            if _default_registry is None:
                _default_registry = MetricsRegistry()
    return _default_registry


def render_metrics_latest(registry: MetricsRegistry | None = None) -> bytes:
    """渲染最新 Prometheus 指标文本。"""
    reg = registry or get_metrics_registry()
    return reg.render_latest()


def metrics_asgi_app(registry: MetricsRegistry | None = None) -> Callable[..., Any]:
    """创建标准 ASGI 3.0 `/metrics` 应用，可独立运行或挂载至 FastAPI/Starlette。"""
    reg = registry or get_metrics_registry()

    async def _asgi_app(scope: dict[str, Any], receive: Callable[..., Any], send: Callable[..., Any]) -> None:
        if scope.get("type") != "http":
            return
        body = reg.render_latest()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", CONTENT_TYPE_LATEST.encode("ascii")),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    return _asgi_app


def mount_metrics_endpoint(app: Any, path: str = "/metrics", registry: MetricsRegistry | None = None) -> None:
    """将 `/metrics` ASGI 端点挂载到 FastAPI / Starlette 应用上。"""
    asgi_handler = metrics_asgi_app(registry=registry)
    if hasattr(app, "mount"):
        app.mount(path, asgi_handler)
    else:
        raise TypeError("目标 app 对象不支持 mount(path, asgi_app) 方法")


@dataclass
class TelemetryManager:
    """OpenTelemetry 初始化与关联属性管理器。"""

    service_name: str
    otlp_endpoint: str | None = None
    enabled: bool = True
    tracer_provider: Any = field(default=None)

    def get_tracer(self, instrumentation_name: str = "etl-agent") -> Any:
        """获取 OpenTelemetry Tracer；若未安装 SDK 则返回空操作占位 Tracer。"""
        if _HAS_OTEL_SDK and self.tracer_provider is not None:
            return self.tracer_provider.get_tracer(instrumentation_name)
        return _NoOpTracer()


class _NoOpSpan:
    """精简环境下的空操作 Span，支持 set_attribute 与上下文管理器协议。"""

    def __init__(self) -> None:
        self.attributes: dict[str, Any] = {}

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value

    def __enter__(self) -> "_NoOpSpan":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        return None


class _NoOpTracer:
    """精简环境下的空操作 Tracer。"""

    def start_as_current_span(self, name: str, **kwargs: Any) -> _NoOpSpan:
        return _NoOpSpan()


def set_span_correlation_attributes(span: Any, **attrs: str | None) -> None:
    """向当前 Span 写入 contracts/observability/telemetry_spec.md 定义的 8 个关联属性。"""
    if span is None or not hasattr(span, "set_attribute"):
        return
    for key in CORRELATION_ATTRIBUTE_KEYS:
        val = attrs.get(key)
        if val is not None:
            span.set_attribute(key, str(val))


def init_telemetry(
    service_name: str = "etl-agent-api",
    otlp_endpoint: str | None = None,
    use_console_exporter: bool = False,
) -> TelemetryManager:
    """初始化 OpenTelemetry TracerProvider（支持 OTLP gRPC 导出与精简环境降级）。"""
    if not _HAS_OTEL_SDK:
        return TelemetryManager(service_name=service_name, otlp_endpoint=otlp_endpoint, enabled=False)

    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)

    if otlp_endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # type: ignore[import-not-found]
                OTLPSpanExporter,
            )

            exporter = OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True)
            provider.add_span_processor(BatchSpanProcessor(exporter))
        except ImportError:
            pass

    if use_console_exporter:
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

    otel_trace.set_tracer_provider(provider)
    return TelemetryManager(
        service_name=service_name,
        otlp_endpoint=otlp_endpoint,
        enabled=True,
        tracer_provider=provider,
    )
