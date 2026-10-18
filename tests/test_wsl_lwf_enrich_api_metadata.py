"""Verify provenance-rich metadata can be added without changing API values."""

from hashlib import sha256
import json
from pathlib import Path
import sys
import tempfile
import unittest
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import wsl_lwf_api_excel as base  # noqa: E402
import wsl_lwf_enrich_api_metadata as enrich  # noqa: E402


class MetadataEnrichmentTest(unittest.TestCase):
    def test_historical_positions_and_unchanged_api_precipitation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "working/wsl_lwf/api_only_excel/20261017T122441Z/example"
            directory.mkdir(parents=True)
            xlsx = directory / base.MAIN_FILE
            metadata = []
            for model in sorted(enrich.SITES):
                exposure = enrich.SITES[model][2]
                cells = {"station_id": model, "api_model": model,
                         "station_exposure_inferred_from_id": exposure,
                         "hourly_metadata_first_utc": "1999-01-01",
                         "hourly_metadata_last_utc": "2022-12-13"}
                metadata.append(tuple(cells.get(field) for field in base.META_HEADERS))
            grid = {"station_id": "novaggio_freiland_27", "period_utc": "2001-01",
                    "D01": "37.5", "computed_complete_month_total_mm": "37.5"}
            hourly = {"station_id": "novaggio_freiland_27", "date_utc": "2001-01-01",
                      "H00": "2.5", "computed_complete_day_total_mm": "37.5"}
            jsonl = directory / "hourly.jsonl"
            jsonl.write_text(json.dumps(tuple(hourly.get(f) for f in base.HOUR_HEADERS)) + "\n")
            sheet_summary = base.write_workbook(
                xlsx, metadata, jsonl, 1, [tuple(grid.get(f) for f in base.GRID_HEADERS)])
            pointer = {"status": "completed_api_only_provisional_source_label_sums",
                       "station_count": 13, "api_run_id": "20261017T122441Z",
                       "workbook": str(xlsx), "workbook_sha256": base.sha(xlsx),
                       "sheets": sheet_summary}
            (root / "working/wsl_lwf/api_only_excel/latest.json").write_text(json.dumps(pointer))
            with ZipFile(xlsx) as z:
                original = [z.read(f"xl/worksheets/sheet{i}.xml") for i in (2, 3)]
            self.assertEqual(enrich.main(["--root", str(root)]), 0)
            report = json.loads((root / "working/wsl_lwf/api_only_excel/metadata_enriched/latest.json").read_text())
            self.assertTrue(report["precipitation_sheets_byte_identical_to_input"])
            with ZipFile(report["workbook"]) as z:
                for index, contents in zip((2, 3), original):
                    self.assertEqual(z.read(f"xl/worksheets/sheet{index}.xml"), contents)
                values = enrich.read_metadata_sheet_enriched(z.read("xl/worksheets/sheet1.xml"),
                                                              base.META_HEADERS + enrich.EXTRA)
            novaggio = next(r for r in values if r["station_id"] == "novaggio_freiland_27")
            self.assertEqual((novaggio["longitude"], novaggio["latitude"], novaggio["elevation_m"]),
                             ("8.83519", "46.02417", "1074"))
            self.assertEqual(novaggio["historical_station_code"], "NOF")
            self.assertEqual(novaggio["hourly_metadata_last_utc"], "2022-12-13")
            self.assertEqual(novaggio["post2016_station_location_verified"], "false")
            self.assertEqual([sha256(content).hexdigest() for content in original],
                             [report["precipitation_sheet_xml_sha256"][f"xl/worksheets/sheet{i}.xml"]
                              for i in (2, 3)])


if __name__ == "__main__":
    unittest.main()
