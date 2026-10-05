#!/usr/bin/env bash
# 一键安装/更新 Ozon 上品自动化工作台（Ubuntu 22.04+ / Debian 12+ / 腾讯云 CVM）
#
#   sudo bash deploy/install.sh                 # 安装（幂等，可重复跑）
#   sudo bash deploy/install.sh --no-nginx      # 不动 nginx
#   sudo bash deploy/install.sh --no-apt        # 不装系统包（**服务器上已跑着别的项目时用这个**）
#
# 做四件事：
#   1) 装系统依赖（python3-venv、git、nginx、certbot）—— --no-apt 时跳过
#   2) 建用户 ozon 与目录 /opt/ozon-workbench、/var/www/ozon-images
#   3) 建 venv 并安装 requirements.txt；拉取上游契约（contracts/fetch_contracts.sh）
#   4) 装 systemd 服务与 nginx 站点（证书你可以稍后用 certbot 申请）
set -euo pipefail

APP_USER="${APP_USER:-ozon}"
APP_DIR="${APP_DIR:-/opt/ozon-workbench}"
IMAGE_ROOT="${IMAGE_ROOT:-/var/www/ozon-images}"
ENV_FILE="${ENV_FILE:-/etc/ozon-workbench.env}"
WITH_NGINX=1
WITH_APT=1
for arg in "$@"; do
  case "$arg" in
    --no-nginx) WITH_NGINX=0 ;;
    --no-apt) WITH_APT=0 ;;
    *) echo "未知参数：$arg" >&2; exit 2 ;;
  esac
done

if [ "$(id -u)" != "0" ]; then
  echo "请用 root 运行：sudo bash deploy/install.sh" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "==> 1/5 系统依赖"
if [ "$WITH_APT" = "1" ]; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y python3 python3-venv python3-pip git curl ca-certificates
  if [ "$WITH_NGINX" = "1" ]; then
    # 注意：nginx 已在跑别的站点时，升级可能重启它。共享服务器请加 --no-apt
    apt-get install -y nginx
    # certbot 用于申请免费 HTTPS 证书（Ozon 只能抓 https 图片）
    apt-get install -y certbot python3-certbot-nginx || true
  fi
else
  echo "    --no-apt：跳过系统包安装（假定 python3/venv/git 已就绪）"
  if ! python3 -c "import venv" 2>/dev/null; then
    echo "    ✗ 缺少 python3-venv：请手动 apt-get install -y python3-venv" >&2
    exit 1
  fi
fi

echo "==> 2/5 建用户与目录"
if ! id -u "$APP_USER" >/dev/null 2>&1; then
  useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
fi
mkdir -p "$APP_DIR" "$IMAGE_ROOT" "$APP_DIR/config"
chown -R "$APP_USER:$APP_USER" "$APP_DIR" "$IMAGE_ROOT"
chmod 755 "$IMAGE_ROOT"
# 共享服务器（--no-apt）常用 ubuntu 用户跑服务，把目录交给它，避免权限问题
if id -u ubuntu >/dev/null 2>&1; then
  chown -R ubuntu:ubuntu "$APP_DIR" "$IMAGE_ROOT" 2>/dev/null || true
fi

echo "==> 3/5 同步代码到 $APP_DIR"
if [ "$SOURCE_DIR" != "$APP_DIR" ]; then
  # 保留运行期产物（products/ keyword-library/ output/ config/），只覆盖代码
  for keep in products keyword-library output config .venv; do
    mkdir -p "$APP_DIR/$keep"
  done
  tar -C "$SOURCE_DIR" \
      --exclude='./.git' --exclude='./.venv' --exclude='./__pycache__' \
      --exclude='./products' --exclude='./keyword-library' --exclude='./output' \
      --exclude='./config' --exclude='./*.xlsx' \
      -cf - . | tar -C "$APP_DIR" -xf -
  chown -R "$APP_USER:$APP_USER" "$APP_DIR"
fi

echo "==> 4/5 建 venv 并安装依赖"
if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
  sudo -u "$APP_USER" python3 -m venv "$APP_DIR/.venv"
fi
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

# 上游契约不进仓库（许可原因），在服务器上现拉
if [ ! -d "$APP_DIR/contracts/original" ] || [ -z "$(ls -A "$APP_DIR/contracts/original" 2>/dev/null || true)" ]; then
  echo "    拉取上游契约 ……"
  sudo -u "$APP_USER" env PYTHON_BIN="$APP_DIR/.venv/bin/python" \
    bash "$APP_DIR/contracts/fetch_contracts.sh" || \
    echo "    ⚠️ 契约拉取失败（网络受限）：稍后重跑 bash contracts/fetch_contracts.sh"
fi

echo "==> 5/5 装服务与站点"
if [ ! -f "$ENV_FILE" ]; then
  install -m 600 -o root -g root "$APP_DIR/deploy/ozon-workbench.env.example" "$ENV_FILE"
  echo "    已生成 $ENV_FILE（请填密钥后 systemctl restart ozon-workbench-api）"
fi
install -m 644 "$APP_DIR/deploy/ozon-workbench-api.service" /etc/systemd/system/ozon-workbench-api.service
systemctl daemon-reload
systemctl enable ozon-workbench-api.service

if [ "$WITH_NGINX" = "1" ]; then
  install -m 644 "$APP_DIR/deploy/nginx-ozon-images.conf" /etc/nginx/conf.d/ozon-images.conf
  nginx -t && systemctl reload nginx
fi

cat <<'EOF'

安装完成。接下来：

1) 填密钥（编辑 /etc/ozon-workbench.env）：
     ARK_API_KEY / ARK_TEXT_MODEL / OZON_*_CLIENT_ID / OZON_*_API_KEY
   然后：sudo systemctl restart ozon-workbench-api

2) 配店铺注册表（只写环境变量名，不写密钥）：
     sudo -u ozon cp /opt/ozon-workbench/deploy/shops.example.json /opt/ozon-workbench/config/shops.json
     sudo -u ozon nano /opt/ozon-workbench/config/shops.json

3) 申请 HTTPS 证书（Ozon 只能抓 https 图片）：
     sudo certbot --nginx -d img.example.com
     （把 nginx-ozon-images.conf 里的 img.example.com 换成你的域名与证书路径）

4) 自检：
     sudo -u ozon /opt/ozon-workbench/.venv/bin/python -m unittest discover -s tests -p "test*.py"
     sudo -u ozon /opt/ozon-workbench/.venv/bin/python -m pipeline.doctor --products-root products

5) 服务地址：http://127.0.0.1:8766/health（外部访问请再加反向代理 + 鉴权）
EOF
