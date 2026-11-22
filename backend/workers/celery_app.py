"""Celery 应用实例、数据面受管命令消费者任务与 Celery Beat 定时任务注册表。

严格遵循 ADR-002、ADR-003 与 `contracts/harness/outbox_events.schema.json`：
1. 注册全部 7 类 Outbox 命令对应的 Celery 任务（`execute_batch_command`、`execute_cdc_command`、
   `cancel_execution_command`、`publish_swap_command`、`rollback_shadow_cleanup`、
   `rollback_swap_restore`、`rollback_cdc_recover`）。
2. Worker 绝不信任消息体内的任何租户标识，强制通过 `SqlAlchemyControlPlaneRepository`
   调用 `resolve_execution_tenant_context(execution_id, command_id)` 派生租户上下文并进入事务级 RLS，
   再委托 `DataPlaneWorkerDispatcher`、`SeaTunnelZetaAdapter`、`DorisPublishAdapter` 与 `RollbackExecutor` 执行。
3. 注册 `RunReconciler` 未知状态对账任务、`OutboxDispatcher` 待投递事件轮询任务与 `ScheduleService` Beat 定时触发任务。
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from celery import Celery
from sqlalchemy import select
from sqlalchemy.orm import Session

# 确保仓库根目录与 backend 目录均在 Python 模块搜索路径中
_BACKEND_DIR = Path(__file__).resolve().parents[1]
_REPO_ROOT = _BACKEND_DIR.parent
for _p in (str(_REPO_ROOT), str(_BACKEND_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app.api.wiring import (  # noqa: E402
    SqlAlchemyControlPlaneRepository,
    _LocalMockZetaTransport,
)
from app.config import get_settings  # noqa: E402
from app.data_plane.reconciler import RunReconciler  # noqa: E402
from app.data_plane.seatunnel_adapter import (  # noqa: E402
    ExternalJobRefRecord,
    SeaTunnelZetaAdapter,
    build_stable_submit_id,
)
from app.data_plane.worker_tasks import DataPlaneWorkerDispatcher  # noqa: E402
from app.execution.state_machine import ExecutionMode, ExecutionRunRecord  # noqa: E402
from app.publishing.doris_adapter import DorisPublishAdapter  # noqa: E402
from app.rollback.executor import RollbackExecutor  # noqa: E402
from app.scheduling.service import ScheduleService  # noqa: E402
from app.shared.db_rls import (  # noqa: E402
    TenantContext,
    apply_rls_context,
    get_runtime_session_factory,
)
from app.shared.models.execution_quality import ExecutionRun, Schedule  # noqa: E402

settings = get_settings()

celery_app = Celery(
    "etl_agent_workers",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_default_queue="etl_agent.commands",
    beat_schedule={
        "outbox-dispatch-every-second": {
            "task": "etl_agent.outbox.dispatch_pending_events",
            "schedule": 1.0,
        },
        "evaluate-due-schedules-every-30s": {
            "task": "etl_agent.scheduling.evaluate_due_schedules",
            "schedule": 30.0,
        },
        "reconcile-unknown-runs-every-30s": {
            "task": "etl_agent.data_plane.reconcile_unknown_run",
            "schedule": 30.0,
        },
    },
)

# 进程级共享数据面适配器单例（保证同一 Worker 进程内的命令幂等缓存与对账作业引用连续）
_default_zeta_transport = _LocalMockZetaTransport()
_default_seatunnel_adapter = SeaTunnelZetaAdapter(transport=_default_zeta_transport)
_default_doris_adapter = DorisPublishAdapter()
_default_rollback_executor = RollbackExecutor(doris_adapter=_default_doris_adapter)
_default_reconciler = RunReconciler(adapter=_default_seatunnel_adapter)


def build_worker_dispatcher(
    session: Session,
    *,
    seatunnel_adapter: SeaTunnelZetaAdapter | None = None,
    doris_publish_adapter: DorisPublishAdapter | None = None,
    rollback_executor: RollbackExecutor | None = None,
) -> DataPlaneWorkerDispatcher:
    """构建绑定当前数据库事务 Session 的 `DataPlaneWorkerDispatcher`。"""
    repo = SqlAlchemyControlPlaneRepository(session)
    st_adapter = seatunnel_adapter or _default_seatunnel_adapter
    doris_adapter = doris_publish_adapter or _default_doris_adapter
    rb_executor = rollback_executor or _default_rollback_executor
    return DataPlaneWorkerDispatcher(
        repository=repo,
        seatunnel_adapter=st_adapter,
        doris_publish_adapter=doris_adapter,
        rollback_executor=rb_executor,
    )


def _execute_outbox_command_with_session(
    payload: dict[str, Any],
    session: Session | None = None,
) -> dict[str, Any]:
    """在受控数据库事务中执行 Outbox 命令分发。"""
    if session is not None:
        dispatcher = build_worker_dispatcher(session)
        return dispatcher.handle_outbox_command(payload)

    factory = get_runtime_session_factory()
    db = factory()
    try:
        dispatcher = build_worker_dispatcher(db)
        outcome = dispatcher.handle_outbox_command(payload)
        db.commit()
        return outcome
    except Exception:
        if db.in_transaction():
            db.rollback()
        raise
    finally:
        db.close()


@celery_app.task(name="etl_agent.data_plane.execute_batch_command")
def execute_batch_command(
    payload: dict[str, Any],
    session: Session | None = None,
) -> dict[str, Any]:
    """消费 `EXECUTION_SUBMIT_BATCH` Outbox 事件并调用 `SeaTunnelZetaAdapter` 提交批处理作业。"""
    return _execute_outbox_command_with_session(payload, session=session)


@celery_app.task(name="etl_agent.data_plane.execute_cdc_command")
def execute_cdc_command(
    payload: dict[str, Any],
    session: Session | None = None,
) -> dict[str, Any]:
    """消费 `EXECUTION_SUBMIT_CDC` Outbox 事件并调用 `SeaTunnelZetaAdapter` 提交 CDC 流作业。"""
    return _execute_outbox_command_with_session(payload, session=session)


@celery_app.task(name="etl_agent.data_plane.cancel_execution_command")
def cancel_execution_command(
    payload: dict[str, Any],
    session: Session | None = None,
) -> dict[str, Any]:
    """消费 `EXECUTION_CANCEL` Outbox 事件并调用 `SeaTunnelZetaAdapter.cancel_job` 受管停止作业。"""
    return _execute_outbox_command_with_session(payload, session=session)


@celery_app.task(name="etl_agent.data_plane.publish_swap_command")
def publish_swap_command(
    payload: dict[str, Any],
    session: Session | None = None,
) -> dict[str, Any]:
    """消费 `EXECUTION_PUBLISH_SWAP` Outbox 事件并调用 `DorisPublishAdapter` 执行 Schema 门禁与原子表替换。"""
    return _execute_outbox_command_with_session(payload, session=session)


@celery_app.task(name="etl_agent.data_plane.rollback_shadow_cleanup")
def rollback_shadow_cleanup(
    payload: dict[str, Any],
    session: Session | None = None,
) -> dict[str, Any]:
    """消费 `ROLLBACK_SHADOW_CLEANUP` Outbox 事件并调用 `RollbackExecutor.execute_shadow_cleanup`。"""
    return _execute_outbox_command_with_session(payload, session=session)


@celery_app.task(name="etl_agent.data_plane.rollback_swap_restore")
def rollback_swap_restore(
    payload: dict[str, Any],
    session: Session | None = None,
) -> dict[str, Any]:
    """消费 `ROLLBACK_SWAP_RESTORE` Outbox 事件并调用 `RollbackExecutor.execute_swap_restore`。"""
    return _execute_outbox_command_with_session(payload, session=session)


@celery_app.task(name="etl_agent.data_plane.rollback_cdc_recover")
def rollback_cdc_recover(
    payload: dict[str, Any],
    session: Session | None = None,
) -> dict[str, Any]:
    """消费 `ROLLBACK_CDC_RECOVER` Outbox 事件并调用 `RollbackExecutor.execute_cdc_recover`（C+D 恢复策略）。"""
    return _execute_outbox_command_with_session(payload, session=session)


@celery_app.task(name="etl_agent.data_plane.reconcile_unknown_run")
def reconcile_unknown_run(
    execution_id: str | None = None,
    session: Session | None = None,
) -> dict[str, Any]:
    """调用 `RunReconciler` 对处于 `UNKNOWN` 或 `RECONCILING` 状态的执行进行受管对账（禁止盲目重提）。"""
    own_session = False
    if session is None:
        factory = get_runtime_session_factory()
        session = factory()
        own_session = True

    try:
        stmt = select(ExecutionRun).where(ExecutionRun.status.in_(("UNKNOWN", "RECONCILING")))
        if execution_id is not None:
            stmt = stmt.where(ExecutionRun.id == uuid.UUID(str(execution_id)))
        runs = session.execute(stmt).scalars().all()

        reconciled: list[dict[str, Any]] = []
        for run_row in runs:
            apply_rls_context(
                session,
                TenantContext(
                    organization_id=run_row.organization_id,
                    project_id=run_row.project_id,
                    actor_id="system:reconciler",
                ),
                require_project=True,
            )
            mode = (
                ExecutionMode.CDC
                if run_row.execution_mode.upper() == "CDC"
                else ExecutionMode.BATCH
            )
            run_rec = ExecutionRunRecord(
                execution_id=str(run_row.id),
                organization_id=str(run_row.organization_id),
                project_id=str(run_row.project_id),
                pipeline_id=str(run_row.pipeline_id),
                pipeline_version_id=str(run_row.pipeline_version_id),
                artifact_hash=run_row.artifact_hash,
                execution_mode=mode,
                preparation_id=str(run_row.preparation_id),
                capability_jti=str(run_row.capability_jti),
                command_id=str(run_row.command_id),
                status="UNKNOWN" if run_row.status == "UNKNOWN" else "RECONCILING",
                external_job_id=run_row.external_job_id,
            )
            stable_id = build_stable_submit_id(str(run_row.id), str(run_row.command_id))
            ref_rec = ExternalJobRefRecord(
                id=str(uuid.uuid4()),
                organization_id=str(run_row.organization_id),
                project_id=str(run_row.project_id),
                execution_id=str(run_row.id),
                stable_submit_id=stable_id,
                external_job_id=run_row.external_job_id,
            )
            outcome = _default_reconciler.reconcile(run_rec, job_ref=ref_rec)
            run_row.status = outcome.resolved_status
            run_row.external_job_id = outcome.external_job_id
            reconciled.append(
                {
                    "execution_id": outcome.execution_id,
                    "resolved_status": outcome.resolved_status,
                    "external_job_id": outcome.external_job_id,
                    "confirmation_state": outcome.confirmation_state.value,
                }
            )
        session.flush()
        if own_session:
            session.commit()
        return {"reconciled_count": len(reconciled), "items": reconciled}
    finally:
        if own_session:
            session.close()


@celery_app.task(name="etl_agent.outbox.dispatch_pending_events")
def dispatch_pending_events(
    batch_size: int | None = None,
    session: Session | None = None,
) -> dict[str, Any]:
    """调用 `OutboxDispatcher.dispatch_once` 领取并投递待发送的事务性 Outbox 事件。"""
    from workers.outbox_dispatcher import (
        OutboxDispatcher,
        create_outbox_dispatcher_session_factory,
    )

    dispatcher = OutboxDispatcher()
    if session is not None:
        return dispatcher.dispatch_once(session, batch_size=batch_size)

    factory = create_outbox_dispatcher_session_factory()
    db = factory()
    try:
        stats = dispatcher.dispatch_once(db, batch_size=batch_size)
        db.commit()
        return stats
    except Exception:
        if db.in_transaction():
            db.rollback()
        raise
    finally:
        db.close()


@celery_app.task(name="etl_agent.scheduling.evaluate_due_schedules")
def evaluate_due_schedules(
    session: Session | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Celery Beat 定时触发任务：扫描到期 `Schedule` 并以 `system:scheduler` 身份调用 `ScheduleService`。"""
    own_session = False
    if session is None:
        factory = get_runtime_session_factory()
        session = factory()
        own_session = True

    current_time = now or datetime.now(timezone.utc)
    schedule_service = ScheduleService()

    try:
        due_schedules = (
            session.execute(
                select(Schedule).where(
                    Schedule.enabled.is_(True),
                    Schedule.next_run_at.is_not(None),
                    Schedule.next_run_at <= current_time,
                )
            )
            .scalars()
            .all()
        )
        results: list[dict[str, Any]] = []
        for sched in due_schedules:
            apply_rls_context(
                session,
                TenantContext(
                    organization_id=sched.organization_id,
                    project_id=sched.project_id,
                    actor_id="system:scheduler",
                ),
                require_project=True,
            )
            res = schedule_service.evaluate_and_trigger_schedule(
                session,
                organization_id=sched.organization_id,
                project_id=sched.project_id,
                schedule_id=sched.id,
                evaluation_time=current_time,
            )
            results.append(res)
        session.flush()
        if own_session:
            session.commit()
        return {"evaluated": len(results), "results": results}
    finally:
        if own_session:
            session.close()
