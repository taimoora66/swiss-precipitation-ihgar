import csv
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from wsl_lwf_screen import screen_station


class ClockChangeScreenTest(unittest.TestCase):
    def test_clock_change_pair_is_preserved_and_conflict_reported(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sample.csv"
            with path.open("w", encoding="utf-8", newline="") as file:
                writer = csv.writer(file, delimiter=";")
                writer.writerow(("timestamp", "precip"))
                writer.writerows([
                    ("2010-10-31 01:00:00", "0"),
                    ("2010-10-31 02:00:00", "2.8"),
                    ("2010-10-31 02:00:00", "3"),
                    ("2010-10-31 03:00:00", "NA"),
                ])
            counts, years, _, _, _ = screen_station(path)
            self.assertEqual(counts["rows"], 4)
            self.assertEqual(counts["autumn_clock_change_duplicates"], 1)
            self.assertEqual(counts["duplicate_conflicting_precip"], 1)
            self.assertEqual(years[2010]["unique_hours"], 3)
            self.assertEqual(years[2010]["numeric_precip_hours"], 2)
            self.assertEqual(counts["missing_precip_rows"], 1)


if __name__ == "__main__":
    unittest.main()
