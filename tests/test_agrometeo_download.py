"""Acquisition failure, provenance and missing-value tests; no live API calls."""
import contextlib
import csv
import io
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import agrometeo_download as ag


RAIN = {"id": 6, "name": {"en": "Precipitation"}, "unit": {"en": "mm"},
        "default_agg": "sum", "aggregations": [{"agg": "sum"}, {"agg": "cumsum"}]}
STATION = {"id": 8, "name": "CHALAIS", "lat_dec": "46.2599388", "long_dec": "7.5011492",
           "altitude": 610, "active": True, "into_service_at": "0025-11-07",
           "sensors": [RAIN], "tags": [{"name": "VS"}]}
DAILY = {"data": [{"date": "2003-01-01 00:00:00", "8_6_sum": "0.4"},
                  {"date": "2003-01-02 00:00:00", "8_6_sum": None},
                  {"date": "2003-01-03 00:00:00", "8_6_sum": "0.0"}]}


def task(scale="day"):
    return dict(station_ids=[8], scale=scale, sensors=["6:sum"],
                **{"from": "2003-01-01", "to": "2003-12-31"})


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.requests.append(self.path)
        path = urlsplit(self.path).path
        query = parse_qs(urlsplit(self.path).query)
        status = 200
        if path == "/stations":
            payload = {"data": [STATION]}
        elif path == "/meteo/stations":
            payload = {"data": [{**STATION, "name": "DIFFERENT_NAME"}]}
        elif path == "/sensors":
            payload = {"data": [RAIN]}
        else:
            if query.get("scale") == ["month"]:
                status = 503 if self.server.fail_month else 200
                payload = {"data": [{"date": "2003-01", "8_6_sum": "0.4"}]}
            else:
                payload = DAILY
        b = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *_):
        pass


class StructuralTests(unittest.TestCase):
    def test_null_blank_sentinel_and_actual_zero_remain_distinct(self):
        self.assertIsNone(ag.number(None))
        self.assertIsNone(ag.number(""))
        self.assertIsNone(ag.number("NA"))
        self.assertIsNone(ag.number("NaN"))
        self.assertIsNone(ag.number(True))
        self.assertEqual(ag.number("0.0"), 0)
        self.assertEqual(ag.number("-999"), -999)  # Retain, do not certify a sentinel.

    def test_wrong_schema_and_new_pagination_are_rejected(self):
        for obj in ({"error": "unavailable"}, {"data": [], "links": {"next": "/next"}},
                    {"data": [], "meta": {"total": 2}},
                    {"data": [], "meta": {"last_page": 2}}):
            with self.subTest(obj=obj), self.assertRaises(ag.AcquisitionError):
                ag.data_list(obj)
        self.assertEqual(ag.data_list({"data": []}), [])

    def test_duplicate_dates_wrong_station_and_timestamp_are_rejected(self):
        for obj in ({"data": DAILY["data"] * 2},
                    {"data": [{"date": "2003-01-01 00:00:00", "9_6_sum": "1"}]},
                    {"data": [{"date": "2003-01-01 01:00:00", "8_6_sum": "1"}]}):
            with self.subTest(obj=obj), self.assertRaises(ag.AcquisitionError):
                ag.valid_payload(obj, task())

    def test_monthly_boundaries_and_leap_years(self):
        windows = list(ag.period_windows(date(2000, 1, 15), date(2000, 3, 15), 5, "month"))
        self.assertEqual(windows, [(date(2000, 2, 1), date(2000, 2, 29))])
        self.assertEqual(list(ag.period_windows(date(2000, 1, 2), date(2000, 1, 20), 5, "month")), [])
        self.assertEqual(list(ag.period_windows(date(1999, 1, 1), date(2004, 1, 1), 5, "day")),
                         [(date(1999, 1, 1), date(2003, 12, 31)), (date(2004, 1, 1), date(2004, 1, 1))])

    def test_out_of_range_raw_rows_excluded_and_overlap_rejected(self):
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE observations (station,scale,stamp,values_json,source,digest,source_row, "
                   "PRIMARY KEY(station,scale,stamp))")
        obj = {"data": DAILY["data"] + [{"date": "2004-01-01 00:00:00", "8_6_sum": "1"}]}
        record = {"local_path": "data/raw/test.json", "sha256": "test"}
        self.assertEqual(ag.extract(db, obj, task(), record), 1)
        self.assertEqual(db.execute("SELECT count(*) FROM observations").fetchone()[0], 3)
        with self.assertRaises(ag.AcquisitionError):
            ag.extract(db, obj, task(), record)
        db.close()

    def test_all_sensors_does_not_request_cumulative_products(self):
        args = SimpleNamespace(all_sensors=True)
        self.assertEqual(ag.specs_for(args, {8: STATION}, {6: RAIN}), ["6:sum"])
        changed = {**RAIN, "default_agg": "avg"}
        with self.assertRaises(ag.AcquisitionError):
            ag.specs_for(args, {8: STATION}, {6: changed})


class LocalAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.requests = []
        self.server.fail_month = False
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        original_client = ag.HTTPClient
        self.client_patch = patch.object(ag, "HTTPClient", side_effect=lambda events, timeout, retries, host:
            original_client(events, timeout, retries, host="127.0.0.1"))
        self.client_patch.start()
        self.base_patch = patch.object(ag, "API_BASE", self.base)
        self.base_patch.start()
        self.sleep_patch = patch.object(ag.time, "sleep", return_value=None)
        self.sleep_patch.start()

    def tearDown(self):
        self.sleep_patch.stop()
        self.base_patch.stop()
        self.client_patch.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def run_fixture(self, *extra):
        args = ag.parser().parse_args(["--output", str(self.root), "--start", "2003-01-01",
            "--end", "2003-12-31", "--timeout", "3", "--retries", "0", *extra])
        with contextlib.redirect_stdout(io.StringIO()):
            code = ag.run(args)
        folder = max((self.root / "runs/agrometeo").iterdir(), key=lambda p: p.stat().st_mtime_ns)
        return code, folder, json.loads((folder / "summary.json").read_text())

    def test_end_to_end_provenance_missing_values_and_catalogue_conflicts(self):
        code, folder, summary = self.run_fixture()
        self.assertEqual(code, 0)
        self.assertEqual(summary["status"], "completed_requested_public_scope")
        self.assertEqual(summary["planned_requests"], 2)
        self.assertEqual(len(self.server.requests), 5)
        requests = [p for p in self.server.requests if p.startswith("/meteo/data")]
        self.assertTrue(all(parse_qs(urlsplit(p).query)["measured"] == ["1"] for p in requests))
        with (folder / "station_inventory.csv").open(encoding="utf-8-sig", newline="") as f:
            inventory = next(csv.DictReader(f, delimiter=";"))
        self.assertIn("implausible_service_date", inventory["metadata_flags"])
        self.assertIn("catalogue_disagreement", inventory["metadata_flags"])
        self.assertEqual(inventory["metadata_service_start"], "0025-11-07")
        csv_path = Path(summary["derived_folder"]) / "daily/AGROMETEO_8.csv"
        with csv_path.open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f, delimiter=";"))
        self.assertEqual([r["6_sum"] for r in rows], ["0.4", "", "0.0"])
        self.assertEqual(len(rows), 3)  # No fabricated missing dates.
        raw_path = self.root / rows[0]["source_json"]
        self.assertEqual(ag.sha256(raw_path), rows[0]["source_sha256"])
        self.assertEqual(json.loads(raw_path.read_text()), DAILY)
        with (folder / "station_coverage.csv").open(encoding="utf-8-sig", newline="") as f:
            daily = next(r for r in csv.DictReader(f, delimiter=";") if r["scale"] == "day")
        self.assertEqual(daily["finite_numeric_cells"], "2")
        self.assertEqual(daily["target_without_numeric_value"], "")
        self.assertEqual(daily["scientific_qc_status"], "not_assessed")

    def test_resume_hash_verification_corruption_recovery_and_refresh(self):
        self.run_fixture()
        initial_count = len(self.server.requests)
        code, _, summary = self.run_fixture()
        self.assertEqual(code, 0)
        self.assertEqual(summary["request_status_counts"], {"verified_local_cache": 2})
        self.assertEqual(len(self.server.requests) - initial_count, 3)  # Metadata always refreshed.
        first_raw = next((self.root / "data/raw/agrometeo/daily").rglob("*.json"))
        first_raw.write_bytes(b"corrupt")
        code, _, summary = self.run_fixture()
        self.assertEqual(code, 0)
        self.assertEqual(summary["request_status_counts"], {"downloaded": 1, "verified_local_cache": 1})
        self.assertTrue(list(first_raw.parent.glob("*.corrupt-*")))
        code, _, summary = self.run_fixture("--refresh")
        self.assertEqual(code, 0)
        self.assertEqual(summary["request_status_counts"], {"downloaded": 2})

    def test_failed_requests_do_not_report_success_or_complete_coverage(self):
        self.server.fail_month = True
        code, folder, summary = self.run_fixture()
        self.assertEqual(code, 1)
        self.assertEqual(summary["status"], "failed_or_incomplete")
        self.assertEqual(summary["failed_requests"], 1)
        with (folder / "station_coverage.csv").open(encoding="utf-8-sig", newline="") as f:
            monthly = next(r for r in csv.DictReader(f, delimiter=";") if r["scale"] == "month")
        self.assertEqual(monthly["requests_complete"], "False")
        self.assertEqual(monthly["finite_numeric_cells"], "0")


if __name__ == "__main__":
    unittest.main()
