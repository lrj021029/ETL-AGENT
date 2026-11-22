"""统一错误码注册表与标准 ErrorResponse 响应构造器。

严格对齐 contracts/openapi/error_codes.yaml 与 contracts/openapi/openapi.yaml，
保证所有 API 错误均返回 {error_code, message, detail, trace_id} 并过滤潜在凭据明文。
"""
from __future__ import annotations

import re
import uuid
from enum import StrEnum
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field


class ErrorCode(StrEnum):
    """冻结契约定义的标准业务与安全错误码枚举。"""

    # 认证与多租户 IAM
    AUTH_INVALID_CREDENTIALS = "AUTH_INVALID_CREDENTIALS"
    AUTH_TOKEN_EXPIRED = "AUTH_TOKEN_EXPIRED"
    AUTH_REFRESH_REUSE_DETECTED = "AUTH_REFRESH_REUSE_DETECTED"
    AUTH_OTP_REQUIRED = "AUTH_OTP_REQUIRED"
    AUTH_OTP_INVALID_OR_EXPIRED = "AUTH_OTP_INVALID_OR_EXPIRED"
    TENANT_CONTEXT_MISSING = "TENANT_CONTEXT_MISSING"
    TENANT_ACCESS_DENIED = "TENANT_ACCESS_DENIED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    SYSTEM_ROLE_IMMUTABLE = "SYSTEM_ROLE_IMMUTABLE"
    SOD_VIOLATION = "SOD_VIOLATION"

    # 连接器、元数据、文件与凭据管理
    CONNECTION_TEST_FAILED = "CONNECTION_TEST_FAILED"
    SECRET_PROVIDER_ERROR = "SECRET_PROVIDER_ERROR"
    METADATA_PROBE_FAILED = "METADATA_PROBE_FAILED"
    FILE_PATH_TRAVERSAL_BLOCKED = "FILE_PATH_TRAVERSAL_BLOCKED"
    CONNECTOR_CAPABILITY_UNSUPPORTED = "CONNECTOR_CAPABILITY_UNSUPPORTED"

    # Pipeline、编译器、SQL AST 门禁与 Agent
    DSL_SCHEMA_INVALID = "DSL_SCHEMA_INVALID"
    SQL_AST_POLICY_VIOLATION = "SQL_AST_POLICY_VIOLATION"
    HOCON_VALIDATION_FAILED = "HOCON_VALIDATION_FAILED"
    DETERMINISTIC_GATE_FAILED = "DETERMINISTIC_GATE_FAILED"
    REPAIR_LOOP_EXHAUSTED = "REPAIR_LOOP_EXHAUSTED"
    AGENT_TOOL_FORBIDDEN = "AGENT_TOOL_FORBIDDEN"
    LLM_PROVIDER_UNAVAILABLE = "LLM_PROVIDER_UNAVAILABLE"

    # Harness、PDP、审批、Capability 与防重放
    PDP_DENIED_P0 = "PDP_DENIED_P0"
    PREPARATION_INVALIDATED = "PREPARATION_INVALIDATED"
    PREPARATION_EXPIRED = "PREPARATION_EXPIRED"
    APPROVAL_SELF_APPROVE_FORBIDDEN = "APPROVAL_SELF_APPROVE_FORBIDDEN"
    APPROVAL_SLOT_CONFLICT = "APPROVAL_SLOT_CONFLICT"
    APPROVAL_INCOMPLETE = "APPROVAL_INCOMPLETE"
    CAPABILITY_INVALID_SIGNATURE = "CAPABILITY_INVALID_SIGNATURE"
    CAPABILITY_EXPIRED = "CAPABILITY_EXPIRED"
    CAPABILITY_REPLAY_DETECTED = "CAPABILITY_REPLAY_DETECTED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"

    # 执行、运行监督、发布、回滚与 CDC
    STATE_TRANSITION_INVALID = "STATE_TRANSITION_INVALID"
    SEATUNNEL_SUBMIT_UNKNOWN = "SEATUNNEL_SUBMIT_UNKNOWN"
    SUPERVISION_THRESHOLD_BREACHED = "SUPERVISION_THRESHOLD_BREACHED"
    PUBLISH_SCHEMA_INCOMPATIBLE = "PUBLISH_SCHEMA_INCOMPATIBLE"
    CDC_RECOVERY_OPERATOR_CONFIRM_REQUIRED = "CDC_RECOVERY_OPERATOR_CONFIRM_REQUIRED"
    EVIDENCE_CHAIN_TAMPER_DETECTED = "EVIDENCE_CHAIN_TAMPER_DETECTED"

    # 数据面行级质量规则错误码
    DQ_NOT_NULL_VIOLATION = "DQ_NOT_NULL_VIOLATION"
    DQ_DATA_TYPE_VIOLATION = "DQ_DATA_TYPE_VIOLATION"
    DQ_REGEX_VIOLATION = "DQ_REGEX_VIOLATION"
    DQ_RANGE_VIOLATION = "DQ_RANGE_VIOLATION"
    DQ_UNIQUE_VIOLATION = "DQ_UNIQUE_VIOLATION"
    DQ_ENUM_VIOLATION = "DQ_ENUM_VIOLATION"
    DQ_RESTRICTED_SQL_VIOLATION = "DQ_RESTRICTED_SQL_VIOLATION"


# 错误码到 HTTP 状态码的契约映射表（严格对齐 contracts/openapi/error_codes.yaml）
ERROR_HTTP_STATUS: dict[str, int] = {
    ErrorCode.AUTH_INVALID_CREDENTIALS: 401,
    ErrorCode.AUTH_TOKEN_EXPIRED: 401,
    ErrorCode.AUTH_REFRESH_REUSE_DETECTED: 401,
    ErrorCode.AUTH_OTP_REQUIRED: 403,
    ErrorCode.AUTH_OTP_INVALID_OR_EXPIRED: 400,
    ErrorCode.TENANT_CONTEXT_MISSING: 403,
    ErrorCode.TENANT_ACCESS_DENIED: 403,
    ErrorCode.PERMISSION_DENIED: 403,
    ErrorCode.SYSTEM_ROLE_IMMUTABLE: 409,
    ErrorCode.SOD_VIOLATION: 403,
    ErrorCode.CONNECTION_TEST_FAILED: 400,
    ErrorCode.SECRET_PROVIDER_ERROR: 502,
    ErrorCode.METADATA_PROBE_FAILED: 502,
    ErrorCode.FILE_PATH_TRAVERSAL_BLOCKED: 403,
    ErrorCode.CONNECTOR_CAPABILITY_UNSUPPORTED: 422,
    ErrorCode.DSL_SCHEMA_INVALID: 422,
    ErrorCode.SQL_AST_POLICY_VIOLATION: 422,
    ErrorCode.HOCON_VALIDATION_FAILED: 422,
    ErrorCode.DETERMINISTIC_GATE_FAILED: 422,
    ErrorCode.REPAIR_LOOP_EXHAUSTED: 422,
    ErrorCode.AGENT_TOOL_FORBIDDEN: 403,
    ErrorCode.LLM_PROVIDER_UNAVAILABLE: 502,
    ErrorCode.PDP_DENIED_P0: 403,
    ErrorCode.PREPARATION_INVALIDATED: 409,
    ErrorCode.PREPARATION_EXPIRED: 409,
    ErrorCode.APPROVAL_SELF_APPROVE_FORBIDDEN: 403,
    ErrorCode.APPROVAL_SLOT_CONFLICT: 403,
    ErrorCode.APPROVAL_INCOMPLETE: 409,
    ErrorCode.CAPABILITY_INVALID_SIGNATURE: 403,
    ErrorCode.CAPABILITY_EXPIRED: 403,
    ErrorCode.CAPABILITY_REPLAY_DETECTED: 409,
    ErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ErrorCode.STATE_TRANSITION_INVALID: 409,
    ErrorCode.SEATUNNEL_SUBMIT_UNKNOWN: 202,
    ErrorCode.SUPERVISION_THRESHOLD_BREACHED: 422,
    ErrorCode.PUBLISH_SCHEMA_INCOMPATIBLE: 422,
    ErrorCode.CDC_RECOVERY_OPERATOR_CONFIRM_REQUIRED: 409,
    ErrorCode.EVIDENCE_CHAIN_TAMPER_DETECTED: 500,
    ErrorCode.DQ_NOT_NULL_VIOLATION: 422,
    ErrorCode.DQ_DATA_TYPE_VIOLATION: 422,
    ErrorCode.DQ_REGEX_VIOLATION: 422,
    ErrorCode.DQ_RANGE_VIOLATION: 422,
    ErrorCode.DQ_UNIQUE_VIOLATION: 422,
    ErrorCode.DQ_ENUM_VIOLATION: 422,
    ErrorCode.DQ_RESTRICTED_SQL_VIOLATION: 422,
}

# 敏感键名与连接串凭据脱敏正则
_SECRET_KEY_PATTERN = re.compile(
    r"(password|passwd|secret|private_key|api_key|access_token|refresh_token|authorization)",
    re.IGNORECASE,
)
_URI_CREDENTIAL_PATTERN = re.compile(r"([a-zA-Z0-9+.-]+://[^:/\s]+:)([^@\s]+)(@)")


def redact_sensitive_value(value: Any) -> Any:
    """递归清洗字典、列表与字符串中的密码、Token、私钥或带凭据的连接 URI。"""
    if isinstance(value, str):
        cleaned = _URI_CREDENTIAL_PATTERN.sub(r"\1***REDACTED***\3", value)
        if "-----BEGIN" in cleaned and "PRIVATE KEY-----" in cleaned:
            return "***REDACTED_PRIVATE_KEY***"
        return cleaned
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for k, v in value.items():
            if _SECRET_KEY_PATTERN.search(str(k)) and str(k) not in (
                "secret_ref_id",
                "secret_version",
                "secret_path",
                "secret_placeholders_json",
            ):
                result[k] = "***REDACTED***"
            else:
                result[k] = redact_sensitive_value(v)
        return result
    if isinstance(value, list):
        return [redact_sensitive_value(item) for item in value]
    return value


class ErrorResponse(BaseModel):
    """OpenAPI 标准错误响应 Schema。"""

    error_code: str = Field(..., description="契约定义的稳定错误码")
    message: str = Field(..., description="面向调用方的错误摘要信息")
    detail: dict[str, Any] | list[Any] | None = Field(default=None, description="附加结构化上下文")
    trace_id: str = Field(default_factory=lambda: f"tr-{uuid.uuid4().hex[:16]}", description="全链路追踪 ID")


class AppError(Exception):
    """控制面统一业务与安全异常基类。"""

    def __init__(
        self,
        error_code: ErrorCode | str,
        message: str,
        detail: dict[str, Any] | list[Any] | None = None,
        http_status: int | None = None,
        trace_id: str | None = None,
    ) -> None:
        super().__init__(message)
        code_str = str(error_code.value if isinstance(error_code, ErrorCode) else error_code)
        self.error_code = code_str
        self.message = redact_sensitive_value(message)
        self.detail = redact_sensitive_value(detail)
        self.http_status = http_status or ERROR_HTTP_STATUS.get(code_str, 400)
        self.trace_id = trace_id or f"tr-{uuid.uuid4().hex[:16]}"

    def to_response_model(self, trace_id: str | None = None) -> ErrorResponse:
        """转换为标准 ErrorResponse 模型。"""
        return ErrorResponse(
            error_code=self.error_code,
            message=self.message,
            detail=self.detail,
            trace_id=trace_id or self.trace_id,
        )


def resolve_trace_id(request: Request | None = None) -> str:
    """从请求头或上下文中提取或生成 trace_id。"""
    if request is not None:
        header_trace = request.headers.get("X-Trace-Id") or request.headers.get("traceparent")
        if header_trace:
            return str(header_trace)[:64]
        if hasattr(request.state, "trace_id") and request.state.trace_id:
            return str(request.state.trace_id)
    return f"tr-{uuid.uuid4().hex[:16]}"


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    """FastAPI 全局 AppError 异常处理器。"""
    trace_id = resolve_trace_id(request)
    payload = exc.to_response_model(trace_id=trace_id).model_dump()
    return JSONResponse(status_code=exc.http_status, content=payload)
