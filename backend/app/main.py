"""FastAPI 控制面主入口应用工厂（Application Factory）。

组装全局 Trace ID 与关联上下文中间件、敏感信息脱敏（Redaction）中间件、统一 ErrorResponse 异常处理器、
CORS 策略、`/metrics` Prometheus 遥测端点挂载以及 16 个控制面模块的全量领域装配（`wire_application`）。
"""
from __future__ import annotations

import json
import re
import sys
import uuid
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

# 确保仓库根目录与 backend 目录均在 Python 模块搜索路径中
_BACKEND_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = _BACKEND_DIR.parent
for _p in (str(_REPO_ROOT), str(_BACKEND_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app.agent.context import TenantAccessDenied  # noqa: E402
from app.api.routers import (  # noqa: E402
    auth_router,
    connections_router,
    execution_router,
    extension_hooks_router,
    health_router,
    metadata_router,
    organizations_router,
    projects_router,
    scheduling_router,
)
from app.api.wiring import wire_application  # noqa: E402
from app.config import Settings, get_settings  # noqa: E402
from app.observability.logging import (  # noqa: E402
    bind_correlation_context,
    clear_correlation_context,
)
from app.observability.telemetry import mount_metrics_endpoint  # noqa: E402
from app.security.errors import HarnessError  # noqa: E402
from app.security.redaction import REDACTED, install_redaction, redact_text  # noqa: E402
from app.shared.errors import (  # noqa: E402
    AppError,
    ErrorCode,
    ErrorResponse,
    app_error_handler,
    resolve_trace_id,
)

# 允许向已认证客户端返回自身签发的会话 JWT 令牌的认证端点集合
_AUTH_TOKEN_ISSUE_PATHS = frozenset({"/api/v1/auth/otp/verify", "/api/v1/auth/refresh"})

# 属于安全元数据引用而非明文机密的键名白名单（不得将其结构化引用值替换为脱敏占位串）
_SAFE_METADATA_KEYS = frozenset(
    {
        "secret_ref_id",
        "secret_version",
        "secret_versions",
        "secret_path",
        "secret_masked_preview",
        "secret_refs",
        "secret_placeholders_json",
        "token_type",
    }
)
_RAW_SECRET_KEY_NAMES = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "secret",
        "secret_key",
        "access_key_secret",
        "private_key",
        "api_key",
        "auth_token",
        "capability_jws",
    }
)


_SECRET_PLACEHOLDER_TOKEN_RE = re.compile(
    r'(?:[A-Za-z0-9_.\-]+\s*[=:]\s*)?"?\$\{SECRET:[A-Za-z0-9_.\-/]+\}"?'
)


def _safe_redact_string(text: str) -> str:
    """在保护合法 ${SECRET:...} 占位符不被误伤的前提下，调用 security.redaction.redact_text 清洗明文机密。"""
    if "${SECRET:" not in text:
        return redact_text(text)
    placeholders: list[str] = []

    def _stash(m: re.Match[str]) -> str:
        idx = len(placeholders)
        placeholders.append(m.group(0))
        return f"__ETL_SAFE_PH_{idx}__"

    stashed = _SECRET_PLACEHOLDER_TOKEN_RE.sub(_stash, text)
    redacted = redact_text(stashed)
    for idx, ph in enumerate(placeholders):
        redacted = redacted.replace(f"__ETL_SAFE_PH_{idx}__", ph)
    return redacted


def _redact_api_payload(obj: object) -> object:
    """对 HTTP 响应 JSON 进行递归敏感信息清洗，同时保留合法的 secret_ref_id / secret_refs 元数据引用结构。"""
    if isinstance(obj, dict):
        cleaned: dict[str, object] = {}
        for k, v in obj.items():
            k_str = str(k)
            if k_str in _SAFE_METADATA_KEYS:
                cleaned[k_str] = _redact_api_payload(v) if isinstance(v, (dict, list)) else v
            elif k_str.lower() in _RAW_SECRET_KEY_NAMES:
                if isinstance(v, str) and v.startswith("${SECRET:") and v.endswith("}"):
                    cleaned[k_str] = v
                else:
                    cleaned[k_str] = REDACTED
            else:
                cleaned[k_str] = _redact_api_payload(v)
        return cleaned
    if isinstance(obj, list):
        return [_redact_api_payload(item) for item in obj]
    if isinstance(obj, str):
        return _safe_redact_string(obj)
    return obj


def create_app(
    *,
    settings: Settings | None = None,
    session_factory: Callable[[], Session] | None = None,
) -> FastAPI:
    """创建并配置 ETL-Agent FastAPI 控制面应用实例。"""
    cfg = settings or get_settings()
    install_redaction()

    app = FastAPI(
        title="ETL-Agent Control Plane API",
        version=cfg.service_version,
        description=(
            "Layer 0 Frozen OpenAPI Contract covering all 16 control-plane modules. "
            "Every project-scoped endpoint enforces Organization + Project membership, "
            "RBAC + ABAC, PostgreSQL transaction-scoped RLS, and response secret redaction."
        ),
    )

    if session_factory is not None:
        app.state.session_factory = session_factory

    # 装配全量 Wave 1 兄弟领域服务与路由处理器钩子
    container = wire_application(app, settings=cfg)

    # 挂载 Prometheus `/metrics` ASGI 端点（来自 observability.telemetry）
    mount_metrics_endpoint(app, path="/metrics", registry=container.metrics_registry)

    # 本地开发控制台 CORS 配置（允许本机回环前端端口 127.0.0.1:5173 与 localhost:5173）
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:5173",
            "http://localhost:5173",
            "http://127.0.0.1:4173",
            "http://localhost:4173",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def trace_and_redaction_middleware(request: Request, call_next):
        """分配或透传全链路 trace_id，绑定结构化日志关联上下文，并对 JSON 响应执行敏感凭据脱敏防线。"""
        trace_id = (
            request.headers.get("X-Trace-Id")
            or request.headers.get("traceparent")
            or f"tr-{uuid.uuid4().hex[:16]}"
        )
        request.state.trace_id = str(trace_id)[:64]
        org_header = request.headers.get("X-Organization-Id")
        bind_correlation_context(trace_id=request.state.trace_id, organization_id=org_header)

        try:
            response = await call_next(request)
            response.headers["X-Trace-Id"] = request.state.trace_id

            # 对非流式 JSON 响应体执行兜底脱敏（保留认证端点正常签发的会话 access_token / refresh_token）
            content_type = response.headers.get("content-type", "")
            if (
                "application/json" in content_type
                and request.url.path not in _AUTH_TOKEN_ISSUE_PATHS
                and request.url.path != "/openapi.json"
            ):
                body_chunks: list[bytes] = []
                async for chunk in response.body_iterator:
                    body_chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode("utf-8"))
                raw_bytes = b"".join(body_chunks)
                try:
                    parsed = json.loads(raw_bytes.decode("utf-8"))
                    redacted_obj = _redact_api_payload(parsed)
                    new_bytes = json.dumps(redacted_obj, ensure_ascii=False).encode("utf-8")
                except Exception:
                    new_bytes = raw_bytes
                new_headers = dict(response.headers)
                new_headers["content-length"] = str(len(new_bytes))
                return Response(
                    content=new_bytes,
                    status_code=response.status_code,
                    headers=new_headers,
                    media_type="application/json",
                )
            return response
        finally:
            clear_correlation_context()

    # 注册统一业务与安全内核异常处理器
    app.add_exception_handler(AppError, app_error_handler)

    @app.exception_handler(HarnessError)
    async def harness_error_handler(request: Request, exc: HarnessError) -> JSONResponse:
        """将 Harness 安全内核异常统一转换为标准 ErrorResponse 格式。"""
        app_err = AppError(exc.code, exc.message, detail=exc.details)
        return await app_error_handler(request, app_err)

    @app.exception_handler(TenantAccessDenied)
    async def tenant_access_denied_handler(request: Request, exc: TenantAccessDenied) -> JSONResponse:
        """将 Agent 跨租户越权异常统一转换为 403 TENANT_ACCESS_DENIED ErrorResponse。"""
        app_err = AppError(ErrorCode.TENANT_ACCESS_DENIED, str(exc))
        return await app_error_handler(request, app_err)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        """将 FastAPI 请求参数校验错误统一转换为契约定义的 ErrorResponse 格式。"""
        trace_id = resolve_trace_id(request)
        err_model = ErrorResponse(
            error_code=ErrorCode.DSL_SCHEMA_INVALID.value,
            message="请求参数格式校验未通过",
            detail=_redact_api_payload(exc.errors()),  # type: ignore[arg-type]
            trace_id=trace_id,
        )
        return JSONResponse(status_code=422, content=err_model.model_dump())

    # 挂载全部核心模块路由与兄弟模块扩展钩子路由
    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(organizations_router)
    app.include_router(projects_router)
    app.include_router(connections_router)
    app.include_router(metadata_router)
    app.include_router(execution_router)
    app.include_router(scheduling_router)
    app.include_router(extension_hooks_router)

    return app


app = create_app()
