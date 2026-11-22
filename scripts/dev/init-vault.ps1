# ==============================================================================
# ETL-Agent 平台 — HashiCorp Vault 持久化模式幂等初始化与 Unseal 脚本
# 路径: scripts/dev/init-vault.ps1
# 规范依据: ADR-001 & DEC-003
# 核心安全约束:
#   1. 若 Vault 尚未初始化，执行初始化并将 Unseal Keys 与 Root Token 仅写入被
#      .gitignore 忽略的本地文件 `infra/vault/init/vault-init.json`。
#   2. 严禁将任何 Unseal Key 或 Root Token 打印到标准输出或日志！
#   3. 幂等执行 Unseal、开启 `kv-v2` (路径 `secret/`)、开启 `transit` 引擎，
#      并幂等创建 Ed25519 签名密钥 `etl-agent-capability` 与 `etl-agent-evidence`。
# ==============================================================================
[CmdletBinding()]
param(
    [string]$VaultAddr = "http://127.0.0.1:8200",
    [string]$InitFilePath = "",
    [int]$MaxWaitSeconds = 30
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
if ([string]::IsNullOrWhiteSpace($InitFilePath)) {
    $InitFilePath = Join-Path $RepoRoot "infra\vault\init\vault-init.json"
}

$InitDir = Split-Path -Parent $InitFilePath
if (-not (Test-Path $InitDir)) {
    New-Item -ItemType Directory -Force -Path $InitDir | Out-Null
}

Write-Host "[Vault-Init] 正在检查持久化 Vault 服务状态 ($VaultAddr) ..." -ForegroundColor Cyan

# 1. 等待 Vault HTTP 接口可达
$elapsed = 0
$initStatusResp = $null
while ($elapsed -lt $MaxWaitSeconds) {
    try {
        $initStatusResp = Invoke-RestMethod -Uri "$VaultAddr/v1/sys/init" -Method Get -TimeoutSec 3
        break
    } catch {
        Start-Sleep -Seconds 2
        $elapsed += 2
    }
}

if ($null -eq $initStatusResp) {
    throw "[Vault-Init] 无法连接到 Vault ($VaultAddr)，请先确认 etl-agent-vault 容器已启动。"
}

# 2. 若尚未初始化，则执行首次初始化并落盘至 gitignored 目录（绝不打印密钥到终端）
if (-not [bool]$initStatusResp.initialized) {
    Write-Host "[Vault-Init] 检测到 Vault 尚未初始化，正在执行持久化初始化 (secret_shares=1, secret_threshold=1) ..." -ForegroundColor Yellow
    $initPayload = @{
        secret_shares    = 1
        secret_threshold = 1
    } | ConvertTo-Json -Depth 4

    $initResult = Invoke-RestMethod -Uri "$VaultAddr/v1/sys/init" -Method Put -Body $initPayload -ContentType "application/json"
    if ($null -eq $initResult.root_token -or $null -eq $initResult.keys_base64) {
        throw "[Vault-Init] Vault 初始化响应异常，未返回有效结构。"
    }

    # 将初始化产物严格写入本地 gitignored 文件，禁止回显
    $serialized = $initResult | ConvertTo-Json -Depth 6
    Set-Content -Path $InitFilePath -Value $serialized -Encoding UTF8 -NoNewline
    Write-Host "[Vault-Init] 初始化完成，凭据已安全保存至本地受保护路径 (未输出至终端)。" -ForegroundColor Green
} else {
    Write-Host "[Vault-Init] Vault 处于已初始化状态。" -ForegroundColor Green
}

# 3. 读取本地初始化凭据文件
if (-not (Test-Path $InitFilePath)) {
    throw "[Vault-Init] Vault 已初始化，但未找到本地凭据文件: $InitFilePath"
}

$vaultCreds = Get-Content -Path $InitFilePath -Raw -Encoding UTF8 | ConvertFrom-Json
$unsealKey = $vaultCreds.keys_base64[0]
if ([string]::IsNullOrWhiteSpace($unsealKey) -and $vaultCreds.keys) {
    $unsealKey = $vaultCreds.keys[0]
}
$rootToken = $vaultCreds.root_token

if ([string]::IsNullOrWhiteSpace($unsealKey) -or [string]::IsNullOrWhiteSpace($rootToken)) {
    throw "[Vault-Init] 本地凭据文件结构不完整，缺少 unseal key 或 root_token。"
}

# 4. 检查 Seal 状态并执行幂等 Unseal
$sealStatus = Invoke-RestMethod -Uri "$VaultAddr/v1/sys/seal-status" -Method Get
if ([bool]$sealStatus.sealed) {
    Write-Host "[Vault-Init] Vault 当前处于 Sealed 状态，正在执行自动 Unseal ..." -ForegroundColor Yellow
    $unsealBody = @{
        key = $unsealKey
    } | ConvertTo-Json

    $unsealResp = Invoke-RestMethod -Uri "$VaultAddr/v1/sys/unseal" -Method Put -Body $unsealBody -ContentType "application/json"
    if ([bool]$unsealResp.sealed) {
        throw "[Vault-Init] Vault Unseal 后仍处于 Sealed 状态，请检查阈值配置。"
    }
    Write-Host "[Vault-Init] Vault 已成功 Unseal。" -ForegroundColor Green
} else {
    Write-Host "[Vault-Init] Vault 当前已处于 Unsealed 就绪状态。" -ForegroundColor Green
}

$authHeaders = @{
    "X-Vault-Token" = $rootToken
}

# 5. 幂等开启 KV v2 秘密引擎 (挂载路径: secret/)
$mounts = Invoke-RestMethod -Uri "$VaultAddr/v1/sys/mounts" -Method Get -Headers $authHeaders
$hasSecretMount = $false
if ($null -ne $mounts -and $null -ne $mounts.PSObject.Properties["secret/"]) {
    $hasSecretMount = $true
} elseif ($null -ne $mounts.data -and $null -ne $mounts.data.PSObject.Properties["secret/"]) {
    $hasSecretMount = $true
}

if (-not $hasSecretMount) {
    Write-Host "[Vault-Init] 正在挂载 kv-v2 引擎至 secret/ ..." -ForegroundColor Yellow
    $kvBody = @{
        type    = "kv"
        options = @{
            version = "2"
        }
    } | ConvertTo-Json -Depth 4
    Invoke-RestMethod -Uri "$VaultAddr/v1/sys/mounts/secret" -Method Post -Headers $authHeaders -Body $kvBody -ContentType "application/json" | Out-Null
    Write-Host "[Vault-Init] kv-v2 (secret/) 挂载成功。" -ForegroundColor Green
} else {
    Write-Host "[Vault-Init] kv-v2 (secret/) 已挂载。" -ForegroundColor Green
}

# 6. 幂等开启 Transit 密码学引擎 (挂载路径: transit/)
$hasTransitMount = $false
if ($null -ne $mounts -and $null -ne $mounts.PSObject.Properties["transit/"]) {
    $hasTransitMount = $true
} elseif ($null -ne $mounts.data -and $null -ne $mounts.data.PSObject.Properties["transit/"]) {
    $hasTransitMount = $true
}

if (-not $hasTransitMount) {
    Write-Host "[Vault-Init] 正在挂载 transit 引擎至 transit/ ..." -ForegroundColor Yellow
    $transitBody = @{
        type = "transit"
    } | ConvertTo-Json
    Invoke-RestMethod -Uri "$VaultAddr/v1/sys/mounts/transit" -Method Post -Headers $authHeaders -Body $transitBody -ContentType "application/json" | Out-Null
    Write-Host "[Vault-Init] transit (transit/) 挂载成功。" -ForegroundColor Green
} else {
    Write-Host "[Vault-Init] transit (transit/) 已挂载。" -ForegroundColor Green
}

# 7. 幂等创建 Ed25519 密钥: etl-agent-capability 与 etl-agent-evidence
$requiredEd25519Keys = @("etl-agent-capability", "etl-agent-evidence")
foreach ($keyName in $requiredEd25519Keys) {
    $keyExists = $false
    try {
        $keyInfo = Invoke-RestMethod -Uri "$VaultAddr/v1/transit/keys/$keyName" -Method Get -Headers $authHeaders
        if ($null -ne $keyInfo) {
            $keyExists = $true
        }
    } catch {
        $keyExists = $false
    }

    if (-not $keyExists) {
        Write-Host ("[Vault-Init] 正在创建 Transit Ed25519 签名密钥: {0} ..." -f $keyName) -ForegroundColor Yellow
        $keyBody = @{
            type = "ed25519"
        } | ConvertTo-Json
        Invoke-RestMethod -Uri "$VaultAddr/v1/transit/keys/$keyName" -Method Post -Headers $authHeaders -Body $keyBody -ContentType "application/json" | Out-Null
        Write-Host ("[Vault-Init] Ed25519 密钥 {0} 创建完成。" -f $keyName) -ForegroundColor Green
    } else {
        Write-Host ("[Vault-Init] Ed25519 密钥 {0} 已存在且就绪。" -f $keyName) -ForegroundColor Green
    }
}

# 清理内存中的敏感变量
$unsealKey = $null
$rootToken = $null
$authHeaders = $null

Write-Host "[Vault-Init] Vault 持久化存储、kv-v2 (secret/) 及 Transit Ed25519 签名密钥全部就绪！" -ForegroundColor Green
exit 0
