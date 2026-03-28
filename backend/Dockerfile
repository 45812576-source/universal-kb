FROM python:3.11-slim AS base

# 系统依赖：MySQL 客户端、构建工具、音频库（funasr/whisperlivekit）
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ \
    default-libmysqlclient-dev \
    libsndfile1 ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装依赖（利用 layer cache）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY . .

# 创建必要目录
RUN mkdir -p uploads/avatars

EXPOSE 8000

# entrypoint: 先跑 migration，再启动服务
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
