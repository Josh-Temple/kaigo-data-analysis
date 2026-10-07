#!/usr/bin/env python3
"""Conservatively qualify insurer-to-municipality mappings.

This script qualifies two conservative geography paths:
1. prefecture + insurer name exactly matches one J-LIS local-government body;
2. an insurer membership list is supported by period-aligned official sources,
   and every member name exactly resolves to one J-LIS body.

Group-insurer demand is kept at insurer level; it is never allocated to members.
"""

from __future__ import annotations

import argparse
from collections import Counter
from html.parser import HTMLParser
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urljoin

import probe_mhlw_ltc_status as ltc


class TableAndLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[dict[str, str]] = []
        self.rows: list[list[str]] = []
        self._href: str | None = None
        self._link_text: list[str] = []
        self._in_tr = False
        self._in_cell = False
        self._cell_text: list[str] = []
        self._row: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._link_text = []
        elif tag == "tr":
            self._in_tr = True
            self._row = []
        elif tag in ("td", "th") and self._in_tr:
            self._in_cell = True
            self._cell_text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._link_text.append(data)
        if self._in_cell:
            self._cell_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "a" and self._href is not None:
            self.links.append({
                "href": self._href,
                "text": normalize_text("".join(self._link_text)),
            })
            self._href = None
            self._link_text = []
        elif tag in ("td", "th") and self._in_cell:
            self._row.append(normalize_text("".join(self._cell_text)))
            self._in_cell = False
            self._cell_text = []
        elif tag == "tr" and self._in_tr:
            if self._row:
                self.rows.append(self._row)
            self._in_tr = False
            self._row = []


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\u3000", " ")).strip()


def decode_html(raw: bytes) -> tuple[str, str]:
    for encoding in ("utf-8", "cp932", "shift_jis"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError("unknown", b"", 0, 1, "unsupported HTML encoding")


def prefecture_from_link_text(text: str) -> str | None:
    match = re.match(r"^(.+?[都道府県])内", text)
    return match.group(1) if match else None


def discover_prefecture_pages(index_url: str) -> tuple[list[dict], dict]:
    raw = ltc.fetch(index_url)
    text, encoding = decode_html(raw)
    parser = TableAndLinkParser()
    parser.feed(text)

    pages = []
    seen = set()
    for link in parser.links:
        prefecture = prefecture_from_link_text(link["text"])
        if not prefecture:
            continue
        url = urljoin(index_url, link["href"])
        key = (prefecture, url)
        if key in seen:
            continue
        seen.add(key)
        pages.append({"prefecture": prefecture, "url": url})

    if len(pages) != 47:
        raise ValueError(f"expected 47 prefecture pages, got {len(pages)}")

    return pages, {
        "index_sha256": hashlib.sha256(raw).hexdigest(),
        "index_encoding": encoding,
    }


def extract_local_governments(prefecture: str, url: str) -> tuple[list[dict], dict]:
    raw = ltc.fetch(url)
    text, encoding = decode_html(raw)
    parser = TableAndLinkParser()
    parser.feed(text)

    records = []
    for row in parser.rows:
        if len(row) < 2:
            continue
        code = row[0].strip()
        name = row[1].strip()
        if re.fullmatch(r"\d{6}", code) and name:
            records.append({
                "prefecture": prefecture,
                "local_government_code": code,
                "name": name,
                "source_url": url,
            })

    if not records:
        raise ValueError(f"no local-government records found for {prefecture}: {url}")

    return records, {
        "prefecture": prefecture,
        "url": url,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "encoding": encoding,
        "record_count": len(records),
    }


def discover_mhlw_workbook_url(config: dict, target_id: str) -> str:
    page_raw = ltc.fetch(config["landing_page"])
    page_text, _ = ltc.decode_html(page_raw)
    parser = ltc.LinkParser()
    parser.feed(page_text)

    target = next(item for item in config["target_tables"] if item["id"] == target_id)
    needle = ltc.normalize_text(target["anchor_contains"])
    matches = [
        link for link in parser.links
        if needle in ltc.normalize_text(link["text"])
    ]
    if not matches:
        raise ValueError(f"MHLW target not found: {target_id}")
    selected = matches[-1] if target.get("expected_occurrence") == "last" else matches[0]
    return urljoin(config["landing_page"], selected["href"])


def insurer_rows_from_category1(raw: bytes) -> list[dict]:
    rows = ltc.first_sheet_rows(raw)
    result = []
    for row in rows:
        prefecture = row.get("A")
        insurer_name = row.get("B")
        total = ltc.as_int(row.get("F"))
        age_75_84 = ltc.as_int(row.get("H"))
        age_85_plus = ltc.as_int(row.get("I"))
        if not prefecture or not insurer_name or total is None:
            continue
        result.append({
            "prefecture": prefecture,
            "insurer_name": insurer_name,
            "category1_insured_total": total,
            "age75plus": (age_75_84 or 0) + (age_85_plus or 0),
        })
    return result


def classify_blocked(name: str) -> str:
    if "広域連合" in name:
        return "wide_area_union"
    if "組合" in name:
        return "administrative_union_or_association"
    return "no_exact_current_local_government_match"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jlis-config", required=True, type=Path)
    parser.add_argument("--ltc-config", required=True, type=Path)
    parser.add_argument("--membership-config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    jlis_config = json.loads(args.jlis_config.read_text(encoding="utf-8"))
    ltc_config = json.loads(args.ltc_config.read_text(encoding="utf-8"))
    membership_config = json.loads(
        args.membership_config.read_text(encoding="utf-8")
    )

    pages, index_meta = discover_prefecture_pages(jlis_config["index_url"])

    governments = []
    page_meta = []
    for page in pages:
        records, meta = extract_local_governments(page["prefecture"], page["url"])
        governments.extend(records)
        page_meta.append(meta)

    government_keys: dict[tuple[str, str], list[dict]] = {}
    for record in governments:
        government_keys.setdefault(
            (record["prefecture"], record["name"]), []
        ).append(record)

    membership_by_key: dict[tuple[str, str], dict] = {}
    for item in membership_config.get("insurers", []):
        if item.get("status") != "qualified_for_2026_06":
            continue
        key = (item["prefecture"], item["insurer_name"])
        if key in membership_by_key:
            raise ValueError(f"duplicate qualified membership: {key}")

        resolved_members = []
        for member_name in item["members"]:
            candidates = government_keys.get((item["prefecture"], member_name), [])
            if len(candidates) != 1:
                raise ValueError(
                    f"membership member must resolve exactly once: "
                    f"{key} -> {member_name!r}; candidates={len(candidates)}"
                )
            resolved_members.append(candidates[0])

        member_codes = [row["local_government_code"] for row in resolved_members]
        if len(member_codes) != len(set(member_codes)):
            raise ValueError(f"duplicate member code inside membership: {key}")

        membership_by_key[key] = {
            **item,
            "resolved_members": resolved_members,
            "local_government_codes": member_codes,
        }

    workbook_url = discover_mhlw_workbook_url(
        ltc_config, "insurer_category1_population"
    )
    workbook_raw = ltc.fetch(workbook_url)
    insurers = insurer_rows_from_category1(workbook_raw)

    matched = []
    blocked = []
    for insurer in insurers:
        key = (insurer["prefecture"], insurer["insurer_name"])
        candidates = government_keys.get(key, [])
        if len(candidates) == 1:
            body = candidates[0]
            matched.append({
                **insurer,
                "local_government_code": body["local_government_code"],
                "local_government_codes": [body["local_government_code"]],
                "jlis_source_url": body["source_url"],
                "match_method": "exact_prefecture_and_name",
            })
            continue

        membership = membership_by_key.get(key)
        if membership is not None and len(candidates) == 0:
            matched.append({
                **insurer,
                "local_government_code": None,
                "local_government_codes": membership["local_government_codes"],
                "member_names": membership["members"],
                "membership_source_urls": membership["source_urls"],
                "membership_temporal_basis": membership["temporal_basis"],
                "match_method": "qualified_insurer_membership",
            })
            continue

        blocked.append({
            **insurer,
            "candidate_count": len(candidates),
            "block_reason": (
                "ambiguous_exact_match"
                if len(candidates) > 1
                else classify_blocked(insurer["insurer_name"])
            ),
        })

    matched_codes = [
        code
        for row in matched
        for code in row["local_government_codes"]
    ]
    duplicate_matched_codes = [
        code for code, count in Counter(matched_codes).items() if count > 1
    ]
    if duplicate_matched_codes:
        raise ValueError(
            f"multiple insurer rows mapped to same local-government code: "
            f"{duplicate_matched_codes[:20]}"
        )

    blocked_reason_counts = Counter(row["block_reason"] for row in blocked)
    match_method_counts = Counter(row["match_method"] for row in matched)

    insurer_keys = {
        (row["prefecture"], row["insurer_name"]) for row in insurers
    }
    unused_qualified_memberships = sorted(
        f"{prefecture} / {insurer_name}"
        for prefecture, insurer_name in membership_by_key
        if (prefecture, insurer_name) not in insurer_keys
    )
    if unused_qualified_memberships:
        raise ValueError(
            "qualified memberships not present in MHLW demand rows: "
            + ", ".join(unused_qualified_memberships)
        )

    result = {
        "schema_version": "0.1",
        "qualification_date": "2026-10-07",
        "demand_period": ltc_config["period"],
        "jlis": {
            "index_url": jlis_config["index_url"],
            **index_meta,
            "prefecture_page_count": len(page_meta),
            "local_government_record_count": len(governments),
            "prefecture_pages": page_meta,
        },
        "mhlw": {
            "category1_workbook_url": workbook_url,
            "category1_workbook_sha256": hashlib.sha256(workbook_raw).hexdigest(),
            "insurer_row_count": len(insurers),
        },
        "crosswalk": {
            "matched_insurer_count": len(matched),
            "blocked_insurer_count": len(blocked),
            "coverage_percent": (len(matched) / len(insurers) * 100) if insurers else None,
            "duplicate_matched_local_government_code_count": len(duplicate_matched_codes),
            "blocked_reason_counts": dict(blocked_reason_counts),
            "match_method_counts": dict(match_method_counts),
            "qualified_membership_count": len(membership_by_key),
            "safe_match_policy": "direct exact J-LIS match, or period-aligned official membership whose member names each exact-match J-LIS",
            "temporal_boundary": "Direct J-LIS matches use current identity at qualification time. Group memberships are used only when their official evidence covers 2026-06.",
        },
        "matched": matched,
        "blocked": blocked,
        "publication_boundary": [
            "matched rows may be used for pilot analysis only after service-office geography compatibility is checked",
            "blocked rows must not receive an inferred municipality code",
            "group-insurer demand is never allocated to member municipalities; member office listings are aggregated upward to the insurer geography",
        ],
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    log_summary = {
        "jlis_local_government_records": len(governments),
        "mhlw_insurers": len(insurers),
        "matched_insurers": len(matched),
        "blocked_insurers": len(blocked),
        "coverage_percent": result["crosswalk"]["coverage_percent"],
        "blocked_reason_counts": dict(blocked_reason_counts),
        "match_method_counts": dict(match_method_counts),
        "sample_matched": matched[:10],
        "qualified_group_matches": [
            row for row in matched
            if row["match_method"] == "qualified_insurer_membership"
        ],
        "blocked": blocked,
    }
    print(json.dumps(log_summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
