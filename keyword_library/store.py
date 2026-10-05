"""关键词库存储层：按类目一个 JSONL 文件，追加/合并写入，原子替换。

设计取舍：
- 一个 ``(category_id, type_id)`` 一个文件，便于"按类目区分"和单类目重算分位数；
- 去重键 = 归一化关键词 + 类目，重复采集只更新指标与 ``last_seen_at``，
  不动 ``first_seen_at`` / ``status`` / ``selected_for``；
- 写文件用临时文件 + ``os.replace``，避免中途失败留下半个文件；
- 断点即文件本身，不需要数据库（后续要并发再加锁）。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .scoring import ScoreConfig, score_records

STATUS_CANDIDATE = "candidate"
STATUS_QUALIFIED = "qualified"
STATUS_IN_LIBRARY = "in_library"
STATUS_USED = "used"
STATUS_REJECTED = "rejected"
VALID_STATUSES = (
    STATUS_CANDIDATE,
    STATUS_QUALIFIED,
    STATUS_IN_LIBRARY,
    STATUS_USED,
    STATUS_REJECTED,
)

DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "keyword-library"
INDEX_NAME = "index.json"

_METRIC_FIELDS = ("search_volume", "competitor_count", "ads_count", "cpc", "trend")


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def normalize_keyword(text: Any) -> str:
    """归一化关键词：NFKC、去首尾空白、压缩内部空白、小写（俄文安全）。"""
    if text is None:
        return ""
    value = unicodedata.normalize("NFKC", str(text)).strip().casefold()
    return re.sub(r"\s+", " ", value)


def record_key(category_id: Any, type_id: Any, keyword: Any) -> str:
    return f"{str(category_id or '').strip()}:{str(type_id or '').strip()}:{normalize_keyword(keyword)}"


def _safe_token(value: Any) -> str:
    text = str(value if value is not None else "unknown").strip()
    token = re.sub(r"[^0-9A-Za-z_.-]+", "_", text)[:64].strip("_")
    return token or "unknown"


def category_file(root: Path | str, category_id: Any, type_id: Any) -> Path:
    return Path(root) / f"{_safe_token(category_id)}-{_safe_token(type_id)}.jsonl"


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp"
    )
    try:
        with handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(handle.name, path)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def _write_jsonl_atomic(path: Path, records: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp"
    )
    try:
        with handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        os.replace(handle.name, path)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def read_jsonl(path: Path) -> tuple[list[dict[str, Any]], int]:
    """读 JSONL，返回 (记录列表, 坏行数)。坏行不静默丢弃计数。"""
    records: list[dict[str, Any]] = []
    broken = 0
    if not path.is_file():
        return records, broken
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                broken += 1
                continue
            if isinstance(value, dict):
                records.append(value)
            else:
                broken += 1
    return records, broken


def load_category(root: Path | str, category_id: Any, type_id: Any) -> list[dict[str, Any]]:
    records, _ = read_jsonl(category_file(root, category_id, type_id))
    return records


def load_all(root: Path | str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(Path(root).glob("*.jsonl")):
        chunk, _ = read_jsonl(path)
        records.extend(chunk)
    return records


def _new_entry(raw: Mapping[str, Any], source: str) -> dict[str, Any]:
    category_id = str(raw.get("category_id") or "").strip()
    type_id = str(raw.get("type_id") or "").strip()
    keyword = str(raw.get("keyword") or raw.get("text") or "").strip()
    entry: dict[str, Any] = {
        "key": record_key(category_id, type_id, keyword),
        "keyword": keyword,
        "normalized_keyword": normalize_keyword(keyword),
        "category_id": category_id,
        "type_id": type_id,
        "category_path_zh": raw.get("category_path_zh") or raw.get("category_path") or None,
        "source": str(raw.get("source") or source),
        "status": STATUS_CANDIDATE,
        "score": None,
        "heat_percentile": None,
        "competition_percentile": None,
        "score_notes": ["尚未打分"],
        "selected_for": [],
        "first_seen_at": now_iso(),
        "last_seen_at": now_iso(),
        "history": [],
    }
    for field in _METRIC_FIELDS:
        entry[field] = raw.get(field)
    extra = raw.get("extra")
    entry["extra"] = dict(extra) if isinstance(extra, Mapping) else {}
    return entry


def _merge_entry(existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """同名关键词再次采集：保留身份字段与人工状态，刷新指标。"""
    changed: list[str] = []
    for field in _METRIC_FIELDS:
        new_value = incoming.get(field)
        if new_value is None:
            continue
        if existing.get(field) != new_value:
            changed.append(f"{field}: {existing.get(field)} -> {new_value}")
            existing[field] = new_value
    if incoming.get("category_path_zh") and not existing.get("category_path_zh"):
        existing["category_path_zh"] = incoming["category_path_zh"]
    if incoming.get("extra"):
        merged = dict(existing.get("extra") or {})
        merged.update(incoming["extra"])
        existing["extra"] = merged
    existing["last_seen_at"] = now_iso()
    if changed:
        existing.setdefault("history", []).append(
            {"at": now_iso(), "event": "metrics_updated", "detail": changed}
        )
    return existing


def upsert(
    root: Path | str,
    records: Iterable[Mapping[str, Any]],
    *,
    source: str = "seerfar",
    config: ScoreConfig | None = None,
) -> dict[str, Any]:
    """写入/合并一批关键词并立即按类目重算分数。

    返回 ``{created, updated, skipped, categories, files}``。
    """
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    skipped = 0
    for raw in records:
        keyword = normalize_keyword(raw.get("keyword") or raw.get("text"))
        if not keyword:
            skipped += 1
            continue
        entry = _new_entry(raw, source)
        grouped.setdefault((entry["category_id"], entry["type_id"]), []).append(entry)

    created = 0
    updated = 0
    files: list[str] = []
    promoted = 0
    demoted = 0

    for (category_id, type_id), incoming_entries in grouped.items():
        path = category_file(root, category_id, type_id)
        existing_records, _ = read_jsonl(path)
        by_key = {str(item.get("key")): item for item in existing_records}
        for incoming in incoming_entries:
            key = incoming["key"]
            current = by_key.get(key)
            if current is None:
                by_key[key] = incoming
                created += 1
            else:
                by_key[key] = _merge_entry(current, incoming)
                updated += 1

        merged = list(by_key.values())
        results = score_records(merged, config)
        for record in merged:
            result = results.get(str(record.get("key")))
            if result is None:
                continue
            before = record.get("status")
            record["score"] = result.score
            record["heat_percentile"] = result.heat_percentile
            record["competition_percentile"] = result.competition_percentile
            record["score_notes"] = result.reasons
            if result.score is None:
                # 人工决策（in_library / used）不因指标缺失被自动回退，只回落自动提升的 qualified
                if before == STATUS_QUALIFIED:
                    record["status"] = STATUS_CANDIDATE
                    demoted += 1
                elif before in {STATUS_IN_LIBRARY, STATUS_USED}:
                    record.setdefault("score_notes", []).append(
                        f"指标缺失，已保留人工状态 {before}"
                    )
            else:
                if result.qualified and before == STATUS_CANDIDATE:
                    record["status"] = STATUS_QUALIFIED
                    promoted += 1
                elif not result.qualified and before == STATUS_QUALIFIED:
                    record["status"] = STATUS_CANDIDATE
                    demoted += 1

        _write_jsonl_atomic(path, merged)
        files.append(str(path))
        index(root)

    return {
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "promoted": promoted,
        "demoted": demoted,
        "categories": len(grouped),
        "files": files,
    }


def rescore(
    root: Path | str,
    config: ScoreConfig | None = None,
    *,
    category_id: str | None = None,
    type_id: str | None = None,
) -> dict[str, Any]:
    """重算分数（默认全部类目）。``candidate -> qualified`` 自动提升，反之回落。"""
    category_root = Path(root)
    if category_id is not None and type_id is not None:
        targets = [category_file(category_root, category_id, type_id)]
    else:
        targets = sorted(category_root.glob("*.jsonl"))

    scored = 0
    promoted = 0
    demoted = 0
    for path in targets:
        records, _ = read_jsonl(path)
        if not records:
            continue
        results = score_records(records, config)
        for record in records:
            result = results.get(str(record.get("key")))
            if result is None:
                continue
            before = record.get("status")
            record["score"] = result.score
            record["heat_percentile"] = result.heat_percentile
            record["competition_percentile"] = result.competition_percentile
            record["score_notes"] = result.reasons
            scored += 1
            if result.score is None:
                if before == STATUS_QUALIFIED:
                    record["status"] = STATUS_CANDIDATE
                    demoted += 1
            elif result.qualified and before == STATUS_CANDIDATE:
                record["status"] = STATUS_QUALIFIED
                promoted += 1
            elif not result.qualified and before == STATUS_QUALIFIED:
                record["status"] = STATUS_CANDIDATE
                demoted += 1
        _write_jsonl_atomic(path, records)
    index(category_root)
    return {"scored": scored, "promoted": promoted, "demoted": demoted, "files": len(targets)}


def set_status(
    root: Path | str,
    keys: Sequence[str],
    status: str,
    *,
    reason: str | None = None,
    product_id: str | None = None,
) -> dict[str, Any]:
    """批量改状态（用于"入库 / 排除 / 已用于某商品"）。"""
    if status not in VALID_STATUSES:
        raise ValueError(f"未知状态：{status}；可选：{', '.join(VALID_STATUSES)}")
    wanted = {str(key) for key in keys}
    changed = 0
    for path in sorted(Path(root).glob("*.jsonl")):
        records, _ = read_jsonl(path)
        touched = False
        for record in records:
            if str(record.get("key")) not in wanted:
                continue
            if record.get("status") == status and product_id is None:
                continue
            record["status"] = status
            record.setdefault("history", []).append(
                {"at": now_iso(), "event": "status_changed", "status": status, "reason": reason}
            )
            if product_id:
                used = list(record.get("selected_for") or [])
                if product_id not in used:
                    used.append(product_id)
                record["selected_for"] = used
            touched = True
            changed += 1
        if touched:
            _write_jsonl_atomic(path, records)
    index(root)
    return {"changed": changed, "status": status}


def _iter_records(root: Path | str, category_id: str | None, type_id: str | None):
    category_root = Path(root)
    if category_id is not None and type_id is not None:
        paths = [category_file(category_root, category_id, type_id)]
    else:
        paths = sorted(category_root.glob("*.jsonl"))
    for path in paths:
        records, _ = read_jsonl(path)
        for record in records:
            if category_id is not None and str(record.get("category_id")) != str(category_id):
                continue
            if type_id is not None and str(record.get("type_id")) != str(type_id):
                continue
            yield record


def query(
    root: Path | str,
    *,
    category_id: str | None = None,
    type_id: str | None = None,
    status: str | None = None,
    min_score: float | None = None,
    text: str | None = None,
    only_qualified: bool = False,
    limit: int | None = None,
    order: str = "score",
) -> list[dict[str, Any]]:
    """查询关键词。``order`` 支持 ``score`` / ``heat`` / ``competition`` / ``keyword``。"""
    needle = normalize_keyword(text) if text else None
    rows: list[dict[str, Any]] = []
    for record in _iter_records(root, category_id, type_id):
        if status is not None and record.get("status") != status:
            continue
        if only_qualified and record.get("status") not in {STATUS_QUALIFIED, STATUS_IN_LIBRARY}:
            continue
        score = record.get("score")
        if min_score is not None and (score is None or float(score) < float(min_score)):
            continue
        if needle and needle not in str(record.get("normalized_keyword") or ""):
            continue
        rows.append(record)

    def sort_key(record: Mapping[str, Any]):
        if order == "keyword":
            return str(record.get("normalized_keyword") or "")
        if order == "heat":
            return -(record.get("heat_percentile") or 0.0)
        if order == "competition":
            return record.get("competition_percentile") if record.get("competition_percentile") is not None else 9.9
        return -(record.get("score") if record.get("score") is not None else -9.9)

    rows.sort(key=sort_key)
    if limit is not None:
        rows = rows[: int(limit)]
    return rows


def index(root: Path | str) -> dict[str, Any]:
    """重建 ``keyword-library/index.json``：按类目的统计与时间戳。"""
    category_root = Path(root)
    category_root.mkdir(parents=True, exist_ok=True)
    categories: dict[str, Any] = {}
    for path in sorted(category_root.glob("*.jsonl")):
        records, broken = read_jsonl(path)
        if not records:
            continue
        first = records[0]
        key = f"{first.get('category_id')}:{first.get('type_id')}"
        categories[key] = {
            "category_id": first.get("category_id"),
            "type_id": first.get("type_id"),
            "category_path_zh": first.get("category_path_zh"),
            "total": len(records),
            "qualified": sum(
                1 for item in records if item.get("status") in {STATUS_QUALIFIED, STATUS_IN_LIBRARY}
            ),
            "in_library": sum(1 for item in records if item.get("status") == STATUS_IN_LIBRARY),
            "used": sum(1 for item in records if item.get("status") == STATUS_USED),
            "missing_metrics": sum(1 for item in records if item.get("score") is None),
            "broken_lines": broken,
            "file": path.name,
            "updated_at": now_iso(),
        }
    payload = {
        "schema_version": "1.0.0",
        "updated_at": now_iso(),
        "category_count": len(categories),
        "total": sum(item["total"] for item in categories.values()),
        "categories": categories,
    }
    _write_json_atomic(category_root / INDEX_NAME, payload)
    return payload


def stats(root: Path | str) -> dict[str, Any]:
    payload = index(root)
    return {
        "category_count": payload["category_count"],
        "total": payload["total"],
        "categories": payload["categories"],
    }
