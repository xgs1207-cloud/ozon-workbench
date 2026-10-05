# HANDOFF —— Ozon 上品自动化工作台（交接说明）

> 面向接手的人（你本人或你的开发）。读完这一份，应该能：跑起来、看懂数据流、知道四个 adapter 怎么接、
> 知道哪些地方**故意保守**以及为什么。

---

## 0. 一句话

本地运行的 1688 → Ozon 上品流水线：**Seerfar 采词 → 关键词库 → 1688 采集 → AI 文案 → 图片规划/生图/质检 →
真实类目属性 → 多店铺幂等上传**。核心链路**零第三方依赖**（纯标准库），可端到端跑通；
只差四个 adapter（模型层、生图后端、对象存储、Ozon HTTP），它们的接口都已经定义好。

**当前能做什么**：从采集入库一路跑到"上传回执"，全程**不接触 Ozon**（干跑/模拟）；
能对真实商品跑完整管线并给出"还差什么才能提交"的清单。

**当前不能做什么**：真正发请求给 Ozon（需要凭据）、用真实模型生成文案（需要选型）、用真实生图出图（需要选型）。

---

## 1. 快速开始

```powershell
cd ozon-workbench

# 1) 自检：240 个测试全绿
python -m unittest discover -s tests -p "test*.py"

# 2) 一键演示（8 个阶段，全本地）
python examples/run_demo.py

# 3) 拉上游数据契约（40 个 JSON Schema，走 gh-proxy 镜像）
powershell -ExecutionPolicy Bypass -File .\contracts\fetch_contracts.ps1

# 4) 导入真实采集素材（没有浏览器插件也能灌数据）
python -m collector.ingest --folder D:\capture\p1 --products-root products

# 5) 跑流水线（默认干跑：永不真提交）
python -m pipeline.runner --product-dir products\P000001 --provider fake --image-generator placeholder --uploader dry-run

# 6) 上线前预检：还差什么
python -m pipeline.doctor --products-root products

# 7) 本地服务（关键词库 + 采集入库 + 选词/文案/运行/预检）
uvicorn api:app --app-dir . --host 127.0.0.1 --port 8766
```

---

## 2. 目录结构

```
ozon-workbench/
├── keyword_library/     M0 关键词库（按类目 JSONL、分位数打分、CLI）
├── collector/           M1 采集侧（ingest 入库、CSV 导入、Seerfar 浏览器适配层）
├── pipeline/            执行层：steps 注册表、status 状态机、batch 批次、runner 执行器、
│                        handlers 模型步骤、catalog 类目/属性、image_* 图片、upload 上传、
│                        publications 台账、stores 店铺、doctor 预检、publish_urls 图链映射
├── models/              模型层接口 + 确定性 fake + 图片规划 + 本地占位生图
├── contracts/           上游 40 个 JSON Schema + 自研轻量校验器（无第三方依赖）
├── rules/               Ozon 标题/简介/图片规则文档 + 可执行校验
├── examples/            一键演示
└── tests/               240 个测试（含端到端回归）
```

---

## 3. 数据流（每个商品的目录就是它的全部状态）

```
products/P000001/
├── input/
│   ├── source.json                  采集输入（1688 链接、SKU、类目、图片计数）
│   ├── raw-snapshot.json            原始抓取
│   ├── source-manifest.json         采集绑定的 sha256 依据
│   ├── category-selection.json      采集时选的类目
│   ├── selected-keywords.json       ★ 从关键词库选的词（M0 → M2 的桥）
│   ├── workbench-sku-overrides.json 人工改过的 SKU 资料（采集输入本身不被改写）
│   ├── main-images/ sku-images/ detail-images/   原图（只允许引用这里的真实文件）
│   └── pending-question.json        只有"关键歧义"才生成（已授权批次永不生成）
├── output/
│   ├── product-analysis.json        商品事实与风险（模型步骤，过 product-analysis 契约）
│   ├── title-ru.json / description-ru.json / keywords-ru.json / copy-ru.json   文案
│   ├── image-plan.json + image-plan-brief.md    图片计划与"给运营看的提示词建议"
│   ├── image-generation-report.json 生图回执（含 generator 名字与 final_images）
│   ├── generated-images/{variant-main,detail}/*.png
│   ├── image-qc-report.json + image-regeneration-request.json   质检与技术回退请求
│   ├── image-public-urls.json       ★ 对象存储 adapter 产出：slot → https URL
│   ├── ozon-category.json           类目（必须 metadata_source=ozon_seller_api）
│   ├── ozon-category-attributes.json 类目属性快照
│   ├── attribute-fill-input.json    属性填值输入（有证据的事实）
│   ├── ozon-attributes-final.json   最终属性（required_summary.missing 必须为 0）
│   ├── platform-grouping-result.json 变体合并判定
│   ├── pricing-result.json / cost-analysis.json   定价与尺寸重量
│   ├── upload-feasibility.json      阶段 A 的 11 项可行性门禁
│   ├── store-publications.json      多店铺台账（task_id / offer_id / 状态 / 错误）
│   ├── store-runs/<店铺>/{payload.json, ozon-result.json}   按店铺的载荷与回执
│   ├── upload-summary.json          本次分发的汇总
│   └── run-report.json              最近一次 runner 运行报告
└── status.json                      ★ 唯一断点：状态机 + 每一步重试计数 + 快照绑定
```

**15 步顺序**（`pipeline/steps.py:PIPELINE_STEPS`）：
`validate_source → product_analysis → category_match → variant_rules → measurements → offer_exists_check →
upload_feasibility → product_positioning → ecommerce_design → russian_copy → field_completion → image_plan →
image_generation → image_qc → ozon_upload`

---

## 4. 不变量（改代码时不要破坏）

1. **绝不提交库存字段**：`pipeline/upload.py` 有正则断言检查载荷全文；调用库存/仓库/激活接口一律禁止。
2. **干跑零写请求**：`run_product(dry_run=True)` 结束时若 `api_write_count != 0` 会抛错。
3. **不合格产物不落盘**：模型输出先过**契约**（`contracts.validate_contract`）+ **规则**（`rules.validate`），
   不过就 `PipelineGateError` → 商品转 `NEEDS_ATTENTION`，一个文件都不写。
4. **没实现的步骤不会假装完成**：缺 handler / 缺前置产物 → 停下并报 `handler_not_implemented` / `missing_inputs`。
5. **同一店铺拿到 `task_id` 后不重复创建**；一家店失败不影响其他店。
6. **不编造数据**：没有结构化尺寸/重量/材质就不填（缺项进 `required_summary.missing` 或 `production_blockers`）；
   定价上**缺采购价就不报价**（而不是拿 0 成本算出误导性价格），超过价格上限降级为 `WARNING`。
7. **图片只能引用 `input/{main,sku,detail}-images` 里的真实文件**；中文不得进入 buyer 可见字段与生图提示词。
8. **预检（doctor）是纯读的**；`upload_feasibility` 也是纯本地（0 网络调用）。
9. **设计/文案/图片计划是投影关系**（见 §5.1b）：投影步骤不调模型；改顺序前先确认没有循环依赖。
10. **契约常量不等于事实**：上游把 `processing.model_mode` 写死为 `connected_codex`，
    我们照契约写入，但真正的生成方记在 `output/design-provenance.json` 与 `validation_warnings` 里。
11. **写请求绝不盲目重试**：明确的 429/5xx 才退避重试；连接层异常（结果未知）如实报 `AMBIGUOUS`，
    交给人核对 —— 宁可慢，不可重复提交。

---

## 5. 四个 adapter 怎么接（这是剩下的全部工作）

### 5.1 模型层（产品分析 / 定位 / 设计 / 文案 / 图片规划）

- 接口：`models/base.py` 的 `ModelProvider`，实现五个方法
  `analyze_product / position_product / design_listing / write_copy_ru / plan_images`。
- 注册：`models/__init__.py:load_provider()`；已实现两个：
  * `fake`：确定性自检（不需要密钥）；
  * `http` / `ark`：OpenAI 兼容端点。**你选定文本与生图都用火山方舟豆包**，所以推荐
    `--provider ark` + `--image-generator doubao`，两者共用 `ARK_API_KEY`：
    ```powershell
    $env:ARK_API_KEY="..."
    $env:ARK_TEXT_MODEL="ep-2026xxxx"     # 方舟控制台的文本接入点 ID
    $env:ARK_IMAGE_MODEL="doubao-seedream-3-0-t2i-250415"   # 生图模型或接入点
    ```
- 参考样例：`models/fake.py` + `models/design.py`（**它们的产物能通过全部契约校验**，新 adapter 可直接对照）。
  注意 `models/design.py` 已经是"确定性装配器"：半结构化的模型输出交给它补全并过契约（`HttpModelProvider` 就是这么做的）。
- 验收：`validate_contract("product-analysis" | "product-positioning" | "ozon-ecommerce-design" | "title-ru" | "description-ru" | "keywords-ru" | "image-plan", …)` 全空；
  文案再过 `rules.validate_copy_bundle`。
- 如果是 Codex CLI：原项目的调用形态是
  `codex exec -C <repo> --skip-git-repo-check --ephemeral --disable chronicle -s danger-full-access -c approval_policy="never" … <prompt>`，
  注意那是**无审批全盘访问**，建议改成受限沙箱或直接换 API。

### 5.1b 三个投影关系（改流水线前必读）

上游语义是"设计步骤产出买家可见内容"，所以：

```
ecommerce_design ──产出──> listing(文案) ──投影──> russian_copy（不调模型）
                 └─物化──> image-plan.json ──校验──> image_plan（不重复调模型）
field_completion ──编译──> ozon-attributes-final.json ──被引用──> 设计的 attribute_decisions
```

设计步骤在需要时才调模型的 `write_copy_ru` / `plan_images`（产物不存在时），
所以**顺序是自洽的**：design(9) → russian_copy(10) → field_completion(11) → image_plan(12)。
如果你要调整顺序，务必保持"先产出、后投影"，否则会出现循环依赖。

### 5.2 生图后端

**已实现豆包（火山方舟）**：`models/doubao_image.py`（用户选定）。

- 实现 `generate(request: ImageRequest) -> {"generated": [...], "generator", "final_images": True}`；
- 参考图以 `data:image/png;base64,…` 随请求发送（图生图/编辑），默认最多 3 张；
- 返回后可选 Pillow 归一化为 3:4、900×1200 png（居中裁剪，不变形）；
- `python -m models.doubao_image --product-dir <商品> --show-request` 先看不发；
- 要换别家（即梦/MJ/自建）只需按同一协议写一个 `generate()`，并在
  `models/__init__.py:load_image_generator()` 里加一个分支。

**约束**（来自 `rules/image-slot-and-qc-rules.md`）：3:4、≥900×1200、仅 png、**禁止后置叠字**、照片级实拍、
参考图是"事实锁"不是可复制画布；`comparison`/`size_spec` 类必须真实原图确定性合成。

### 5.3 对象存储（替换原项目的 24h 隧道）

- 职责：把 `output/generated-images/**` 上传到你的对象存储，然后产出
  **`output/image-public-urls.json`**（`{"urls": {"main-S1": "https://…", …}}`）。
- 现成工具：`python -m pipeline.publish_urls --product-dir products/P000001 --base-url https://cdn.example.com`
  （只写映射，不传文件、不联网）。
- 门禁：上传载荷要求**每个图位都有 https 地址**，否则进 `production_blockers`。

### 5.4 Ozon HTTP

**读的部分已经实现**（`pipeline/ozon_http.py` + `pipeline/category.py`）：

- `OzonClient` 依赖注入式 `Transport`：真实是 `UrllibTransport`（Client-Id / Api-Key 头），离线是 `FixtureTransport`；
- 已实现三个只读接口（类目树 / 类目属性 / 字典值），`category_match` 会把响应规范成
  `ozon-category-attributes` 契约形状并写 `metadata_source=ozon_seller_api`；
- 凭据从 `config/shops.json` 的 `*_env` 名字去环境变量取；缺了就报清楚缺哪个变量；
- **还没用真实凭据验证过**：`contracts/fixtures/` 是按官方文档字段构造的样例。
  拿到凭据后先跑 `python -m pipeline.ozon_http --check --shop <店铺> --category-id <id> --type-id <id>` 对齐。

**写的部分已实现**（`pipeline/ozon_write.py`）：`/v3/product/import` 的请求构建、响应解析与保守重试，
凭据同样从 `config/shops.json` 的环境变量名解析；`OzonWriteUploader` 实现了 `Uploader` 协议
（`performs_api_writes = True`）。请求体里**不含任何库存字段**（正则断言 + 测试）。

**拿到凭据后的验收步骤**（顺序别省）：

1. `python -m pipeline.ozon_http --check --shop <店铺> --category-id <id> --type-id <id>`
   —— 对齐只读侧（类目树/属性字段名）；
2. `python -m pipeline.ozon_write --payload products\<P######>\output\store-runs\<店铺>\payload.json --show-request`
   —— **不联网**地看一眼将发送的请求体，重点核对：`description_category_id/type_id`、
   `price` 两位小数、`attributes` 里字典属性是否走了 `dictionary_value_id`、
   `weight`(g) 与 `depth/width/height`(mm) 的单位；
3. `--send --store <店铺> --i-understand-this-hits-ozon` 小批量试单（建议先 1 个 SKU）；
4. 拿回执里的 `task_id` 去查 `/v1/product/import/info`（只读）确认最终状态与逐项错误；
5. 若字段名与实际 API 有差异，改 `build_import_request` 即可 —— 传输层、重试与记账都不用动。

**终态跟踪已实现**（`pipeline/ozon_status.py`）：提交拿到 `task_id` 后自动**只读**轮询
`/v1/product/import/info` 到终态，把逐 SKU 结果写进台账、`import-info.json` 与 `ozon-result.json`；
也可事后跑 `python -m pipeline.ozon_status --product-dir <商品>` 补确认（`--fixture` 可离线演练）。
`doctor` 会显示每家店的 `imported / failed / pending` 与 task_id。

**已知待改进**：没有定时/后台轮询（现在是提交时确认一次 + 手动补确认）；Ozon 侧若有长审核队列，
可以考虑把 `confirm_task()` 接到定时任务里。

**定价/尺寸（`measurements`）已实现**（`pipeline/measurements.py`）：成本加成定价 + 可配置费率
（`config/pricing.json`），产物 `pricing-result.json` / `measurements.json` / `profit-analysis.json`。
⚠️ 它**不是**上游那份 `pricing-result` 契约（上游绑死作者本机的 Excel 运费表 `RETS`）——
我们定义了自己的两个契约，见 `contracts/workbench-*.schema.json`。
如果要接上游运费表，只需要把 `compute_sku_pricing` 换成读表实现，产物形状再补一个适配器即可。

**每个商品要真实提交，还需要**：人工确认的尺寸重量（`input/workbench-sku-overrides.json`）、
定价配置（`config/pricing.json` 或接受内置默认值）、以及图片公网地址。

---

### 5.5 选品闭环（关键词库 → 找货 → 采集）

用户流程的前两段由两个工具承接（都是**只读/离线**的，不发业务请求）：

1. `python -m collector.sourcing --library keyword-library --top 20`
   → `output/sourcing-plan.{json,md}`：每个达标词给出 **Ozon 复核链接**（俄文词）、
   **1688 找货链接**（中文词；默认取 Seerfar 中文类目名，`--translate --provider ark` 时让模型翻）、
   打分依据，并把**疑似品牌词**（含拉丁字母/®™）单列提醒"按品类找货、不要照抄品牌"；
2. `python -m pipeline.category --name "Простыня" [--fixture-dir contracts/fixtures]`
   → `config/category-bindings.json`：类目名 → 真实 `category_id/type_id`（只读匹配，找不到就报 unmatched）；
   `python -m collector.seerfar_xlsx ... --bindings config/category-bindings.json` 会把真实 id 套进关键词库。

---

## 6. 排查手册

| 现象 | 先看什么 |
|---|---|
| 商品卡住不动 | `status.json` 的 `current_step` / `next_action` / `failed_step` / `warnings`；再看 `output/run-report.json` |
| 某步骤"未实现" | run report 的 `stop_reason=handler_not_implemented`：该步骤还没接 adapter（见第 5 节） |
| 缺前置产物 | `stop_reason=missing_inputs` + `executed[].missing_inputs`：补对应产物或先跑前序步骤 |
| 提交被拒 | `output/store-runs/<店铺>/payload.json` 的 `production_blockers`；或 `pipeline.doctor` |
| 图片问题 | `output/image-qc-report.json`（`critical_failures` / `decision`）+ `image-regeneration-request.json` |
| 重复创建风险 | `output/store-publications.json`：该店铺是否已有 `task_id` |
| 整体还差什么 | `python -m pipeline.doctor --products-root products` |

---

## 7. 与原项目的关系

**复用**（契约与规则层，已搬运）：`templates/*.schema.json` 40 个契约、`rules/` 两份规则文档提炼自
`.agents/skills/ozon-ecommerce-designer/SKILL.md` 与 `references/product-specific-image-standard.md`。

**有意偏离**（都在代码注释里写明）：

1. 状态机把 `NEEDS_ATTENTION` 单列一支 `attention_resume` —— 原项目归进终态会导致"失败重试丢掉断点"；
2. `upload_feasibility` 在阶段 A（第 7 步）跑，最终属性第 11 步才有 → 按上游语义回退读预览属性，两个都没有记 WARN；
3. 变体判定不能依赖 `is_aspect`（上游类目快照契约里没有这个字段）→ 按名称+字典匹配，**拿不准就不合并**；
4. 我们的 `status.json` 是上游契约的**超集**（多 `collection_id` / `source_url`），对接原 uploader 时需要容忍或挪走；
5. `ozon-upload-config.json` 降级为可选输入：它要求真实整数尺寸重量，我们不编造。

**没继承的坑**：作者私有的 `/Users/apple/Documents/洪辰知识库/…`、macOS 写死的 `codex_command`、
`shop_name: zhonglian1`、`/usr/bin/open` 与 `/bin/ps`、`SIGSTOP/SIGCONT`（Windows 不可用）。

**许可提醒**：原项目是 PolyForm Noncommercial 1.0.0 —— 搬运其契约与规则自用没问题，
一旦对外销售/代运营/SaaS 需向其著作权人取得商业授权。本工作台自身的代码由你决定许可方式。

---

## 8. 未完成清单

- [ ] 真实模型 adapter（产品分析 / 定位 / 设计 / 文案 / 图片规划）
- [ ] 真实生图后端 adapter
- [ ] 对象存储 adapter（产 `image-public-urls.json`）—— 已有 `pipeline.publish_urls` 生成映射，缺"上传文件"那一步
- [ ] Ozon 写侧：用真实凭据校准 `build_import_request` 的字段名（确认流程已实现）
- [ ] （可选）接上游 Excel 运费表 `RETS`，替换现在的成本加成定价
- [ ] 用真实凭据校准 `contracts/fixtures/` 里的类目响应样例（`pipeline.ozon_http --check`）
- [ ] 视觉语义质检（商品一致性/合规等 4 个维度）—— 现在显式标注"未评分"
- [ ] Seerfar 采集适配层的选择器校准（需要真实页面的列名）
- [ ] 界面层（当前只有 HTTP 接口 + CLI；界面可另建前端接这些接口）
