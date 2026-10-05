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


def validate_copy_bundle(bundle: Mapping[str, Any]) -> list[str]:
    """把标题/描述/标签的规则串成一个入口，供 handler 调用。"""
    problems: list[str] = []
    problems.extend(validate_title_ru(bundle.get("title_ru")))
    problems.extend(validate_description_ru(bundle.get("description_ru")))
    problems.extend(validate_description_sections(bundle.get("description_sections")))
    problems.extend(validate_hashtags(list(bundle.get("hashtags") or [])))
    return problems


def iter_problem_paths(problems: Iterable[str]) -> list[str]:
    return [str(item) for item in problems]
