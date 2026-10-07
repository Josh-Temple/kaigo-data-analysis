#!/usr/bin/env python3
"""Validate source and metric registries without loading analytical data."""

from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATH = ROOT / "config" / "data_sources.json"
METRIC_PATH = ROOT / "config" / "metrics.json"

SOURCE_REQUIRED = {
    "id",
    "title",
    "publisher",
    "landing_page",
    "geographic_grain",
    "temporal_grain",
    "cadence",
    "status",
    "intended_use",
    "limits",
}
ALLOWED_SOURCE_STATUS = {"candidate", "qualified", "secondary_only", "blocked"}

METRIC_REQUIRED = {
    "id",
    "label",
    "formula",
    "unit",
    "required_source_ids",
    "interpretation_limit",
}


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def fail(messages: list[str]) -> None:
    for message in messages:
        print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def validate() -> None:
    errors: list[str] = []
    sources_doc = load_json(SOURCE_PATH)
    metrics_doc = load_json(METRIC_PATH)

    datasets = sources_doc.get("datasets")
    metrics = metrics_doc.get("metrics")

    if not isinstance(datasets, list) or not datasets:
        errors.append("config/data_sources.json must contain a non-empty datasets array")
        datasets = []

    if not isinstance(metrics, list) or not metrics:
        errors.append("config/metrics.json must contain a non-empty metrics array")
        metrics = []

    source_ids: set[str] = set()
    for index, source in enumerate(datasets):
        if not isinstance(source, dict):
            errors.append(f"datasets[{index}] must be an object")
            continue

        missing = SOURCE_REQUIRED - source.keys()
        if missing:
            errors.append(
                f"datasets[{index}] missing fields: {', '.join(sorted(missing))}"
            )

        source_id = source.get("id")
        if not isinstance(source_id, str) or not source_id.strip():
            errors.append(f"datasets[{index}].id must be a non-empty string")
        elif source_id in source_ids:
            errors.append(f"duplicate source id: {source_id}")
        else:
            source_ids.add(source_id)

        status = source.get("status")
        if status not in ALLOWED_SOURCE_STATUS:
            errors.append(
                f"datasets[{index}].status must be one of {sorted(ALLOWED_SOURCE_STATUS)}"
            )

        url = source.get("landing_page")
        if not isinstance(url, str) or not url.startswith("https://"):
            errors.append(f"datasets[{index}].landing_page must use https")

        for field in ("intended_use", "limits"):
            value = source.get(field)
            if not isinstance(value, list) or not value:
                errors.append(f"datasets[{index}].{field} must be a non-empty array")

    metric_ids: set[str] = set()
    for index, metric in enumerate(metrics):
        if not isinstance(metric, dict):
            errors.append(f"metrics[{index}] must be an object")
            continue

        missing = METRIC_REQUIRED - metric.keys()
        if missing:
            errors.append(
                f"metrics[{index}] missing fields: {', '.join(sorted(missing))}"
            )

        metric_id = metric.get("id")
        if not isinstance(metric_id, str) or not metric_id.strip():
            errors.append(f"metrics[{index}].id must be a non-empty string")
        elif metric_id in metric_ids:
            errors.append(f"duplicate metric id: {metric_id}")
        else:
            metric_ids.add(metric_id)

        refs = metric.get("required_source_ids")
        if not isinstance(refs, list) or not refs:
            errors.append(
                f"metrics[{index}].required_source_ids must be a non-empty array"
            )
        else:
            for ref in refs:
                if ref not in source_ids:
                    errors.append(
                        f"metric {metric_id!r} references unknown source id {ref!r}"
                    )

        limit = metric.get("interpretation_limit")
        if not isinstance(limit, str) or not limit.strip():
            errors.append(
                f"metrics[{index}].interpretation_limit must be a non-empty string"
            )

    if errors:
        fail(errors)

    print(
        f"OK: {len(source_ids)} sources and {len(metric_ids)} metrics validated"
    )


if __name__ == "__main__":
    validate()
