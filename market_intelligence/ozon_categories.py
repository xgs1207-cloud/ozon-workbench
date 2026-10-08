"""Search real Ozon Seller API leaf categories, with a one-day local cache."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

from pipeline.ozon_http import OzonClient, OzonCredentials, UrllibTransport
from pipeline.stores import ensure_registry, list_shops
from pipeline.category_cache import read_cache, shop_generation, write_cache


def _client(shop_id: str | None) -> tuple[OzonClient, str]:
    from pipeline.shop_authorization import select_read_shop

    registry = ensure_registry(None)
    shops = list_shops(registry)
    selected = shop_id or registry.get("default_read_shop")
    # Legacy registries may predate default_read_shop. Never replace an explicit
    # or configured default that is disabled, invalid or expired with another shop.
    if not selected:
        selected = next((str(item["id"]) for item in shops if item.get("enabled")), None)
    shop = select_read_shop(registry, selected)
    credentials = OzonCredentials.from_shop(shop)
    return OzonClient(UrllibTransport(credentials)), str(shop["id"])


def load_tree(cache_root: Path | str, *, shop_id: str | None = None, refresh: bool = False,
              client: OzonClient | None = None, language: str = "ZH_HANS") -> dict[str, Any]:
    resolved_id = str(shop_id or "injected-client")
    if client is None:
        client, resolved_id = _client(shop_id)
    scope = hashlib.sha256(f"{resolved_id}:{language}".encode("utf-8")).hexdigest()[:24]
    target = Path(cache_root) / f"ozon-category-tree-{scope}.json"
    generation = shop_generation(cache_root, resolved_id)
    cached = read_cache(target, ttl=86400) if not refresh else None
    if (cached and cached.get("result") and cached.get("shop_id") == resolved_id
            and cached.get("language") == language):
        return {**cached, "cache_hit": True}
    tree = client.fetch_category_tree(language=language)
    if not isinstance(tree.get("result"), list) or not tree["result"]:
        raise ValueError("Ozon 返回空类目树，无法确认真实类目")
    from datetime import datetime, timezone
    tree = {**tree, "shop_id": resolved_id, "language": language,
            "cache_generation": generation,
            "fetched_at": datetime.now(timezone.utc).isoformat(), "cache_hit": False,
            "source": "ozon_seller_api", "api_endpoint": "/v1/description-category/tree"}
    write_cache(target, tree)
    return tree


def leaf_categories(tree: Mapping[str, Any]) -> list[dict[str, Any]]:
    leaves: list[dict[str, Any]] = []

    def walk(node: Mapping[str, Any], names: list[str], category_id: int | None) -> None:
        if node.get("disabled") is True:
            return
        name = str(node.get("category_name") or node.get("type_name") or "").strip()
        path = [*names, name] if name else names
        current_id = node.get("description_category_id") or category_id
        type_id = node.get("type_id")
        if type_id and current_id and not node.get("children"):
            leaves.append({"category_id": int(current_id), "type_id": int(type_id),
                           "name": name, "path": path, "source": "ozon_seller_api"})
        for child in node.get("children") or []:
            if isinstance(child, Mapping):
                walk(child, path, int(current_id) if current_id else None)

    for root in tree.get("result") or []:
        if isinstance(root, Mapping):
            walk(root, [], None)
    return leaves


def search(tree: Mapping[str, Any], query: str, *, limit: int = 30) -> list[dict[str, Any]]:
    text = query.strip().casefold()
    if len(text) < 2:
        return []
    rows = [row for row in leaf_categories(tree) if text in " / ".join(row["path"]).casefold()]
    rows.sort(key=lambda row: (text not in row["name"].casefold(), len(row["path"]), row["name"]))
    return rows[:limit]
