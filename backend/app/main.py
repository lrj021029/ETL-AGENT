"""FastAPI 控制面主入口应用工厂（Application Factory）。

组装全局 Trace ID 中间件、统一 ErrorResponse 异常处理器、CORS 策略与 16 个控制面模块路由。
"""
from __future__ import annotations

import uuid
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.routers import (
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
from app.config import Settings, get_settings
from app.shared.errors import AppError, ErrorCode, ErrorResponse, app_error_handler, resolve_trace_id


def create_app(
    *,
    settings: Settings | None = None,
    session_factory: Callable[[], Session] | None = None,
) -> FastAPI:
    """创建并配置 ETL-Agent FastAPI 控制面应用实例。"""
    cfg = settings or get_settings()
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

    # 本地开发控制台 CORS 配置（仅允许本机回环前端端口）
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
    async def trace_id_middleware(request: Request, call_next):
        """为每个请求分配或透传全链路 trace_id，并在响应头中回传 X-Trace-Id。"""
        trace_id = (
            request.headers.get("X-Trace-Id")
            or request.headers.get("traceparent")
            or f"tr-{uuid.uuid4().hex[:16]}"
        )
        request.state.trace_id = str(trace_id)[:64]
        response = await call_next(request)
        response.headers["X-Trace-Id"] = request.state.trace_id
        return response

    # 注册统一业务/安全异常处理器
    app.add_exception_handler(AppError, app_error_handler)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        """将 FastAPI 请求参数校验错误统一转换为契约定义的 ErrorResponse 格式。"""
        trace_id = resolve_trace_id(request)
        err_model = ErrorResponse(
            error_code=ErrorCode.DSL_SCHEMA_INVALID.value,
            message="请求参数格式校验未通过",
            detail=exc.errors(),
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
