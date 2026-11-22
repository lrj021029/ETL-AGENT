# M1 平台基础设施与 Docker Compose 实施手册 (`docs/implementation/platform/README.md`)

- **负责模块**：Platform / DevEx (`@platform`)
- **对应里程碑**：M1 Infrastructure & Compose
- **架构依据**：`ENV-OVERRIDE-001`、`DECISION_REGISTER.md`、`ADR-001`、`ADR-002`、`contracts/versions.lock.yaml`

---

## 1. Windows 本机开发与容器运行基线

| 组件 | 锁定规格 / 实测版本 | 说明 |
|---|---|---|
| 宿主机操作系统 | Windows 11 Pro (`10.0.26300`) | 物理内存 31.63 GB，`C:` 可用 ~148 GB，`D:` 可用 ~182 GB |
| 开发终端 Shell | Windows PowerShell `5.1.26100.9818` | 所有脚本兼容 PS 5.1，不使用 `&&` 或 WSL Bash |
| 容器运行时 | Kiri Engine `v0.2.2` (Docker Client `29.9.2` / Server `27.5.1` `linux/amd64`) | Compose `v5.6.1`，通过 `C:\Users\FireFly\AppData\Local\Kiri\bin` 调用 |
| 语言工具链 | Python `3.12.9` / Node `v26.13.1` / pnpm `9.12.3` | Node 路径 `D:\code\nodejs` 置于 `PATH` 最前 |

---

## 2. 单一 `compose.yaml` 服务清单、端口绑定与内存分配

所有默认服务（共 **20 个服务**）均在默认启动集合中（未隐藏在 `profiles` 下），且所有暴露到宿主机的端口**严格绑定至 `127.0.0.1`**，内部通过 `etl-internal` bridge 网络通信。

### 2.1 默认启动服务矩阵

| 分类 | 服务名 | 锁定镜像 / Dockerfile | 宿主机回环端口 (`127.0.0.1`) | 持久化 Named Volume | 内存限额 (`mem_limit`) |
|---|---|---|---|---|---|
| 控制面与 UI | `frontend` | `docker/frontend.Dockerfile` | `127.0.0.1:5173->5173` | `frontend_cache` | `512m` |
| 控制面与 UI | `api` | `docker/backend.Dockerfile` | `127.0.0.1:8000->8000` | `backend_uploads` | `768m` |
| 控制面与 UI | `celery-worker` | `docker/backend.Dockerfile` | 仅内部网络 (`etl-internal`) | `backend_uploads` | `768m` |
| 控制面与 UI | `celery-beat` | `docker/backend.Dockerfile` | 仅内部网络 (`etl-internal`) | `celery_beat_data` | `384m` |
| 控制面与 UI | `outbox-dispatcher` | `docker/backend.Dockerfile` | 仅内部网络 (`etl-internal`) | `outbox_dispatcher_state` | `384m` |
| 核心基础设施 | `postgres` | `postgres:16-alpine` | `127.0.0.1:5432->5432` | `postgres_data` | `1024m` |
| 核心基础设施 | `redis` | `redis:7-alpine` | `127.0.0.1:6379->6379` | `redis_data` | `512m` |
| 核心基础设施 | `vault` | `hashicorp/vault:1.18` | `127.0.0.1:8200->8200` | `vault_data` | `512m` |
| 核心基础设施 | `minio` | `minio/minio:RELEASE.2024-12-18T13-15-44Z` | `127.0.0.1:9000->9000`, `127.0.0.1:9001->9001` | `minio_data` | `512m` |
| 核心基础设施 | `mailpit` | `axllent/mailpit:v1.21` | `127.0.0.1:1025->1025`, `127.0.0.1:8025->8025` | `mailpit_data` | `256m` |
| 数据面与演示库 | `seatunnel-zeta` | `apache/seatunnel:2.3.9` | `127.0.0.1:5801->5801` | `seatunnel_data` | `2048m` (JVM `-Xmx1536m`) |
| 数据面与演示库 | `demo-mysql` | `mysql:8.0` | `127.0.0.1:3306->3306` | `demo_mysql_data` | `1024m` |
| 数据面与演示库 | `demo-postgres` | `postgres:16-alpine` (`wal_level=logical`) | `127.0.0.1:5433->5432` | `demo_postgres_data` | `512m` |
| 数据面与演示库 | `doris-fe` | `apache/doris:fe-2.1.7` | `127.0.0.1:8030->8030`, `127.0.0.1:9030->9030` | `doris_fe_meta`, `doris_fe_log` | `2560m` (JVM `-Xmx2048m`) |
| 数据面与演示库 | `doris-be` | `apache/doris:be-2.1.7` | `127.0.0.1:8040->8040` | `doris_be_storage`, `doris_be_log` | `4096m` (`mem_limit=4G`) |
| 可观测性栈 | `otel-collector` | `otel/opentelemetry-collector-contrib:0.116.1` | `127.0.0.1:4317->4317`, `127.0.0.1:4318->4318` | `otel_data` | `512m` |
| 可观测性栈 | `prometheus` | `prom/prometheus:v2.55.1` | `127.0.0.1:9090->9090` | `prometheus_data` | `512m` |
| 可观测性栈 | `loki` | `grafana/loki:3.3.2` | `127.0.0.1:3100->3100` | `loki_data` | `512m` |
| 可观测性栈 | `tempo` | `grafana/tempo:2.6.1` | `127.0.0.1:3200->3200` | `tempo_data` | `512m` |
| 可观测性栈 | `grafana` | `grafana/grafana:11.4.0` | `127.0.0.1:3000->3000` | `grafana_data` | `512m` |

> **总计默认服务 `mem_limit` 上限总和**：`18,304 MB` (~`17.88 GB`)，所有 JVM 与 C++ 存储引擎均配置了显式堆内存上限。

### 2.2 同文件可选扩展 Profiles

- `vault-dev`：快速内存开发模式 Vault（端口 `127.0.0.1:8201`）
- `ha-seatunnel`：多节点 SeaTunnel Zeta Worker 扩展节点（`seatunnel-zeta-worker`）
- `ext-clickhouse`：扩展分析型数据源 ClickHouse（`clickhouse/clickhouse-server:24.8-alpine`）

---

## 3. 安全基础设施关键设计

1. **PostgreSQL 三角色物理分离 (`infra/databases/postgres/init_roles.sql`)**：
   - `etl_migrator`：仅用于 Alembic 迁移和表结构拥有者（Table Owner）。
   - `etl_runtime`：FastAPI 与 Celery Worker 运行时使用，**明确 `NOBYPASSRLS`** 且不拥有任何业务表。
   - `etl_outbox_dispatcher`：Outbox Dispatcher 专用最小权限角色，**明确 `NOBYPASSRLS`**。
2. **HashiCorp Vault 持久化模式与幂等初始化 (`infra/vault/config/vault.hcl` + `scripts/dev/init-vault.ps1`)**：
   - 默认采用 `file` 持久化后端挂载至 `vault_data` 卷。
   - `scripts/dev/init-vault.ps1` 自动检测初始化状态：首次初始化将 Unseal Key 与 Root Token **仅写入** `.gitignore` 保护的 `infra/vault/init/vault-init.json`，**绝不输出到终端或日志**。
   - 自动完成 Unseal、挂载 `kv-v2` (`secret/`)、挂载 `transit` (`transit/`)，并创建 Ed25519 密钥 `etl-agent-capability` 与 `etl-agent-evidence`。

---

## 4. Windows PowerShell 从零启动操作指南

> **权限说明**：以下所有命令均在**普通用户权限**的 Windows PowerShell 5.1 中执行，无需管理员权限（前提是 Kiri Docker Engine 已在当前用户会话下运行）。

```powershell
# 1. 设置工具链 PATH（每次新开 PowerShell 会话时执行，脚本内部也已自动内置）
$env:PATH = "C:\Users\FireFly\AppData\Local\Kiri\bin;D:\code\nodejs;$env:PATH"

# 2. 执行本机环境、工具链与 Compose 静态配置预检
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\dev\env-check.ps1

# 3. 仅校验启动配置（不拉起容器）
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\dev\up.ps1 -ValidateOnly

# 4. 启动全部默认服务并自动初始化/解封持久化 Vault
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\dev\up.ps1

# 5. 查看容器健康检查状态与持久化数据卷（附加 -ShowStats 查看实时内存/CPU）
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\dev\status.ps1 -ShowStats

# 6. 查看指定服务日志
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\dev\logs.ps1 -Service api -Tail 100

# 7. 安全停止容器栈（严格保留所有持久化 Named Volumes，绝不删除数据卷）
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\dev\down.ps1
```
