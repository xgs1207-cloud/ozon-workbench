"""Lossless Ozon-template export, sharing the validated Seller API draft.

Artifact spreadsheet import/export is not a byte-preserving OOXML editor: Ozon
stores schema/dictionaries in hidden sheets and relies on native validation.
Accordingly this application adapter changes only sheetData rows, copies every
other ZIP part byte-for-byte, and verifies that preservation after export. No
openpyxl authoring, network requests, or seller writes are performed here.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
from typing import Any, Mapping
import xml.etree.ElementTree as ET
import zipfile

from .ozon_write import OzonWriteError, build_import_request, media_for_variant

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
MAX_PART_BYTES = 16 * 1024**2


class ListingExportError(ValueError):
    pass


def _xml(archive: zipfile.ZipFile, name: str) -> ET.Element:
    data = archive.read(name)
    if len(data) > MAX_PART_BYTES or b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise ListingExportError("模板XML过大或包含不支持的实体声明")
    return ET.fromstring(data)


def _cell_value(cell: ET.Element, strings: list[str]) -> Any:
    if cell.get("t") == "inlineStr":
        return "".join(node.text or "" for node in cell.findall(".//m:t", NS))
    node = cell.find("m:v", NS)
    value = node.text if node is not None and node.text is not None else ""
    if cell.get("t") == "s":
        return strings[int(value)]
    return value


def _row_values(root: ET.Element, row: int, strings: list[str]) -> dict[str, Any]:
    element = root.find(f"m:sheetData/m:row[@r='{row}']", NS)
    return {re.sub(r"\d+$", "", cell.get("r", "")): _cell_value(cell, strings)
            for cell in element or []}


def _normalize_label(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).rstrip("*＊")


def _load_template(path: Path | str) -> dict[str, Any]:
    path = Path(path)
    if path.stat().st_size > 64 * 1024**2:
        raise ListingExportError("模板文件过大")
    raw_bytes = path.read_bytes()
    with zipfile.ZipFile(io.BytesIO(raw_bytes)) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or len(names) > 250 or sum(x.file_size for x in archive.infolist()) > 64 * 1024**2:
            raise ListingExportError("模板ZIP结构或大小不安全")
        if any(any(marker in name.lower() for marker in ("vbaproject", "/activex/", "/embeddings/", "/externallinks/", "customui/")) for name in names):
            raise ListingExportError("仅接受官方空白XLSX；模板不能包含宏、嵌入对象或外链工作簿")
        for name in names:
            if name.endswith(".rels") and any(str(node.get("TargetMode") or "").lower() == "external" for node in _xml(archive, name)):
                raise ListingExportError("模板包含外部关系，无法作为受控官方空白模板")
        strings = ["".join(node.text or "" for node in item.findall(".//m:t", NS))
                   for item in _xml(archive, "xl/sharedStrings.xml")] if "xl/sharedStrings.xml" in names else []
        rels = {node.get("Id"): node.get("Target") for node in _xml(archive, "xl/_rels/workbook.xml.rels")}
        sheets: dict[str, str] = {}
        workbook = _xml(archive, "xl/workbook.xml")
        for node in workbook.find("m:sheets", NS) or []:
            target = str(rels.get(node.get(f"{{{REL_NS}}}id")) or "")
            part = target.lstrip("/") if target.startswith("/") else str(PurePosixPath("xl") / target)
            if ".." in PurePosixPath(part).parts or part not in names:
                raise ListingExportError("模板工作表路径无效")
            name = str(node.get("name"))
            if name in sheets or part in sheets.values():
                raise ListingExportError("模板存在重复工作表名称或路径")
            sheets[name] = part
        if "configs" not in sheets:
            raise ListingExportError("缺少Ozon官方configs，不能从自建Excel推定官方模板")
        config_root = _xml(archive, sheets["configs"])
        configs: dict[str, str] = {}
        metadata = None
        for row in config_root.find("m:sheetData", NS) or []:
            values = [_cell_value(cell, strings) for cell in row]
            if not values:
                continue
            if values[0] == "XLS_TEMPLATE_INFO_BASE64":
                encoded = "".join(str(v) for v in values[1:])
                metadata = json.loads(base64.b64decode(encoded, validate=True))
            else:
                configs[str(values[0])] = str(values[1]) if len(values) > 1 else ""
        if not isinstance(metadata, dict) or not isinstance(metadata.get("attributes"), dict):
            raise ListingExportError("官方模板属性配置缺失或无效")
        aliases = metadata.get("additional_column_by_name") or {}
        sheet_name = str(aliases.get("list_name") or "模板")
        if sheet_name not in sheets:
            raise ListingExportError("模板主工作表缺失")
        first_row = int(configs.get("PRODUCTS_FIRST_DATA_ROW_INDEX") or 5)
        title_row = int(configs.get("PRODUCTS_TITLE_ROW_INDEX") or 2)
        if first_row <= title_row or first_row > 100:
            raise ListingExportError("模板数据起始行配置无效")
        root = _xml(archive, sheets[sheet_name])
        for part in sheets.values():
            if _xml(archive, part).find(".//m:f", NS) is not None:
                raise ListingExportError("模板包含可执行单元格公式，请重新下载官方空白模板")
        headers = _row_values(root, title_row, strings)
        if len(set(_normalize_label(v) for v in headers.values())) != len(headers):
            raise ListingExportError("模板存在重复标题，无法可靠映射")
        labels = {_normalize_label(v): col for col, v in headers.items()}
        fields = {key: labels[_normalize_label(label)] for key, label in aliases.items()
                  if _normalize_label(label) in labels}
        attributes = {int(key): val for key, val in metadata["attributes"].items()}
        attr_columns = {key: labels[_normalize_label(attr.get("Name"))] for key, attr in attributes.items()
                        if not attr.get("ComplexID") and _normalize_label(attr.get("Name")) in labels}
        operational = {fields.get(key) for key in ("offer_id", "name", "price", "review_promo", "weight", "width", "height", "depth", "picture", "pictures")}
        if len(set(attr_columns.values())) != len(attr_columns) or set(attr_columns.values()) & operational:
            raise ListingExportError("模板属性列映射有冲突，不能覆盖商品基本字段")
        hint_row = _row_values(root, first_row - 1, strings)
        picture_hint = str(hint_row.get(fields.get("pictures", "")) or "")
        limit_match = re.search(r"数量限制为\s*(\d+)", picture_hint)
        max_additional = int(limit_match[1]) if limit_match else 14
        result = {"path": path, "sha256": hashlib.sha256(raw_bytes).hexdigest(), "raw_bytes": raw_bytes,
                  "sheets": sheets, "strings": strings, "configs": configs, "metadata": metadata,
                  "sheet_name": sheet_name, "headers": headers, "fields": fields, "attributes": attributes,
                  "attr_columns": attr_columns, "first_data_row": first_row, "title_row": title_row,
                  "max_additional_images": max_additional}
        offer_column = fields.get("offer_id")
        if not offer_column:
            raise ListingExportError("模板缺少货号列")
        type_column = attr_columns.get(8229)
        type_choices = ((attributes.get(8229) or {}).get("LookupData") or {}).get("Values") or {}
        allowed_types = {str(choice.get("Value")) for choice in type_choices.values()}
        for row in root.find("m:sheetData", NS) or []:
            if int(row.get("r", "0")) >= first_row:
                for cell in row:
                    column = re.sub(r"\d+$", "", cell.get("r", ""))
                    value = _cell_value(cell, strings)
                    if value and not (column == "A" and str(value).isdigit()) and not (column == type_column and str(value) in allowed_types):
                        raise ListingExportError("模板含已有商品数据，请上传新的空白官方模板；不会覆盖原商品")
        for name in (metadata.get("complex_list") or {}).values():
            if name not in sheets:
                raise ListingExportError("复杂属性工作表缺失")
            for row in _xml(archive, sheets[name]).find("m:sheetData", NS) or []:
                if int(row.get("r", "0")) >= first_row and any(_cell_value(cell, strings) for cell in row):
                    raise ListingExportError("模板媒体/PDF工作表含已有数据，请上传空白模板")
        return result


def _template_summary(data: dict[str, Any]) -> dict[str, Any]:
    return {"sha256": data["sha256"], "category_id": int(data["configs"].get("DESCRIPTION_CATEGORY_ID") or 0),
            "currency_code": data["configs"].get("CURRENCY"), "name": data["metadata"].get("name"),
            "first_data_row": data["first_data_row"], "sheets": list(data["sheets"]),
            "column_count": len(data["headers"]), "columns": data["headers"],
            "max_additional_images": data["max_additional_images"]}


def inspect_template(template_path: Path | str) -> dict[str, Any]:
    return _template_summary(_load_template(template_path))


def _column(number: int) -> str:
    value = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        value = chr(65 + remainder) + value
    return value


def _complex_row(template: dict[str, Any], complex_id: int, offer: str, values: Mapping[int, Any]) -> tuple[str, dict[str, Any]]:
    metadata = template["metadata"]
    sheet = (metadata.get("complex_list") or {}).get(str(complex_id))
    mapping = (metadata.get("complex_attribute_by_parent_id") or {}).get(str(complex_id)) or {}
    if sheet not in template["sheets"]:
        raise ListingExportError(f"模板没有复杂属性{complex_id}工作表")
    row: dict[str, Any] = {"A": offer}
    for identifier, value in values.items():
        index = mapping.get(str(identifier))
        definition = template["attributes"].get(identifier)
        if not isinstance(index, int) or isinstance(index, bool) or index < 1 or not definition or int(definition.get("ComplexID") or 0) != complex_id:
            raise ListingExportError(f"复杂属性{identifier}没有可靠模板映射，不能静默丢弃")
        column = _column(index + 1)
        if column in row:
            raise ListingExportError("复杂属性列映射有冲突，不能静默覆盖")
        row[column] = value
    return sheet, row


def _attribute_value(attribute: Mapping[str, Any], definition: Mapping[str, Any]) -> Any:
    values = attribute.get("values") or []
    choices = (definition.get("LookupData") or {}).get("Values") or {}
    maximum = int(definition.get("MaxValueCount") or 0)
    if len(values) > 1 and not definition.get("IsCollection") or maximum and len(values) > maximum:
        raise ListingExportError(f"属性{definition.get('ID')}值数量超出模板限制")
    converted = []
    kind = str(definition.get("Type") or "String").lower()
    for entry in values:
        value = entry.get("value")
        dictionary_id = str(entry.get("dictionary_value_id") or "")
        if choices:
            if dictionary_id:
                choice = choices.get(dictionary_id)
                if not choice:
                    raise ListingExportError(f"属性{definition.get('ID')}字典值不在当前模板中，请重新下载匹配模板")
                value = choice.get("Value")
            elif value not in [choice.get("Value") for choice in choices.values()]:
                raise ListingExportError(f"属性{definition.get('ID')}值不匹配模板下拉选项")
        if value is None or value == "":
            raise ListingExportError(f"属性{definition.get('ID')}缺少可导出值")
        if kind in ("integer", "decimal"):
            if isinstance(value, bool):
                raise ListingExportError("数字属性不能使用布尔值")
            try:
                number = float(value)
            except (TypeError, ValueError) as error:
                raise ListingExportError(f"属性{definition.get('ID')}必须是数字") from error
            if not math.isfinite(number) or kind == "integer" and not number.is_integer():
                raise ListingExportError(f"属性{definition.get('ID')}数字格式无效")
            value = int(number) if kind == "integer" else number
        elif kind == "boolean":
            if str(value).lower() not in ("true", "false"):
                raise ListingExportError(f"属性{definition.get('ID')}必须是true/false")
            value = "是" if str(value).lower() == "true" else "否"
        converted.append(value)
    if len(converted) == 1:
        return converted[0]
    return ";".join('"' + str(v).replace('"', '""') + '"' if ";" in str(v) else str(v) for v in converted)


def _plan_rows(payload: Mapping[str, Any], template: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    body = build_import_request(payload)
    category = payload.get("category") or {}
    if int(category.get("category_id") or 0) != int(template["configs"].get("DESCRIPTION_CATEGORY_ID") or 0):
        raise ListingExportError("模板类目ID与商品真实类目ID不一致，禁止替换隐藏配置或套用同名类目")
    if payload.get("production_blockers"):
        raise ListingExportError("商品存在上架阻断项，不能导出可上传模板")
    if payload.get("complex_attributes"):
        raise ListingExportError("未支持的complex_attributes不能静默丢弃，请使用规范videos/video_cover字段")
    if len(body["items"]) > 500:
        raise ListingExportError("单次模板导出最多500个规格")
    aliases = template["metadata"].get("additional_column_by_name") or {}
    fields, definitions, attr_columns = template["fields"], template["attributes"], template["attr_columns"]
    result: dict[str, list[dict[str, Any]]] = {template["sheet_name"]: []}
    seen = set()
    variants = payload["variants"]
    for index, (item, variant) in enumerate(zip(body["items"], variants), 1):
        offer = item["offer_id"]
        if not offer or len(offer) > 50 or offer in seen:
            raise ListingExportError("货号必须非空、唯一且不超过50字符")
        seen.add(offer)
        if str(item["currency_code"]).upper() != str(template["configs"].get("CURRENCY") or "").upper():
            raise ListingExportError("模板币种与商品售价币种不一致，禁止隐式换汇")
        if not math.isfinite(float(item["price"])) or float(item["price"]) <= 0:
            raise ListingExportError(f"货号{offer}缺少正数售价")
        primary_image = item.get("primary_image") or (item["images"][0] if item["images"] else "")
        additional_images = item["images"] if item.get("primary_image") else item["images"][1:]
        if len(additional_images) > template["max_additional_images"]:
            raise ListingExportError("附图超过当前模板上限，不能静默丢弃")
        row: dict[str, Any] = {"A": index}
        mapping = {"offer_id": offer, "name": item["name"], "price": float(item["price"]),
                   "picture": primary_image, "pictures": " ".join(additional_images),
                   "weight": item.get("weight"), "width": item.get("width"),
                   "height": item.get("height"), "depth": item.get("depth"),
                   "review_promo": aliases.get("promotion_no") or "否"}
        for field, value in mapping.items():
            if value is not None and value != "":
                if field not in fields:
                    raise ListingExportError(f"模板不支持字段{field}，不能静默丢弃")
                row[fields[field]] = value
        attributes = {int(attr["id"]): attr for attr in item["attributes"]}
        # Type is a mandatory template attribute even when the API uses type_id.
        if 8229 not in attributes:
            attributes[8229] = {"id": 8229, "values": [{"dictionary_value_id": item["type_id"]}]}
        for identifier, attribute in attributes.items():
            if identifier not in attr_columns or identifier not in definitions:
                raise ListingExportError(f"属性{identifier}无法映射当前模板，不能静默丢弃")
            row[attr_columns[identifier]] = _attribute_value(attribute, definitions[identifier])
        for identifier, definition in definitions.items():
            if definition.get("IsRequired") and not definition.get("ComplexID") and identifier not in attributes:
                raise ListingExportError(f"缺少模板必填属性{identifier}")
        for column, label in template["headers"].items():
            if str(label).endswith("*") and row.get(column) in (None, ""):
                raise ListingExportError(f"货号{offer}缺少必填字段{label}")
        result[template["sheet_name"]].append(row)
        videos, cover = media_for_variant(payload, variant)
        for video in videos:
            if video.get("size_bytes") is not None and video["size_bytes"] > 2 * 1024**3:
                raise ListingExportError("视频超过当前模板2GB上限")
            sheet, media_row = _complex_row(template, 100001, offer, {21837: video["title"], 21841: video["url"]})
            result.setdefault(sheet, []).append(media_row)
        if cover:
            sheet, media_row = _complex_row(template, 100002, offer, {21845: cover["url"]})
            result.setdefault(sheet, []).append(media_row)
    return result


def validate_export(payload: Mapping[str, Any], template_path: Path | str) -> dict[str, Any]:
    try:
        template = _load_template(template_path)
        plan = _plan_rows(payload, template)
        return {"ok": True, "blockers": [], "warnings": ["导出不等于导入或审核通过；模板须为本次下载的官方空白模板"],
                "template": _template_summary(template), "rows": {name: len(rows) for name, rows in plan.items()},
                "api_writes_performed": False}
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile, ET.ParseError, OzonWriteError) as error:
        return {"ok": False, "blockers": [str(error)], "warnings": [], "api_writes_performed": False}


def _patch_sheet(data: bytes, rows: list[dict[str, Any]], first_row: int) -> bytes:
    text = data.decode("utf-8")
    match = re.search(r"(<sheetData\b[^>]*>)(.*?)(</sheetData>)", text, re.S)
    if not match:
        raise ListingExportError("不支持的模板sheetData结构")
    row_pattern = r'<row\b(?=[^>]*\br="(\d+)")[^>]*?(?:/>|>.*?</row>)'
    old_rows = list(re.finditer(row_pattern, match[2], re.S))
    original = {int(match[1]): ET.fromstring(match[0]) for match in old_rows}
    prototype = next((m[0] for m in old_rows if int(m[1]) == first_row), None)
    prototype_root = ET.fromstring(prototype) if prototype else None
    prototype_cells = {re.sub(r"\d+$", "", c.get("r", "")): dict(c.attrib) for c in prototype_root or []}
    replacements = {}
    for offset, values in enumerate(rows):
        number = first_row + offset
        old = original.get(number)
        if old is None:
            raise ListingExportError("规格/视频数量超出模板预留行，请下载更大模板；不改校验或结构")
        row = ET.Element("row", {**dict(old.attrib), "r": str(number)})
        cell_attributes = {re.sub(r"\d+$", "", c.get("r", "")): dict(c.attrib) for c in old}
        columns = set(cell_attributes) | set(values)
        def order(column):
            total = 0
            for char in column: total = total * 26 + ord(char) - 64
            return total
        for column in sorted(columns, key=order):
            attributes = dict(cell_attributes.get(column) or prototype_cells.get(column) or {})
            attributes.pop("t", None)
            attributes["r"] = f"{column}{number}"
            cell = ET.SubElement(row, "c", attributes)
            value = values.get(column)
            if value is None or value == "":continue
            if isinstance(value, bool):
                cell.set("t", "b");ET.SubElement(cell, "v").text = "1" if value else "0"
            elif isinstance(value, (int, float)):
                ET.SubElement(cell, "v").text = str(value)
            else:
                # Inline strings are literal text, never formulas (including '=...').
                if any(ord(char) < 32 and char not in "\t\n\r" or 0xD800 <= ord(char) <= 0xDFFF or ord(char) in (0xFFFE, 0xFFFF) for char in str(value)):
                    raise ListingExportError("字段包含XML不允许的控制字符")
                cell.set("t", "inlineStr")
                node = ET.SubElement(ET.SubElement(cell, "is"), "t")
                node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
                node.text = str(value)
        replacements[number] = ET.tostring(row, encoding="unicode")
    used = set()
    def replace_row(m):
        number = int(m[1]);used.add(number)
        return replacements.get(number, m[0])
    new_data = re.sub(row_pattern, replace_row, match[2], flags=re.S)
    # Fresh Ozon templates provide preformatted rows; extending beyond them is
    # refused rather than rewriting dimensions/validation outside the data area.
    if set(replacements) - used:
        raise ListingExportError("规格/视频数量超出模板预留行，请下载更大模板；不改校验或结构")
    return (text[:match.start(2)] + new_data + text[match.end(2):]).encode("utf-8")


def export_listing_xlsx(payload: Mapping[str, Any], template_path: Path | str, output_path: Path | str) -> dict[str, Any]:
    template_path, output_path = Path(template_path), Path(output_path)
    if template_path.resolve() == output_path.resolve() or output_path.exists():
        raise ListingExportError("禁止覆盖原模板或已有导出文件")
    template = _load_template(template_path)
    plan = _plan_rows(payload, template)
    if hashlib.sha256(template_path.read_bytes()).hexdigest() != template["sha256"]:
        raise ListingExportError("模板在校验过程中发生变化，请重试")
    changed: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(template["raw_bytes"])) as source:
        for sheet, rows in plan.items():
            part = template["sheets"][sheet]
            changed[part] = _patch_sheet(source.read(part), rows, template["first_data_row"])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(prefix="ozon-export-", suffix=".xlsx.tmp", dir=output_path.parent, delete=False)
        temporary = Path(handle.name);handle.close()
        try:
            with zipfile.ZipFile(temporary, "w") as destination:
                destination.comment = source.comment
                for info in source.infolist():
                    destination.writestr(copy.copy(info), changed.get(info.filename, source.read(info.filename)))
            preserved_parts = len(source.namelist()) - len(changed)
            with zipfile.ZipFile(temporary) as written:
                if written.namelist() != source.namelist() or written.testzip():
                    raise ListingExportError("导出ZIP校验失败")
                for name in source.namelist():
                    if name not in changed and written.read(name) != source.read(name):
                        raise ListingExportError("模板未修改部件发生变化，拒绝导出")
            os.replace(temporary, output_path)
        finally:
            temporary.unlink(missing_ok=True)
    return {"ok": True, "path": str(output_path), "template_sha256": template["sha256"],
            "rows": {name: len(rows) for name, rows in plan.items()}, "changed_parts": list(changed),
            "preserved_parts": preserved_parts, "api_writes_performed": False}
