#!/usr/bin/env python3
"""Build a source-qualified pilot metric for home-visit care.

The pilot uses direct exact J-LIS insurer matches plus source-qualified
group-insurer memberships. Designated-city wards and qualified group members
are aggregated upward to the insurer geography. Demand is never allocated down
from a group insurer to member municipalities.
"""

from __future__ import annotations

import argparse
from collections import Counter
import csv
import io
import json
from pathlib import Path
import statistics

import probe_mhlw_ltc_status as ltc
import probe_mhlw_visit_care as offices
import qualify_insurer_municipality_crosswalk as crosswalk


def discover_ltc_urls(config: dict) -> dict[str, str]:
    return {
        target["id"]: crosswalk.discover_mhlw_workbook_url(config, target["id"])
        for target in config["target_tables"]
    }


def demand_rows(category1_raw: bytes, certified_raw: bytes) -> list[dict]:
    cat_rows = ltc.first_sheet_rows(category1_raw)
    cert_rows = ltc.first_sheet_rows(certified_raw)

    certified = {}
    for row in cert_rows:
        if row.get("A") and row.get("B"):
            value = ltc.as_int(row.get("R"))
            if value is not None:
                certified[(row["A"], row["B"])] = value

    result = []
    for row in cat_rows:
        prefecture = row.get("A")
        insurer_name = row.get("B")
        total = ltc.as_int(row.get("F"))
        age75_84 = ltc.as_int(row.get("H"))
        age85plus = ltc.as_int(row.get("I"))
        if not prefecture or not insurer_name or total is None:
            continue
        key = (prefecture, insurer_name)
        if key not in certified:
            raise ValueError(f"certified denominator missing for {key}")
        result.append({
            "prefecture": prefecture,
            "insurer_name": insurer_name,
            "category1_insured": total,
            "age75plus": (age75_84 or 0) + (age85plus or 0),
            "category1_certified": certified[key],
        })
    return result


def visit_care_counts(raw: bytes) -> tuple[Counter[str], dict]:
    text, encoding = offices.decode_csv(raw)
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if not reader.fieldnames:
        raise ValueError("office CSV has no header")

    municipality_col = offices.choose_header(
        reader.fieldnames,
        offices.MUNICIPALITY_CODE_CANDIDATES,
        "municipality code",
    )
    office_id_col = offices.choose_header(
        reader.fieldnames,
        offices.OFFICE_ID_CANDIDATES,
        "office id",
    )

    counts: Counter[str] = Counter()
    office_ids = []
    for row in reader:
        code = offices.normalize(row.get(municipality_col))
        office_id = offices.normalize(row.get(office_id_col))
        if not code or not office_id:
            raise ValueError("visit-care source has null municipality code or office id")
        counts[code] += 1
        office_ids.append(office_id)

    if len(office_ids) != len(set(office_ids)):
        raise ValueError("visit-care source has duplicate office ids")

    return counts, {
        "row_count": len(office_ids),
        "encoding": encoding,
        "municipality_code_count": len(counts),
    }


def direct_office_geography(
    body: dict,
    governments: list[dict],
) -> tuple[list[str], str]:
    child_wards = [
        item for item in governments
        if item["prefecture"] == body["prefecture"]
        and item["local_government_code"] != body["local_government_code"]
        and item["name"].startswith(body["name"])
        and item["name"][len(body["name"]):].endswith("区")
        and item["name"][len(body["name"]):]
    ]
    codes = [body["local_government_code"]]
    mode = "direct_local_government"
    if child_wards:
        codes.extend(item["local_government_code"] for item in child_wards)
        mode = "designated_city_plus_wards"
    return codes, mode


def percentile(sorted_values: list[float], p: float) -> float | None:
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = (len(sorted_values) - 1) * p
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    fraction = position - lower
    return sorted_values[lower] * (1 - fraction) + sorted_values[upper] * fraction


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jlis-config", required=True, type=Path)
    parser.add_argument("--ltc-config", required=True, type=Path)
    parser.add_argument("--office-config", required=True, type=Path)
    parser.add_argument("--membership-config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    jlis_config = json.loads(args.jlis_config.read_text(encoding="utf-8"))
    ltc_config = json.loads(args.ltc_config.read_text(encoding="utf-8"))
    office_config = json.loads(args.office_config.read_text(encoding="utf-8"))
    membership_config = json.loads(
        args.membership_config.read_text(encoding="utf-8")
    )

    pages, _ = crosswalk.discover_prefecture_pages(jlis_config["index_url"])
    governments = []
    for page in pages:
        records, _ = crosswalk.extract_local_governments(
            page["prefecture"], page["url"]
        )
        governments.extend(records)

    government_by_key: dict[tuple[str, str], list[dict]] = {}
    for record in governments:
        government_by_key.setdefault(
            (record["prefecture"], record["name"]), []
        ).append(record)

    membership_by_key: dict[tuple[str, str], dict] = {}
    for item in membership_config.get("insurers", []):
        if item.get("status") != "qualified_for_2026_06":
            continue
        key = (item["prefecture"], item["insurer_name"])
        if key in membership_by_key:
            raise ValueError(f"duplicate qualified membership: {key}")
        member_bodies = []
        for member_name in item["members"]:
            candidates = government_by_key.get((item["prefecture"], member_name), [])
            if len(candidates) != 1:
                raise ValueError(
                    f"qualified membership member must exact-match J-LIS: "
                    f"{key} -> {member_name!r}; candidates={len(candidates)}"
                )
            member_bodies.append(candidates[0])
        membership_by_key[key] = {
            **item,
            "member_bodies": member_bodies,
        }

    ltc_urls = discover_ltc_urls(ltc_config)
    category1_raw = ltc.fetch(ltc_urls["insurer_category1_population"])
    certified_raw = ltc.fetch(ltc_urls["insurer_certified_total"])
    demand = demand_rows(category1_raw, certified_raw)

    snapshot = next(
        item for item in office_config["snapshots"]
        if item["as_of"] == "2026-06-30"
    )
    office_raw = ltc.fetch(snapshot["url"])
    office_counts, office_meta = visit_care_counts(office_raw)

    matched_rows = []
    blocked = []
    for row in demand:
        key = (row["prefecture"], row["insurer_name"])
        candidates = government_by_key.get(key, [])
        membership = membership_by_key.get(key)

        local_government_code = None
        membership_source_urls = None
        member_names = None

        if len(candidates) == 1:
            body = candidates[0]
            geography_codes, geography_mode = direct_office_geography(
                body, governments
            )
            local_government_code = body["local_government_code"]
        elif len(candidates) == 0 and membership is not None:
            geography_codes = []
            for body in membership["member_bodies"]:
                body_codes, _ = direct_office_geography(body, governments)
                geography_codes.extend(body_codes)
            if len(geography_codes) != len(set(geography_codes)):
                raise ValueError(f"duplicate geography code in membership: {key}")
            geography_mode = "qualified_insurer_membership_aggregate"
            membership_source_urls = membership["source_urls"]
            member_names = membership["members"]
        else:
            blocked.append({
                **row,
                "block_reason": (
                    "ambiguous_exact_match"
                    if len(candidates) > 1
                    else crosswalk.classify_blocked(row["insurer_name"])
                ),
            })
            continue

        office_count = sum(office_counts.get(code, 0) for code in geography_codes)
        age75plus = row["age75plus"]
        certified = row["category1_certified"]

        matched_rows.append({
            **row,
            "local_government_code": local_government_code,
            "geography_mode": geography_mode,
            "office_geography_codes": geography_codes,
            "member_names": member_names,
            "membership_source_urls": membership_source_urls,
            "visit_care_office_count": office_count,
            "offices_per_10k_age75plus": (
                office_count / age75plus * 10000 if age75plus else None
            ),
            "offices_per_10k_category1_certified": (
                office_count / certified * 10000 if certified else None
            ),
        })

    national_category1 = sum(row["category1_insured"] for row in demand)
    national_age75plus = sum(row["age75plus"] for row in demand)
    national_certified = sum(row["category1_certified"] for row in demand)
    matched_category1 = sum(row["category1_insured"] for row in matched_rows)
    matched_age75plus = sum(row["age75plus"] for row in matched_rows)
    matched_certified = sum(row["category1_certified"] for row in matched_rows)

    metric_values = sorted(
        row["offices_per_10k_age75plus"]
        for row in matched_rows
        if row["offices_per_10k_age75plus"] is not None
    )

    designated = sorted(
        (
            {
                "prefecture": row["prefecture"],
                "insurer_name": row["insurer_name"],
                "age75plus": row["age75plus"],
                "category1_certified": row["category1_certified"],
                "visit_care_office_count": row["visit_care_office_count"],
                "offices_per_10k_age75plus": row["offices_per_10k_age75plus"],
                "offices_per_10k_category1_certified": row["offices_per_10k_category1_certified"],
                "ward_count": len(row["office_geography_codes"]) - 1,
            }
            for row in matched_rows
            if row["geography_mode"] == "designated_city_plus_wards"
        ),
        key=lambda item: item["offices_per_10k_age75plus"] or -1,
        reverse=True,
    )

    yokohama = next(
        (
            row for row in designated
            if row["prefecture"] == "神奈川県"
            and row["insurer_name"] == "横浜市"
        ),
        None,
    )

    large_municipalities = [
        row for row in matched_rows
        if row["age75plus"] >= 10000
        and row["geography_mode"] != "qualified_insurer_membership_aggregate"
    ]
    large_sorted = sorted(
        large_municipalities,
        key=lambda item: item["offices_per_10k_age75plus"] or -1,
    )

    geography_mode_counts = Counter(
        row["geography_mode"] for row in matched_rows
    )

    result = {
        "schema_version": "0.1",
        "analysis_status": "pilot_observation_only",
        "service": "訪問介護",
        "office_snapshot": "2026-06-30",
        "demand_period": ltc_config["period"],
        "source_checks": {
            "office": office_meta,
            "demand_insurer_rows": len(demand),
            "matched_insurer_rows": len(matched_rows),
            "blocked_insurer_rows": len(blocked),
            "geography_mode_counts": dict(geography_mode_counts),
            "qualified_group_insurer_rows": geography_mode_counts.get(
                "qualified_insurer_membership_aggregate", 0
            ),
        },
        "population_coverage": {
            "category1_insured_percent": matched_category1 / national_category1 * 100,
            "age75plus_percent": matched_age75plus / national_age75plus * 100,
            "category1_certified_percent": matched_certified / national_certified * 100,
        },
        "metric_distribution": {
            "unit": "listed home-visit-care offices per 10,000 age-75-plus category-1 insured persons",
            "n": len(metric_values),
            "p25": percentile(metric_values, 0.25),
            "median": statistics.median(metric_values),
            "p75": percentile(metric_values, 0.75),
        },
        "designated_cities": designated,
        "yokohama": yokohama,
        "large_municipality_exploration": {
            "threshold": "age75plus >= 10000",
            "count": len(large_sorted),
            "lowest_10": [
                {
                    "prefecture": row["prefecture"],
                    "insurer_name": row["insurer_name"],
                    "age75plus": row["age75plus"],
                    "visit_care_office_count": row["visit_care_office_count"],
                    "offices_per_10k_age75plus": row["offices_per_10k_age75plus"],
                }
                for row in large_sorted[:10]
            ],
            "highest_10": [
                {
                    "prefecture": row["prefecture"],
                    "insurer_name": row["insurer_name"],
                    "age75plus": row["age75plus"],
                    "visit_care_office_count": row["visit_care_office_count"],
                    "offices_per_10k_age75plus": row["offices_per_10k_age75plus"],
                }
                for row in large_sorted[-10:][::-1]
            ],
        },
        "blocked_reason_counts": dict(Counter(
            row["block_reason"] for row in blocked
        )),
        "interpretation_limits": [
            "office count is a listing count, not service capacity, staffing, utilization, quality, or accessible supply",
            "the denominator is category-1 insured population age 75+, not observed home-visit-care users",
            "rankings are exploratory comparisons only and do not establish shortage or oversupply",
            "qualified group insurers aggregate member-municipality office listings upward; group demand is not allocated down to members",
            "unresolved group insurers and insurer names remain excluded, not imputed",
            "J-LIS geography is current at qualification time; exact 2026-06-30 temporal equivalence remains a separate check",
        ],
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(json.dumps({
        "source_checks": result["source_checks"],
        "population_coverage": result["population_coverage"],
        "metric_distribution": result["metric_distribution"],
        "yokohama": result["yokohama"],
        "designated_cities": result["designated_cities"],
        "large_municipality_exploration": result["large_municipality_exploration"],
        "blocked_reason_counts": result["blocked_reason_counts"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
