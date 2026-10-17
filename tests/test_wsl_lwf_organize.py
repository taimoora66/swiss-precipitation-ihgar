import csv
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from wsl_lwf_organize import organize


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class HourlyOrganizationTests(unittest.TestCase):
    def fixture(self, root):
        source = root / "data/raw/wsl_lwf/observations/sample.csv"
        source.parent.mkdir(parents=True)
        source.write_text("timestamp;precip;temp\n"
            "2020-10-25 01:00:00;0;3\n"
            "2020-10-25 02:00:00;0.2;4\n"
            "2020-10-25 02:00:00;0.3;5\n"
            "2020-10-25 03:00:00;NA;6\n", encoding="utf-8")
        spatial = '{"type": "Point", "coordinates": [9.0,46.0]}'
        package = {"name": "lwfmeteo-test", "spatial": spatial, "doi": "doi:test",
                   "license_id": "test", "resources": [{"id": "resource-1", "name": "Field (TSF)"}]}
        snap = root / "data/raw/wsl_lwf/metadata/package.json"
        snap.parent.mkdir(parents=True)
        snap.write_text(json.dumps({"result": package}), encoding="utf-8")
        run = root / "runs/wsl_lwf/20261015T000000Z"
        run.mkdir(parents=True)
        (run / "summary.json").write_text(json.dumps({"status": "completed_selected_scope",
            "failures": 0, "station_csvs_scanned": 1, "public_datasets": 1}), encoding="utf-8")
        (run / "catalogue_snapshots.json").write_text(json.dumps([{
            "url": "https://www.envidat.ch/api/3/action/package_show?id=lwfmeteo-test",
            "local_path": snap.relative_to(root).as_posix(), "sha256": digest(snap)}]), encoding="utf-8")
        (run / "resource_manifest.json").write_text(json.dumps([{
            "resource_id": "resource-1", "resource_format": "CSV", "station_code": "TSF",
            "local_path": source.relative_to(root).as_posix(), "sha256": digest(source)}]), encoding="utf-8")
        inv = {"dataset": "lwfmeteo-test", "station_code": "TSF", "station_exposure": "open_field",
               "dataset_spatial": spatial, "resource_name": "Field (TSF)", "resource_id": "resource-1",
               "doi": "doi:test", "license_id": "test", "rows": "4",
               "first_timestamp": "2020-10-25 01:00:00", "last_timestamp": "2020-10-25 03:00:00",
               "precipitation_numeric": "3", "precipitation_missing": "1",
               "other_precipitation": "0", "negative_precipitation": "0",
               "duplicate_or_nonmonotonic_timestamps": "1",
               "local_path": source.relative_to(root).as_posix(), "sha256": digest(source)}
        with (run / "station_inventory.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(inv), delimiter=";")
            w.writeheader()
            w.writerow(inv)
        return run, source

    def test_duplicate_hour_and_missing_cell_are_preserved_not_summed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            run, _ = self.fixture(root)
            out, result = organize(root, run)
            self.assertEqual((result["hourly_rows"], result["duplicate_timestamp_rows"]), (4, 1))
            with (out / "hourly.csv").open(encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f, delimiter=";"))
            self.assertEqual([r["raw_precip"] for r in rows], ["0", "0.2", "0.3", "NA"])
            self.assertEqual([r["raw_temp"] for r in rows], ["3", "4", "5", "6"])
            self.assertEqual(rows[2]["duplicate_timestamp_review"], "yes")
            self.assertEqual(rows[3]["precipitation_status"], "missing")
            with (out / "daily_observation_counts.csv").open(encoding="utf-8-sig", newline="") as f:
                day = list(csv.DictReader(f, delimiter=";"))[0]
            self.assertEqual((day["source_rows"], day["unique_timestamp_labels"]), ("4", "3"))
            self.assertNotIn("precipitation_total", day)

    def test_changed_source_fails_before_output(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            run, source = self.fixture(root)
            source.write_bytes(source.read_bytes().replace(b"0.3", b"0.8"))
            with self.assertRaisesRegex(ValueError, "SHA-mismatched"):
                organize(root, run)
            self.assertFalse((root / "working/wsl_lwf/organized" / run.name).exists())


if __name__ == "__main__":
    unittest.main()
