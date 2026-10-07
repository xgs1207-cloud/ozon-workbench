"""提交后核对：读回 Ozon 上真实存下来的商品，逐项对账（**只读**）。

真提交跑通后，光看"imported"不够 —— 要确认 Ozon 那边**真的存下了**我们填的东西。
实测中这一层抓到过的问题类型：属性值丢失、变体没合并、图片没被 Ozon 转存、SKU 没分配。

检查项：
1. 每个 offer 都能在 Ozon 读回来；
2. Ozon 分配了真实 SKU（``sku`` 不为 0）；
3. 我们填的属性在 Ozon 上有值（逐条对比 ``ozon-attributes-final.json``）；
4. 图片被 Ozon 转存（主图变成 Ozon 自己的地址）；
5. 变体合并：所有 offer 的 ``model_info.model_id`` 相同，且 ``count`` 等于 offer 数。

用法::

    python -m pipeline.ozon_verify --product-dir products/P000006 --store default
    python -m pipeline.ozon_verify --product-dir products/P000006 --store default --json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit

from .ozon_status import _read_json, _transport_for_store

PATH_PRODUCT_ATTRIBUTES = "/v4/product/info/attributes"


def _attribute_text(value: Any) -> str:
    return str(value).lower() if isinstance(value, bool) else "" if value is None else str(value)


def _attributes_of(document: Mapping[str, Any], sku_id: str | None = None) -> dict[int, list[str]]:
    """Read only the current SKU's overrides, never another variant's values."""
    result: dict[int, list[str]] = {}
    for item in document.get("common_attributes") or []:
        if isinstance(item, Mapping) and item.get("attribute_id") is not None:
            value = _attribute_text(item.get("value")).strip()
            if value:
                result.setdefault(int(item["attribute_id"]), []).append(value)
    by_sku = document.get("attributes_by_sku") if isinstance(document.get("attributes_by_sku"), Mapping) else {}
    for rows in ([by_sku.get(sku_id) or []] if sku_id is not None else []):
        override_ids = {int(row["attribute_id"]) for row in rows if isinstance(row, Mapping)
                        and row.get("attribute_id") is not None}
        for attribute_id in override_ids:
            result.pop(attribute_id, None)
        for item in rows or []:
            if isinstance(item, Mapping) and item.get("attribute_id") is not None:
                value = _attribute_text(item.get("value")).strip()
                if value:
                    result.setdefault(int(item["attribute_id"]), []).append(value)
    return result


def _expected_for_variant(payload: Mapping[str, Any], variant: Mapping[str, Any]) -> dict[int, list[dict[str, Any]]]:
    common = [row for row in payload.get("attributes") or [] if isinstance(row, Mapping)]
    overrides = [row for row in variant.get("attributes") or [] if isinstance(row, Mapping)]
    ids = {row.get("attribute_id") for row in overrides}
    result: dict[int, list[dict[str, Any]]] = {}
    for row in [*(row for row in common if row.get("attribute_id") not in ids), *overrides]:
        if row.get("attribute_id") is None:
            continue
        result.setdefault(int(row["attribute_id"]), []).append({
            "value": _attribute_text(row.get("value")),
            "dictionary_value_id": row.get("dictionary_value_id"),
        })
    return result


def verify_submitted(
    product_dir: Path | str,
    *,
    store_id: str,
    registry_path: Path | str | None = None,
    transport: Any | None = None,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    product = Path(product_dir)
    ledger = _read_json(product / "output" / "store-publications.json")
    store_entry = ((ledger.get("stores") or {}).get(store_id) or {}) if isinstance(ledger, Mapping) else {}
    offer_ids = [
        str(item.get("offer_id"))
        for item in (store_entry.get("sku_publications") or [])
        if isinstance(item, Mapping) and item.get("offer_id")
    ]
    if not offer_ids:
        return {
            "ok": False,
            "product_id": product.name,
            "store": store_id,
            "error": "台账里没有这个店铺的 offer_id（先提交并跑 pipeline.ozon_status 确认）",
            "checks": [],
            "api_writes_performed": False,
        }

    if transport is None:
        transport = _transport_for_store(
            store_id, transport_factory=None, registry_path=registry_path, env=env
        )
    response = transport.post(PATH_PRODUCT_ATTRIBUTES, {"filter": {"offer_id": offer_ids}, "limit": 100})
    items = response.get("result") or response.get("items") or []
    by_offer = {str(item.get("offer_id")): item for item in items if isinstance(item, Mapping)}

    compiled_attributes = _read_json(product / "output" / "ozon-attributes-final.json")
    payload = _read_json(product / "output/store-runs" / store_id / "payload.json")
    variants = {str(row.get("offer_id")): row for row in payload.get("variants") or []
                if isinstance(row, Mapping)}
    source_skus = {str(row.get("offer_id")): str(row.get("sku_id") or "")
                   for row in store_entry.get("sku_publications") or [] if isinstance(row, Mapping)}
    grouping = _read_json(product / "output/platform-grouping-result.json")
    must_merge = (payload.get("product_group") or {}).get("must_merge")
    if must_merge is None:
        strategy = grouping.get("upload_strategy")
        must_merge = strategy == "merged_variants" if strategy else len(offer_ids) > 1
    checks: list[dict[str, Any]] = []

    missing_offers = [offer for offer in offer_ids if offer not in by_offer]
    checks.append(
        {
            "name": "offers_readable",
            "ok": not missing_offers,
            "detail": f"读回 {len(by_offer)}/{len(offer_ids)}" + (f"；缺 {missing_offers}" if missing_offers else ""),
        }
    )

    model_ids: set[Any] = set()
    for offer in offer_ids:
        item = by_offer.get(offer)
        if not item:
            continue
        sku = item.get("sku")
        checks.append(
            {
                "name": f"sku_assigned[{offer}]",
                "ok": str(sku or "").isdigit() and int(sku) > 0,
                "detail": f"sku={sku}",
            }
        )
        stored = {
            int(attribute["id"]): [
                str(value.get("value") or "")
                for value in (attribute.get("values") or [])
                if isinstance(value, Mapping)
            ]
            for attribute in (item.get("attributes") or [])
            if isinstance(attribute, Mapping) and attribute.get("id") is not None
        }
        variant = variants.get(offer)
        expected = (_expected_for_variant(payload, variant) if variant is not None else {
            key: [{"value": value} for value in values] for key, values in
            _attributes_of(compiled_attributes, source_skus.get(offer)).items()})
        stored_ids = {int(attribute["id"]): {int(value.get("dictionary_value_id") or 0)
            for value in attribute.get("values") or [] if isinstance(value, Mapping)}
            for attribute in item.get("attributes") or [] if isinstance(attribute, Mapping)
            and attribute.get("id") is not None}
        for attribute_id, values in expected.items():
            # Dictionary IDs are authoritative; text attributes compare the
            # expected values for this exact offer, not the union of all SKUs.
            wanted = {value["value"] for value in values if value.get("value")}
            stored_values = set(stored.get(attribute_id) or [])
            ok = all((int(value.get("dictionary_value_id") or 0) in stored_ids.get(attribute_id, set()))
                if int(value.get("dictionary_value_id") or 0) > 0 else value.get("value") in stored_values
                for value in values)
            # 类型(8229) 等由类和型决定，Ozon 可能不放在 attributes 里 → 只提示不判失败
            soft = attribute_id in {8229}
            checks.append(
                {
                    "name": f"attribute[{offer}][{attribute_id}]",
                    "ok": ok or soft,
                    "soft": soft,
                    "detail": f"期望 {sorted(wanted)}｜Ozon 上 {sorted(stored_values) or '（无此属性）'}",
                }
            )
        primary = str(item.get("primary_image") or "")
        image_host = (urlsplit(primary).hostname or "").casefold()
        checks.append(
            {
                "name": f"image_rehosted[{offer}]",
                "ok": bool(primary) and (image_host == "ozone.ru" or image_host.endswith(".ozone.ru")),
                "detail": primary or "（没有主图）",
            }
        )
        model_info = item.get("model_info") or {}
        if isinstance(model_info, Mapping) and model_info.get("model_id"):
            model_ids.add(model_info.get("model_id"))
            count = int(model_info.get("count") or 0)
            if must_merge:
                checks.append(
                    {
                        "name": f"variant_merged[{offer}]",
                        "ok": count >= len(offer_ids),
                        "detail": f"model_id={model_info.get('model_id')}｜count={count}（offer 数 {len(offer_ids)}）",
                    }
                )

    if len(offer_ids) > 1 and must_merge:
        checks.append(
            {
                "name": "same_model_id",
                "ok": len(model_ids) == 1 and bool(model_ids),
                "detail": f"model_id 集合={sorted(str(item) for item in model_ids)}",
            }
        )

    failed = [check for check in checks if not check["ok"] and not check.get("soft")]
    return {
        "ok": not failed,
        "product_id": product.name,
        "store": store_id,
        "offers": offer_ids,
        "checks": checks,
        "failed": [check["name"] for check in failed],
        "soft_failures": [check["name"] for check in checks if not check["ok"] and check.get("soft")],
        "api_writes_performed": False,
        "note": "只读核对：读的是 Ozon 服务端上真实存下来的数据",
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="提交后核对：读回 Ozon 上的商品逐项对账（只读）")
    parser.add_argument("--product-dir", required=True)
    parser.add_argument("--store", required=True)
    parser.add_argument("--registry", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = verify_submitted(args.product_dir, store_id=args.store, registry_path=args.registry)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"商品 {report['product_id']} → 店铺 {report['store']}｜核对：{'✅ 全部通过' if report['ok'] else '❌ 有未通过项'}")
        if report.get("error"):
            print("  ", report["error"])
        for check in report["checks"]:
            mark = "✅" if check["ok"] else ("⚠️" if check.get("soft") else "❌")
            print(f"  {mark} {check['name']}：{check['detail']}")
        if report.get("soft_failures"):
            print("  （⚠️ 为提示项：这类属性由 Ozon 的类目/型决定，可能不出现在 attributes 里）")
    return 0 if report["ok"] else 1


if __name__ == "__main__":  # pragma: no cover - 命令行入口
    raise SystemExit(main())
