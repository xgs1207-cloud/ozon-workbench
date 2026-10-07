"""Offline API/XLS parity and lossless official-template preservation tests."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

from pipeline.listing_export import (
    ListingExportError, NS, _load_template, export_listing_xlsx, inspect_template, validate_export,
)

OFFICIAL_TEMPLATE = Path("D:/AI作图/抗压玩具_07.10.2026.xlsx")
MAIN_HEADERS = {
    "A": "№", "B": "货号*", "C": "商品名称", "D": "非促销最高价格，CNY*", "E": "划线价，CNY",
    "F": "加速评价收集", "G": "SKU", "H": "条形码（序列号/EAN）", "I": "欧亚经济联盟的HS编码",
    "J": "毛重，克*", "K": "包装宽度，毫米*", "L": "包装高度，毫米*", "M": "包装长度，毫米*",
    "N": "主图链接*", "O": "附加图片链接", "P": "照片货号", "Q": "品牌*", "R": "型号名称（针对合并为一张商品卡片）*",
    "S": "商品颜色", "T": "颜色名称", "U": "类型*", "V": "统一计量单位中的商品数量", "W": "#主题标签",
    "X": "简介", "Y": "JSON富内容", "Z": "组合成类似的产品", "AA": "抗压玩具类型", "AB": "颜色样本",
    "AC": "材料", "AD": "智能手机控制", "AE": "签名18+", "AF": "元件数量，个", "AG": "保质期（天）",
    "AH": "保证", "AI": "原产国", "AJ": "原厂包装数量", "AK": "需要标记代码", "AL": "错误", "AM": "缺点",
}


def payload() -> dict:
    return {
        "category": {"category_id": 1001, "type_id": 92811},
        "title": "Игрушка антистресс", "description": "Подтверждённое описание.",
        "hashtags": ["#подарок", "#уютный_дом"],
        "attributes": [{"attribute_id": 85, "value": "无品牌", "dictionary_value_id": 126745801},
                       {"attribute_id": 9048, "value": "OZW-QA-P000001"}],
        "images": [{"role": "detail", "url": "https://cdn.example.com/detail.png"}],
        "sku_measurements": {"package_dimensions": {"length_mm": 120, "width_mm": 90, "height_mm": 80, "weight_g": 200}},
        "variants": [{"source_sku_id": "S1", "offer_id": "QA-P000001-S1", "price": "10.50", "currency_code": "CNY",
                      "display_name_ru": "Игрушка антистресс красная", "color_image": "https://cdn.example.com/red.png", "attributes": []},
                     {"source_sku_id": "S2", "offer_id": "QA-P000001-S2", "price": "12.50", "currency_code": "CNY",
                      "display_name_ru": "Игрушка антистресс синяя", "color_image": "https://cdn.example.com/blue.png", "attributes": []}],
        "production_blockers": [],
    }


def make_template(path: Path, *, category=1001, first_row=5, capacity=8, mutate_metadata=None) -> None:
    """Small official-like OOXML fixture, not an alternate seller template."""
    definitions = {}
    for identifier, column, kind, required in (
        (85, "Q", "String", True), (9048, "R", "String", True), (8229, "U", "String", True),
        (23171, "W", "String", False), (4191, "X", "String", False), (4975, "AC", "String", False),
        (11650, "AJ", "Integer", False), (23536, "AK", "Boolean", False),
    ):
        definitions[str(identifier)] = {"ID": identifier, "Name": MAIN_HEADERS[column].rstrip("*"), "Type": kind,
                                         "IsRequired": required, "IsCollection": False, "MaxValueCount": 1, "ComplexID": 0,
                                         "LookupData": {"Values": {}}}
    definitions["8229"]["LookupData"]["Values"] = {"92811": {"ID": 92811, "Value": "抗压玩具"}}
    definitions["4975"]["LookupData"]["Values"] = {"10": {"ID": 10, "Value": "硅胶"}}
    for identifier, complex_id, name in ((21837, 100001, "视频标题"), (21841, 100001, "视频链接"),
                                        (21845, 100002, "封面视频链接")):
        definitions[str(identifier)] = {"ID": identifier, "Name": name, "Type": "String", "ComplexID": complex_id, "IsRequired": False}
    aliases = {"offer_id": MAIN_HEADERS["B"], "name": MAIN_HEADERS["C"], "price": MAIN_HEADERS["D"],
               "review_promo": MAIN_HEADERS["F"], "weight": MAIN_HEADERS["J"], "width": MAIN_HEADERS["K"],
               "height": MAIN_HEADERS["L"], "depth": MAIN_HEADERS["M"], "picture": MAIN_HEADERS["N"],
               "pictures": MAIN_HEADERS["O"], "list_name": "模板", "promotion_no": "否", "promotion_yes": "是"}
    metadata = {"name": "抗压玩具", "attributes": definitions, "additional_column_by_name": aliases,
                "complex_list": {"100001": "Ozon.视频", "100002": "Ozon视频封面"},
                "complex_attribute_by_parent_id": {"100001": {"21837": 1, "21841": 2}, "100002": {"21845": 1}}}
    if mutate_metadata:mutate_metadata(metadata)
    encoded = base64.b64encode(json.dumps(metadata, ensure_ascii=False).encode()).decode()
    namespace = NS["m"]
    def sheet(headers=None, configs=None):
        root = ET.Element("worksheet", {"xmlns": namespace})
        ET.SubElement(root, "dimension", {"ref": "A1:AM100"})
        data = ET.SubElement(root, "sheetData")
        def row(number, values):
            r = ET.SubElement(data, "row", {"r": str(number), "ht": "31", "customHeight": "1"})
            for col, value in values.items():
                c = ET.SubElement(r, "c", {"r": f"{col}{number}", "s": "2", "t": "inlineStr"})
                ET.SubElement(ET.SubElement(c, "is"), "t").text = str(value)
        if configs:
            for number, (key, value) in enumerate(configs.items(), 1):row(number, {"A": key, "B": value})
        else:
            row(2, headers or {"A": "货号*"})
            row(4, {"O": "数量限制为14"} if headers == MAIN_HEADERS else {})
            for number in range(first_row, first_row + capacity):
                row(number, {column: "" for column in headers or {"A": ""}})
        ET.SubElement(root, "dataValidations", {"count": "1"})
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)
    names = ["模板", "configs", "info", "Ozon.视频", "Ozon视频封面", "PDF 文件", "validation", "Ozon视频_validation", "Ozon视频封面_validation", "PDF文件_validation"]
    wb = ET.Element("workbook", {"xmlns": namespace, "xmlns:r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"})
    sheets = ET.SubElement(wb, "sheets")
    relationships = ET.Element("Relationships", {"xmlns": "http://schemas.openxmlformats.org/package/2006/relationships"})
    parts = {}
    for index, name in enumerate(names, 1):
        ET.SubElement(sheets, "sheet", {"name": name, "sheetId": str(index), "r:id": f"rId{index}", "state": "hidden" if name in ("configs", "info", "validation") else "visible"})
        ET.SubElement(relationships, "Relationship", {"Id": f"rId{index}", "Target": f"worksheets/sheet{index}.xml", "Type": "http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"})
        configs = {"PRODUCTS_TITLE_ROW_INDEX": 2, "PRODUCTS_FIRST_DATA_ROW_INDEX": first_row,
                   "DESCRIPTION_CATEGORY_ID": category, "CURRENCY": "CNY", "XLS_TEMPLATE_INFO_BASE64": encoded} if name == "configs" else None
        headers = MAIN_HEADERS if index == 1 else {"A": "货号*", "B": "视频标题", "C": "视频链接"} if index == 4 else {"A": "货号*", "B": "封面视频链接"}
        parts[f"xl/worksheets/sheet{index}.xml"] = sheet(headers, configs)
    parts["xl/workbook.xml"] = ET.tostring(wb)
    parts["xl/_rels/workbook.xml.rels"] = ET.tostring(relationships)
    parts["xl/styles.xml"] = b"<styleSheet>fixture-styles-must-remain-byte-identical</styleSheet>"
    parts["docProps/custom.xml"] = b"opaque-metadata-must-remain-byte-identical"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.comment = b"official-fixture-archive-comment"
        for name, data in parts.items():archive.writestr(name, data)


def cells(path: Path, part="xl/worksheets/sheet1.xml", number=5) -> tuple[ET.Element, dict]:
    with zipfile.ZipFile(path) as archive:root = ET.fromstring(archive.read(part))
    row = root.find(f"m:sheetData/m:row[@r='{number}']", NS)
    values = {}
    for cell in row:
        text = "".join(n.text or "" for n in cell.findall(".//m:t", NS))
        value = cell.find("m:v", NS)
        values[re.sub(r"\d+$", "", cell.get("r"))] = text or (value.text if value is not None else "")
    return row, values


class ListingExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name);self.template = self.root / "official-fixture.xlsx"
        make_template(self.template)

    def test_inspect_and_two_sku_rows_preserve_every_unmodified_part(self):
        info = inspect_template(self.template)
        self.assertEqual((info["category_id"], info["currency_code"], info["column_count"], info["first_data_row"]), (1001, "CNY", 39, 5))
        self.assertEqual(len(info["sheets"]), 10)
        before = self.template.read_bytes();out = self.root / "export.xlsx"
        report = export_listing_xlsx(payload(), self.template, out)
        self.assertTrue(report["ok"]);self.assertFalse(report["api_writes_performed"])
        self.assertEqual(report["rows"], {"模板": 2})
        self.assertEqual(self.template.read_bytes(), before)
        with zipfile.ZipFile(self.template) as source, zipfile.ZipFile(out) as result:
            self.assertEqual(source.namelist(), result.namelist());self.assertEqual(source.comment, result.comment)
            for name in source.namelist():
                if name != "xl/worksheets/sheet1.xml":self.assertEqual(source.read(name), result.read(name))
            original = source.read("xl/worksheets/sheet1.xml").decode();changed = result.read("xl/worksheets/sheet1.xml").decode()
            for text in (original, changed):ET.fromstring(text)
            self.assertEqual(original.split("<sheetData>")[0], changed.split("<sheetData>")[0])
            self.assertEqual(original.split("</sheetData>")[1], changed.split("</sheetData>")[1])
        row, values = cells(out)
        self.assertEqual(row.get("ht"), "31");self.assertEqual(row.get("customHeight"), "1")
        self.assertTrue(all(c.get("s") == "2" for c in row))
        self.assertEqual(values["B"], "QA-P000001-S1");self.assertEqual(values["F"], "否")
        self.assertEqual(values["X"], payload()["description"]);self.assertEqual(values["W"], "#подарок #уютный_дом")
        self.assertEqual(values["U"], "抗压玩具");self.assertEqual(values["J"], "200")
        self.assertEqual(values["M"], "120");self.assertEqual(values["N"], "https://cdn.example.com/red.png")
        self.assertEqual(cells(out, number=6)[1]["B"], "QA-P000001-S2")
        self.assertEqual(values["G"], "")  # do not invent existing Ozon SKU.

    def test_media_written_to_metadata_mapped_tabs_without_source_sku_as_ozon_sku(self):
        draft = payload();draft["videos"] = [{"url": "https://vkvideo.ru/video-123_456", "title": "Обзор", "duration_seconds": 15, "source_sku_id": "S1"}]
        draft["video_cover"] = {"url": "https://cdn.example.com/cover.mov", "duration_seconds": 8, "source_sku_id": "S2"}
        out = self.root / "media.xlsx";report = export_listing_xlsx(draft, self.template, out)
        self.assertEqual(report["rows"], {"模板": 2, "Ozon.视频": 1, "Ozon视频封面": 1})
        self.assertEqual(cells(out, "xl/worksheets/sheet4.xml")[1]["A"], "QA-P000001-S1")
        self.assertEqual(cells(out, "xl/worksheets/sheet4.xml")[1]["C"], "https://vkvideo.ru/video-123_456")
        self.assertEqual(cells(out, "xl/worksheets/sheet5.xml")[1]["A"], "QA-P000001-S2")
        self.assertEqual(cells(out, "xl/worksheets/sheet5.xml")[1]["B"], "https://cdn.example.com/cover.mov")

    def test_category_currency_required_values_and_unknown_attributes_block(self):
        cases = []
        d = payload();d["category"]["category_id"] = 17028973;cases.append((d, "类目ID"))
        d = payload();d["variants"][0]["currency_code"] = "RUB";cases.append((d, "币种"))
        d = payload();d["attributes"] = [row for row in d["attributes"] if row["attribute_id"] != 9048];cases.append((d, "必填属性9048"))
        d = payload();d["attributes"].append({"attribute_id": 999999, "value": "unknown"});cases.append((d, "不能静默丢弃"))
        d = payload();d["attributes"].append({"attribute_id": 4975, "dictionary_value_id": 99999, "value": "硅胶"});cases.append((d, "字典值"))
        d = payload();d["attributes"].append({"attribute_id": 11650, "value": "1.5"});cases.append((d, "数字格式"))
        d = payload();d["sku_measurements"] = {};cases.append((d, "毛重"))
        d = payload();d["production_blockers"] = ["missing evidence"];cases.append((d, "阻断项"))
        d = payload();d["variants"][1]["offer_id"] = d["variants"][0]["offer_id"];cases.append((d, "唯一"))
        d = payload();d["images"] = [{"role": "detail", "url": f"https://cdn.example.com/{i}.png"} for i in range(15)];cases.append((d, "附图"))
        d = payload();d["complex_attributes"] = [{"attributes": []}];cases.append((d, "complex_attributes"))
        for draft, message in cases:
            with self.subTest(message=message):
                report = validate_export(draft, self.template)
                self.assertFalse(report["ok"]);self.assertIn(message, report["blockers"][0])
                self.assertFalse(report["api_writes_performed"])

    def test_template_video_size_and_mapping_are_stricter_than_api(self):
        draft = payload();draft["videos"] = [{"url": "https://vkvideo.ru/video-123_456", "title": "Обзор", "size_bytes": 3 * 1024**3}]
        self.assertIn("2GB", validate_export(draft, self.template)["blockers"][0])
        draft["videos"][0].pop("size_bytes")
        make_template(self.template, mutate_metadata=lambda meta: meta["complex_attribute_by_parent_id"]["100001"].pop("21841"))
        self.assertIn("没有可靠模板映射", validate_export(draft, self.template)["blockers"][0])

    def test_existing_data_original_path_and_existing_exports_not_overwritten(self):
        out = self.root / "export.xlsx";export_listing_xlsx(payload(), self.template, out)
        previous = out.read_bytes()
        with self.assertRaises(ListingExportError):export_listing_xlsx(payload(), self.template, out)
        self.assertEqual(previous, out.read_bytes())
        with self.assertRaises(ListingExportError):export_listing_xlsx(payload(), self.template, self.template)
        self.assertIn("已有商品数据", validate_export(payload(), out)["blockers"][0])

    def test_strings_are_not_excel_formulas_and_unsafe_xml_fails_atomically(self):
        draft = payload();draft["attributes"][1]["value"] = '=HYPERLINK("https://example.com", "literal")'
        out = self.root / "literal.xlsx";export_listing_xlsx(draft, self.template, out)
        row, values = cells(out)
        self.assertEqual(values["R"], draft["attributes"][1]["value"])
        self.assertEqual(row.find("m:c[@r='R5']", NS).get("t"), "inlineStr")
        self.assertIsNone(row.find("m:c[@r='R5']/m:f", NS))
        draft["attributes"][1]["value"] = "bad\x01value"
        out = self.root / "bad.xlsx"
        with self.assertRaisesRegex(ListingExportError, "控制字符"):export_listing_xlsx(draft, self.template, out)
        self.assertFalse(out.exists());self.assertEqual(list(self.root.glob("ozon-export-*")), [])

    def test_capacity_and_write_failure_leave_no_export_and_preserve_template(self):
        make_template(self.template, capacity=1)
        original = self.template.read_bytes();out = self.root / "too-many.xlsx"
        with self.assertRaisesRegex(ListingExportError, "预留行"):export_listing_xlsx(payload(), self.template, out)
        self.assertFalse(out.exists());self.assertEqual(original, self.template.read_bytes())
        make_template(self.template)
        with patch("pipeline.listing_export.os.replace", side_effect=OSError("offline simulated disk failure")):
            with self.assertRaises(OSError):export_listing_xlsx(payload(), self.template, out)
        self.assertFalse(out.exists());self.assertEqual(list(self.root.glob("ozon-export-*")), [])

    def test_no_explicit_primary_uses_first_image_only_once(self):
        draft = payload()
        for variant in draft["variants"]:variant.pop("color_image")
        draft["images"].append({"role": "detail", "url": "https://cdn.example.com/detail2.png"})
        out = self.root / "gallery.xlsx";export_listing_xlsx(draft, self.template, out)
        values = cells(out)[1]
        self.assertEqual(values["N"], "https://cdn.example.com/detail.png")
        self.assertEqual(values["O"], "https://cdn.example.com/detail2.png")

    def test_active_content_and_nonblank_offerless_data_are_rejected(self):
        with zipfile.ZipFile(self.template, "a") as archive:archive.writestr("xl/vbaProject.bin", b"not-executed")
        self.assertIn("宏", validate_export(payload(), self.template)["blockers"][0])
        make_template(self.template)
        parts = {}
        with zipfile.ZipFile(self.template) as archive:
            for name in archive.namelist():parts[name] = archive.read(name)
        original = parts["xl/worksheets/sheet1.xml"]
        parts["xl/worksheets/sheet1.xml"] = original.replace(b'<c r="C5" s="2" t="inlineStr"><is><t /></is></c>', b'<c r="C5" s="2" t="inlineStr"><is><t>existing draft</t></is></c>')
        with zipfile.ZipFile(self.template, "w") as archive:
            for name, data in parts.items():archive.writestr(name, data)
        self.assertIn("已有商品数据", validate_export(payload(), self.template)["blockers"][0])
        parts["xl/worksheets/sheet1.xml"] = original.replace(b'<c r="C5" s="2" t="inlineStr"><is><t /></is></c>', b'<c r="C5" s="2"><f>1+1</f></c>')
        with zipfile.ZipFile(self.template, "w") as archive:
            for name, data in parts.items():archive.writestr(name, data)
        self.assertIn("公式", validate_export(payload(), self.template)["blockers"][0])

    @unittest.skipUnless(OFFICIAL_TEMPLATE.exists(), "Official user template not present in this environment")
    def test_real_official_template_all_hidden_schema_and_parts_preserved(self):
        digest = hashlib.sha256(OFFICIAL_TEMPLATE.read_bytes()).hexdigest()
        info = inspect_template(OFFICIAL_TEMPLATE)
        self.assertEqual((info["category_id"], info["column_count"], len(info["sheets"])), (17032503, 39, 10))
        draft = payload();draft["category"]["category_id"] = 17032503
        draft["videos"] = [{"url": "https://vkvideo.ru/video-123_456", "title": "Обзор", "duration_seconds": 15}]
        draft["video_cover"] = {"url": "https://cdn.example.com/cover.mov", "duration_seconds": 8}
        out = self.root / "actual.xlsx";report = export_listing_xlsx(draft, OFFICIAL_TEMPLATE, out)
        self.assertEqual(set(report["changed_parts"]), {"xl/worksheets/sheet1.xml", "xl/worksheets/sheet4.xml", "xl/worksheets/sheet5.xml"})
        with zipfile.ZipFile(OFFICIAL_TEMPLATE) as original, zipfile.ZipFile(out) as result:
            for name in original.namelist():
                if name not in report["changed_parts"]:self.assertEqual(original.read(name), result.read(name), name)
        self.assertEqual(cells(out, number=6)[1]["B"], "QA-P000001-S2")
        self.assertEqual(cells(out, "xl/worksheets/sheet4.xml", number=6)[1]["A"], "QA-P000001-S2")
        self.assertEqual(hashlib.sha256(OFFICIAL_TEMPLATE.read_bytes()).hexdigest(), digest)


if __name__ == "__main__":unittest.main()
