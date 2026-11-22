# ADR-001: 本机 Windows Docker Compose 拓扑与 Vault 持久化模式

- **状态**：APPROVED（2026-11-22）
- **决策者**：用户 + Principal Architect

## 背景
项目在 Windows 11 Pro 本机进行全量开发，使用 Kiri Docker Linux Engine 运行容器，且需满足“单个 `compose.yaml` 文件、默认启动核心与真实演示服务”及“Vault 默认持久化模式”的要求。

## 决策
1. **单一 `compose.yaml`**：位于项目根目录 `d:\etl-agent\compose.yaml`。
2. **默认启动服务集合**：
   - 控制面与前端：`frontend` (Vue 3)、`api` (FastAPI)、`celery-worker`、`celery-beat`、`outbox-dispatcher`
   - 核心基础设施：`postgres` (PostgreSQL 16)、`redis` (Redis 7)、`vault` (HashiCorp Vault 持久化 file/raft 模式)、`minio` (MinIO)、`mailpit` (本地 Email OTP SMTP 捕获)
   - 数据面：`seatunnel-zeta` (单节点 Zeta 集群)、`demo-mysql` (MySQL 8.0，开启 Binlog ROW 模式支持 CDC)、`demo-postgres` (PostgreSQL 16，开启 `wal_level=logical` 支持 CDC)、`doris-fe` (单 FE)、`doris-be` (单 BE)
   - 可观测性：`otel-collector`、`prometheus`、`loki`、`tempo`、`grafana`
3. **扩展 Profiles（同文件管理）**：
   - `vault-dev`：可选快速内存开发模式
   - `ha-seatunnel`：多节点 SeaTunnel Zeta Worker 扩展
   - `ext-clickhouse` / `ext-oracle`：可选重型扩展数据源
4. **Vault 持久化模式约定**：
   - 使用 Docker Named Volume 持久化 Vault 数据；开启 `kv-v2`（路径 `secret/`）和 `transit`（Ed25519 密钥 `etl-agent-capability` 与 `etl-agent-evidence`）。
   - 提供幂等 PowerShell/Python 初始化与 Unseal 脚本（`scripts/dev/init-vault.ps1`），将本地初始化产物写入被 `.gitignore` 忽略的 `infra/vault/init/` 目录，严禁入库或打印到标准输出日志。
5. **网络隔离**：
   - 内部服务（PG/Redis/Vault/SeaTunnel/Doris BE/MySQL）默认仅加入内部 bridge 网络 `etl-internal`，仅在需要本机调试时绑定 `127.0.0.1`。前端、API、Grafana、Mailpit UI 仅绑定 `127.0.0.1`。
