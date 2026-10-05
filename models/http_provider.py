"""通用模型适配器：OpenAI 兼容 / DeepSeek 风格的 HTTP provider（不绑定任何供应商）。

设计原则（很重要）：

1. **模型负责创意，装配器负责结构** —— 让通用模型直接产出 23KB 设计契约或 20 字段图位，
   基本不可能一次过契约。所以：
   * ``analyze_product`` / ``write_copy_ru`` / ``position_product``：模型输出 JSON，我们**严格过契约 + 规则**，
     不合法就带着"校验问题清单"重试，重试仍不过就**如实失败**（不悄悄降级）；
   * ``design_listing``：模型只产出**买家可见文案**（listing），结构化部分（SKU 计划、属性决策、
     visual_system、决策留痕）由 ``models.design`` 的确定性装配器补齐并过契约；
   * ``plan_images``：槽位/合成方式/叠字规则由技能规则装配器决定，模型只润色提示词（可选）。
2. **注入式传输层**：``ChatTransport`` 协议 + ``OpenAICompatibleTransport``（urllib），
   测试/离线演练用脚本化传输层，全程零网络。
3. **凭据只从环境变量读**：``MODEL_BASE_URL`` / ``MODEL_API_KEY`` / ``MODEL_NAME`` 等，缺了就报清楚缺哪个。
4. 也可以接 Codex CLI：实现同一个 ``ModelProvider`` 接口（或只用它做 ``ChatTransport``）即可，见 HANDOFF §5.1。
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from .base import (
    AnalysisRequest,
    CopyRequest,
    DesignRequest,
    ImagePlanRequest,
    ImageRequest,
    ModelError,
    PositionRequest,
)

from contracts.normalize import normalize_payload

DEFAULT_TIMEOUT = 90
DEFAULT_TEMPERATURE = 0.3
DEFAULT_MAX_ATTEMPTS = 3
#: 单次回复上限（够写完整 JSON，又能挡住异常长回复烧钱）
DEFAULT_MAX_TOKENS = 4000

JSON_FENCE = re.compile(r"```(?:json)?\s*(.+?)```", re.DOTALL)

ENV_KEYS = ("MODEL_BASE_URL", "MODEL_API_KEY", "MODEL_NAME")


# --------------------------------------------------------------------- 传输层


class ChatTransport(Protocol):
    name: str

    def complete(self, *, system: str, user: str, temperature: float | None = None) -> str: ...


def _extract_content(response: Mapping[str, Any]) -> str:
    """兼容 OpenAI 风格与常见变体（``choices[0].message.content`` / ``output_text`` / ``content``）。"""
    choices = response.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0] if isinstance(choices[0], Mapping) else {}
        message = first.get("message") if isinstance(first.get("message"), Mapping) else {}
        content = message.get("content") or first.get("text")
        if isinstance(content, str) and content.strip():
            return content
    for key in ("output_text", "content", "text", "response"):
        value = response.get(key)
        if isinstance(value, str) and value.strip():
            return value
    raise ModelError(f"无法从模型响应里取到文本内容：{list(response)[:6]}")


class OpenAICompatibleTransport:
    """``POST {base_url}/chat/completions``（OpenAI 兼容端点，DeepSeek 等同样适用）。"""

    name = "openai-compatible"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: int = DEFAULT_TIMEOUT,
        urlopen: Any | None = None,
        response_format: bool = True,
        extra_headers: Mapping[str, str] | None = None,
        thinking: str | None = None,
        max_tokens: int | None = None,
    ) -> None:
        cleaned = str(base_url).strip().rstrip("/")
        if not cleaned.startswith(("http://", "https://")):
            raise ModelError(f"MODEL_BASE_URL 必须是 http(s) 地址：{base_url!r}")
        self.base_url = cleaned
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self._urlopen = urlopen or urllib.request.urlopen
        self.response_format = response_format
        self.extra_headers = dict(extra_headers or {})
        #: 方舟推理模型的"思考开关"：``disabled`` 时又快又省（实测 13.1s/527token → 4.5s/83token）
        self.thinking = (str(thinking).strip().lower() or None) if thinking else None
        self.max_tokens = int(max_tokens) if max_tokens else None

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/chat/completions"

    def build_request(self, *, system: str, user: str, temperature: float | None = None) -> urllib.request.Request:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if temperature is not None:
            body["temperature"] = temperature
        if self.max_tokens:
            body["max_tokens"] = self.max_tokens
        if self.thinking:
            # 方舟/豆包：{"type": "disabled"} 关掉思考链 —— 快 3 倍、输出 token 少 6 倍
            body["thinking"] = {"type": self.thinking}
        if self.response_format:
            body["response_format"] = {"type": "json_object"}
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            **self.extra_headers,
        }
        return urllib.request.Request(
            self.endpoint,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )

    def complete(self, *, system: str, user: str, temperature: float | None = None) -> str:
        request = self.build_request(system=system, user=user, temperature=temperature)
        try:
            with self._urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            detail = ""
            try:
                detail = error.read().decode("utf-8")[:300]
            except Exception:  # noqa: BLE001
                detail = ""
            raise ModelError(f"模型接口返回 HTTP {error.code}：{detail or error.reason}") from error
        except urllib.error.URLError as error:
            raise ModelError(f"无法连接模型接口：{error.reason}") from error
        try:
            payload = json.loads(raw)
        except ValueError as error:
            raise ModelError(f"模型接口返回的不是 JSON：{raw[:200]}") from error
        if not isinstance(payload, Mapping):
            raise ModelError("模型接口返回的不是 JSON 对象")
        return _extract_content(payload)


# --------------------------------------------------------------------- JSON 提取与修复


def extract_json(text: str) -> dict[str, Any] | None:
    """从模型输出里抠出 JSON 对象：容忍 ```json 围栏、前后废话、嵌套花括号。"""
    if not isinstance(text, str):
        return None
    candidate = text.strip()
    fenced = JSON_FENCE.search(candidate)
    if fenced:
        candidate = fenced.group(1).strip()
    try:
        value = json.loads(candidate)
        return value if isinstance(value, dict) else None
    except ValueError:
        pass

    start = candidate.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(candidate)):
            char = candidate[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    snippet = candidate[start : index + 1]
                    try:
                        value = json.loads(snippet)
                    except ValueError:
                        break
                    return value if isinstance(value, dict) else None
        start = candidate.find("{", start + 1)
    return None


# --------------------------------------------------------------------- 提示词


SYSTEM_JSON = (
    "你是跨境电商（Ozon 俄罗斯站）的资深上品运营与俄语内容专家。"
    "只输出一个 JSON 对象，不要输出解释、Markdown 代码围栏或任何多余文字。"
    "所有面向买家的文本必须是俄语；严禁编造采集数据里没有的材质、认证、承重、尺寸或品牌。"
    "拿不准的字段写 null 或省略，不要猜测。"
)


def _context_block(**parts: Any) -> str:
    lines = []
    for key, value in parts.items():
        if value in (None, {}, []):
            continue
        lines.append(f"### {key}\n{json.dumps(value, ensure_ascii=False)[:6000]}")
    return "\n\n".join(lines)


def _keywords_of(request: Any) -> list[Any]:
    """已选关键词可能在 request.selected_keywords / extra / source 里（不同请求类型不一样）。"""
    direct = getattr(request, "selected_keywords", None)
    if direct:
        return list(direct)
    extra = getattr(request, "extra", None)
    if isinstance(extra, Mapping) and extra.get("selected_keywords"):
        return list(extra["selected_keywords"])
    source = getattr(request, "source", None)
    if isinstance(source, Mapping) and source.get("selected_keywords"):
        return list(source["selected_keywords"])
    return []


def enrich_facts_from_inputs(payload: dict[str, Any], request: Any) -> list[str]:
    """把**我们本来就确切知道**的事实补进 ``facts``（代码填，不靠模型照抄）。

    - 类目中文名：``source.selected_category`` 或 ``input/category-selection.json``
    - 尺寸/重量：``input/workbench-sku-overrides.json`` 里**人工确认过**的值
    - 品牌：来源里没有品牌时按项目既定规则填 ``Нет бренда``（类目字典里确实有这个值，
      属性编译阶段也是这么填的）
    """
    notes: list[str] = []
    facts = payload.get("facts")
    if not isinstance(facts, dict):
        return notes
    source = getattr(request, "source", None) or {}
    product_dir = getattr(request, "product_dir", None)

    def read_json(relative: str) -> dict[str, Any]:
        if not product_dir:
            return {}
        path = Path(product_dir) / relative
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, Mapping) else {}

    if not facts.get("category_cn"):
        selection = source.get("selected_category") if isinstance(source.get("selected_category"), Mapping) else {}
        hint = selection.get("category_path_zh") or selection.get("category_name")
        if not hint:
            side = read_json("input/category-selection.json")
            hint = side.get("category_path_zh") or side.get("category_name")
        if hint:
            facts["category_cn"] = str(hint)
            notes.append(f"category_cn 由采集/类目选择补全：{hint}")

    overrides = read_json("input/workbench-sku-overrides.json")
    product_block = overrides.get("product") if isinstance(overrides.get("product"), Mapping) else {}

    # 1688 详情页属性（材质/包装数量/认证）——真模型曾因缺这些而要求人工确认
    attributes = source.get("attributes_zh") if isinstance(source.get("attributes_zh"), Mapping) else {}
    # 人工确认入口：运营填一次 input/human-confirmations.json，**优先于**采集值（真商品总会缺信息）
    confirmations = read_json("input/human-confirmations.json")
    if confirmations:
        notes.append("已应用 input/human-confirmations.json（人工确认值优先）")

    material = str(
        confirmations.get("material")
        or confirmations.get("material_zh")
        or attributes.get("material")
        or source.get("material_zh")
        or ""
    ).strip()
    quantity = (
        confirmations.get("package_quantity")
        if confirmations.get("package_quantity") not in (None, "")
        else attributes.get("package_quantity") or source.get("package_quantity")
    )
    certifications = (
        confirmations.get("certifications")
        if confirmations.get("certifications") not in (None, "", [])
        else attributes.get("certifications") or source.get("certifications_zh")
    )
    if confirmations.get("product_weight_g"):
        product_block = {**product_block, "product_weight_g": confirmations["product_weight_g"]}
    dims_confirmed = confirmations.get("product_dimensions_mm")
    if isinstance(dims_confirmed, Mapping) and all(dims_confirmed.get(key) for key in ("length", "width", "height")):
        product_block = {
            **product_block,
            "product_length_mm": dims_confirmed["length"],
            "product_width_mm": dims_confirmed["width"],
            "product_height_mm": dims_confirmed["height"],
        }

    if material and not facts.get("materials"):
        facts["materials"] = [material]
        notes.append(f"materials 补全：{material}")
    if quantity not in (None, "", 0) and str(facts.get("package_quantity") or "") == "unknown":
        facts["package_quantity"] = {"value": quantity, "source": "采集属性/人工确认"}
        notes.append(f"package_quantity 补全：{quantity}")
    if isinstance(certifications, str):
        certifications = [item for item in (item.strip() for item in certifications.split("、")) if item]
    if certifications and not facts.get("certifications"):
        facts["certifications"] = list(certifications)
        notes.append(f"certifications 补全：{list(certifications)}")

    # SKU 图片引用：图我们已经采集到了（input/sku-images/），不能让它空着
    sku_images = sorted(
        f"input/sku-images/{item.name}" for item in (Path(product_dir) / "input" / "sku-images").glob("*")
        if item.is_file()
    ) if product_dir else []
    if sku_images:
        filled = 0
        for index, sku in enumerate(facts.get("skus") or []):
            if not isinstance(sku, dict) or sku.get("image_refs"):
                continue
            if index < len(sku_images):
                sku["image_refs"] = [sku_images[index]]
            else:
                sku["image_refs"] = list(sku_images[:1])  # 图不够时共用第一张（并如实记在说明里）
            filled += 1
        if filled:
            notes.append(f"facts.skus[].image_refs 用采集到的 SKU 图补全（{len(sku_images)} 张，{filled} 个 SKU）")
    length, width, height = (
        product_block.get("product_length_mm"),
        product_block.get("product_width_mm"),
        product_block.get("product_height_mm"),
    )
    if all(isinstance(item, (int, float)) and item > 0 for item in (length, width, height)):
        facts["dimensions"] = {
            "length_mm": length,
            "width_mm": width,
            "height_mm": height,
            "source": "input/workbench-sku-overrides.json（人工确认）",
        }
        notes.append(f"dimensions 用人工确认值补全：{length}×{width}×{height} mm")
    weight = product_block.get("product_weight_g")
    if isinstance(weight, (int, float)) and weight > 0:
        facts["weight"] = {"value_g": weight, "source": "input/workbench-sku-overrides.json（人工确认）"}
        notes.append(f"weight 用人工确认值补全：{weight} g")

    if not facts.get("brand") and not str(source.get("brand") or "").strip():
        from pipeline.attributes import UNBRANDED_TEXT  # 延迟导入：避免 models↔pipeline 循环依赖

        facts["brand"] = UNBRANDED_TEXT
        notes.append(f"品牌：来源无品牌，按项目规则填「{UNBRANDED_TEXT}」")
    payload["facts"] = facts
    return notes


NARRATIVE_FIELDS = ("selling_points", "inferences", "unknowns", "risks", "recommendation")


def build_narrative_prompt(request: Any, *, facts: Mapping[str, Any] | None = None) -> str:
    """只让模型写"叙述字段"的提示词。

    形状必须与 ``product-analysis`` 契约逐字一致（这里踩过坑：自己编的
    ``inferences={area,statement,basis}`` 与契约的 ``{field,value,confidence,basis}`` 不符，
    导致真模型连续 3 次过不了校验）。``tests/test_http_provider.py`` 有一条防漂移测试，
    会拿契约里的必填字段名来核对这段提示词。

    ``facts``：系统已按采集 + 人工确认数据补全的事实。**必须给模型看**，否则它会因为
    "source 里没有品牌/重量"而要求人工确认（真机踩过），而这些我们其实已经确切知道。
    """
    return (
        "请只输出下面这 5 个键（JSON 对象，**不要输出 facts / processing / schema_version 等**，"
        "那部分由系统按采集数据填写）：\n"
        "- `selling_points`：数组，每项 {\"text\": 字符串, \"evidence\": [证据来源字符串]}，3–6 条\n"
        "- `inferences`：数组，每项 {\"field\": 字符串, \"value\": 任意, "
        "\"confidence\": \"low\"|\"medium\"|\"high\", \"basis\": [依据字符串]}\n"
        "- `unknowns`：数组，每项 {\"field\": 字符串, \"reason\": 字符串, \"needed_from_human\": true|false}\n"
        "- `risks`：数组，每项 {\"area\": 字符串, \"level\": \"low\"|\"medium\"|\"high\"|\"critical\", "
        "\"message\": 字符串, \"blocking\": true|false}\n"
        "- `recommendation`：{\"decision\": \"continue\"|\"needs_human_input\"|\"reject\"|\"unknown\", \"reason\": 字符串}\n"
        "硬规则：只依据下面的事实与关键词；不要编造参数、认证、品牌或材质；"
        "拿不准就写进 unknowns，或把 decision 设为 needs_human_input。"
        "数组字段不能是 null；只输出 JSON，不要解释文字。\n\n"
        + _context_block(
            confirmed_facts=facts,
            source=request.source,
            selected_keywords=_keywords_of(request),
            positioning=getattr(request, "positioning", None),
        )
    )


def _schema_hint(contract: str) -> str:
    """给模型一份**够精确**的字段表（含数组元素的必填字段与枚举、并解析 ``$ref``）。

    真机踩坑记录：只列顶层必填时，模型会漏掉数组元素内部的必填字段
    （例如 ``product-positioning.buyer_selling_points[].claim_type``）→ 连续 3 次过不了校验。
    """
    try:
        from contracts import load_contract

        schema = load_contract(contract)
    except Exception:  # noqa: BLE001 - 没有契约时也给个提示
        return f"（请严格产出 {contract} 契约形状）"

    defs = schema.get("$defs") or schema.get("definitions") or {}

    def resolve(node: Any) -> Mapping[str, Any]:
        seen = 0
        while isinstance(node, Mapping) and node.get("$ref") and seen < 4:
            name = str(node["$ref"]).split("/")[-1]
            node = defs.get(name) or {}
            seen += 1
        return node if isinstance(node, Mapping) else {}

    lines: list[str] = []

    def enums_of(node: Mapping[str, Any]) -> str:
        values = node.get("enum")
        return f"（取值：{'|'.join(str(item) for item in values)}）" if isinstance(values, list) and values else ""

    def describe(node: Any, path: str, depth: int) -> None:
        node = resolve(node)
        if depth > 2 or not node:
            return
        properties = node.get("properties") if isinstance(node.get("properties"), Mapping) else {}
        if properties:
            required = set(node.get("required") or [])
            names = "、".join(f"{key}{'*' if key in required else ''}" for key in list(properties)[:20])
            if path:
                lines.append(f"- `{path}` 包含字段：{names}")
            for key, child in list(properties.items())[:20]:
                child = resolve(child)
                if isinstance(child.get("items"), Mapping):
                    item = resolve(child["items"])
                    item_required = item.get("required") or []
                    detail = ""
                    if item.get("properties"):
                        detail = "；每项必填 " + "、".join(str(name) for name in item_required)
                        extra = [
                            f"{name}{enums_of(resolve(item['properties'][name]))}"
                            for name in list(item["properties"])[:8]
                            if resolve(item["properties"][name]).get("enum")
                        ]
                        if extra:
                            detail += "；枚举：" + "，".join(extra)
                    lines.append(f"- `{path}.{key}[]`：数组{detail}")
                    if not item_required:
                        describe(item, f"{path}.{key}[]", depth + 1)
                elif child.get("properties"):
                    describe(child, f"{path}.{key}".lstrip("."), depth + 1)
        elif isinstance(node.get("items"), Mapping):
            describe(node["items"], path, depth + 1)

    describe(schema, "", 0)
    top = "、".join(f"{key}*" for key in (schema.get("required") or [])) or "（无）"
    return (
        f"输出必须满足 {contract} 契约。带 * 的是必填。顶层必填：{top}。\n"
        + ("\n".join(lines[:20]) + "\n" if lines else "")
        + "硬规则：① 只能出现契约里列出的键，多一个键都不行（additionalProperties=false）；"
        "② 数组字段不能是 null，没内容就给 []；对象字段不能是 null，没内容就给 {}；"
        "③ 类型必须一致（字符串就是字符串，不要给数组或对象），枚举只能取列出的值；"
        "④ 数组元素内部的必填字段一个都不能少；⑤ 不要输出解释文字或代码围栏。"
    )


# --------------------------------------------------------------------- provider


ARK_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"


@dataclass
class ProviderConfig:
    base_url: str
    api_key: str
    model: str
    timeout: int = DEFAULT_TIMEOUT
    temperature: float = DEFAULT_TEMPERATURE
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    fallback_to_deterministic: bool = False
    #: 方舟/豆包思考开关（``disabled`` = 又省钱又快；实测 13.1s→4.5s、527→83 token）
    thinking: str | None = None
    #: 单次回复上限，避免最坏情况烧钱
    max_tokens: int | None = DEFAULT_MAX_TOKENS

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ProviderConfig":
        source = env if env is not None else os.environ
        missing = [key for key in ENV_KEYS if not str(source.get(key) or "").strip()]
        if missing:
            raise ModelError(
                "模型层配置不完整，缺少环境变量：" + ", ".join(missing)
                + "（示例：MODEL_BASE_URL=https://api.deepseek.com/v1 MODEL_API_KEY=sk-xxx MODEL_NAME=deepseek-chat）"
            )
        return cls(
            base_url=str(source["MODEL_BASE_URL"]).strip(),
            api_key=str(source["MODEL_API_KEY"]).strip(),
            model=str(source["MODEL_NAME"]).strip(),
            **_optional_numbers(source),
        )

    @classmethod
    def ark_from_env(cls, env: Mapping[str, str] | None = None) -> "ProviderConfig":
        """火山方舟：**与生图共用 ARK_API_KEY**，文本端点是 OpenAI 兼容的 /api/v3。

        只需要 ``ARK_API_KEY`` + ``ARK_TEXT_MODEL``（或 ``MODEL_NAME``）；
        端点可用 ``ARK_BASE_URL`` / ``MODEL_BASE_URL`` 覆盖。
        """
        source = env if env is not None else os.environ
        api_key = str(source.get("MODEL_API_KEY") or source.get("ARK_API_KEY") or "").strip()
        model = str(source.get("MODEL_NAME") or source.get("ARK_TEXT_MODEL") or "").strip()
        base_url = str(
            source.get("MODEL_BASE_URL") or source.get("ARK_BASE_URL") or ARK_BASE_URL
        ).strip()
        missing = []
        if not api_key:
            missing.append("ARK_API_KEY（或 MODEL_API_KEY）")
        if not model:
            missing.append("ARK_TEXT_MODEL（或 MODEL_NAME，填方舟控制台的接入点 ID，如 ep-2026xxxx）")
        if missing:
            raise ModelError("火山方舟文本模型配置不完整，缺少：" + "、".join(missing))
        return cls(
            base_url=base_url,
            api_key=api_key,
            model=model,
            **_optional_numbers(source),
        )


def _optional_numbers(source: Mapping[str, str]) -> dict[str, Any]:
    def number(key: str, fallback: Any, cast: Any) -> Any:
        raw = str(source.get(key) or "").strip()
        if not raw:
            return fallback
        try:
            return cast(raw)
        except ValueError:
            return fallback

    return {
        "timeout": number("MODEL_TIMEOUT", DEFAULT_TIMEOUT, int),
        "temperature": number("MODEL_TEMPERATURE", DEFAULT_TEMPERATURE, float),
        "max_attempts": max(1, number("MODEL_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS, int)),
        "max_tokens": number("MODEL_MAX_TOKENS", DEFAULT_MAX_TOKENS, int),
        "thinking": str(source.get("ARK_THINKING") or source.get("MODEL_THINKING") or "disabled").strip().lower() or None,
        "fallback_to_deterministic": str(source.get("MODEL_FALLBACK_TO_DETERMINISTIC") or "").strip().lower()
        in {"1", "true", "yes", "on"},
    }


class HttpModelProvider:
    """把 OpenAI 兼容端点接到 ``ModelProvider`` 接口上（含契约校验与修复重试）。"""

    name = "http"

    def __init__(
        self,
        transport: ChatTransport,
        *,
        temperature: float = DEFAULT_TEMPERATURE,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        fallback_to_deterministic: bool = False,
    ) -> None:
        self.transport = transport
        self.temperature = temperature
        self.max_attempts = max(1, int(max_attempts))
        self.fallback_to_deterministic = fallback_to_deterministic
        #: 每次调用的记录（供排查：第几次、是否修复成功、问题清单）
        self.calls: list[dict[str, Any]] = []
        #: 不适合写进契约对象的说明（契约多为 additionalProperties:false）
        self.last_notes: list[str] = []

    # ---------------------------------------------------------------- 基础设施

    def _call_json(
        self,
        *,
        task: str,
        user: str,
        validate: Callable[[dict[str, Any]], list[str]],
        system: str = SYSTEM_JSON,
        attempts: int | None = None,
        contract: str | None = None,
    ) -> tuple[dict[str, Any], list[str]]:
        """调模型 → 抠 JSON →（按契约机械归一化）→ 校验 → 失败带问题清单重试。"""
        warnings: list[str] = []
        problems: list[str] = []
        prompt = user
        schema: Mapping[str, Any] | None = None
        if contract:
            try:
                from contracts import load_contract

                schema = load_contract(contract)
            except Exception:  # noqa: BLE001 - 拿不到契约就跳过归一化
                schema = None
        for attempt in range(1, (attempts or self.max_attempts) + 1):
            text = self.transport.complete(system=system, user=prompt, temperature=self.temperature)
            payload = extract_json(text)
            fixes: list[str] = []
            if payload is None:
                problems = ["输出不是合法 JSON 对象（可能需要去掉解释文字或代码围栏）"]
            else:
                if schema is not None:
                    payload, fixes = normalize_payload(payload, schema)
                problems = list(validate(payload))
            self.calls.append(
                {
                    "task": task,
                    "attempt": attempt,
                    "ok": not problems,
                    "problems": problems[:6],
                    "normalized": fixes[:6],
                    "chars": len(text),
                }
            )
            if fixes:
                warnings.extend(f"{task}: {item}" for item in fixes[:6])
            if not problems:
                if attempt > 1:
                    warnings.append(f"{task}: 第 {attempt} 次尝试通过校验（前一次输出不合法）")
                return payload, warnings
            if attempt < (attempts or self.max_attempts):
                prompt = (
                    f"{user}\n\n### 上一次输出不合法，请修正后重新只输出 JSON\n"
                    + "\n".join(f"- {item}" for item in problems[:12])
                )
        raise ModelError(f"{task} 连续 {attempts or self.max_attempts} 次未通过校验：" + "；".join(problems[:6]))

    def _deterministic(self) -> Any:
        """降级用的确定性实现（仅在 fallback_to_deterministic 打开时使用）。"""
        from .fake import FakeProvider

        return FakeProvider()

    # ---------------------------------------------------------------- 五个方法

    def analyze_product(self, request: AnalysisRequest) -> dict[str, Any]:
        """**事实由代码填，模型只写叙述**。

        为什么这么设计（真机实测得出的结论）：契约要求 ``facts`` 把采集数据（SKU/价格/尺寸/图片引用）
        结构化重述一遍，让语言模型逐字照抄结构化数据是最容易出错的事 —— 真模型连续 3 次都写不对
        （数组写成字符串、dimensions 写成字符串、skus 缺必填子字段）。
        而这些都是**我们本来就确切掌握的数据**，交给代码从 source.json 派生更准；模型只负责
        卖点/推断/缺失项/风险/建议这些真正需要语言与判断的字段。
        """
        from contracts import validate_contract

        base = self._deterministic().analyze_product(request)
        facts_notes = enrich_facts_from_inputs(base, request)
        warnings: list[str] = ["facts 由代码从 input/source.json 派生；模型只写叙述字段"] + facts_notes

        narrative_prompt = build_narrative_prompt(request, facts=base.get("facts"))

        problems: list[str] = []
        prompt = narrative_prompt
        for attempt in range(1, self.max_attempts + 1):
            text = self.transport.complete(system=SYSTEM_JSON, user=prompt, temperature=self.temperature)
            narrative = extract_json(text)
            if narrative is None:
                problems = ["输出不是合法 JSON 对象（去掉解释文字或代码围栏）"]
            else:
                merged = dict(base)
                for key in ("selling_points", "inferences", "unknowns", "risks", "recommendation"):
                    if key in narrative and narrative[key] not in (None, [], {}):
                        merged[key] = narrative[key]
                problems = list(validate_contract("product-analysis", merged))
                self.calls.append(
                    {"task": "product_analysis", "attempt": attempt, "ok": not problems,
                     "problems": problems[:6], "chars": len(text)}
                )
                if not problems:
                    return merged
            if attempt < self.max_attempts:
                prompt = (
                    f"{narrative_prompt}\n\n### 上一次输出不合法，请修正后重新只输出 JSON\n"
                    + "\n".join(f"- {item}" for item in problems[:12])
                )
        if self.fallback_to_deterministic:
            # 契约里没有可放"降级说明"的字段，出处只留在 provider 调用轨迹与 run-report 里
            self.calls.append({"task": "product_analysis", "attempt": self.max_attempts + 1,
                               "ok": True, "problems": [], "note": "模型叙述不可用，已退化为确定性基座"})
            return base
        raise ModelError(
            f"product_analysis 连续 {self.max_attempts} 次未通过校验：" + "；".join(problems[:6])
        )

    def write_copy_ru(self, request: CopyRequest) -> dict[str, Any]:
        from contracts import validate_contract
        from rules.validate import copy_bundle_hint, validate_copy_bundle

        def validate(data: dict[str, Any]) -> list[str]:
            problems: list[str] = []
            bundle = data.get("copy_bundle") if isinstance(data.get("copy_bundle"), Mapping) else {}
            for key, contract in (("title_ru", "title-ru"), ("description_ru", "description-ru"), ("keywords_ru", "keywords-ru")):
                document = data.get(key)
                if not isinstance(document, Mapping):
                    problems.append(f"缺少 {key}（{contract} 契约要求的对象）")
                    continue
                problems.extend(f"{key}: {item}" for item in validate_contract(contract, document))
            problems.extend(f"copy_bundle: {item}" for item in validate_copy_bundle(bundle))
            return problems

        user = (
            "请基于采集数据、商品分析与已选关键词，产出俄文标题/简介/关键词三份文档 + copy_bundle。\n"
            f"{_schema_hint('title-ru')}\n{_schema_hint('description-ru')}\n{_schema_hint('keywords-ru')}\n"
            f"{copy_bundle_hint()}\n"
            "要求：标题 25–120 字符且包含核心词；简介至少 300 字符、五个部分都要写；"
            "标签（hashtags）只能是西里尔字母、形如 #термос；不要出现中文、拼音或未证实的参数。\n\n"
            + _context_block(
                source=request.source,
                analysis=request.analysis,
                selected_keywords=request.selected_keywords,
                positioning=request.positioning,
            )
        )
        payload, warnings = self._call_json(task="russian_copy", user=user, validate=validate)
        bundle = dict(payload.get("copy_bundle") or {})
        bundle.setdefault("generated_by", getattr(self.transport, "name", self.name))
        bundle["generated_by"] = f"{bundle['generated_by']}+http"
        bundle["warnings"] = list(bundle.get("warnings") or []) + warnings
        payload["copy_bundle"] = bundle
        return payload

    def position_product(self, request: PositionRequest) -> dict[str, Any]:
        from contracts import validate_contract

        user = (
            "请产出商品定位 JSON（product-positioning 契约）。\n"
            f"{_schema_hint('product-positioning')}\n"
            "要求：定位、买家画像、动机等必须能从数据推断；推断类写 claim_type=supported_inference；"
            "没有依据的字段写 null 并列入 unknowns；positioning_evidence 每条都要带 source_refs。\n\n"
            + _context_block(
                source=request.source,
                analysis=request.analysis,
                copy_bundle=request.copy_bundle,
                pricing=request.pricing,
            )
        )
        payload, _ = self._call_json(
            task="product_positioning",
            user=user,
            validate=lambda data: validate_contract("product-positioning", data),
            contract="product-positioning",
        )
        return payload

    def design_listing(self, request: DesignRequest) -> dict[str, Any]:
        """模型出买家可见文案，结构化部分交给确定性装配器（见模块 docstring）。"""
        from models.design import build_design_document

        copy_bundle = request.copy_bundle
        warnings: list[str] = []
        if not copy_bundle:
            copy = self.write_copy_ru(
                CopyRequest(
                    product_id=request.product_id,
                    product_dir=request.product_dir,
                    source=request.source,
                    source_refs=request.source_refs,
                    analysis=request.analysis,
                    selected_keywords=list(_keywords_of(request)),
                    positioning=request.positioning,
                )
            )
            copy_bundle = dict(copy.get("copy_bundle") or {})
            warnings.append("设计步骤内联调模型生成文案")

        design = build_design_document(
            product_id=request.product_id,
            source=request.source,
            analysis=request.analysis,
            copy_bundle=copy_bundle,
            image_plan=request.image_plan,
            attributes_final=request.attributes_final,
            positioning=request.positioning,
            source_refs=request.source_refs,
            generated_by=f"{getattr(self.transport, 'name', self.name)}+assembler",
        )
        processing = design.setdefault("processing", {})
        processing["validation_warnings"] = list(processing.get("validation_warnings") or []) + warnings
        self.last_notes.extend(warnings)
        return design

    def plan_images(self, request: ImagePlanRequest) -> dict[str, Any]:
        from models.image_plan import build_image_plan

        plan = build_image_plan(
            product_dir=request.product_dir,
            source=request.source,
            copy_bundle=request.copy_bundle,
            analysis=request.analysis,
            source_refs=request.source_refs,
        )
        self.last_notes.append(
            "图片计划由规则装配器生成（槽位/合成方式/叠字规则来自技能约束）；"
            "如需模型润色提示词，可在本 adapter 里接 prompt enrichment"
        )
        return plan

    def translate_terms(
        self,
        keywords: Sequence[str],
        context: Mapping[str, Any] | None = None,
    ) -> dict[str, str]:
        """俄文关键词 → 中文找货词（给 1688 搜索用）。只接受输入里出现过的键。"""
        wanted = [str(item).strip() for item in keywords if str(item).strip()]
        if not wanted:
            return {}

        def validate(data: dict[str, Any]) -> list[str]:
            terms = data.get("terms") if isinstance(data.get("terms"), Mapping) else data
            if not isinstance(terms, Mapping):
                return ["必须是 {俄文关键词: 中文找货词} 的对象"]
            unknown = [key for key in terms if str(key) not in wanted]
            problems = [f"出现了未要求的键：{item}" for item in unknown[:5]]
            empty = [key for key, value in terms.items() if not str(value or "").strip()]
            problems.extend(f"{item} 的中文词为空" for item in empty[:5])
            return problems

        user = (
            "把下面的俄文电商关键词逐个翻成**适合在 1688 搜索的中文词**（简洁的品类/材质/规格词，不要句子）。\n"
            '只输出 JSON：{"terms": {"<原俄文关键词>": "<中文找货词>"}}\n'
            "键必须与给定关键词完全一致，不要新增或遗漏。\n\n"
            + _context_block(keywords=wanted, context=dict(context or {}))
        )
        payload, _ = self._call_json(task="translate_terms", user=user, validate=validate)
        terms = payload.get("terms") if isinstance(payload.get("terms"), Mapping) else payload
        return {str(key): str(value).strip() for key, value in (terms or {}).items() if str(value).strip()}

    def generate_image(self, request: ImageRequest) -> dict[str, Any]:
        raise ModelError(
            "HTTP provider 不负责生图：请接入生图后端（见 HANDOFF §5.2），"
            "本 provider 只做文本类模型步骤"
        )


# --------------------------------------------------------------------- 入口


def build_provider_from_env(env: Mapping[str, str] | None = None, *, ark: bool = False) -> HttpModelProvider:
    config = ProviderConfig.ark_from_env(env) if ark else ProviderConfig.from_env(env)
    transport = OpenAICompatibleTransport(
        base_url=config.base_url,
        api_key=config.api_key,
        model=config.model,
        timeout=config.timeout,
        thinking=config.thinking,
        max_tokens=config.max_tokens,
    )
    return HttpModelProvider(
        transport,
        temperature=config.temperature,
        max_attempts=config.max_attempts,
        fallback_to_deterministic=config.fallback_to_deterministic,
    )


__all__ = [
    "ARK_BASE_URL",
    "ChatTransport",
    "HttpModelProvider",
    "OpenAICompatibleTransport",
    "ProviderConfig",
    "build_provider_from_env",
    "extract_json",
]
