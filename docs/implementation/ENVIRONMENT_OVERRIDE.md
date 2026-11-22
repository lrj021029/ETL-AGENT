# ENV-OVERRIDE-001: Windows 本机全量开发与运行环境登记

- **记录编号**：`ENV-OVERRIDE-001`
- **生效日期**：2026-11-22
- **权威依据**：用户在当前实施对话中的最新明确决策及 `MULTI_AGENT_TASK_PROMPTS.md` §0.1
- **保护声明**：`docs/PRD.md` 与 `docs/TECHNICAL_SPEC.md` 保持原样只读，未经授权不作修改。本登记仅覆盖开发与运行环境拓扑，不扩大覆盖任何产品或安全业务架构不变量。

## 1. 实测本机环境基线

| 项目 | 实测结果 |
|---|---|
| 操作系统 | Microsoft Windows 11 专业版（10.0.26300），物理内存 ~31.6 GB |
| 磁盘可用空间 | `C:` 149.19 GB，`D:` 182.64 GB |
| 主仓库根目录 | `d:\etl-agent`（Windows NTFS，Git 2.53.0.windows.2） |
| 开发 Shell | Windows PowerShell 5.1（兼容命令，不用 Bash 专属语法） |
| 容器运行时 | Kiri Engine v0.2.2（Docker Client 29.9.2 / Engine API 1.47 / `linux/amd64` / Compose v5.6.1），位于 `C:\Users\FireFly\AppData\Local\Kiri\bin\docker.exe` |
| Python | Python 3.12.9 (`C:\Users\FireFly\AppData\Local\Programs\Python\Python312\python.exe`), pip 26.0.1 |
| Node / pnpm | Node `v26.13.1` (`D:\code\nodejs\node.exe`), npm `10.7.0`, pnpm `9.12.3` |

## 2. 覆盖条款与工具链注意事项

1. **禁止使用 Mac → SSH → WSL2 远程开发链路**：全部开发、Git、Worktree、终端与 Docker Compose 管理均在 Windows 本机执行，与 WSL 开发路径解耦。
2. **本机 Docker 配额策略**：按用户最新决定，Docker 在本机运行不以 18GB/20GB 作为阻塞性硬性失败门槛；但仍为容器保留合理观测与日志轮转实践。
3. **PATH 卫生规范（所有 Agent 必读）**：
   - 执行容器命令前，需确保 `C:\Users\FireFly\AppData\Local\Kiri\bin` 在 `PATH` 中（或直接调用 `C:\Users\FireFly\AppData\Local\Kiri\bin\docker.exe`）。
   - 执行 Node 命令前，需确保 `D:\code\nodejs` 位于 `C:\Users\FireFly\AppData\Roaming\Antigravity\bin` 之前，避免无扩展名 wrapper 拦截 `node`/`npm`。
   - 统一前置命令：
     ```powershell
     $env:PATH = "C:\Users\FireFly\AppData\Local\Kiri\bin;D:\code\nodejs;$env:PATH"
     ```
4. **Windows PowerShell 5.1 规范**：
   - 不使用 `&&`（改用 `;` 并检查 `$LASTEXITCODE` 或使用独立 Python 脚本）。
   - 宿主机与容器数据卷优先使用 Docker Named Volume，避免 NTFS 跨文件系统性能瓶颈。
