"""规则层：文档（*.md）与可执行校验（validate.py）。

    from rules.validate import validate_copy_bundle
"""

from .validate import (
    MAX_HASHTAGS,
    canonical_hashtag,
    normalize_capacity_text,
    normalize_russian_color_name,
    validate_color_name,
    validate_copy_bundle,
    validate_description_ru,
    validate_description_sections,
    validate_hashtags,
    validate_title_ru,
)

__all__ = [
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
]
