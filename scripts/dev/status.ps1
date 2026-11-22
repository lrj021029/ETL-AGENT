# ==============================================================================
# ETL-Agent 平台 — 容器健康状态与资源监控脚本 (scripts/dev/status.ps1)
# 兼容环境：Windows PowerShell 5.1+
# 用途：查询 Compose 服务运行状态、Healthcheck 结果、内存/CPU 实时占用与持久化卷列表
# ==============================================================================
[CmdletBinding()]
param(
    [switch]$ShowStats
)

$ErrorActionPreference = "Stop"

$env:PATH = "C:\Users\FireFly\AppData\Local\Kiri\bin;D:\code\nodejs;$env:PATH"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $RepoRoot

$envFile = Join-Path $RepoRoot ".env"
if (-not (Test-Path $envFile)) {
    $envFile = Join-Path $RepoRoot ".env.example"
}

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " ETL-Agent 服务运行与健康检查状态" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
& docker compose --env-file $envFile ps
if ($LASTEXITCODE -ne 0) {
    throw "无法查询 docker compose ps 状态。"
}

Write-Host "`n============================================================" -ForegroundColor Cyan
Write-Host " ETL-Agent 持久化 Named Volumes 状态" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan
& docker volume ls --filter "name=etl-agent"

if ($ShowStats) {
    Write-Host "`n============================================================" -ForegroundColor Cyan
    Write-Host " 容器实时内存与 CPU 资源快照 (docker stats --no-stream)" -ForegroundColor Cyan
    Write-Host "============================================================" -ForegroundColor Cyan
    & docker stats --no-stream
}

exit 0
