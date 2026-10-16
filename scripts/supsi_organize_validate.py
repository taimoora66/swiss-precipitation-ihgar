#!/usr/bin/env python3
"""Validate SUPSI extraction against archived PDFs, then organize by station.

Requires pdfplumber and the adjacent supsi_extract.py. The source PDFs and the
extracted daily/monthly CSVs are read only. This is transcription QC, not
scientific approval of the precipitation observations.
"""
from __future__ import annotations

import argparse
import calendar
import csv
import json
import re
import shutil
import sys
import uuid
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import supsi_extract as source

COLLECTION = "supsi_ticino_yearbook_rain"
META_FIELDS = ["name", "published", "published_period_raw", "catchment_raw",
               "sensor_type", "istsos_id", "assigned_id", "wgs84_longitude",
               "wgs84_latitude", "lv95_easting", "lv95_northing",
               "lv95_elevation_m", "metadata_flags", "publication_footer",
               "station_sheet_url"]
ADDED = ["collection", "metadata_scope"] + ["catalogue_" + x for x in META_FIELDS]
KEY_FIELDS = {"daily": "date", "monthly": "month"}
KEY_PATTERN = {"daily": re.compile(r"^\d{4}-\d{2}-\d{2}$"),
               "monthly": re.compile(r"^\d{4}-\d{2}$")}
MONTHLY_CHECKS = ("printed_total_raw", "printed_total_mm", "symbol", "cell_status",
                  "sum_numeric_daily_mm", "numeric_days", "dash_days", "blank_days",
                  "difference_mm", "arithmetic_status")


def read_csv(path: Path):
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle, delimiter=";"))


def write_csv(path: Path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter=";", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def safe_pdf(root, relative, station, year, digest):
    rel = Path(relative.replace("\\", "/"))
    if rel.is_absolute() or ".." in rel.parts or rel.parts[:3] != ("data", "raw", "supsi"):
        raise ValueError(f"Unsafe PDF path for {station} {year}: {relative}")
    if rel.name != f"{station}_{year}.pdf" or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError(f"Invalid source filename or SHA256 for {station} {year}")
    path = root / rel
    if source.fingerprint(path) != digest:
        raise ValueError(f"Archived PDF SHA256 mismatch: {relative}")
    return path


def validate_key(row, kind):
    key = row[KEY_FIELDS[kind]]
    if not KEY_PATTERN[kind].fullmatch(key):
        raise ValueError(f"Invalid {kind} date: {key!r}")
    parsed = date.fromisoformat(key + "-01" if kind == "monthly" else key)
    if parsed.year != int(row["year"]) or (kind == "daily" and
            (parsed.month != int(row["month"]) or parsed.day != int(row["day"]))):
        raise ValueError(f"Date/year/month/day disagree: {row['station_id']} {key}")
    return key


def index_rows(path, kind, metadata, issues):
    rows = read_csv(path)
    expected_columns = source.DAILY_COLUMNS if kind == "daily" else source.MONTHLY_COLUMNS
    if not rows or any(field not in rows[0] for field in expected_columns):
        raise ValueError(f"Missing expected columns or no rows: {path}")
    by_key = {}
    for row_no, row in enumerate(rows, 2):
        sid = row["station_id"]
        try:
            if sid not in metadata:
                raise ValueError(f"station absent from inventory: {sid}")
            key = validate_key(row, kind)
            identity = (sid, key)
            if identity in by_key:
                raise ValueError(f"duplicate {kind} key: {sid} {key}")
            by_key[identity] = row
        except Exception as exc:
            issues.append(dict(kind="input_invalid", station_id=sid, period=row.get(KEY_FIELDS[kind], ""),
                               field=kind, expected="valid unique key", actual=str(exc), row_number=row_no))
    return rows, by_key


def compare(expected, actual, station, period, kind, issues):
    columns = source.DAILY_COLUMNS if kind == "daily" else source.MONTHLY_COLUMNS
    if actual is None:
        issues.append(dict(kind="missing_extracted_cell", station_id=station, period=period,
                           field=kind, expected="present in PDF", actual=""))
        return
    for field in columns:
        want = str(expected[field])
        got = actual[field]
        # The only allowed representation variance is slash direction in the source path.
        if field == "source_pdf_path":
            got = got.replace("\\", "/")
        if want != got:
            issues.append(dict(kind="source_cell_mismatch", station_id=station, period=period,
                               field=field, expected=want, actual=got))


def aggregate_month(rows):
    count = Counter(r["cell_status"] for r in rows)
    numeric = sum((Decimal(r["value_mm"]) for r in rows if r["cell_status"] == "numeric"), Decimal(0))
    return numeric, count


def validate_month(daily, monthly, issues):
    sid, period = monthly["station_id"], monthly["month"]
    year, month = map(int, period.split("-"))
    if len(daily) != calendar.monthrange(year, month)[1]:
        issues.append(dict(kind="missing_daily_date", station_id=sid, period=period, field="count",
                           expected=calendar.monthrange(year, month)[1], actual=len(daily)))
        return
    try:
        numeric, count = aggregate_month(daily)
        actuals = {"sum_numeric_daily_mm": numeric, "numeric_days": count["numeric"],
                   "dash_days": count["dash_uninterpreted"], "blank_days": count["blank"]}
        for field, computed in actuals.items():
            recorded = Decimal(monthly[field]) if field == "sum_numeric_daily_mm" else int(monthly[field])
            if computed != recorded:
                issues.append(dict(kind="monthly_calculation_mismatch", station_id=sid,
                                   period=period, field=field, expected=str(computed), actual=monthly[field]))
        if monthly["cell_status"] == "numeric":
            difference = numeric - Decimal(monthly["printed_total_mm"])
            if difference != Decimal(monthly["difference_mm"]):
                issues.append(dict(kind="monthly_calculation_mismatch", station_id=sid, period=period,
                                   field="difference_mm", expected=str(difference), actual=monthly["difference_mm"]))
    except (ValueError, InvalidOperation, KeyError) as exc:
        issues.append(dict(kind="monthly_calculation_invalid", station_id=sid, period=period,
                           field="monthly", expected="numeric fields consistent", actual=str(exc)))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--extraction-id", required=True)
    parser.add_argument("--acquisition-id", help="Defaults to extraction ID without the final suffix")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    extraction_id = args.extraction_id
    acquisition_id = args.acquisition_id or extraction_id.rsplit("_", 1)[0]
    if not re.fullmatch(r"[A-Za-z0-9_]+", extraction_id + acquisition_id):
        parser.error("Run IDs must contain only letters, digits, underscores")
    derived = root / "data/derived/supsi" / extraction_id
    extraction_run = root / "runs/supsi_extraction" / extraction_id
    acquisition_run = root / "runs/supsi" / acquisition_id
    needed = [derived / "daily.csv", derived / "monthly.csv", extraction_run / "pdf_review.csv",
              acquisition_run / "station_inventory.csv"]
    for path in needed:
        if not path.is_file():
            parser.error(f"Required input missing: {path}")
    inventory = read_csv(needed[3])
    metadata = {r["station_id"]: r for r in inventory}
    if len(metadata) != len(inventory):
        parser.error("Duplicate station IDs in inventory")
    review = read_csv(needed[2])
    if len({(r["station_id"], r["year"]) for r in review}) != len(review):
        parser.error("Duplicate station/year in PDF review")
    issues = []
    daily, daily_map = index_rows(needed[0], "daily", metadata, issues)
    monthly, monthly_map = index_rows(needed[1], "monthly", metadata, issues)
    source_cells = {"daily": 0, "monthly": 0}
    expected_daily, expected_monthly = set(), set()
    for no, rec in enumerate(review, 1):
        sid, year = rec["station_id"], rec["year"]
        if rec["status"] not in ("review", "transcribed"):
            issues.append(dict(kind="failed_yearbook", station_id=sid, period=year, field="status",
                               expected="review or transcribed", actual=rec["status"]))
            continue
        try:
            pdf = safe_pdf(root, rec["source_pdf_path"], sid, year, rec["source_pdf_sha256"])
            pdf_daily, pdf_monthly, again = source.extract_one(pdf, sid, int(year),
                rec["source_pdf_path"].replace("\\", "/"), rec["source_pdf_sha256"])
            for field in source.REVIEW_COLUMNS:
                actual = rec[field].replace("\\", "/") if field == "source_pdf_path" else rec[field]
                if str(again[field]) != actual:
                    issues.append(dict(kind="review_mismatch", station_id=sid, period=year,
                                       field=field, expected=str(again[field]), actual=actual))
            for row in pdf_daily:
                key = (sid, row["date"])
                expected_daily.add(key)
                compare(row, daily_map.get(key), sid, row["date"], "daily", issues)
            for row in pdf_monthly:
                key = (sid, row["month"])
                expected_monthly.add(key)
                compare(row, monthly_map.get(key), sid, row["month"], "monthly", issues)
            source_cells["daily"] += len(pdf_daily)
            source_cells["monthly"] += len(pdf_monthly)
        except Exception as exc:
            issues.append(dict(kind="source_pdf_invalid", station_id=sid, period=year,
                               field="pdf", expected="available and identical", actual=str(exc)))
        if no % 100 == 0:
            print(f"Validated {no}/{len(review)} PDFs", flush=True)
    for kind, mapping, expected in (("daily", daily_map, expected_daily),
                                    ("monthly", monthly_map, expected_monthly)):
        for sid, period in mapping.keys() - expected:
            issues.append(dict(kind="unexpected_extracted_cell", station_id=sid, period=period,
                               field=kind, expected="absent", actual="present"))
    month_days = defaultdict(list)
    for row in daily:
        month_days[(row["station_id"], row["date"][:7])].append(row)
    for row in monthly:
        validate_month(month_days[(row["station_id"], row["month"])], row, issues)

    report_dir = root / "runs/supsi_organization" / extraction_id
    report_dir.mkdir(parents=True, exist_ok=True)
    stations = sorted({r["station_id"] for r in daily})
    coverage = []
    for sid in stations:
        years = sorted({int(r["year"]) for r in review if r["station_id"] == sid
                        and r["status"] in ("review", "transcribed")})
        dr = [r for r in daily if r["station_id"] == sid]
        mr = [r for r in monthly if r["station_id"] == sid]
        coverage.append(dict(station_id=sid, name=metadata[sid]["name"],
            first_pdf_year=min(years), last_pdf_year=max(years), yearbooks=len(years),
            years_absent_between=f"{'|'.join(str(y) for y in range(min(years), max(years)+1) if y not in years)}",
            first_date=min(r["date"] for r in dr), last_date=max(r["date"] for r in dr),
            daily_rows=len(dr), daily_numeric=sum(r["cell_status"] == "numeric" for r in dr),
            daily_dash_uninterpreted=sum(r["cell_status"] == "dash_uninterpreted" for r in dr),
            daily_blank=sum(r["cell_status"] == "blank" for r in dr),
            monthly_rows=len(mr), monthly_printed_blank=sum(r["cell_status"] == "blank" for r in mr),
            monthly_arithmetic_mismatch=sum(r["arithmetic_status"] == "mismatch_review" for r in mr),
            catalogue_metadata_flags=metadata[sid].get("metadata_flags", ""),
            current_coordinates_not_historical="yes"))
    write_csv(report_dir / "coverage.csv", coverage, list(coverage[0]))
    issue_fields = ["kind", "station_id", "period", "field", "expected", "actual", "row_number"]
    write_csv(report_dir / "cell_issues.csv", issues, issue_fields)
    summary = {"extraction_id": extraction_id, "acquisition_id": acquisition_id,
               "source_yearbooks": len(review), "source_daily_cells": source_cells["daily"],
               "source_monthly_cells": source_cells["monthly"],
               "extracted_daily_rows": len(daily), "extracted_monthly_rows": len(monthly),
               "stations_in_extraction": len({r["station_id"] for r in daily}),
               "issues": len(issues), "issue_counts": dict(Counter(i["kind"] for i in issues)),
               "daily_cell_status": dict(Counter(r["cell_status"] for r in daily)),
               "source_monthly_status": dict(Counter(r["arithmetic_status"] for r in monthly)),
               "status": "validated_transcription" if not issues else "blocked_source_mismatch",
               "scientific_qc_performed": False,
               "created_at_utc": datetime.now(timezone.utc).isoformat()}
    if issues:
        (report_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        print(f"BLOCKED: {len(issues)} issues. See {report_dir}", file=sys.stderr)
        return 1

    output = root / "working/supsi/stations" / extraction_id
    staging = root / "working/supsi/stations" / (".stations_" + uuid.uuid4().hex)
    grouped = {"daily": defaultdict(list), "monthly": defaultdict(list)}
    for kind, rows in (("daily", daily), ("monthly", monthly)):
        for row in rows:
            sid = row["station_id"]
            meta = metadata[sid]
            extra = {"collection": COLLECTION,
                     "metadata_scope": "current_catalogue; historical site may differ"}
            extra.update({"catalogue_" + field: meta.get(field, "") for field in META_FIELDS})
            grouped[kind][sid].append({**row, **extra})
    try:
        for sid in sorted({r["station_id"] for r in daily}):
            target = staging / sid
            target.mkdir(parents=True)
            station_meta = {"collection": COLLECTION, "metadata_scope": "current_catalogue; historical site may differ",
                            **metadata[sid]}
            write_csv(target / "Metadata.csv", [station_meta], list(station_meta))
            for kind in ("daily", "monthly"):
                rows = grouped[kind][sid]
                rows.sort(key=lambda r: r[KEY_FIELDS[kind]])
                fields = (source.DAILY_COLUMNS if kind == "daily" else source.MONTHLY_COLUMNS) + ADDED
                write_csv(target / (kind.capitalize() + ".csv"), rows, fields)
        if output.exists():
            # Never silently replace a previously reviewed station package.
            raise FileExistsError(f"Station output exists; archive or remove it first: {output}")
        staging.rename(output)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    summary["station_output"] = str(output)
    summary["report_output"] = str(report_dir)
    (report_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"Validated {len(daily)} daily and {len(monthly)} monthly cells in "
          f"{len(review)} PDFs. Station files: {output}. Report: {report_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
