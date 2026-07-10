#!/usr/bin/env bash
set -e

REPO_URL="https://github.com/chenmo999/xianyu-auto-reply.git"
BRANCH="chenmo-main"
APP_DIR="/root/xianyu-auto-reply"
COMPOSE_FILE="docker-compose.deploy.yml"

echo "======================================"
echo " Chenmo Xianyu Auto Reply 一键部署"
echo "======================================"

if [ "$(id -u)" != "0" ]; then
  echo "请使用 root 用户执行"
  exit 1
fi

echo "1. 安装基础工具..."
if command -v apt-get >/dev/null 2>&1; then
  apt-get update -y
  apt-get install -y git curl ca-certificates openssl
elif command -v yum >/dev/null 2>&1; then
  yum install -y git curl ca-certificates openssl
fi

echo "2. 检查 Docker..."
if ! command -v docker >/dev/null 2>&1; then
  echo "未检测到 Docker，开始安装 Docker..."
  curl -fsSL https://get.docker.com | sh
  systemctl enable docker || true
  systemctl start docker || true
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose 插件不可用，请检查 Docker 版本"
  exit 1
fi

echo "3. 拉取 Chenmo 版本代码..."
if [ -d "$APP_DIR/.git" ]; then
  cd "$APP_DIR"
  git fetch origin
  git checkout "$BRANCH"
  git pull origin "$BRANCH"
else
  rm -rf "$APP_DIR"
  git clone -b "$BRANCH" "$REPO_URL" "$APP_DIR"
  cd "$APP_DIR"
fi

rand_hex() {
  openssl rand -hex 16 2>/dev/null || date +%s%N | md5sum | cut -c1-32
}

echo "4. 创建 .env 配置..."
if [ ! -f ".env" ]; then
  MYSQL_ROOT_PASSWORD="$(rand_hex)"
  MYSQL_PASSWORD="$(rand_hex)"
  JWT_SECRET_KEY="$(rand_hex)$(rand_hex)"

  cat > .env <<ENVEOF
TZ=Asia/Shanghai

MYSQL_DATABASE=xianyu_data
MYSQL_ROOT_PASSWORD=${MYSQL_ROOT_PASSWORD}
MYSQL_USER=xianyu
MYSQL_PASSWORD=${MYSQL_PASSWORD}
MYSQL_HOST=xianyu-mysql
MYSQL_PORT=3306
MYSQL_DATA_PATH=/root/xianyu_auto_reply/mysql/data

REDIS_HOST=xianyu-redis
REDIS_PORT=6379
REDIS_PASSWORD=xianyu@2026

JWT_SECRET_KEY=${JWT_SECRET_KEY}
SECRET_KEY=${JWT_SECRET_KEY}

FRONTEND_PORT=9000
BACKEND_WEB_PORT=8089
WEBSOCKET_PORT=8090
SCHEDULER_PORT=8091

DATABASE_URL=mysql+pymysql://xianyu:${MYSQL_PASSWORD}@xianyu-mysql:3306/xianyu_data?charset=utf8mb4
ENVEOF

  chmod 600 .env
  echo ".env 已自动生成"
else
  echo ".env 已存在，保留原配置"
fi

echo "5. 构建并启动容器..."
docker compose -f "$COMPOSE_FILE" --env-file .env build backend-web frontend
docker compose -f "$COMPOSE_FILE" --env-file .env up -d

echo "6. 部署完成"
echo
echo "请打开："
echo "http://服务器IP:9000"
echo
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
