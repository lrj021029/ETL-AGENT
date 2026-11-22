"""Apache SeaTunnel Zeta 2.3.9 REST 适配器与内存级 Secret 物化实现。

严格遵循 TECHNICAL_SPEC §5.3、§10.1、§10.3：
- 仅允许已冻结的 HOCON 模板（包含 `${SECRET:...}` 占位符，不含明文密码）进入提交链路。
- 先登记 `ExternalJobRefRecord`（绑定唯一 `stable_submit_id`）并将 `ExecutionRun` 置为 `SUBMITTING`，再发起外部提交。
- 若网络超时或响应丢失，立即将状态置为 `UNKNOWN`（`RECONCILIATION_PENDING`），绝不自动重发第二次提交请求。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
from typing import Any, Callable, Protocol
import uuid

import httpx

from backend.app.execution.state_machine import (
    ConfirmationState,
    ExecutionMode,
    ExecutionRunRecord,
    ExecutionStateMachine,
)

_SECRET_PLACEHOLDER_REGEX = re.compile(r"\$\{SECRET:([A-Z0-9_]+)\}")
# 检测 HOCON 中是否存在未走 ${SECRET:...} 占位符的明文密码赋值
_PLAINTEXT_SECRET_LINE_REGEX = re.compile(
    r'^\s*(?:password|passwd|secret_key|access_key_secret|token)\s*=\s*"?([^"\n\r]+)"?\s*$',
    flags=re.IGNORECASE | re.MULTILINE,
)


class SeaTunnelSubmitUnknownError(Exception):
    """当 SeaTunnel Zeta 作业提交超时或响应丢失、结果不确定时抛出（HTTP 202 / 待对账）。"""

    error_code: str = "SEATUNNEL_SUBMIT_UNKNOWN"

    def __init__(
        self,
        message: str,
        *,
        execution_id: str,
        stable_submit_id: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.execution_id = execution_id
        self.stable_submit_id = stable_submit_id
        self.detail = {
            "execution_id": execution_id,
            "stable_submit_id": stable_submit_id,
            **(detail or {}),
        }


def build_stable_submit_id(execution_id: str, command_id: str) -> str:
    """基于 `execution_id` 与 `command_id` 生成幂等且确定性的外部提交标识 `stable_submit_id`。"""
    exec_clean = re.sub(r"[^a-zA-Z0-9]", "", execution_id)[:12].lower()
    cmd_clean = re.sub(r"[^a-zA-Z0-9]", "", command_id)[:12].lower()
    if not exec_clean or not cmd_clean:
        raise ValueError("execution_id 与 command_id 不能为空")
    return f"stjob-{exec_clean}-{cmd_clean}"


def verify_hocon_has_no_plaintext_secrets(hocon_template: str) -> list[str]:
    """静态校验 HOCON 模板中所有敏感凭据字段必须使用 `${SECRET:...}` 占位符，严禁包含明文密码。

    返回提取到的全部 `${SECRET:...}` 占位符列表；若检测到明文凭据则抛出 `ValueError`。
    """
    for match in _PLAINTEXT_SECRET_LINE_REGEX.finditer(hocon_template):
        raw_val = match.group(1).strip()
        if not _SECRET_PLACEHOLDER_REGEX.fullmatch(raw_val):
            raise ValueError(
                "HOCON 模板安全违规：检测到未通过 ${SECRET:...} 占位符引用的疑似明文凭据！"
            )
    return sorted(set(_SECRET_PLACEHOLDER_REGEX.findall(hocon_template)))


def materialize_hocon_secrets_in_memory(
    hocon_template: str,
    secret_resolver: Callable[[str], str],
) -> str:
    """仅在内存最短生命周期内将 HOCON 模板中的 `${SECRET:KEY}` 替换为真实凭据。

    注意：物化后的字符串仅用于发往内部网络的 SeaTunnel Zeta 请求体，绝不可落盘或写入日志。
    """
    verify_hocon_has_no_plaintext_secrets(hocon_template)

    def _replace(match: re.Match[str]) -> str:
        secret_key = match.group(1)
        secret_value = secret_resolver(secret_key)
        if not secret_value:
            raise ValueError(f"无法从 SecretProvider 解析凭据占位符: ${{SECRET:{secret_key}}}")
        return secret_value

    return _SECRET_PLACEHOLDER_REGEX.sub(_replace, hocon_template)


@dataclass
class ExternalJobRefRecord:
    """外部引擎作业引用与对账事实记录（对应 `contracts/db/erd.md` §2.5 `external_job_refs` 表）。"""

    id: str
    organization_id: str
    project_id: str
    execution_id: str
    stable_submit_id: str
    engine_type: str = "SEATUNNEL_ZETA_2_3_9"
    external_job_id: str | None = None
    submit_state: str = "SUBMITTING"
    last_reconciled_at: str | None = None
    raw_status_json: dict[str, Any] = field(default_factory=dict)


class ZetaTransport(Protocol):
    """SeaTunnel Zeta 2.3.9 HTTP REST 传输层协议（支持注入真实 httpx 或故障注入传输层）。"""

    def post_submit_job(
        self,
        *,
        job_name: str,
        hocon_body: str,
    ) -> dict[str, Any]:
        """调用 Zeta `/hazelcast/rest/maps/submit-job` 提交作业。"""

    def get_job_info(self, external_job_id: str) -> dict[str, Any]:
        """调用 Zeta `/hazelcast/rest/maps/job-info/{jobId}` 查询作业状态与指标。"""

    def list_jobs(self) -> list[dict[str, Any]]:
        """调用 Zeta `/hazelcast/rest/maps/running-jobs` 与 `/finished-jobs` 获取作业清单用于对账。"""

    def post_stop_job(
        self,
        external_job_id: str,
        *,
        is_stop_with_save_point: bool = False,
    ) -> dict[str, Any]:
        """调用 Zeta `/hazelcast/rest/maps/stop-job` 受管取消或带保存点停止作业。"""


class HttpxZetaTransport:
    """基于 `httpx` 的真实 SeaTunnel Zeta 2.3.9 REST API 客户端传输实现。"""

    def __init__(
        self,
        base_url: str = "http://seatunnel-zeta:5801",
        timeout_seconds: float = 15.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds

    def post_submit_job(
        self,
        *,
        job_name: str,
        hocon_body: str,
    ) -> dict[str, Any]:
        """向 SeaTunnel Zeta 2.3.9 提交 HOCON 格式作业。"""
        url = f"{self._base_url}/hazelcast/rest/maps/submit-job"
        params = {"jobName": job_name, "format": "hocon"}
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.post(
                url,
                params=params,
                content=hocon_body.encode("utf-8"),
                headers={"Content-Type": "text/plain; charset=utf-8"},
            )
            resp.raise_for_status()
            return dict(resp.json())

    def get_job_info(self, external_job_id: str) -> dict[str, Any]:
        """查询单个 SeaTunnel Zeta 作业详情与运行指标。"""
        url = f"{self._base_url}/hazelcast/rest/maps/job-info/{external_job_id}"
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.get(url)
            resp.raise_for_status()
            return dict(resp.json())

    def list_jobs(self) -> list[dict[str, Any]]:
        """列出 Zeta 集群中的运行中与已结束作业列表，用于 UNKNOWN 状态对账。"""
        jobs: list[dict[str, Any]] = []
        with httpx.Client(timeout=self._timeout) as client:
            running_resp = client.get(f"{self._base_url}/hazelcast/rest/maps/running-jobs")
            if running_resp.status_code == 200:
                data = running_resp.json()
                if isinstance(data, list):
                    jobs.extend(data)
            finished_resp = client.get(f"{self._base_url}/hazelcast/rest/maps/finished-jobs")
            if finished_resp.status_code == 200:
                data = finished_resp.json()
                if isinstance(data, list):
                    jobs.extend(data)
        return jobs

    def post_stop_job(
        self,
        external_job_id: str,
        *,
        is_stop_with_save_point: bool = False,
    ) -> dict[str, Any]:
        """向 SeaTunnel Zeta 发送受管停止作业指令。"""
        url = f"{self._base_url}/hazelcast/rest/maps/stop-job"
        payload = {
            "jobId": external_job_id,
            "isStopWithSavePoint": is_stop_with_save_point,
        }
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            return dict(resp.json())


class SeaTunnelZetaAdapter:
    """SeaTunnel Zeta 2.3.9 受管数据面适配器。

    负责：
    1. 幂等登记 `ExternalJobRefRecord`（以 `stable_submit_id` 为唯一键）；
    2. 内存级物化 Secret 并提交 HOCON 作业；
    3. 提交响应丢失或超时时严格进入 `UNKNOWN`（`RECONCILIATION_PENDING`），禁止盲目重提；
    4. 查询作业状态、提取度量指标及受管取消作业。
    """

    def __init__(
        self,
        transport: ZetaTransport | None = None,
    ) -> None:
        self._transport: ZetaTransport = transport or HttpxZetaTransport()
        # 按 stable_submit_id 与 execution_id 索引外部作业引用记录
        self._job_refs_by_submit_id: dict[str, ExternalJobRefRecord] = {}
        self._job_refs_by_execution_id: dict[str, ExternalJobRefRecord] = {}

    def get_job_ref_by_execution(self, execution_id: str) -> ExternalJobRefRecord | None:
        """查询指定 `execution_id` 绑定的外部作业引用。"""
        return self._job_refs_by_execution_id.get(execution_id)

    def submit_job(
        self,
        *,
        run: ExecutionRunRecord,
        hocon_template: str,
        secret_resolver: Callable[[str], str],
        trace_id: str = "",
    ) -> ExternalJobRefRecord:
        """向 SeaTunnel Zeta 提交批处理或 CDC 作业。

        核心安全与一致性保障：
        - 若同一 `stable_submit_id` 已提交或处于 `UNKNOWN` 待对账状态，绝不发起第二次 HTTP 提交！
        - 提交前先将 `run` 状态推进至 `SUBMITTING`（`REQUEST_SUBMITTED`）。
        - 若传输层抛出超时或连接中断异常，将 `run` 置为 `UNKNOWN`（`RECONCILIATION_PENDING`）并抛出 `SeaTunnelSubmitUnknownError`。
        """
        stable_submit_id = build_stable_submit_id(run.execution_id, run.command_id)
        existing_ref = self._job_refs_by_submit_id.get(stable_submit_id)
        if existing_ref is not None:
            # 幂等保护：同一稳定提交 ID 已经发起过提交（无论 CONFIRMED 还是 UNKNOWN），直接返回不重提
            return existing_ref

        # 校验 HOCON 模板无明文密码并在内存中物化 Secret
        materialized_hocon = materialize_hocon_secrets_in_memory(
            hocon_template,
            secret_resolver,
        )

        # 推进状态机至 SUBMITTING (REQUEST_SUBMITTED)
        if run.status == "CREATED":
            ExecutionStateMachine.transition(
                run,
                "QUEUED",
                reason_code="COMMAND_DEQUEUED_FOR_SUBMIT",
                confirmation_state=ConfirmationState.REQUEST_SUBMITTED,
                trace_id=trace_id,
            )
        if run.status == "QUEUED":
            ExecutionStateMachine.transition(
                run,
                "SUBMITTING",
                reason_code="SEATUNNEL_SUBMIT_INTENT_PERSISTED",
                confirmation_state=ConfirmationState.REQUEST_SUBMITTED,
                trace_id=trace_id,
                detail={"stable_submit_id": stable_submit_id},
            )

        # 先持久化外部提交意图记录
        job_ref = ExternalJobRefRecord(
            id=str(uuid.uuid4()),
            organization_id=run.organization_id,
            project_id=run.project_id,
            execution_id=run.execution_id,
            stable_submit_id=stable_submit_id,
            submit_state="SUBMITTING",
        )
        self._job_refs_by_submit_id[stable_submit_id] = job_ref
        self._job_refs_by_execution_id[run.execution_id] = job_ref

        try:
            response = self._transport.post_submit_job(
                job_name=stable_submit_id,
                hocon_body=materialized_hocon,
            )
        except (TimeoutError, ConnectionError, httpx.TimeoutException, httpx.TransportError) as exc:
            # 关键不变量：提交响应丢失或网络中断时，进入 UNKNOWN 待对账状态，严禁盲目重提！
            job_ref.submit_state = "UNKNOWN"
            job_ref.raw_status_json = {"submit_error_type": type(exc).__name__}
            ExecutionStateMachine.transition(
                run,
                "UNKNOWN",
                reason_code="SEATUNNEL_SUBMIT_UNKNOWN",
                confirmation_state=ConfirmationState.RECONCILIATION_PENDING,
                trace_id=trace_id,
                detail={"stable_submit_id": stable_submit_id},
            )
            raise SeaTunnelSubmitUnknownError(
                f"SeaTunnel Zeta 作业提交响应不确定 (stable_submit_id={stable_submit_id})，"
                "已转入 UNKNOWN 状态等待 RunReconciler 对账，禁止盲目重提。",
                execution_id=run.execution_id,
                stable_submit_id=stable_submit_id,
            ) from exc
        finally:
            # 立即清除局部变量对已物化 HOCON 的引用
            del materialized_hocon

        ext_job_id = str(response.get("jobId") or response.get("job_id") or "")
        if not ext_job_id:
            job_ref.submit_state = "UNKNOWN"
            job_ref.raw_status_json = response
            ExecutionStateMachine.transition(
                run,
                "UNKNOWN",
                reason_code="SEATUNNEL_SUBMIT_MISSING_JOB_ID",
                confirmation_state=ConfirmationState.RECONCILIATION_PENDING,
                trace_id=trace_id,
            )
            raise SeaTunnelSubmitUnknownError(
                "SeaTunnel Zeta 提交响应未返回有效 jobId，已转入 UNKNOWN 待对账状态",
                execution_id=run.execution_id,
                stable_submit_id=stable_submit_id,
            )

        job_ref.external_job_id = ext_job_id
        job_ref.submit_state = "ENGINE_CONFIRMED"
        job_ref.raw_status_json = response

        target_running_state = (
            "RUNNING" if run.execution_mode == ExecutionMode.BATCH else "INITIAL_SNAPSHOT"
        )
        ExecutionStateMachine.transition(
            run,
            target_running_state,
            reason_code="SEATUNNEL_JOB_SUBMIT_CONFIRMED",
            confirmation_state=ConfirmationState.ENGINE_CONFIRMED,
            external_job_id=ext_job_id,
            trace_id=trace_id,
            detail={"stable_submit_id": stable_submit_id, "external_job_id": ext_job_id},
        )
        return job_ref

    def get_job_status(self, external_job_id: str) -> dict[str, Any]:
        """查询外部 SeaTunnel 作业当前状态。"""
        info = self._transport.get_job_info(external_job_id)
        return {
            "external_job_id": external_job_id,
            "job_status": str(info.get("jobStatus", "UNKNOWN")).upper(),
            "job_name": info.get("jobName"),
            "error_msg": info.get("errorMsg"),
            "metrics": info.get("metrics") or {},
        }

    def get_job_metrics(self, external_job_id: str) -> dict[str, Any]:
        """从 SeaTunnel Zeta `job-info` 响应中提取标准化读写行数与字节指标。"""
        info = self._transport.get_job_info(external_job_id)
        raw_metrics = info.get("metrics") or {}
        read_rows = int(raw_metrics.get("SourceReceivedCount", raw_metrics.get("read_rows", 0)))
        write_rows = int(raw_metrics.get("SinkWriteCount", raw_metrics.get("write_rows", 0)))
        write_bytes = int(raw_metrics.get("SinkWriteBytes", raw_metrics.get("write_bytes", 0)))
        error_rows = int(raw_metrics.get("ErrorRowCount", raw_metrics.get("error_rows", 0)))
        return {
            "external_job_id": external_job_id,
            "read_rows": read_rows,
            "write_rows": write_rows,
            "write_bytes": write_bytes,
            "error_rows": error_rows,
            "raw_metrics": raw_metrics,
        }

    def cancel_job(
        self,
        external_job_id: str,
        *,
        with_savepoint: bool = False,
    ) -> dict[str, Any]:
        """向 SeaTunnel Zeta 发送受管取消或带保存点停止请求。"""
        resp = self._transport.post_stop_job(
            external_job_id,
            is_stop_with_save_point=with_savepoint,
        )
        return {
            "external_job_id": external_job_id,
            "stop_requested": True,
            "with_savepoint": with_savepoint,
            "engine_response": resp,
        }

    def list_cluster_jobs(self) -> list[dict[str, Any]]:
        """获取 Zeta 集群中的作业清单供 `RunReconciler` 对账。"""
        return self._transport.list_jobs()
