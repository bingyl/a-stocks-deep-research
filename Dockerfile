# A股深研 API — 生产镜像（Linux；默认 SelectorEventLoop，无需 Windows 补丁）
# 构建: docker build -t a-stock-api .
# 运行: docker run --rm -p 8000:8000 --env-file .env ^
#          -v "%cd%/data:/app/data" -v "%cd%/logs:/app/logs" a-stock-api
# Linux/macOS:
#   docker run --rm -p 8000:8000 --env-file .env \
#     -v "$(pwd)/data:/app/data" -v "$(pwd)/logs:/app/logs" a-stock-api

FROM python:3.12-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # 容器内数据/日志默认路径（可用卷覆盖）
    VECTOR_CHROMA_DIR=/app/data/vector_chroma

WORKDIR /app

# curl：健康检查；部分 wheel 构建偶发需要的基础工具尽量少装
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# 先装依赖，利用层缓存
COPY requirements.txt requirements-vector.txt ./
RUN pip install --upgrade pip \
    && pip install -r requirements.txt -r requirements-vector.txt

# 应用代码（不含 .env / data / logs）
COPY main.py ./
COPY app ./app
COPY static ./static

RUN mkdir -p /app/data /app/logs \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/docs >/dev/null || exit 1

# 勿用 main.py 的 127.0.0.1；容器内需监听 0.0.0.0
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
