#!/usr/bin/env python3
"""Build an hourly, daily and monthly API-only WSL LWF precipitation workbook.

Reads only the saved EnviDat LWF API acquisition (metadata and yearly CSVs)
and its hashed download manifest. It never reads the 2016 archive, PANGAEA
files, or any earlier WSL workbook. Daily/monthly amounts are provisional:
each valid 60-minute value is integrated over an assumed one-hour interval.
"""

import argparse
from collections import Counter, defaultdict
import csv
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import uuid
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile


FIELDS = ("timestamp_iso", "precipitation_60", "precipitation_60_multi",
          "precipitation_10", "precipitation_10_multi")
HOURLY = FIELDS[1:3]
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
NA = {"", "NA", "N/A", "NULL", "NONE"}
API = "https://www.envidat.ch/data-api/lwf"
MAIN_FILE = "WSL_LWF_API_Only_Selected_All.xlsx"
NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def require(ok, message):
    if not ok:
        raise ValueError(message)


def sha(path):
    h = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def iso_utc(raw):
    try:
        value = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Unparseable API timestamp {raw!r}") from exc
    require(value.tzinfo is not None and value.utcoffset() == timedelta(0),
            f"API timestamp is not explicitly UTC: {raw!r}")
    return value


def number(raw):
    if raw.strip().upper() in NA:
        return "missing", None
    try:
        n = Decimal(raw.strip())
    except InvalidOperation:
        return "invalid", None
    if not n.is_finite():
        return "invalid", None
    if n < 0:
        return "negative", None
    return "numeric", n


def decimal_text(n):
    return None if n is None else format(n, "f")


def months_between(first, last):
    year, month = first.year, first.month
    while (year, month) <= (last.year, last.month):
        stop = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
        yield year, month, (stop - date(year, month, 1)).days
        year, month = stop.year, stop.month


def first_last(metadata):
    stamps = [metadata.get(f, {}).get(f"timestamp_iso_{side}")
              for f in HOURLY for side in ("earliest", "latest")]
    stamps = [iso_utc(s) for s in stamps if s]
    require(stamps, "API metadata contains no hourly precipitation timestamps")
    return min(stamps).date(), max(stamps).date()


def metadata_timestamp(metadata, field, side):
    value = metadata.get(field, {}).get(f"timestamp_iso_{side}")
    if value:
        iso_utc(value)
    return value or ""


def load_acquisition(root, run_id, expected):
    report = root / "runs" / "wsl_lwf_live" / run_id
    raw = root / "data" / "raw" / "wsl_lwf" / "live_api" / run_id
    summary_file = report / "summary.json"
    manifest_file = report / "download_manifest.jsonl"
    coverage_file = report / "coverage.csv"
    require(all(p.is_file() for p in (summary_file, manifest_file, coverage_file, raw / "models.json")),
            f"Incomplete saved API acquisition: {report}")
    summary = json.loads(summary_file.read_text(encoding="utf-8-sig"))
    require(summary.get("run_id") == run_id and
            summary.get("status") == "raw_api_acquisition_complete_not_reconciled",
            "Select the completed raw API acquisition, not an archive-based run")
    models = summary.get("selected_models")
    require(isinstance(models, list) and len(models) == expected and len(set(models)) == expected,
            f"Expected {expected} distinct API models in acquisition summary")
    listed = json.loads((raw / "models.json").read_text(encoding="utf-8-sig"))
    require(isinstance(listed, list) and set(models) <= set(listed),
            "Saved API station list disagrees with selected models")
    with coverage_file.open("r", encoding="utf-8-sig", newline="") as source:
        coverage_rows = list(csv.DictReader(source))
    coverage = {r["model"]: r for r in coverage_rows}
    require(len(coverage_rows) == len(coverage), "Repeated API coverage metadata row")
    require(set(coverage) == set(models), "API coverage and selected models differ")
    metadata = {}
    for model in models:
        require(re.fullmatch(r"[a-z0-9_]+", model), f"Unsafe model name: {model!r}")
        file = raw / "metadata" / f"{model}.json"
        require(file.is_file() and sha(file) == coverage[model]["metadata_sha256"],
                f"API metadata missing or hash changed: {model}")
        metadata[model] = json.loads(file.read_text(encoding="utf-8-sig"))
    entries = {}
    for line in manifest_file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        model, year = item["model"], int(item["year"])
        key = (model, year)
        require(model in models and key not in entries, f"Unexpected/repeated API year: {key}")
        require(item["status"] in ("downloaded", "empty_response"), f"Incomplete request: {key}")
        expected_path = (Path("data/raw/wsl_lwf/live_api") / run_id / "csv" /
                         model / f"{year}.csv")
        require(item["relative_path"].replace("\\", "/") == expected_path.as_posix(),
                f"Unexpected source path for {key}")
        path = root / expected_path
        require(path.is_file() and sha(path) == item["sha256"] and
                path.stat().st_size == int(item["bytes"]),
                f"API year file missing or changed: {key}")
        entries[key] = item
    require(entries, "No downloaded API precipitation CSVs")
    actual_files = {p.relative_to(root).as_posix() for model in models
                    for p in (raw / "csv" / model).glob("*.csv")}
    require(actual_files == {e["relative_path"].replace("\\", "/") for e in entries.values()},
            "Unmanifested or missing API year CSV")
    for model in models:
        first, last = first_last(metadata[model])
        years = {y for m, y in entries if m == model}
        require(years == set(range(first.year, last.year + 1)),
                f"API year requests do not span metadata's full hourly period: {model}")
    return report, raw, summary, coverage, metadata, entries


def process_year(root, model, item, days, totals):
    file = root / item["relative_path"]
    year = int(item["year"])
    request_start = date.fromisoformat(item["request_start"])
    request_end = date.fromisoformat(item["request_end_exclusive"])
    require(request_start < request_end and request_start.year == year,
            f"Bad request boundaries: {model} {year}")
    count = 0
    first_numeric = last_numeric = None
    with file.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        require(tuple(reader.fieldnames or ()) == FIELDS, f"Unexpected API CSV header: {file}")
        for row in reader:
            count += 1
            require(None not in row and all(row[f] is not None for f in FIELDS),
                    f"Malformed API row in {file} line {count + 1}")
            stamp = iso_utc(row["timestamp_iso"])
            require(request_start <= stamp.date() < request_end,
                    f"API row outside request range: {model} {stamp}")
            values = tuple(number(row[f]) for f in HOURLY)
            if any(status == "numeric" for status, _ in values):
                first_numeric = min(first_numeric, stamp) if first_numeric else stamp
                last_numeric = max(last_numeric, stamp) if last_numeric else stamp
            if stamp.minute == 0 and stamp.second == 0 and stamp.microsecond == 0:
                days[stamp.date()][stamp.hour].append(values)
                totals["hourly_clock_rows"] += 1
            elif any(status != "missing" for status, _ in values):
                days[stamp.date()]["off_hour"].append(values)
                totals["off_hour_hourly_field_rows"] += 1
    require(count == int(item["rows"]), f"API row count differs from manifest: {file}")
    require((count > 0) == (item["status"] == "downloaded"),
            f"API file status disagrees with row count: {file}")
    totals["api_csv_rows"] += count
    return first_numeric, last_numeric


def assess_day(hours):
    values = []
    hourly_values = [None] * 24
    chosen = Counter()
    missing = duplicate = ambiguous = invalid = 0
    off_hour = len(hours.get("off_hour", []))
    for hour in range(24):
        occurrences = hours.get(hour, [])
        if not occurrences:
            missing += 1
            continue
        if len(occurrences) != 1:
            duplicate += len(occurrences)
            continue
        (state_a, value_a), (state_b, value_b) = occurrences[0]
        if "invalid" in (state_a, state_b) or "negative" in (state_a, state_b):
            invalid += 1
        elif state_a == state_b == "numeric":
            ambiguous += 1
        elif state_a == state_b == "missing":
            missing += 1
        elif state_a == "numeric":
            values.append(value_a)
            hourly_values[hour] = value_a
            chosen[HOURLY[0]] += 1
        elif state_b == "numeric":
            values.append(value_b)
            hourly_values[hour] = value_b
            chosen[HOURLY[1]] += 1
        else:
            invalid += 1
    complete = len(values) == 24 and not (duplicate or ambiguous or invalid or off_hour)
    return {"complete": complete, "amount": sum(values, Decimal(0)) if complete else None,
            "hour_values": hourly_values,
            "partial": sum(values, Decimal(0)) if values else None, "valid_hours": len(values),
            "clock_rows": sum(len(v) for k, v in hours.items() if k != "off_hour"),
            "missing_hours": missing, "duplicate_rows": duplicate,
            "ambiguous_hours": ambiguous, "invalid_hours": invalid,
            "off_hour_rows": off_hour, "fields": chosen}


META_HEADERS = (
    "station_id", "station_name", "provider", "station_exposure_inferred_from_id",
    "owner", "owner_status", "longitude", "latitude", "elevation_m",
    "coordinate_elevation_status", "instrument", "instrument_status",
    "api_model", "timestamp_timezone", "time_window_status", "measurement_resolution",
    "hourly_primary_field", "hourly_multi_field", "hourly_integration_assumption",
    "source_monthly_total_status", "hourly_metadata_first_utc", "hourly_metadata_last_utc",
    "first_numeric_hourly_utc", "last_numeric_hourly_utc", "source_year_files",
    "api_csv_rows", "hourly_clock_rows", "complete_provisional_days",
    "complete_provisional_months", "api_metadata_url", "api_documentation_url",
    "metadata_sha256", "download_manifest_sha256", "api_run_id", "scientific_qc_performed",
    "hourly_ordinary_first_utc", "hourly_ordinary_last_utc",
    "hourly_multi_first_utc", "hourly_multi_last_utc",
    "ten_min_ordinary_first_utc", "ten_min_ordinary_last_utc",
    "ten_min_multi_first_utc", "ten_min_multi_last_utc",
)
GRID_HEADERS = (
    "station_id", "station_name", "provider", "year", "month_number", "month",
    "period_utc", "calendar_days", *(f"D{d:02}" for d in range(1, 32)),
    "complete_days", "days_with_hourly_rows", "valid_hourly_slots", "clock_hour_rows",
    "missing_hourly_slots", "duplicate_hour_rows", "ambiguous_both_fields_hours",
    "invalid_or_negative_hours", "off_hour_hourly_rows", "ordinary_hour_values",
    "multi_hour_values", "source_monthly_total_mm", "source_monthly_total_status",
    "computed_complete_month_total_mm", "sum_of_complete_days_mm",
    "partial_valid_hour_amount_mm", "aggregation_status", "incomplete_day_details",
    "source_year_csv", "source_year_sha256", "assumed_time_basis", "scientific_qc_performed",
)
HOUR_HEADERS = (
    "station_id", "station_name", "provider", "date_utc", "year", "month_number",
    "day_of_month", *(f"H{h:02d}" for h in range(24)),
    "valid_hourly_slots", "clock_hour_rows", "missing_hourly_slots",
    "duplicate_hour_rows", "ambiguous_both_fields_hours", "invalid_or_negative_hours",
    "off_hour_hourly_rows", "ordinary_hour_values", "multi_hour_values",
    "computed_complete_day_total_mm", "partial_valid_hour_amount_mm",
    "aggregation_status", "source_year_csv", "source_year_sha256",
    "assumed_time_basis", "scientific_qc_performed",
)
HOUR_NUMERIC = {
    "year", "month_number", "day_of_month", *(f"H{h:02d}" for h in range(24)),
    "valid_hourly_slots", "clock_hour_rows", "missing_hourly_slots",
    "duplicate_hour_rows", "ambiguous_both_fields_hours", "invalid_or_negative_hours",
    "off_hour_hourly_rows", "ordinary_hour_values", "multi_hour_values",
    "computed_complete_day_total_mm", "partial_valid_hour_amount_mm",
}
META_NUMERIC = {"source_year_files", "api_csv_rows", "hourly_clock_rows",
                "complete_provisional_days", "complete_provisional_months"}
GRID_NUMERIC = {"year", "month_number", "calendar_days", *(f"D{d:02}" for d in range(1, 32)),
                "complete_days", "days_with_hourly_rows", "valid_hourly_slots", "clock_hour_rows",
                "missing_hourly_slots", "duplicate_hour_rows", "ambiguous_both_fields_hours",
                "invalid_or_negative_hours", "off_hour_hourly_rows", "ordinary_hour_values",
                "multi_hour_values", "source_monthly_total_mm", "computed_complete_month_total_mm",
                "sum_of_complete_days_mm", "partial_valid_hour_amount_mm"}


def make_tables(root, run_id, report, coverage, metadata, entries, hourly_sink):
    meta_rows, grid_rows = [], []
    manifest_hash = sha(report / "download_manifest.jsonl")
    all_counts = Counter()
    for model in sorted(metadata):
        info = metadata[model]
        first, last = first_last(info)
        days = defaultdict(lambda: defaultdict(list))
        counts = Counter()
        starts, ends = [], []
        files = {y: e for (m, y), e in entries.items() if m == model}
        for year in sorted(files):
            a, b = process_year(root, model, files[year], days, counts)
            if a:
                starts.append(a)
                ends.append(b)
        require(starts, f"No numeric hourly precipitation for {model}")
        day_values = {d: assess_day(h) for d, h in days.items()}
        exposure = ("forest" if "_bestand_" in model else
                    "open_field" if "_freiland_" in model else "unresolved")
        name = model.replace("_bestand_", " forest ").replace("_freiland_", " open field ").rsplit(" ", 1)[0].replace("_", " ").title()
        completed_days = completed_months = 0
        for year, month, n_days in months_between(first, last):
            source = files[year]
            stats = Counter()
            daily = []
            details = []
            partial = Decimal(0)
            partial_available = False
            sum_complete = Decimal(0)
            n_complete = 0
            n_with_rows = 0
            for day_num in range(1, n_days + 1):
                d = date(year, month, day_num)
                result = day_values.get(d) or assess_day({})
                hourly_row = (
                    model, name, "WSL / LWF EnviDat API", d.isoformat(), year, month,
                    day_num, *(decimal_text(v) for v in result["hour_values"]),
                    result["valid_hours"], result["clock_rows"], result["missing_hours"],
                    result["duplicate_rows"], result["ambiguous_hours"], result["invalid_hours"],
                    result["off_hour_rows"], result["fields"][HOURLY[0]],
                    result["fields"][HOURLY[1]], decimal_text(result["amount"]),
                    decimal_text(result["partial"]),
                    "complete_UTC_source_label_provisional" if result["complete"] else "incomplete_or_ambiguous",
                    source["relative_path"].replace("\\", "/"), source["sha256"],
                    "UTC_source_date_one_hour_assumed", "false",
                )
                require(len(hourly_row) == len(HOUR_HEADERS), "Incorrect hourly grid width")
                hourly_sink.write(json.dumps(hourly_row, ensure_ascii=False) + "\n")
                all_counts["hourly_daily_grid_rows"] += 1
                if result["clock_rows"]:
                    n_with_rows += 1
                if result["complete"]:
                    n_complete += 1
                    completed_days += 1
                    sum_complete += result["amount"]
                else:
                    details.append(f"{day_num:02d}:missing={result['missing_hours']},dup={result['duplicate_rows']},both={result['ambiguous_hours']},invalid={result['invalid_hours']},offhour={result['off_hour_rows']}")
                daily.append(result["amount"])
                for field in ("valid_hours", "clock_rows", "missing_hours", "duplicate_rows",
                              "ambiguous_hours", "invalid_hours", "off_hour_rows"):
                    stats[field] += result[field]
                stats["ordinary"] += result["fields"][HOURLY[0]]
                stats["multi"] += result["fields"][HOURLY[1]]
                if result["partial"] is not None:
                    partial_available = True
                    partial += result["partial"]
            complete_month = n_complete == n_days
            completed_months += complete_month
            monthly_total = sum_complete if complete_month else None
            daily.extend([None] * (31 - n_days))
            grid_rows.append((
                model, name, "WSL / LWF EnviDat API", year, month, MONTHS[month - 1],
                f"{year:04d}-{month:02d}", n_days, *(decimal_text(d) for d in daily),
                n_complete, n_with_rows, stats["valid_hours"], stats["clock_rows"],
                stats["missing_hours"], stats["duplicate_rows"], stats["ambiguous_hours"],
                stats["invalid_hours"], stats["off_hour_rows"], stats["ordinary"], stats["multi"],
                None, "not_supplied_by_acquired_API", decimal_text(monthly_total),
                decimal_text(sum_complete) if n_complete else None,
                decimal_text(partial) if partial_available else None,
                "complete_UTC_source_label_provisional" if complete_month else "incomplete_or_ambiguous",
                " | ".join(details), source["relative_path"].replace("\\", "/"),
                source["sha256"], "UTC_source_date_one_hour_assumed", "false",
            ))
        all_counts.update(counts)
        all_counts["complete_days"] += completed_days
        all_counts["complete_months"] += completed_months
        md = {
            "station_id": model, "station_name": name, "provider": "WSL / LWF EnviDat API",
            "station_exposure_inferred_from_id": exposure,
            "owner": None, "owner_status": "not_supplied_by_acquired_API",
            "longitude": None, "latitude": None, "elevation_m": None,
            "coordinate_elevation_status": "not_supplied_by_acquired_API",
            "instrument": None, "instrument_status": "not_supplied_by_acquired_API",
            "api_model": model, "timestamp_timezone": "UTC_explicit_API_timestamp_offset",
            "time_window_status": "hourly_observation_window_not_verified",
            "measurement_resolution": "60_minute_field_names; 10_minute_fields_preserved_in_raw_CSV_only",
            "hourly_primary_field": HOURLY[0], "hourly_multi_field": HOURLY[1],
            "hourly_integration_assumption": "hourly_value_mm_per_hour_times_one_hour_provisional",
            "source_monthly_total_status": "not_supplied_by_acquired_API",
            "hourly_metadata_first_utc": first.isoformat(), "hourly_metadata_last_utc": last.isoformat(),
            "first_numeric_hourly_utc": min(starts).isoformat(),
            "last_numeric_hourly_utc": max(ends).isoformat(),
            "source_year_files": len(files), "api_csv_rows": counts["api_csv_rows"],
            "hourly_clock_rows": counts["hourly_clock_rows"],
            "complete_provisional_days": completed_days,
            "complete_provisional_months": completed_months,
            "api_metadata_url": coverage[model]["metadata_url"],
            "api_documentation_url": API + "/", "metadata_sha256": coverage[model]["metadata_sha256"],
            "download_manifest_sha256": manifest_hash, "api_run_id": run_id,
            "scientific_qc_performed": "false",
        }
        for field, prefix in (("precipitation_60", "hourly_ordinary"),
                              ("precipitation_60_multi", "hourly_multi"),
                              ("precipitation_10", "ten_min_ordinary"),
                              ("precipitation_10_multi", "ten_min_multi")):
            md[prefix + "_first_utc"] = metadata_timestamp(info, field, "earliest")
            md[prefix + "_last_utc"] = metadata_timestamp(info, field, "latest")
        meta_rows.append(tuple(md[h] for h in META_HEADERS))
        print(f"{model}: {len(files)} API years; {completed_days} complete UTC days; "
              f"{completed_months} complete months", flush=True)
    require(len(meta_rows) < 1048576 and len(grid_rows) < 1048576 and
            all_counts["hourly_daily_grid_rows"] < 1048576,
            "Excel worksheet row limit exceeded")
    return meta_rows, grid_rows, all_counts


def excel_col(n):
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def append_cell(rowxml, col, rownum, value, digest, numeric):
    if value is None or value == "":
        return
    address = excel_col(col) + str(rownum)
    if numeric:
        n = Decimal(str(value))
        require(n.is_finite(), f"Non-finite numeric Excel value at {address}")
        kind, data = "n", decimal_text(n)
        rowxml.write(f'<c r="{address}" s="2"><v>{escape(data)}</v></c>')
    else:
        kind, data = "s", str(value)
        rowxml.write(f'<c r="{address}" t="inlineStr" s="1"><is><t xml:space="preserve">{escape(data)}</t></is></c>')
    digest.update(json.dumps((address, kind, data), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    digest.update(b"\n")


def sheet_xml(headers, rows, freeze_col, numeric_headers):
    """Return OOXML bytes and digest of every populated saved cell."""
    import io
    out = io.StringIO()
    h = hashlib.sha256()
    max_row, max_col = len(rows) + 1, len(headers)
    out.write('<?xml version="1.0" encoding="utf-8"?>')
    out.write(f'<worksheet xmlns="{NS}"><dimension ref="A1:{excel_col(max_col)}{max_row}"/>')
    out.write(f'<sheetViews><sheetView workbookViewId="0"><pane xSplit="{freeze_col - 1}" ySplit="1" topLeftCell="{excel_col(freeze_col)}2" activePane="bottomRight" state="frozen"/></sheetView></sheetViews>')
    out.write('<sheetFormatPr defaultRowHeight="16"/>')
    out.write(f'<cols><col min="1" max="{min(freeze_col - 1, max_col)}" width="21" customWidth="1"/><col min="{freeze_col}" max="{max_col}" width="15" customWidth="1"/></cols>')
    out.write('<sheetData>')
    for row_number, values in enumerate((headers, *rows), 1):
        require(len(values) == max_col, f"Wrong number of cells at Excel row {row_number}")
        out.write(f'<row r="{row_number}"' + (' s="3" customFormat="1"' if row_number == 1 else '') + '>')
        for c, value in enumerate(values, 1):
            if row_number == 1:
                address = excel_col(c) + str(row_number)
                data = str(value)
                out.write(f'<c r="{address}" t="inlineStr" s="3"><is><t>{escape(data)}</t></is></c>')
                h.update(json.dumps((address, "s", data), ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
            else:
                append_cell(out, c, row_number, value, h, headers[c - 1] in numeric_headers)
        out.write('</row>')
    out.write('</sheetData>')
    out.write(f'<autoFilter ref="A1:{excel_col(max_col)}{max_row}"/>')
    out.write('</worksheet>')
    return out.getvalue().encode("utf-8"), h.hexdigest()


def hourly_rows_from_jsonl(path):
    with path.open("r", encoding="utf-8") as source:
        for line in source:
            yield json.loads(line)


def stream_hourly_sheet(target, path, expected_rows):
    """Write the large daily grid without holding its XML in memory."""
    headers = HOUR_HEADERS
    h = hashlib.sha256()
    max_col = len(headers)
    max_row = expected_rows + 1
    prefix = ('<?xml version="1.0" encoding="utf-8"?>'
              f'<worksheet xmlns="{NS}"><dimension ref="A1:{excel_col(max_col)}{max_row}"/>'
              '<sheetViews><sheetView workbookViewId="0"><pane xSplit="7" ySplit="1" '
              'topLeftCell="H2" activePane="bottomRight" state="frozen"/>'
              '</sheetView></sheetViews><sheetFormatPr defaultRowHeight="16"/>'
              f'<cols><col min="1" max="7" width="21" customWidth="1"/>'
              f'<col min="8" max="{max_col}" width="15" customWidth="1"/></cols><sheetData>')
    target.write(prefix.encode("utf-8"))
    seen = 0
    def write_row(row_number, values):
        require(len(values) == max_col, f"Wrong number of cells at hourly row {row_number}")
        row = io.StringIO()
        row.write(f'<row r="{row_number}"' + (' s="3" customFormat="1"' if row_number == 1 else '') + '>')
        for c, value in enumerate(values, 1):
            if row_number == 1:
                address = excel_col(c) + "1"
                data = str(value)
                row.write(f'<c r="{address}" t="inlineStr" s="3"><is><t>{escape(data)}</t></is></c>')
                h.update(json.dumps((address, "s", data), ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
            else:
                append_cell(row, c, row_number, value, h, headers[c - 1] in HOUR_NUMERIC)
        row.write('</row>')
        target.write(row.getvalue().encode("utf-8"))
    write_row(1, headers)
    for seen, values in enumerate(hourly_rows_from_jsonl(path), 1):
        require(seen <= expected_rows, "Hourly source row count exceeded prepared count")
        write_row(seen + 1, values)
    require(seen == expected_rows, "Hourly source row count changed during write")
    target.write((f'</sheetData><autoFilter ref="A1:{excel_col(max_col)}{max_row}"/>'
                  '</worksheet>').encode("utf-8"))
    return h.hexdigest()


def write_workbook(path, meta_rows, hourly_jsonl, hourly_count, grid_rows):
    sheets = (("Station_metadata", META_HEADERS, meta_rows, 4, META_NUMERIC),
              ("Hourly_daily_grid", HOUR_HEADERS, hourly_jsonl, 8, HOUR_NUMERIC),
              ("Daily_month_grid", GRID_HEADERS, grid_rows, 9, GRID_NUMERIC))
    verification = {}
    tmp = path.with_suffix(".xlsx.part")
    with ZipFile(tmp, "w", compression=ZIP_DEFLATED, compresslevel=6) as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="utf-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/worksheets/sheet3.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/></Types>')
        z.writestr("_rels/.rels", '<?xml version="1.0" encoding="utf-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr("xl/workbook.xml", '<?xml version="1.0" encoding="utf-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Station_metadata" sheetId="1" r:id="rId1"/><sheet name="Hourly_daily_grid" sheetId="2" r:id="rId2"/><sheet name="Daily_month_grid" sheetId="3" r:id="rId3"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", '<?xml version="1.0" encoding="utf-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/><Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet3.xml"/><Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
        z.writestr("xl/styles.xml", '<?xml version="1.0" encoding="utf-8"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="2"><font><sz val="10"/><name val="Aptos"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="10"/><name val="Aptos"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF24466B"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="4"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFill="1" applyFont="1"/></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')
        for index, (name, headers, rows, freeze, numeric_headers) in enumerate(sheets, 1):
            if index == 2:
                with z.open(f"xl/worksheets/sheet{index}.xml", "w", force_zip64=True) as target:
                    digest = stream_hourly_sheet(target, hourly_jsonl, hourly_count)
                row_count = hourly_count
            else:
                content, digest = sheet_xml(headers, rows, freeze, numeric_headers)
                z.writestr(f"xl/worksheets/sheet{index}.xml", content)
                row_count = len(rows)
            verification[name] = {"rows_including_header": row_count + 1,
                                  "populated_cells_sha256": digest}
    with ZipFile(tmp) as z:
        require(z.testzip() is None, "Saved XLSX ZIP CRC failed")
        for index, (name, headers, rows, _, _) in enumerate(sheets, 1):
            h = hashlib.sha256()
            count = 0
            with z.open(f"xl/worksheets/sheet{index}.xml") as xml:
                events = ET.iterparse(xml, events=("start", "end"))
                _, root_element = next(events)
                for event, element in events:
                    if event != "end":
                        continue
                    if element.tag == f"{{{NS}}}c":
                        address = element.attrib["r"]
                        if element.attrib.get("t") == "inlineStr":
                            kind = "s"
                            data = element.findtext(f"{{{NS}}}is/{{{NS}}}t") or ""
                        else:
                            kind = "n"
                            data = element.findtext(f"{{{NS}}}v")
                        h.update(json.dumps((address, kind, data), ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n")
                        element.clear()
                    elif element.tag == f"{{{NS}}}row":
                        count += 1
                        element.clear()
                        root_element.clear()
            require(count == verification[name]["rows_including_header"] and
                    h.hexdigest() == verification[name]["populated_cells_sha256"],
                    f"Saved XLSX cell mismatch in {name}")
    os.replace(tmp, path)
    return verification


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--run-id", default="20261017T122441Z")
    parser.add_argument("--expected-stations", type=int, default=13)
    args = parser.parse_args(argv)
    require(re.fullmatch(r"[A-Za-z0-9_-]+", args.run_id) and args.expected_stations > 0,
            "Unsafe run ID or expected station count")
    root = args.root.resolve()
    report, raw, summary, coverage, metadata, entries = load_acquisition(
        root, args.run_id, args.expected_stations)
    with tempfile.TemporaryDirectory(prefix="wsl_api_hourly_") as temporary:
        hourly_jsonl = Path(temporary) / "hourly_daily_rows.jsonl"
        with hourly_jsonl.open("w", encoding="utf-8") as hourly_sink:
            meta_rows, grid_rows, counts = make_tables(
                root, args.run_id, report, coverage, metadata, entries, hourly_sink)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + uuid.uuid4().hex[:8]
        output = root / "working" / "wsl_lwf" / "api_only_excel" / args.run_id / stamp
        output.mkdir(parents=True, exist_ok=False)
        workbook = output / MAIN_FILE
        verification = write_workbook(
            workbook, meta_rows, hourly_jsonl, counts["hourly_daily_grid_rows"], grid_rows)
    result = {
        "status": "completed_api_only_provisional_source_label_sums",
        "api_run_id": args.run_id, "input_scope": "saved_api_metadata_and_yearly_csv_only",
        "station_count": len(meta_rows), "source_year_files": len(entries),
        "hourly_daily_grid_rows": counts["hourly_daily_grid_rows"],
        "monthly_grid_rows": len(grid_rows), "counts": dict(counts),
        "source_monthly_totals_available": False,
        "longitude_latitude_elevation_in_API": False,
        "instrument_and_owner_in_API": False,
        "hourly_source_fields": list(HOURLY),
        "ten_minute_fields_used_in_sums": False,
        "one_hour_integration_assumed": True,
        "timestamp_basis": "original UTC API date labels",
        "observation_window_verified": False,
        "scientific_qc_performed": False,
        "sheets": verification,
        "workbook": str(workbook), "workbook_sha256": sha(workbook),
        "manifest_sha256": sha(report / "download_manifest.jsonl"),
        "summary_sha256": sha(report / "summary.json"),
        "output_directory": str(output),
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    (output / "summary.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    pointer = root / "working" / "wsl_lwf" / "api_only_excel" / "latest.json"
    temporary = pointer.with_suffix(".json.part")
    temporary.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, pointer)
    print(f"Completed API-only export: {len(meta_rows)} models, "
          f"{counts['hourly_daily_grid_rows']} days, {len(grid_rows)} months: {workbook}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"API-only export stopped: {exc}", file=sys.stderr)
        sys.exit(1)
