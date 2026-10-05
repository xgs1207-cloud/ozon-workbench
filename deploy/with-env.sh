#!/usr/bin/env bash
# 用服务器上的环境变量文件跑任意命令（systemd 会自己读 env 文件，手动敲命令不会）。
#
# 用法：
#   bash deploy/with-env.sh .venv/bin/python -m pipeline.oss_cos --check
#   bash deploy/with-env.sh .venv/bin/python -m pipeline.day --products products --store default
#
# 前置：/etc/ozon-workbench.env 需要让运行用户读到（安装脚本会设成 root:ubuntu 640）。
# 也可以指定别的文件：OZON_ENV_FILE=/path/to/env bash deploy/with-env.sh ...
set -u

ENV_FILE="${OZON_ENV_FILE:-/etc/ozon-workbench.env}"

if [ "$#" -eq 0 ]; then
  echo "用法：bash deploy/with-env.sh <命令> [参数...]" >&2
  exit 2
fi

if [ -f "$ENV_FILE" ]; then
  if [ -r "$ENV_FILE" ]; then
    # set -a 让文件里的赋值自动导出（文件里是 KEY=VALUE，没有 shell 逻辑执行风险）
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
  else
    echo "读不到 $ENV_FILE：检查权限（安装脚本应设为 root:ubuntu 640）；" >&2
    echo "或用 sudo：sudo -E bash deploy/with-env.sh $*" >&2
    exit 3
  fi
else
  echo "没有 $ENV_FILE：环境变量会缺失（这才正常，如果你还没建这个文件）" >&2
fi

echo "[with-env] 已载入 ${ENV_FILE}；已设置的关键变量：" >&2
for name in COS_BUCKET COS_REGION COS_KEY_PREFIX ARK_TEXT_MODEL ARK_IMAGE_MODEL APP_MODE; do
  value="${!name:-}"
  if [ -n "$value" ]; then echo "  $name=$value" >&2; fi
done
for name in COS_SECRET_ID COS_SECRET_KEY ARK_API_KEY OZON_DEFAULT_CLIENT_ID OZON_DEFAULT_API_KEY; do
  value="${!name:-}"
  if [ -n "$value" ]; then echo "  $name=<已设置>" >&2; else echo "  $name=<空>" >&2; fi
done

exec "$@"
