# Ozon 图片槽位 / QC / 上传规则（M3 校验依据）

来源：原项目 `jlcglobal/jlc-global-ozon-auto-listing`（`main`，sha `55bc1522`）。
分三类：**[官方]** 违反会导致 Ozon 侧失败、**[项目]** 作者自定风格、**[未验证]** 未能确认。
行号为近似值（大文件抓取有截断）。

> 用法约定：**[项目]** 的硬数字（3:4、900×1200、N+8、90/75 阈值）我们**保留为默认值但做成配置**，
> 因为它们是作者经验值而非 Ozon 官方原文；**[官方]** 项必须是硬门禁。

---

## 一、槽位体系（三层 role，别混）

| 层 | 取值 | 来源 |
|---|---|---|
| **slot id** | 主图 `main-<source_sku_id>` 或 `main-{序号:03d}`；详情/免责统一 `detail-{序号:03d}`（`detail-001`…`detail-008`）。调度器用 `startswith("main-")` 判主图 | `scripts/image_planner.py`（slot 赋值段）、`scripts/image_slot_scheduler.py:≈L78` |
| **参考图 id** | `main-001` / `detail-*` / `sku-001`；排除机制认 `main-004` 式 id | planner `usable_reference_images` |
| **referenceImage.role** | `main` \| `sku` \| `detail` \| `disclaimer` | `templates/image-plan.schema.json:≈L150` |
| **image_type（业务角色，10 种）** | `main, benefit, feature, scene, usage, problem_solution, detail, size_spec, comparison, disclaimer`，各带 `decision_job` + `required_composition` | `scripts/image_generator_contract.py:≈L22-64` |
| **layout_type** | `sku_main` \| `core_benefit` \| `structure_callout` \| `usage_scene` \| `sku_comparison` \| `purchase_notice` | schema `≈L200` |
| **overlay role** | `benefit` \| `specification` \| `callout` \| `notice` \| `brand_watermark` | schema `≈L285` |
| **上传侧 role** | `variant_main` \| `detail` \| `color`；颜色图 slot `color-{sku_id}` | `ozon-uploader/ozon_uploader/images.py:≈L120-200` |

⚠️ 仓库里**没有 `color_sample` 角色**；颜色只在上传侧作为 color 图追加，且优先复用同 SKU 主图（`images.py:≈L155`）。

## 二、数量规则

**准确表述**：已选 SKU 数 N ∈ 1–10，**每个 SKU 恰好 1 张独立主图 + 整套恰好 8 张共享详情图 = N+8 张**。
N=1 → 9 张，N=10 → 18 张；**主图顺序必须与选中 SKU 顺序完全一致**。
来源：`rules/image-rules.md:≈L20-26`、`templates/image-plan.schema.json`（main `maxItems=10`，detail `minItems=maxItems=8`）、
`image_generator_contract.py` 的三处硬断言。

例外与坑：
- **disclaimer 不是第 9 张**（原文：*a commercial detail role, not a ninth image*），仍留在 `detail_images` 内以保持 8 张 —— `image_planner.py` 注释；
- 单 SKU 只用 5 个 layout（**禁 comparison**），多 SKU 才用全 6 个；
- 8 张详情的角色是**可选集合**，禁止强制"固定第 8 张免责图" —— `.agents/skills/ozon-ecommerce-designer/SKILL.md`；
- 共享详情图只能使用**所有已选 SKU 共有的事实**；SKU 差异只允许用一张真实原图**确定性合成** —— `rules/image-rules.md:≈L24`。

## 三、各角色的设计目的与提示词要点

- **main（SKU 主图）**：3 秒内答完"是什么 / 什么场景 / 本 SKU 规格 / ≥1 个真实购买理由"；3:4；商品约占宽 45–65%、高 50–70%；
  标题通常 ≤2 行；**商品必须是最大视觉区**；文字只留 ≤1–2 条有来源证明的卖点 + 小号 JLC GLOBAL 水印；
  **禁止**把整段 listing 标题 / SKU 名 / 型号做成大字块。
  来源：`references/product-specific-image-standard.md:≈L18-42`、`image_generator_contract.py:≈L68-160`。
- **detail ×8**：形成购买决策顺序（认知 → 用途 → 结构材质 → 尺寸步骤 → SKU 选择 → 真实场景 → 近摄 → 购买提醒），
  每张回答不同的购买问题，**不能只换背景**。
- **size_spec**：只用商品本体尺寸（`source_field` 固定为 `cost-analysis.product_dimensions`），估算必须标 `Примерные размеры`，
  **禁包装尺寸** —— `image_generator_contract.py:≈L195-205`。
- **deterministic_image_types = ["comparison","size_spec"]**：必须真实原图确定性合成，**禁 AI 重绘** —— `rules/image-rules.md:≈L40-45`。
- **disclaimer**：说明已确认的限制以降退货；**不得把 unknown 写成负面声明**。
- **叠字**：**禁止后置叠字**。`overlay_strategy=single_pass_model_native_typography`、`post_generation_overlay_forbidden=true`、
  `empty_placeholder_panels_forbidden=true` —— schema `≈L63-105`、contract `≈L230`。
- **文字白名单**：只渲染 `russian_text` / `overlay_plan` 里的精确字符串；禁中文 / 乱码 / 供应商水印 / 浏览器界面 / product-id 徽章。
- **构图参数**：无固定像素边距，由 `art_direction.negative_space` / `composition` 决定；overlay box 为归一化 0–1 四元组；
  `font_size_ratio` 0.014–0.12；palette 3–5 色；`product_scale_percent` 20–85；
  水印 `margin_ratio 0.035 / max_width_ratio 0.18 / opacity 0.24 / bottom_right`（`config/image-watermark.json`）。
- **照片级硬要求**：必须像卖家实拍（材质纹理、景深、环境光、软阴影），**禁 3D/CGI/矢量/插画**；
  SKU 主图默认 `generate_from_reference`，用 SKU 自身参考图锁结构·颜色·比例。

## 四、参考资料怎么用（fact lock，不是可复制画布）

- SKU 主图的优先级：**SKU 自身参考图 = 身份锁**；同品主图 / 详情图 = 结构·使用·尺寸·场景证据 —— `image_generator_contract.py:≈L340-360`。
- 只认可 `input/main-images`、`input/sku-images`、`input/detail-images` 内、且在本集合 manifest 注册过的图；
  `output/`、历史/其他商品、`test-data/` 一律禁止 —— `rules/image-rules.md:≈L12-16`、`scripts/image_asset_boundaries.py`。
- 操作枚举：`generate_from_reference`（默认）/ `edit_real_image` / `compose_from_real_images` / `needs_human_input`。
- **Ozon 竞品参考图任务**（`$ozon-image-prompt-reverse`）**只允许**影响相机手感、镜头距离、光线、背景真实度、构图节奏、卖家照片瑕疵；
  **禁止继承**竞品品牌·店名·水印·型号·认证·包装·配件·原文案·尺寸重量材质功能；该任务文件缺失**不得阻塞产线**。
  混变体图可用 `output/image-reference-exclusions.json` 排除（audit §23 的 `main-004` 案例）。
  SKU 参考图 <600px 时只锁变体与颜色，需配同品高清主图（audit §22）。
- **1688 原图禁止直接上传**：`raw_1688_image_direct_upload_forbidden=true`。

## 五、QC 规则

**文件级**（`rules/image_qc_rules.json:≈L25-40`、`scripts/image_qc.py`）：
PNG 头必须可读；**3:4（容差 0.002）**、宽 ≥900、高 ≥1200、仅 png。
比例错 → `aspect_ratio_mismatch` **critical + reject**；分辨率不足 → medium/revise；不可读 → `image_file_unreadable` critical。

**检查项**：5 维加权 30/25/20/15/10 共 17 个 criterion（product_identity 12、color_consistency 6、structure_consistency 8、
accessory_consistency 4、explicit_sales_purpose 8…）；**每项必须有 evidence**，扣分与 status 必须一致，总分不得手填。

**阈值**：≥90 `pass`（仅推荐人工审核，**≠ 批准上传**）；75–89 `revise`；<75 `reject`；**任一 critical 一律 reject**。

**critical failure 清单**：规则文件 12 项 —— `image_file_unreadable`、`aspect_ratio_mismatch`、`source_preflight_missing`、
`source_reference_too_small`、`product_identity_changed`、`wrong_sku_or_color`、`product_structure_changed`、
`unverified_accessory_added`、`false_parameter_claim`、`chinese_text_present`、`garbage_text_present`、`russian_text_unreadable`；
代码另追 `product_pixel_lock_missing` / `product_pixel_lock_failed`。

**回退**：`revise` = 只重试未通过图位，已通过图不动；`reject` = 按失败图位回退图片计划，**禁止整套重生成**。
**重试上限**：每图位 1 次初始 + 1 次定向重试（`max_attempts=2`）；服务不可用 / prelaunch failure **不消耗**重试；
某波出现终态失败则不再开后续波；**通过槽位永不重跑**；计数写 `status.json.image_slot_retry_count_by_slot`。
**并发**：`image_slot_concurrency` ≤3；图片通道 ≤4。
另有空白占位面板检测（低方差 + 四边对比）与 HSV 颜色占比测量工具（后者在 audit §24 已撤销为硬门）。

## 六、上传侧图片要求（我们要替换的部分）

- **公网可访问**：原项目做法是本地静态服务 + **Cloudflare quick tunnel**（`*.trycloudflare.com` 属于 `temporary_host_suffixes`，
  `exact_hosts` 仅 `ir.ozone.ru`）—— `rules/ozon-image-cdn-domains.json`、`ozon_uploader/images.py`。
  → **我们的替换点：直接上传到你已有的对象存储，返回 HTTPS URL，不再开隧道。**
- **时效**：固定 TTL 24h（`DEFAULT_IMAGE_CHANNEL_TTL_SECONDS=24*60*60`、`close_policy=fixed_ttl`），单商品一通道，全局并发 4，启动重试默认 3 次。
  → 换对象存储后，"24h 时效"这一约束自然消失（我们的 URL 长期有效），但要保留"URL 可访问性校验"这一步。
- **张数与顺序**：按 `draft["images"]` 顺序展开，staged 名 `{序号:02d}-{role}.png`；水印在 staging 本地叠加（Pillow，
  role ∈ [variant_main, main, detail, color]）；上传前从当前 `image-plan.json` + 真实文件**即时重建图片完整性 gate**；
  相同 sha256 已上传 → skip。
- **通道重建**：公网 URL 校验失败 → 停旧通道并新建；仅本机 TLS 探测失败记 `local_tls_probe_unavailable`，不误判；
  校验结果缓存 `image-public-validation-cache.json`。
- dry-run 默认；production 必须 `UPLOAD_MODE=production` **精确值**；颜色图回退链
  `SKU image → 视觉校验主图 → QC 通过图 → missing`，**missing 阻止 production**。

## 七、官方硬约束 vs 项目自定

**[项目]（保留为默认值，做成配置）**：3:4、≥900×1200、仅 png、N+8、恰好 8 张详情、水印参数、90/75 阈值、重试/并发/TTL 24h、8 张角色可选集。

**[官方]（仅二阶证据，但必须当硬门禁）**：图片必须**公网 HTTPS 可访问**（Ozon 按 URL 拉取）；
中文文字、水印、比例错、俄文不可读这些被项目视为 Ozon 侧硬失败。

**[未验证]**：仓库未引用 Ozon 官方图片规范原文 —— 官方最低分辨率 / 最大张数 / 格式白名单**未验证**。

---

### 待补 / 已知缺口

- `.agents/skills/image-generator/SKILL.md` 只取到 base64（本会话无法解码），其约束以 `scripts/image_generator_contract.py`
  与 `docs/audit-20260814-main-flow.md` §22–27 为准；
- `scripts/image_planner.py`（97 KB）超出单次抓取上限，slot 赋值段行号为近似值；
- Ozon 官方图片规范原文（分辨率/张数/格式）需要另行确认。
