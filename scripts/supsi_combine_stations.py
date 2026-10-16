#!/usr/bin/env python3
"""Combine validated SUPSI station CSVs and reconcile every exported field.

This makes separate all-station Daily and Monthly CSVs and a station register.
The original PDFs, extraction and station files are read only.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import sys
import uuid
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

COLLECTION = "supsi_ticino_yearbook_rain"
SITE_HISTORY_REVIEW = {"P_CAL", "P_GNO", "P_GRA", "P_MAG", "P_MEN", "P_NOVAZ_TOT"}


def read_rows(path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        yield from csv.DictReader(handle, delimiter=";")


def csv_writer(path, fields):
    handle = path.open("w", encoding="utf-8-sig", newline="")
    writer = csv.DictWriter(handle, fieldnames=fields, delimiter=";", extrasaction="raise")
    writer.writeheader()
    return handle, writer


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def check_date(row, kind):
    period = row["date" if kind == "Daily" else "month"]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}" if kind == "Daily" else r"\d{4}-\d{2}", period):
        raise ValueError(f"Invalid {kind} period: {period}")
    value = date.fromisoformat(period if kind == "Daily" else period + "-01")
    if str(value.year) != row["year"]:
        raise ValueError(f"Year disagrees with period: {period}")
    if kind == "Daily" and (str(value.month) != row["month"] or str(value.day) != row["day"]):
        raise ValueError(f"Daily month/day disagrees with period: {period}")
    return period


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--extraction-id", required=True)
    parser.add_argument("--acquisition-id", help="Defaults to extraction ID minus final suffix")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    run = args.extraction_id
    acquisition = args.acquisition_id or run.rsplit("_", 1)[0]
    if not re.fullmatch(r"[A-Za-z0-9_]+", run + acquisition):
        parser.error("Invalid run ID")
    station_root = root / "working/supsi/stations" / run
    source_summary = root / "runs/supsi_organization" / run / "summary.json"
    inventory_path = root / "runs/supsi" / acquisition / "station_inventory.csv"
    coverage_path = root / "runs/supsi_organization" / run / "coverage.csv"
    for path in (station_root, source_summary, inventory_path, coverage_path):
        if not path.exists():
            parser.error(f"Required input absent: {path}")
    source_report = json.loads(source_summary.read_text(encoding="utf-8"))
    if source_report.get("status") != "validated_transcription" or source_report.get("issues") != 0:
        parser.error("Station source package has not passed the PDF transcription audit")
    inventory_rows = list(read_rows(inventory_path))
    inventory = {r["station_id"]: r for r in inventory_rows}
    coverage_rows = list(read_rows(coverage_path))
    coverage = {r["station_id"]: r for r in coverage_rows}
    if len(inventory) != len(inventory_rows) or len(coverage) != len(coverage_rows):
        parser.error("Duplicate station IDs in inventory or coverage")
    ids = sorted(p.name for p in station_root.iterdir() if p.is_dir())
    if len(ids) != source_report["stations_in_extraction"] or set(ids) != set(coverage):
        parser.error("Station directories, coverage and source audit counts differ")
    if set(ids) - set(inventory):
        parser.error("A station is absent from the acquisition inventory")

    out = root / "working/supsi/combined" / run
    if out.exists():
        parser.error(f"Combined output already exists; existing reviewed files will not be overwritten: {out}")
    staging = root / "working/supsi/combined" / (".stage_" + uuid.uuid4().hex)
    staging.mkdir(parents=True)
    report = root / "runs/supsi_combined" / run
    report.mkdir(parents=True, exist_ok=True)
    issues = []
    counts = Counter()
    statuses = {"Daily": Counter(), "Monthly": Counter()}
    station_register = []
    all_catalogue = []
    expected_headers = {}
    writers = {}
    handles = []
    try:
        # Keep every catalogue entry, including four without downloaded yearbooks.
        for rec in inventory_rows:
            all_catalogue.append({**rec, "collection": COLLECTION,
                                  "archived_yearbook_in_this_run": "yes" if rec["station_id"] in ids else "no",
                                  "metadata_scope": "current catalogue; historical sites need separate review"})
        for sid in ids:
            meta_rows = list(read_rows(station_root / sid / "Metadata.csv"))
            if len(meta_rows) != 1:
                raise ValueError(f"Expected one Metadata row at {sid}")
            meta = meta_rows[0]
            for field, value in inventory[sid].items():
                if meta.get(field) != value:
                    raise ValueError(f"Station metadata differs from acquisition: {sid} {field}")
            cov = coverage[sid]
            if not meta["lv95_easting"] or not meta["lv95_northing"]:
                coordinate_status = "missing_current_catalogue_coordinates"
            elif sid in SITE_HISTORY_REVIEW:
                coordinate_status = "known_site_history_review"
            else:
                coordinate_status = "current_catalogue_only_historical_unverified"
            station_register.append({**meta,
                "collection": COLLECTION, "first_pdf_date": cov["first_date"],
                "last_pdf_date": cov["last_date"], "yearbooks": cov["yearbooks"],
                "daily_rows": cov["daily_rows"], "monthly_rows": cov["monthly_rows"],
                "daily_blank": cov["daily_blank"],
                "daily_dash_uninterpreted": cov["daily_dash_uninterpreted"],
                "monthly_printed_blank": cov["monthly_printed_blank"],
                "monthly_arithmetic_mismatch": cov["monthly_arithmetic_mismatch"],
                "coordinate_status": coordinate_status,
                "metadata_scope": "current catalogue; not historical coordinates",
                "inventory_sha256": file_hash(inventory_path)})
            for kind in ("Daily", "Monthly"):
                path = station_root / sid / (kind + ".csv")
                with path.open(encoding="utf-8-sig", newline="") as handle:
                    reader = csv.DictReader(handle, delimiter=";")
                    fields = reader.fieldnames
                    if not fields or "catalogue_lv95_elevation_m" not in fields:
                        raise ValueError(f"Metadata fields absent: {path}")
                    if kind not in expected_headers:
                        expected_headers[kind] = fields
                        writer_handle, writer = csv_writer(staging / (kind.lower() + ".csv"), fields)
                        handles.append(writer_handle)
                        writers[kind] = writer
                    elif fields != expected_headers[kind]:
                        raise ValueError(f"Header changed at {path}")
                    previous = ""
                    for row in reader:
                        period = check_date(row, kind)
                        if period <= previous:
                            raise ValueError(f"Duplicate or unsorted {kind} period: {sid} {period}")
                        previous = period
                        if row["station_id"] != sid or row["collection"] != COLLECTION:
                            raise ValueError(f"Station ID or collection differs: {sid} {period}")
                        for field in ("name", "wgs84_longitude", "wgs84_latitude",
                                      "lv95_easting", "lv95_northing", "lv95_elevation_m",
                                      "istsos_id", "metadata_flags"):
                            if row["catalogue_" + field] != meta[field]:
                                raise ValueError(f"Joined metadata differs: {sid} {period} {field}")
                        if not re.fullmatch(r"[0-9a-f]{64}", row["source_pdf_sha256"]):
                            raise ValueError(f"Source PDF checksum absent: {sid} {period}")
                        writers[kind].writerow(row)
                        counts[kind] += 1
                        statuses[kind][row["cell_status"]] += 1
            if int(cov["daily_rows"]) != counts["Daily"] - sum(int(x["daily_rows"]) for x in station_register[:-1]):
                raise ValueError(f"Daily count differs from coverage: {sid}")
            if int(cov["monthly_rows"]) != counts["Monthly"] - sum(int(x["monthly_rows"]) for x in station_register[:-1]):
                raise ValueError(f"Monthly count differs from coverage: {sid}")
        for handle in handles:
            handle.close()
        handles.clear()
        if counts["Daily"] != source_report["source_daily_cells"] or counts["Monthly"] != source_report["source_monthly_cells"]:
            raise ValueError("Combined row counts differ from the PDF cell audit")
        for filename, records in (("stations.csv", station_register), ("catalogue_all.csv", all_catalogue)):
            handle, writer = csv_writer(staging / filename, list(records[0]))
            with handle:
                writer.writerows(records)

        # Independently read each combined row and compare every value to the
        # corresponding station file. This detects export formatting or truncation.
        audited_fields = Counter()
        for kind in ("Daily", "Monthly"):
            combined = iter(read_rows(staging / (kind.lower() + ".csv")))
            for sid in ids:
                for original in read_rows(station_root / sid / (kind + ".csv")):
                    exported = next(combined, None)
                    if exported != original:
                        raise ValueError(f"Combined {kind} row changed: {sid} {original.get('date', original.get('month'))}")
                    audited_fields[kind] += len(original)
            if next(combined, None) is not None:
                raise ValueError(f"Extra rows in combined {kind} export")
        result = {"extraction_id": run, "acquisition_id": acquisition,
                  "stations_with_yearbooks": len(ids), "stations_in_catalogue": len(inventory),
                  "daily_rows": counts["Daily"], "monthly_rows": counts["Monthly"],
                  "daily_cells_by_status": dict(statuses["Daily"]),
                  "monthly_cells_by_status": dict(statuses["Monthly"]),
                  "fields_compared_after_export": dict(audited_fields),
                  "stations_without_current_coordinates": [r["station_id"] for r in station_register
                      if r["coordinate_status"] == "missing_current_catalogue_coordinates"],
                  "known_site_history_review": sorted(SITE_HISTORY_REVIEW & set(ids)),
                  "issues": 0, "status": "combined_verified",
                  "scientific_qc_performed": False,
                  "inventory_sha256": file_hash(inventory_path),
                  "created_at_utc": datetime.now(timezone.utc).isoformat()}
        staging.rename(out)
        (report / "summary.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(f"Combined {len(ids)} data stations: {counts['Daily']} daily and "
              f"{counts['Monthly']} monthly rows; output {out}; report {report}")
        return 0
    except Exception as exc:
        issues.append(str(exc))
        (report / "summary.json").write_text(json.dumps({"status": "blocked", "issues": issues}, indent=2) + "\n")
        print(f"BLOCKED: {exc}; report {report}", file=sys.stderr)
        return 1
    finally:
        for handle in handles:
            handle.close()
        shutil.rmtree(staging, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
