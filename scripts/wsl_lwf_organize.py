#!/usr/bin/env python3
"""Organize source-verified WSL LWF hourly snapshots without rainfall aggregation.

Python 3.10+, standard library only. Daily/monthly outputs count *observations*,
not precipitation totals. Source timestamps remain naive and unchanged.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import shutil
import sys
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


def sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def source_path(root, relative, expected):
    path = (root / relative).resolve()
    if root not in path.parents or not path.is_file() or sha256(path) != expected:
        raise ValueError(f"Missing, unsafe or SHA-mismatched source: {relative}")
    return path


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f, delimiter=";")
        if not reader.fieldnames or len(reader.fieldnames) != len(set(reader.fieldnames)):
            raise ValueError(f"Missing or duplicated CSV columns: {path}")
        yield from reader


def writer(path, columns):
    f = path.open("w", encoding="utf-8-sig", newline="")
    w = csv.DictWriter(f, fieldnames=columns, delimiter=";", extrasaction="raise")
    w.writeheader()
    return f, w


def precipitation(raw):
    raw = raw.strip()
    if raw.upper() in ("", "NA", "NAN", "NULL"):
        return "missing", ""
    try:
        number = float(raw)
        if math.isfinite(number):
            return ("negative_review" if number < 0 else "numeric"), raw
    except ValueError:
        pass
    return "invalid_review", ""


OBS = ["station_key", "station_code", "timestamp_source", "source_date", "source_month",
       "precipitation_numeric", "precipitation_status", "duplicate_timestamp_review",
       "out_of_order_review", "source_row"]
COUNT = ["station_key", "station_code", "station_exposure", "period",
         "source_rows", "unique_timestamp_labels", "numeric_precipitation_rows",
         "missing_precipitation_rows", "invalid_precipitation_rows",
         "negative_precipitation_rows", "duplicate_timestamp_rows",
         "out_of_order_rows", "summary_scope"]


def location(spatial):
    try:
        obj = json.loads(spatial)
        lon, lat = obj["coordinates"]
        if (obj.get("type") == "Point" and type(lon) in (int, float)
                and type(lat) in (int, float) and math.isfinite(lon)
                and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90):
            return str(lon), str(lat), "dataset_point_shared_by_resources_not_station_verified"
    except (ValueError, TypeError, KeyError):
        pass
    return "", "", "missing_or_invalid_dataset_point"


def checked_run(root, run):
    summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    if (summary.get("status") not in ("completed_public_LWF_scope", "completed_selected_scope")
            or summary.get("failures") != 0 or not summary.get("station_csvs_scanned")):
        raise ValueError("WSL acquisition is not complete for its selected scope")
    inventory = list(read_csv(run / "station_inventory.csv"))
    if len(inventory) != summary["station_csvs_scanned"] or len(inventory) != len({r["resource_id"] for r in inventory}):
        raise ValueError("Station inventory count or resource IDs disagree")
    manifest = json.loads((run / "resource_manifest.json").read_text(encoding="utf-8"))
    resources = {r["resource_id"]: r for r in manifest if r.get("resource_format", "").upper() == "CSV"}
    if len(resources) != len(inventory):
        raise ValueError("Manifest station resource count differs")
    snapshots = json.loads((run / "catalogue_snapshots.json").read_text(encoding="utf-8"))
    packages = {}
    for snap in snapshots:
        path = source_path(root, snap["local_path"], snap["sha256"])
        if "package_show?" not in snap["url"]:
            continue
        package = json.loads(path.read_text(encoding="utf-8-sig"))["result"]
        packages[package["name"]] = package
    if len(packages) != summary["public_datasets"]:
        raise ValueError("Package metadata snapshots incomplete")
    for r in inventory:
        m = resources.get(r["resource_id"])
        package = packages.get(r["dataset"])
        if (m is None or package is None or m["local_path"] != r["local_path"]
                or m["sha256"] != r["sha256"] or m["station_code"] != r["station_code"]
                or package.get("spatial", "") != r["dataset_spatial"]
                or package.get("doi", "") != r["doi"]
                or package.get("license_id", "") != r["license_id"]
                or not any(x["id"] == r["resource_id"] and x.get("name", "") == r["resource_name"]
                           for x in package["resources"])):
            raise ValueError(f"Station metadata or manifest mismatch: {r['resource_id']}")
    return summary, inventory, len(snapshots)


def write_count_rows(output, station, values, scope):
    for period, c in sorted(values.items()):
        output.writerow({"station_key": station["station_key"],
            "station_code": station["station_code"], "station_exposure": station["station_exposure"],
            "period": period, "source_rows": c["rows"], "unique_timestamp_labels": c["unique"],
            "numeric_precipitation_rows": c["numeric"], "missing_precipitation_rows": c["missing"],
            "invalid_precipitation_rows": c["invalid"], "negative_precipitation_rows": c["negative"],
            "duplicate_timestamp_rows": c["duplicate"], "out_of_order_rows": c["out_of_order"],
            "summary_scope": scope})


def organize(root, run):
    summary, inventory, snapshots = checked_run(root, run)
    run_id = run.name
    if not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", run_id):
        raise ValueError(f"Invalid acquisition run folder: {run_id}")
    out = root / "working/wsl_lwf/organized" / run_id
    if out.exists():
        raise ValueError(f"Output exists; refusing to overwrite: {out}")
    out.parent.mkdir(parents=True, exist_ok=True)
    stage = out.parent / (".stage_" + uuid.uuid4().hex)
    stage.mkdir()
    handles = []
    try:
        # Source schemas can differ across stations. Scan only headers first so
        # combined and per-station files have exactly the same source columns.
        sources, sensor_fields = [], []
        for r in inventory:
            path = source_path(root, r["local_path"], r["sha256"])
            with path.open(encoding="utf-8-sig", newline="") as f:
                fields = csv.reader(f, delimiter=";").__next__()
            if len(fields) != len(set(fields)) or "timestamp" not in fields or "precip" not in fields:
                raise ValueError(f"Invalid source header: {r['local_path']}")
            for field in fields:
                if field not in sensor_fields:
                    sensor_fields.append(field)
            sources.append((r, path, fields))
        obs_fields = OBS + ["raw_" + name for name in sensor_fields]
        combined_handle, combined = writer(stage / "hourly.csv", obs_fields)
        daily_handle, daily_writer = writer(stage / "daily_observation_counts.csv", COUNT)
        monthly_handle, monthly_writer = writer(stage / "monthly_observation_counts.csv", COUNT)
        handles.extend([combined_handle, daily_handle, monthly_handle])
        register, totals, points = [], Counter(), []
        for index, (r, path, fields) in enumerate(sources, 1):
            code = r["station_code"] or "UNKNOWN"
            station_key = "WSL_LWF_" + code + "_" + r["resource_id"][:8]
            lon, lat, loc_status = location(r["dataset_spatial"])
            station = {**r, "collection": "wsl_lwf", "station_key": station_key,
                "dataset_longitude": lon, "dataset_latitude": lat,
                "location_scope": loc_status, "elevation_m": "",
                "timestamp_timezone": "unverified_naive_source_timestamp",
                "precipitation_interval": "unverified_no_daily_aggregation",
                "source_columns": ",".join(fields),
                "hourly_csv": f"stations/{station_key}/Hourly.csv"}
            folder = stage / "stations" / station_key
            folder.mkdir(parents=True)
            station_handle, station_writer = writer(folder / "Hourly.csv", obs_fields)
            handles.append(station_handle)
            days, months = defaultdict(Counter), defaultdict(Counter)
            stats, seen = Counter(), set()
            first = last = previous = None
            for rownum, raw in enumerate(read_csv(path), 1):
                if None in raw or None in raw.values() or set(raw) != set(fields):
                    raise ValueError(f"Malformed source row: {r['local_path']} #{rownum}")
                stamp = raw["timestamp"]
                try:
                    parsed = datetime.fromisoformat(stamp)
                    if parsed.tzinfo is not None:
                        raise ValueError("Unexpected timezone-aware source timestamp")
                except (ValueError, TypeError) as exc:
                    raise ValueError(f"Invalid source timestamp: {r['local_path']} #{rownum}") from exc
                if first is None:
                    first = stamp
                last = stamp
                duplicate = stamp in seen
                out_of_order = previous is not None and stamp <= previous
                seen.add(stamp)
                previous = stamp
                status, numeric = precipitation(raw["precip"] or "")
                day, month = parsed.date().isoformat(), parsed.strftime("%Y-%m")
                observation = {"station_key": station_key,
                    "station_code": code, "timestamp_source": stamp,
                    "source_date": day, "source_month": month,
                    "precipitation_numeric": numeric, "precipitation_status": status,
                    "duplicate_timestamp_review": "yes" if duplicate else "no",
                    "out_of_order_review": "yes" if out_of_order else "no",
                    "source_row": rownum}
                observation.update({"raw_" + name: value for name, value in raw.items()})
                combined.writerow(observation)
                station_writer.writerow(observation)
                stats["rows"] += 1
                stats["duplicate"] += duplicate
                stats["out_of_order"] += out_of_order
                for period_counts in (days[day], months[month]):
                    period_counts["rows"] += 1
                    period_counts["unique"] += not duplicate
                    period_counts["duplicate"] += duplicate
                    period_counts["out_of_order"] += out_of_order
                    period_counts["negative"] += status == "negative_review"
                    period_counts["numeric"] += status in ("numeric", "negative_review")
                    period_counts["missing"] += status == "missing"
                    period_counts["invalid"] += status == "invalid_review"
                stats[status] += 1
            station_handle.close()
            handles.remove(station_handle)
            expected = {"rows": "rows", "numeric": "precipitation_numeric",
                        "missing": "precipitation_missing", "invalid_review": "other_precipitation",
                        "negative_review": "negative_precipitation",
                        "out_of_order": "duplicate_or_nonmonotonic_timestamps"}
            for actual, source_column in expected.items():
                if stats[actual] != int(r[source_column]):
                    raise ValueError(f"Source inventory disagrees: {station_key} {source_column}")
            if first != r["first_timestamp"] or last != r["last_timestamp"]:
                raise ValueError(f"Source inventory dates disagree: {station_key}")
            write_count_rows(daily_writer, station, days, "observed_rows_no_daily_rainfall_total")
            write_count_rows(monthly_writer, station, months, "observed_rows_no_monthly_rainfall_total")
            station.update(source_rows=stats["rows"], numeric_precipitation_rows=stats["numeric"] + stats["negative_review"],
                           missing_precipitation_rows=stats["missing"],
                           invalid_precipitation_rows=stats["invalid_review"],
                           negative_precipitation_rows=stats["negative_review"],
                           duplicate_timestamp_rows=stats["duplicate"], out_of_order_rows=stats["out_of_order"],
                           daily_periods_with_rows=len(days), monthly_periods_with_rows=len(months))
            register.append(station)
            if lon and lat:
                points.append(station)
            for name in ("rows", "numeric", "missing", "invalid_review", "negative_review", "duplicate", "out_of_order"):
                totals[name] += stats[name]
            if index % 5 == 0 or index == len(sources):
                print(f"Verified {index}/{len(sources)} station files", flush=True)
        for h in handles:
            h.close()
        handles.clear()
        columns = list(register[0])
        for filename, rows in (("station_register.csv", register), ("qgis_dataset_points.csv", points)):
            h, w = writer(stage / filename, columns)
            w.writerows(rows)
            h.close()
        for station in register:
            h, w = writer(stage / "stations" / station["station_key"] / "Metadata.csv", columns)
            w.writerow(station)
            h.close()
        result = {"acquisition_run": run_id, "acquisition_status": summary["status"],
            "status": "organized_source_rows_verified_no_rainfall_aggregation",
            "station_files": len(register), "dataset_points": len(points),
            "hourly_rows": totals["rows"], "numeric_precipitation_rows": totals["numeric"] + totals["negative_review"],
            "missing_precipitation_rows": totals["missing"],
            "invalid_precipitation_rows": totals["invalid_review"],
            "negative_precipitation_rows": totals["negative_review"],
            "duplicate_timestamp_rows": totals["duplicate"],
            "out_of_order_rows": totals["out_of_order"],
            "daily_periods_with_rows": sum(r["daily_periods_with_rows"] for r in register),
            "monthly_periods_with_rows": sum(r["monthly_periods_with_rows"] for r in register),
            "raw_station_csv_sha_verified": len(sources), "metadata_snapshots_sha_verified": snapshots,
            "acquisition_inventory_sha256": sha256(run / "station_inventory.csv"),
            "timestamp_timezone_verified": False, "precipitation_interval_verified": False,
            "station_coordinates_verified": False, "scientific_qc_performed": False,
            "created_at_utc": datetime.now(timezone.utc).isoformat()}
        (stage / "summary.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        stage.rename(out)
    except BaseException:
        for h in handles:
            h.close()
        shutil.rmtree(stage)
        raise
    return out, result


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument("--run-id", required=True)
    args = p.parse_args(argv)
    root = args.root.resolve()
    if not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", args.run_id):
        p.error("Expected UTC acquisition run ID YYYYMMDDTHHMMSSZ")
    run = root / "runs/wsl_lwf" / args.run_id
    try:
        out, result = organize(root, run)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"Organized {result['station_files']} station files and {result['hourly_rows']} hourly source rows: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
