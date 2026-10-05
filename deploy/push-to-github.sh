#!/usr/bin/env bash
# 从这台服务器把仓库推到 GitHub（本机连不上 github.com 时用这个）。
#
# 背景：国内网络常出现「api.github.com 通、github.com 被重置」，于是 gh 的设备码登录和
# git push 都会失败。服务器（香港/海外）能直连 github.com，所以从服务器推最省事。
#
# 用法（在服务器上）：
#   1) 先把 token 放进一个临时文件（600），别写进命令行：
#        printf '%s' 'gho_xxx' > /tmp/.gh-token && chmod 600 /tmp/.gh-token
#      （或用 `gh auth token` 从已登录的机器上取）
#   2) 把仓库打包传上来（含 .git 才有历史）：
#        # 本机：tar -czf repo.tar.gz -C <父目录> <仓库目录>  &&  scp repo.tar.gz 服务器:/tmp/
#   3) bash deploy/push-to-github.sh /tmp/.gh-token /tmp/repo.tar.gz [<仓库目录名>] [<owner/repo>]
#
# 脚本做的事：解包 → 设置 remote → 用环境变量 + 临时 credential helper 推送 → 确认远端 HEAD → 清理 token。
set -u

TOKEN_FILE="${1:-/tmp/.gh-token}"
REPO_TAR="${2:-/tmp/repo.tar.gz}"
REPO_DIR="${3:-ozon-workbench}"
SLUG="${4:-xgs1207-cloud/ozon-workbench}"

if [ ! -f "$TOKEN_FILE" ]; then echo "缺少 token 文件：$TOKEN_FILE"; exit 2; fi
if [ ! -f "$REPO_TAR" ]; then echo "缺少仓库包：$REPO_TAR"; exit 2; fi
if ! command -v git >/dev/null 2>&1; then
  echo "安装 git ..."; sudo -n apt-get install -y -q git >/dev/null 2>&1 || { echo "装 git 失败"; exit 3; }
fi

WORK="$(mktemp -d)"
tar -xzf "$REPO_TAR" -C "$WORK" 2>/dev/null || { echo "解包失败"; exit 4; }
cd "$WORK/$REPO_DIR" || { echo "包里没有 $REPO_DIR"; exit 5; }

git config user.name "xgs1207-cloud"
git config user.email "xgs1207-cloud@users.noreply.github.com"
echo "本地 HEAD: $(git rev-parse --short HEAD) | 提交数: $(git rev-list --count HEAD) | 分支: $(git branch --show-current)"

git remote remove origin 2>/dev/null || true
git remote add origin "https://github.com/${SLUG}.git"

export GH_TOKEN="$(tr -d '\r\n' < "$TOKEN_FILE")"
HELPER='!f() { echo username=x-access-token; echo password=$GH_TOKEN; }; f'

echo "开始推送 ..."
git -c credential.helper="$HELPER" push -u origin "$(git branch --show-current)" 2>&1 | tail -5
STATUS=${PIPESTATUS[0]}

echo "--- 远端确认 ---"
git -c credential.helper="$HELPER" ls-remote origin 2>/dev/null | head -3
echo "本地  HEAD: $(git rev-parse HEAD)"

cd / && rm -rf "$WORK"
rm -f "$TOKEN_FILE"
echo "已清理临时目录与 token 文件（push 退出码 $STATUS）"
exit "$STATUS"
