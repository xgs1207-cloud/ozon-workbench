"""数字解析（价格、尺寸、搜索量…）：处理小数点/千分位的歧义。

采集数据里同一个数字可能写成 ``1234.5`` / ``1,234.5`` / ``1 234,5`` / ``19,0``，
直接 ``float()`` 或"一律删逗号"都会出错（``19,0`` 会变成 190）。规则：

1. 先去掉货币符号、单位、空白（含不换行空格）；
2. 若同时有点和逗号：**最后出现的那个是小数点**，另一个是千分位；
3. 只有逗号时：逗号后 1–2 位数字视为小数逗号（``19,0`` → 19.0），
   3 位且前面还有数字视为千分位（``1,234`` → 1234）；
4. 解析不了就返回 ``None`` —— 不猜。
"""

from __future__ import annotations

import re
from typing import Any

_STRIP_PATTERN = re.compile(r"[^0-9.,\-]")


def parse_number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return None if number != number or number in (float("inf"), float("-inf")) else number

    text = str(value).replace("\u00a0", " ").strip()
    if not text:
        return None
    text = re.sub(r"\s+", "", text)
    text = _STRIP_PATTERN.sub("", text)
    if not text or text in {"-", ".", ","}:
        return None

    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        head, _, tail = text.rpartition(",")
        if head and len(tail) in (1, 2) and "," not in head:
            text = f"{head.replace(',', '')}.{tail}"
        else:
            text = text.replace(",", "")

    try:
        return float(text)
    except ValueError:
        return None


def is_positive_int(value: Any) -> bool:
    """正整数判定（用于 category_id / type_id 这类必须为正整数的字段）。"""
    number = parse_number(value)
    return number is not None and number > 0 and float(number).is_integer()
