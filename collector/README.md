# 采集侧：关键词怎么进库

关键词库有三个入口，任选其一；**入库即自动打分**（按 `(category_id, type_id)` 分组算分位数）。

## 入口 1：Seerfar 页面适配层（浏览器控制台 / 油猴 / 扩展 content script）

1. 起服务：`uvicorn api:app --app-dir ozon-workbench --host 127.0.0.1 --port 8766`
2. 打开 Seerfar 的关键词页，把 `collector/seerfar_adapter.js` 的内容粘进控制台（或做成油猴脚本）
3. 设置类目并推送：

```js
ozonWb.WB.categoryId = "1001";     // Ozon 真实 category_id
ozonWb.WB.typeId = "2001";         // Ozon 真实 type_id
ozonWb.WB.categoryPathZh = "家居/厨房";
await ozonWb.collectAndPush();
```

**选择器要按实际页面校准**：默认按 `table / tbody tr / td` 抓，并优先用**表头文本**猜列（`columnHints`）。
如果抓不到，先 `await ozonWb.collect()` 看返回，再改 `WB.selectors`（`table` / `row` / `cell` / `headerCell`）。

## 入口 2：CSV / TSV 导入（不依赖 DOM，最稳）

```powershell
python collector/import_csv.py seerfar.csv --category-id 1001 --type-id 2001 --category-path-zh "家居/厨房"
```

列名按别名自动识别（`keyword/关键词/ключев/запрос`、`search volume/搜索量/показы`、`competition/竞争/конкурент` …），
识别不到的列**会被跳过而不是猜**。加 `--api` 改为 POST 到服务。

## 入口 3：直接调 API

```
POST http://127.0.0.1:8766/api/keywords/ingest
{
  "source": "seerfar",
  "category": {"category_id": "1001", "type_id": "2001", "category_path_zh": "家居/厨房"},
  "keywords": [
    {"keyword": "термос 500 мл", "search_volume": 12400, "competitor_count": 830},
    {"keyword": "термос для чая", "search_volume": 3100, "competitor_count": 120}
  ]
}
```

其余接口：`GET /api/keywords`（筛选）、`GET /api/keywords/export`（给标题生成取词）、
`POST /api/keywords/score`（调 λ / 门槛后重算）、`POST /api/keywords/status`（入库/排除/标记已用）、
`GET /api/keywords/categories`（按类目统计）。

## 字段映射与"不猜"约定

| 库字段 | 含义 | 缺失时 |
|---|---|---|
| `search_volume` | 热度：搜索量 | `score=None`，状态保持 `candidate`，`score_notes` 记原因 |
| `competitor_count` | 竞争：竞品数 | 同上 |
| `ads_count` / `cpc` / `trend` | 可选辅助指标 | 原样保留，不参与默认打分 |
| `category_id` / `type_id` | **类目主键，必须是 Ozon 真实类目** | 入库直接 422 拒绝 |
| `extra` | 数据源其它字段 | 原样保存，便于以后换算法 |

状态流转：`candidate` --达标--> `qualified` --人工确认--> `in_library` --用于某商品--> `used`；人工 `rejected` 不会被自动改回。
**人工状态（`in_library` / `used`）不会因为指标缺失或门槛变严被自动回退**，只有自动提升的 `qualified` 会回落。

## 注意

- Seerfar 是第三方付费站点，页面结构变动会让适配层失效，也可能受其使用条款约束；**只抓自己账号有权查看的数据**，并让这条链路可失败（抓不到就走 CSV）。
- 类目请用 Ozon 真实 `category_id` + `type_id`（后续 M4 会直接从 Ozon 类目接口拉，界面里选，不再手填）。
