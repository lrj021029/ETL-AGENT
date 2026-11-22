"""不可变 ArtifactManifest 与 PipelineVersion 构建器。

内容哈希仅覆盖语义内容：在计算摘要前，所有动态元数据（created_at、updated_at、
临时路径等）都会被递归剥离，因此时间戳变化绝不会改变制品摘要。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from .canonical import sha256_digest

# 不参与制品内容哈希的动态元数据字段
DYNAMIC_FIELDS = frozenset(
    {"created_at", "updated_at", "frozen_at", "timestamp", "temp_path", "tmp_path", "local_path", "artifact_hash"}
)


def strip_dynamic(obj: Any) -> Any:
    """递归移除动态元数据字段。"""
    if isinstance(obj, dict):
        return {k: strip_dynamic(v) for k, v in obj.items() if k not in DYNAMIC_FIELDS}
    if isinstance(obj, (list, tuple)):
        return [strip_dynamic(x) for x in obj]
    return obj


def content_hash(content: dict[str, Any]) -> str:
    """对剥离动态字段后的内容计算 Canonical JSON SHA256。"""
    return sha256_digest(strip_dynamic(content))


class ArtifactManifest(BaseModel):
    """冻结的制品清单；frozen=True 保证运行时不可变。"""

    model_config = ConfigDict(frozen=True)

    content: dict[str, Any]
    artifact_hash: str
    created_at: str | None = None  # 动态时间戳，不进入哈希输入

    @classmethod
    def build(cls, content: dict[str, Any], created_at: str | None = None) -> "ArtifactManifest":
        return cls(content=strip_dynamic(content), artifact_hash=content_hash(content), created_at=created_at)

    def verify(self) -> bool:
        """重新计算摘要并比对，验证清单未被篡改。"""
        return self.artifact_hash == content_hash(self.content)


class PipelineVersion(BaseModel):
    """不可变流水线版本，绑定唯一 ArtifactManifest。"""

    model_config = ConfigDict(frozen=True)

    pipeline_id: str
    revision: int
    manifest: ArtifactManifest

    @property
    def artifact_hash(self) -> str:
        return self.manifest.artifact_hash


def build_pipeline_version(
    *,
    pipeline_id: str,
    revision: int,
    plan: dict[str, Any],
    quality_contract: dict[str, Any],
    supervision_contract: dict[str, Any],
    hocon_text: str,
    sql_hashes: dict[str, str] | None = None,
    secret_refs: list[dict[str, Any]] | None = None,
    created_at: str | None = None,
) -> PipelineVersion:
    """汇总各子契约摘要并构建不可变 PipelineVersion。"""
    content = {
        "schema_version": "1.0.0",
        "pipeline_id": pipeline_id,
        "revision": revision,
        "plan_digest": content_hash(plan),
        "quality_contract_digest": content_hash(quality_contract),
        "supervision_contract_digest": content_hash(supervision_contract),
        "hocon_digest": sha256_digest(hocon_text),
        "sql_hashes": dict(sql_hashes or {}),
        "secret_refs": [
            {"placeholder": s["placeholder"], "secret_version": s["secret_version"]} for s in (secret_refs or [])
        ],
    }
    return PipelineVersion(
        pipeline_id=pipeline_id,
        revision=revision,
        manifest=ArtifactManifest.build(content, created_at=created_at),
    )
