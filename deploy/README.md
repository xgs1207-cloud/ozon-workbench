# 部署到腾讯云服务器（CVM）—— 从零到能跑

> 目标：工作台跑在**你的服务器**上，生成的图片存在**你的服务器**上并通过 https 给 Ozon 抓取。
> 本文里的 `/opt/ozon-workbench`、`/var/www/ozon-images`、`img.example.com` 都可以改，改的时候整篇保持一致。

---

## 0. 先确认三件事

| 需要 | 说明 |
|---|---|
| 服务器 | 腾讯云 CVM，Ubuntu 22.04 / 24.04 或 Debian 12（CentOS 系也能跑，命令换成 `dnf` 即可） |
| 域名（**必需**） | Ozon 只能抓 **https** 图片，而我们的上传门禁也只接受 https。用域名 + Let's Encrypt 免费证书，或改用腾讯云 COS 的 https 域名 |
| 密钥 | 火山方舟 `ARK_API_KEY`（文本 + 生图共用）、Ozon 店铺 `Client-Id` / `Api-Key` |

> 没有域名怎么办？两个选择：① 在腾讯云买一个便宜域名并解析到本机 IP（备案看情况，仅图片抓取通常用海外/香港节点更简单）；
> ② 用腾讯云 COS 存图片（自带 https 域名），此时把 `pipeline.oss_local` 换成 COS 上传即可（映射文件格式一样）。

---

## 1. 上传代码

**方式 A：从 GitHub（推荐，装了 gh 之后）**

```bash
sudo apt-get update && sudo apt-get install -y git
git clone https://github.com/xgs1207-cloud/ozon-workbench.git /opt/ozon-workbench
```

**方式 B：从本机直接推（不经过 GitHub）**

```powershell
# 在你自己的 Windows 上执行（把 user@1.2.3.4 换成你的服务器）
scp -r E:\抖音自动化项目\ozon-workbench user@1.2.3.4:/tmp/ozon-workbench
```

```bash
sudo mv /tmp/ozon-workbench /opt/ozon-workbench
```

---

## 2. 一键安装（幂等，可重复跑）

```bash
cd /opt/ozon-workbench
sudo bash deploy/install.sh
```

它做四件事：装系统依赖（python3-venv/git/nginx/certbot）→ 建用户 `ozon` 与目录 →
建 venv 装 `requirements.txt` 并拉取上游契约（`contracts/fetch_contracts.sh`，Linux 版，
因为许可原因契约不进仓库）→ 装 systemd 服务与 nginx 站点。

> ⚠️ **共享服务器**（上面已经跑着别的项目/站点）请用：
> ```bash
> sudo bash deploy/install.sh --no-apt
> ```
> `--no-apt` 不碰 apt，因此**不会升级/重启 nginx**；代价是要自己保证 `python3-venv`、`git` 已安装。
> 本工作台的 nginx 站点用**独立的 server_name 或 location**，不会改动已有站点。


---

## 3. 填密钥与店铺（**密钥只在这里**）

```bash
sudo nano /etc/ozon-workbench.env       # 已经由安装脚本生成（chmod 600）
#   ARK_API_KEY=...
#   ARK_TEXT_MODEL=ep-2026xxxx
#   OZON_DEFAULT_CLIENT_ID=... / OZON_DEFAULT_API_KEY=...

sudo -u ozon cp /opt/ozon-workbench/deploy/shops.example.json /opt/ozon-workbench/config/shops.json
sudo -u ozon nano /opt/ozon-workbench/config/shops.json   # 只写环境变量名，不写密钥
sudo systemctl restart ozon-workbench-api
curl -s http://127.0.0.1:8766/health
```

---

## 4. 图片存储（你的服务器 + nginx + https）

```bash
# 4.1 域名解析到本机 IP 后申请证书
sudo certbot --nginx -d img.example.com

# 4.2 确认 nginx 站点（安装脚本已放好，记得把域名换成你的）
sudo nginx -t && sudo systemctl reload nginx

# 4.3 把生成的图片同步过去（在商品目录上跑；--dry-run 可以先看要做什么）
sudo -u ozon /opt/ozon-workbench/.venv/bin/python -m pipeline.oss_local \
  --product-dir products/P000001 \
  --root /var/www/ozon-images \
  --base-url https://img.example.com
```

它会：把 `output/generated-images/**` 按 `<product_id>/<slot>.png` 复制到 `/var/www/ozon-images`（按 sha256 增量），
并写出 `output/image-public-urls.json`（slot → https URL）——**上传载荷就读它**。

自检：浏览器打开 `https://img.example.com/<product_id>/main-S1.png` 能看到图 → Ozon 也能抓到。

---

## 5. 日常怎么用（服务器上）

```bash
cd /opt/ozon-workbench

# 采集：把 1688 素材放进服务器某个目录（或用浏览器插件/API 导入）
.venv/bin/python -m collector.ingest --folder /data/capture/p1 \
    --keyword "простынь на резинке 160х200"

# 一条链跑到底（干跑）：fake 模型 + 占位生图，绝不碰 Ozon
.venv/bin/python -m pipeline.runner --product-dir products/P000001 \
    --provider fake --image-generator placeholder --uploader dry-run

# 真实模型 + 豆包生图 + 图片同步
.venv/bin/python -m pipeline.runner --product-dir products/P000001 \
    --provider ark --image-generator doubao --ozon-real
.venv/bin/python -m pipeline.oss_local --product-dir products/P000001 \
    --root /var/www/ozon-images --base-url https://img.example.com

# 上线前预检 / 提交后确认
.venv/bin/python -m pipeline.doctor --products-root products
.venv/bin/python -m pipeline.ozon_status --product-dir products/P000001
```

真实提交（**只有这一步会写 Ozon**，需要 `APP_MODE=production`）：

```bash
APP_MODE=production .venv/bin/python -m pipeline.runner \
  --product-dir products/P000001 --provider ark --image-generator doubao \
  --uploader ozon-api --execute-upload --i-understand-this-hits-ozon
```

---

## 6. 定时任务（可选）

```bash
sudo -u ozon crontab -e
# 每天早上 7 点确认一次 import 终态
0 7 * * * cd /opt/ozon-workbench && .venv/bin/python -m pipeline.ozon_status --product-dir products/P000001 >> logs/status.log 2>&1
```

---

## 7. 排错

| 现象 | 原因 / 处理 |
|---|---|
| 上传被拦 "以下图位没有可用的 https 公网地址" | 还没跑 `oss_local`，或 `--base-url` 不是 https |
| 浏览器打不开图片 | nginx 站点没 reload、安全组没放 443、或证书域名不匹配 |
| `缺凭据环境变量` | `/etc/ozon-workbench.env` 没填或 systemd 没重启 |
| 契约缺失（`找不到契约`） | 服务器上没拉成功：`bash contracts/fetch_contracts.sh` |
| API 打不开 | `systemctl status ozon-workbench-api`、`journalctl -u ozon-workbench-api -n 50` |

部署相关的改动都会在 `git log` 里；本工作台**不含**任何密钥（`.gitignore` 排除 `config/`、`.env*`、`.xlsx`、`products/`、`output/`）。
