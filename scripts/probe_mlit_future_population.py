#!/usr/bin/env python3
"""Discover the official MLIT Kanagawa 250m future-population download.

The landing page exposes the file through page-side download controls. This
probe does not guess a storage URL. It records the exact HTML evidence around
the official filename and extracts candidate URLs/attributes for qualification.
"""

from __future__ import annotations

import argparse
from html.parser import HTMLParser
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urljoin
from urllib.request import Request, urlopen


def fetch(url: str) -> bytes:
    request = Request(
        url,
        headers={
            "User-Agent": "kaigo-data-analysis/0.1 (+https://github.com/Josh-Temple/kaigo-data-analysis)",
            "Accept": "text/html,*/*",
        },
    )
    with urlopen(request, timeout=120) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status} for {url}")
        return response.read()


def decode(raw: bytes) -> tuple[str, str]:
    for encoding in ("utf-8", "utf-8-sig", "cp932", "shift_jis"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8-replace"


class EvidenceParser(HTMLParser):
    def __init__(self, expected_filename: str) -> None:
        super().__init__()
        self.expected_filename = expected_filename
        self.elements: list[dict] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        clean = {k: v for k, v in attrs if v is not None}
        joined = " ".join(str(v) for v in clean.values())
        if self.expected_filename in joined or any(
            key in clean for key in ("href", "onclick", "data-url", "value")
        ):
            self.elements.append({"tag": tag, "attrs": clean})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    config = json.loads(args.config.read_text(encoding="utf-8"))
    raw = fetch(config["landing_page"])
    text, encoding = decode(raw)
    filename = config["expected_filename"]

    occurrences = [m.start() for m in re.finditer(re.escape(filename), text)]
    contexts = []
    for pos in occurrences:
        start = max(0, pos - 600)
        end = min(len(text), pos + len(filename) + 900)
        contexts.append(text[start:end])

    html = EvidenceParser(filename)
    html.feed(text)

    exact_elements = [
        element for element in html.elements
        if filename in json.dumps(element, ensure_ascii=False)
    ]

    candidate_urls = []
    for element in html.elements:
        for key, value in element["attrs"].items():
            if not isinstance(value, str):
                continue
            if filename in value:
                for url in re.findall(r"https?://[^\"'<> ]+|[^\"'<> ]+\\.zip", value):
                    candidate_urls.append({
                        "tag": element["tag"],
                        "attribute": key,
                        "raw": url,
                        "resolved": urljoin(config["landing_page"], url),
                    })

    # Also scan raw HTML/scripts for URL-like strings containing the exact file.
    raw_patterns = re.findall(
        r"""(?P<url>(?:https?://|/|\.\.?/)[^\"'<>\s]*"""
        + re.escape(filename)
        + r"""[^\"'<>\s]*)""",
        text,
    )
    for url in raw_patterns:
        candidate_urls.append({
            "tag": "raw_html",
            "attribute": "text",
            "raw": url,
            "resolved": urljoin(config["landing_page"], url),
        })

    deduped = []
    seen = set()
    for item in candidate_urls:
        key = item["resolved"]
        if key not in seen:
            seen.add(key)
            deduped.append(item)

    result = {
        "schema_version": "0.1",
        "source_id": config["source_id"],
        "prefecture": config["prefecture"],
        "landing_page": config["landing_page"],
        "landing_page_sha256": hashlib.sha256(raw).hexdigest(),
        "landing_page_encoding": encoding,
        "expected_filename": filename,
        "filename_occurrence_count": len(occurrences),
        "exact_html_elements": exact_elements,
        "candidate_download_urls": deduped,
        "html_contexts": contexts,
        "status": (
            "download_candidate_discovered"
            if deduped
            else "filename_verified_download_url_not_yet_resolved"
        ),
        "boundary": "No storage URL is constructed from a guessed path.",
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps({
        "status": result["status"],
        "filename_occurrence_count": result["filename_occurrence_count"],
        "candidate_download_urls": result["candidate_download_urls"],
        "exact_html_elements": result["exact_html_elements"],
        "html_contexts": result["html_contexts"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
