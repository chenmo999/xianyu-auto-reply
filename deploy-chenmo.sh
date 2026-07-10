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

load_env_value() {
  awk -F= -v key="$1" '$1==key {sub(/^[^=]*=/,""); gsub(/\r|"/,""); print; exit}' .env
}

DB_NAME="$(load_env_value MYSQL_DATABASE)"
MYSQL_ROOT_PASSWORD="$(load_env_value MYSQL_ROOT_PASSWORD)"

echo "6. 等待 MySQL 就绪..."
MYSQL_READY=0
for i in $(seq 1 60); do
  if docker exec xianyu-mysql mysqladmin ping -uroot -p"$MYSQL_ROOT_PASSWORD" --silent >/dev/null 2>&1; then
    MYSQL_READY=1
    break
  fi
  sleep 2
done

if [ "$MYSQL_READY" != "1" ]; then
  echo "MySQL 启动超时，请检查 xianyu-mysql 容器日志"
  docker logs --tail=100 xianyu-mysql || true
  exit 1
fi

echo "7. 等待数据库表初始化..."
ACCOUNT_TABLE_READY=0
for i in $(seq 1 60); do
  TABLE_FOUND="$(docker exec -i xianyu-mysql mysql -N -s -uroot -p"$MYSQL_ROOT_PASSWORD" "$DB_NAME" -e "SHOW TABLES LIKE 'xy_accounts';" 2>/dev/null | tr -d '\r')"
  if [ "$TABLE_FOUND" = "xy_accounts" ]; then
    ACCOUNT_TABLE_READY=1
    break
  fi
  sleep 2
done

if [ "$ACCOUNT_TABLE_READY" != "1" ]; then
  echo "未检测到 xy_accounts 表，数据库初始化可能失败"
  docker logs --tail=100 xianyu-backend-web || true
  exit 1
fi

ensure_mysql_column() {
  TABLE_NAME="$1"
  COLUMN_NAME="$2"
  COLUMN_DEF="$3"

  EXISTS="$(docker exec -i xianyu-mysql mysql -N -s -uroot -p"$MYSQL_ROOT_PASSWORD" "$DB_NAME" -e "SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA='$DB_NAME' AND TABLE_NAME='$TABLE_NAME' AND COLUMN_NAME='$COLUMN_NAME';" 2>/dev/null | tr -d '\r')"

  if [ "$EXISTS" = "1" ]; then
    echo "字段已存在：$TABLE_NAME.$COLUMN_NAME"
  else
    echo "新增字段：$TABLE_NAME.$COLUMN_NAME"
    docker exec -i xianyu-mysql mysql -uroot -p"$MYSQL_ROOT_PASSWORD" "$DB_NAME" -e "ALTER TABLE $TABLE_NAME ADD COLUMN $COLUMN_DEF;"
  fi
}

echo "8. 自动升级 Chenmo 数据库字段..."
ensure_mysql_column "xy_accounts" "category" "category VARCHAR(50) NOT NULL DEFAULT '未分类' COMMENT '账号分类' AFTER status"
ensure_mysql_column "xy_accounts" "sort_order" "sort_order INT NOT NULL DEFAULT 0 COMMENT '账号排序' AFTER category"

echo "数据库字段检查完成"

echo "9. 部署完成"
echo
echo "请打开："
echo "http://服务器IP:9000"
echo
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
