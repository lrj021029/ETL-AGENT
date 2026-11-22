"""无副作用执行故障诊断引擎（No-Side-Effect Failure Diagnosis Engine）。

严格遵循 PRD §5.2 与 TECHNICAL_SPEC §11.5：
1. 分析运行指标、拒绝率超限、质量规则错误码分布、SeaTunnel 异常摘要、Schema Drift 及 CDC 位点状态。
2. 输出带置信度（confidence）与根因分类（root_cause_category）的可解释建议。
3. 诊断引擎仅作只读分析（`advisory_only=True`, `side_effects_executed=False`），绝不自动执行写库、重提或回滚。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class DiagnosisActionSuggestion:
    """单条只读恢复或排查操作建议（需由人类 Maker/Operator 经受管流程发起）。"""

    action_code: str
    target_role: str
    description: str
    requires_harness_approval: bool = True

    def to_dict(self) -> dict[str, Any]:
        """转换为字典结构。"""
        return {
            "action_code": self.action_code,
            "target_role": self.target_role,
            "description": self.description,
            "requires_harness_approval": self.requires_harness_approval,
        }


@dataclass(frozen=True)
class DiagnosisReport:
    """结构化无副作用故障诊断报告（存入 `execution_runs.diagnosis_json`）。"""

    execution_id: str
    root_cause_category: str
    primary_error_code: str
    confidence: float
    summary: str
    evidence_indicators: list[str]
    recommended_actions: list[DiagnosisActionSuggestion]
    advisory_only: bool = True
    side_effects_executed: bool = False
    generated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def __post_init__(self) -> None:
        """强制保证零副作用标记不可被篡改为自动执行。"""
        if not self.advisory_only or self.side_effects_executed:
            raise ValueError("FailureDiagnosisEngine 报告必须保持 advisory_only=True 且 side_effects_executed=False")

    def to_dict(self) -> dict[str, Any]:
        """转换为 JSON 可序列化字典。"""
        return {
            "execution_id": self.execution_id,
            "root_cause_category": self.root_cause_category,
            "primary_error_code": self.primary_error_code,
            "confidence": round(self.confidence, 3),
            "summary": self.summary,
            "evidence_indicators": list(self.evidence_indicators),
            "recommended_actions": [a.to_dict() for a in self.recommended_actions],
            "advisory_only": True,
            "side_effects_executed": False,
            "generated_at": self.generated_at,
        }


class FailureDiagnosisEngine:
    """零副作用故障根因分析与恢复建议生成引擎。"""

    def diagnose(
        self,
        *,
        execution_id: str,
        status: str,
        status_reason: str,
        error_code_counts: dict[str, int] | None = None,
        supervision_breach: dict[str, Any] | None = None,
        schema_drift: dict[str, Any] | None = None,
        engine_error_message: str | None = None,
    ) -> DiagnosisReport:
        """基于多维只读上下文生成确定性、可解释的故障诊断报告。"""
        err_counts = error_code_counts or {}
        indicators: list[str] = [f"status={status}", f"status_reason={status_reason}"]

        # 1. 运行监督阈值超限（整体拒绝率 > 20%、窗口拒绝率、绝对错误数或预算超限）
        if supervision_breach or status == "INTERRUPTED" or "SUPERVISION" in status_reason:
            breach_type = (supervision_breach or {}).get("breach_type", status_reason)
            observed = (supervision_breach or {}).get("observed_value")
            threshold = (supervision_breach or {}).get("threshold")
            indicators.append(f"supervision_breach={breach_type}(observed={observed}, threshold={threshold})")
            if err_counts:
                top_dq = sorted(err_counts.items(), key=lambda kv: kv[1], reverse=True)
                indicators.append(
                    "quality_error_distribution="
                    + ",".join(f"{k}:{v}" for k, v in top_dq[:5])
                )

            return DiagnosisReport(
                execution_id=execution_id,
                root_cause_category="RUNTIME_SUPERVISION_BREACH",
                primary_error_code="SUPERVISION_THRESHOLD_BREACHED",
                confidence=0.95,
                summary=(
                    f"运行监督触发受管中断（{breach_type}）：观测值 {observed} 超过契约阈值 {threshold}。"
                    "违规行已分流至错误表，影子表未执行发布。"
                ),
                evidence_indicators=indicators,
                recommended_actions=[
                    DiagnosisActionSuggestion(
                        action_code="ROLLBACK_SHADOW_CLEANUP",
                        target_role="OPERATOR",
                        description="通过 Harness 发起未发布影子表受管清理（SHADOW_CLEANUP）。",
                        requires_harness_approval=True,
                    ),
                    DiagnosisActionSuggestion(
                        action_code="INSPECT_REJECTED_RECORDS",
                        target_role="MAKER",
                        description="审查错误表脱敏样本与上游数据格式变化，更新清洗规则后冻结新 PipelineVersion。",
                        requires_harness_approval=False,
                    ),
                ],
            )

        # 2. CDC 位点丢失或 Replication Slot / Binlog 过期
        if status in ("CHECKPOINT_LOST", "POSITION_EXPIRED") or status_reason in (
            "CHECKPOINT_LOST",
            "POSITION_EXPIRED",
        ):
            indicators.append(f"cdc_fault={status}")
            return DiagnosisReport(
                execution_id=execution_id,
                root_cause_category="CDC_OFFSET_OR_CHECKPOINT_FAULT",
                primary_error_code="CDC_RECOVERY_OPERATOR_CONFIRM_REQUIRED",
                confidence=0.96,
                summary=(
                    f"CDC 作业检测到 {status}（历史检查点不可用或源库 Binlog/WAL 位点已过期）。"
                    "按 C+D 恢复策略，系统已暂停自动恢复并等待 Operator 显式确认。"
                ),
                evidence_indicators=indicators,
                recommended_actions=[
                    DiagnosisActionSuggestion(
                        action_code="ROLLBACK_CDC_RECOVER_WITH_CONFIRM",
                        target_role="OPERATOR",
                        description="经 Harness 审批并显式附带 Operator 确认后执行重新快照或位点重置恢复。",
                        requires_harness_approval=True,
                    ),
                ],
            )

        # 3. 发布前 Schema 不兼容或上游 Schema Drift
        if (
            "PUBLISH_SCHEMA_INCOMPATIBLE" in status_reason
            or (schema_drift and schema_drift.get("has_drift"))
        ):
            if schema_drift:
                indicators.append(f"schema_drift_severity={schema_drift.get('severity')}")
            return DiagnosisReport(
                execution_id=execution_id,
                root_cause_category="SCHEMA_INCOMPATIBILITY_OR_DRIFT",
                primary_error_code="PUBLISH_SCHEMA_INCOMPATIBLE",
                confidence=0.94,
                summary="影子表与目标表 Schema 不兼容（列/类型/非空约束或 Key 模型不匹配），Doris 原子替换已被发布门禁阻断。",
                evidence_indicators=indicators,
                recommended_actions=[
                    DiagnosisActionSuggestion(
                        action_code="ROLLBACK_SHADOW_CLEANUP",
                        target_role="OPERATOR",
                        description="清理未发布的影子表临时资源。",
                        requires_harness_approval=True,
                    ),
                    DiagnosisActionSuggestion(
                        action_code="REFRESH_METADATA_SNAPSHOT",
                        target_role="MAKER",
                        description="重新执行只读 Metadata Probe 生成新快照并在 Studio 对齐字段映射。",
                        requires_harness_approval=False,
                    ),
                ],
            )

        # 4. SeaTunnel 提交状态不确定（UNKNOWN）
        if status == "UNKNOWN" or "SEATUNNEL_SUBMIT_UNKNOWN" in status_reason:
            return DiagnosisReport(
                execution_id=execution_id,
                root_cause_category="EXTERNAL_ENGINE_STATE_UNCERTAIN",
                primary_error_code="SEATUNNEL_SUBMIT_UNKNOWN",
                confidence=0.90,
                summary="SeaTunnel Zeta 作业提交响应超时或中断，当前状态进入 UNKNOWN，严禁盲目重提。",
                evidence_indicators=indicators,
                recommended_actions=[
                    DiagnosisActionSuggestion(
                        action_code="TRIGGER_RECONCILIATION",
                        target_role="OPERATOR",
                        description="调用受管对账接口（Reconcile）按 stable_submit_id 向 Zeta 查询真实作业状态。",
                        requires_harness_approval=False,
                    ),
                ],
            )

        # 5. 默认引擎或质量门禁执行失败
        if engine_error_message:
            indicators.append(f"engine_error={engine_error_message[:200]}")
        primary_code = next(iter(err_counts.keys()), "DQ_RESTRICTED_SQL_VIOLATION" if "DQ_" in status_reason else "STATE_TRANSITION_INVALID")
        return DiagnosisReport(
            execution_id=execution_id,
            root_cause_category="DATA_PLANE_EXECUTION_FAILURE",
            primary_error_code=primary_code,
            confidence=0.80,
            summary=f"数据面执行进入 {status} 状态（原因：{status_reason}）。请结合错误摘要与日志排查并由 Operator 决定回滚动作。",
            evidence_indicators=indicators,
            recommended_actions=[
                DiagnosisActionSuggestion(
                    action_code="EVALUATE_MANAGED_ROLLBACK",
                    target_role="OPERATOR",
                    description="根据当前是否已完成 Swap，在控制台选择影子表清理（SHADOW_CLEANUP）或版本恢复（SWAP_RESTORE）。",
                    requires_harness_approval=True,
                ),
            ],
        )
