# 店铺授权与官方商品卡表单

## 官方接口依据

核对日期：2026-10-07。在线 Ozon 文档检索出现重定向，接口字段另对照 ozon-api skill 中 2026-10-03 核验的官方 OpenAPI 快照；账号可用性必须以真实请求为准。

- [官方授权说明](https://docs.ozon.ru/api/seller/#tag/Auth)：Seller API 使用 Client-Id 和 Api-Key。
- [密钥角色与有效期](https://docs.ozon.ru/api/seller/#operation/AccessAPI_RolesByToken)：`POST /v1/roles`。
- [真实类目树](https://docs.ozon.ru/api/seller/#operation/DescriptionCategoryAPI_GetTree)：`POST /v1/description-category/tree`。
- [类目字段定义](https://docs.ozon.ru/api/seller/#operation/DescriptionCategoryAPI_GetAttributes)：`POST /v1/description-category/attribute`。
- [字典可选值](https://docs.ozon.ru/api/seller/#operation/DescriptionCategoryAPI_GetAttributeValues)：`POST /v1/description-category/attribute/values`。
- [官方 OpenAPI](https://docs.ozon.ru/api/seller/swagger.json)。

接口返回字段定义，不是卖家后台的 HTML/CSS 模板。工作台据此组织必填/选填、官方分组、数据类型、多值上限、规格区分属性和字典选择器。中文来自官方 ZH_HANS 返回值，没有翻译的字段保留官方原文。

## 使用

1. 在电脑端侧栏进入「店铺授权」。公网 HTTP 禁止提交密钥；使用 HTTPS 或已经建立 SSH 隧道的 `http://127.0.0.1:8766`。
2. 从 [Ozon Seller API 密钥页](https://seller.ozon.ru/app/settings/api-keys) 获取凭据，仅在授权表单输入。选择能读取类目、属性及字典的最小权限；工作台检查实际返回的权限和有效期，Pro 会员不能代替 API 凭据。
3. 点击「验证并保存授权」。仅调用权限/类目读取接口，成功后启用店铺。此操作不会开启批次自动发布，不会提交商品。
4. 商品审核页面选择读取店铺，搜索并确认真实末级类目与商品类型。选品研究类目不可替代官方上架类目。
5. 官方表单随即出现，无需先调用 AI。商品共用字段可逐 SKU 覆盖；清空 SKU 覆盖表示该 SKU 暂不填写，恢复继承才使用共用值。
6. 点击字段的「读取官方可选值」，可以搜索或继续翻页。保存的是官方选项 ID 和原文，不能用自由文字冒充字典选项。
7. 草稿允许缺项；最终上架还必须经过已存在的审核、价格、尺寸、图片及预检门禁。修改类目会清除不再适用的旧属性确认，但保留材质等独立商品事实。

## 调用与安全

- 类目树和属性定义缓存 24 小时，按店铺、语言、类目、类型隔离；重新授权会清掉该店铺的元数据缓存。
- 字典只按需读取单页/搜索，默认每次 50 条，最多 100 条，不为展示表单下载所有品牌等大型字典。
- 界面不保存或回显密钥。注册表只存随机密文引用；凭据保存在 Git 忽略的保险库中，经 Fernet 加密。Linux 目录权限 0700、文件 0600；Windows 使用当前目录 ACL。密钥不会复制到商品数据。
- 环境变量方式仍可使用。`WORKBENCH_SHOP_REGISTRY_PATH` 和 `WORKBENCH_SHOP_VAULT_ROOT` 支持独立部署/测试路径。
- 备份时必须同时保留保险库 `master.key` 和 `*.fernet`，仅备份注册表不能恢复凭据；它们不能推送 GitHub。
- 文档与接口缓存用于减少重复请求，不保证平台永久不变，也不保证商品审核通过。没有实际发出上架请求时不能声称已成功上架。

## 当前边界

普通文本、整数、小数、布尔、单/多值字典、共用和逐 SKU 字段支持编辑。复合属性的官方定义完整保留并展示，但目前不支持复合组编辑和提交；必填复合属性会保持缺项并阻止继续发布，绝不扁平化丢失结构。账号、品牌或类目准入资格不能仅由「字段全部填写」推断。
