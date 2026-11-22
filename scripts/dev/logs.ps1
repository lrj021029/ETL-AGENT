# ==============================================================================
# ETL-Agent 平台 — 容器日志查看辅助脚本 (scripts/dev/logs.ps1)
# 兼容环境：Windows PowerShell 5.1+
# 用途：按服务名查看最近 N 行容器日志或实时跟踪日志流
# ==============================================================================
[CmdletBinding()]
param(
    [string]$Service = "",
    [int]$Tail = 100,
    [switch]$Follow
)

$ErrorActionPreference = "Stop"

$env:PATH = "C:\Users\FireFly\AppData\Local\Kiri\bin;D:\code\nodejs;$env:PATH"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $RepoRoot

$envFile = Join-Path $RepoRoot ".env"
if (-not (Test-Path $envFile)) {
    $envFile = Join-Path $RepoRoot ".env.example"
}

$logArgs = @("compose", "--env-file", $envFile, "logs", "--tail", $Tail.ToString())
if ($Follow) {
    $logArgs += "-f"
}
if (-not [string]::IsNullOrWhiteSpace($Service)) {
    $logArgs += $Service
}

& docker @logArgs
exit $LASTEXITCODE
