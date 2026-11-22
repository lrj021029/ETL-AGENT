# =============================================================================
# ETL-Agent 控制面后端 Dockerfile (docker/backend.Dockerfile)
# 适用服务：api (FastAPI)、celery-worker、celery-beat、outbox-dispatcher
# 基础镜像：python:3.12-slim（锁定 Python 3.12 运行时基线）
# =============================================================================

FROM python:3.12-slim AS base

# 设置标准 Python 容器环境变量，禁止生成 .pyc 并开启实时日志刷新
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/workspace/backend

WORKDIR /workspace

# 安装必要的系统基础工具（curl 用于容器健康检查，libpq5 用于 PostgreSQL 驱动）
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
        libpq5 \
    && rm -rf /var/lib/apt/lists/*

# 创建非特权运行账号，遵守最小权限容器安全原则
RUN groupadd --gid 10001 etluser \
    && useradd --uid 10001 --gid etluser --shell /bin/bash --create-home etluser

# 优先复制 pyproject.toml 以利用 Docker 构建层缓存安装依赖
COPY backend/pyproject.toml /workspace/backend/pyproject.toml
COPY contracts /workspace/contracts

WORKDIR /workspace/backend

# 安装项目声明的 Python 依赖包
RUN pip install --upgrade pip setuptools wheel \
    && pip install .

# 复制后端业务代码与数据库迁移脚本
COPY backend /workspace/backend

# 调整工作目录所有权并切换为非 root 用户
RUN chown -R etluser:etluser /workspace
USER etluser

EXPOSE 8000

# 默认启动 FastAPI 控制面服务（Worker 与 Outbox Dispatcher 在 compose.yaml 中覆盖 command）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
