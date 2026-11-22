"""受管 Cron 定时调度、时区/DST 计算与并发/错过策略执行服务。

严格遵循 TECHNICAL_SPEC §12、DEC-004 与 ADR-003 §4：
1. 创建或修改 Schedule 必须针对固定 `pipeline_version_id` 绑定已审批通过的 `StandingSchedulePreparation`
   （`preparation_type == 'STANDING_SCHEDULE'` 且 `status == 'APPROVED'`）。
2. 严禁绕过 Prepare / Approve 直接创建未受审的定时任务。
3. 支持标准 5 段 Cron 表达式解析、IANA 时区与夏令时（DST）下一触发时间计算。
4. 显式参数化 `concurrency_policy`（默认推荐 `FORBID`）、`misfire_policy`（默认推荐 `SKIP`）与
   `version_binding_mode`（默认推荐 `PINNED`），并在触发时强制执行并发与错过策略。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytz
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.shared.errors import AppError, ErrorCode
from app.shared.interfaces import ToolBrokerProtocol
from app.shared.models.execution_quality import ExecutionRun, Schedule, ScheduleTrigger
from app.shared.models.governance_harness import Preparation

VALID_CONCURRENCY_POLICIES = frozenset({"FORBID", "ALLOW", "REPLACE"})
VALID_MISFIRE_POLICIES = frozenset({"SKIP", "FIRE_ONCE", "CATCH_UP"})
VALID_VERSION_BINDING_MODES = frozenset({"PINNED", "LATEST_APPROVED"})

# 活跃运行中的状态集合（用于 FORBID / REPLACE 并发策略判断）
ACTIVE_EXECUTION_STATUSES = frozenset(
    {
        "CREATED",
        "QUEUED",
        "SUBMITTING",
        "RUNNING",
        "VALIDATING",
        "PUBLISHING",
        "INITIAL_SNAPSHOT",
        "STREAMING",
    }
)


def validate_timezone_name(tz_name: str) -> tzinfo:
    """校验并返回时区对象（兼容 zoneinfo 与 pytz 以支持 Windows 原生环境）。"""
    clean_tz = (tz_name or "").strip()
    if not clean_tz:
        raise AppError(ErrorCode.DSL_SCHEMA_INVALID, "timezone 时区名称不能为空")
    try:
        return ZoneInfo(clean_tz)
    except ZoneInfoNotFoundError:
        try:
            return pytz.timezone(clean_tz)
        except pytz.UnknownTimeZoneError as exc:
            raise AppError(
                ErrorCode.DSL_SCHEMA_INVALID,
                f"无效或未识别的 IANA 时区名称：'{clean_tz}'",
                detail={"timezone": clean_tz},
            ) from exc


def _parse_cron_field(field_str: str, min_val: int, max_val: int, field_name: str) -> set[int]:
    """解析单个 Cron 字段（支持 *, */n, a-b, a-b/n, 逗号列表）。"""
    values: set[int] = set()
    for part in field_str.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"空字段分段 ({field_name})")

        step = 1
        if "/" in part:
            base, step_str = part.split("/", 1)
            if not step_str.isdigit() or int(step_str) <= 0:
                raise ValueError(f"非法步长 '{part}' ({field_name})")
            step = int(step_str)
        else:
            base = part

        if base == "*":
            start, end = min_val, max_val
        elif "-" in base:
            s_str, e_str = base.split("-", 1)
            start, end = int(s_str), int(e_str)
        else:
            val = int(base)
            if field_name == "day_of_week" and val == 7:
                val = 0
            if not (min_val <= val <= max_val):
                raise ValueError(f"数值 {val} 超出范围 [{min_val}, {max_val}] ({field_name})")
            values.add(val)
            continue

        if field_name == "day_of_week" and end == 7:
            end = 6
        if start > end or start < min_val or end > max_val:
            raise ValueError(f"范围 {start}-{end} 超出 [{min_val}, {max_val}] ({field_name})")
        for v in range(start, end + 1, step):
            values.add(v)

    if not values:
        raise ValueError(f"字段 '{field_str}' 未匹配任何有效时间值 ({field_name})")
    return values


def validate_cron_expression(cron_expr: str) -> dict[str, set[int]]:
    """严格校验标准 5 段 Cron 表达式（分 时 日 月 周）。"""
    parts = (cron_expr or "").strip().split()
    if len(parts) != 5:
        raise AppError(
            ErrorCode.DSL_SCHEMA_INVALID,
            f"Cron 表达式必须为标准 5 段格式（分 时 日 月 周），当前收到 {len(parts)} 段：'{cron_expr}'",
            detail={"cron_expression": cron_expr},
        )
    try:
        return {
            "minute": _parse_cron_field(parts[0], 0, 59, "minute"),
            "hour": _parse_cron_field(parts[1], 0, 23, "hour"),
            "day": _parse_cron_field(parts[2], 1, 31, "day_of_month"),
            "month": _parse_cron_field(parts[3], 1, 12, "month"),
            "weekday": _parse_cron_field(parts[4], 0, 6, "day_of_week"),
        }
    except ValueError as exc:
        raise AppError(
            ErrorCode.DSL_SCHEMA_INVALID,
            f"非法 Cron 表达式 '{cron_expr}'：{exc}",
            detail={"cron_expression": cron_expr},
        ) from exc


def compute_next_cron_run(
    cron_expr: str,
    tz_name: str = "UTC",
    *,
    base_time: datetime | None = None,
) -> datetime:
    """结合目标时区与夏令时（DST）规则计算下一次 Cron 触发的 UTC 时间。"""
    parsed = validate_cron_expression(cron_expr)
    tz = validate_timezone_name(tz_name)

    now_utc = base_time or datetime.now(timezone.utc)
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    else:
        now_utc = now_utc.astimezone(timezone.utc)

    # 从下一整分钟开始向前推进（避免同一分钟重复触发，且天然跳过 DST 春季跳变不存在的本地时间）
    cursor_utc = (now_utc + timedelta(minutes=1)).replace(second=0, microsecond=0)
    max_iterations = 366 * 24 * 60  # 最多搜索 1 年

    for _ in range(max_iterations):
        local_dt = cursor_utc.astimezone(tz)
        # Python weekday(): 周一=0..周日=6 -> 转换为 Cron 周日=0, 周一=1..周六=6
        cron_weekday = (local_dt.weekday() + 1) % 7
        if (
            local_dt.month in parsed["month"]
            and local_dt.day in parsed["day"]
            and cron_weekday in parsed["weekday"]
            and local_dt.hour in parsed["hour"]
            and local_dt.minute in parsed["minute"]
        ):
            return cursor_utc
        cursor_utc += timedelta(minutes=1)

    raise AppError(
        ErrorCode.DSL_SCHEMA_INVALID,
        f"无法在未来 366 天内为 Cron 表达式 '{cron_expr}' 计算有效触发时间",
    )


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class ScheduleService:
    """受管定时调度生命周期与 Celery Beat 触发协同服务。"""

    def __init__(
        self,
        tool_broker: ToolBrokerProtocol | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.tool_broker = tool_broker

    def create_schedule(
        self,
        session: Session,
        *,
        organization_id: uuid.UUID,
        project_id: uuid.UUID,
        pipeline_id: uuid.UUID,
        pipeline_version_id: uuid.UUID,
        standing_preparation_id: uuid.UUID,
        cron_expression: str,
        timezone_name: str = "UTC",
        enabled: bool = True,
        concurrency_policy: str | None = None,
        misfire_policy: str | None = None,
        version_binding_mode: str | None = None,
        actor_user_id: uuid.UUID,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """创建绑定至已审批 `StandingSchedulePreparation` 的受管定时调度。"""
        # 1. 校验 Cron 与时区
        next_run = compute_next_cron_run(cron_expression, timezone_name, base_time=now)

        # 2. 校验策略枚举
        conc_policy = (concurrency_policy or self.settings.schedule_default_concurrency_policy).strip().upper()
        if conc_policy not in VALID_CONCURRENCY_POLICIES:
            raise AppError(
                ErrorCode.DSL_SCHEMA_INVALID,
                f"无效的 concurrency_policy '{conc_policy}'，可选值为 {sorted(VALID_CONCURRENCY_POLICIES)}",
            )

        mis_policy = (misfire_policy or self.settings.schedule_default_misfire_policy).strip().upper()
        if mis_policy not in VALID_MISFIRE_POLICIES:
            raise AppError(
                ErrorCode.DSL_SCHEMA_INVALID,
                f"无效的 misfire_policy '{mis_policy}'，可选值为 {sorted(VALID_MISFIRE_POLICIES)}",
            )

        bind_mode = (version_binding_mode or self.settings.schedule_default_version_binding).strip().upper()
        if bind_mode not in VALID_VERSION_BINDING_MODES:
            raise AppError(
                ErrorCode.DSL_SCHEMA_INVALID,
                f"无效的 version_binding_mode '{bind_mode}'，可选值为 {sorted(VALID_VERSION_BINDING_MODES)}",
            )

        # 3. 强制核验 StandingSchedulePreparation 状态、类型与版本绑定（遵循 ADR-003 §4）
        prep = session.get(Preparation, standing_preparation_id)
        if prep is None or prep.organization_id != organization_id or prep.project_id != project_id:
            raise AppError(
                ErrorCode.PREPARATION_INVALIDATED,
                "绑定的 StandingSchedulePreparation 不存在或不属于当前项目上下文",
            )
        if prep.preparation_type != "STANDING_SCHEDULE":
            raise AppError(
                ErrorCode.PREPARATION_INVALIDATED,
                f"Schedule 只能绑定 preparation_type='STANDING_SCHEDULE' 的准备单，当前为 '{prep.preparation_type}'",
            )
        if prep.status != "APPROVED":
            raise AppError(
                ErrorCode.APPROVAL_INCOMPLETE,
                f"StandingSchedulePreparation 尚未完成审批（当前状态：{prep.status}），禁止创建或启用定时调度",
            )
        current_time = now or datetime.now(timezone.utc)
        if _ensure_utc(prep.expires_at) <= _ensure_utc(current_time):
            raise AppError(
                ErrorCode.PREPARATION_EXPIRED,
                "绑定的 StandingSchedulePreparation 已过期，请重新发起 Prepare 与审批",
            )
        if prep.pipeline_id != pipeline_id or prep.pipeline_version_id != pipeline_version_id:
            raise AppError(
                ErrorCode.PREPARATION_INVALIDATED,
                "Schedule 的 pipeline_id / pipeline_version_id 与已审批的 StandingSchedulePreparation 不一致",
            )

        schedule = Schedule(
            organization_id=organization_id,
            project_id=project_id,
            pipeline_id=pipeline_id,
            pipeline_version_id=pipeline_version_id,
            standing_preparation_id=standing_preparation_id,
            cron_expression=cron_expression.strip(),
            timezone=timezone_name.strip(),
            enabled=enabled,
            concurrency_policy=conc_policy,
            misfire_policy=mis_policy,
            version_binding_mode=bind_mode,
            next_run_at=next_run if enabled else None,
            last_triggered_at=None,
            created_by=actor_user_id,
        )
        session.add(schedule)
        session.flush()
        return self._serialize_schedule(schedule)

    def list_schedules(
        self,
        session: Session,
        *,
        organization_id: uuid.UUID,
        project_id: uuid.UUID,
    ) -> list[dict[str, Any]]:
        """列出项目内的所有受管定时调度。"""
        rows = (
            session.execute(
                select(Schedule)
                .where(
                    Schedule.organization_id == organization_id,
                    Schedule.project_id == project_id,
                )
                .order_by(Schedule.created_at.desc())
            )
            .scalars()
            .all()
        )
        return [self._serialize_schedule(s) for s in rows]

    def toggle_schedule(
        self,
        session: Session,
        *,
        organization_id: uuid.UUID,
        project_id: uuid.UUID,
        schedule_id: uuid.UUID,
        enabled: bool,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """启用或暂停定时调度（重新启用时重验 StandingSchedulePreparation 有效性并重算 next_run_at）。"""
        schedule = session.get(Schedule, schedule_id)
        if schedule is None or schedule.organization_id != organization_id or schedule.project_id != project_id:
            raise AppError(ErrorCode.TENANT_ACCESS_DENIED, "目标 Schedule 不存在或跨租户访问被拒绝")

        if enabled:
            prep = session.get(Preparation, schedule.standing_preparation_id)
            if prep is None or prep.status != "APPROVED":
                raise AppError(
                    ErrorCode.PREPARATION_INVALIDATED,
                    "关联的 StandingSchedulePreparation 已失效或未处于 APPROVED 状态，无法启用 Schedule",
                )
            schedule.next_run_at = compute_next_cron_run(
                schedule.cron_expression,
                schedule.timezone,
                base_time=now,
            )
        else:
            schedule.next_run_at = None

        schedule.enabled = enabled
        session.flush()
        return self._serialize_schedule(schedule)

    def evaluate_and_trigger_schedule(
        self,
        session: Session,
        *,
        organization_id: uuid.UUID,
        project_id: uuid.UUID,
        schedule_id: uuid.UUID,
        evaluation_time: datetime | None = None,
        misfire_grace_seconds: int = 300,
    ) -> dict[str, Any]:
        """由 Celery Beat 到点调用的受管触发评估器（以 `system:scheduler` 身份执行）。

        1. 重验 Schedule 是否启用及 `StandingSchedulePreparation` 是否仍处于 `APPROVED` 且未过期。
        2. 执行错过策略（`misfire_policy`）：若当前时间超过 `scheduled_for + misfire_grace_seconds`
           且策略为 `SKIP`，则记录 `SKIPPED_MISFIRE` 并推进 `next_run_at`。
        3. 执行并发策略（`concurrency_policy`）：
           - `FORBID`：若同一 Pipeline 存在未结束的活跃 `ExecutionRun`，记录 `SKIPPED_CONCURRENCY` 并跳过本次。
           - `REPLACE`：标记旧活跃运行进入 `CANCEL_REQUESTED`，再触发新运行。
           - `ALLOW`：允许并行触发。
        4. 调用 `ToolBrokerProtocol.trigger_standing_schedule`（若未注入则记录受管触发意图）
           签发当次 5 分钟单次 Ed25519 Capability 并推进 `next_run_at`。
        """
        schedule = session.get(Schedule, schedule_id)
        if schedule is None or schedule.organization_id != organization_id or schedule.project_id != project_id:
            raise AppError(ErrorCode.TENANT_ACCESS_DENIED, "Schedule 不存在")
        if not schedule.enabled or schedule.next_run_at is None:
            raise AppError(ErrorCode.STATE_TRANSITION_INVALID, "Schedule 未处于启用状态")

        now = _ensure_utc(evaluation_time or datetime.now(timezone.utc))
        scheduled_for = _ensure_utc(schedule.next_run_at)

        prep = session.get(Preparation, schedule.standing_preparation_id)
        if prep is None or prep.status != "APPROVED":
            raise AppError(
                ErrorCode.PREPARATION_INVALIDATED,
                "StandingSchedulePreparation 已失效，拒绝触发定时任务",
            )
        if _ensure_utc(prep.expires_at) <= now:
            raise AppError(
                ErrorCode.PREPARATION_EXPIRED,
                "StandingSchedulePreparation 已过期，拒绝触发定时任务",
            )

        # 检查是否发生错过调度（Misfire）
        delay_seconds = (now - scheduled_for).total_seconds()
        if delay_seconds > misfire_grace_seconds and schedule.misfire_policy == "SKIP":
            trigger_row = ScheduleTrigger(
                organization_id=organization_id,
                project_id=project_id,
                schedule_id=schedule.id,
                scheduled_for=scheduled_for,
                triggered_at=now,
                status="SKIPPED",
                execution_id=None,
                skip_reason=f"MISFIRE_SKIP: delay={int(delay_seconds)}s > grace={misfire_grace_seconds}s",
            )
            session.add(trigger_row)
            schedule.next_run_at = compute_next_cron_run(
                schedule.cron_expression,
                schedule.timezone,
                base_time=now,
            )
            session.flush()
            return {
                "trigger_id": str(trigger_row.id),
                "status": "SKIPPED",
                "skip_reason": trigger_row.skip_reason,
                "next_run_at": schedule.next_run_at.isoformat(),
            }

        # 检查并发策略（Concurrency Policy）
        active_runs = (
            session.execute(
                select(ExecutionRun).where(
                    ExecutionRun.organization_id == organization_id,
                    ExecutionRun.project_id == project_id,
                    ExecutionRun.pipeline_id == schedule.pipeline_id,
                    ExecutionRun.status.in_(ACTIVE_EXECUTION_STATUSES),
                )
            )
            .scalars()
            .all()
        )
        if active_runs and schedule.concurrency_policy == "FORBID":
            trigger_row = ScheduleTrigger(
                organization_id=organization_id,
                project_id=project_id,
                schedule_id=schedule.id,
                scheduled_for=scheduled_for,
                triggered_at=now,
                status="SKIPPED",
                execution_id=None,
                skip_reason=f"CONCURRENCY_FORBID: active_execution_id={active_runs[0].id}",
            )
            session.add(trigger_row)
            schedule.next_run_at = compute_next_cron_run(
                schedule.cron_expression,
                schedule.timezone,
                base_time=now,
            )
            session.flush()
            return {
                "trigger_id": str(trigger_row.id),
                "status": "SKIPPED",
                "skip_reason": trigger_row.skip_reason,
                "next_run_at": schedule.next_run_at.isoformat(),
            }

        if active_runs and schedule.concurrency_policy == "REPLACE":
            for r in active_runs:
                r.status = "CANCEL_REQUESTED"
                r.status_reason = f"Replaced by schedule {schedule.id} under REPLACE policy"

        idempotency_key = f"sched-{schedule.id}-{int(scheduled_for.timestamp())}"
        execution_id: uuid.UUID | None = None

        if self.tool_broker is not None:
            broker_res = self.tool_broker.trigger_standing_schedule(
                organization_id=organization_id,
                project_id=project_id,
                schedule_id=schedule.id,
                standing_preparation_id=schedule.standing_preparation_id,
                idempotency_key=idempotency_key,
                trace_id=f"tr-sched-{uuid.uuid4().hex[:12]}",
            )
            if "execution_id" in broker_res and broker_res["execution_id"]:
                execution_id = uuid.UUID(str(broker_res["execution_id"]))

        trigger_row = ScheduleTrigger(
            organization_id=organization_id,
            project_id=project_id,
            schedule_id=schedule.id,
            scheduled_for=scheduled_for,
            triggered_at=now,
            status="TRIGGERED",
            execution_id=execution_id,
            skip_reason=None,
        )
        session.add(trigger_row)

        schedule.last_triggered_at = now
        if schedule.misfire_policy == "CATCH_UP" and delay_seconds > misfire_grace_seconds:
            schedule.next_run_at = compute_next_cron_run(
                schedule.cron_expression,
                schedule.timezone,
                base_time=scheduled_for,
            )
        else:
            schedule.next_run_at = compute_next_cron_run(
                schedule.cron_expression,
                schedule.timezone,
                base_time=now,
            )
        session.flush()

        return {
            "trigger_id": str(trigger_row.id),
            "status": "TRIGGERED",
            "principal": "system:scheduler",
            "execution_id": str(execution_id) if execution_id else None,
            "idempotency_key": idempotency_key,
            "next_run_at": schedule.next_run_at.isoformat(),
        }

    @staticmethod
    def _serialize_schedule(schedule: Schedule) -> dict[str, Any]:
        return {
            "id": str(schedule.id),
            "organization_id": str(schedule.organization_id),
            "project_id": str(schedule.project_id),
            "pipeline_id": str(schedule.pipeline_id),
            "pipeline_version_id": str(schedule.pipeline_version_id),
            "standing_preparation_id": str(schedule.standing_preparation_id),
            "cron_expression": schedule.cron_expression,
            "timezone": schedule.timezone,
            "enabled": schedule.enabled,
            "concurrency_policy": schedule.concurrency_policy,
            "misfire_policy": schedule.misfire_policy,
            "version_binding_mode": schedule.version_binding_mode,
            "next_run_at": schedule.next_run_at.isoformat() if schedule.next_run_at else None,
            "last_triggered_at": (
                schedule.last_triggered_at.isoformat() if schedule.last_triggered_at else None
            ),
            "created_by": str(schedule.created_by),
            "created_at": schedule.created_at.isoformat(),
        }
