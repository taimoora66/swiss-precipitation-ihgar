"""The station organizer must block changed source cells and invalid dates."""
import csv
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/supsi_organize_validate.py"
FIXTURE = ROOT.parent / "supsi-pilot"
RUN = "20261015T093047Z_204fc74c_ed430bd1"
ACQUISITION = "20261015T093047Z_204fc74c"


class StationOrganizerIntegration(unittest.TestCase):
    def setUp(self):
        if not FIXTURE.exists():
            self.skipTest("Pilot archive is not available")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for relative in (f"data/derived/supsi/{RUN}/daily.csv",
                         f"data/derived/supsi/{RUN}/monthly.csv",
                         f"runs/supsi_extraction/{RUN}/pdf_review.csv",
                         f"runs/supsi/{ACQUISITION}/station_inventory.csv"):
            destination = self.root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(FIXTURE / relative, destination)
        for pdf in (FIXTURE / "data/raw/supsi/yearbook").rglob("*.pdf"):
            relative = pdf.relative_to(FIXTURE)
            destination = self.root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(pdf, destination)

    def run_audit(self):
        return subprocess.run([sys.executable, str(SCRIPT), "--root", str(self.root),
                               "--extraction-id", RUN], capture_output=True, text=True)

    def test_original_pdf_cells_reconcile(self):
        result = self.run_audit()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        station = self.root / "working/supsi/stations" / RUN / "P_NEG"
        self.assertTrue((station / "Daily.csv").exists())
        with (station / "Daily.csv").open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle, delimiter=";"))
        self.assertEqual(len(rows), 365)
        self.assertEqual(rows[0]["date"], "2023-01-01")
        self.assertEqual(rows[0]["collection"], "supsi_ticino_yearbook_rain")
        self.assertEqual(rows[0]["catalogue_name"], "Alpe di Neggia - Gambarogno")

    def test_changed_value_blocks_publication(self):
        path = self.root / "data/derived/supsi" / RUN / "daily.csv"
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle, delimiter=";"))
        rows[0]["raw_token"] = "999.9"
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]), delimiter=";")
            writer.writeheader()
            writer.writerows(rows)
        result = self.run_audit()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "working/supsi/stations" / RUN).exists())
        issues = (self.root / "runs/supsi_organization" / RUN / "cell_issues.csv").read_text(encoding="utf-8-sig")
        self.assertIn("source_cell_mismatch", issues)
        self.assertIn("2023-01-01", issues)

    def test_invalid_calendar_date_blocks_publication(self):
        path = self.root / "data/derived/supsi" / RUN / "daily.csv"
        with path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle, delimiter=";"))
        rows[0]["date"] = "2023-02-30"
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]), delimiter=";")
            writer.writeheader()
            writer.writerows(rows)
        result = self.run_audit()
        self.assertNotEqual(result.returncode, 0)
        issues = (self.root / "runs/supsi_organization" / RUN / "cell_issues.csv").read_text(encoding="utf-8-sig")
        self.assertIn("input_invalid", issues)
        self.assertIn("2023-02-30", issues)


if __name__ == "__main__":
    unittest.main()
