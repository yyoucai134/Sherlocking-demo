# Sherlocking Dockerfile — 深度研究 Agent 容器镜像
#
# 构建: docker build -t sherlocking .
# 运行: docker run -it --rm sherlocking          (TUI 交互)
#        docker run --rm sherlocking run "问题"   (单次研究)
#
# 镜像包含: Python 3.12 + 项目代码 + 全部依赖 + Node.js 20（MCP stdio + pi agent）
# 挂载卷: /app/.sherlocking（运行时数据） /app/skills（用户 skill）

FROM python:3.12-slim AS base

# 系统依赖：git（skill clone）+ curl + Node.js 20（MCP stdio server + pi coding agent）
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Docker CLI（仅客户端，不装 daemon）：
# Web 容器通过挂载的 /var/run/docker.sock 在宿主机上创建/销毁代码执行沙箱（DooD 模式）。
# 用官方静态二进制，避免添加 apt 源；x86_64/arm64 自动适配。
RUN DOCKER_CLI_VERSION=27.3.1 \
    && arch="$(dpkg --print-architecture)" \
    && curl -fsSL "https://download.docker.com/linux/static/stable/${arch}/docker-${DOCKER_CLI_VERSION}.tgz" \
       | tar -xz -C /tmp \
    && mv /tmp/docker/docker /usr/local/bin/docker \
    && chmod +x /usr/local/bin/docker \
    && rm -rf /tmp/docker

WORKDIR /app

# 先复制依赖描述（利用 Docker layer cache）
COPY pyproject.toml ./

# 创建 stub 包目录骗过 editable install（pip install -e 要求源码目录存在）
# 这样 pip install 层只在 pyproject.toml 变化时才重建，代码改动不会触发重装
RUN mkdir -p sherlocking && touch sherlocking/__init__.py

# 安装项目依赖（editable + dev 测试工具）
# pyyaml 已在 pyproject.toml dependencies 中（memory store 需要）
# optional provider 包按需安装：pip install -e ".[anthropic,gemini,ollama]"
RUN pip install --no-cache-dir -e ".[dev]"

# 最后复制真正的源码（代码改动只触发这一层重建，几秒完成）
COPY sherlocking/ sherlocking/

# 运行时数据卷（.sherlocking: DB/logs/artifacts/memory，skills: 用户 skill）
VOLUME ["/app/.sherlocking", "/app/skills"]

# 默认环境变量（可被 -e 或 .env 覆盖）
# 容器内默认 Local sandbox（无需 DinD）；如需 Docker 隔离挂载 docker.sock
ENV SHERLOCKING_SKILL_DB_PATH=/app/.sherlocking/skills.db \
    SHERLOCKING_MCP_CONFIG_PATH=/app/.sherlocking/mcp_servers.yaml \
    SHERLOCKING_MEMORY_STORAGE_PATH=/app/.sherlocking/memory \
    SHERLOCKING_MEMORY_USE=default \
    SHERLOCKING_MEMORY_PHASE2_ENABLED=true \
    SHERLOCKING_MULTIAGENT_ENABLED=true \
    SHERLOCKING_SANDBOX_EXECUTOR=local \
    PYTHONUNBUFFERED=1

# 入口：sherlocking 命令
ENTRYPOINT ["sherlocking"]
CMD []
