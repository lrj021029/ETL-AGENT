# ADR-004: QualityContract 两阶段分流、Runtime Supervision 与 Doris 原子发布

- **状态**：APPROVED（2026-11-22）
- **决策者**：用户 + Principal Architect + Data

## 决策
1. **两阶段质量门禁（避免无状态行级算子硬塞全局 SQL 校验）**：
   - **Stage 1（SeaTunnel 流式行级分流）**：对 `NOT_NULL`、`DATA_TYPE`、`REGEX`、`RANGE`、`ENUM` 等行级规则，在 SeaTunnel 作业内将合法行写入目标影子表（`<target>__shadow_<short_exec_id>`），违规行连同 `execution_id, rule_id, error_code, reason_summary, raw_payload_redacted, detected_at` 分流写入错误表（`<target>__error`）。
   - **Stage 2（影子表级校验，`VALIDATING` 状态）**：SeaTunnel 批作业落盘影子表后、发布前，由受管只读 SQL 校验器（经同一 AST 沙箱）对影子表执行 `UNIQUE` 与受限 `SELECT` 质量查询，将违规摘要计入 `quality_reports`、`quality_rule_results` 与 `rejected_record_summaries`，汇总最终 `validated_rows` 与 `error_rows`。
2. **Runtime Supervision 拒绝率与中断语义**：
   - 拒绝率公式：`reject_rate = error_rows / validated_rows`（`validated_rows == 0` 时不触发除零）。
   - 整体拒绝率阈值已确认为 **20%**，按字面 `>` 触发（`reject_rate > 0.20` 触发；`== 0.20` 不因该条件单独触发）。
   - `min_validated_rows`、`max_reject_rate_window`、`window_definition`、`max_error_rows_absolute`、`max_read_rows`、`max_write_bytes`、`max_duration_seconds`、`max_amplification_ratio` 全部通过 `RuntimeSupervisionContract` 配置化传入（严禁硬编码为不可修改常量）。
   - 超限时由 Worker 经受管 `SeaTunnelAdapter.cancel_job` 触发硬中断，保存失败快照与无副作用诊断建议，绝不自动回滚，等待 Operator 经 Harness 发起受管回滚。
3. **Doris 原子发布与版本恢复**：
   - 发布前由 `PublishAdapter` 强制执行 Schema 兼容校验（列名、数据类型、Nullability、Key 模型与分区兼容性）。
   - 通过后执行 `ALTER TABLE <target> REPLACE WITH TABLE <shadow> PROPERTIES('swap'='true')`，原表数据保留为 `<shadow>` 归档引用并登记 `PublishedDatasetVersion`（保存 `previous_version_id`、`archive_table_ref`、`swap_result`）。
   - 已发布版本回滚通过再次受管 Swap 恢复，并新建 `rollback_runs` 与 `evidence_events`，永不删除历史记录。
