#!/usr/bin/env python3
"""Qualify Digital Agency current local-government CSV and report its schema."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
from urllib.request import Request, urlopen


def fetch(url: str) -> bytes:
    request = Request(
        url,
        headers={
            "User-Agent": "kaigo-data-analysis/0.1 (+https://github.com/Josh-Temple/kaigo-data-analysis)",
            "Accept": "text/csv,*/*",
        },
    )
    with urlopen(request, timeout=120) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status} for {url}")
        return response.read()


def decode(raw: bytes) -> tuple[str, str]:
    for encoding in ("utf-8-sig", "utf-8", "cp932"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError("unknown", b"", 0, 1, "unsupported CSV encoding")


def normalize(value: str | None) -> str:
    return "" if value is None else value.replace("\ufeff", "").strip()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    config = json.loads(args.config.read_text(encoding="utf-8"))
    raw = fetch(config["csv_url"])
    text, encoding = decode(raw)
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if not reader.fieldnames:
        raise ValueError("CSV has no header")

    rows = list(reader)
    normalized_headers = [normalize(h) for h in reader.fieldnames]
    sample = []
    for row in rows[:5]:
        sample.append({
            normalize(k): normalize(v)
            for k, v in row.items()
        })

    result = {
        "schema_version": "0.1",
        "source_id": config["source_id"],
        "publisher": config["publisher"],
        "landing_page": config["landing_page"],
        "csv_url": config["csv_url"],
        "as_of": config["as_of"],
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "encoding": encoding,
        "row_count": len(rows),
        "column_count": len(normalized_headers),
        "headers": normalized_headers,
        "sample_rows": sample,
        "qualification_scope": [
            "official Digital Agency landing page and CSV",
            "file reachability",
            "encoding",
            "hash",
            "schema discovery",
            "row count"
        ],
        "boundary": "This dataset is not itself evidence of long-term-care insurer membership. It may only support current municipality identity/code matching."
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
