#!/usr/bin/env python3
"""Screen archived WSL LWF station CSVs without changing or aggregating them.

Python 3.10+, standard library only. Hourly timestamps may be local civil time:
clock-change duplicates are identified as a pattern, not automatically fixed.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from meteoswiss_download import AcquisitionError, atomic_json, write_csv

CSV_FIELDS = ["dataset", "station_code", "station_exposure", "doi", "local_path", "sha256",
              "first_timestamp", "last_timestamp", "rows", "numeric_precip_hours",
              "missing_precip_rows", "invalid_precip_rows", "negative_precip_rows",
              "duplicate_timestamps", "duplicate_conflicting_precip",
              "autumn_clock_change_duplicates", "other_duplicates", "forward_gaps_over_1h",
              "largest_forward_gap_hours", "first_year", "last_year", "published_2023_data",
              "dataset_spatial", "screen_status"]
YEAR_FIELDS = ["dataset", "station_code", "station_exposure", "year", "period_status",
               "rows", "unique_hours", "numeric_precip_hours", "missing_precip_rows",
               "duplicate_timestamps", "nominal_year_hours", "coverage_note"]
ISSUE_FIELDS = ["station_code", "dataset", "priority", "issue", "evidence", "review_action"]


def hash_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def local_path(root, value):
    target = (root / value).resolve()
    if root not in target.parents or not target.is_file():
        raise AcquisitionError(f"Missing or unsafe raw file: {value}")
    return target


def parse_precip(value):
    value = value.strip()
    if value.upper() in ("", "NA", "NAN", "NULL"):
        return "missing", None
    try:
        n = float(value)
        if math.isfinite(n):
            return "numeric", n
    except ValueError:
        pass
    return "invalid", None


def autumn_clock_pattern(ts):
    """Last Sunday in October, 02:00; suggestive of Swiss local civil time."""
    d = ts.date()
    return ts.hour == 2 and d.month == 10 and d.weekday() == 6 and (d + timedelta(days=7)).month == 11


def screen_station(path):
    counts = Counter()
    years = defaultdict(Counter)
    seen = {}
    first = last = previous = None
    max_forward_gap = 0.0
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file, delimiter=";")
        if not reader.fieldnames or "timestamp" not in reader.fieldnames or "precip" not in reader.fieldnames:
            raise AcquisitionError(f"Missing timestamp or precip column in {path}")
        for row in reader:
            if None in row:
                raise AcquisitionError(f"Malformed row in {path}")
            try:
                ts = datetime.fromisoformat(row["timestamp"])
            except (ValueError, TypeError) as exc:
                raise AcquisitionError(f"Invalid timestamp {row['timestamp']!r} in {path}") from exc
            if ts.tzinfo:
                raise AcquisitionError(f"Timezone-aware timestamp unexpectedly present in {path}")
            kind, value = parse_precip(row["precip"] or "")
            y = years[ts.year]
            counts["rows"] += 1
            y["rows"] += 1
            if first is None or ts < first:
                first = ts
            if last is None or ts > last:
                last = ts
            if previous is not None:
                delta = (ts - previous).total_seconds() / 3600
                if delta > 1:
                    counts["forward_gaps_over_1h"] += 1
                    max_forward_gap = max(max_forward_gap, delta)
            previous = ts
            if ts in seen:
                counts["duplicate_timestamps"] += 1
                y["duplicate_timestamps"] += 1
                if autumn_clock_pattern(ts):
                    counts["autumn_clock_change_duplicates"] += 1
                else:
                    counts["other_duplicates"] += 1
                if seen[ts] != (kind, value):
                    counts["duplicate_conflicting_precip"] += 1
            else:
                seen[ts] = kind, value
                y["unique_hours"] += 1
                if kind == "numeric":
                    y["numeric_precip_hours"] += 1
                    counts["numeric_precip_hours"] += 1
            if kind == "missing":
                counts["missing_precip_rows"] += 1
                y["missing_precip_rows"] += 1
            elif kind == "invalid":
                counts["invalid_precip_rows"] += 1
            elif value < 0:
                counts["negative_precip_rows"] += 1
    if not counts["rows"]:
        raise AcquisitionError(f"Empty station file: {path}")
    return counts, years, first, last, max_forward_gap


def audit(root, source, output):
    with source.open("r", encoding="utf-8-sig", newline="") as file:
        records = list(csv.DictReader(file, delimiter=";"))
    if not records or len({r.get("resource_id") for r in records}) != len(records):
        raise AcquisitionError("Station inventory empty or contains duplicate resource IDs")
    station_rows, year_rows, issues = [], [], []
    errors = []
    for i, r in enumerate(records, 1):
        code, dataset = r["station_code"], r["dataset"]
        try:
            path = local_path(root, r["local_path"])
            actual_hash = hash_file(path)
            if actual_hash != r["sha256"]:
                raise AcquisitionError(f"SHA-256 mismatch for {path}")
            counts, years, first, last, largest = screen_station(path)
            if (counts["rows"] != int(r["rows"]) or first.isoformat(sep=" ") != r["first_timestamp"]
                    or last.isoformat(sep=" ") != r["last_timestamp"]):
                raise AcquisitionError(f"Station inventory disagrees with raw CSV: {code}")
            counter_fields = ("rows", "numeric_precip_hours", "missing_precip_rows", "invalid_precip_rows",
                              "negative_precip_rows", "duplicate_timestamps", "duplicate_conflicting_precip",
                              "autumn_clock_change_duplicates", "other_duplicates", "forward_gaps_over_1h")
            station_rows.append({**{k: r.get(k, "") for k in CSV_FIELDS},
                **{key: counts[key] for key in counter_fields}, "first_timestamp": first.isoformat(sep=" "),
                "last_timestamp": last.isoformat(sep=" "), "largest_forward_gap_hours": largest,
                "first_year": first.year, "last_year": last.year,
                "published_2023_data": int(2023 in years and years[2023]["numeric_precip_hours"] > 0),
                "screen_status": "screened_raw_not_daily_validated"})
            for year in range(1951, 2024):
                c = years[year]
                nominal = (date(year + 1, 1, 1) - date(year, 1, 1)).days * 24
                if c["rows"] == 0:
                    status = "outside_published_span" if year < first.year or year > last.year else "no_rows_inside_span"
                elif year in (first.year, last.year) and (first.date() > date(year, 1, 1) or last.date() < date(year, 12, 31)):
                    status = "partial_boundary_year"
                else:
                    status = "rows_present_not_daily_validated"
                year_rows.append(dict(dataset=dataset, station_code=code,
                    station_exposure=r.get("station_exposure", ""), year=year,
                    period_status=status, nominal_year_hours=nominal,
                    coverage_note="Civil-time DST and accumulation period unverified; no hourly values aggregated",
                    **{key: c[key] for key in ("rows", "unique_hours", "numeric_precip_hours", "missing_precip_rows", "duplicate_timestamps")}))
            def issue(priority, name, evidence, action):
                issues.append(dict(station_code=code, dataset=dataset, priority=priority,
                                   issue=name, evidence=evidence, review_action=action))
            if last.year < 2023:
                issue("high", "public_snapshot_ends_before_2023", str(last.date()),
                      "Request/locate updated WSL LWF data; do not label subsequent years sensor gaps")
            if counts["duplicate_conflicting_precip"]:
                issue("high", "same_timestamp_different_precip", str(counts["duplicate_conflicting_precip"]),
                      "Verify timezone and clock-change hours with provider before daily totals")
            if counts["other_duplicates"]:
                issue("high", "duplicate_not_autumn_clock_pattern", str(counts["other_duplicates"]),
                      "Inspect source rows; preserve both pending resolution")
            if counts["negative_precip_rows"] or counts["invalid_precip_rows"]:
                issue("high", "invalid_or_negative_precipitation",
                      f"invalid={counts['invalid_precip_rows']};negative={counts['negative_precip_rows']}",
                      "Check provider codes and correction metadata")
            if counts["missing_precip_rows"]:
                issue("medium", "missing_precipitation_cells", str(counts["missing_precip_rows"]),
                      "Distinguish missing observations from zero rainfall; quantify usable days later")
            if counts["autumn_clock_change_duplicates"]:
                issue("medium", "autumn_clock_change_pattern", str(counts["autumn_clock_change_duplicates"]),
                      "Confirm timestamp timezone and fall-back hour mapping with WSL")
            if r.get("station_exposure") == "forest":
                issue("medium", "forest_exposure", "forest station",
                      "Clarify precipitation/throughfall quantity before gridding inclusion")
        except (AcquisitionError, OSError, ValueError, KeyError) as exc:
            errors.append(dict(dataset=dataset, station_code=code, error=f"{type(exc).__name__}: {exc}"))
        print(f"{i}/{len(records)} {code}: {'error' if errors and errors[-1].get('station_code') == code else 'screened'}", flush=True)
    output.mkdir(parents=True, exist_ok=False)
    write_csv(output / "station_screen.csv", station_rows, CSV_FIELDS)
    write_csv(output / "yearly_screen_1951_2023.csv", year_rows, YEAR_FIELDS)
    write_csv(output / "review_issues.csv", issues, ISSUE_FIELDS)
    atomic_json(output / "errors.json", errors)
    summary = dict(created_at_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        source_inventory=str(source), station_files_in_inventory=len(records), screened=len(station_rows),
        errors=len(errors), issue_counts=dict(Counter(i["issue"] for i in issues)),
        status="completed" if not errors else "incomplete",
        scientific_status="screening_only_no_daily_aggregation_or_homogeneity_assessment")
    atomic_json(output / "summary.json", summary)
    return summary


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument("--run", type=Path, help="Acquisition run folder; default latest completed full-scope run")
    args = p.parse_args(argv)
    root = args.root.resolve()
    if args.run:
        run = args.run.resolve()
    else:
        runs = sorted((root / "runs/wsl_lwf").glob("*/summary.json"), reverse=True)
        matches = [f.parent for f in runs if json.loads(f.read_text(encoding="utf-8")).get("status") == "completed_public_LWF_scope"]
        if not matches:
            p.error("No completed full-scope LWF run found; pass --run")
        run = matches[0]
    source = run / "station_inventory.csv"
    if not source.is_file():
        p.error(f"Missing station inventory: {source}")
    output = root / "runs/wsl_lwf_screen" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    try:
        result = audit(root, source, output)
    except (AcquisitionError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Status: {result['status']}. Reports: {output}")
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())
