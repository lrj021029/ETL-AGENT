"""基于 `SELECT ... FOR UPDATE SKIP LOCKED` 与租约（Lease）机制的事务性 Outbox 投递器。

严格遵循 ADR-002、ADR-003 与 contracts/harness/outbox_events.schema.json：
1. 使用最小权限数据库角色 `etl_outbox_dispatcher`（仅拥有 `outbox_events` 与 `outbox_deliveries` 的读写权限，无任何其他业务表权限）。
2. 通过 `SELECT ... FOR UPDATE SKIP LOCKED` 并发安全地领取待投递或租约已过期的记录，写入 `lease_owner` 与 `lease_until`。
3. 校验事件负载符合 `OutboxEventPayload` 契约（绝不向消息附加未验证的明文 Secret）。
4. 投递至 Celery（At-least-once 语义）；成功后标记 `status = 'DELIVERED'`，失败时递增重试并在耗尽 `max_attempts` 后标记 `FAILED`。
"""
from __future__ import annotations

import socket
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import create_engine, or_, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings, get_settings
from app.shared.errors import redact_sensitive_value
from app.shared.models.governance_harness import OutboxDelivery, OutboxEvent
from workers.celery_app import celery_app

# Outbox 事件类型到 Celery 任务名的标准路由映射（对齐 contracts/harness/outbox_events.schema.json）
EVENT_TYPE_TO_TASK_NAME: dict[str, str] = {
    "EXECUTION_SUBMIT_BATCH": "etl_agent.data_plane.execute_batch_command",
    "EXECUTION_SUBMIT_CDC": "etl_agent.data_plane.execute_cdc_command",
    "EXECUTION_CANCEL": "etl_agent.data_plane.cancel_execution_command",
    "EXECUTION_PUBLISH_SWAP": "etl_agent.data_plane.publish_swap_command",
    "ROLLBACK_SHADOW_CLEANUP": "etl_agent.data_plane.rollback_shadow_cleanup",
    "ROLLBACK_SWAP_RESTORE": "etl_agent.data_plane.rollback_swap_restore",
    "ROLLBACK_CDC_RECOVER": "etl_agent.data_plane.rollback_cdc_recover",
}

REQUIRED_OUTBOX_PAYLOAD_FIELDS: tuple[str, ...] = (
    "schema_version",
    "event_type",
    "command_id",
    "execution_id",
    "capability_jti",
    "capability_jws",
    "trace_id",
    "created_at",
)


def validate_outbox_payload(payload: dict[str, Any]) -> None:
    """校验待投递的 Outbox 事件负载符合 contracts/harness/outbox_events.schema.json。"""
    missing = [f for f in REQUIRED_OUTBOX_PAYLOAD_FIELDS if f not in payload or not payload[f]]
    if missing:
        raise ValueError(f"Outbox 事件负载缺失契约必填字段：{missing}")
    if payload.get("schema_version") != "1.0.0":
        raise ValueError(f"不支持的 Outbox schema_version：{payload.get('schema_version')}")
    if payload.get("event_type") not in EVENT_TYPE_TO_TASK_NAME:
        raise ValueError(f"未知的 Outbox event_type：{payload.get('event_type')}")


class OutboxDispatcher:
    """基于 PostgreSQL 行锁租约（FOR UPDATE SKIP LOCKED）的可靠 Outbox 投递器。"""

    def __init__(
        self,
        *,
        task_sender: Callable[[str, dict[str, Any]], Any] | None = None,
        dispatcher_id: str | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.dispatcher_id = dispatcher_id or f"dispatcher-{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
        self._task_sender = task_sender or self._default_celery_sender

    @staticmethod
    def _default_celery_sender(task_name: str, payload: dict[str, Any]) -> Any:
        """默认使用 Celery send_task 投递命令消息。"""
        return celery_app.send_task(task_name, kwargs={"payload": payload})

    def claim_batch(
        self,
        session: Session,
        *,
        batch_size: int | None = None,
        lease_seconds: int | None = None,
        now: datetime | None = None,
    ) -> list[tuple[OutboxDelivery, OutboxEvent]]:
        """使用 `SELECT ... FOR UPDATE SKIP LOCKED` 领取一批待投递或租约已过期的 Outbox 记录。"""
        current_time = now or datetime.now(timezone.utc)
        limit_n = batch_size or self.settings.outbox_batch_size
        lease_dur = timedelta(seconds=lease_seconds or self.settings.outbox_lease_seconds)

        stmt = (
            select(OutboxDelivery, OutboxEvent)
            .join(OutboxEvent, OutboxEvent.id == OutboxDelivery.outbox_event_id)
            .where(
                OutboxDelivery.attempts < OutboxDelivery.max_attempts,
                or_(
                    OutboxDelivery.status == "PENDING",
                    (OutboxDelivery.status == "LEASED") & (OutboxDelivery.lease_until <= current_time),
                ),
            )
            .order_by(OutboxEvent.created_at.asc())
            .limit(limit_n)
            .with_for_update(skip_locked=True)
        )

        claimed_rows = session.execute(stmt).all()
        result: list[tuple[OutboxDelivery, OutboxEvent]] = []
        for delivery, event in claimed_rows:
            delivery.status = "LEASED"
            delivery.lease_owner = self.dispatcher_id
            delivery.lease_until = current_time + lease_dur
            delivery.attempts += 1
            delivery.updated_at = current_time
            result.append((delivery, event))

        session.flush()
        return result

    def dispatch_once(
        self,
        session: Session,
        *,
        batch_size: int | None = None,
        lease_seconds: int | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """执行单轮领取与投递循环，返回投递统计结果。"""
        current_time = now or datetime.now(timezone.utc)
        claimed = self.claim_batch(
            session,
            batch_size=batch_size,
            lease_seconds=lease_seconds,
            now=current_time,
        )

        delivered_count = 0
        failed_count = 0
        retried_count = 0

        for delivery, event in claimed:
            payload = dict(event.payload_ref_json or {})
            try:
                validate_outbox_payload(payload)
                task_name = EVENT_TYPE_TO_TASK_NAME[event.event_type]
                self._task_sender(task_name, payload)

                delivery.status = "DELIVERED"
                delivery.delivered_at = current_time
                delivery.lease_until = None
                delivery.last_error = None
                delivery.updated_at = current_time
                delivered_count += 1
            except Exception as exc:
                safe_err = str(redact_sensitive_value(str(exc)))
                delivery.last_error = safe_err
                delivery.lease_owner = None
                delivery.lease_until = None
                delivery.updated_at = current_time
                if delivery.attempts >= delivery.max_attempts:
                    delivery.status = "FAILED"
                    failed_count += 1
                else:
                    delivery.status = "PENDING"
                    retried_count += 1

        session.flush()
        return {
            "dispatcher_id": self.dispatcher_id,
            "claimed": len(claimed),
            "delivered": delivered_count,
            "retried": retried_count,
            "failed": failed_count,
        }


def create_outbox_dispatcher_session_factory(settings: Settings | None = None) -> sessionmaker[Session]:
    """使用最小权限角色 `etl_outbox_dispatcher` 创建独立的数据库 Session 工厂（遵循 ADR-002）。"""
    cfg = settings or get_settings()
    engine = create_engine(cfg.outbox_database_url, pool_pre_ping=True, future=True)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
