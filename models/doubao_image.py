"""豆包（火山方舟 Ark）生图适配器：``/api/v3/images/generations``。

用户选择：**生图用豆包**。这里实现"参考图 + 提示词 → 整套图片"的接入点：

- **参考图是事实锁**：把 ``image-plan.json`` 里该槽位的 ``reference_product_images`` 以
  ``data:image/png;base64,...`` 形式随请求发给模型（图生图/编辑），而不是"描述一遍让它画"；
- **只读计划、不改计划**：槽位、输出路径、叠字文案全部来自 ``output/image-plan.json``；
- **尺寸按我们的硬约束**：默认请求 3:4 尺寸，并在返回后**可选地用 Pillow 归一化**成 900×1200、
  仅 png（我们自己的 QC 会再验一遍；没有 Pillow 时如实报错而不是悄悄放过）；
- **不叠字**：``watermark=false``，提示词里也禁中文/水印（技能规则）；
- **保守重试**：明确的 429/5xx 才重试；连接层异常不盲目重试（生图是计费调用）；
- 凭据只从环境变量读：``ARK_API_KEY``（必需）、``ARK_BASE_URL``、``ARK_IMAGE_MODEL``、``ARK_IMAGE_SIZE``。

⚠️ 模型名与可用尺寸以火山方舟当前文档为准：``ARK_IMAGE_MODEL`` 建议填你在控制台创建的**接入点 ID**
或官方模型名；``ARK_IMAGE_SIZE`` 若不被支持，接口会返回明确错误（我们把它原样抛出，不猜）。
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping, Sequence

from .base import ImageRequest, ModelError

DEFAULT_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
DEFAULT_MODEL = "doubao-seedream-3-0-t2i-250415"
DEFAULT_SIZE = "1152x1536"  # 3:4；若你的模型不支持，用 ARK_IMAGE_SIZE 覆盖
DEFAULT_TIMEOUT = 180
DEFAULT_MAX_ATTEMPTS = 2
RETRY_STATUSES = (429, 500, 502, 503, 504)
MAX_REFERENCE_IMAGES = 3
MAX_REFERENCE_BYTES = 8 * 1024 * 1024
TARGET_SIZE = (900, 1200)  # 我们的 QC 要求：3:4 且 ≥900×1200

ENV_KEYS = ("ARK_API_KEY",)
DATA_URL_PATTERN = re.compile(r"^data:(?P<mime>image/[a-zA-Z0-9.+-]+);base64,", re.IGNORECASE)


def _mime_for(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(suffix, "image/png")


class ArkImageTransport:
    """``POST {base_url}/images/generations``（注入式 ``urlopen``，便于离线测试）。"""

    name = "ark-images"

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        timeout: int = DEFAULT_TIMEOUT,
        watermark: bool = False,
        urlopen: Any | None = None,
    ) -> None:
        cleaned = str(base_url).strip().rstrip("/")
        if not cleaned.startswith(("http://", "https://")):
            raise ModelError(f"ARK_BASE_URL 必须是 http(s) 地址：{base_url!r}")
        if not str(api_key).strip():
            raise ModelError("ARK_API_KEY 为空")
        self.api_key = str(api_key).strip()
        self.model = str(model).strip() or DEFAULT_MODEL
        self.base_url = cleaned
        self.timeout = timeout
        self.watermark = watermark
        self._urlopen = urlopen or urllib.request.urlopen

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/images/generations"

    def build_request(self, body: Mapping[str, Any]) -> urllib.request.Request:
        return urllib.request.Request(
            self.endpoint,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
            method="POST",
        )

    def build_body(
        self,
        *,
        prompt: str,
        images: Sequence[str] = (),
        size: str | None = None,
        seed: int | None = None,
        response_format: str = "b64_json",
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "size": size or DEFAULT_SIZE,
            "response_format": response_format,
            "watermark": bool(self.watermark),
        }
        if images:
            body["image"] = list(images) if len(images) > 1 else images[0]
        if seed is not None:
            body["seed"] = int(seed)
        return body

    # ---------------------------------------------------------------- HTTP

    def _post(self, body: Mapping[str, Any]) -> dict[str, Any]:
        request = self.build_request(body)
        try:
            with self._urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            detail = ""
            try:
                detail = error.read().decode("utf-8")[:400]
            except Exception:  # noqa: BLE001
                detail = ""
            raise ModelError(f"豆包生图接口返回 HTTP {error.code}：{detail or error.reason}") from error
        except urllib.error.URLError as error:
            raise ModelError(f"无法连接豆包生图接口：{error.reason}") from error
        try:
            payload = json.loads(raw)
        except ValueError as error:
            raise ModelError(f"豆包生图接口返回的不是 JSON：{raw[:200]}") from error
        if isinstance(payload, Mapping) and isinstance(payload.get("error"), Mapping):
            error_body = payload["error"]
            raise ModelError(
                f"豆包生图失败（{error_body.get('code') or 'unknown'}）：{error_body.get('message') or ''}"
            )
        if not isinstance(payload, Mapping):
            raise ModelError("豆包生图接口返回的不是 JSON 对象")
        return dict(payload)

    def _download(self, url: str) -> bytes:
        request = urllib.request.Request(url, method="GET")
        try:
            with self._urlopen(request, timeout=self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            raise ModelError(f"下载生图结果失败（HTTP {error.code}）：{url[:120]}") from error
        except urllib.error.URLError as error:
            raise ModelError(f"下载生图结果失败：{error.reason}") from error

    def generate(
        self,
        *,
        prompt: str,
        images: Sequence[str] = (),
        size: str | None = None,
        seed: int | None = None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> list[bytes]:
        """返回图片字节（b64_json 直接解码；url 形式则下载）。"""
        body = self.build_body(prompt=prompt, images=images, size=size, seed=seed)
        attempts = 0
        last_error: ModelError | None = None
        while attempts < max(1, int(max_attempts)):
            attempts += 1
            try:
                payload = self._post(body)
                break
            except ModelError as error:
                last_error = error
                message = str(error)
                retryable = any(f"HTTP {status}" in message for status in RETRY_STATUSES)
                if not retryable or attempts >= max(1, int(max_attempts)):
                    raise
        else:  # pragma: no cover - 兜底
            raise last_error or ModelError("豆包生图失败")

        data = payload.get("data")
        if not isinstance(data, list) or not data:
            raise ModelError(f"豆包生图响应里没有 data：{json.dumps(payload, ensure_ascii=False)[:200]}")
        results: list[bytes] = []
        for item in data:
            if not isinstance(item, Mapping):
                continue
            if item.get("b64_json"):
                try:
                    results.append(base64.b64decode(str(item["b64_json"])))
                except (ValueError, TypeError) as error:
                    raise ModelError(f"base64 图片解码失败：{error}") from error
            elif item.get("url"):
                results.append(self._download(str(item["url"])))
        if not results:
            raise ModelError("豆包生图响应里没有可用图片（既无 b64_json 也无 url）")
        return results


def normalize_to_qc_png(data: bytes, target: tuple[int, int] = TARGET_SIZE) -> bytes:
    """把图片归一化成严格 3:4 且**尺寸等于目标**（900×1200）的 PNG。

    真机踩坑：早先只在"比目标小"时才缩放，于是 seedream 返回的 1920×2560 直接落盘，
    同一个商品里主图 1920×2560、详情图 900×1200，尺寸不一致。现在统一裁成 3:4 后
    **按目标尺寸缩放**（Ozon 主图推荐就是 900×1200，文件也更小、上传更快）。
    """
    try:
        import io

        from PIL import Image
    except ImportError as error:  # pragma: no cover - 环境里已装
        raise ModelError("图片归一化需要 Pillow（pip install Pillow），或关闭 normalize") from error

    with Image.open(io.BytesIO(data)) as image:
        image = image.convert("RGB")
        width, height = image.size
        target_ratio = target[0] / target[1]
        current_ratio = width / height
        if abs(current_ratio - target_ratio) > 0.002:
            # 按 3:4 居中裁剪，避免拉伸变形
            if current_ratio > target_ratio:
                new_width = int(round(height * target_ratio))
                left = max(0, (width - new_width) // 2)
                image = image.crop((left, 0, left + new_width, height))
            else:
                new_height = int(round(width / target_ratio))
                top = max(0, (height - new_height) // 2)
                image = image.crop((0, top, width, top + new_height))
        if image.size != target:
            image = image.resize(target, Image.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()


class DoubaoImageGenerator:
    """按图片计划逐槽位出图（参考图 + 提示词 → 本地 png）。"""

    name = "doubao"
    produces_final_images = True

    def __init__(
        self,
        transport: ArkImageTransport,
        *,
        size: str = DEFAULT_SIZE,
        use_reference: bool = True,
        max_reference_images: int = MAX_REFERENCE_IMAGES,
        normalize: bool = True,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        slot_filter: Sequence[str] | None = None,
    ) -> None:
        self.transport = transport
        self.size = size
        self.use_reference = use_reference
        self.max_reference_images = max_reference_images
        self.normalize = normalize
        self.max_attempts = max_attempts
        self.slot_filter = set(slot_filter) if slot_filter else None

    # ---------------------------------------------------------------- 计划

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, Mapping) else {}

    def _planned_slots(self, product_dir: Path) -> list[dict[str, Any]]:
        plan = self._read_json(product_dir / "output" / "image-plan.json")
        slots = [
            item
            for item in (list(plan.get("main_images") or []) + list(plan.get("detail_images") or []))
            if isinstance(item, Mapping)
        ]
        if not slots:
            raise ModelError("缺少 output/image-plan.json：先生成图片计划再出图")
        if self.slot_filter:
            slots = [item for item in slots if str(item.get("slot")) in self.slot_filter]
            if not slots:
                raise ModelError(f"过滤后没有可出图的槽位：{sorted(self.slot_filter)}")
        return slots

    def _reference_payload(self, product_dir: Path, slot: Mapping[str, Any]) -> list[str]:
        if not self.use_reference:
            return []
        paths = [str(item) for item in (slot.get("reference_product_images") or [])]
        if not paths:
            # 回退：用该 SKU 的采集原图
            sku_id = str(slot.get("source_sku_id") or "").strip()
            directory = product_dir / "input" / ("sku-images" if sku_id else "main-images")
            paths = [str(item.relative_to(product_dir)) for item in sorted(directory.glob("*.png"))[:1]] if directory.is_dir() else []
        payload: list[str] = []
        for relative in paths[: self.max_reference_images]:
            target = product_dir / relative
            if not target.is_file() or target.stat().st_size > MAX_REFERENCE_BYTES:
                continue
            data = base64.b64encode(target.read_bytes()).decode("ascii")
            payload.append(f"data:{_mime_for(target)};base64,{data}")
        return payload

    # ---------------------------------------------------------------- 出图

    def generate(self, request: ImageRequest) -> dict[str, Any]:
        product_dir = Path(request.product_dir)
        slots = self._planned_slots(product_dir)
        generated: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []

        for slot in slots:
            slot_name = str(slot.get("slot") or "unknown")
            prompt = str(slot.get("prompt") or "").strip()
            if not prompt:
                skipped.append({"slot": slot_name, "reason": "计划里没有提示词"})
                continue
            relative = str(slot.get("output_path") or f"output/generated-images/{slot_name}.png")
            target = product_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                images = self.transport.generate(
                    prompt=prompt,
                    images=self._reference_payload(product_dir, slot),
                    size=self.size,
                    max_attempts=self.max_attempts,
                )
            except ModelError as error:
                skipped.append({"slot": slot_name, "reason": str(error)})
                continue
            data = images[0]
            if self.normalize:
                data = normalize_to_qc_png(data)
            target.write_bytes(data)
            generated.append(
                {
                    "slot": slot_name,
                    "path": relative,
                    "bytes": len(data),
                    "reference_images": len(self._reference_payload(product_dir, slot)),
                }
            )

        if not generated:
            reasons = "；".join(f"{item['slot']}: {item['reason']}" for item in skipped[:3]) or "没有可出图的槽位"
            raise ModelError(f"豆包生图全部失败：{reasons}")

        note = f"豆包生图（{self.transport.model}，尺寸请求 {self.size}"
        note += "，返回后归一化为 900×1200 png）" if self.normalize else "）"
        return {
            "generator": self.name,
            "final_images": True,
            "generated": generated,
            "skipped": skipped,
            "note": note,
            "model": self.transport.model,
        }

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **overrides: Any) -> "DoubaoImageGenerator":
        source = env if env is not None else os.environ
        api_key = str(source.get("ARK_API_KEY") or "").strip()
        if not api_key:
            raise ModelError(
                "缺少 ARK_API_KEY：请在火山方舟控制台创建 API Key 并设置为环境变量"
                "（可选：ARK_BASE_URL / ARK_IMAGE_MODEL / ARK_IMAGE_SIZE）"
            )
        transport = ArkImageTransport(
            api_key=api_key,
            model=str(source.get("ARK_IMAGE_MODEL") or DEFAULT_MODEL),
            base_url=str(source.get("ARK_BASE_URL") or DEFAULT_BASE_URL),
        )
        size = str(source.get("ARK_IMAGE_SIZE") or DEFAULT_SIZE)
        options: dict[str, Any] = {"size": size}
        options.update(overrides)
        return cls(transport, **options)


# --------------------------------------------------------------------- CLI


def main(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="豆包生图：按图片计划出图（或只看将发送的请求）")
    parser.add_argument("--product-dir", required=True)
    parser.add_argument("--slot", action="append", dest="slots", help="只出这些槽位（可重复）")
    parser.add_argument("--show-request", action="store_true", help="只打印请求摘要（不联网、不生成）")
    parser.add_argument("--limit", type=int, default=None, help="最多出几张（试跑用）")
    parser.add_argument("--no-reference", action="store_true", help="不发送参考图（纯文生图）")
    args = parser.parse_args(argv)

    product_dir = Path(args.product_dir)
    try:
        generator = DoubaoImageGenerator.from_env(
            slot_filter=args.slots,
            use_reference=not args.no_reference,
            normalize=not args.show_request,
        )
    except ModelError as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False, indent=2))
        return 1

    if args.show_request:
        slots = generator._planned_slots(product_dir)
        if args.limit:
            slots = slots[: args.limit]
        preview = []
        for slot in slots:
            references = generator._reference_payload(product_dir, slot)
            preview.append(
                {
                    "slot": slot.get("slot"),
                    "output_path": slot.get("output_path"),
                    "size": generator.size,
                    "model": generator.transport.model,
                    "reference_images": len(references),
                    "prompt_chars": len(str(slot.get("prompt") or "")),
                    "russian_text": slot.get("russian_text"),
                }
            )
        print(
            json.dumps(
                {
                    "ok": True,
                    "mode": "show-request",
                    "api": f"POST {generator.transport.endpoint}",
                    "api_writes_performed": False,
                    "slots": preview,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    slots = generator._planned_slots(product_dir)
    if args.limit:
        generator.slot_filter = {str(item.get("slot")) for item in slots[: args.limit]}
    try:
        result = generator.generate(
            ImageRequest(product_id=product_dir.name, product_dir=product_dir, source={})
        )
    except ModelError as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False, indent=2))
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "generator": result["generator"],
                "model": result["model"],
                "images": len(result["generated"]),
                "skipped": result["skipped"],
                "note": result["note"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
