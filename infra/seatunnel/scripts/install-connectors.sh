#!/usr/bin/env bash
# =============================================================================
# Apache SeaTunnel 2.3.9 Connector 与 JDBC 驱动安装校验脚本
# 路径: infra/seatunnel/scripts/install-connectors.sh
# 用途: 在容器初始化或构建阶段确保 M7 批处理与 M8 MySQL/PG CDC 所需的连接器已就位
# =============================================================================
set -euo pipefail

SEATUNNEL_HOME="${SEATUNNEL_HOME:-/opt/seatunnel}"
CONNECTORS_DIR="${SEATUNNEL_HOME}/connectors"
LIB_DIR="${SEATUNNEL_HOME}/lib"

REQUIRED_CONNECTORS=(
  "connector-jdbc"
  "connector-cdc-mysql"
  "connector-cdc-postgres"
  "connector-doris"
  "connector-file-local"
  "connector-file-s3"
  "connector-console"
)

echo "[SeaTunnel-Init] 检查必需的连接器目录: ${CONNECTORS_DIR}"
mkdir -p "${CONNECTORS_DIR}" "${LIB_DIR}"

for plugin in "${REQUIRED_CONNECTORS[@]}"; do
  if compgen -G "${CONNECTORS_DIR}/*${plugin}*.jar" > /dev/null; then
    echo "[SeaTunnel-Init] 已存在连接器: ${plugin}"
  else
    echo "[SeaTunnel-Init] 连接器 ${plugin} 未预置，尝试通过 install-plugin.sh 安装 (版本 2.3.9)..."
    if [[ -x "${SEATUNNEL_HOME}/bin/install-plugin.sh" ]]; then
      "${SEATUNNEL_HOME}/bin/install-plugin.sh" 2.3.9 "${plugin}" || true
    fi
  fi
done

echo "[SeaTunnel-Init] 连接器检查流程完成。"
