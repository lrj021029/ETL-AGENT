"""外部作业未知状态对账器（RunReconciler）。

严格遵循 TECHNICAL_SPEC §3.3、§10.3 与 `contracts/harness/execution_state_machine.yaml`：
- 当 `ExecutionRun` 因提交超时或 Worker 重启处于 `UNKNOWN` 状态时，先推进至 `RECONCILING`（`RECONCILIATION_PENDING`）。
- 使用持久化在 `ExternalJobRefRecord` 中的确定性 `stable_submit_id`（对应 Zeta `jobName`）或 `external_job_id` 查询 Zeta 集群。
- 若在 Zeta 中找到对应作业：绑定真实的 `external_job_id`，按引擎状态推进至 `RUNNING`（或 `STREAMING` / `VALIDATING` / `FAILED` / `CANCELLED`），
  并将确认状态更新为 `ENGINE_CONFIRMED`。
- 整个对账过程严格只读查询与状态对齐，绝不盲目重发 `submit-job`。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from backend.app.data_plane.seatunnel_adapter import (
    ExternalJobRefRecord,
    SeaTunnelZetaAdapter,
    build_stable_submit_id,
)
from backend.app.execution.state_machine import (
    ConfirmationState,
    ExecutionMode,
    ExecutionRunRecord,
    ExecutionStateMachine,
)


@dataclass(frozen=True)
class ReconciliationOutcome:
    """单次 `UNKNOWN` 状态对账结果。"""

    execution_id: str
    stable_submit_id: str
    found_on_engine: bool
    external_job_id: str | None
    resolved_status: str
    confirmation_state: ConfirmationState
    engine_raw_status: str | None


class RunReconciler:
    """SeaTunnel Zeta 作业状态对账服务。"""

    def __init__(self, adapter: SeaTunnelZetaAdapter) -> None:
        self._adapter = adapter

    @staticmethod
    def _map_zeta_status_to_run_status(
        mode: ExecutionMode,
        zeta_status: str,
    ) -> str:
        """将 SeaTunnel Zeta 原生作业状态映射为平台受控状态机状态。

        注意：
        - 批处理在 Zeta 侧 `FINISHED` 后，进入平台 `VALIDATING` 阶段（执行 Stage 2 影子表校验后再进入 `PUBLISHING`），
          而不是跳过校验直接标为 `SUCCEEDED`！
        - CDC 永续流作业绝不映射为批处理的 `SUCCEEDED`！
        """
        st = zeta_status.upper().strip()
        if mode == ExecutionMode.BATCH:
            if st in ("RUNNING", "CREATED", "SCHEDULED", "STARTING", "DOING_SAVEPOINT"):
                return "RUNNING"
            if st == "FINISHED":
                return "VALIDATING"
            if st in ("CANCELED", "CANCELLED", "CANCELLING"):
                return "CANCELLED"
            if st in ("FAILED", "FAILING"):
                return "FAILED"
            return "UNKNOWN"

        # CDC 模式状态映射
        if st in ("STARTING", "CREATED", "SCHEDULED"):
            return "INITIAL_SNAPSHOT"
        if st == "RUNNING":
            return "STREAMING"
        if st in ("CANCELED", "CANCELLED", "FINISHED", "SAVEPOINT_DONE"):
            return "STOPPED"
        if st in ("FAILED", "FAILING"):
            return "FAILED"
        return "UNKNOWN"

    def reconcile(
        self,
        run: ExecutionRunRecord,
        *,
        job_ref: ExternalJobRefRecord | None = None,
        actor_id: str = "system:reconciler",
        trace_id: str = "",
    ) -> ReconciliationOutcome:
        """对处于 `UNKNOWN` 或 `RECONCILING` 状态的 `ExecutionRun` 执行确定性对账。"""
        resolved_ref = job_ref or self._adapter.get_job_ref_by_execution(run.execution_id)
        stable_submit_id = (
            resolved_ref.stable_submit_id
            if resolved_ref is not None
            else build_stable_submit_id(run.execution_id, run.command_id)
        )

        # 第一步：从 UNKNOWN 进入 RECONCILING (RECONCILIATION_PENDING)
        if run.status == "UNKNOWN":
            ExecutionStateMachine.transition(
                run,
                "RECONCILING",
                reason_code="RECONCILIATION_STARTED",
                actor_id=actor_id,
                confirmation_state=ConfirmationState.RECONCILIATION_PENDING,
                trace_id=trace_id,
                detail={"stable_submit_id": stable_submit_id},
            )

        # 第二步：向 SeaTunnel Zeta 查询作业列表或已知 external_job_id
        matched_job: dict[str, Any] | None = None
        known_ext_id = run.external_job_id or (
            resolved_ref.external_job_id if resolved_ref else None
        )

        if known_ext_id:
            try:
                status_info = self._adapter.get_job_status(known_ext_id)
                matched_job = {
                    "jobId": known_ext_id,
                    "jobName": status_info.get("job_name") or stable_submit_id,
                    "jobStatus": status_info.get("job_status", "UNKNOWN"),
                }
            except Exception:
                matched_job = None

        if matched_job is None:
            cluster_jobs = self._adapter.list_cluster_jobs()
            for item in cluster_jobs:
                item_name = str(item.get("jobName") or item.get("job_name") or "")
                item_id = str(item.get("jobId") or item.get("job_id") or "")
                if item_name == stable_submit_id or (known_ext_id and item_id == known_ext_id):
                    matched_job = item
                    break

        now_iso = datetime.now(timezone.utc).isoformat()
        if matched_job is None:
            # 集群中未找到匹配作业：转为 FAILED（确认未在集群创建），仍绝不自动盲目重提
            if resolved_ref is not None:
                resolved_ref.submit_state = "NOT_FOUND_ON_ENGINE"
                resolved_ref.last_reconciled_at = now_iso
            ExecutionStateMachine.transition(
                run,
                "FAILED",
                reason_code="RECONCILIATION_JOB_NOT_FOUND",
                actor_id=actor_id,
                confirmation_state=ConfirmationState.ENGINE_CONFIRMED,
                trace_id=trace_id,
                detail={"stable_submit_id": stable_submit_id},
            )
            return ReconciliationOutcome(
                execution_id=run.execution_id,
                stable_submit_id=stable_submit_id,
                found_on_engine=False,
                external_job_id=None,
                resolved_status=run.status,
                confirmation_state=run.confirmation_state,
                engine_raw_status=None,
            )

        ext_job_id = str(matched_job.get("jobId") or matched_job.get("job_id"))
        raw_zeta_status = str(matched_job.get("jobStatus") or matched_job.get("job_status") or "RUNNING")
        mapped_status = self._map_zeta_status_to_run_status(run.execution_mode, raw_zeta_status)

        if resolved_ref is not None:
            resolved_ref.external_job_id = ext_job_id
            resolved_ref.submit_state = "RECONCILED_CONFIRMED"
            resolved_ref.last_reconciled_at = now_iso
            resolved_ref.raw_status_json = dict(matched_job)

        target_confirmation = (
            ConfirmationState.RECONCILIATION_PENDING
            if mapped_status == "UNKNOWN"
            else ConfirmationState.ENGINE_CONFIRMED
        )
        ExecutionStateMachine.transition(
            run,
            mapped_status,
            reason_code=f"RECONCILED_FROM_ZETA_{raw_zeta_status.upper()}",
            actor_id=actor_id,
            confirmation_state=target_confirmation,
            external_job_id=ext_job_id,
            trace_id=trace_id,
            detail={
                "stable_submit_id": stable_submit_id,
                "external_job_id": ext_job_id,
                "zeta_status": raw_zeta_status,
            },
        )
        return ReconciliationOutcome(
            execution_id=run.execution_id,
            stable_submit_id=stable_submit_id,
            found_on_engine=True,
            external_job_id=ext_job_id,
            resolved_status=run.status,
            confirmation_state=run.confirmation_state,
            engine_raw_status=raw_zeta_status,
        )
