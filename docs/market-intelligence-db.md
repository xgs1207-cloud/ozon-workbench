# 选品市场数据库：数据源、入库口径与低调用费策略

## 已实现的底座

默认 SQLite 文件：`runtime/market-intelligence.sqlite3`，可用 `WORKBENCH_MARKET_DB_PATH` 改路径。`runtime/` 已忽略，不上传 Git。此库与现有 `products/` 素材目录及 `keyword-library/` 人工选词库分离；**采集快照不是自动入选关键词**。

表：`ingest_batches` 记录来源、数据集、采集方式、月份桶、统计口径、时间、页面及批次哈希；`observations` 保留每行原始 JSON、类目键、统计口径和去重哈希；`category_mappings` 预留 Seerfar 类目与 Ozon 上架类目/类型的**人工验证映射**。同一查询的 Seerfar“月搜热度”和 Ozon `client_count` 永不合并为同一个数字。来源、月份、口径和原始字段必须随评分结果一起展示。

有明确报表月时按来源/实体/月份/原始行去重；Ozon 热词等没有历史月份参数的接口按**抓取日**去重，保留跨日快照，但抓取日不伪装成报表月。

写入 API：`POST /api/collector/market-snapshots`；查询：`GET /api/market-data/stats` 与 `GET /api/market-data/observations?dataset=keywords&source=seerfar&period=2026-09`。三者均须请求头 `X-Market-Ingest-Token`，服务端设置环境变量 `WORKBENCH_MARKET_INGEST_TOKEN`。如果未设置，接口拒绝写入/读取。每批最多 200 行、1 MB；重复批次和重复原始行去重。**不要把 Seerfar/Ozon API Key 当作这个写入令牌。**

`period_kind` 标明统计口径：`calendar_month`（默认，适用于明确自然月报表）或 `rolling_30d`（最近 30 天滚动值）。滚动值的 `period=YYYY-MM` **只表示采集发生的月份桶**，并不表示数据覆盖这个完整自然月；采集时间以 `captured_at` 为准。推荐器对同一类目或词只沿用最新记录对应的一种口径，每月桶最多取一条，不把滚动值和自然月报表拼成三个月历史。月初、月末采集的滚动窗口仍可能重叠，`history_months` 仅表示不同月份桶数，不等于独立自然月样本数。

插件：打开 Seerfar 报表并等待表格加载 → 点击扩展 → 确认统计口径、采集月份桶和写入令牌 → “将当前可见报表页入库”。当前识别有表头的类目、关键词及带 SKU 的商品报表；只抓当前可见页，最多 200 行，不自动翻页、不访问站内隐藏接口。原始单元格字符串原样存储，页面路径去掉 URL 查询参数。页面改版后须重新核对表头和字段，未识别时不会猜测入库。公网工作台须先提供 HTTPS；扩展拒绝将这批数据和令牌发到公网 HTTP 地址。本机 `127.0.0.1` 可用于测试。

Ozon 官方 API 导入器只在加 `--execute` 时请求**一页**，不自动循环。优先读取环境变量 `OZON_CLIENT_ID` / `OZON_API_KEY`，也兼容服务器现有的 `OZON_DEFAULT_CLIENT_ID` / `OZON_DEFAULT_API_KEY`；密钥不写浏览器插件、不写数据库。示例：

```powershell
python -m market_intelligence.ozon_sync categories
python -m market_intelligence.ozon_sync keywords
# 核对 dry_run 输出、订阅权限和环境变量后，才添加 --execute
```

`categories` 请求 `POST /v1/analytics/category/comparison`：`MONTH`、三级类目、GMV 降序、至多 100 行；`keywords` 请求 `POST /v1/search-queries/top`：至多 50 行。两者都标注需要 **Premium Pro**；用户说的“Pro 会员”须在 Seller 后台或真实 API 403/200 响应中确认是否就是该档位。Ozon 搜索热词接口**没有指定历史月份的参数**，因此导入器不伪造报表月份，只记录抓取时间。Ozon 类目比较的 `MONTH` 是相对周期，不能直接和 Seerfar 的明确 `YYYY-MM` 当同一窗口。

## 应采集哪些数据

| 阶段 | Seerfar 优先字段 | Ozon API 可用交叉证据 | 用途和注意 |
|---|---|---|---|
| 类目 | 类目 ID/路径、跨境可售、销售额/销量及增长、平均价格、竞对/竞品/品牌数、转化集中度、退货取消率、季节性、重量/体积、统计月 | 类目比较 `id/label/metric_gmv/metric_gmv_growth/metric_items/metric_sellers/metric_brands/metric_leader_share/metric_buyout` | 看需求规模与稳定、卖家拥挤度、头部垄断、退货风险；Ozon 三级分析类目不等于上架用 `description_category_id/type_id` |
| 关键词 | 原俄文词、Seerfar 类目 ID/名称、月搜热度、月搜增长、竞对数、竞品数、商品数、加购/转化、集中度、退货取消率、均价、统计月 | 热门词 `query/client_count/sellers_count/items_views/add_to_cart/conversion_to_cart/avg_price`；自有商品词接口仅作店铺验证 | Seerfar 热度与 Ozon 搜索用户数是不同口径；关键词可能跨类目，先归入“待映射”，再关联已验证类目 |
| 商品/SKU | Ozon SKU、类目、卖家/品牌/销售方式、售价、销量/销售额及增长、评价数/评分、退货取消率、上架天数、统计月 | 自有商品 `/v1/analytics/data`、`/v1/analytics/product-queries`；公开类目/热词接口不能替代竞品 SKU 明细 | 找供给空档及真实需求，保留 SKU 粒度；1688 成本与物流成本后续才计算毛利，不把销售额当利润 |

Seerfar 官方开放文档中的类目明细、市场词、商品报表分别为 `/open-api/category/detail/search/ozon`、`/open-api/market/search/ozon`、`/open-api/productReport/search/ozon`，商品详情 `/open-api/product/detail/search/ozon`。开放文档未给出一个可保证覆盖全站的“所有类目清单”接口；先用已有导出/插件发现候选类目，再按 ID 补 API 历史，不能假设类目报表接口能无成本全站扫描。其公开示例价目列出的前三类报表约 10 积分/请求、商品详情约 3 积分/请求；**实际扣费以账户平台显示为准**。本版没有自动调用 Seerfar 付费接口。

## 筛选逻辑（尚未自动评分）

1. **数据完整性**：至少 3 个可比月才给“稳定需求”分；不足则标记“证据不足”，不能填零。类目须人工核实 Ozon 真正的上架类目与跨境可售；关键词须保留俄文原词。指标分别标注来源、统计月、单位、是否估算。类目规模、关键词热度要在同类目/同来源/同月份比较，不跨来源直接相加。
2. **类目排序**：需求规模（销售额与销量）、近 3–6 月趋势及波动、竞争密度（卖家数/商品数相对需求）、头部集中度构成主分。用户偏好的“中国直发”场景里，退货取消率做**扣分项**，不是一票否决；重量/体积与履约路径暂不进主评分。季节性单独标识，避免旺季一个月冲高误判为稳定。推荐用各指标的类目内分位数排序，初始权重需回测后调整，不凭空设硬门槛。
3. **关键词排序**：仅在已入围类目中筛；先看持续热度/增长是否稳定，再按搜索需求 ÷ 同类目竞争供给、转化与头部集中度排序；退货取消率轻度扣分。Ozon 热词如果能匹配到同一俄文词，只增加“平台验证”置信度，不把 `client_count` 填到 Seerfar 的“月搜热度”。同一个词可属于多个类目，分开评价。
4. **商品机会**：只查入围类目、入围词关联的商品 SKU；用销量/销售额趋势、评论门槛、评分、上架时间、卖家类型和退货率辨别“有需求但供给未固化”。先人工选择规格和 1688 货源，成本/售价/平台费完整后才能算是否值得上架。
5. **复核**：结果必须能回点原始快照和批次；旧数据过期、样本覆盖不足、Seerfar 与 Ozon 趋势矛盾时降置信度，而不是悄悄给出确定推荐。

## 控费顺序

先读现有 XLSX 和插件可见报表 → 用 Ozon Premium Pro 的**单页**类目/热门词验证（若会员确有权限） → 只对前 10–20 个候选类目查询 Seerfar 多月明细 → 只对这些类目分页查词 → 只对最终少数 SKU 查商品详情。所有请求按 `source + endpoint + filters + period + page` 做缓存，月报每月更新、商品候选每周或人工触发更新；每个任务设置页数上限、预计积分、预览和人工确认。真实扣费接口尚未接入，不会后台自动运行。

依据：[Seerfar 开放 API 文档](http://doc.seerfar.cn/api-docs.html?lang=zh-CN)、[Ozon Seller API](https://docs.ozon.ru/api/seller/)、[Ozon 对自有商品搜索词接口的说明](https://dev.ozon.ru/news/512-Novye-metody-dlia-raboty-s-analitikoi-po-zaprosam-tovarov-v-Seller-API/)。

## 研究工作台（2026-10 版本）

`GET /api/research/categories` 和 `GET /api/research/keywords?category_key=...` 从已入库的 Seerfar 月度快照计算推荐；Ozon 热词仅作独立印证，不把两家的热度加在一起。页面 `/` 显示推荐依据，是唯一工作台；`/advanced` 已退役并返回 410。筛选参数在 `GET/PUT /api/research/config`：默认至少 3 个不同统计月份，类目权重为稳定度 35%、竞争 35%、需求 20%、趋势 10%；关键词为稳定度 30%、竞争 30%、需求 25%、转化 15%。退货取消率仅扣分，类目的季节性系数另有最多 8 分的透明扣分。跨境不可售的记录不进入推荐。

竞争密度同月同源计算：类目用 `竞品数 / 销量`，关键词用 `竞品数 / 月搜热度`；不把“竞对数”与“竞品数”相加，也不拿商品数量除以卢布 GMV。原始字段和证据行 ID 保留，可追溯到 `observations`。Seerfar 关键词单元格的第二行中文翻译只作阅读辅助，上架关键词取第一行俄文原词。实际导出的类目表含 22 列、市场词表含 24 列；`转换集中度/转化集中度` 两种列名均识别。

已有 XLSX 可通过 `python -m market_intelligence.import_seerfar_xlsx --db runtime/market-intelligence.sqlite3 --period YYYY-MM --categories <类目表.xlsx> --keywords <市场词表.xlsx>` 入库，不调用付费 API。`--period` 必须填**数据的统计月**，不是导出日期；若文件是“最近 30 天”滚动窗口，不应伪装成某个自然月以满足 3 个月门槛。单次导出的两张 200 行表只有一个时间窗口，因此会显示候选和初筛分，但不会把“需求稳定”当成已证实，也不会开放推荐批次创建；需补足不同统计月的历史快照，或由用户明确调整门槛并接受有限置信度。

研究批次 `POST /api/research/sessions` 冻结研究类目、一个主词和至多 10 个辅词；一个批次可关联多件 1688 采集商品。每件商品独立确认 SKU、Ozon Seller API 真实末级类目、事实、尺寸重量、逐 SKU 售价、俄文文案、图片计划与参考图、整套图片和 Ozon 必填属性。火山方舟文本模型经 `guided/prepare` 触发，生图按图位手动触发以控制费用。审核许可绑定输入与产物摘要；改动后旧许可失效。

自动发布须同时满足服务器 `WORKBENCH_AUTO_PUBLISH_ARMED=1`、批次显式开启、所有商品审核确认、COS 图片 HTTPS 发布、production 载荷和只读预检通过。同一商品仅自动尝试一次 Ozon 写入；结果不明或失败时转人工，不盲目重试。**此处提交的是商品卡导入，不代表审核通过或有库存可售**。没有正式 Ozon 沙盒，部署测试不得使用真实写接口。
