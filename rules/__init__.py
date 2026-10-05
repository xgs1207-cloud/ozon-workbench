"""规则层：文档（*.md）与可执行校验（validate.py）。

    from rules.validate import validate_copy_bundle          # 显式导入（推荐）
    from rules import validate_title_ru                      # 便捷再导出

再导出用 **惰性** 方式（PEP 562）：这样 `python -m rules.validate` 不会先被父包的
eager import 拉起来一次（否则 Python 会打 "found in sys.modules ... unpredictable behaviour"）。
"""

from typing import Any

_LAZY_EXPORTS = {
    "MAX_HASHTAGS",
    "canonical_hashtag",
    "normalize_capacity_text",
    "normalize_russian_color_name",
    "validate_color_name",
    "validate_copy_bundle",
    "validate_description_ru",
    "validate_description_sections",
    "validate_hashtags",
    "validate_title_ru",
    "official_copy_checks",
    "OFFICIAL_MAX_TITLE",
    "OFFICIAL_MAX_DESCRIPTION",
    "RECOMMENDED_TITLE",
}

__all__ = sorted(_LAZY_EXPORTS)


def __getattr__(name: str) -> Any:
    if name in _LAZY_EXPORTS:
        from . import validate as _validate

        return getattr(_validate, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | _LAZY_EXPORTS)
