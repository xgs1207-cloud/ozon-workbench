# 1688 视频 → Ozon 卡片：能力与验收边界

## 数据与步骤

1. 插件 0.4.34 采集当前商品的视频 DOM/JSON/开放 Shadow DOM；网络资源仅做被动补全，必须能匹配当前商品的播放器 ID 或原地址。广告、推荐、直播和未确认归属资源不入库。不主动播放/请求视频，不提取浏览器凭据。
2. 后台明确获取视频或上传有使用权的原 MP4/MOV。源地址只私有保存。实际文件探测、哈希与选中规格校验通过后，用户确认使用权、俄文视频标题和规格关联。
3. 显式发布到 COS，按内容哈希复用并检查匿名访问；公开地址和源文件/哈希绑定保存。这里的成功只表示对象存储已验证，未表示 Ozon 接受视频。
4. 正常商品卡请求把视频放入 `/v3/product/import` 的 `complex_attributes`；每个规格只取通用视频或该规格的显式覆盖，不取其他规格的视频。视频 URL 的属性是 `21841`，标题是 `21837`，二者 `complex_id=100001`。
5. import 终态单独记录。创建卡片成功仍将视频标为需要回读；不会借轮询再次提交。
6. 用户显式执行只读回读，仅请求台账里当前店铺的固定货号：`/v4/product/info/attributes` 与 `/v3/product/info/list` 各一次。GET 仅返回本地缓存，无网络调用。已提交的只读权限不等于重新开放编辑或提交。

## 官方接口依据

依据 [Ozon Seller API 官方文档](https://docs.ozon.ru/api/seller/#operation/ProductAPI_ImportProductsV3) 的 2026-10-03 官方 OpenAPI 本地快照，普通商品视频为 MP4/MOV、8 秒至 5 分钟、每个货号最多 5 段。普通视频 `100001/21841/21837` 与短视频封面 `100002/21845` 分开；静态 poster 不是短视频封面。

2026-10-08 尝试抓取官方在线 API 与 Seller Education 媒体规则页面时出现循环跳转，未能重新验证在线正文。因此本轮以已保存官方快照确认请求和响应结构，不声称已经实时核对到规则的任何更新。工作台的 100 MB 文件上限是本产品安全限制，不冒称 Ozon 文件大小上限。

`/v4/product/info/attributes` 的官方响应是平铺的 `complex_attributes[]`，每项有 `id`、`complex_id`、`values[]`；实现同时识别导入式分组结构，视频 URL 和标题按同组、同数组位置对账。`/v3/product/info/list` 的商品处理/审核状态不是独立视频处理终态接口。没有证据时不会生成“视频审核通过”结论。

## 回读状态契约

`POST /api/workbench/products/{id}/guided/readback` 请求 `{ "store": "店铺ID" }`；`GET` 同路径带 `?store=...` 只读已缓存报告。返回 `{ok, report, api_writes_performed:false}`。

`report.media_readback` 包含 `status`、`video_expected`、`video_readable`、`items[]`，每项是精确货号、源 SKU、期望/实读数量、视频标题/地址对账与 `pending`/`failed`。状态为：

- `not_selected`：提交快照未选普通视频。
- `expectation_unknown`：缺少原提交快照，不能据此说未选视频，也不能完成视频对账。
- `processing_or_not_yet_readable`：未读回或商品仍在处理，只建议手动只读刷新。
- `readable_in_api`：接口读回同一提交地址、标题与数量；不是买家页播放证明。
- `rehosted_in_api_unverified_identity`：Ozon 地址、标题和顺序匹配，但仅这些信息不能证明源视频字节相同。
- `failed` / 单项 `readback_mismatch`：平台视频错误或对账不一致，不自动重提。
- `readback_failed`：只读网络/授权失败，无自动重试或提交，错误正文和签名参数不返回前端。

没有台账货号时不调用 Ozon。缓存绑定商品/店铺/货号/提交快照指纹；快照变化、异店缓存、重复或其他货号响应不能作为完成证据。`buyer_playback_verified` 和 `storefront_verified` 始终为 false，需要人工在真实商品页确认。

## 本轮验证

新测试使用真实私有视频存储、COS 发布服务、商品请求编译与回读服务，COS/Ozon 均为内存夹具。覆盖视频按 SKU 绑定、重复发布复用、延迟未读回、官方平铺和分组复杂属性、错误/超时、错货号、缓存陈旧、已发布只读以及源数据不变。没有真实 COS/Ozon 写入、付费模型调用、新上架或库存操作；没有声称已验证真实 PDP 视频可播放。
