# Ozon 上品自动化工作台（本地）

本地运行、按类目组织的一体化上品流水线。左侧是"素材与数据"，右侧是"AI 生产"，最后落到多店铺上传。

> **接手先读 [HANDOFF.md](HANDOFF.md)**：架构、数据流、不变量、四个 adapter 怎么接、排查手册、未完成清单。

## 端到端流程

```
① Seerfar 采词 ──► 打分筛选(高热度/低竞争) ──► 关键词库(按 Ozon 类目)
                                                      │
② 1688 采集(图片/详情图/规格) ──► 选上架 SKU ──► ③ AI 总结产品信息
                                                      │
                              ④ 选词 + 产品信息 ──► 俄文标题 / 简介（守 Ozon 规则）
                                                      │
⑤ 参考图 + 提示词 ──► 图片规划建议 ──► 生图 ──► 对象存储 ──► 图片 QC
                                                      │
⑥ Ozon 真实类目 + 属性填充（upload_feasibility 门禁）
                                                      │
⑦ 多店铺上传（幂等，有 task_id 不重复建）+ 结果回写
```

## 里程碑

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M0 | 关键词库：schema、按类目存储、高热度低竞争打分、采集入库接口、筛选/选词 API | **已落地并跑通**（Python 3.11.9 / 80 个测试全绿） |
| M1 | 1688 采集与 SKU 选择、AI 产品信息总结 | **采集入库 + 状态机 + 批次 + 干跑 runner 已落地并跑通**（真实采集数据能一路走到阶段 A 门禁）；AI 分析待你选定模型层 |
| M2 | 选词 → 俄文标题/简介生成（复用 Ozon 规则与 skill 约束） | **已落地**：契约校验 + 规则校验 + 模型层接口 + 标题/简介生成（fake 模型层跑通）；接真实模型待你拍板 |
| M3 | 参考图 + 提示词 → 图片规划建议 → 生图 → 对象存储 → QC | **图片规划 + 占位生图 + 技术质检已落地**；真实生图后端与对象存储 adapter 待你选定 |
| M4 | Ozon 真实类目/属性填充 + 多店铺上传 | **属性编译 + 变体规则 + 上传载荷 + 多店铺幂等台账 + 干跑提交已落地**；只差真正的 Ozon HTTP 调用（需你的凭据） |

两条不变量贯穿所有里程碑：**不碰 Ozon 写接口**（`api_write_count` 必须为 0，runner 强制校验）、
**不合格的产物不落盘**（模型输出先过契约与规则校验，不过就转 `NEEDS_ATTENTION`）。

## 复用映射（来自原项目 jlcglobal/jlc-global-ozon-auto-listing）

| 本工作台 | 复用的原项目模块 | 改法 |
|---|---|---|
| ② 1688 采集 | `collector/edge-extension/`（SKU+类目抽屉、缺货不可选、≤10 SKU）、`POST /api/collector/products`、`templates/collector-capture.schema.json` | 基本原样复用；接入本工作台的入库接口 |
| ③ 产品信息总结 | `scripts/product_analysis_fast.py`、`templates/product-analysis.schema.json` | 换模型层 |
| ④ 标题/简介 | `.agents/skills/ozon-ecommerce-designer/SKILL.md`（50KB 规则）、`scripts/russian_seo_rules.py`、`ozon_ecommerce_designer_contract.py --materialize`、`templates/{title-ru,description-ru,copy-ru}.schema.json` | 把"人工选词"接成输入 |
| ⑤ 生图 | `scripts/image_planner.py`(97KB)、`image_slot_scheduler`、`image_wave_executor`、`image_generator_contract`、`image_qc.py --hard-gate`、`templates/image-plan.schema.json`、`ozon-reference-tasks` | 换生图后端 + 换公网图链为自有对象存储 |
| ⑥ 类目/属性 | `scripts/ozon_metadata_matcher.py`、`ozon-adapter/cli.py --fetch`（强制 `metadata_source=="ozon_seller_api"`）、`ozon_attribute_compiler.py`、`ozon-field-completion/cli.py`、11 项 `upload_feasibility` 门禁 | 基本原样复用 |
| ⑦ 多店铺上传 | `scripts/multi_store_upload.py --execute [--only-store]`、`ozon-uploader/`、`ozon-adapter/shops.json`、`store_publications.json` | 基本原样复用 |

**不要继承的依赖**：原项目 `AGENTS.md` 指向作者私有知识库 `/Users/apple/Documents/洪辰知识库/SOURCE_OF_TRUTH.md`，以及 `config/pipeline-settings.json` 里写死的 `codex_command`（macOS ChatGPT 内置路径）与 `shop_name: zhonglian1`。

**我们与上游契约的已知差异**：本工作台的 `status.json` 是上游 `status.schema.json` 的**超集** ——
上游必填字段齐全，但额外记了 `collection_id` / `source_url`（`tests/test_contracts_lite.py` 里有用例锁定这一点）。
对接原项目 uploader 时要么容忍多余字段，要么把这些字段挪到旁挂文件。

## 目录结构

```
ozon-workbench/
├── keyword_library/        # M0：关键词库
│   ├── scoring.py          #   高热度低竞争打分（按类目分位数）
│   ├── store.py            #   按类目 JSONL 存储、去重、状态流转
│   ├── cli.py              #   ingest / rescore / query / stats
│   └── schema.json         #   关键词记录契约
├── pipeline/               # 状态机 + 批次 + 干跑 runner + 模型步骤
│   ├── steps.py            #   15 步注册表（顺序、阶段、门禁、产物）
│   ├── status.py           #   status.json 状态机（断点、续跑、失败、快照）
│   ├── batch.py            #   批次创建、去重、冻结 SKU 快照
│   ├── context.py          #   StepContext / PipelineGateError（runner 与 handlers 共用）
│   ├── runner.py           #   干跑执行器（永不碰 Ozon，未实现的步骤不会假装完成）
│   ├── handlers.py         #   模型步骤：product_analysis / russian_copy / image_plan
│   ├── catalog.py          #   本地步骤：variant_rules（变体规则）/ field_completion（属性编译）
│   ├── measurements.py     #   定价引擎 + 尺寸重量整理（只接受确认值，不估算）
│   ├── image_probe.py      #   纯标准库图片探测（真实宽高/格式）+ 最小 PNG 写入
│   ├── image_generation.py #   image_generation handler（可插拔生图后端）
│   ├── image_qc.py         #   image_qc handler（技术质检真跑 + 语义缺口显式标注）
│   ├── attributes.py       #   属性编译与变体判定逻辑（不联网、不编造值）
│   ├── stores.py           #   店铺注册表（只存环境变量名，绝不存密钥）
│   ├── publications.py     #   多店铺发布台账与分发计划（幂等）
│   ├── upload.py           #   上传载荷构建 + 门禁 + 可替换 uploader（默认干跑）
│   ├── ozon_http.py        #   Ozon 只读适配器（类目树/属性/字典值，注入式传输层）
│   ├── ozon_write.py       #   Ozon 写适配器（/v3/product/import 请求构建、响应解析、保守重试）
│   ├── ozon_status.py      #   提交后终态跟踪（/v1/product/import/info，只读轮询 + 回写台账）
│   ├── category.py         #   category_match 真实实现（写 metadata_source=ozon_seller_api）
│   ├── doctor.py           #   上线前预检：还差什么才能真正提交（不发起任何 Ozon 调用）
│   ├── publish_urls.py     #   图位 → 公网 URL 映射（对象存储 adapter 的落地点）
│   └── selection.py        #   已选关键词（关键词库 → 文案生成的桥）
├── models/                 # 模型层接口 + 确定性 fake + HTTP 适配器 + 定位/设计装配器 + 图片规划 + 占位生图
│   ├── http_provider.py    #   OpenAI 兼容 / DeepSeek 风格端点（凭据走环境变量，含契约校验与修复重试）
│   ├── doubao_image.py     #   豆包（火山方舟）生图：参考图 + 提示词 → 900×1200 png，含 QC 归一化
│   ├── design.py           #   product-positioning / ozon-ecommerce-design 的确定性装配（只写有证据的内容）
│   ├── image_plan.py       #   N 张 SKU 主图 + 8 张共享详情图的槽位计划与提示词
│   └── local_image.py      #   本地占位图生成器（尺寸合规、明确标注"非最终图片"）
├── api.py                  # 本地 HTTP 接口（关键词库 + 采集入库 + 选词/文案，8766）
├── parsing.py              # 数字解析（价格/尺寸的小数点与千分位歧义，统一入口）
├── collector/              # 采集侧（**Seerfar xlsx 导入**、CSV 导入、采集入库、浏览器适配层）
│   └── ingest.py           #   采集入库：校验、查重、落盘、采集绑定
├── contracts/              # 上游契约（original/*.schema.json）+ fixtures/ 录制样例 + 轻量校验器
├── rules/                  # 规则文档（*.md）+ 可执行校验（validate.py）
├── examples/               # 可直接跑的样例数据与演示脚本
└── tests/
```

## 用法与自检

```powershell
cd ozon-workbench
python -m unittest discover -s tests -p "test*.py"          # 本机实测：393 passed（Python 3.11.9，含端到端回归）
python -m collector.seerfar_xlsx --xlsx "D:\AI作图\Seerfar-*.xlsx" --library keyword-library   # Seerfar 导出表 → 关键词库
python -m models.doubao_image --product-dir products\P000001 --show-request                    # 看将发送的生图请求（不联网）
python -m models.doubao_image --product-dir products\P000001 --limit 1                         # 真的出图（需 ARK_API_KEY）
python examples/run_demo.py                                 # 一键演示：采集→…→生图→质检→属性→载荷→干跑提交→预检
python -m pipeline.doctor --products-root products          # 上线前预检：还差什么才能真正提交
python -m pipeline.ozon_http --check --fixture-dir contracts/fixtures --category-id 1001 --type-id 2001   # 离线自检
python -m pipeline.ozon_http --check --shop default --category-id 1001 --type-id 2001                     # 真实只读（需凭据）
python -m pipeline.publish_urls --product-dir products\P000001 --base-url https://cdn.example.com   # 图位→公网URL
python -m pipeline.ozon_write --payload products\P000001\output\store-runs\shop-a\payload.json --show-request  # 看将发送的请求体（不联网）
python -m pipeline.ozon_write --payload <同上> --send --store shop-a --i-understand-this-hits-ozon             # 真的提交（需凭据）
python -m pipeline.ozon_status --product-dir products\P000001 --store shop-a                                  # 事后确认终态（只读）
python -m pipeline.ozon_status --product-dir products\P000001 --fixture contracts\fixtures\ozon-import-info.json  # 离线演练
python -m pipeline.runner --product-dir products\P000001 --provider fake --image-generator placeholder --uploader dry-run --ozon-fixture contracts/fixtures
python -m collector.ingest --folder D:\capture\p1 --products-root products   # 文件夹导入真实采集
python -m pipeline.runner --product-dir products\P000001 --provider fake     # 带模型层跑流水线（仍是干跑）
python -m pipeline.handlers --product-dir products\P000001 --step russian_copy --provider fake
python -m pipeline.handlers --product-dir products\P000001 --step image_plan --provider fake
python -m keyword_library.cli ingest examples/seerfar-sample.json
uvicorn api:app --app-dir . --host 127.0.0.1 --port 8766    # 关键词库 + 采集入库 + 选词/文案
```

### M2：文案生成（契约 + 规则双校验）

1. **选词**：`PUT /api/workbench/products/{id}/keywords`（直接给词，或 `from_library=true` 按分数取），
   落 `input/selected-keywords.json`；
2. **生成**：`POST /api/workbench/products/{id}/copy {"steps":["product_analysis","russian_copy"]}`，
   产物写 `output/product-analysis.json`、`output/{title-ru,description-ru,keywords-ru}.json`、`output/copy-ru.json`；
3. **不通过就不落盘**：模型输出先过 `contracts.validate_contract`（上游 `templates/*.schema.json`）
   与 `rules.validate_copy_bundle`（标签数量/字符集、五段描述、标题长度与中文、容量与颜色归一），
   任一不过 → `PipelineGateError` → 商品转 `NEEDS_ATTENTION`，**不写任何产物文件**；
4. **模型层可替换**：`models/base.py` 定义接口，`models/fake.py` 是确定性实现（不联网、不随机）。
   接 Codex CLI 或 API 只需再加一个 adapter；`load_provider("...")` 目前只有 `fake`。

### M3：图片规划与提示词建议

`python -m pipeline.handlers --step image_plan`（或流水线里的第 12 步）产出两个文件：

- `output/image-plan.json` —— 严格符合上游 `image-plan` 契约的槽位计划：
  **每个已选 SKU 恰好 1 张主图（顺序与选中 SKU 一致）+ 整套恰好 8 张共享详情图**；
  每个槽位带 `purpose` / `buyer_question` / `visual_goal` / `scene_description` / `russian_text` /
  `reference_image_ids` / `operation` / `art_direction`（14 个必填字段）/ `overlay_plan`（1–7 条叠字指令）/ `prompt`；
- `output/image-plan-brief.md` —— 给运营看的**规划说明 + 提示词建议**（每张图要体现什么、回答哪个买家问题、
  叠什么俄文、用什么英文提示词），这正是"AI 告诉我图该怎么规划"那一步。

规则落点（`rules/image-slot-and-qc-rules.md`）：8 张详情图按购买决策顺序（认知 → 用途 → 结构 → 差异 → 使用 →
场景 → 细节 → 提醒）；单 SKU 时**不做 SKU 对比图**；比较类槽位用真实原图确定性合成；
**不编造尺寸/材质/认证**（没有结构化尺寸就不给 `measurement_annotation`，确认不了的槽位标 `needs_human_input` + 风险项）；
中文只出现在给运营看的说明里，`russian_text` 与 `prompt` 只有俄文/拉丁字符（有测试锁定这条）；
`overlay_strategy = single_pass_model_native_typography`（禁止后置叠字）。
**图片规划的前置条件是文案**：没有 `output/copy-ru.json` 就拒绝规划，而不是自己编一段俄文。

**生图（可插拔）与图片质检（真跑技术检查）**：

- `image_generation` 只在传入后端时注册；`models/local_image.py` 是**本地占位图**生成器（3:4、900×1200，
  明确标注 `final_images=false`），用来在没接真实生图前打通流水线；
- `image_qc` 是纯本地步骤，用 **纯标准库**读 PNG/JPEG/WebP/GIF 头拿到真实宽高（不依赖 Pillow），检查：
  文件可读、格式必须 png、**3:4（容差 0.002）**、**≥900×1200**、图位齐全、参考图缺失告警；
- **不谎报 pass**：商品一致性/转化逻辑/风格/合规这四个维度需要视觉模型，报告中会标注"本地未评分"，
  整体 `decision=revise` 但 `regenerate_needed=false`（技术全过时不触发无意义重生成）；
  只有**真正该重做的问题**（比例错、非 png、不可读、分辨率不足）才会写 `output/image-regeneration-request.json`；
- **上传门禁读质检报告**：缺少报告、有 `critical_failures`、或 `decision=reject` 都会进 `production_blockers`
  （占位图会作为 `suggestions` 提示"正式上架前必须换真实生图后端重跑"）。

### M4：类目属性与多店铺分发（本地部分）

**类目来自真实的 Ozon 只读接口**（`pipeline/ozon_http.py` + `pipeline/category.py`）：

- `OzonClient` 只依赖一个**注入式传输层**（`post(path, body)`）：真实 HTTP 是 `UrllibTransport`
  （`Client-Id` / `Api-Key` 头，凭据只从环境变量读），测试与离线自检用 `FixtureTransport`（`contracts/fixtures/`）；
- 只实现**只读**三个接口：`/v1/description-category/tree`、`/description-category/attribute`、
  `/description-category/attribute/values`（有测试断言模块里不出现任何写接口路径）；
- `category_match` 会把 Ozon 响应规范成契约形状的快照（`dictionary_id: 0 → null`），
  **类目在类目树里找不到时不谎报确认**（写 `api_match_needs_review` + 告警），类目树整体为空则硬失败；
- 缺凭据时报清楚缺哪个环境变量，而不是模糊的 401；
- ⚠️ `contracts/fixtures/` 里的样例是**按官方 API 文档字段构造的**，还没有用真实凭据验证过 ——
  拿凭据后先跑 `python -m pipeline.ozon_http --check --shop <店铺>` 对齐一次。


**变体规则**（`pipeline/catalog.py` → `attributes.evaluate_variant_rules`）：从 SKU 真实差异（俄语颜色、归一容量）
判断能否按变体合并，只认类目快照里存在的对应属性。⚠️ 上游 `ozon-category-attributes` 契约里**没有 `is_aspect`**，
所以这里按「属性名 + 字典值」匹配；**拿不准就不合并**（`rule_required` / `separate_cards`），绝不冒险把非 aspect 属性当变体维度。

**属性编译**（`field_completion`）：只填**有证据**的字段 —— 类目字典里的 `Нет бренда`（项目"无品牌"规则，带 `dictionary_value_id`）、
SKU 自己的俄语颜色、归一后的容量；中文材质等需要翻译的事实**不直接进买家字段**（标成 `NEEDS_TRANSLATION:` 待处理）。
缺的必需属性如实进 `required_summary.missing`，由提交前的门禁拦下，而不是在这里报错转人工。

**门禁位置**：`upload_feasibility` 是**阶段 A 第 7 步**，那时最终属性还没编译（第 11 步才有），
所以按上游语义它回退读预览属性 `ozon-attributes.json`；两个都没有时记 **WARN** 而不是 FAIL。
真正的「必填属性 missing == 0」硬门禁在提交前（upload payload / `ozon_upload`）。

**多店铺**（`pipeline/stores.py` + `pipeline/publications.py`）：
- 注册表 `config/shops.json` **只存环境变量名**（`client_id_env` / `api_key_env`），密钥从进程环境读；
  写注册表时若发现疑似真密钥会被 `validate_registry` 拦下；示例店铺默认 `enabled=false`，防误传；
- 台账 `output/store-publications.json` 对齐上游 `store-publications` 契约：**已拿到 Ozon `task_id` 的店铺不重复创建**，
  失败的店可以重试，一家店失败不影响其他店；`plan_publications()` 产出「create / skip + 原因」的分发计划。

**上传链路**（`pipeline/upload.py`）：

1. **按店铺构建载荷**（`shop_name` 在载荷里），严格对齐上游 `ozon-upload-payload` 契约；
2. **`production_blockers` 列出全部阻断项**，目前包括：类目非 Ozon API 来源、类目不匹配、属性快照来源不可信、
   必填属性 missing > 0、缺俄文标题/简介、**缺卢布售价**、**缺商品/包装尺寸重量**（不编造）、
   图位缺 https 公网地址、变体映射 `RULE_REQUIRED`；production 模式下 blocker 非空即拒绝提交；
3. **绝不提交库存字段**：载荷文本里有正则断言检查，`api_request_template.inventory_fields_included = false`；
4. **干跑不写假 `task_id`**（否则幂等规则会在真正上传时把商品挡在门外），干跑回执 `status=skipped` / `task_id=unknown`；
5. **危险配置保护**：production 模式下使用干跑 uploader 会被直接拒绝（`performs_api_writes = False`），
   防止"以为在上传、其实什么都没发生"；
6. **图片公网地址**来自 `output/image-public-urls.json`（slot → https URL）—— 这就是替换原项目 24h Cloudflare 隧道的接入点。
   按店铺回执落在 `output/store-runs/<店铺>/{payload.json, ozon-result.json}`（后者对齐 `ozon-result` 契约）。

### 生图（豆包 / 火山方舟）

```powershell
$env:ARK_API_KEY="你的方舟 API Key"
$env:ARK_IMAGE_MODEL="doubao-seedream-3-0-t2i-250415"   # 或你在控制台创建的接入点 ID（ep-xxx）
$env:ARK_IMAGE_SIZE="1152x1536"                          # 3:4；模型不支持时换一个，报错会原样带出
python -m models.doubao_image --product-dir products\P000001 --show-request   # 先看请求（不联网、不计费）
python -m models.doubao_image --product-dir products\P000001 --limit 1        # 试出 1 张
python -m pipeline.runner --product-dir products\P000001 --provider http --image-generator doubao --uploader dry-run
```

- **参考图是事实锁**：把计划里该槽位的 `reference_product_images` 以 `data:image/png;base64,…` 随请求发给模型
  （图生图/编辑），而不是"描述一遍让它画"；默认最多 3 张、单张 ≤8MB，可用 `--no-reference` 关闭；
- **尺寸与格式自动对齐我们的 QC**：默认请求 3:4，返回后用 Pillow **居中裁剪 + 缩放到 900×1200、只存 png**
  （裁剪而非拉伸，避免变形）；没有 Pillow 时会明确报错而不是悄悄放过；
- **不叠字**：`watermark=false`，提示词同样禁中文/水印；叠字文案来自 `image-plan.json`（技能规则决定）；
- **逐槽位容错**：某张失败只记 `skipped`，全部失败才抛错（不会半途写坏计划）；
- **计费调用的重试很保守**：只有明确的 429/5xx 才重试，连接层异常不盲目重试。

### Seerfar 导出表 → 关键词库

列名按你 2026-10-05 的真实导出表核对过（24 列全部识别，773 行解析零跳过）：

```powershell
python -m collector.seerfar_xlsx --xlsx "D:\AI作图\Seerfar-Market20261005_2000.xlsx" --library keyword-library
python -m collector.seerfar_xlsx --xlsx <同上> --competition-field 商品数        # 换竞争口径（竞对数/商品数/竞品数）
python -m collector.seerfar_xlsx --xlsx <同上> --category-id 1001 --type-id 2001  # 显式绑定 Ozon 真实类目
```

| 表格列 | 映射 |
|---|---|
| 关键词 | 去掉换行与中文释义括注（`простынь на резинке 160х200\n(橡胶枕160x200)` → 俄文关键词） |
| 月搜热度 | `search_volume`（热度指标） |
| 竞对数 / 商品数 / 竞品数 | `competitor_count`（默认**竞对数**，三者原值都留在 `extra` 里可换口径重算） |
| 月搜增长 | `trend`（百分数） |
| 其它 20 列 | 解析后原样进 `extra`（₽ 去符号、`782 g`→数值+单位、`%`→百分数），**不做有损转换** |
| 类目 | 拆成 `category_name_zh` / `category_name_ru` |

⚠️ 这份表**没有 Ozon 的 category_id / type_id**：不显式指定时按类目名生成 `seerfar-<hash>` 合成主键
（只用于关键词库分组）；真正上架用的类目来自 Ozon Seller API（`pipeline/category.py`）。

**实测（你 2026-10-05 的导出）**：单类目「床单 / Простыня」773 条关键词、24 列全部识别、0 行跳过；
按默认门槛（热度分位 ≥0.6 且竞争分位 ≤0.6，λ=0.6）筛出 **121 条高热度低竞争关键词**，最高分几条：

| score | 热度分位 | 竞争分位 | 关键词 | 月搜热度 | 竞对数 |
|---|---|---|---|---|---|
| 0.937 | 0.966 | 0.048 | `yerrna` | 6,543 | 4 |
| 0.928 | 0.963 | 0.058 | `yerrna постельное белье` | 6,293 | 10 |
| 0.916 | 0.980 | 0.106 | `шуйские ситцы` | 12,308 | 34 |
| 0.911 | 0.968 | 0.095 | `шуйские ситцы постельное белье` | 7,830 | 30 |
| 0.898 | 0.933 | 0.058 | `озон хоум` | 3,118 | 10 |

### 选品清单（关键词库 → Ozon 复核 + 1688 找货）

```powershell
python -m collector.sourcing --library keyword-library --top 20              # 生成 output/sourcing-plan.{json,md}
python -m collector.sourcing --library keyword-library --top 20 --csv plan.csv
python -m collector.sourcing --library keyword-library --top 20 --translate --provider ark   # 让模型给中文找货词
```

每个词给出三件事：**Ozon 市场复核链接**（俄文词直接搜）、**1688 找货链接**（中文词，1688 搜俄文搜不到）、
**打分依据**（score / 热度分位 / 竞争分位 / 月搜热度 / 竞对数）。

- **中文找货词**默认取 Seerfar 表里的中文类目名（`床单`），`--translate` 时让模型翻译（模型不可用就回收类目名并告警，不猜）；
- **疑似品牌词会被标出来**：含拉丁字母/®™ 的词单列一节提醒"按品类找货、不要照抄品牌"，
  避免侵权与被 Ozon 下架（你的真实数据里前 5 名有 2 个属于这类）；
- 清单**不发任何网络请求**：只生成链接与优先级；市场数据仍由你在 Ozon 页面人工复核。

**实测（你的 773 条词）**：默认门槛下达标 121 条，清单前 5 名见下表（`--top 5`）：

| # | score | 关键词 | 类型 | 1688 找货词 |
|---|---|---|---|---|
| 1 | 0.937 | `yerrna` | 品牌? | 床单 |
| 2 | 0.928 | `yerrna постельное белье` | 品牌? | 床单 |
| 3 | 0.916 | `шуйские ситцы` | 品类 | 床单 |
| 4 | 0.911 | `шуйские ситцы постельное белье` | 品类 | 床单 |
| 5 | 0.898 | `озон хоум` | 品类 | 床单 |

### 采集清单（选品清单 → 采集任务，带状态跟踪）

```powershell
python -m collector.collection_plan --plan output/sourcing-plan.json --products products --top 10
python -m collector.collection_plan --library keyword-library --products products          # 直接从词库重建
python -m collector.collection_plan --mark products\P000001 --keyword "простынь на резинке 160х200"  # 补记关键词
```

- 每个词给出 **目标 Ozon 类目**（来自绑定；未绑定时会明确显示"未绑定"）、两个搜索入口、打分；
- **状态不靠人工打勾**：扫 `products/*/input/source.json` 与 `selected-keywords.json` 里真实记录的关键词，
  判定 `⬜ 待采集` / `✅ 已采集`，并列出已采集的商品与它们的当前状态；
- **采集时带上关键词**（这样商品与关键词库天然对齐、还能跳过手工选词）：
  ```powershell
  python -m collector.ingest --folder D:\capture\p1 --keyword "простынь на резинке 160х200"
  ```
  入库后会写 `source.json.keywords` + 直接写好 `input/selected-keywords.json`；
  若没给类目但给了 `keyword_category`，类目也会沿用绑定值（**不猜**，只是沿用）；
- `doctor` 新增「关键词 → 商品」一节，回答"这个词下有几个商品、走到哪一步、能不能提交"。



### 类目绑定（类目名 → 真实 category_id/type_id）

Seerfar 表只有类目**名称**（`床单` / `Простыня`），没有 Ozon 的 id，而关键词库与商品都要用真实类目：

```powershell
# 离线演练（用 contracts/fixtures 的类目树）
python -m pipeline.category --name "Термосы" --fixture-dir contracts/fixtures
# 真实模式（需要 Ozon 凭据）
python -m pipeline.category --name "Простыня" --name "Наволочка" --shop shop-a
python -m pipeline.category --from-library keyword-library          # 一次解析库里所有类目名
# 把绑定套用到 Seerfar 导入（真实 id 进关键词库）
python -m collector.seerfar_xlsx --xlsx "<表>" --library keyword-library --bindings config/category-bindings.json
```

- 只读、按名字匹配（精确 > 前缀 > 包含），**找不到就如实报 unmatched**，不猜一个 id；
- `--auto-pick-unique`：只有唯一精确匹配时才自动选定，歧义则留给人判断；
- 结果写 `config/category-bindings.json`（本地配置，已在 .gitignore 里）。

### 模型层（fake / http）

```powershell
# 自检：确定性 fake（不需要任何密钥）
python -m pipeline.runner --product-dir products\P000001 --provider fake

# 火山方舟豆包（与生图共用同一个 ARK_API_KEY）—— 你选定的方案
$env:ARK_API_KEY="你的方舟 API Key"
$env:ARK_TEXT_MODEL="ep-2026xxxx（方舟控制台的文本接入点 ID）"
python -m pipeline.runner --product-dir products\P000001 --provider ark --image-generator doubao

# 或任意 OpenAI 兼容端点（DeepSeek / 通义 / 自建 vLLM）
$env:MODEL_BASE_URL="https://api.deepseek.com/v1"
$env:MODEL_API_KEY="sk-xxx"
$env:MODEL_NAME="deepseek-chat"
python -m pipeline.runner --product-dir products\P000001 --provider http
```

`--provider ark` 的等价写法是 `--provider http` + `MODEL_BASE_URL=https://ark.cn-beijing.volces.com/api/v3`；
`ark` 只是为了少配两行：端点与密钥默认取 `ARK_BASE_URL` / `ARK_API_KEY`，模型名取 `ARK_TEXT_MODEL`。

**核心原则：模型负责创意，装配器负责结构。** 让通用模型直接产出 23KB 设计契约或 20 字段图位基本不可能一次过契约，所以：

| 步骤 | 模型做什么 | 装配器做什么 |
|---|---|---|
| `product_analysis` | 输出完整 `product-analysis` JSON | 只做契约校验（不合格就带问题清单重试） |
| `product_positioning` | 输出完整 `product-positioning` JSON | 只做契约校验 |
| `russian_copy` | 标题/简介/关键词/标签 | 契约 + 规则双校验（标签必须西里尔字母等） |
| `ecommerce_design` | 只产出**买家可见文案**（listing） | SKU 计划、属性决策、visual_system、决策留痕由 `models/design.py` 补齐并过契约 |
| `image_plan` | （可选）润色提示词 | 槽位/合成方式/叠字规则来自技能约束，由规则装配器产出 |

- **修复重试**：输出不是 JSON 或不过契约时，会把**校验问题清单**回灌给模型重试（`MODEL_MAX_ATTEMPTS`，默认 2 次）；
  重试仍不过就**如实失败**（转人工），不做静默降级；
- **可选降级**：`MODEL_FALLBACK_TO_DETERMINISTIC=true` 时，分析失败会退回确定性实现（轨迹里留痕，便于对照）；
- **绝不静默吞掉问题**：每次调用的结果记在 `provider.calls`（第几次、是否通过、问题清单、字符数），
  因为多数契约是 `additionalProperties: false`，不能往里塞 `warnings` 字段；
- **只从环境变量读凭据**：缺哪个变量就报哪个（实测：`模型层配置不完整，缺少环境变量：MODEL_BASE_URL, MODEL_API_KEY, MODEL_NAME`）；
- 也可接 Codex CLI：实现同一个 `ModelProvider` 接口（或只实现 `ChatTransport`）即可，见 HANDOFF §5.1。



上游语义是"**设计步骤产出全部买家可见内容**"，所以我们照它实现，并且**理清了三个投影关系**
（这是这套流水线最容易搞错的地方）：

| 步骤 | 谁产出 | 谁只是投影/校验 |
|---|---|---|
| 俄文文案 | `ecommerce_design`（设计文档的 `listing`） | `russian_copy` 从设计**纯投影**（不调模型），设计不存在时才退回调模型 |
| 图片计划 | `ecommerce_design`（物化 `output/image-plan.json`） | `image_plan` 校验物化产物，不重复调模型 |
| 属性决策 | `field_completion` 编译 | 设计里的 `attribute_decisions` 引用编译结果 |

- 设计文档严格过 `ozon-ecommerce-design` 契约：16 个顶层键、`listing` 7 键、
  `main_images` = SKU 数、`detail_images` **恰好 8 张**、每个图位 20 个必填字段（`prompt` ≥120 字符）、
  `decision_trace` **恰好 7 步**且 `compliance_status=PASS`；
- **只写有证据的内容**：定位里没有证据的字段写 `null` 并进 `unknowns`（例如买家画像）；
  每个图位没有参考图就**直接拒绝**（参考图是身份锁，契约也要求 `source_references` ≥1）；
- **上游契约怪癖照实披露**：`processing.model_mode` 被写死为常量 `connected_codex`，
  我们照契约写入，同时在 `validation_warnings` 与 `output/design-provenance.json` 里记录真正的生成方；
- 买家可见字段（标题/简介/叠字）**不得含中文**，有测试按 CJK 正则守着。

### 上传写侧（ozon_write：`/v3/product/import`）

`pipeline/ozon_write.py` 把上传载荷翻译成真实请求并解析响应，用**同一套注入式传输层**，所以离线可测：

- **请求构建**：`offer_id` / `name`(≤255) / `description_category_id` / `type_id` / `price`(两位小数字符串) /
  `currency_code` / `vat` / `attributes` / `images` + `primary_image` / `description`，
  以及 **`weight`(g) + `depth`/`width`/`height`(mm)**；没有确认过的尺寸重量就不填（**不编造**）；
- **字典属性必须传 `dictionary_value_id`**：有字典 id 的属性不能再传文本值（Ozon 会拒），无字典的走 `values`；
- **绝不提交库存字段**：请求体过正则断言（`stock/stocks/inventory/warehouse`），违反直接抛错；
- **保守的重试策略**：
  * 明确的 HTTP 429/5xx → 按退避重试（Ozon import 以 `offer_id` 为键，属更新语义，重试不会多建卡片）；
  * **连接层异常（超时/断网）→ 绝不自动重试**：写请求的结果未知，盲目重试可能重复提交 ——
    如实返回"结果未知，请人工核对 import 任务列表"（回执里 `code=AMBIGUOUS`）；
  * 其它 4xx → 不重试；
- **回执**：`task_id` + 逐项 `{offer_id, product_id, status, errors}`，
  状态取 `processing`（import 返回任务号，最终结果需查 `/v1/product/import/info`，该查询是只读的）；
- **上线前的检查路径**：`--show-request` 打印将发送的请求体（**不联网**）→ 人工核对 →
  `--send --store <店铺> --i-understand-this-hits-ozon` 小批量试单；runner 里用 `--uploader ozon-api`
  必须同时给 `--execute-upload` 与 `--i-understand-this-hits-ozon`，否则直接拒绝启动；
- ⚠️ 请求字段按 Ozon 官方 v3 文档构造，**未用真实凭据验证过**：拿到凭据后先 `--show-request` 核对字段，
  再小批量试单（HANDOFF §5.4 有验收步骤）。
### 提交后的终态跟踪（ozon_status）

Ozon 的 import 只给任务号，最终结果要另查一次。所以提交之后：

- `upload_product()` 在拿到 `task_id` 后**自动只读确认一次**（uploader 支持 `confirm` 时），
  按次数/间隔轮询 `/v1/product/import/info` 直到所有条目进入终态或超时；
- 结果写三处：`output/store-publications.json`（**逐 SKU 终态 + ozone product_id**）、
  `output/store-runs/<店铺>/import-info.json`（每次轮询的历史与计数）、`ozon-result.json`（状态改为 `created`/`failed`/`processing`）；
- **确认是只读的**：`api_writes` 不会因为确认而增加（有测试守着"提交 1 次写 + 确认后仍是 1"）；
- **确认失败不会丢掉"已提交"这个事实**：`task_id` 已入台账，只是多一条告警；
- 也能事后手动确认（不需要重新提交）：`python -m pipeline.ozon_status --product-dir <商品> [--store <店铺>]`，
  加 `--fixture` 可离线演练；`doctor` 会显示每家店的 `imported / failed / pending` 与 task_id。



**为什么不用上游的 `pricing-result` 契约**：那份契约绑死了作者本机的 Excel 运费表
（`worksheet` 常量 `RETS`、`exchange_rate.source` 常量 `RETS!P2`、`workbook_sha256`）——
没有那张表就不该编一个 sha256 假装读过。所以我们定义自己的两个契约
（[workbench-pricing-result](ozon-workbench/contracts/workbench-pricing-result.schema.json)、
[workbench-measurements](ozon-workbench/contracts/workbench-measurements.schema.json)），
并保留"以后接上游运费表"的位置。

- **费率与策略可配置**：`config/pricing.json`（没有就用内置默认值并告警）——
  汇率、类目佣金、物流佣金、收单/提现费、每公斤运费与最低运费、包装费、
  体积重除数（默认 6000）、目标利润率、利润率下限、**价格上限**、取整与尾数（默认尾数 90）；
- **算法**：成本加成 —— `基础成本 = 采购价 + 运费(max(最低, 计费重×单价)) + 包装费 + 其它`；
  `售价 = 基础成本 / (1 − 目标利润率 − 总费率)`；计费重取"实际重 vs 体积重"较大者；
  裸价向上抬到好看价（1478 → 1490）；利润率不低于下限 → `UPLOAD`，低于下限 → `WARNING`，过低 → `REJECT`；
  **超过价格上限降级为 `WARNING`**（成本加成定价抬价会失去竞争力，这是真实风险）；
- **缺采购价就不报价**（`REJECT` + 错误），而不是拿 0 成本算出一个误导性的价格；
- **尺寸重量只接受确认值**：来自 `input/workbench-sku-overrides.json`（人在界面里填的）或
  采集里明确存在的结构化字段；没有就是"缺"，进 `measurements.warnings` 与上传 `production_blockers`，
  **绝不估算**；包装 < 商品本体会被判 `hierarchy_ok=false` 并阻断上传；
- 产物：`output/pricing-result.json`、`output/measurements.json`、`output/profit-analysis.json`（都过契约）。

### 定价与尺寸重量（measurements）
### 上线前预检（doctor）

```powershell
python -m pipeline.doctor --products-root products            # 人读报告
python -m pipeline.doctor --products-root products --json      # 机器读
```

它**不重复实现门禁**：每个商品的可提交性直接复用 `build_upload_payload()` 的 `production_blockers`（单一事实来源），
额外补充两类检查：

- **环境**：上游契约是否拉齐、`config/shops.json` 是否合法、哪些店铺启用、**凭据环境变量是否就绪**（只读环境变量，从不写密钥）；
- **商品结构**：12 项产物是否齐全（采集输入、选词、分析、文案、图片计划、生图报告、质检报告、类目、类目快照、
  最终属性、定价、尺寸重量、图片公网地址、发布台账），加上目标店铺、发布台账与幂等状态。

输出一份带「可提交 / 阻断项首条 / 下一步」清单的报告；退出码非 0 表示还有阻断。**预检是纯读的**（测试锁定了这一点）。

**HTTP 接口**（同一个服务，`uvicorn api:app --app-dir . --port 8766`）：

| 接口 | 说明 |
|---|---|
| `POST /api/collector/products` | 采集入库（重复 409） |
| `POST /api/collector/products/import-folder` | 文件夹导入 |
| `GET /api/collector/products[/{id}]` | 商品列表 / 详情 |
| `PUT/GET /api/workbench/products/{id}/keywords` | 选词（手动或从关键词库） |
| `POST /api/workbench/products/{id}/copy` | 生成产品分析 + 俄文文案（fake 可自检） |
| `GET /api/workbench/products/{id}/artifacts` | 产物清单 + 质检/属性/图片摘要 |
| `GET /api/workbench/products/{id}/publications` | 发布台账 + 分发计划（幂等状态） |
| `POST /api/workbench/products/{id}/run` | 跑流水线（默认干跑 + fake + 占位生图，绝不真提交） |
| `GET /api/workbench/doctor` | 预检报告 |

### M1：采集入库与素材文件夹

**采集素材文件夹约定**（都可缺省，命令行/`product.json` 提供元数据）：

```
capture-<offer_id>/
├── product.json          # 可选：source_url / title_zh / category / skus
├── main-images/          # 主图
├── sku-images/           # SKU 图
└── detail-images/        # 详情图
```

`import_folder()` / `POST /api/collector/products/import-folder` 会：校验（必须 1688 商品页、SKU 1–10 且都有采购价）→
按 offer 查重（重复返回 `DuplicateCaptureError` / HTTP 409，带 `open_existing` / `create_new_version` 两个选项）→
分配 `P######` → 写 `input/{source.json, raw-snapshot.json, source-manifest.json, category-selection.json}` + 复制图片 →
写 `output/` 与 `status.json(COLLECTED)` → 生成 `collection_id` 与 manifest sha256（供"采集绑定"用）。

**接口**：`POST /api/collector/products`（采集入库，`?allow_new_version=true` 显式新建版本）、
`POST /api/collector/products/import-folder`、`GET /api/collector/products`（可按 status 过滤）、
`GET /api/collector/products/{product_id}`（含 status / source / manifest）。

### 自检与实测结果

测试覆盖四部分：**M0 关键词库**（分位数打分、缺失指标、去重合并、状态流转、CSV 列名识别、HTTP 接口）、
**M1 采集入库**（校验、按 offer 查重与新建版本、图片复制去重、采集绑定 manifest sha256、与批次/runner 的联动）、
**M1 状态机与批次**（续跑分支、批次选择/去重/门禁、SKU 快照）、
**干跑 runner**（停在 `--until`、缺 handler/输入就停、**拒绝上传**、门禁失败转人工、`api_write_count == 0` 不变量）、
以及**数字解析**（`19,0` vs `1,234` 这类小数点/千分位歧义）。

`examples/run_demo.py` 的实测流程：① 文件夹采集入库（`P000001`，2 主图 + 1 详情图，`manifest_files=6`）→
② 再次采集同一 offer 被查重拦下并给出 `open_existing` / `create_new_version` → ③ 建批次并授权（冻结 SKU 快照 + 采集绑定 sha256）→
④ 干跑：`validate_source` 真跑完成，随后在 `product_analysis` 停下并报 `handler_not_implemented`，`api_write_count` 保持 0
—— 这就是"宁可停在半路，也不假装成功"的边界。

`examples/seerfar-sample.json` 的**实测结果**（该文件类目是占位值，请替换成真实 Ozon 类目）：
`created: 9`、`promoted: 1` —— 只有 `термос 500 мл` 达标（热度分位 0.8125、竞争分位 0.5625、score **0.475**）；
`термос 1 литр` score 0.3 但热度分位 0.5625 < 门槛 0.6，仍是 `candidate`；`термос без данных` 无指标 → `score=None`、状态保持 `candidate`。
默认门槛偏严是故意的：先调 `POST /api/keywords/score` 的 `min_heat_percentile` / `lam` 校准口径，再放宽。

关键约定（写死在代码里，避免"猜数据"）：
- 指标缺失 → `score=None`，**不参与**达标判定，也不会把人工入库/已用的状态自动回退；
- 打分按 `(category_id, type_id)` 分组算分位数，**跨类目不可比**；
- `category_id` / `type_id` 必填，入库接口直接 422 拒绝，防止类目维度糊掉。

## 仓库与许可

- 本仓库**不包含**上游作者的契约文件 `contracts/original/`（PolyForm Noncommercial 1.0.0，对外分发有许可问题）：
  用 `powershell -ExecutionPolicy Bypass -File .\contracts\fetch_contracts.ps1` 自行拉取；
- 仓库里**不含**任何真实密钥：`config/shops.json` 只写环境变量名，`.gitignore` 排除 `config/`、`.env*`、
  采集与导出数据（`*.xlsx`）、运行时产物（`products/`、`keyword-library/`、`output/`）；
- 本工作台自身的代码许可由你决定（当前未加 `LICENSE` 文件）。

## 决策状态

已确定：

1. **生图用豆包（火山方舟）** —— 已实现 `models/doubao_image.py`（参考图 + 提示词、900×1200 png 归一化、
   逐槽位容错、`--show-request` 先看不发）；
2. **Seerfar 实际列名已核对**（24 列全部识别，773 行零跳过）—— 已实现 `collector/seerfar_xlsx.py`；
3. **代码推到你的 GitHub 仓库** —— 本地仓库已建好（`git init` + 首个提交，95 个文件，
   `.gitignore` 已排除密钥/上游契约/业务数据），**只差远端地址与认证方式**。

仍待你确认：

1. **GitHub 仓库地址**（`owner/repo`）与认证方式：推荐你先跑 `gh auth login`，之后就由我推送；
   或者给我一个带 `repo` 权限的令牌（不推荐，令牌会出现在会话里）；
2. **文本模型**用哪家（`models/http_provider.py` 已就绪：任何 OpenAI 兼容端点都能接，
   包括火山方舟的 `https://ark.cn-beijing.volces.com/api/v3`）；
3. **对象存储**用哪个（只需上传图片并产出 `output/image-public-urls.json`，已有 `publish_urls` 生成映射）；
4. 本仓库要不要加 `LICENSE`（以及许可类型）。

