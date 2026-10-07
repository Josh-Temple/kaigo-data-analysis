#!/usr/bin/env python3
"""Outcome-free source probe for MHLW home-visit-care office snapshots.

Downloads two official CSV snapshots, validates a minimal schema, and reports
summary-level listing changes. It never commits raw rows and does not interpret
listing disappearance as closure.
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import re
import tempfile
from typing import Iterable
from urllib.request import Request, urlopen


OFFICE_ID_CANDIDATES = ("事業所番号", "No")
MUNICIPALITY_CODE_CANDIDATES = ("都道府県コード又は市区町村コード",)
CITY_CANDIDATES = ("市区町村名",)
SERVICE_CANDIDATES = ("サービスの種類",)


@dataclass
class Snapshot:
    as_of: str
    published_at: str
    url: str


def normalize(value: str | None) -> str:
    if value is None:
        return ""
    return value.replace("\ufeff", "").strip()


def choose_header(headers: Iterable[str], candidates: tuple[str, ...], role: str) -> str:
    normalized = {normalize(header): header for header in headers}
    for candidate in candidates:
        if candidate in normalized:
            return normalized[candidate]
    raise ValueError(
        f"required {role} column not found; candidates={candidates}; "
        f"headers={sorted(normalized)}"
    )


def download(url: str) -> bytes:
    request = Request(
        url,
        headers={
            "User-Agent": "kaigo-data-analysis/0.1 (+https://github.com/Josh-Temple/kaigo-data-analysis)",
            "Accept": "text/csv,text/plain,*/*",
        },
    )
    with urlopen(request, timeout=120) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status} for {url}")
        return response.read()


def decode_csv(raw: bytes) -> tuple[str, str]:
    for encoding in ("utf-8-sig", "utf-8", "cp932"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError("unknown", b"", 0, 1, "unsupported CSV encoding")


def inspect_snapshot(snapshot: Snapshot, raw: bytes) -> dict:
    text, encoding = decode_csv(raw)
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if not reader.fieldnames:
        raise ValueError(f"no CSV header for snapshot {snapshot.as_of}")

    office_id_col = choose_header(reader.fieldnames, OFFICE_ID_CANDIDATES, "office id")
    municipality_col = choose_header(
        reader.fieldnames, MUNICIPALITY_CODE_CANDIDATES, "municipality code"
    )
    city_col = choose_header(reader.fieldnames, CITY_CANDIDATES, "city")
    service_col = choose_header(reader.fieldnames, SERVICE_CANDIDATES, "service")

    rows = 0
    office_ids: list[str] = []
    municipality_counts: Counter[str] = Counter()
    municipality_names: dict[str, str] = {}
    service_counts: Counter[str] = Counter()
    required_nulls = Counter()
    municipality_code_lengths = Counter()

    for row in reader:
        rows += 1
        office_id = normalize(row.get(office_id_col))
        municipality_code = normalize(row.get(municipality_col))
        city = normalize(row.get(city_col))
        service = normalize(row.get(service_col))

        if not office_id:
            required_nulls["office_id"] += 1
        else:
            office_ids.append(office_id)

        if not municipality_code:
            required_nulls["municipality_code"] += 1
        else:
            municipality_counts[municipality_code] += 1
            municipality_code_lengths[str(len(municipality_code))] += 1
            if city and municipality_code not in municipality_names:
                municipality_names[municipality_code] = city

        if not city:
            required_nulls["city"] += 1

        if not service:
            required_nulls["service"] += 1
        else:
            service_counts[service] += 1

    office_counter = Counter(office_ids)
    duplicate_office_ids = sum(1 for count in office_counter.values() if count > 1)

    return {
        "as_of": snapshot.as_of,
        "published_at": snapshot.published_at,
        "source_url": snapshot.url,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "encoding": encoding,
        "row_count": rows,
        "column_count": len(reader.fieldnames),
        "headers": [normalize(header) for header in reader.fieldnames],
        "selected_columns": {
            "office_id": normalize(office_id_col),
            "municipality_code": normalize(municipality_col),
            "city": normalize(city_col),
            "service": normalize(service_col),
        },
        "required_null_counts": dict(required_nulls),
        "unique_office_ids": len(office_counter),
        "duplicate_office_id_values": duplicate_office_ids,
        "municipality_count": len(municipality_counts),
        "municipality_code_length_distribution": dict(municipality_code_lengths),
        "service_value_counts": dict(service_counts.most_common(20)),
        "_office_ids": sorted(office_counter),
        "_municipality_counts": dict(municipality_counts),
        "_municipality_names": municipality_names,
    }


def compare_snapshots(before: dict, after: dict) -> dict:
    before_ids = set(before["_office_ids"])
    after_ids = set(after["_office_ids"])
    listed_added = sorted(after_ids - before_ids)
    listed_removed = sorted(before_ids - after_ids)

    municipality_codes = set(before["_municipality_counts"]) | set(
        after["_municipality_counts"]
    )
    changes = []
    for code in municipality_codes:
        before_count = before["_municipality_counts"].get(code, 0)
        after_count = after["_municipality_counts"].get(code, 0)
        delta = after_count - before_count
        if delta:
            name = (
                after["_municipality_names"].get(code)
                or before["_municipality_names"].get(code)
                or ""
            )
            changes.append(
                {
                    "municipality_code": code,
                    "municipality_name": name,
                    "before_count": before_count,
                    "after_count": after_count,
                    "net_change": delta,
                }
            )
    changes.sort(key=lambda item: (-abs(item["net_change"]), item["municipality_code"]))

    return {
        "before_as_of": before["as_of"],
        "after_as_of": after["as_of"],
        "row_count_change": after["row_count"] - before["row_count"],
        "unique_office_id_change": after["unique_office_ids"] - before["unique_office_ids"],
        "listed_added_candidate_count": len(listed_added),
        "listed_removed_candidate_count": len(listed_removed),
        "municipalities_with_nonzero_net_change": len(changes),
        "largest_absolute_municipality_changes": changes[:30],
        "semantic_boundary": [
            "listed_added_candidate_count is not automatically a count of new openings",
            "listed_removed_candidate_count is not automatically a count of closures",
            "office listing counts do not measure capacity, staffing, utilization, or quality",
        ],
    }


def strip_private_work_fields(snapshot: dict) -> dict:
    return {key: value for key, value in snapshot.items() if not key.startswith("_")}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    snapshots = [Snapshot(**item) for item in manifest["snapshots"]]
    if len(snapshots) != 2:
        raise ValueError("probe currently requires exactly two snapshots")

    inspected = []
    for snapshot in snapshots:
        raw = download(snapshot.url)
        inspected.append(inspect_snapshot(snapshot, raw))

    result = {
        "schema_version": "0.1",
        "source_id": manifest["source_id"],
        "service_code": manifest["service_code"],
        "service_name": manifest["service_name"],
        "source_page": manifest["source_page"],
        "probe_scope": "source qualification and listing-change summaries only",
        "snapshots": [strip_private_work_fields(item) for item in inspected],
        "comparison": compare_snapshots(inspected[0], inspected[1]),
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(json.dumps({
        "service": result["service_name"],
        "snapshots": [
            {
                "as_of": item["as_of"],
                "rows": item["row_count"],
                "unique_office_ids": item["unique_office_ids"],
                "municipalities": item["municipality_count"],
                "sha256": item["sha256"],
                "required_null_counts": item["required_null_counts"],
                "selected_columns": item["selected_columns"],
            }
            for item in result["snapshots"]
        ],
        "comparison": result["comparison"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
