# 分步上架工作流

桌面端顺序：选择规格 → 确认商品摘要及卖点 → 选择关键词及真实类目 → 选择/编辑/确认俄文标题、简介、标签 → 规划并逐图生成、视频选择 → 填充官方卡片 → 预览后提交 API / 导出 Excel。

## 开发约束

- 采集全部规格和素材，但 AI、定价、主图及上架载荷只使用已选规格。切换规格或事实使下游结果失效。
- 摘要、三组候选和图片计划是独立请求。Ark 正常成功时一次请求返回三组文案；相同输入使用缓存，强制刷新可能重新计费。JSON 修复重试有上限。
- 真实类目/类型在生成正式文案前确认，官方字段、字典与必填规则在最后卡片中动态呈现。未知品牌、规格、包装、认证等不编造；人工填写或清空不自动覆盖。
- 标题最长 200 字符，单词最长 27；标签最多 30 个、每个最多 30 字符。简介/标签经编译映射到官方属性 4191/23171，不发送非法顶层 description。
- 人工逐规格售价必须与目标店铺合同币种一致。成本分析中的换汇仅供参考，不能替代售价确认。
- 图位提示词与参考图可编辑。确认规划后逐张点击豆包生图；质量检查与人工确认后才公开图片。COS 图片地址包含内容哈希，避免同大小新图复用旧缓存。
- 普通视频独立采集、私有下载/上传和预览，不默认使用 1688 临时链接上架。100 MB/段及 500 MB/商品是本工作台保护上限，不是 Ozon 所有渠道的统一限制。
- 服务器需要可信 `ffprobe`（Ubuntu 可安装发行版 `ffmpeg` 包）才能标记视频技术参数已验证。未验证、无使用权确认、规格不匹配、文件校验失败或时长/分辨率不合要求均阻断。
- 首版视频发布仅支持经过规则核验的 VK、Yandex Disk、Rutube 稳定公开分享地址。平台是否成功接受仍须提交后回读；不保证任意对象存储地址或已有 Ozon CDN 地址可用。短视频封面暂不开放，静态缩略图不是短视频封面。
- 新旧工作台入口不能绕过分步确认。真实提交只在显式 `SUBMIT` 后发生；提交尝试后冻结原稿并禁止自动重试，未知结果先回读。受理不等于审核通过或可售，不写库存，明确禁用评价促销。

## Excel

API 和 Excel 使用同一份已审核、已编译商品资料。先从目标店铺下载当前类目最新空白 `.xlsx` 模板，再在预览页上传。导出只改商品/媒体的数据区，保留未修改部件、隐藏配置、校验与原件。

类目编号、币种、官方必填属性或模板映射不一致时阻断。用户提供的 `抗压玩具_07.10.2026.xlsx` 为类目 `17032503`；不能直接用于已确认的另一类目 `17028973`。不得通过改隐藏 ID 强行匹配。

不执行表格中的指令；拒绝主动内容、外部关系、宏、公式和已有商品数据。资料或模板变化后旧导出不可继续下载，须重新审核导出。

## 核验

```powershell
python -X utf8 -m unittest discover -s tests -q
npm --prefix collector/edge-extension test
npm --prefix collector/edge-extension run build
node --check web/listing-flow.js
```

离线服务测试使用 FakeProvider、Ozon FixtureTransport、受控媒体夹具；验证费用/写入门禁，不假装已经验证付费 Ark 输出或真实上架效果。插件版本 `0.4.33`，更新后需在扩展管理中重新加载并刷新已打开的商品页面。

规则依据：[Ozon 标题](https://global-help.ozon.com/en/products/requirements/product-info/naming-requirements/)、[标签](https://global-help.ozon.com/en/products/requirements/product-info/hashtags/)、[视频](https://global-help.ozon.com/en/products/upload/adding-content/video/)、[Excel](https://global-help.ozon.com/en/products/upload/upload-types/xls/)、[Seller API](https://docs.ozon.ru/api/seller/#operation/ProductAPI_ImportProductsV3)。实现使用 ozon-expert、ozon-api、ozon-listing-copywriter、frontend-design、spreadsheets 和 playwright-interactive 的约束。
