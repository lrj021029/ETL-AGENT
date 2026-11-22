# Layer 0 契约：Canonical JSON 与 SHA256 摘要规范

- **Contract Version**：`1.0.0`
- **Owner**：Lead / Architect + Security / Harness
- **Consumers**：Agent/DSL (ArtifactManifest), Security (PreparationFingerprint, ApprovalDigest, EvidenceLedger), QA

## 1. Canonical JSON 规则（RFC 8785 JCS 兼容）

1. 编码：UTF-8，无 BOM。
2. 键排序：所有 JSON Object 递归按 Unicode 码点（UTF-8/UTF-16 常用 BMP 一致，ASCII 标识符完全一致）升序排序。
3. 分隔符：紧凑格式，`separators=(",", ":")`，无前后空格与换行。
4. 字符串：`ensure_ascii=False`，仅转义标准 JSON 控制字符、`"` 与 `\`。
5. 数值：`allow_nan=False`（禁止 `NaN`/`Infinity`）；整值不附带 `.0`；浮点统一规范化（移除无意义尾随零，`-0.0` 归一为 `0`）。
6. 空值与缺失：明确区分字段缺失与 `null`；制品哈希输入对象必须使用固定 Schema 序列化，且排除非语义元数据字段（如 `created_at`、`updated_at`、临时文件路径）。

## 2. 哈希与链布局

- **ArtifactManifest Hash**：
  `artifact_hash = "sha256:" + sha256(canonical_bytes(manifest_content)).hexdigest()`
- **Preparation Fingerprint**：
  `fingerprint = "sha256:" + sha256(canonical_bytes(frozen_preparation_inputs)).hexdigest()`
- **Evidence Ledger Event Hash**：
  `genesis_previous_hash = "sha256:" + "0" * 64`
  `event_hash = "sha256:" + sha256(canonical_bytes(event_without_hash) + b"\n" + previous_hash.encode("utf-8")).hexdigest()`

## 3. 固定测试向量

见同目录 [test_vectors.json](file:///d:/etl-agent/contracts/crypto/test_vectors.json)，所有实现必须通过该文件的逐字节精确比对测试。
