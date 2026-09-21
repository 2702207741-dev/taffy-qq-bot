# 塔菲 bot 容器镜像（云端 ECS 用；本地 Windows 保持原生启动不动）。
#
# 零新依赖铁律同样适用于镜像：仅安装 pyproject.toml 声明的依赖
# （uv sync --frozen --no-dev），镜像内不装任何额外包。
#
# 构建：
#   docker build -t taffy-bot .
# 运行（推荐用 docker compose up -d，见 docker-compose.yml）：
#   docker run -d --name taffy-bot -p 127.0.0.1:8080:8080 \
#     -v "$PWD/data:/app/data" -v "$PWD/.env:/app/.env:ro" \
#     -e TZ=Asia/Shanghai taffy-bot

FROM python:3.12-slim

# 时区固定为上海（本地 Windows 与云端 ECS 行为一致）。
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# uv 官方安装脚本（固定版本，保证可复现）。
COPY --from=ghcr.io/astral-sh/uv:0.8.19 /uv /uvx /bin/

WORKDIR /app

# 先复制依赖清单，利用 Docker 层缓存避免每次改代码都重装依赖。
COPY pyproject.toml uv.lock ./

# 只装运行依赖（--no-dev：测试/静态检查工具不进镜像）。
RUN uv sync --frozen --no-dev

# 复制源码（.dockerignore 已排除 data/、.venv/、.env）。
COPY . .

# bot 监听 8080（健康检查用 /health/live）。
EXPOSE 8080

# 容器内只暴露内网端口；公网访问经宿主反向代理或仅本机。
CMD ["uv", "run", "--frozen", "--no-dev", "python", "bot.py"]
