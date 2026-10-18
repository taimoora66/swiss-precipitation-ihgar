"""Offline API-only export checks with hourly and 10-minute source rows."""

import csv
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from xml.etree import ElementTree as ET
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import wsl_lwf_api_excel as api  # noqa: E402


def write_fixture(root):
    run = "sample_api"
    model = "isone_freiland_13"
    raw = root / "data/raw/wsl_lwf/live_api" / run
    report = root / "runs/wsl_lwf_live" / run
    folder = raw / "csv" / model
    folder.mkdir(parents=True)
    (raw / "metadata").mkdir()
    report.mkdir(parents=True)
    (raw / "models.json").write_text(json.dumps([model]), encoding="utf-8")
    metadata = {
        "precipitation_60": {"timestamp_iso_earliest": "2000-01-01T00:00:00Z",
                             "timestamp_iso_latest": "2000-01-31T23:00:00Z"},
        "precipitation_60_multi": {"timestamp_iso_earliest": "2000-02-01T00:00:00Z",
                                   "timestamp_iso_latest": "2000-02-29T23:00:00Z"},
        "precipitation_10": {"timestamp_iso_earliest": "2000-01-01T00:00:00Z",
                             "timestamp_iso_latest": "2000-02-29T23:50:00Z"},
    }
    md_file = raw / "metadata" / f"{model}.json"
    md_file.write_text(json.dumps(metadata), encoding="utf-8")
    md_sha = hashlib.sha256(md_file.read_bytes()).hexdigest()
    with (report / "coverage.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, ["model", "metadata_url", "metadata_sha256"])
        writer.writeheader()
        writer.writerow({"model": model, "metadata_url": "https://www.envidat.ch/data-api/lwf/metadata/example",
                         "metadata_sha256": md_sha})
    (report / "summary.json").write_text(json.dumps({
        "status": "raw_api_acquisition_complete_not_reconciled", "run_id": run,
        "selected_models": [model]}), encoding="utf-8")
    csv_file = folder / "2000.csv"
    start = datetime(2000, 1, 1, tzinfo=timezone.utc)
    stop = datetime(2000, 3, 1, tzinfo=timezone.utc)
    with csv_file.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(api.FIELDS)
        count = 0
        stamp = start
        while stamp < stop:
            ordinary = "1.0" if stamp.month == 1 else "NA"
            multi = "1.0" if stamp.month == 2 else "NA"
            if stamp == datetime(2000, 2, 2, 3, tzinfo=timezone.utc):
                ordinary = "2.0"  # simultaneous gauges: ambiguity, not an automatic merge
            writer.writerow((stamp.isoformat(), ordinary, multi, "0.0", "NA"))
            count += 1
            if stamp == start:
                writer.writerow(((stamp + timedelta(minutes=10)).isoformat(), "NA", "NA", "8.0", "NA"))
                count += 1  # a 10-minute value must never be added to the hourly sum
                writer.writerow(("2000-01-01T11:43:40+00:00", "NA", "NA", "0.5", "NA"))
                count += 1  # subminute 10-minute record must not affect the daily sum
                writer.writerow(("2000-02-03T11:00:40+00:00", "0.5", "NA", "NA", "NA"))
                count += 1  # anomalous hourly field invalidates this day's total
            stamp += timedelta(hours=1)
    file_bytes = csv_file.read_bytes()
    record = {
        "status": "downloaded", "model": model, "year": 2000,
        "request_start": "2000-01-01", "request_end_exclusive": "2000-03-01",
        "relative_path": csv_file.relative_to(root).as_posix(),
        "sha256": hashlib.sha256(file_bytes).hexdigest(), "bytes": len(file_bytes),
        "rows": count, "header": ",".join(api.FIELDS),
    }
    (report / "download_manifest.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    return run, csv_file


def cells(z, sheet):
    ns = {"m": api.NS}
    xml = ET.fromstring(z.read(f"xl/worksheets/sheet{sheet}.xml"))
    rows = []
    for row in xml.findall("m:sheetData/m:row", ns):
        result = {}
        for c in row.findall("m:c", ns):
            value = c.find("m:is/m:t", ns) if c.attrib.get("t") == "inlineStr" else c.find("m:v", ns)
            result[c.attrib["r"].rstrip("0123456789")] = value.text if value is not None else None
        rows.append(result)
    return rows


class ApiOnlyExportTests(unittest.TestCase):
    def test_complete_month_and_ambiguous_multi_are_separate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run, _ = write_fixture(root)
            self.assertEqual(api.main(["--root", str(root), "--run-id", run,
                                       "--expected-stations", "1"]), 0)
            result = json.loads((root / "working/wsl_lwf/api_only_excel/latest.json").read_text())
            self.assertEqual(result["station_count"], 1)
            self.assertEqual(result["hourly_daily_grid_rows"], 60)
            self.assertEqual(result["counts"]["complete_months"], 1)
            self.assertEqual(result["counts"]["off_hour_hourly_field_rows"], 1)
            with ZipFile(result["workbook"]) as z:
                metadata = cells(z, 1)
                hourly = cells(z, 2)
                grid = cells(z, 3)
            meta = dict(zip(api.META_HEADERS, [metadata[1].get(api.excel_col(i)) for i in range(1, len(api.META_HEADERS) + 1)]))
            self.assertIsNone(meta["elevation_m"])
            self.assertEqual(meta["instrument_status"], "not_supplied_by_acquired_API")
            headers = {grid[0].get(api.excel_col(i)): api.excel_col(i) for i in range(1, len(api.GRID_HEADERS) + 1)}
            january, february = grid[1:3]
            self.assertEqual(january[headers["computed_complete_month_total_mm"]], "744.0")
            self.assertIsNone(february.get(headers["computed_complete_month_total_mm"]))
            self.assertIsNone(january.get(headers["source_monthly_total_mm"]))
            self.assertIsNone(february.get(headers["D02"]))
            self.assertIsNone(february.get(headers["D03"]))
            hour_columns = {hourly[0].get(api.excel_col(i)): api.excel_col(i)
                            for i in range(1, len(api.HOUR_HEADERS) + 1)}
            first_day, ambiguous_day, offhour_day = hourly[1], hourly[33], hourly[34]
            self.assertEqual(first_day[hour_columns["H00"]], "1.0")
            self.assertEqual(first_day[hour_columns["H23"]], "1.0")
            self.assertEqual(first_day[hour_columns["computed_complete_day_total_mm"]], "24.0")
            self.assertIsNone(ambiguous_day.get(hour_columns["H03"]))
            self.assertIsNone(ambiguous_day.get(hour_columns["computed_complete_day_total_mm"]))
            self.assertEqual(offhour_day[hour_columns["H11"]], "1.0")
            self.assertIsNone(offhour_day.get(hour_columns["computed_complete_day_total_mm"]))

    def test_mutated_api_year_blocks_export(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            run, source = write_fixture(root)
            with source.open("a", encoding="utf-8") as stream:
                stream.write("2000-02-29T23:00:00+00:00,100,NA,NA,NA\n")
            with self.assertRaisesRegex(ValueError, "changed"):
                api.load_acquisition(root, run, 1)
            self.assertFalse((root / "working/wsl_lwf/api_only_excel").exists())


if __name__ == "__main__":
    unittest.main()
