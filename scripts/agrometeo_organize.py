#!/usr/bin/env python3
"""Audit and organize Agrometeo precipitation without changing acquired inputs.

The output is a transcription/provenance view of sensor 6:sum. Daily and
monthly API responses remain independent; a match of their sums is not QC.
"""
from __future__ import annotations

import argparse
import calendar
import csv
import hashlib
import json
import re
import shutil
import uuid
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path


FIELDS = [
    "collection", "station_code", "station_id", "catalogue_station_name",
    "catalogue_canton", "catalogue_latitude", "catalogue_longitude",
    "catalogue_elevation_m", "catalogue_metadata_flags", "metadata_scope",
    "period", "precipitation_mm", "source_value_raw", "value_status",
    "raw_cell_kind", "source_json", "source_sha256", "source_row",
    "metadata_source_sha256", "sensor_spec", "measurement_filter",
]
AUDIT_FIELDS = [
    "station_code", "month", "daily_dates_returned", "expected_days",
    "numeric_daily_days", "negative_daily_days", "monthly_record_returned",
    "daily_numeric_sum_mm", "source_monthly_mm", "difference_mm", "status",
]


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        yield from csv.DictReader(f, delimiter=";")


def writer(path, fields):
    f = path.open("w", encoding="utf-8-sig", newline="")
    w = csv.DictWriter(f, fieldnames=fields, delimiter=";", extrasaction="raise")
    w.writeheader()
    return f, w


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def decimal_value(value):
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        n = Decimal(str(value))
        return n if n.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def plain(n):
    return format(n, "f") if n is not None else ""


class SourceReader:
    def __init__(self, root, manifest):
        self.root = root
        self.manifest = manifest
        self.verified = set()

    @lru_cache(maxsize=2)
    def rows(self, relative, expected_sha):
        path = (self.root / relative).resolve()
        if self.root not in path.parents or not path.is_file():
            raise ValueError(f"Source JSON missing or outside project: {relative}")
        key = (relative, expected_sha)
        if key not in self.verified:
            if digest(path) != expected_sha:
                raise ValueError(f"Source JSON SHA-256 differs: {relative}")
            self.verified.add(key)
        with path.open(encoding="utf-8-sig") as f:
            obj = json.load(f)
        if not isinstance(obj, dict) or not isinstance(obj.get("data"), list):
            raise ValueError(f"Source JSON has no data array: {relative}")
        return obj["data"]

    def cell(self, row, sid, scale, specs):
        relative, sha = row["source_json"], row["source_sha256"]
        m = self.manifest.get(relative)
        if m is None or m["sha256"] != sha or m["kind"] != scale:
            raise ValueError(f"Source JSON is absent or different in manifest: {relative}")
        records = self.rows(relative, sha)
        index = int(row["source_row"])
        if not 1 <= index <= len(records):
            raise ValueError(f"Source row outside JSON: {relative} #{index}")
        raw = records[index - 1]
        if not isinstance(raw, dict) or not isinstance(raw.get("date"), str):
            raise ValueError(f"Invalid source row: {relative} #{index}")
        stamp = raw["date"][:10] if scale == "daily" else raw["date"]
        if stamp != row["date"]:
            raise ValueError(f"Source date differs from extract: {relative} #{index}")
        for spec in specs:
            field = spec.replace(":", "_")
            raw_key = f"{sid}_{field}"
            raw_value = raw.get(raw_key)
            if isinstance(raw_value, (dict, list, bool)):
                raise ValueError(f"Unsupported sensor value: {relative} #{index} {raw_key}")
            expected_csv = "" if raw_value is None else str(raw_value)
            if row[field] != expected_csv:
                raise ValueError(f"Sensor cell differs from original JSON: {relative} #{index} {raw_key}")
        key = f"{sid}_6_sum"
        if key not in raw:
            value, kind = None, "absent"
        else:
            value = raw[key]
            kind = ("null" if value is None else "empty_string" if value == "" else
                    "numeric_string" if isinstance(value, str) and decimal_value(value) is not None else
                    "number" if isinstance(value, (int, float)) and not isinstance(value, bool) else
                    "other_string" if isinstance(value, str) else "unsupported")
        if kind == "unsupported":
            raise ValueError(f"Unsupported source value: {relative} #{index}")
        return value, kind


def monthly_audit(sid, daily, monthly):
    for month in sorted(set(daily) | set(monthly)):
        days = daily.get(month, {})
        expected = calendar.monthrange(int(month[:4]), int(month[5:7]))[1]
        numeric = [v for v in days.values() if v is not None]
        negatives = sum(v < 0 for v in numeric)
        month_value = monthly.get(month)
        complete = len(days) == expected and len(numeric) == expected
        daily_sum = sum(numeric, Decimal(0)) if complete else None
        if month not in monthly:
            status = "no_monthly_record"
        elif month_value is None:
            status = "monthly_missing_numeric"
        elif not complete:
            status = "incomplete_daily"
        elif negatives:
            status = "negative_daily_review"
        elif daily_sum == month_value:
            status = "matches_numeric_sum"
        else:
            status = "mismatch_review"
        yield dict(station_code=f"AGROMETEO_{sid}", month=month,
                   daily_dates_returned=len(days), expected_days=expected,
                   numeric_daily_days=len(numeric), negative_daily_days=negatives,
                   monthly_record_returned="yes" if month in monthly else "no",
                   daily_numeric_sum_mm=plain(daily_sum), source_monthly_mm=plain(month_value),
                   difference_mm=plain(daily_sum - month_value) if daily_sum is not None and month_value is not None else "",
                   status=status)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument("--run-id", required=True)
    args = p.parse_args()
    root = args.root.resolve()
    run_id = args.run_id
    if not re.fullmatch(r"[A-Za-z0-9_]+", run_id):
        p.error("Invalid run ID")
    run = root / "runs/agrometeo" / run_id
    summary_file = run / "summary.json"
    if not summary_file.is_file():
        p.error(f"Missing run summary: {summary_file}")
    summary = json.loads(summary_file.read_text(encoding="utf-8"))
    config = json.loads((run / "configuration.json").read_text(encoding="utf-8"))
    if (summary.get("status") not in ("completed_selected_scope", "completed_requested_public_scope")
            or summary.get("failed_requests") != 0 or config.get("measured") != 1
            or not {"day", "month"}.issubset(config.get("scales", []))
            or "6:sum" not in summary.get("sensor_specs", [])):
        p.error("Need a complete measured-only run with day, month and precipitation 6:sum")
    inventory_file = run / "station_inventory.csv"
    coverage_file = run / "station_coverage.csv"
    manifest_file = run / "download_manifest.csv"
    for path in (inventory_file, coverage_file, manifest_file, run / "sensor_inventory.csv"):
        if not path.is_file():
            p.error(f"Missing source report: {path}")
    specs = list(summary["sensor_specs"])
    if len(specs) != len(set(specs)) or any(not re.fullmatch(r"\d+:(avg|min|max|sum)", s) for s in specs):
        raise ValueError("Duplicate or unsupported sensor specification")
    extra_fields = ["source_" + s.replace(":", "_") for s in specs if s != "6:sum"]
    output_fields = FIELDS + extra_fields
    sensors = {r["sensor_id"]: r for r in read_csv(run / "sensor_inventory.csv")}
    if "6" not in sensors or sensors["6"]["default_aggregation"] != "sum":
        raise ValueError("Sensor 6:sum definition not present in metadata")
    inventory = {r["station_id"]: r for r in read_csv(inventory_file)}
    selected = [str(sid) for sid in summary["selected_station_ids"]]
    if len(selected) != len(set(selected)) or set(selected) - set(inventory):
        raise ValueError("Selected station IDs missing or repeated in inventory")
    metadata_manifest = json.loads((run / "metadata_manifest.json").read_text(encoding="utf-8"))
    for entry in metadata_manifest.values():
        path = (root / entry["local_path"]).resolve()
        if root not in path.parents or not path.is_file() or digest(path) != entry["sha256"]:
            raise ValueError(f"Metadata source missing or SHA-mismatched: {entry['local_path']}")
    metadata_shas = {entry["sha256"] for entry in metadata_manifest.values()}
    if any(meta["metadata_source_sha256"] not in metadata_shas for meta in inventory.values()):
        raise ValueError("Station inventory references unknown metadata SHA")
    coverage_rows = [r for r in read_csv(coverage_file) if r["sensor_spec"] == "6:sum"]
    coverage = {(r["station_id"], r["scale"]): r for r in coverage_rows}
    if len(coverage) != len(coverage_rows):
        raise ValueError("Duplicate station-scale precipitation coverage")
    conflicts = defaultdict(set)
    for c in read_csv(run / "metadata_conflicts.csv"):
        conflicts[c["station_id"]].add(c["field"])
    manifest = {r["local_path"]: r for r in read_csv(manifest_file)
                if r["kind"] in ("daily", "monthly") and r["status"] in ("downloaded", "verified_local_cache")}
    source = SourceReader(root, manifest)
    point_rows = []
    out = root / "working/agrometeo/organized" / run_id
    if out.exists():
        p.error(f"Output exists; refusing to overwrite: {out}")
    out.parent.mkdir(parents=True, exist_ok=True)
    stage = out.parent / (".stage_" + uuid.uuid4().hex)
    stage.mkdir()
    handles = []
    counts, statuses, audit_counts = Counter(), Counter(), Counter()
    register = []
    try:
        combined = {}
        for scale in ("daily", "monthly"):
            handle, combined[scale] = writer(stage / (scale + ".csv"), output_fields)
            handles.append(handle)
        ahandle, awriter = writer(stage / "monthly_reconciliation.csv", AUDIT_FIELDS)
        handles.append(ahandle)
        for i, sid in enumerate(selected, 1):
            meta = inventory[sid]
            code = f"AGROMETEO_{sid}"
            folder = stage / "stations" / code
            folder.mkdir(parents=True)
            daily, monthly = defaultdict(dict), {}
            station_stats = Counter()
            for scale in ("daily", "monthly"):
                source_scale = "day" if scale == "daily" else "month"
                cov = coverage.get((sid, source_scale))
                if cov is None or cov["requests_complete"] != "True":
                    raise ValueError(f"Missing or incomplete coverage: {code} {scale}")
                relative = cov["derived_csv"]
                path = (root / relative).resolve()
                if root not in path.parents or not path.is_file() or digest(path) != cov["derived_sha256"]:
                    raise ValueError(f"Derived station CSV missing or SHA-mismatched: {relative}")
                shandle, swriter = writer(folder / (scale.capitalize() + ".csv"), output_fields)
                handles.append(shandle)
                seen = set()
                cells = Counter()
                for row in read_csv(path):
                    if row.get("station_code") != code or any(s.replace(":", "_") not in row for s in specs):
                        raise ValueError(f"Station or sensor column differs: {relative}")
                    period = row["date"]
                    try:
                        checked = date.fromisoformat(period if scale == "daily" else period + "-01")
                    except ValueError as exc:
                        raise ValueError(f"Invalid {scale} date: {code} {period}") from exc
                    if (len(period) != (10 if scale == "daily" else 7) or period in seen
                            or not config["start"][:10] <= checked.isoformat() <= config["end"][:10]):
                        raise ValueError(f"Duplicate, malformed or out-of-scope period: {code} {period}")
                    seen.add(period)
                    raw, kind = source.cell(row, sid, scale, specs)
                    n = decimal_value(raw)
                    value_status = ("missing_sensor_key" if kind == "absent" else
                                    "blank_or_null" if kind in ("null", "empty_string") else
                                    "nonnumeric_review" if n is None else
                                    "numeric_negative_review" if n < 0 else "numeric")
                    cells["returned_cells"] += kind != "absent"
                    cells["finite_numeric_cells"] += n is not None and kind != "absent"
                    cells["null_or_nonnumeric_cells"] += n is None and kind != "absent"
                    cells["negative_precipitation_cells"] += n is not None and n < 0
                    record = dict(collection="agrometeo", station_code=code, station_id=sid,
                        catalogue_station_name=meta["station_name"], catalogue_canton=meta["canton"],
                        catalogue_latitude=meta["latitude"], catalogue_longitude=meta["longitude"],
                        catalogue_elevation_m=meta["elevation_m"], catalogue_metadata_flags=meta["metadata_flags"],
                        metadata_scope="current_catalogue_not_historical_location", period=period,
                        precipitation_mm=plain(n), source_value_raw=row["6_sum"], value_status=value_status,
                        raw_cell_kind=kind, source_json=row["source_json"], source_sha256=row["source_sha256"],
                        source_row=row["source_row"], metadata_source_sha256=meta["metadata_source_sha256"],
                        sensor_spec="6:sum", measurement_filter="measured=1")
                    record.update({"source_" + s.replace(":", "_"):row[s.replace(":", "_")]
                                   for s in specs if s != "6:sum"})
                    swriter.writerow(record)
                    combined[scale].writerow(record)
                    station_stats[scale + "_rows"] += 1
                    counts[scale + "_rows"] += 1
                    statuses[value_status] += 1
                    if scale == "daily":
                        daily[period[:7]][checked.day] = n
                    else:
                        monthly[period] = n
                for field in ("returned_cells", "finite_numeric_cells", "null_or_nonnumeric_cells", "negative_precipitation_cells"):
                    if int(cov[field]) != cells[field]:
                        raise ValueError(f"Coverage count disagrees: {code} {scale} {field}")
                shandle.close()
                handles.remove(shandle)
            for row in monthly_audit(sid, daily, monthly):
                awriter.writerow(row)
                audit_counts[row["status"]] += 1
            lat, lon = decimal_value(meta["latitude"]), decimal_value(meta["longitude"])
            valid_location = lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180
            coordinate_conflict = bool(conflicts[sid] & {"lat_dec", "long_dec", "altitude"})
            location_status = ("invalid_current_catalogue_coordinates" if not valid_location else
                               "catalogue_location_conflict_review" if coordinate_conflict else
                               "current_catalogue_unverified_historical")
            register.append({**meta, "daily_rows":station_stats["daily_rows"],
                             "monthly_rows":station_stats["monthly_rows"],
                             "daily_csv":"stations/" + code + "/Daily.csv",
                             "monthly_csv":"stations/" + code + "/Monthly.csv",
                             "metadata_scope":"current_catalogue_not_historical_location",
                             "location_status":location_status})
            if valid_location:
                point_rows.append(register[-1])
            if i % 20 == 0 or i == len(selected):
                print(f"Verified {i}/{len(selected)} stations", flush=True)
        for handle in handles:
            handle.close()
        handles.clear()
        register_fields = list(register[0])
        rh, rw = writer(stage / "station_register.csv", register_fields)
        for r in register:
            rw.writerow(r)
        rh.close()
        ph, pw = writer(stage / "qgis_station_points.csv", register_fields)
        for r in point_rows:
            pw.writerow(r)
        ph.close()
        all_fields = list(next(iter(inventory.values()))) + ["selected_in_run", "metadata_scope"]
        ch, cw = writer(stage / "catalogue_all.csv", all_fields)
        for sid, meta in inventory.items():
            cw.writerow({**meta, "selected_in_run":"yes" if sid in selected else "no",
                         "metadata_scope":"current_catalogue_not_historical_location"})
        ch.close()
        for r in register:
            mh, mw = writer(stage / "stations" / r["station_code"] / "Metadata.csv", register_fields)
            mw.writerow(r)
            mh.close()
        result = dict(run_id=run_id, status="organized_source_cells_verified",
                      stations=len(selected), catalogue_stations=len(inventory),
                      daily_rows=counts["daily_rows"], monthly_rows=counts["monthly_rows"],
                      qgis_current_catalogue_points=len(point_rows),
                      location_status_counts=dict(Counter(r["location_status"] for r in register)),
                      value_status_counts=dict(statuses), monthly_reconciliation=dict(audit_counts),
                      raw_json_files_sha_verified=len(source.verified),
                      acquisition_inventory_sha256=digest(inventory_file),
                      sensor_spec="6:sum", preserved_sensor_specs=specs,
                      measurement_filter="measured=1",
                      scientific_qc_performed=False,
                      created_at_utc=datetime.now(timezone.utc).isoformat())
        (stage / "summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        stage.rename(out)
    except BaseException:
        for handle in handles:
            handle.close()
        shutil.rmtree(stage)
        raise
    print(f"Organized {len(selected)} stations, {counts['daily_rows']} daily and {counts['monthly_rows']} monthly rows: {out}")


if __name__ == "__main__":
    main()
