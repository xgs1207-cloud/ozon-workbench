# 1688 / Ozon / Seerfar 采集 Edge 插件

从原项目 `jlcglobal/jlc-global-ozon-auto-listing` 复用，默认工作台地址为公网入口 `http://43.132.190.110:8088`（nginx 反向代理 + Basic Auth）。1688/Ozon 商品采集可沿用此入口；**Seerfar 市场报表因包含独立写入令牌，必须用 HTTPS 或本地 SSH 隧道**。

## 功能

- 在 1688 商品详情页一键采集：标题、供应商、SKU（含规格/采购价/SKU 图）、主图、详情图、详情页属性。
- 采集后在页面右侧抽屉里**选上架 SKU（≤10 个）+ 选最终 Ozon 类目**，确认后直接 POST 到工作台入库。
- 重复采集自动查重（同一 offer 已入库会提示，可选择建新版本）。
- 也支持采集 Ozon 参考页（竞品文案/图片），存到 `references/`。
- Seerfar 页面有独立 content script；在当前显示的报表列中采集，支持人工启动、最多 20 页的逐页入库与停止。

## Seerfar 报表采集到工作台

插件只读取你已登录的 Seerfar 页面里**当前显示的表格列**，不调用 Seerfar 付费 API。打开报表前，先在 Seerfar 勾选需要的指标列。单页最多读取 200 行；在弹窗中由你指定本次采集 1–20 页，插件会逐页等待表格更新、去重、上传，遇到分页结束、页面未更新或接口错误会停止并显示已入库数量。若页面上有多个分页器且无法确定报表所属分页器，插件会停止而不盲目点击。市场数据写入令牌是工作台单独配置的 `WORKBENCH_MARKET_INGEST_TOKEN`，**不是 Seerfar API Key**，请由管理员通过安全方式提供。

在 `https://www.seerfar.cn/admin/market`（以及 `.html` 形式）上，Seerfar 指标是**最近 30 天滚动窗口**。插件自动用本机时间生成采集月份 `YYYY-MM` 并发送 `period_kind=rolling_30d`；这个月份只是快照归档键，**不是自然月统计**。其他类目/商品报表仍需手填其真实自然月；如果它们也显示“最近 30 天”，不要冒填自然月，应先确认并扩展相应口径。推荐同月只计一次滚动快照。

当前公网入口是 HTTP，插件会拒绝把市场数据令牌发送到那里。请先在本机保持 SSH 隧道运行：

```powershell
ssh -i "D:\AI作图\ozonfinancedeploy.pem" -N -L 8766:127.0.0.1:8766 ubuntu@43.132.190.110
```

若本机 8766 被占用，可把上面 `-L` 的**左侧**端口改为 8767，再把插件工作台地址相应设为 `http://127.0.0.1:8767`。在插件「工作台连接」填 `http://127.0.0.1:8766`（或实际映射端口），点「保存并连接」；打开并刷新 Seerfar HTTPS 报表页，先选好指标列，再打开插件、填写工作台写入令牌，点「验证并保存令牌」。只有工作台返回有效验证响应时，令牌才写入此浏览器的 `chrome.storage.local`；下次打开插件会自动回填，即使后续报表表格识别失败也不用重输。它不会同步到其他设备、不会写入代码或仓库，清除时点「清除本机令牌」。**本机持久保存并非加密**，浏览器配置文件被他人访问时令牌仍可能被读取，请只在私人电脑使用。卸载插件或点清除后，本机副本会删除；但服务端令牌并不会因此撤销。服务端仍会逐次校验令牌，不能取消校验。

选择最多采集页数后，点「采集当前及后续页」；开始采集前会再次验证令牌。**保持弹窗打开**，失焦关闭会中断本次逐页循环；已成功入库的页不会回滚，可从浏览器当前所在页继续。每页最多 200 行，必要时按小于 1 MB 的批次上传，重复行会跳过。真实登录页面尚需在用户浏览器中做最终验收。

升级到 **0.4.28** 后，请到 `edge://extensions/` / `chrome://extensions/` 点击此已解压扩展的「重新加载」，并刷新已打开的 Seerfar 页面。HTTPS 自定义工作台地址会在「保存并连接」时请求该站点的访问权限。

## 安装（Edge / Chrome，加载已解压的扩展）

1. 打开 `edge://extensions/`（Chrome 是 `chrome://extensions/`）。
2. 右上角打开「开发人员模式」。
3. 点「加载解压缩的扩展」，选择本目录（`collector/edge-extension/`）。
4. 固定插件到工具栏。

## 配置工作台地址（只需一次）

工作台通过公网端口 8088 提供，带 Basic Auth。在插件弹窗里展开「工作台连接」：

- **工作台地址**填入带登录信息的完整地址：

  ```
  http://<账号>:<由管理员分发的密码>@43.132.190.110:8088
  ```

- 点「保存并连接」→ 应显示「已连接共享工作台」。

地址（含密码）只保存在本机浏览器的 `chrome.storage.local`，不会同步、不进仓库。
浏览器里直接访问工作台：`http://43.132.190.110:8088/`，登录信息向管理员获取，不要写进仓库。

市场报表入库会发送独立写入令牌，**不允许经公网 HTTP 发送**；请使用 HTTPS 工作台地址，或通过 SSH 隧道把工作台映射到本机 `127.0.0.1:8766`。原有商品采集不受此限制。

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
- `tests/seerfar.test.cjs` — 无账号的表格、地址、授权与安全门槛回归测试（`npm test`）。`npm run build` 检查现有 JavaScript 语法；旧 `src/*.ts` 源码不在本仓库，未依赖 TypeScript 编译。
- `popup.html` / `popup.js` / `popup.css` — 插件弹窗 UI。
- `background.js` — service worker：代理 HTTP 请求（注入认证头、绕开混合内容限制）、打开操作台标签页。
- `page-probe.js` — 注入页面上下文读取 `window` 里的商品数据。
- `category-tree.zh-CN.json`（6MB）— Ozon 官方简体中文类目树缓存（选类目用）。
- `category-rules-cache.json`（24MB）— 类目属性规则缓存（选类目后加载必填属性）。

> 两个缓存 JSON 是静态数据；如果 Ozon 类目有大更新，可重新从原项目拉取或让工作台提供实时接口（插件会优先调接口，失败才回退到本地缓存）。
