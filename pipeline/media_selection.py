"""One ordered image-selection boundary shared by studio and publication.

Legacy plans publish their complete main/detail set. Studio plans explicitly
select an ordered subset; missing selected files remain errors, not omissions.
An empty selection is a valid draft, never proof of a publishable product.
"""
from __future__ import annotations

from typing import Any, Mapping


def selected_image_specs(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key, role in (("main_images", "variant_main"), ("detail_images", "detail")):
        for item in plan.get(key) or []:
            if isinstance(item, Mapping):
                row = dict(item)
                row["role"] = role
                rows.append(row)
    if plan.get("studio_mode") is not True:
        return rows
    chosen = plan.get("selected_slots")
    if not isinstance(chosen, list) or any(not isinstance(slot, str) or not slot.strip() for slot in chosen):
        raise ValueError("图片工作室必须保存显式有序的 selected_slots 列表")
    if len(set(chosen)) != len(chosen):
        raise ValueError("选中的图片图位不能重复")
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        slot = str(row.get("slot") or "")
        if not slot or slot in indexed:
            raise ValueError("图片工作室计划包含空白或重复图位")
        indexed[slot] = row
    unknown = [slot for slot in chosen if slot not in indexed]
    if unknown:
        raise ValueError("选中图片不在当前计划中：" + ", ".join(unknown))
    selected: list[dict[str, Any]] = []
    for slot in chosen:
        row = indexed[slot]
        generated = row.get("generated_spec")
        if isinstance(generated, Mapping):
            if generated.get("slot") not in (None, slot):
                raise ValueError("已生成图片的图位身份与草稿不一致")
            for identity in ("source_sku_id", "output_path"):
                if identity in generated and generated.get(identity) != row.get(identity):
                    raise ValueError("已生成图片的规格或输出路径与图位不一致，请重新核对图片来源")
            # Editing a draft prompt does not rewrite the provenance of an
            # already adopted file. Failed redo jobs retain that prior version.
            row = {**row, **generated, "slot": slot, "role": row["role"]}
        selected.append(row)
    return selected


def selected_plan_slots(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Alias for consumers needing full ordered slot specifications."""
    return selected_image_specs(plan)


def selected_media_version(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Version only adopted image identity/order, excluding redo-job drafts."""
    from .guided_review import slot_fingerprint
    specs = selected_image_specs(plan)
    return {"selected_slots": [row.get("slot") for row in specs],
            "specs": [{"slot": row.get("slot"), "role": row["role"],
                       "source_sku_id": row.get("source_sku_id"),
                       "slot_fingerprint": slot_fingerprint(row),
                       **({"origin": "captured", "capture_receipt": row.get("capture_receipt")}
                          if row.get("origin") == "captured" else {})} for row in specs]}
