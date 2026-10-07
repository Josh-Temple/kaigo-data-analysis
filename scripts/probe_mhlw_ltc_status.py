#!/usr/bin/env python3
"""Discover and qualify insurer-level files from MHLW LTC monthly report pages."""

from __future__ import annotations

import argparse
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
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
        })

    result = {
        "schema_version": "0.1",
        "source_id": config["source_id"],
        "period": config["period"],
        "landing_page": config["landing_page"],
        "landing_page_sha256": hashlib.sha256(page_raw).hexdigest(),
        "landing_page_encoding": page_encoding,
        "link_count": len(html.links),
        "tables": qualified,
        "qualification_scope": [
            "official landing page reachable",
            "target insurer-level links discoverable without guessed file URLs",
            "target files reachable",
            "target file hashes and container signatures recorded"
        ],
        "not_yet_qualified": [
            "spreadsheet sheet names and cell layout",
            "insurer code field",
            "row count",
            "category-1 insured extraction",
            "certified-person extraction",
            "municipality-code join"
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
