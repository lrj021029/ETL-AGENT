# ==============================================================================
# ETL-Agent 平台 — Windows 本机开发环境预检脚本 (scripts/dev/env-check.ps1)
# 兼容环境：Windows PowerShell 5.1+
# 用途：检查宿主机操作系统、内存/磁盘余量、Kiri Docker Engine、Compose 配置、
#       Python 3.12、Node/pnpm 及 Git 安全忽略规则。
# ==============================================================================
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

# 统一注入本机 Kiri Docker 与 Node.js 工具链 PATH（遵循 ENV-OVERRIDE-001）
$env:PATH = "C:\Users\FireFly\AppData\Local\Kiri\bin;D:\code\nodejs;$env:PATH"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $RepoRoot

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " ETL-Agent Windows 本机开发环境预检 (ENV-OVERRIDE-001)" -ForegroundColor Cyan
Write-Host "============================================================" -ForegroundColor Cyan

# 1. 检查操作系统与物理内存
$osInfo = Get-CimInstance Win32_OperatingSystem
$totalRamGb = [math]::Round($osInfo.TotalVisibleMemorySize / 1MB, 2)
$freeRamGb = [math]::Round($osInfo.FreePhysicalMemory / 1MB, 2)
Write-Host ("[OS] {0} ({1}) | 物理内存: {2} GB (当前可用: {3} GB)" -f $osInfo.Caption, $osInfo.Version, $totalRamGb, $freeRamGb) -ForegroundColor Green

# 2. 检查磁盘可用空间
$drives = Get-PSDrive -PSProvider FileSystem | Where-Object { $_.Name -in @("C", "D") }
foreach ($d in $drives) {
    $freeGb = [math]::Round($d.Free / 1GB, 2)
    Write-Host ("[Disk] 磁盘 {0}: 可用空间 {1} GB" -f $d.Name, $freeGb) -ForegroundColor Green
}

# 3. 检查 PowerShell 版本
$psVer = $PSVersionTable.PSVersion.ToString()
Write-Host ("[PowerShell] 版本: {0}" -f $psVer) -ForegroundColor Green

# 4. 检查 Git 版本
$gitOut = & git --version
if ($LASTEXITCODE -ne 0) {
    throw "Git 检查失败，请确认 git 已安装并在 PATH 中。"
}
Write-Host ("[Git] {0}" -f $gitOut) -ForegroundColor Green

# 5. 检查 Python 版本
$pyOut = & python --version 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "Python 检查失败，请确认 Python 3.12 已安装。"
}
Write-Host ("[Python] {0}" -f $pyOut) -ForegroundColor Green

# 6. 检查 Node 与 pnpm 版本
$nodeOut = & node --version
if ($LASTEXITCODE -ne 0) {
    throw "Node.js 检查失败，请确认 D:\code\nodejs 已在 PATH 最前方。"
}
$pnpmOut = & pnpm --version
if ($LASTEXITCODE -ne 0) {
    throw "pnpm 检查失败，请确认 pnpm 已安装。"
}
Write-Host ("[Node/pnpm] Node {0} | pnpm {1}" -f $nodeOut, $pnpmOut) -ForegroundColor Green

# 7. 检查 Docker Client 与 Server Engine
Write-Host "[Docker] 正在验证 Docker Client 与 Linux Container Engine..." -ForegroundColor Yellow
$dockerVer = & docker version --format "Client={{.Client.Version}} | Server={{.Server.Version}} ({{.Server.Os}}/{{.Server.Arch}})"
if ($LASTEXITCODE -ne 0) {
    throw "Docker Engine 未就绪或无法连接，请检查 Kiri Docker Engine 运行状态。"
}
Write-Host ("[Docker] {0}" -f $dockerVer) -ForegroundColor Green

# 8. 检查 Docker Compose 版本与静态配置校验
$composeVer = & docker compose version
if ($LASTEXITCODE -ne 0) {
    throw "Docker Compose 插件不可用。"
}
Write-Host ("[Compose] {0}" -f $composeVer) -ForegroundColor Green

$envFile = Join-Path $RepoRoot ".env.example"
if (-not (Test-Path $envFile)) {
    throw "缺少 .env.example 文件: $envFile"
}

& docker compose --env-file $envFile config --quiet
if ($LASTEXITCODE -ne 0) {
    throw "compose.yaml 静态语法与依赖校验失败 (docker compose config --quiet)！"
}
Write-Host "[Compose] compose.yaml 静态配置校验通过 (ExitCode=0)。" -ForegroundColor Green

# 9. 检查 .gitignore 敏感路径保护规则
$gitignorePath = Join-Path $RepoRoot ".gitignore"
$gitignoreText = Get-Content -Path $gitignorePath -Raw
foreach ($pattern in @(".env", "infra/vault/init/")) {
    if ($gitignoreText -notmatch [regex]::Escape($pattern)) {
        throw ".gitignore 缺少关键密钥保护规则: $pattern"
    }
}
Write-Host "[Security] .gitignore 已正确保护 .env 与 infra/vault/init/ 本地密钥目录。" -ForegroundColor Green

Write-Host "============================================================" -ForegroundColor Cyan
Write-Host " 预检结果: PASS (所有工具链、容器引擎与 Compose 配置均正常)" -ForegroundColor Green
Write-Host "============================================================" -ForegroundColor Cyan
exit 0
