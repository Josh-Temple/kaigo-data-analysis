#!/usr/bin/env python3
"""Discover and qualify insurer-level files from MHLW LTC monthly report pages."""

from __future__ import annotations

import argparse
import hashlib
from html.parser import HTMLParser
import io
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET
import zipfile
from urllib.parse import urljoin
from urllib.request import Request, urlopen


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[dict[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        self._href = dict(attrs).get("href")
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href is not None:
            text = normalize_text("".join(self._text))
            self.links.append({"text": text, "href": self._href})
            self._href = None
            self._text = []


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\u3000", " ")).strip()


def fetch(url: str) -> bytes:
    request = Request(
        url,
        headers={
            "User-Agent": "kaigo-data-analysis/0.1 (+https://github.com/Josh-Temple/kaigo-data-analysis)",
            "Accept": "*/*",
        },
    )
    with urlopen(request, timeout=120) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status} for {url}")
        return response.read()


def decode_html(raw: bytes) -> tuple[str, str]:
    for encoding in ("utf-8", "cp932", "shift_jis"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError("unknown", b"", 0, 1, "unsupported HTML encoding")


def _xlsx_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    path = "xl/sharedStrings.xml"
    if path not in zf.namelist():
        return []
    root = ET.fromstring(zf.read(path))
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    values = []
    for item in root.findall("m:si", ns):
        values.append("".join(node.text or "" for node in item.findall(".//m:t", ns)))
    return values


def _xlsx_cell_value(cell: ET.Element, shared: list[str]) -> str | None:
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.findall(".//m:t", ns))
    value = cell.find("m:v", ns)
    if value is None or value.text is None:
        return None
    if cell_type == "s":
        try:
            return shared[int(value.text)]
        except (ValueError, IndexError):
            return value.text
    return value.text


def inspect_xlsx(raw: bytes, max_rows: int = 20) -> dict:
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        main_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        rel_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
        pkg_rel_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
        workbook = ET.fromstring(zf.read("xl/workbook.xml"))
        rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        rel_map = {
            rel.attrib["Id"]: rel.attrib["Target"]
            for rel in rels.findall(f"{{{pkg_rel_ns}}}Relationship")
        }
        shared = _xlsx_shared_strings(zf)
        sheet_summaries = []
        for sheet in workbook.findall(f".//{{{main_ns}}}sheet"):
            rel_id = sheet.attrib[f"{{{rel_ns}}}id"]
            target = rel_map[rel_id].lstrip("/")
            sheet_path = target if target.startswith("xl/") else f"xl/{target}"
            sheet_root = ET.fromstring(zf.read(sheet_path))
            dimension = sheet_root.find(f"{{{main_ns}}}dimension")
            sample_rows = []
            for row in sheet_root.findall(f".//{{{main_ns}}}sheetData/{{{main_ns}}}row"):
                cells = []
                for cell in row.findall(f"{{{main_ns}}}c"):
                    value = _xlsx_cell_value(cell, shared)
                    if value not in (None, ""):
                        cells.append({
                            "cell": cell.attrib.get("r"),
                            "value": value,
                        })
                if cells:
                    sample_rows.append({
                        "row": int(row.attrib.get("r", "0")),
                        "cells": cells,
                    })
                if len(sample_rows) >= max_rows:
                    break
            sheet_summaries.append({
                "name": sheet.attrib["name"],
                "dimension": dimension.attrib.get("ref") if dimension is not None else None,
                "sample_rows": sample_rows,
            })
        return {
            "sheet_count": len(sheet_summaries),
            "sheets": sheet_summaries,
        }


def _column_from_cell_ref(ref: str) -> str:
    match = re.match(r"([A-Z]+)", ref)
    return match.group(1) if match else ""


def first_sheet_rows(raw: bytes) -> list[dict[str, str]]:
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        main_ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        rel_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
        pkg_rel_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
        workbook = ET.fromstring(zf.read("xl/workbook.xml"))
        rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        rel_map = {
            rel.attrib["Id"]: rel.attrib["Target"]
            for rel in rels.findall(f"{{{pkg_rel_ns}}}Relationship")
        }
        first_sheet = workbook.find(f".//{{{main_ns}}}sheet")
        if first_sheet is None:
            return []
        rel_id = first_sheet.attrib[f"{{{rel_ns}}}id"]
        target = rel_map[rel_id].lstrip("/")
        sheet_path = target if target.startswith("xl/") else f"xl/{target}"
        root = ET.fromstring(zf.read(sheet_path))
        shared = _xlsx_shared_strings(zf)
        rows = []
        for row in root.findall(f".//{{{main_ns}}}sheetData/{{{main_ns}}}row"):
            values = {"_row": row.attrib.get("r", "")}
            for cell in row.findall(f"{{{main_ns}}}c"):
                ref = cell.attrib.get("r", "")
                col = _column_from_cell_ref(ref)
                value = _xlsx_cell_value(cell, shared)
                if col and value is not None:
                    values[col] = value
            rows.append(values)
        return rows


def as_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value.replace(",", "")))
    except (TypeError, ValueError):
        return None


def summarize_category1_rows(raw: bytes) -> tuple[dict, set[tuple[str, str]]]:
    rows = first_sheet_rows(raw)
    national = next((row for row in rows if row.get("A") == "全国計"), None)
    if national is None:
        raise ValueError("category-1 workbook has no 全国計 row")

    records = []
    for row in rows:
        if not row.get("A") or not row.get("B"):
            continue
        total = as_int(row.get("F"))
        age_65_74 = as_int(row.get("G"))
        age_75_84 = as_int(row.get("H"))
        age_85_plus = as_int(row.get("I"))
        if total is None:
            continue
        records.append({
            "key": (row["A"], row["B"]),
            "total": total,
            "age_65_74": age_65_74,
            "age_75_84": age_75_84,
            "age_85_plus": age_85_plus,
        })

    keys = [record["key"] for record in records]
    duplicate_keys = len(keys) - len(set(keys))
    required_null_rows = sum(
        1 for record in records
        if None in (
            record["total"],
            record["age_65_74"],
            record["age_75_84"],
            record["age_85_plus"],
        )
    )
    age_band_mismatch_rows = sum(
        1 for record in records
        if None not in (
            record["age_65_74"],
            record["age_75_84"],
            record["age_85_plus"],
        )
        and record["age_65_74"] + record["age_75_84"] + record["age_85_plus"]
        != record["total"]
    )
    total_sum = sum(record["total"] for record in records)
    age75plus_sum = sum(
        (record["age_75_84"] or 0) + (record["age_85_plus"] or 0)
        for record in records
    )
    national_total = as_int(national.get("F"))
    national_age75plus = (as_int(national.get("H")) or 0) + (as_int(national.get("I")) or 0)

    return ({
        "insurer_row_count": len(records),
        "unique_prefecture_insurer_keys": len(set(keys)),
        "duplicate_key_count": duplicate_keys,
        "required_numeric_null_row_count": required_null_rows,
        "age_band_sum_mismatch_row_count": age_band_mismatch_rows,
        "sum_category1_insured": total_sum,
        "national_row_category1_insured": national_total,
        "sum_matches_national_row": total_sum == national_total,
        "sum_age75plus": age75plus_sum,
        "national_row_age75plus": national_age75plus,
        "age75plus_sum_matches_national_row": age75plus_sum == national_age75plus,
        "column_contract": {
            "A": "prefecture",
            "B": "insurer_name",
            "F": "category1_insured_total",
            "G": "age_65_74",
            "H": "age_75_84",
            "I": "age_85_plus",
        },
    }, set(keys))


def summarize_certified_rows(raw: bytes) -> tuple[dict, set[tuple[str, str]]]:
    rows = first_sheet_rows(raw)
    national = next((row for row in rows if row.get("A") == "全国計"), None)
    if national is None:
        raise ValueError("certification workbook has no 全国計 row")

    records = []
    for row in rows:
        if not row.get("A") or not row.get("B"):
            continue
        total_certified = as_int(row.get("J"))
        category1_certified = as_int(row.get("R"))
        if total_certified is None or category1_certified is None:
            continue
        records.append({
            "key": (row["A"], row["B"]),
            "total_certified": total_certified,
            "category1_certified": category1_certified,
        })

    keys = [record["key"] for record in records]
    duplicate_keys = len(keys) - len(set(keys))
    first_insured_over_total_rows = sum(
        1 for record in records
        if record["category1_certified"] > record["total_certified"]
    )
    total_sum = sum(record["total_certified"] for record in records)
    category1_sum = sum(record["category1_certified"] for record in records)
    national_total = as_int(national.get("J"))
    national_category1 = as_int(national.get("R"))

    return ({
        "insurer_row_count": len(records),
        "unique_prefecture_insurer_keys": len(set(keys)),
        "duplicate_key_count": duplicate_keys,
        "category1_certified_over_total_row_count": first_insured_over_total_rows,
        "sum_total_certified": total_sum,
        "national_row_total_certified": national_total,
        "total_sum_matches_national_row": total_sum == national_total,
        "sum_category1_certified": category1_sum,
        "national_row_category1_certified": national_category1,
        "category1_sum_matches_national_row": category1_sum == national_category1,
        "column_contract": {
            "A": "prefecture",
            "B": "insurer_name",
            "J": "all_certified_total",
            "R": "category1_certified_total",
        },
    }, set(keys))


def classify_file(raw: bytes) -> str:
    if raw.startswith(b"PK\x03\x04"):
        return "zip_container_likely_xlsx"
    if raw.startswith(bytes.fromhex("D0CF11E0A1B11AE1")):
        return "ole_compound_likely_xls"
    if raw.startswith(b"%PDF"):
        return "pdf"
    return "unknown"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    config = json.loads(args.config.read_text(encoding="utf-8"))
    page_raw = fetch(config["landing_page"])
    page_text, page_encoding = decode_html(page_raw)

    html = LinkParser()
    html.feed(page_text)

    qualified = []
    extraction_keys: dict[str, set[tuple[str, str]]] = {}
    extraction_summaries: dict[str, dict] = {}
    for target in config["target_tables"]:
        needle = normalize_text(target["anchor_contains"])
        matches = [
            link for link in html.links
            if needle in normalize_text(link["text"])
        ]
        if not matches:
            diagnostic_links = [
                {
                    "text": link["text"],
                    "href": link["href"],
                }
                for link in html.links
                if ".xls" in link["href"].lower()
            ]
            print(json.dumps({
                "diagnostic": "target anchor not found",
                "target_id": target["id"],
                "target_anchor_contains": target["anchor_contains"],
                "excel_links": diagnostic_links,
            }, ensure_ascii=False, indent=2))
            raise ValueError(
                f"target anchor not found: {target['id']} / {target['anchor_contains']}"
            )

        selected = matches[-1] if target.get("expected_occurrence") == "last" else matches[0]
        file_url = urljoin(config["landing_page"], selected["href"])
        raw = fetch(file_url)
        if target["id"] == "insurer_category1_population":
            extraction_summary, keys = summarize_category1_rows(raw)
            extraction_summaries[target["id"]] = extraction_summary
            extraction_keys[target["id"]] = keys
        elif target["id"] == "insurer_certified_total":
            extraction_summary, keys = summarize_certified_rows(raw)
            extraction_summaries[target["id"]] = extraction_summary
            extraction_keys[target["id"]] = keys
        else:
            extraction_summary = None

        qualified.append({
            "id": target["id"],
            "anchor_text": selected["text"],
            "match_count_on_page": len(matches),
            "selected_match_rule": target.get("expected_occurrence", "first"),
            "url": file_url,
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "file_signature": classify_file(raw),
            "extension": Path(file_url.split("?", 1)[0]).suffix.lower(),
            "xlsx_structure": inspect_xlsx(raw) if classify_file(raw) == "zip_container_likely_xlsx" else None,
            "extraction_summary": extraction_summary,
        })

    category1_keys = extraction_keys.get("insurer_category1_population", set())
    certified_keys = extraction_keys.get("insurer_certified_total", set())
    category1_summary = extraction_summaries.get("insurer_category1_population", {})
    certified_summary = extraction_summaries.get("insurer_certified_total", {})
    category1_total = category1_summary.get("sum_category1_insured")
    category1_certified = certified_summary.get("sum_category1_certified")
    certification_rate = (
        category1_certified / category1_total
        if category1_total and category1_certified is not None
        else None
    )
    cross_table = {
        "matched_prefecture_insurer_keys": len(category1_keys & certified_keys),
        "category1_only_key_count": len(category1_keys - certified_keys),
        "certified_only_key_count": len(certified_keys - category1_keys),
        "key_sets_match": category1_keys == certified_keys,
        "national_category1_certification_rate": certification_rate,
        "national_category1_certification_rate_percent": (
            certification_rate * 100 if certification_rate is not None else None
        ),
        "join_boundary": "prefecture + insurer_name is sufficient only for cross-table validation inside the same monthly report; municipality joins remain blocked until an official insurer-code mapping is acquired",
    }

    result = {
        "schema_version": "0.1",
        "source_id": config["source_id"],
        "period": config["period"],
        "landing_page": config["landing_page"],
        "landing_page_sha256": hashlib.sha256(page_raw).hexdigest(),
        "landing_page_encoding": page_encoding,
        "link_count": len(html.links),
        "tables": qualified,
        "cross_table_validation": cross_table,
        "qualification_scope": [
            "official landing page reachable",
            "target insurer-level links discoverable without guessed file URLs",
            "target files reachable",
            "target file hashes and container signatures recorded"
        ],
        "not_yet_qualified": [
            "official insurer code field or mapping",
            "municipality-code join",
            "publication-ready insurer-level dataset"
        ]
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
