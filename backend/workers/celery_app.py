"""Celery 应用实例与队列配置。

负责连接 Redis Broker / Result Backend，并定义 Outbox 事件对应的异步 Worker 任务路由。
注意：Celery Worker 收到消息后绝不信任消息体内的 tenant_id，必须先通过
`resolve_execution_tenant_context(execution_id, command_id)` 派生租户上下文并进入事务级 RLS。
"""
from __future__ import annotations

from celery import Celery

from app.config import get_settings

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
)
