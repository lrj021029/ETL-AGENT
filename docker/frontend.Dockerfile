# =============================================================================
# ETL-Agent 前端工作台 Dockerfile (docker/frontend.Dockerfile)
# 技术栈：Node + pnpm 9.12.3 + Vue 3 + TypeScript + Vite
# 用途：构建前端静态资源并通过 Vite Preview 提供本地容器化Web服务
# =============================================================================

FROM node:22-alpine AS builder

# 启用 corepack 并锁定 pnpm 版本与 contracts/versions.lock.yaml 对齐
ENV PNPM_HOME="/pnpm"
ENV PATH="$PNPM_HOME:$PATH"
RUN corepack enable && corepack prepare pnpm@9.12.3 --activate

WORKDIR /app

# 复制包清单并安装前端依赖
COPY frontend/package.json ./
COPY frontend/pnpm-lock.yaml* ./
RUN pnpm install --frozen-lockfile=false

# 复制前端源码并执行类型检查与生产构建
COPY frontend/ ./
RUN pnpm run build

# 运行阶段：使用非特权 node 用户启动 Vite Preview 服务
FROM node:22-alpine AS runner

ENV PNPM_HOME="/pnpm"
ENV PATH="$PNPM_HOME:$PATH"
RUN apk add --no-cache curl \
    && corepack enable \
    && corepack prepare pnpm@9.12.3 --activate

WORKDIR /app

COPY --from=builder --chown=node:node /app/package.json ./package.json
COPY --from=builder --chown=node:node /app/node_modules ./node_modules
COPY --from=builder --chown=node:node /app/dist ./dist
COPY --from=builder --chown=node:node /app/vite.config.* ./

USER node

EXPOSE 5174

# 监听 0.0.0.0:5174 供宿主机 127.0.0.1:5174 映射访问；/api 与 /health 由 vite preview 代理到 ETL_API_PROXY_TARGET
CMD ["pnpm", "run", "preview", "--host", "0.0.0.0", "--port", "5174"]
