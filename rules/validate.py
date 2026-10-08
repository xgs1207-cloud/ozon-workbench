"""从原项目规则提炼的**可执行校验**（M2 用）。

文档在 ``rules/*.md``，这里是代码。原则：文档里标 [官方] 的当硬门禁，
标 [项目] 的做成默认值但可配置，[未验证] 的不实现（宁缺毋滥）。
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Sequence

from parsing import parse_number

#: Ozon 标签上限（官方硬约束）
MAX_HASHTAGS = 30
#: 标签字符集：仅西里尔字母、无空格/连字符/数字/下划线（项目规则）
HASHTAG_PATTERN = re.compile(r"^#[А-Яа-яЁё]+$")
#: 通用凑数标签（原项目 BANNED_GENERIC_TAGS 的部分摘录，可按需扩充）
BANNED_GENERIC_TAGS = {
    "#товар", "#товары", "#покупка", "#магазин", "#скидка", "#распродажа",
    "#хорошийвыбор", "#качество", "#дешево", "#лучший", "#топ", "#новинка",
    "#подарок", "#акция", "#выгодно", "#рекомендую", "#musthave", "#тренд",
}
#: 不许单独使用的弱标签（原项目 WEAK_SINGLE_TAGS 的部分摘录）
WEAK_SINGLE_TAGS = {
    "товар", "покупка", "дом", "кухня", "вещь", "штука", "подарок", "качество",
    "магазин", "цена", "выбор", "набор",
}
#: 颜色词标准化（含中文/英文 → 俄语）
COLOR_MAPPINGS: dict[str, str] = {
    "黑色": "чёрный", "黑": "чёрный", "black": "чёрный", "чёрный": "чёрный", "черный": "чёрный",
    "白色": "белый", "白": "белый", "white": "белый", "белый": "белый",
    "红色": "красный", "红": "красный", "red": "красный", "красный": "красный",
    "蓝色": "синий", "蓝": "синий", "blue": "синий", "синий": "синий", "голубой": "голубой",
    "绿色": "зелёный", "绿": "зелёный", "green": "зелёный", "зелёный": "зелёный", "зеленый": "зелёный",
    "灰色": "серый", "灰": "серый", "gray": "серый", "grey": "серый", "серый": "серый",
    "银色": "серебристый", "银": "серебристый", "silver": "серебристый", "серебристый": "серебристый",
    "金色": "золотистый", "金": "золотистый", "gold": "золотистый", "золотистый": "золотистый", "золотой": "золотистый",
    "米色": "бежевый", "beige": "бежевый", "бежевый": "бежевый",
    "棕色": "коричневый", "棕": "коричневый", "brown": "коричневый", "коричневый": "коричневый",
    "卡其": "хаки", "卡其色": "хаки", "khaki": "хаки", "haki": "хаки", "хаки": "хаки",
    "透明": "прозрачный", "transparent": "прозрачный", "прозрачный": "прозрачный",
    "粉色": "розовый", "粉": "розовый", "pink": "розовый", "розовый": "розовый",
    "黄色": "жёлтый", "黄": "жёлтый", "yellow": "жёлтый", "жёлтый": "жёлтый", "желтый": "жёлтый",
    "紫色": "фиолетовый", "紫": "фиолетовый", "purple": "фиолетовый", "фиолетовый": "фиолетовый",
    "橙色": "оранжевый", "橙": "оранжевый", "orange": "оранжевый", "оранжевый": "оранжевый",
}

_CAPACITY_PATTERN = re.compile(r"^\s*([0-9]+(?:[.,][0-9]+)?)\s*(л|l|литр[а-я]*|мл|ml)\s*$", re.IGNORECASE)
_DESCRIPTION_SECTIONS = ("product_value", "usage_scenarios", "core_advantages", "usage_method", "notices")


# --------------------------------------------------------------------- 标签


def validate_hashtags(tags: Sequence[Any], *, max_tags: int = MAX_HASHTAGS) -> list[str]:
    """按原项目规则校验标签集合（数量、字符集、去重、凑数/弱标签禁令）。"""
    problems: list[str] = []
    if not isinstance(tags, Sequence) or isinstance(tags, (str, bytes)):
        return ["hashtags 必须是数组"]
    if len(tags) > max_tags:
        problems.append(f"标签数 {len(tags)} 超过上限 {max_tags}（Ozon 官方硬约束）")

    seen: set[str] = set()
    for index, raw in enumerate(tags):
        tag = str(raw or "").strip()
        if not tag:
            problems.append(f"hashtags[{index}] 为空")
            continue
        folded = tag.casefold()
        if folded in seen:
            problems.append(f"hashtags[{index}] 重复：{tag}")
            continue
        seen.add(folded)
        if tag != tag.casefold():
            problems.append(f"hashtags[{index}] 必须全小写：{tag}")
        if not HASHTAG_PATTERN.match(tag):
            problems.append(f"hashtags[{index}] 只能是 # + 西里尔字母（拒绝拉丁/数字/下划线/连字符）：{tag}")
            continue
        if not 2 <= len(tag) <= 30:
            problems.append(f"hashtags[{index}] 长度需在 2–30（含 #）：{tag}")
        if tag in BANNED_GENERIC_TAGS:
            problems.append(f"hashtags[{index}] 属通用凑数标签：{tag}")
        body = tag.lstrip("#")
        if body in WEAK_SINGLE_TAGS:
            problems.append(f"hashtags[{index}] 不允许单独使用弱标签：{tag}")
    return problems


def canonical_hashtag(text: Any) -> str | None:
    """把候选词规整为合法标签；含非法字符直接返回 None（不抢救）。"""
    raw = str(text or "").strip().casefold().replace("ё", "е")
    if not raw:
        return None
    words = re.findall(r"[а-я]+", raw)
    if not words:
        return None
    if re.search(r"[a-z0-9_]", raw):
        return None
    tag = "#" + "".join(words)
    if not 2 <= len(tag) <= 30:
        return None
    return tag


# --------------------------------------------------------------------- 容量 / 颜色


def normalize_capacity_text(text: Any) -> str | None:
    """容量归一：``12 л`` → ``12л``；``1.5 л`` → ``1500мл``；``500 ml`` → ``500мл``。"""
    if text is None:
        return None
    match = _CAPACITY_PATTERN.match(str(text))
    if not match:
        return None
    value = parse_number(match.group(1))
    if value is None or value <= 0:
        return None
    unit = match.group(2).casefold()
    if unit in {"мл", "ml"}:
        return f"{int(round(value))}мл"
    if value >= 10 and float(value).is_integer():
        return f"{int(value)}л"
    return f"{int(round(value * 1000))}мл"


def normalize_russian_color_name(text: Any) -> str | None:
    """颜色名标准化：查表 + 小写 + ``ё→е`` 归一；识别不了返回 None（不猜）。"""
    raw = str(text or "").strip().casefold()
    if not raw:
        return None
    mapped = COLOR_MAPPINGS.get(raw)
    if mapped is None:
        for key, value in COLOR_MAPPINGS.items():
            if key and key in raw:
                mapped = value
                break
    if mapped is None:
        return None
    return mapped.replace("ё", "е")


def validate_color_name(text: Any) -> list[str]:
    """颜色字段（attr 10097）不得混容量/数字/拉丁字母/型号。"""
    raw = str(text or "").strip()
    problems: list[str] = []
    if not raw:
        return ["颜色名为空"]
    if re.search(r"\d", raw):
        problems.append(f"颜色名不得含数字：{raw}")
    if re.search(r"[A-Za-z]", raw):
        problems.append(f"颜色名不得含拉丁字母：{raw}")
    if normalize_capacity_text(raw):
        problems.append(f"颜色名不得是容量：{raw}")
    if normalize_russian_color_name(raw) is None:
        problems.append(f"颜色名不是可识别的自然俄语颜色词：{raw}")
    return problems


# --------------------------------------------------------------------- 标题 / 描述


def validate_title_ru(title: Any, *, min_length: int = 10, max_length: int = 120) -> list[str]:
    text = str(title or "")
    problems: list[str] = []
    if len(text) < min_length:
        problems.append(f"标题长度 {len(text)} 小于 {min_length}")
    if len(text) > max_length:
        problems.append(f"标题长度 {len(text)} 大于 {max_length}")
    if re.search(r"[\u4e00-\u9fff]", text):
        problems.append("标题含中文字符（必须自然俄语）")
    core = text.split(",")[0].strip()
    if core and text.count(core) > 1:
        problems.append(f"标题核心词重复：{core}")
    return problems


def validate_description_sections(sections: Mapping[str, Any] | None) -> list[str]:
    problems: list[str] = []
    if not isinstance(sections, Mapping):
        return [f"缺少 sections（必须包含 {', '.join(_DESCRIPTION_SECTIONS)}）"]
    for name in _DESCRIPTION_SECTIONS:
        text = str(sections.get(name) or "")
        if len(text) < 10:
            problems.append(f"sections.{name} 长度 {len(text)} 小于 10")
    return problems


def validate_description_ru(text: Any, *, min_length: int = 80) -> list[str]:
    body = str(text or "")
    problems: list[str] = []
    if len(body) < min_length:
        problems.append(f"描述长度 {len(body)} 小于 {min_length}")
    if re.search(r"<[^>]+>", body):
        problems.append("描述正文不得含 HTML 标签")
    if re.search(r"[\u4e00-\u9fff]", body):
        problems.append("描述含中文字符（buyer 可见文案必须俄语）")
    return problems


def copy_bundle_hint() -> str:
    """``copy_bundle`` 的形状由**本文件的规则**决定，不是契约文件 —— 必须写进模型提示词。

    真机踩坑：提示词没写 copy_bundle 形状时，真模型给出的标题/描述是空的、五个 section 全缺，
    还写了 ``#простыня200х200`` 这种带数字的标签（规则只允许西里尔字母）。
    """
    sections = "、".join(_DESCRIPTION_SECTIONS)
    return (
        "输出必须是一个 JSON 对象，同时包含 ``title_ru`` / ``description_ru`` / ``keywords_ru`` 三份文档，"
        "以及 ``copy_bundle``。**copy_bundle 的形状（硬要求）**：\n"
        "- `title_ru`：字符串，10–120 字符，必须含核心关键词\n"
        "- `description_ru`：字符串，至少 300 字符\n"
        f"- `description_sections`：对象，必须同时包含 {sections}（每个字段至少 10 字符）\n"
        f"- `hashtags`：数组，最多 {MAX_HASHTAGS} 个，每个形如 `#простыня` —— **只能有西里尔字母**，"
        "不能含数字、拉丁字母、下划线、连字符，也不能有空格（一个标签一个词）\n"
        "- `primary_keywords`：数组，取自「已选关键词」\n"
        "- `bullets_ru`：数组（可选），每项 {text_ru, evidence}\n"
        "证据字段（`evidence` / `source_refs` / `basis`）一律是**字符串数组**，"
        "元素写成来源路径或字段名（例如 `input/source.json`、`facts.materials`），**不要写成对象**。\n"
        "禁止：中文/拼音、价格与折扣词、联系方式与外链、最高级绝对化用语（лучший/№1 等）、表情符号。"
    )


def validate_copy_bundle(bundle: Mapping[str, Any]) -> list[str]:
    """把标题/描述/标签的规则串成一个入口，供 handler 调用。"""
    problems: list[str] = []
    problems.extend(validate_title_ru(bundle.get("title_ru")))
    problems.extend(validate_description_ru(bundle.get("description_ru")))
    problems.extend(validate_description_sections(bundle.get("description_sections")))
    problems.extend(validate_hashtags(list(bundle.get("hashtags") or [])))
    problems.extend(official_copy_checks(bundle)["blocking"])
    return problems


def normalize_keyword_phrase(value: Any) -> str:
    """Only case/whitespace normalization; never inflect, reorder or stem."""
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def keyword_phrase_present(text: Any, phrase: Any, *, at_start: bool = False) -> bool:
    body, query = normalize_keyword_phrase(text), normalize_keyword_phrase(phrase)
    if not query:
        return False
    pattern = (r"^" if at_start else r"(?<!\w)") + re.escape(query) + r"(?!\w)"
    return re.search(pattern, body) is not None


def keyword_copy_checks(bundle: Mapping[str, Any], keywords: Sequence[Mapping[str, Any]]) -> list[str]:
    """User-selected queries constrain placement, not the AI's title template."""
    for field in ("primary_keywords", "secondary_keywords"):
        terms = bundle.get(field, [])
        if not isinstance(terms, list) or any(not isinstance(term, str) for term in terms):
            return [f"{field} 必须是字符串数组；没有已选关键词时使用 []"]
    rows = [row for row in keywords if row.get("role") not in {"ad", "reject", "exclude"}
            and row.get("relevance") != "conflict" and normalize_keyword_phrase(row.get("keyword"))]
    cores = [row for row in rows if row.get("role") == "core"]
    if len(cores) > 1:
        return ["标题只能使用一个主关键词，请将其余词设为副关键词"]
    core = cores[0] if cores else rows[0] if rows else None
    if core is None:
        return []  # Explicit category-only copy has no fabricated query.
    phrase = core["keyword"]
    problems = []
    if bundle.get("core_keyword") and normalize_keyword_phrase(bundle["core_keyword"]) != normalize_keyword_phrase(phrase):
        problems.append("core_keyword 必须记录用户所选主关键词，不得替换为模型自拟词")
    if not keyword_phrase_present(bundle.get("title_ru"), phrase, at_start=True):
        problems.append("标题必须以主关键词完整短语开头；仅允许规范大小写和空格，不换序、不拆开、不插入属性词")
    core_pattern = r"(?<!\w)" + re.escape(normalize_keyword_phrase(phrase)) + r"(?!\w)"
    if len(re.findall(core_pattern, normalize_keyword_phrase(bundle.get("title_ru")))) > 1:
        problems.append("标题不要重复堆砌主关键词")
    # A secondary query may also be a genuine colour/material/feature.
    # Lexical coincidence alone is not a reason to reject copy; factual proof
    # and review are handled by candidate_evidence.
    permitted_primary = {normalize_keyword_phrase(phrase)}
    if any(normalize_keyword_phrase(term) not in permitted_primary for term in bundle.get("primary_keywords") or []):
        problems.append("primary_keywords 只能记录主关键词；副关键词放入 secondary_keywords")
    for term in bundle.get("secondary_keywords") or []:
        if not keyword_phrase_present(bundle.get("description_ru"), term):
            problems.append("简介使用的副关键词也要保留完整短语，仅规范大小写和空格：" + str(term))
    return problems


def guided_copy_bundle_hint(*, allow_description_emoji: bool = True) -> str:
    """The current editor accepts readable prose, not five mandatory sections."""
    decoration = ("简介使用少量恰当 emoji 作段落/卖点标记；标题不使用 emoji。"
                  if allow_description_emoji else "标题和简介不使用 emoji，简介用空行与项目符号排版。")
    return (
        "每组 copy_bundle 包含：title_ru 字符串；description_ru 字符串；"
        "description_sections 对象（可为空或只包含有事实依据的 product_value/usage_scenarios/"
        "core_advantages/usage_method/notices，不必凑齐五段）；hashtags 字符串数组；"
        "core_keyword；primary_keywords 数组（仅主关键词）；secondary_keywords 数组（已选副关键词）。\n"
        "标题必须从唯一主关键词完整短语开始，只允许大小写与空格规范；"
        "短语内部不改词形、不换序、不拆开、不插入属性词。主关键词后由 AI 自然融合已证实属性与卖点，"
        "不采用固定拼接模板，三个候选都遵循相同前置规则。标题只主动融合主关键词，"
        "副关键词主要在简介自然使用，不为了覆盖副词而堆砌进标题；"
        "真实颜色、材质或卖点与副关键词恰好重合时允许保留，以已证实事实为依据。\n"
        "简介面向俄语买家，短段落之间留空行，必要时用简短卖点列表；不要输出 Markdown 粗体或 HTML。"
        "不为了字数补写无依据的用途、材质、安全、年龄或注意事项。" + decoration + "\n"
        f"hashtags 最多 {MAX_HASHTAGS} 个，只用小写 # + 西里尔字母，不含空格、数字或拉丁字母。"
        "禁止联系方式、外链、价格促销词和无法举证的绝对化承诺。"
    )


def validate_guided_copy_bundle(bundle: Mapping[str, Any], *, allow_description_emoji: bool = True) -> list[str]:
    """Scoped editor policy; archived/CLI five-section contracts stay unchanged."""
    problems = validate_title_ru(bundle.get("title_ru"), min_length=10, max_length=200)
    if any(len(word) > 27 for word in str(bundle.get("title_ru") or "").split()):
        problems.append("标题单词最多 27 字符；主关键词不可截断，请改选符合字段限制的完整短语")
    problems.extend(validate_description_ru(bundle.get("description_ru")))
    sections = bundle.get("description_sections", {})
    if not isinstance(sections, Mapping):
        problems.append("description_sections 必须是对象；不需要分段元数据时使用 {}")
    elif any(key not in _DESCRIPTION_SECTIONS or not isinstance(value, str) for key, value in sections.items()):
        problems.append("description_sections 仅支持已知分段名称和字符串内容")
    problems.extend(validate_hashtags(bundle.get("hashtags") if bundle.get("hashtags") is not None else []))
    problems.extend(official_copy_checks(bundle, allow_description_emoji=allow_description_emoji)["blocking"])
    if _EMOJI_PATTERN.search(str(bundle.get("title_ru") or "")):
        problems.append("标题不使用 emoji；可在简介中少量使用段落标记")
    if not allow_description_emoji and _EMOJI_PATTERN.search(str(bundle.get("description_ru") or "")):
        problems.append("当前简介样式关闭了 emoji，请使用空行与项目符号")
    return problems


# ------------------------------------------------- Ozon 官方硬规则（拒审高发区）

#: Historical compatibility exports for the archived copy validator. These are
#: workbench guards, not newly verified universal Ozon limits. Current editor
#: policy is 200 title characters; Seller API/card constraints are checked later.
OFFICIAL_MAX_TITLE = 255
OFFICIAL_MAX_DESCRIPTION = 6000
#: 工作台可读性推荐长度，只提示，不代表搜索排名或固定展示截断位置。
RECOMMENDED_TITLE = 120
#: 描述接近上限时的提醒阈值
DESCRIPTION_NEAR_LIMIT = 5500

#: 联系方式/外链/社交账号：Ozon 明令禁止出现在商品名与描述里
_CONTACT_PATTERN = re.compile(
    r"(?:https?://|www\.|\bt\.me\b|@[A-Za-z0-9._-]+\.[A-Za-z]{2,}"
    r"|(?:\+7|\b8)[\s\-()]?\d{3}[\s\-()]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}"
    r"|whatsapp|telegram|instagram|wechat|вконтакте|\bvk\.com\b|微信号|微信|电话|почта\s*:|email\s*:)",
    re.IGNORECASE,
)
#: 价格/促销词：Ozon 不允许在名称与描述里写价格、折扣、促销
_PRICE_PATTERN = re.compile(
    r"(?:\bцена\b|\bцены\b|\bскидк[а-я]*\b|\bакци[яию]\b|\bраспродаж[а-я]*\b|\bдешев[а-я]*\b|\bруб\.?\b|₽|\bпромокод\b)",
    re.IGNORECASE,
)
#: 最高级/绝对化用语（俄罗斯广告法要求可举证，Ozon 也会拦）
_SUPERLATIVE_PATTERN = re.compile(
    r"(?:лучш[а-я]+|сам[а-я]+\s+лучш[а-я]+|№\s?1|номер\s?1|перв[а-я]+\s+в\s+мире|идеальн[а-я]+\s+выбор|единственн[а-я]+)",
    re.IGNORECASE,
)
_EMOJI_PATTERN = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF]")
_CAPS_WORD_PATTERN = re.compile(r"\b[А-ЯЁA-Z]{4,}\b")
_PUNCT_RUN_PATTERN = re.compile(r"[!?]{2,}|\.{4,}")


def _official_text_checks(text: str, *, where: str, allow_emoji: bool = False) -> tuple[list[str], list[str]]:
    """返回 (blocking, advisory)。两个字段的公共部分：联系方式、价格、大写、标点、emoji。"""
    blocking: list[str] = []
    advisory: list[str] = []
    if not text:
        return blocking, advisory
    contact = _CONTACT_PATTERN.search(text)
    if contact:
        blocking.append(f"{where}含联系方式/外链（Ozon 禁止）：{contact.group(0)!r}")
    price = _PRICE_PATTERN.search(text)
    if price:
        blocking.append(f"{where}含价格/促销词（Ozon 禁止）：{price.group(0)!r}")
    caps = _CAPS_WORD_PATTERN.findall(text)
    if caps:
        advisory.append(f"{where}有全大写单词（Ozon 不鼓励 CAPS LOCK）：{', '.join(sorted(set(caps))[:5])}")
    emoji_count = len(_EMOJI_PATTERN.findall(text))
    if emoji_count and not allow_emoji:
        advisory.append(f"{where}含 emoji；标题不使用表情，简介按所选排版样式处理")
    elif emoji_count > 4:
        advisory.append("简介 emoji 较多，建议仅保留少量段落标记以免影响可读性（排版建议，非排名因素）")
    if _PUNCT_RUN_PATTERN.search(text):
        advisory.append(f"{where}有连续标点（!!! / ?? / ....）")
    return blocking, advisory


def official_copy_checks(bundle: Mapping[str, Any], *, allow_description_emoji: bool | None = None) -> dict[str, list[str]]:
    """Content checks plus historical compatibility guards, with severity.

    Recommendation lengths and decorative description emoji are not search
    ranking factors or newly verified universal platform prohibitions.
    """
    blocking: list[str] = []
    advisory: list[str] = []
    title = str(bundle.get("title_ru") or "")
    description = str(bundle.get("description_ru") or "")

    title_blocking, title_advisory = _official_text_checks(title, where="标题")
    desc_emoji = bundle.get("copy_policy_version") == 3 if allow_description_emoji is None else allow_description_emoji
    desc_blocking, desc_advisory = _official_text_checks(description, where="描述", allow_emoji=desc_emoji)
    blocking.extend(title_blocking)
    blocking.extend(desc_blocking)
    advisory.extend(title_advisory)
    advisory.extend(desc_advisory)

    if len(title) > OFFICIAL_MAX_TITLE:
        blocking.append(f"标题长度 {len(title)} 超过历史工作台兼容上限 {OFFICIAL_MAX_TITLE}")
    elif len(title) > RECOMMENDED_TITLE:
        advisory.append(f"标题长度 {len(title)} 超过推荐 {RECOMMENDED_TITLE}，建议精简以便买家快速理解")

    if len(description) > OFFICIAL_MAX_DESCRIPTION:
        blocking.append(f"描述长度 {len(description)} 超过工作台兼容上限 {OFFICIAL_MAX_DESCRIPTION}")
    elif len(description) > DESCRIPTION_NEAR_LIMIT:
        advisory.append(f"描述长度 {len(description)} 已接近上限 {OFFICIAL_MAX_DESCRIPTION}")

    superlative = _SUPERLATIVE_PATTERN.search(f"{title} {description}")
    if superlative:
        advisory.append(f"含绝对化用语（俄罗斯广告法需可举证）：{superlative.group(0)!r}")

    return {"blocking": sorted(set(blocking)), "advisory": sorted(set(advisory))}


def main(argv: Sequence[str] | None = None) -> int:
    """快速自检：把一段标题/描述丢进来，看会不会被 Ozon 规则拦。"""
    import argparse
    import json as _json

    parser = argparse.ArgumentParser(description="对照 Ozon 官方规则检查标题/描述/标签")
    parser.add_argument("--title", default="")
    parser.add_argument("--description", default="")
    parser.add_argument("--hashtag", action="append", dest="hashtags")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    bundle = {
        "title_ru": args.title,
        "description_ru": args.description,
        "hashtags": args.hashtags or [],
    }
    official = official_copy_checks(bundle)
    # CLI 只做标题/描述层面的检查（sections 与标签是流水线步骤的必填项，不在这里报）
    internal = validate_title_ru(args.title) + validate_description_ru(args.description)
    report = {
        "ok": not official["blocking"] and not internal,
        "blocking": official["blocking"],
        "advisory": official["advisory"],
        "internal": internal,
    }
    if args.json:
        print(_json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("阻断项：" + ("无" if not report["blocking"] else ""))
        for item in report["blocking"]:
            print(f"  ⛔ {item}")
        for item in internal:
            print(f"  ⛔ {item}")
        print("建议项：")
        for item in report["advisory"]:
            print(f"  ⚠️ {item}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    import sys as _sys

    _sys.exit(main())


def iter_problem_paths(problems: Iterable[str]) -> list[str]:
    return [str(item) for item in problems]
