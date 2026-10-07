# 部署到腾讯云服务器（CVM）—— 从零到能跑

> 目标：工作台跑在**你的服务器**上，生成的图片存在**你的服务器**上并通过 https 给 Ozon 抓取。
> 本文里的 `/opt/ozon-workbench`、`/var/www/ozon-images`、`img.example.com` 都可以改，改的时候整篇保持一致。

桌面工作台请按[七步上架流程](../docs/guided-listing-flow.md)逐项确认；以下一条链 CLI 为旧流程的运维入口，不代替工作台人工审核。
生图默认服务可在环境文件配置 `IMAGE_GENERATOR=rightapi`，见[RightAPI 接入说明](../docs/rightapi-images.md)。

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
sudo nano /etc/ozon-workbench.env       # 安装脚本已生成；共享服务器上权限为 root:ubuntu 640
#   ARK_API_KEY=...
#   ARK_TEXT_MODEL=ep-2026xxxx
#   OZON_DEFAULT_CLIENT_ID=... / OZON_DEFAULT_API_KEY=...

sudo -u ozon cp /opt/ozon-workbench/deploy/shops.example.json /opt/ozon-workbench/config/shops.json
sudo -u ozon nano /opt/ozon-workbench/config/shops.json   # 只写环境变量名，不写密钥
sudo systemctl restart ozon-workbench-api
curl -s http://127.0.0.1:8766/health
```

> ⚠️ **手动敲命令看不到 env 文件**（systemd 会自己读，你的 shell 不会）。所以要这样跑 CLI：
> ```bash
> bash deploy/with-env.sh .venv/bin/python -m pipeline.oss_cos --check
> ```
> 它会先载入 `/etc/ozon-workbench.env`，并只打印"哪些变量已设置 / 为空"（**不回显密钥值**）。

---

## 4. 图片存储（腾讯云 COS ／ 自建 nginx，二选一）

### 方案 A：腾讯云 COS（你选定的方案）

```bash
# 4.1 在腾讯云控制台建一个 bucket（建议「公有读私有写」，否则 Ozon 抓不到图片）
#     记下：bucket 名（形如 my-bucket-1250000000）与 region（形如 ap-hongkong）

# 4.2 把凭据写进环境变量文件（不要写进仓库）
sudo nano /etc/ozon-workbench.env
#   COS_SECRET_ID=AKIDxxx
#   COS_SECRET_KEY=xxx
#   COS_BUCKET=my-bucket-1250000000
#   COS_REGION=ap-hongkong
#   COS_KEY_PREFIX=ozon-images
sudo systemctl restart ozon-workbench-api

# 4.3 ★ 先自检：PUT 探针 → 匿名 GET（模拟 Ozon 抓取）→ DELETE
cd /opt/ozon-workbench
bash deploy/with-env.sh .venv/bin/python -m pipeline.oss_cos --check
#   ok=true 才算通过；anonymous_get=http_403 说明桶/前缀不是公有读

# 4.3b 如果 --check 报 AccessDenied：先跑权限矩阵，定位是"没授权"还是"前缀不对"
bash deploy/with-env.sh .venv/bin/python deploy/cos-perm-diag.py

# 4.3c 免密钥探测（只验"Ozon 能不能抓到"，不需要密钥）
bash deploy/with-env.sh .venv/bin/python -m pipeline.oss_cos --probe --key ozon-images/P000002/main-S1.png

# 4.4 正式上传
.venv/bin/python -m pipeline.oss_cos --product-dir products/P000001
```

### 方案 B：自建 nginx + Let's Encrypt

```bash
# 4.1 域名解析到本机 IP 后申请证书
sudo certbot --nginx -d img.example.com

# 4.2 确认 nginx 站点（安装脚本已放好，记得把域名换成你的）
sudo nginx -t && sudo systemctl reload nginx

# 4.3 把生成的图片同步过去（在商品目录上跑；--dry-run 可以先看要做什么）
sudo -u ubuntu /opt/ozon-workbench/.venv/bin/python -m pipeline.oss_local \
  --product-dir products/P000001 \
  --root /var/www/ozon-images \
  --base-url https://img.example.com
```

两条路都会写出 `output/image-public-urls.json`（slot → https URL）——**上传载荷就读它**。


---

## 5. 日常怎么用（服务器上）

**最常用的一条命令**（生图 → 发布图片 → 质检 → 载荷 → 提交，一条跑完）：
```bash
cd /opt/ozon-workbench
# 干跑（零写请求，先看载荷与阻断项）
.venv/bin/python -m pipeline.launch --product-dir products/P000002 --store default \
    --provider ark --image-generator doubao --uploader dry-run \
    --ozon-fixture contracts/fixtures --oss cos

# 真提交（需要 /etc/ozon-workbench.env 里的凭据 + config/shops.json 里 enabled=true）
APP_MODE=production .venv/bin/python -m pipeline.launch --product-dir products/P000002 --store default \
    --provider ark --image-generator doubao --uploader ozon-api \
    --execute-upload --i-understand-this-hits-ozon --oss cos

# 批量：所有 COLLECTED 商品
.venv/bin/python -m pipeline.launch --products-root products --store default \
    --provider ark --image-generator doubao --oss cos
```

**一天一条命令**（选词 → 选品 → 采集清单 → 跑商品 → 预检）：

```bash
cd /opt/ozon-workbench
.venv/bin/python -m pipeline.day --xlsx data/Seerfar-*.xlsx --products products \
    --store default --oss cos --provider ark --image-generator doubao
```

**手工分步**（想单步排查时）：

```bash
# 采集：把 1688 素材放进服务器某个目录（或用浏览器插件/API 导入）
.venv/bin/python -m collector.ingest --folder /data/capture/p1 \
    --keyword "простынь на резинке 160х200"

# 一条链跑到底（干跑）：fake 模型 + 占位生图，绝不碰 Ozon
.venv/bin/python -m pipeline.runner --product-dir products/P000001 \
    --provider fake --image-generator placeholder --uploader dry-run

# 真实模型 + 豆包生图 + 图片同步
.venv/bin/python -m pipeline.runner --product-dir products/P000001 \
    --provider ark --image-generator doubao --ozon-real
.venv/bin/python -m pipeline.oss_cos --product-dir products/P000001     # 或 pipeline.oss_local

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

## 8. 共享服务器模式（已在真实腾讯云机器上跑通）

如果这台机器上**已经跑着别的项目**（例如 `/opt/ozon-finance`、`/opt/penguin-*` 与 nginx 站点），
请用**最小侵入**方式部署，不要跑 `install.sh` 的 apt 部分（可能升级/重启 nginx）：

```bash
# 1) 只传代码（用 git archive，自动排除密钥、业务数据与 .git）
#    本机：git archive --format=tar.gz -o ozon-workbench.tar.gz HEAD
scp -i <密钥> ozon-workbench.tar.gz ubuntu@<IP>:/tmp/

# 2) 解压到**独立目录**（不碰别人的目录）
ssh -i <密钥> ubuntu@<IP> '
  sudo mkdir -p /opt/ozon-workbench
  sudo tar -xzf /tmp/ozon-workbench.tar.gz -C /opt/ozon-workbench
  sudo chown -R ubuntu:ubuntu /opt/ozon-workbench'

# 3) Python 环境（服务器自带 python3.14，无需 apt）
ssh -i <密钥> ubuntu@<IP> 'cd /opt/ozon-workbench &&
  python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt &&
  bash contracts/fetch_contracts.sh'

# 4) 自检（在服务器上跑全部测试 + 演示）
ssh -i <密钥> ubuntu@<IP> 'cd /opt/ozon-workbench &&
  .venv/bin/python -m unittest discover -s tests -p "test*.py" 2>&1 | tail -3 &&
  .venv/bin/python examples/run_demo.py >/dev/null && echo "演示 OK"'

# 5) 目录与配置
ssh -i <密钥> ubuntu@<IP> '
  mkdir -p /opt/ozon-workbench/config
  cp /opt/ozon-workbench/deploy/shops.example.json /opt/ozon-workbench/config/shops.json
  sudo install -m 600 -o root -g root \
       /opt/ozon-workbench/deploy/ozon-workbench.env.example /etc/ozon-workbench.env
  sudo mkdir -p /var/www/ozon-images && sudo chown ubuntu:ubuntu /var/www/ozon-images'

# 6) 只新增一个 systemd 服务（用 ubuntu 用户跑；服务名独立，不动别人的）
ssh -i <密钥> ubuntu@<IP> '
  sed -e "s/^User=ozon$/User=ubuntu/" -e "s/^Group=ozon$/Group=ubuntu/" \
      /opt/ozon-workbench/deploy/ozon-workbench-api.service | sudo tee /etc/systemd/system/ozon-workbench-api.service >/dev/null
  sudo systemctl daemon-reload && sudo systemctl enable --now ozon-workbench-api'
curl -s http://127.0.0.1:8766/health   # 在服务器上执行
```

### 真实踩过的四个坑（都已在代码里修掉，别再踩）

| 坑 | 现象 | 修法 |
|---|---|---|
| Windows 上 `git archive` 把脚本打成 **CRLF** | `set: pipefail: invalid option name`，契约一个都拉不下来 | 加 `.gitattributes`（`*.sh text eol=lf`），并有测试守着 git 里的 blob 不含 CR |
| `ReadWritePaths` 指向**尚不存在**的目录 | 服务 `226/NAMESPACE` 崩溃重启（`ActiveState=activating`），但手动起的进程还能跑，容易误判"已经好了" | unit 里写成 `ReadWritePaths=-/路径`（`-` = 缺失不致命），安装脚本同时 `mkdir -p` |
| 共享服务器上跑 `install.sh` | `apt-get install nginx` 可能升级/重启 nginx，影响别人站点 | 用 `--no-apt`，或按上面这份手工流程 |
| 缺 `httpx` | `tests/test_api*.py` 整块导入失败（TestClient 依赖），服务器自检跑不全 | `requirements.txt` 里加 `httpx`，并有测试守着 |

---

## 9. 怎么访问工作台（推荐：SSH 隧道，零暴露）

服务只在服务器上监听 `127.0.0.1:8766`（不给公网开口子）。你在自己电脑上开一条隧道即可：

```powershell
# Windows PowerShell（一次开着，别关；Ctrl+C 断开）
ssh -N -i D:\AI作图\ozonfinancedeploy.pem -L 8766:127.0.0.1:8766 ubuntu@43.132.190.110
```

然后浏览器打开：

| 地址 | 用途 |
|---|---|
| <http://127.0.0.1:8766/docs> | 交互式 API 文档（点开就能试） |
| <http://127.0.0.1:8766/health> | 健康检查 |
| <http://127.0.0.1:8766/api/keywords?only_qualified=true&limit=50&order=score> | 达标关键词（高热度低竞争） |
| <http://127.0.0.1:8766/api/workbench/doctor> | 上线前预检（还差什么） |
| <http://127.0.0.1:8766/api/workbench/keyword-products> | 关键词 → 商品 汇总 |

要**公网访问**（例如手机上看）时，加一层 nginx 反代 + 密码，别直接把 8766 暴露到公网：

```bash
sudo apt-get install -y apache2-utils && sudo htpasswd -c /etc/nginx/.htpasswd yourname
# 再在 nginx 加一个独立 server：location / { auth_basic "ozon"; auth_basic_user_file /etc/nginx/.htpasswd;
#   proxy_pass http://127.0.0.1:8766; }
```

## 10. 真实数据流程（已在服务器上用你的 Seerfar 表跑通）

```bash
cd /opt/ozon-workbench
mkdir -p data && cp /path/to/Seerfar-*.xlsx data/     # 表放服务器上（不进仓库）

# ① 关键词入库 + 打分
.venv/bin/python -m collector.seerfar_xlsx --xlsx data/Seerfar-*.xlsx --library keyword-library
# ② 选品清单（Ozon 复核 + 1688 找货 + 品牌词提醒）→ output/sourcing-plan.md
.venv/bin/python -m collector.sourcing --library keyword-library --top 20 --out-dir .
# ③ 采集清单（哪些词还没采）→ output/collection-plan.md
.venv/bin/python -m collector.collection_plan --plan output/sourcing-plan.json --products products --out-dir .
# ④ 预检：环境/凭据/商品/关键词映射
.venv/bin/python -m pipeline.doctor --products-root products
```

**采集（素材在你 Windows 本机时）**：开隧道后在你本机推：

```powershell
ssh -N -i D:\AI作图\ozonfinancedeploy.pem -L 8766:127.0.0.1:8766 ubuntu@43.132.190.110
python -m collector.push_capture --folder D:\capture\p1 --keyword "простынь на резинке 160х200"
```

服务端接口 `POST /api/collector/products/capture` 收 base64 图片 → 解包入库（sha256 去重、带上关键词）。
实测：Windows → 隧道 → 服务器入库成功（`P000002`，7 张图全部落盘，关键词直接写进 `selected-keywords.json`）。

> 服务器上现在有两个**演示商品**（`products/P000001`、`P000002`，用的是占位图）：
> 想清掉就 `rm -rf /opt/ozon-workbench/products/P00000{1,2}`（连带台账一起删）。


实测（你的表）：**773 行解析零跳过 → 121 条高热度低竞争达标 → 清单前 3 名 `yerrna` / `yerrna постельное белье` /
`шуйские ситцы`**（前两个被标注"疑似品牌词：1688 按品类找货、不要照抄品牌"）；
`doctor` 当时指出"缺 Ozon 凭据、还没有商品"——这正是你填完密钥后要看的报告。

## 11. 排错

| 现象 | 原因 / 处理 |
|---|---|
| 上传被拦 "以下图位没有可用的 https 公网地址" | 还没跑 `oss_local`，或 `--base-url` 不是 https |
| 浏览器打不开图片 | nginx 站点没 reload、安全组没放 443、或证书域名不匹配 |
| `缺凭据环境变量` | `/etc/ozon-workbench.env` 没填或 systemd 没重启 |
| 契约缺失（`找不到契约`） | 服务器上没拉成功：`bash contracts/fetch_contracts.sh` |
| API 打不开 | `systemctl status ozon-workbench-api`、`journalctl -u ozon-workbench-api -n 50` |

部署相关的改动都会在 `git log` 里；本工作台**不含**任何密钥（`.gitignore` 排除 `config/`、`.env*`、`.xlsx`、`products/`、`output/`）。
