# ADR-005: Canonical JSON 摘要规范与 Evidence Ledger 哈希链布局

- **状态**：APPROVED（2026-11-22）
- **决策者**：Principal Architect + Security

## 决策
1. **Canonical JSON 算法**：采用 **RFC 8785 (JSON Canonicalization Scheme, JCS)** 语义子集：
   - UTF-8 无 BOM 编码；
   - JSON Object 按 Unicode 码点升序排序 Key；
   - 无多余空白字符（分隔符 `","` 与 `":"`）；
   - 字符串仅做标准 JSON 转义，非 ASCII 字符以 UTF-8 原样输出（`ensure_ascii=False`）；
   - 禁止 `NaN`/`Infinity`；整数不带 `.0`，浮点数使用标准规范十进制表示；
   - 缺失字段与显式 `null` 语义严格区分（在 Pydantic 模型导出时按规范契约固定字段集）；
   - 二进制大对象以 `sha256:<hex>` 内容摘要引用，不包含本地动态路径或生成时间戳。
2. **ArtifactManifest 与 PreparationFingerprint 摘要**：
   - `artifact_hash = "sha256:" + SHA256(jcs_bytes(manifest_content))`（排除纯展示时间戳字段）。
   - `preparation_fingerprint = "sha256:" + SHA256(jcs_bytes(frozen_preparation_inputs))`。
3. **Evidence Ledger 字节布局与签名检查点**：
   - 每条事件计算：`event_hash = "sha256:" + SHA256(jcs_bytes(event_without_hash) + b"\n" + previous_hash.encode("utf-8"))`。
   - 创世事件的 `previous_hash` 固定为 `"sha256:" + "0"*64`（按组织+项目维度独立链，含单调递增 `sequence_no` 防重排/删行）。
   - 每 `N` 条事件（默认可配置如 50）或固定周期生成 `evidence_checkpoints`，对 `jcs_bytes(checkpoint_payload)` 使用 Ed25519 签名，记录 `signing_key_id (kid)`、`signature_b64url` 与公钥/Vault Transit key version，便于密钥轮转后仍可独立验证历史检查点。
   - 所有跨模块共享测试向量固化在 `contracts/crypto/test_vectors.json`。
