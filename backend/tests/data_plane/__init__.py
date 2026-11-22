"""数据面（Data Plane / SeaTunnel / Quality / Supervision / Publish / Rollback）单元测试与契约/故障注入测试套件。

重要声明：
本目录下的测试属于 L0（静态契约与安全边界）与 L1（确定性领域逻辑与模拟故障注入）测试；
绝不将本套件中的模拟传输层结果伪称为 L2 真实 SeaTunnel Zeta / Doris 容器端到端执行结果。
L2 真实容器验收将在 Wave 1 合并并冷启动 Docker Compose 全栈服务后独立运行并记录 `test_evidence_ref`。
"""
