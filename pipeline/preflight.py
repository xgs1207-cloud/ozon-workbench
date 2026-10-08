"""提交前预检：**只读、不发任何写请求**，把真提交会踩的坑提前找出来。

检查项：
1. 店铺：是否存在、是否 enabled、凭据是否就绪（密钥只在环境变量里，不打印）；
2. 载荷：按 ``production`` 模式构建，跑契约校验 + 阻断项 + 库存字段禁令；
3. 图片：公网地址是否真的能被匿名取到（HTTP 200）——"桶没公开读"就是在这里被抓住的；
4. 属性：必填是否填满（缺哪个 id 说清楚）。

用法::

    python -m pipeline.preflight --product-dir products/P000002 --shop default
    python -m pipeline.preflight --product-dir products/P000006 --shop default --no-url-check --json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .stores import credential_report, ensure_registry, list_shops
from .upload import IMAGE_URLS_FILE, build_upload_payload, payload_problems

DEFAULT_TIMEOUT = 25


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def check_image_urls(
    urls: Sequence[str],
    *,
    urlopen: Callable[..., Any] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    limit: int = 12,
    expected_content: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """匿名 GET 我们的图片地址（Ozon 也是匿名来取的）。"""
    opener = urlopen or urllib.request.urlopen
    results: list[dict[str, Any]] = []
    for url in list(urls)[:limit]:
        item: dict[str, Any] = {"url": url, "ok": False, "status": None, "bytes": None, "error": None}
        try:
            request = urllib.request.Request(str(url), method="GET")
            with opener(request, timeout=timeout) as response:
                body = response.read()
                item.update(ok=int(getattr(response, "status", 200)) == 200, status=getattr(response, "status", None), bytes=len(body))
                expected = (expected_content or {}).get(url)
                if expected is not None:
                    item["content_matches"] = (len(body) == expected.get("size_bytes")
                                               and hashlib.sha256(body).hexdigest() == expected.get("sha256"))
                    if not item["content_matches"]:
                        item.update(ok=False, error="公开图片内容与当前已审核文件不一致，请重新公开图片")
        except urllib.error.HTTPError as error:
            item.update(status=error.code, error=f"HTTP {error.code}")
        except Exception as error:  # noqa: BLE001
            item["error"] = f"{type(error).__name__}: {error}"
        results.append(item)
    return results


def preflight(
    product_dir: Path | str,
    *,
    shop: str,
    registry_path: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    verify_urls: bool = True,
    urlopen: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    product = Path(product_dir)
    registry = ensure_registry(registry_path)
    shops = {str(item.get("id")): item for item in list_shops(registry)}
    shop_entry = shops.get(str(shop))
    credentials = next((row for row in credential_report(registry, env) if str(row["shop_id"]) == str(shop)), None)

    problems: list[str] = []
    if shop_entry is None:
        problems.append(f"店铺不存在：{shop}（可用：{', '.join(shops) or '（无）'}）")
    else:
        if not shop_entry.get("enabled"):
            problems.append(f"店铺 {shop} 未启用（python -m pipeline.stores --enable {shop}）")
        if credentials and not credentials["ready"]:
            problems.append(f"店铺 {shop} 缺凭据：{', '.join(credentials['missing_env'])}")

    urls_payload = _read_json(product / IMAGE_URLS_FILE)
    image_urls = urls_payload.get("urls") if isinstance(urls_payload.get("urls"), Mapping) else urls_payload
    if not image_urls:
        problems.append("图片还没发布到对象存储（缺 output/image-public-urls.json）")
    from .oss_cos import image_publication_binding
    binding = image_publication_binding(product, urls_payload)
    problems.extend(binding["problems"])
    from .rich_content import publication_binding
    try:
        rich_binding = publication_binding(product)
    except ValueError as error:
        rich_binding = {"urls": [], "expected_content": {}}
        problems.append(str(error))

    payload = build_upload_payload(
        product_dir=product,
        shop_name=shop,
        upload_mode="production",
        image_urls=image_urls or {},
        currency_code=str((shop_entry or {}).get("default_currency_code") or "").upper() or None,
    )
    payload_blockers = list(payload.get("production_blockers") or [])
    contract_problems = [
        item for item in payload_problems(payload, upload_mode="production")
        if not item.startswith("production 模式下存在阻断项")
    ]
    problems.extend(f"载荷：{item}" for item in contract_problems)

    # 币种一致性：真机踩坑——店铺合同是 CNY，我们提交了 RUB，Ozon 直接拒收
    shop_currency = str((shop_entry or {}).get("default_currency_code") or "").upper()
    variant_currencies = sorted(
        {
            str(item.get("currency_code") or "").upper()
            for item in (payload.get("variants") or [])
            if isinstance(item, Mapping) and item.get("currency_code")
        }
    )
    if shop_currency and variant_currencies and shop_currency not in variant_currencies:
        problems.append(
            f"载荷币种 {variant_currencies} 与店铺合同币种 {shop_currency} 不一致"
            "（Ozon 会报 currency_differs_from_contract）"
        )

    attributes = payload.get("attributes") or []
    variants = payload.get("variants") or []
    gate = payload.get("image_upload_gate") or {}

    url_checks: list[dict[str, Any]] = []
    if verify_urls and image_urls:
        url_checks = check_image_urls(
            [str(item.get("url")) for item in (payload.get("images") or []) if isinstance(item, Mapping) and item.get("url")],
            urlopen=urlopen,
            limit=len(payload.get("images") or []),
            expected_content=binding.get("expected_content"),
        )
        for check in url_checks:
            if not check["ok"]:
                problems.append(f"图片取不到（匿名 GET 失败）：{check['url']}｜{check.get('error') or check.get('status')}")
    if verify_urls and rich_binding["urls"]:
        rich_checks = check_image_urls(rich_binding["urls"], urlopen=urlopen,
            limit=len(rich_binding["urls"]), expected_content=rich_binding["expected_content"])
        url_checks.extend(rich_checks)
        for check in rich_checks:
            if not check["ok"]:
                problems.append("富内容公开图片无法访问或内容不一致：" + str(check["url"]))

    return {
        "ok": not problems and not payload_blockers,
        "product_id": product.name,
        "shop": shop,
        "shop_enabled": bool(shop_entry.get("enabled")) if shop_entry else False,
        "credentials_ready": bool(credentials and credentials["ready"]),
        "payload_contract_problems": contract_problems,
        "production_blockers": payload_blockers,
        "problems": problems,
        "image_upload_gate": gate,
        "image_publication_binding": {key: value for key, value in binding.items() if key != "expected_content"},
        "attributes": len(attributes),
        "variants": len(variants),
        "currency": sorted(
            {
                str(item.get("currency_code") or "").upper()
                for item in variants
                if isinstance(item, Mapping) and item.get("currency_code")
            }
        ),
        "shop_currency": shop_currency,
        "images": len(payload.get("images") or []),
        "url_checks": url_checks,
        "api_endpoint": (payload.get("api_request_template") or {}).get("api"),
        "api_writes_performed": False,
        "note": "只读预检：没有向 Ozon 发送任何请求",
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="提交前预检（只读：不发任何写请求）")
    parser.add_argument("--product-dir", required=True)
    parser.add_argument("--shop", required=True, help="店铺 id（config/shops.json 里的 id）")
    parser.add_argument("--registry", default=None)
    parser.add_argument("--no-url-check", action="store_true", help="跳过图片匿名可达性检查")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = preflight(
        Path(args.product_dir), shop=args.shop, registry_path=args.registry, verify_urls=not args.no_url_check
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"商品 {report['product_id']} → 店铺 {report['shop']}｜预检结论：{'✅ 可以提交' if report['ok'] else '❌ 还不能提交'}")
        print(f"  店铺启用：{report['shop_enabled']}｜凭据就绪：{report['credentials_ready']}")
        print(f"  载荷：属性 {report['attributes']} 条｜变体 {report['variants']} 个｜图片 {report['images']} 张｜接口 {report['api_endpoint']}")
        gate = report["image_upload_gate"]
        print(f"  图片门禁：passed={gate.get('passed')}｜缺槽位={gate.get('missing_slots')}｜https_only={gate.get('https_only')}")
        for check in report["url_checks"]:
            print(f"  图片可达性：HTTP {check['status']}｜{check['bytes']} 字节｜{check['url']}")
        for item in report["production_blockers"]:
            print(f"  ⛔ 阻断：{item}")
        for item in report["problems"]:
            print(f"  ⚠️ {item}")
        if report["ok"]:
            print("  → 预检通过。真提交是**写操作**，需要显式 production 模式 + 你确认。")
    return 0 if report["ok"] else 1


if __name__ == "__main__":  # pragma: no cover - 命令行入口
    raise SystemExit(main())
