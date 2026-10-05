# contracts/ —— 从原项目搬来的数据契约

原项目 `templates/` 下有 **55 个 JSON Schema**，是整条流水线的数据契约。我们按阶段只需要其中一部分，
用 `fetch_contracts.ps1` 拉取（脚本按 **gh-proxy → jsDelivr → raw** 的顺序重试：

- `raw.githubusercontent.com` 在当前网络下基本不可达；
- `cdn.jsdelivr.net` 对 **>20 KB** 的文件会 302 跳到 raw（同样不可达）；
- `https://gh-proxy.com/https://raw.githubusercontent.com/...` 可返回全文。

```powershell
pwsh -File contracts/fetch_contracts.ps1
```

拉下来的文件放在 `contracts/original/`，按阶段对应关系如下（**不要改这些文件**，
要改就在 `contracts/` 下另建我们自己的覆盖版本）。

| 阶段 | 契约文件 | 用途 |
|---|---|---|
| M1 采集 | `collector-capture.schema.json`、`source.schema.json`、`source-manifest.schema.json` | 1688 采集入库载荷与商品原始输入 |
| M1 分析 | `product-analysis.schema.json`、`product-positioning.schema.json` | AI 产品信息总结产物 |
| M2 文案 | `ozon-ecommerce-design.schema.json`、`title-ru.schema.json`、`description-ru.schema.json`、`copy-ru.schema.json`、`keywords-ru.schema.json`、`key`…（见下） | 标题/简介/标签产物 |
| M3 图片 | `image-plan.schema.json`、`image-asset-contract.schema.json`、`image-qc-report.schema.json`、`ozon-upload-config.schema.json` | 图片槽位计划、生成契约、QC 报告、上传配置 |
| M4 上架 | `ozon-category.schema.json`、`ozon-category-attributes.schema.json`、`ozon-attributes-final.schema.json`、`ozon-attributes.schema.json`、`ozon-upload-payload.schema.json`、`ozon-upload-preflight.schema.json`、`ozon-result.schema.json`、`store-publications.schema.json`、`status.schema.json`、`batch.schema.json`、`batch-result.schema.json` | 类目、属性编译、上传载荷、回执、状态与批次 |
| 可选辅助 | `pricing-result.schema.json`、`cost-analysis.schema.json`、`profit-analysis.schema.json`、`variant-grouping-result.schema.json` | 定价/变体（如果后面要做） |

几个值得先读的：

- **`status.schema.json`**（11.6 KB）—— 商品状态机与断点字段的权威定义；
- **`ozon-ecommerce-design.schema.json`**（23 KB）—— 标题/简介/标签/图片方案的统一设计产物，M2 的核心；
- **`image-plan.schema.json`**（16 KB）—— 槽位体系与每张图的要求，M3 的核心；
- **`ozon-upload-payload.schema.json`** —— 真正提交给 Ozon 的请求体形状。

## 为什么要"搬契约"而不是"搬代码"

原项目的执行层（`run_batch.py` 192 KB、`collector_routes.py` 240 KB）耦合了 Codex CLI 调用、
作者私有知识库路径和 macOS 假设。契约层（schema）是干净的、无副作用的，先按契约对齐数据，
再逐块替换执行实现，改造成本最低。
