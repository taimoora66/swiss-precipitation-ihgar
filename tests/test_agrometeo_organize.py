import hashlib
import json
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from agrometeo_organize import SourceReader, monthly_audit


class OrganizationSafetyTests(unittest.TestCase):
    def test_changed_derived_cell_is_rejected_against_archived_json(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            relative = "data/raw/agrometeo/daily/source.json"
            path = root / relative
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"data": [{"date": "2003-01-01 00:00:00",
                                                "3_6_sum": "1.2", "3_1_avg": "4.0"}]}))
            sha = hashlib.sha256(path.read_bytes()).hexdigest()
            source = SourceReader(root, {relative: {"sha256": sha, "kind": "daily"}})
            row = {"date": "2003-01-01", "6_sum": "1.2", "1_avg": "5.0",
                   "source_json": relative, "source_sha256": sha, "source_row": "1"}
            with self.assertRaisesRegex(ValueError, "Sensor cell differs"):
                source.cell(row, "3", "daily", ["6:sum", "1:avg"])
            row["1_avg"] = "4.0"
            self.assertEqual(source.cell(row, "3", "daily", ["6:sum", "1:avg"]),
                             ("1.2", "numeric_string"))

    def test_missing_day_is_not_a_monthly_sum_match(self):
        daily = {"2003-02": {day: Decimal("0") for day in range(1, 28)}}
        monthly = {"2003-02": Decimal("0")}
        result = list(monthly_audit("3", daily, monthly))
        self.assertEqual(result[0]["status"], "incomplete_daily")
        self.assertEqual(result[0]["daily_numeric_sum_mm"], "")


if __name__ == "__main__":
    unittest.main()
