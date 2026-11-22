"""三类受管回滚（SHADOW_CLEANUP / SWAP_RESTORE / CDC_RECOVER）与 C+D 策略执行器实现。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import re
from typing import Any, Callable
import uuid

from backend.app.execution.state_machine import (
    ConfirmationState,
    ExecutionMode,
    ExecutionRunRecord,
    ExecutionStateMachine,
)
from backend.app.publishing.doris_adapter import (
    DorisPublishAdapter,
    DorisTableSchemaSpec,
    PublishedDatasetVersionRecord,
)

_SAFE_TABLE_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")


class RollbackType(str, Enum):
    """三类受管回滚类型枚举。"""

    SHADOW_CLEANUP = "SHADOW_CLEANUP"
    SWAP_RESTORE = "SWAP_RESTORE"
    CDC_RECOVER = "CDC_RECOVER"


class CdcRecoveryOperatorConfirmRequiredError(Exception):
    """当 CDC 处于 CHECKPOINT_LOST 或 POSITION_EXPIRED 且未获 Operator 显式确认重新快照时抛出。"""

    error_code: str = "CDC_RECOVERY_OPERATOR_CONFIRM_REQUIRED"

    def __init__(self, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}


@dataclass(frozen=True)
class RollbackCommandContext:
    """经 Harness / Tool Broker 授权的回滚命令上下文。"""

    organization_id: str
    project_id: str
    preparation_id: str
    capability_jti: str
    command_id: str
    initiated_by: str
    trace_id: str = ""

    def __post_init__(self) -> None:
        """强制验证 Harness 授权三要素（preparation_id, capability_jti, command_id）非空。"""
        if not self.preparation_id or not self.capability_jti or not self.command_id:
            raise ValueError("回滚命令必须携带非空的 Harness 授权三要素: preparation_id, capability_jti, command_id")


@dataclass
class RollbackRunRecord:
    """受管回滚执行记录（对应 `contracts/db/erd.md` §2.5 `rollback_runs` 表）。"""

    id: str
    organization_id: str
    project_id: str
    rollback_type: RollbackType
    target_execution_id: str
    target_published_version_id: str | None
    preparation_id: str
    capability_jti: str
    command_id: str
    status: str
    result_json: dict[str, Any]
    initiated_by: str
    started_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    ended_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """序列化为持久化字典。"""
        return {
            "id": self.id,
            "organization_id": self.organization_id,
            "project_id": self.project_id,
            "rollback_type": self.rollback_type.value,
            "target_execution_id": self.target_execution_id,
            "target_published_version_id": self.target_published_version_id,
            "preparation_id": self.preparation_id,
            "capability_jti": self.capability_jti,
            "command_id": self.command_id,
            "status": self.status,
            "result_json": dict(self.result_json),
            "initiated_by": self.initiated_by,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
        }


class RollbackExecutor:
    """受管数据面回滚与 CDC C+D 恢复执行器。"""

    def __init__(
        self,
        *,
        doris_adapter: DorisPublishAdapter | None = None,
        sql_executor: Callable[[str], dict[str, Any]] | None = None,
    ) -> None:
        self._doris_adapter = doris_adapter or DorisPublishAdapter(sql_executor=sql_executor)
        self._sql_executor = sql_executor
        self.executed_sql_log: list[str] = []
        self._executed_commands: dict[str, RollbackRunRecord] = {}

    def execute_shadow_cleanup(
        self,
        *,
        ctx: RollbackCommandContext,
        run: ExecutionRunRecord,
        shadow_table_name: str,
    ) -> RollbackRunRecord:
        """回滚类型 1：未发布影子数据清理（`SHADOW_CLEANUP`）。

        清理受管影子表（`DROP TABLE IF EXISTS <shadow_table>`），并将批处理状态迁移至 `ROLLING_BACK -> ROLLED_BACK`。
        按 `command_id` 保持幂等：相同 `command_id` 重投时直接返回已创建的 `RollbackRunRecord`。
        """
        if ctx.command_id in self._executed_commands:
            return self._executed_commands[ctx.command_id]

        if not _SAFE_TABLE_PATTERN.match(shadow_table_name.strip()) or "__shadow_" not in shadow_table_name:
            raise ValueError(
                f"SHADOW_CLEANUP 仅允许清理包含 '__shadow_' 标识的受管影子表: {shadow_table_name}"
            )

        ExecutionStateMachine.transition(
            run,
            "ROLLING_BACK",
            reason_code="ROLLBACK_SHADOW_CLEANUP_STARTED",
            actor_id=ctx.initiated_by,
            confirmation_state=ConfirmationState.REQUEST_SUBMITTED,
            trace_id=ctx.trace_id,
        )

        drop_sql = f"DROP TABLE IF EXISTS {shadow_table_name.strip()}"
        if self._sql_executor is not None:
            self._sql_executor(drop_sql)
        self.executed_sql_log.append(drop_sql)

        ExecutionStateMachine.transition(
            run,
            "ROLLED_BACK",
            reason_code="ROLLBACK_SHADOW_CLEANUP_COMPLETED",
            actor_id=ctx.initiated_by,
            confirmation_state=ConfirmationState.ENGINE_CONFIRMED,
            trace_id=ctx.trace_id,
        )

        now_iso = datetime.now(timezone.utc).isoformat()
        record = RollbackRunRecord(
            id=str(uuid.uuid4()),
            organization_id=ctx.organization_id,
            project_id=ctx.project_id,
            rollback_type=RollbackType.SHADOW_CLEANUP,
            target_execution_id=run.execution_id,
            target_published_version_id=None,
            preparation_id=ctx.preparation_id,
            capability_jti=ctx.capability_jti,
            command_id=ctx.command_id,
            status="SUCCEEDED",
            result_json={
                "cleaned_shadow_table": shadow_table_name.strip(),
                "cleanup_sql": drop_sql,
                "historical_records_preserved": True,
            },
            initiated_by=ctx.initiated_by,
            ended_at=now_iso,
        )
        self._executed_commands[ctx.command_id] = record
        return record

    def execute_swap_restore(
        self,
        *,
        ctx: RollbackCommandContext,
        run: ExecutionRunRecord,
        published_version: PublishedDatasetVersionRecord,
        target_schema: DorisTableSchemaSpec,
        archive_schema: DorisTableSchemaSpec,
    ) -> RollbackRunRecord:
        """回滚类型 2：已发布数据集版本恢复（`SWAP_RESTORE`）。

        使用 `PublishedDatasetVersionRecord.archive_table_ref` 经 Schema 兼容门禁校验后，
        再次执行 `ALTER TABLE <target> REPLACE WITH TABLE <archive> PROPERTIES('swap'='true')` 恢复旧版本数据。
        """
        if ctx.command_id in self._executed_commands:
            return self._executed_commands[ctx.command_id]

        if not published_version.archive_table_ref:
            raise ValueError(
                f"已发布版本 {published_version.id} 缺少 archive_table_ref，无法执行 SWAP_RESTORE"
            )

        # 恢复前同样强制执行 Schema 兼容校验
        self._doris_adapter.verify_schema_compatibility(target_schema, archive_schema)

        ExecutionStateMachine.transition(
            run,
            "ROLLING_BACK",
            reason_code="ROLLBACK_SWAP_RESTORE_STARTED",
            actor_id=ctx.initiated_by,
            confirmation_state=ConfirmationState.REQUEST_SUBMITTED,
            trace_id=ctx.trace_id,
        )

        restore_sql = DorisPublishAdapter.build_atomic_swap_sql(
            published_version.target_table_ref,
            published_version.archive_table_ref,
        )
        if self._sql_executor is not None:
            self._sql_executor(restore_sql)
        self.executed_sql_log.append(restore_sql)

        ExecutionStateMachine.transition(
            run,
            "ROLLED_BACK",
            reason_code="ROLLBACK_SWAP_RESTORE_COMPLETED",
            actor_id=ctx.initiated_by,
            confirmation_state=ConfirmationState.ENGINE_CONFIRMED,
            trace_id=ctx.trace_id,
        )

        now_iso = datetime.now(timezone.utc).isoformat()
        record = RollbackRunRecord(
            id=str(uuid.uuid4()),
            organization_id=ctx.organization_id,
            project_id=ctx.project_id,
            rollback_type=RollbackType.SWAP_RESTORE,
            target_execution_id=run.execution_id,
            target_published_version_id=published_version.id,
            preparation_id=ctx.preparation_id,
            capability_jti=ctx.capability_jti,
            command_id=ctx.command_id,
            status="SUCCEEDED",
            result_json={
                "restored_target_table": published_version.target_table_ref,
                "restored_from_archive_table": published_version.archive_table_ref,
                "restored_previous_version_id": published_version.previous_version_id,
                "restore_sql": restore_sql,
                "historical_records_preserved": True,
            },
            initiated_by=ctx.initiated_by,
            ended_at=now_iso,
        )
        self._executed_commands[ctx.command_id] = record
        return record

    def execute_cdc_recover(
        self,
        *,
        ctx: RollbackCommandContext,
        run: ExecutionRunRecord,
        checkpoint_valid: bool,
        position_expired: bool = False,
        replication_slot_active: bool = True,
        operator_confirmed_resnapshot: bool = False,
        operator_confirmation_reason: str | None = None,
    ) -> RollbackRunRecord:
        """回滚类型 3：CDC 作业 C+D 恢复策略执行器（`CDC_RECOVER`）。

        C+D 策略核心不变量（DEC-010 / TECHNICAL_SPEC §10.5）：
        1. **策略 C（历史检查点优先）**：若存在合法且未过期的 `checkpoint_ref`（`checkpoint_valid=True`、
           `position_expired=False`、`replication_slot_active=True` 且当前状态非 `CHECKPOINT_LOST`/`POSITION_EXPIRED`），
           直接从该历史检查点恢复至 `RECOVERING -> STREAMING`。
        2. **策略 D（风险阻断与 Operator 显式确认）**：一旦处于 `CHECKPOINT_LOST` 或 `POSITION_EXPIRED`，
           或检测到检查点无效、Binlog/WAL 位点过期、PostgreSQL 复制槽失效：
           - 若 `operator_confirmed_resnapshot` 不为 `True` 或缺少 `operator_confirmation_reason`，
             **必须立即阻断并抛出 `CdcRecoveryOperatorConfirmRequiredError`（`CDC_RECOVERY_OPERATOR_CONFIRM_REQUIRED`）**，
             严禁静默丢弃位点或自动回退全量重跑！
           - 仅在 Operator 显式确认后，才允许通过 `RECOVERING -> INITIAL_SNAPSHOT` 重新快照并记录审计。
        """
        if ctx.command_id in self._executed_commands:
            return self._executed_commands[ctx.command_id]

        if run.execution_mode != ExecutionMode.CDC:
            raise ValueError("CDC_RECOVER 仅适用于 CDC 模式的 ExecutionRun")

        has_checkpoint_or_offset_fault = (
            run.status in ("CHECKPOINT_LOST", "POSITION_EXPIRED")
            or not checkpoint_valid
            or position_expired
            or not replication_slot_active
            or not run.checkpoint_ref
        )

        if not has_checkpoint_or_offset_fault:
            # 路径 C：存在有效历史检查点，执行无损增量位点恢复 (RECOVERING -> STREAMING)
            ExecutionStateMachine.transition(
                run,
                "RECOVERING",
                reason_code="CDC_RECOVER_FROM_VALID_CHECKPOINT",
                actor_id=ctx.initiated_by,
                confirmation_state=ConfirmationState.REQUEST_SUBMITTED,
                trace_id=ctx.trace_id,
            )
            ExecutionStateMachine.transition(
                run,
                "STREAMING",
                reason_code="CDC_CHECKPOINT_RESTORED_STREAMING",
                actor_id=ctx.initiated_by,
                confirmation_state=ConfirmationState.ENGINE_CONFIRMED,
                trace_id=ctx.trace_id,
            )
            now_iso = datetime.now(timezone.utc).isoformat()
            record = RollbackRunRecord(
                id=str(uuid.uuid4()),
                organization_id=ctx.organization_id,
                project_id=ctx.project_id,
                rollback_type=RollbackType.CDC_RECOVER,
                target_execution_id=run.execution_id,
                target_published_version_id=None,
                preparation_id=ctx.preparation_id,
                capability_jti=ctx.capability_jti,
                command_id=ctx.command_id,
                status="SUCCEEDED",
                result_json={
                    "recovery_policy": "POLICY_C_HISTORICAL_CHECKPOINT",
                    "restored_checkpoint_ref": run.checkpoint_ref,
                    "resnapshot_triggered": False,
                    "target_cdc_status": run.status,
                },
                initiated_by=ctx.initiated_by,
                ended_at=now_iso,
            )
            self._executed_commands[ctx.command_id] = record
            return record

        # 路径 D：检查点丢失或位点/Slot 过期，强制检查 Operator 是否已显式确认
        if not operator_confirmed_resnapshot or not (operator_confirmation_reason or "").strip():
            raise CdcRecoveryOperatorConfirmRequiredError(
                f"CDC 作业 {run.execution_id} 处于位点或检查点失效状态 "
                f"(status={run.status}, checkpoint_valid={checkpoint_valid}, "
                f"position_expired={position_expired}, slot_active={replication_slot_active})。"
                "按 C+D 恢复策略，严禁静默重新快照，必须由 Operator 显式确认后方可执行！",
                detail={
                    "execution_id": run.execution_id,
                    "current_status": run.status,
                    "checkpoint_ref": run.checkpoint_ref,
                    "checkpoint_valid": checkpoint_valid,
                    "position_expired": position_expired,
                    "replication_slot_active": replication_slot_active,
                    "required_confirmation_flag": "operator_confirmed_resnapshot=True",
                },
            )

        # Operator 已显式确认并附带理由：执行受管重新快照恢复 (RECOVERING -> INITIAL_SNAPSHOT)
        ExecutionStateMachine.transition(
            run,
            "RECOVERING",
            reason_code="CDC_RECOVER_OPERATOR_CONFIRMED_RESNAPSHOT",
            actor_id=ctx.initiated_by,
            confirmation_state=ConfirmationState.REQUEST_SUBMITTED,
            trace_id=ctx.trace_id,
            detail={"operator_confirmation_reason": operator_confirmation_reason},
        )
        ExecutionStateMachine.transition(
            run,
            "INITIAL_SNAPSHOT",
            reason_code="CDC_RESNAPSHOT_STARTED_BY_OPERATOR_CONFIRM",
            actor_id=ctx.initiated_by,
            confirmation_state=ConfirmationState.ENGINE_CONFIRMED,
            trace_id=ctx.trace_id,
            detail={"operator_confirmation_reason": operator_confirmation_reason},
        )

        now_iso = datetime.now(timezone.utc).isoformat()
        record = RollbackRunRecord(
            id=str(uuid.uuid4()),
            organization_id=ctx.organization_id,
            project_id=ctx.project_id,
            rollback_type=RollbackType.CDC_RECOVER,
            target_execution_id=run.execution_id,
            target_published_version_id=None,
            preparation_id=ctx.preparation_id,
            capability_jti=ctx.capability_jti,
            command_id=ctx.command_id,
            status="SUCCEEDED",
            result_json={
                "recovery_policy": "POLICY_D_OPERATOR_CONFIRMED_RESNAPSHOT",
                "previous_checkpoint_ref": run.checkpoint_ref,
                "operator_confirmed_resnapshot": True,
                "operator_confirmation_reason": operator_confirmation_reason.strip(),
                "confirmed_by_operator_id": ctx.initiated_by,
                "resnapshot_triggered": True,
                "target_cdc_status": run.status,
            },
            initiated_by=ctx.initiated_by,
            ended_at=now_iso,
        )
        self._executed_commands[ctx.command_id] = record
        return record
