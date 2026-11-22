"""跨模块协作抽象协议（Protocol Interfaces）。

定义 Backend 与 SecretProvider、PDP、ToolBroker、DataPlane、EvidenceLedger 等兄弟模块
之间的解耦接口，使得各模块可通过依赖注入无缝插拔，避免循环引用与越界修改。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from uuid import UUID


@dataclass(frozen=True)
class SecretMetadata:
    """凭据元数据视图（绝不包含明文 secret 值）。"""

    secret_ref_id: UUID
    organization_id: UUID
    project_id: UUID
    provider_type: str
    secret_path: str
    current_version: int
    masked_preview: str
    status: str


@runtime_checkable
class SecretProviderProtocol(Protocol):
    """SecretProvider 标准接口（遵循 TECHNICAL_SPEC §5.3 与 DEC-003）。"""

    def put_secret(
        self,
        *,
        organization_id: UUID,
        project_id: UUID,
        secret_name: str,
        secret_payload: dict[str, Any],
        actor_id: UUID,
    ) -> SecretMetadata:
        """保存或创建新凭据，仅返回脱敏引用元数据。"""
        ...

    def get_secret_for_execution(
        self,
        *,
        organization_id: UUID,
        project_id: UUID,
        secret_ref_id: UUID,
        version: int | None = None,
    ) -> dict[str, Any]:
        """仅在受控执行或受管连通性测试最短生命周期内物化凭据。"""
        ...

    def rotate_secret(
        self,
        *,
        organization_id: UUID,
        project_id: UUID,
        secret_ref_id: UUID,
        new_secret_payload: dict[str, Any],
        actor_id: UUID,
    ) -> SecretMetadata:
        """轮转凭据并递增 secret_version。"""
        ...

    def revoke_secret(
        self,
        *,
        organization_id: UUID,
        project_id: UUID,
        secret_ref_id: UUID,
        actor_id: UUID,
    ) -> SecretMetadata:
        """吊销凭据引用。"""
        ...

    def get_secret_metadata(
        self,
        *,
        organization_id: UUID,
        project_id: UUID,
        secret_ref_id: UUID,
    ) -> SecretMetadata:
        """查询脱敏凭据元数据（禁止返回明文）。"""
        ...


@runtime_checkable
class PdpEngineProtocol(Protocol):
    """RBAC + ABAC 策略决策点（PDP）协议接口（对齐 contracts/harness/pdp_contract.json）。"""

    def evaluate(self, request_payload: dict[str, Any]) -> dict[str, Any]:
        """执行策略评估并返回 decision、risk_level、required_approval_slots 等。"""
        ...


@runtime_checkable
class ToolBrokerProtocol(Protocol):
    """Tool Broker 唯一外部副作用出口协议（遵循 ADR-003）。"""

    def commit_preparation(
        self,
        *,
        organization_id: UUID,
        project_id: UUID,
        preparation_id: UUID,
        actor_id: UUID,
        idempotency_key: str,
        trace_id: str,
    ) -> dict[str, Any]:
        """重验 Preparation、签发并原子消费单次 Ed25519 Capability、创建 ExecutionRun 与 Outbox 事件。"""
        ...

    def trigger_standing_schedule(
        self,
        *,
        organization_id: UUID,
        project_id: UUID,
        schedule_id: UUID,
        standing_preparation_id: UUID,
        idempotency_key: str,
        trace_id: str,
    ) -> dict[str, Any]:
        """以受限系统身份 system:scheduler 触发定时调度，重验指纹并签发当次 5 分钟单次 Capability。"""
        ...


@runtime_checkable
class DataPlaneAdapterProtocol(Protocol):
    """SeaTunnel Zeta 数据面适配器协议接口。"""

    def submit_job(self, *, stable_submit_id: str, hocon_text: str, job_name: str) -> dict[str, Any]:
        """向 SeaTunnel Zeta 提交作业。"""
        ...

    def get_job_status(self, *, external_job_id: str) -> dict[str, Any]:
        """查询 SeaTunnel 作业状态与指标。"""
        ...

    def cancel_job(self, *, external_job_id: str) -> dict[str, Any]:
        """受管停止或取消 SeaTunnel 作业。"""
        ...
