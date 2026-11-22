# ==============================================================================
# ETL-Agent 平台 — 安全停止容器栈脚本 (scripts/dev/down.ps1)
# 兼容环境：Windows PowerShell 5.1+
# 核心安全约束：
#   严禁传递 -v / --volumes 或执行任何删除持久化 Docker Named Volume 的操作！
#   停止容器后，PostgreSQL、Vault、MinIO、Doris、SeaTunnel 等数据卷完整保留。
# ==============================================================================
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$env:PATH = "C:\Users\FireFly\AppData\Local\Kiri\bin;D:\code\nodejs;$env:PATH"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $RepoRoot

$envFile = Join-Path $RepoRoot ".env"
if (-not (Test-Path $envFile)) {
    $envFile = Join-Path $RepoRoot ".env.example"
}

Write-Host "[Down] 正在停止并移除 ETL-Agent 容器（保留全部持久化 Named Volumes 数据卷）..." -ForegroundColor Cyan
& docker compose --env-file $envFile down --remove-orphans
if ($LASTEXITCODE -ne 0) {
    throw "[Down] docker compose down 执行失败！"
}

Write-Host "[Down] 容器栈已安全停止。所有持久化 Volume (postgres_data, vault_data, minio_data, doris_* 等) 均已完整保留。" -ForegroundColor Green
exit 0
