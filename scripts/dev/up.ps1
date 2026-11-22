# ==============================================================================
# ETL-Agent 平台 — 一键启动开发环境脚本 (scripts/dev/up.ps1)
# 兼容环境：Windows PowerShell 5.1+
# 用途：
#   1. 若不存在 .env 则从 .env.example 生成本地占位配置
#   2. 校验 compose.yaml 配置语法
#   3. 启动全部默认服务（或指定 Profile）并自动调用 init-vault.ps1 解封 Vault
# ==============================================================================
[CmdletBinding()]
param(
    [string[]]$Profiles = @(),
    [switch]$Build,
    [switch]$ValidateOnly
)

$ErrorActionPreference = "Stop"

# 注入本机 Kiri Docker 与 Node.js PATH（遵循 ENV-OVERRIDE-001）
$env:PATH = "C:\Users\FireFly\AppData\Local\Kiri\bin;D:\code\nodejs;$env:PATH"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $RepoRoot

$envPath = Join-Path $RepoRoot ".env"
$envExamplePath = Join-Path $RepoRoot ".env.example"

if (-not (Test-Path $envPath)) {
    Write-Host "[Up] 未找到 .env，正在从 .env.example 复制本地开发默认占位配置..." -ForegroundColor Yellow
    Copy-Item -Path $envExamplePath -Destination $envPath
}

Write-Host "[Up] 正在校验 compose.yaml 配置..." -ForegroundColor Cyan
& docker compose --env-file $envPath config --quiet
if ($LASTEXITCODE -ne 0) {
    throw "[Up] docker compose config 校验失败！"
}

if ($ValidateOnly) {
    Write-Host "[Up] ValidateOnly 模式：compose.yaml 校验通过，未启动容器。" -ForegroundColor Green
    exit 0
}

$composeArgs = @("compose", "--env-file", $envPath)
foreach ($p in $Profiles) {
    if (-not [string]::IsNullOrWhiteSpace($p)) {
        $composeArgs += @("--profile", $p)
    }
}
$composeArgs += @("up", "-d")
if ($Build) {
    $composeArgs += "--build"
}

Write-Host ("[Up] 正在启动 ETL-Agent 容器栈: docker {0}" -f ($composeArgs -join " ")) -ForegroundColor Cyan
& docker @composeArgs
if ($LASTEXITCODE -ne 0) {
    throw "[Up] 容器栈启动失败，请执行 scripts/dev/status.ps1 与 scripts/dev/logs.ps1 排查。"
}

# 自动执行持久化 Vault 的幂等初始化与 Unseal
$initVaultScript = Join-Path $PSScriptRoot "init-vault.ps1"
if (Test-Path $initVaultScript) {
    Write-Host "[Up] 正在执行持久化 Vault 幂等初始化与 Unseal..." -ForegroundColor Cyan
    & powershell -NoProfile -ExecutionPolicy Bypass -File $initVaultScript
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "[Up] Vault 自动初始化返回非零状态码，请检查 etl-agent-vault 容器日志。"
    }
}

Write-Host "[Up] ETL-Agent 默认服务栈已启动完成。可使用 .\scripts\dev\status.ps1 查看健康检查状态。" -ForegroundColor Green
exit 0
