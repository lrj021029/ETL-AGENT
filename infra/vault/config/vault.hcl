# =============================================================================
# HashiCorp Vault 1.18 持久化模式配置文件 (infra/vault/config/vault.hcl)
# 遵循 ADR-001 & DEC-003：
#   - 默认采用 file 持久化存储后端，挂载至 Docker Named Volume (vault_data -> /vault/file)
#   - 由 scripts/dev/init-vault.ps1 幂等执行初始化、Unseal、挂载 kv-v2 (secret/)
#     与 transit 引擎（Ed25519 密钥 etl-agent-capability 与 etl-agent-evidence）
#   - 严禁将 unseal keys 或 root token 提交至代码库
# =============================================================================

ui = true
disable_mlock = true

storage "file" {
  path = "/vault/file"
}

listener "tcp" {
  address     = "0.0.0.0:8200"
  tls_disable = 1
}

api_addr = "http://vault:8200"
default_lease_ttl = "168h"
max_lease_ttl = "720h"
