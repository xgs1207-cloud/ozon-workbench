# 1688 / Ozon 采集 Edge 插件

从原项目 `jlcglobal/jlc-global-ozon-auto-listing` 复用，默认工作台地址已改为公网入口 `http://43.132.190.110:8088`（nginx 反向代理 + Basic Auth），**不再需要 SSH 隧道**。

## 功能

- 在 1688 商品详情页一键采集：标题、供应商、SKU（含规格/采购价/SKU 图）、主图、详情图、详情页属性。
- 采集后在页面右侧抽屉里**选上架 SKU（≤10 个）+ 选最终 Ozon 类目**，确认后直接 POST 到工作台入库。
- 重复采集自动查重（同一 offer 已入库会提示，可选择建新版本）。
- 也支持采集 Ozon 参考页（竞品文案/图片），存到 `references/`。
- Seerfar 页面有独立 content script。

## 安装（Edge / Chrome，加载已解压的扩展）

1. 打开 `edge://extensions/`（Chrome 是 `chrome://extensions/`）。
2. 右上角打开「开发人员模式」。
3. 点「加载解压缩的扩展」，选择本目录（`collector/edge-extension/`）。
4. 固定插件到工具栏。

## 配置工作台地址（只需一次）

工作台通过公网端口 8088 提供，带 Basic Auth。在插件弹窗里展开「工作台连接」：

- **工作台地址**填入带登录信息的完整地址：

  ```
  http://ozon:ozon2026wb@43.132.190.110:8088
  ```

- 点「保存并连接」→ 应显示「已连接共享工作台」。

地址（含密码）只保存在本机浏览器的 `chrome.storage.local`，不会同步、不进仓库。
浏览器里直接访问工作台：`http://43.132.190.110:8088/`，登录账号 `ozon` / 密码 `ozon2026wb`。

> 备选（SSH 隧道，离线调试用）：若仍想用隧道，地址填 `http://127.0.0.1:8766`，
> 隧道命令 `ssh -i "D:\AI作图\ozonfinancedeploy.pem" -N -L 8766:127.0.0.1:8766 ubuntu@43.132.190.110`。

## 采集 1688 商品

1. 浏览器打开 1688 商品详情页（`detail.1688.com/offer/<id>.html`），等页面完全加载（SKU 图都出来）。
   **插件是在页面加载时注入的，装完插件后要刷新（F5）一次。**
2. 点插件图标 → 确认标题、主图/SKU/详情图数量正常 → 点「采集当前商品」。
3. 页面右侧弹出 SKU 抽屉：勾选要上架的 SKU（≤10），在类目搜索框里选最终 Ozon 类目（等必填属性加载完）。
4. 点「确认采集」→ 插件把数据 + 图片 URL POST 到工作台，服务端自动下载图片入库。
5. 成功后自动跳转到操作台 `http://43.132.190.110:8088/?product_id=P000xxx`。

## 工作原理（与本工作台的接口契约）

| 插件调用 | 工作台端点 | 说明 |
|---|---|---|
| 测试连接 | `GET /api/workbench/summary` | 返回商品数 / 就绪店铺数 |
| 采集前查重 | `GET /api/collector/duplicates?source_url=...` | 同一 offer 是否已入库 |
| 采集入库 | `POST /api/collector/products` | 载荷含 `title_cn` / `main_images[]` / `detail_images[]` / `skus[]` / `ozon_category_selection`；服务端带 Referer 下载图片 |
| Ozon 参考页 | `POST /api/collector/ozon-reference-page` | 存到 `references/` |
| 打开操作台 | `GET /1688-collection` / `/ozon-reference` / `/command-center` | 303 跳转到 `/` |

请求头：`X-Factory-Device-Id`（区分设备）、`Authorization: Basic ...`（nginx 认证，来自地址里的用户信息）。

## 文件说明

- `manifest.json` — MV3 清单。
- `content.js` — 1688 / Ozon 页面采集 + SKU 抽屉 + 类目选择（核心，147KB）。
- `seerfar-content.js` — Seerfar 页面采集。
- `popup.html` / `popup.js` / `popup.css` — 插件弹窗 UI。
- `background.js` — service worker：代理 HTTP 请求（注入认证头、绕开混合内容限制）、打开操作台标签页。
- `page-probe.js` — 注入页面上下文读取 `window` 里的商品数据。
- `category-tree.zh-CN.json`（6MB）— Ozon 官方简体中文类目树缓存（选类目用）。
- `category-rules-cache.json`（24MB）— 类目属性规则缓存（选类目后加载必填属性）。

> 两个缓存 JSON 是静态数据；如果 Ozon 类目有大更新，可重新从原项目拉取或让工作台提供实时接口（插件会优先调接口，失败才回退到本地缓存）。
