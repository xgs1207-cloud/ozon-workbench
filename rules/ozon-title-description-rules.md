# Ozon 俄文标题 / 简介 / 标签规则（M2 校验依据）

来源：原项目 `jlcglobal/jlc-global-ozon-auto-listing`（`main`，sha `55bc1522`）的 skill 与 schema，
逐条标注来源文件；分三类：**[官方]** 违反会导致 Ozon 校验/上传失败、**[项目]** 作者自定风格、**[未验证]** 未能确认。

> 用法约定：**[官方]** 必须落成代码校验（生成后不过就重试或转人工）；**[项目]** 做成可配置开关，默认跟随。

---

## 一、标题 `title_ru`

| 规则 | 值 | 类别 | 来源 |
|---|---|---|---|
| 长度硬区间 | 10–120 字符 | [项目]（Ozon 官方上限未在本仓库验证） | `templates/title-ru.schema.json` |
| 短标题 `short_title_ru` | 2–80 字符 | [项目] | 同上 |
| 必填字段 | `product_id`、`title_ru` | [项目] | 同上 |
| 来源证据 | `source_refs` ≥2 且唯一 | [项目] | 同上 |
| 核心词 | 单独字段 `core_keyword`，≥2 字符 | [项目] | 同上 |
| 语言 | 自然俄语 SEO；**禁止中文语序直译** | [项目] | `.agents/skills/ozon-ecommerce-designer/SKILL.md` |
| 结构 | 逗号分段、核心词在前；检测到「核心词, 核心词」重复时由确定性函数删除前段 | [项目] | `scripts/russian_seo_rules.py:379-390`（`remove_duplicate_core_title`） |
| 关键词位置/顺序细则 | —— | [未验证] | —— |

**SKU 差异（颜色/容量/数量）不许进标题正文**：颜色只进属性 `Название цвета`（attr **10097**），容量只进 capacity/size 字段。
颜色名内**不得**含容量、数字、单位、中文、拉丁字母、型号 —— 反例：`601-800 мл`、`卡其色1.9L`、`black 1000 ml`。
来源：`SKILL.md`「Ozon color-name fields」段、`scripts/russian_color_rules.py`、`is_russian_color_name`。

**主图文字**：不得把整条 listing 标题当大字块，但必须体现「产品类型名 + SKU 差异 + 1 条核心利益」。来源：`SKILL.md`。

---

## 二、简介 / 描述 `description_ru`

| 规则 | 值 | 类别 | 来源 |
|---|---|---|---|
| 五段结构 | `product_value / usage_scenarios / core_advantages / usage_method / notices` | [项目] | `templates/description-ru.schema.json` |
| 每段必填且 ≥10 字符 | 是 | [项目] | 同上 |
| 整段 ≥80 字符 | 是 | [项目] | 同上 |
| 段落证据 | `section_evidence` ≥5，section 枚举同五段 | [项目] | 同上 |
| 格式 | 纯文本字符串，**不得含 HTML 标签** | [项目] | 同上 |
| 长度上限（Ozon 6000 字符） | —— | [未验证] | —— |

**富文本是独立产物**（`templates/rich-content.schema.json`）：`format=ozon_rich_content_json`、`version: 0.3`、
widget ∈ `raShowcase / billboard`、block ∈ `{img, title, text}`；`textBlock.content` 用
`not: {pattern: "<[^>]+>"}` **显式禁止 HTML**；`img` 只接受 `https://` 或 `asset://`；`size ∈ size2-5`、`align ∈ left/center/right`、`color ∈ color1-2`。

**禁止编造**（品牌、认证、保修、海关编码、安全/承重、材质、功能、配件、数量）：`SKILL.md`「Do not invent brand, certification, warranty…」+ `AGENTS.md` 硬约束。
`disclaimer` 只是图片角色之一，**无证据时可以不用**。`Аннотация` 类描述属性不得写系统套话或首句摘要，须是真实多句俄语卡片摘要。

---

## 三、标签 / hashtags

| 规则 | 值 | 类别 | 来源 |
|---|---|---|---|
| 数量上限 | **30**（`maxItems=30`、`count 0–30`、`uniqueItems=true`；编译器截断 30） | **[官方]**（超 30 该校验即 FAIL） | `templates/ozon-tags.schema.json`、`scripts/russian_seo_rules.py:229-253` |
| 字符集 | pattern `^#[А-Яа-яЁё]+$` —— **仅西里尔字母**，无空格/连字符/数字/下划线 | [项目] | 同上 |
| 单标签长度 | schema 3–30；代码要求含 `#` 共 2–30（即最多 29 个字母） | [项目] | `HASHTAG_PATTERN` / `valid_hashtag` |
| 语言 | 常量 `ru`；多词直接拼成一个词（如 `#кружкадляавтомобиля`） | [项目] | 同上 |
| 去重 | 大小写不敏感；每个 tag 必须等于其 canonical 形式，否则整体 FAIL | [项目] | `validate_hashtag_set` |
| 禁止凑数 | `BANNED_GENERIC_TAGS`（`#товар`… 共 18 个）整条剔除 | [项目] | 同上 |
| 弱标签 | `WEAK_SINGLE_TAGS`（`товар/покупка/дом/кухня`… 约 39 个）不得单独使用 | [项目] | 同上 |
| 非法字符 | 含拉丁字母/数字/下划线的候选**整条拒绝**，不做"剥离非法字符"抢救 | [项目] | `canonical_hashtag` 注释 |

---

## 四、俄文书写规范

- **数字与单位**（`russian_seo_rules.py:179-227`、`_normalize_capacity_text`）：
  `N л`：N ≥ 10 且为整数 → `Nл`；否则 → `int(round(N*1000))мл`。
  `мл / ml / l / литр(а/ов)` 统一成 `NNNмл`。重量克数必须为正整数（小数进位）。 [项目]
- **大小写**：标签与搜索词全小写；标题与描述保留普通俄语大小写与标点
  （`canonical_search_keyword` docstring 明确：下划线规则**不适用于**标题/描述/图片文案）。 [项目]
- **颜色词标准写法**（`russian_color_rules.py` `_COLOR_MAPPINGS`）：`normalize_russian_color_name` 输出小写、`ё→е` 归一；
  例：卡其/haki→`хаки`、透明→`прозрачный`、银→`серебристый`、金→`золотистый`、灰→`серый`、米/beige→`бежевый`、棕/brown→`коричневый`。
  颜色字段只允许 1 个或多个自然俄语颜色词。 [官方]（10097 必须为字典值或自由文本的合法俄语色名）
- **语法性别/格**：仅通过 `TAG_REPAIRS` 例子体现（`кружка машины → кружка для автомобиля`、`работы → для работы`、
  `холодных напитков → для холодных напитков`），即需要 `для + 属格` 之类搭配；**没有形式化规则** → [未验证]（疑为作者偏好）。

---

## 五、官方硬约束汇总（可直接转成校验）

1. **tags ≤ 30**，且 `uniqueItems`、仅西里尔字母。
2. 上传前 `missing_required_attributes / invalid_values / errors` 必须为空才 `upload_allowed`
   —— `templates/ozon-upload-preflight.schema.json`。
3. SKU 变体**只能建在 `is_aspect=true` 的属性上** —— `ozon-adapter/ozon_adapter/variant_rules.py`。
4. 颜色名（attr 10097）须为自然俄语色名，**不得混容量或数字**。
5. 富文本必须是 `ozon_rich_content_json` v0.3 结构，图片须 `https://`。
6. 产品/包装重量与尺寸、品牌**不得虚构**（`AGENTS.md`）。

## 六、数据流约束（M2 实现时别踩）

`output/copy-ru.json` 是设计师原始文案（`title_ru / short_title / bullets_ru / description_ru / keywords_ru / hashtags_ru` …）；
而 `output/ozon-tags.json`、`ozon-draft.json`、`ozon-attributes-final.json` **只能由 field_completion 这一个出口生成**，
materialize 只做投影响应 —— **不得自造第二份标题/描述/标签**。
来源：`AGENTS.md`、`docs/audit-20260814-main-flow.md`「双写合并」条。

---

### 待补

- Ozon 官方标题/描述字符上限（仓库内只有项目自定值）→ 需要抓 Ozon Seller 帮助页或类目属性接口确认；
- 颜色词大小写是否有官方要求；
- 正式语法性别/格规则（目前只有 `TAG_REPAIRS` 例子）。
