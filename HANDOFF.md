# HANDOFF —— Ozon 上品自动化工作台（交接给下一位开发者 / 下一个 AI）

> 本文档描述的是**当前真实状态**（不是设计意图）。所有"已完成"都有可复现的验证命令；
> 所有"未完成"都写清了缺什么、谁来做、怎么做完。
> 读完这份 + 跑一遍 §2 的自检，你应该能在 15 分钟内接手并继续开发。

最后更新：2026-10（工作台公网入口：nginx 反向代理 + Basic Auth，端口 8088，无需 SSH 隧道）。

---

## 0. 一句话说清这是什么

从 **Seerfar 俄文关键词** 到 **Ozon 真实上架** 的自动化流水线：

```
Seerfar 采词 → 热度/竞争筛选 → 按类目建关键词库 → 选品清单（Ozon 复核链接 + 1688 找货链接）
→ 1688 采集（图片/详情图/规格）→ 选上架 SKU → AI 产品分析 → 选词 + 产品信息 → 俄文标题/简介（过 Ozon 规则）
→ 图片规划（槽位 + 提示词 + 卖点建议）→ 豆包生图 → 图片发布到对象存储 → 真实类目与上架属性
→ 多店铺上传（幂等，按 task_id 记账）→ 提交后回读核对
```

**现在能跑到的程度**：单店铺**已经真实提交成功并上线**（见 §3 证据）；整套流程有一条网页操作台可以点。

---

## 1. 代码、环境、凭据（先搞清这三件事）

| 项 | 值 |
|---|---|
| 本机仓库 | `E:\抖音自动化项目\ozon-workbench`（git，分支 `main`） |
| GitHub | `https://github.com/xgs1207-cloud/ozon-workbench`（public） |
| 服务器 | `ubuntu@43.132.190.110`，代码在 `/opt/ozon-workbench`，服务 `ozon-workbench-api`（uvicorn，监听 `127.0.0.1:8766`） |
| SSH 私钥 | `D:\AI作图\ozonfinancedeploy.pem` |
| 服务端配置/密钥 | `/etc/ozon-workbench.env`（`root:ubuntu`，`640`）——**只读用，永不打印、永不提交** |
| 网页操作台（公网，推荐） | `http://43.132.190.110:8088/`，Basic Auth 登录（账号 `ozon` / 密码见 `/etc/nginx/ozon-workbench.htpasswd`，当前 `ozon2026wb`）。nginx 配置见 `deploy/nginx/ozon-workbench.conf`，轻量服务器防火墙已放行 TCP:8088 |
| 网页操作台（隧道，备用） | 本机开隧道后访问 `http://127.0.0.1:8766/`：`ssh -i "D:\AI作图\ozonfinancedeploy.pem" -N -L 8766:127.0.0.1:8766 ubuntu@43.132.190.110` |
| 1688 采集插件 | `collector/edge-extension/`，Edge 加载已解压扩展；默认地址 `http://43.132.190.110:8088`。弹窗里填 `http://ozon:ozon2026wb@43.132.190.110:8088` 保存即可（凭据只存本机 chrome.storage）。详见 `collector/edge-extension/README.md` |

**两个必须知道的坑**：

1. **本机连不上 github.com**（被墙），但 `api.github.com` 可以。所以推送是从**服务器**发起的：
   `deploy/push-to-github.sh`（token 由 Windows 凭据管理器里的 `git:https://github.com` 读出，P/Invoke `CredRead`）。
2. **服务器上的 CLI 必须走 `bash deploy/with-env.sh <命令>`**：systemd 的环境文件在普通 shell 里不会自动加载。

---

## 2. 十五分钟自检（照着跑，跑通就说明你接手成功）

```powershell
# 本机：仓库自检（751 个测试；本机现为 Python 3.14.7，skip 56 个——此前在 Python 3.11 上 751 全 passed）
cd E:\抖音自动化项目\ozon-workbench
python -X utf8 -W ignore::UserWarning -m unittest discover -s tests -p "test*.py"

# 服务端：同步最新代码 + 跑同一套测试（服务器 Python 3.14，会 skip 4 个）
$key = "D:\AI作图\ozonfinancedeploy.pem"
git archive --format=tar.gz -o ..\ozon-workbench.tar.gz HEAD
cmd /c "ssh -i `"$key`" ubuntu@43.132.190.110 `"cat > /tmp/ozon-workbench.tar.gz`" < `"E:\抖音自动化项目\ozon-workbench.tar.gz`""
ssh -i $key ubuntu@43.132.190.110 "sudo -n tar -xzf /tmp/ozon-workbench.tar.gz -C /opt/ozon-workbench && sudo -n chown -R ubuntu:ubuntu /opt/ozon-workbench && cd /opt/ozon-workbench && .venv/bin/python -X utf8 -W ignore::UserWarning -m unittest discover -s tests -p 'test*.py' 2>&1 | tail -2 && sudo -n systemctl restart ozon-workbench-api"

# 服务端：看一眼全局状态（只读）
ssh -i $key ubuntu@43.132.190.110 "cd /opt/ozon-workbench && bash deploy/with-env.sh .venv/bin/python -X utf8 -m pipeline.doctor --products-root products 2>/dev/null | tail -25"
ssh -i $key ubuntu@43.132.190.110 "cd /opt/ozon-workbench && bash deploy/with-env.sh .venv/bin/python -X utf8 -m pipeline.stores --list 2>/dev/null | grep -v '^  [A-Z_]*='"
```

⚠️ **PowerShell 5.1 陷阱**（本机没有 pwsh/bash）：不要把多行 Python 塞进 `ssh "..."` 或 `python -c "..."`——
引号会被吃掉，且**整段脚本解析失败时一行都不会执行**（曾因此以为"已部署"其实没部署）。
把脚本写成文件再 `scp`/`cat >` 过去，或让 Python 读 `stdin`。

---

## 3. 当前真实状态（含证据）

### 3.1 已经真跑通并验证过的

| 环节 | 证据 |
|---|---|
| 真实提交 Ozon | task `5757999940` → `P000006-S1 = product 6524432190`、`P000006-S2 = product 6524432159`，`imported 2/2，0 错误` |
| 变体合并 | Ozon 回读 `model_info = {"model_id": 6525162180, "count": 2}` → 两变体合并成**一张商品卡** |
| Ozon 分配 SKU | `5956082914`（S1）/ `5956083009`（S2） |
| 属性真的落库 | `85 品牌 = Нет бренда`（dict 126745801，用**字典搜索接口**查到的官方值）、`10096 商品颜色 = белый/серый`（Ozon 映射到它自己的字典 61571/61576）、`9048 型号名称 = PS-200x200` |
| 图片被 Ozon 抓取 | Ozon 已把我们的 COS 图转存到 `ir.ozone.ru` |
| 对象存储 | 腾讯云 COS `ozon-images-1486640018`（ap-hongkong），**匿名可读已开**；`pipeline.oss_cos` 上传 + 写 `output/image-public-urls.json` |
| 文本模型 | 火山方舟 `ep-20260822160819-ncbxz`，**关思考**（`ARK_THINKING=disabled`）：实测 13.1s/527token → **3.4s/63token** |
| 生图模型 | `doubao-seedream-5-0-260128`（该账号可直接调用；要求 **≥3.69M 像素**，现用 `1920x2560`，出图后归一化 900×1200） |
| 关键词库 | 773 行 Seerfar 真实数据 → 24/24 列 → **121 个达标词**；`keyword-library/17028731-92612.jsonl` |
| 真实类目 | 床单 → `category_id 17028731 / type_id 92612`（路径 住宅和花园→床上用品→床单）；43 个真实属性、3 个必填 |
| 网页操作台 | `web/console.html`（单文件、零依赖）+ API：`GET /`、`/api/workbench/{steps,stores,summary,doctor}`、`products/{id}/{summary,skus,keywords,copy,artifacts,publications,preflight,verify,publish-images,submit,run}`、`collector/{products,duplicates,ozon-reference-page}` |
| 1688 采集插件 | `collector/edge-extension/`（MV3 Edge 插件，从原项目复用）：1688 页面抓标题/SKU/主图/详情图/属性 → 页面内抽屉选 SKU（≤10）+ 选 Ozon 类目 → 直接 POST `/api/collector/products` 入库（服务端带 Referer 下载图片）。也支持 Ozon 参考页采集。默认走公网 8088（Basic Auth 由 background 注入），无需隧道 |
| 公网入口 | nginx 监听 8088 + Basic Auth 代理到 `127.0.0.1:8766`（配置 `deploy/nginx/ozon-workbench.conf`，服务器实际路径 `/etc/nginx/sites-available/ozon-workbench`）；轻量服务器防火墙放行 8088 |
| 测试 | **本机 751 OK**（Python 3.14.7，skip 56；Python 3.11 上全 passed）；服务器同套（Python 3.14，skip 4） |

### 3.2 服务器上的商品

```
P000001  COLLECTED        completed  1/16
P000002  UPLOADING        completed 16/16  ready_to_submit=True
P000003  IMAGES_GENERATED completed 15/16
P000004  IMAGES_GENERATED completed 15/16
P000005  NEEDS_ATTENTION  completed  2/16   ← 真模型要求人工确认（材质/认证缺失）
P000006  UPLOADED         completed 15/16   ← ★ 已真实上线（见 §3.1）
```

### 3.3 还没做完的（按优先级）

| # | 事项 | 卡在哪 | 怎么做完 |
|---|---|---|---|
| 1 | ~~1688 真实采集~~ ✅ 已用 Edge 插件解决 | 原控制台脚本流程仍可用（`collector/capture_1688.js` + `fetch_images.py` + `push_capture.py`），但推荐用 `collector/edge-extension/`：浏览器加载已解压扩展 → 开 SSH 隧道（8766）→ 1688 商品页点插件采集 → 选 SKU + 类目 → 自动入库。详见 `collector/edge-extension/README.md`。 | — |
| 2 | **多店铺上传** | 第二家店的 `OZON_SHOP_B_CLIENT_ID/API_KEY` 还没配（`config/shops.json` 里 `shop-b` 仍 `enabled=false`） | 用户在 `/etc/ozon-workbench.env` 补两个变量 → `python -m pipeline.stores --enable shop-b` → 同一商品跑一次提交即可（机制已支持按店铺独立载荷+台账） |
| 3 | **真图替换占位图** | P000006 的详情图还是占位图（生图时参考图是占位图，所以出来的是白盒子） | 用 #1 的 Edge 插件重新采集一个真商品（会自动下载真照片），再跑 `image_generation`（`image_qc` 语义分也会随之改善；当前 `decision=revise, score=15`） |
| 4 | **图片语义质检** | `image_qc` 的 4 个语义维度未评分（显式标 `not_configured`） | 接一个视觉模型（方舟有 vision 模型但当前账号多为 Shutdown，需先在控制台开通接入点） |
| 5 | **型号名称（9048）** | 来源与字典都没有，只能人工定 | 已做人工确认入口：`products/<id>/input/human-confirmations.json` → `{"attributes": {"9048": "你的型号"}}`（**只补空缺、不覆盖机器值**） |
| 6 | **批量上架操作台** | 现在一次选一个商品 | 在 `web/console.html` 加多选 + 逐商品调用现有 `/run`、`/publish-images`、`/submit` |
| 7 | 定时确认 Ozon 终态 | 现在是提交时确认一次 + 手动补确认 | 把 `pipeline.ozon_status.confirm_task()` 接进定时任务 |

---

## 4. 代码地图（先看这些文件）

```
ozon-workbench/
├── api.py                    FastAPI：关键词库 / 采集入库 / 选词 / 文案 / 运行 / 预检 / 操作台
├── web/console.html          ★ 网页操作台（单文件、零依赖、无构建）
├── keyword_library/          关键词库（按类目 JSONL、分位数打分、CLI）
├── collector/
│   ├── edge-extension/       ★ Edge 采集插件（MV3）：1688/Ozon 页面采集 + SKU 抽屉 + 类目选择，直接 POST 入库
│   ├── seerfar_xlsx.py       Seerfar 导出表 → 关键词库
│   ├── sourcing.py           选品清单（含品牌观察名单 classify_keyword）
│   ├── collection_plan.py    采集清单（哪些词已采集）
│   ├── capture_1688.js       ★ 浏览器抓取脚本（标题/SKU/图片/详情页属性/正文）
│   ├── fetch_images.py       本机下载图片（带 Referer，绕 CDN 校验）
│   ├── push_capture.py       打包推送到服务器 API
│   └── ingest.py             入库：input/source.json + 图片目录 + 关键词绑定
├── models/
│   ├── http_provider.py      ★ 方舟文本模型适配（关思考、契约校验、机械归一化、重试）
│   ├── doubao_image.py       方舟生图适配（参考图 base64、归一化 900×1200）
│   └── fake.py               确定性模型（离线自检与测试基座）
├── pipeline/
│   ├── steps.py              15 步顺序与中文名（单一来源）
│   ├── runner.py             执行器（状态机、断点、门禁）
│   ├── status.py             每个商品的 status.json（唯一断点）
│   ├── catalog.py            类目匹配 + 属性编译
│   ├── attributes.py         属性填值（含"字典唯一值=强制值"、品牌字典搜索、人工确认）
│   ├── upload.py             按店铺构建载荷 + 门禁 + 分发（含库存字段禁令）
│   ├── ozon_write.py         ★ `/v3/product/import` 请求构建与发送
│   ├── ozon_status.py        提交后只读轮询终态 + 台账回填
│   ├── ozon_verify.py        ★ 提交后回读 Ozon（SKU/属性/图片转存/变体合并）
│   ├── preflight.py          ★ 提交前只读预检（店铺/凭据/载荷/图片可达性/**币种**）
│   ├── oss_cos.py            腾讯云 COS 上传 + 公网地址
│   ├── stores.py             店铺注册表（含 CLI：--list/--check/--enable）
│   └── doctor.py             全局预检（每个商品"还差什么"）
├── rules/                    Ozon 标题/简介/图片可执行规则 + copy_bundle 形状提示
├── contracts/                上游 40 个 JSON Schema + 轻量校验器 + 自研契约
│   └── normalize.py          契约驱动的机械归一化（丢未知键 / null→[]、{} / 字符串→单元素数组）
├── deploy/
│   ├── nginx/ozon-workbench.conf  公网入口配置（8088 + Basic Auth 反代）
│   ├── with-env.sh                服务器 CLI 加载 /etc/ozon-workbench.env
│   └── push-to-github.sh          从服务器推 GitHub
└── tests/                    751 个测试（含端到端回归）
```

---

## 5. 铁律（改代码前务必读，破坏它们会出真事故）

1. **干跑零写请求**：`run_product(dry_run=True)` 结束时 `api_write_count != 0` 会抛错。
2. **绝不提交库存字段**：`pipeline/upload.py` 对载荷全文做正则断言。
3. **不合格产物不落盘**：模型输出先过**契约**（`contracts.validate_contract`）+ **规则**（`rules.validate`），
   不过就 `PipelineGateError` → 商品转 `NEEDS_ATTENTION`。
4. **同一店铺拿到 `task_id` 后不重复创建**；一家店失败不影响其他店。
5. **不编造数据**：缺尺寸/重量/材质就不填（进 `required_summary.missing` 或 `production_blockers`）；
   缺采购价就不报价。拿不准就把 `decision` 设为 `needs_human_input` 交给人。
6. **真实提交是写操作**：`POST .../submit` 必须带 `{"confirm": "SUBMIT"}`；CLI 必须带
   `--i-understand-this-hits-ozon`；production 模式才允许真发请求。
7. **密钥只从环境变量读**，代码里不许出现密钥；接口/日志/文档里都不许打印密钥
   （`pipeline.stores` 有专门测试断言密钥不出现在输出里）。
8. **不要动服务器上别人的东西**：`/opt/ozon-finance`、`/opt/penguin-*`、用户自己的 nginx 站点都不要碰。
9. **契约常量 ≠ 事实**：上游把 `processing.model_mode` 写死 `connected_codex`，我们照写，但真实生成方记在
   `output/design-provenance.json`。
10. **写请求绝不盲目重试**：只有明确 429/5xx 才退避；连接层异常报 `AMBIGUOUS` 交人核对。

---

## 6. 踩过的坑（每条都是真机换来的，别再踩）

**Ozon 接口**
- `POST /v3/product/import` 的属性值**必须放在 `values[]` 里**：
  `{"id":8229,"values":[{"dictionary_value_id":92612,"value":"床单"}]}`。
  写成顶层 `dictionary_value_id` → **必填属性值静默变 None**，提交被拒（曾真实踩到）。
- **币种必须等于店铺合同币种**：合同 CNY 却提交 RUB → `currency_differs_from_contract`。
  价格要取同币种字段（`selling_price_cny` / `selling_price_rub`）；`pipeline.preflight` 会在提交前拦下。
- 属性名语言随拉取参数变化：拉 `ZH_HANS` 时属性名是中文（"品牌"/"颜色名称"），
  匹配模式表**必须同时含中俄英**（只写俄文会导致分支永远进不去）。
- 品牌字典上千个值且分页；用 `/v1/description-category/attribute/values/search` **精确查值**
  （`Нет бренда` = id 126745801），别想拉全量。
- 变体合并靠"同 `型号名称` + 变体属性不同"；回读看 `model_info.model_id/count` 判断是否合并。

**模型（火山方舟）**
- API Key 形如 `ark-<uuid>-<5位>`；`apikey-<时间戳>-xxxx` 那种**不是**方舟 key（会报
  `The API key format is incorrect`）。
- 模型/接入点要**按账号开通**：`GET /api/v3/models` 能列出账号可用模型（`status=None` 通常可用，
  `Shutdown`/`Retiring` 不可用，`ModelNotOpen` 要去控制台开通）。
- 推理模型默认会烧思考链：加 `"thinking": {"type": "disabled"}` 后**快 4 倍、输出 token 少 8 倍**。
- 回复可能被 `max_tokens` 截断（文案任务曾产出 11000+ 字符）→ 已加截断检测 + "要求精简"的重试。
- 契约是 `additionalProperties:false`：模型多塞字段会判不合规 → 已加**机械归一化**
  （丢未知键 / `null→[]/{}` / 字符串→单元素数组 / 对象→内部唯一字符串），语义错误仍如实报出。

**生图**
- `doubao-seedream-5-0-*` 要求 **≥3,686,400 像素**，`1152x1536` 会被拒。
- 归一化必须**双向**（大于目标也要缩），否则同一商品里主图 1920×2560、详情图 900×1200 不一致。
- 参考图决定内容：拿占位图当参考 → 出来就是白盒子（要真照片）。

**对象存储**
- 腾讯云 COS 子账号只给 `QcloudCOSDataFullControl` 时**不能改桶 ACL/策略**；匿名读必须在控制台开
  （安全管理→阻止公共访问关闭 + 概览→访问权限→公有读私有写）。
- Ozon 是匿名抓图，务必用 `pipeline.oss_cos --probe` / `pipeline.preflight` 验证 `HTTP 200`。

**工程环境**
- Windows PowerShell 5.1：无 `pwsh`、无 bash、无 `<` 重定向（用 `cmd /c`）；内联 Python 极易被引号吃掉，
  且**整段解析失败 = 一行不执行**（会造成"以为部署了其实没有"）。
- `git archive` 在 Windows 会写 CRLF → shell 脚本到服务器报 `set: pipefail: invalid option name`；
  已用 `.gitattributes`（`*.sh text eol=lf`）+ 回归测试锁住。
- systemd `ReadWritePaths` 指向不存在的目录会 `226/NAMESPACE` 崩溃重启（已用 `-` 前缀 + mkdir 解决）。
- 服务器 Python 是 3.14：某些库行为与本机 3.11 有差异，改完两边都跑测试。

---

## 7. 与"上一个 AI / 上一版"的协作方式（用户明确要求过）

- **卡在用户侧的事，先停下来教用户怎么做，不要继续闷头开发**（原话："哪里有问题需要我解决，应该停下来教我怎么解决，不是一直开发"）。
- **模型调用要既省钱又快捷**：默认关思考链、单次回复设上限、重试次数别贪多。
- **开发前先联网搜可用的 skill 再动手**，界面要好看：
  - [frontend-design](https://github.com/anthropics/skills/tree/main/skills/frontend-design)（官方，明确反对"AI 味"通用美学）
  - 索引：[awesome-claude-ui-armory](https://github.com/ezra-y/awesome-claude-ui-armory)、电商流程：[DTC-skills](https://github.com/Lee-NG915/DTC-skills)
  - 现有操作台就是照 `frontend-design` 做的：**工序导轨**是唯一"用力"的地方（工序是真序列，编号才有意义），
    冷石墨台面 + Ozon 自家蓝作唯一强调色，刻意避开：米色+陶土、近黑+荧光、等高圆角卡片墙、渐变装饰、
    全大写小标签、圆点分隔元信息、按钮后加箭头、逐段淡入。
- 用户看不懂代码时，用"证据 + 一句话结论"的方式汇报（命令输出、HTTP 状态、token 用量），别只讲实现。

---

## 8. 变更后怎么上线（照抄）

```powershell
# 1) 本机测试
python -X utf8 -W ignore::UserWarning -m unittest discover -s tests -p "test*.py"

# 2) 提交（用文件传中文提交信息，避免 PowerShell 引号问题）
$msg = "你的提交信息"
[System.IO.File]::WriteAllText("$env:TEMP\cm.txt", $msg, (New-Object System.Text.UTF8Encoding($false)))
git add -A; git commit -q -F "$env:TEMP\cm.txt"

# 3) 传到服务器（scp 不稳，用 cat > 通道）
$key = "D:\AI作图\ozonfinancedeploy.pem"
git archive --format=tar.gz -o ..\ozon-workbench.tar.gz HEAD
cmd /c "ssh -i `"$key`" ubuntu@43.132.190.110 `"cat > /tmp/ozon-workbench.tar.gz`" < `"E:\抖音自动化项目\ozon-workbench.tar.gz`""
ssh -i $key ubuntu@43.132.190.110 "sudo -n tar -xzf /tmp/ozon-workbench.tar.gz -C /opt/ozon-workbench && sudo -n chown -R ubuntu:ubuntu /opt/ozon-workbench && sudo -n systemctl restart ozon-workbench-api"

# 4) 推 GitHub（agent 侧脚本；token 从 Windows 凭据管理器读，别打印）
#    见 deploy/push-to-github.sh：把仓库 tar 与 token 传上去，在服务器上 git push
```

---

## 9. 第一次接手时的建议动作顺序

1. 跑 §2 自检（本机 + 服务器测试都绿）。
2. 打开操作台看现状（隧道 + 浏览器），点 `P000006` → 「提交后核对」，应当看到
   `ok=true / sku / model_info(count=2)`。
3. 读 §5 铁律 + §6 踩坑（这两节能省你几天）。
4. 挑 §3.3 里的一件事做：**最省事的是 #5（型号名称人工确认）**，
   **最有价值的是 #1（1688 采集）+ #3（真图）**，因为这两件做完就能再真实上架一个真商品。
