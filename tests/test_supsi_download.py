"""Small offline checks of SUPSI catalogue and preservation rules."""
import csv
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import supsi_download as sup  # noqa: E402


def fixture():
    out = io.StringIO()
    writer = csv.DictWriter(out, ["istSOS", "Nome", "Periodo da", "Pubblicato", "footer"])
    writer.writeheader()
    writer.writerow(dict(istSOS="P_OLD", Nome="Old", **{"Periodo da": "1979-1981"},
                         Pubblicato="si", footer=""))
    writer.writerow(dict(istSOS="P_JOIN", Nome="Join", **{"Periodo da": "1988"},
                         Pubblicato="si", footer="Composite 1988-2021, 2022 onward"))
    writer.writerow(dict(istSOS="P_HIDDEN", Nome="Hidden", **{"Periodo da": "2022"},
                         Pubblicato="no", footer=""))
    f = {"type": "Feature", "geometry": {"coordinates": [8.7, 46.1, 1400]},
         "properties": {"name": "P_JOIN", "id": 7, "sensortype": "virtual",
                        "samplingTime": {"beginposition": "2007-01-01"}}}
    geo = json.dumps({"type": "FeatureCollection", "features": [f]}).encode()
    f["geometry"]["coordinates"] = [2700000, 1100000, 1395]
    lv = json.dumps({"type": "FeatureCollection", "features": [f]}).encode()
    return {"tabelle_annuario_rain.csv": out.getvalue().encode(),
            "geojson.json": geo, "geojson2056.json": lv}


class CatalogueTests(unittest.TestCase):
    def test_retired_unpublished_composite_and_distinct_elevations(self):
        stations = sup.parse_catalogues(fixture())
        self.assertEqual(len(stations), 3)
        self.assertIn("missing_current_geojson_feature", stations[0]["metadata_flags"])
        self.assertEqual(stations[1]["wgs84_elevation_m"], 1400)
        self.assertEqual(stations[1]["lv95_elevation_m"], 1395)
        self.assertIn("virtual_sensor", stations[1]["metadata_flags"])
        plan = sup.yearbook_plan(stations, 1979, 2023, {s["station_id"] for s in stations})
        self.assertEqual([p["year"] for p in plan if p["station_id"] == "P_OLD"],
                         [1979, 1980, 1981])
        self.assertNotIn("P_HIDDEN", [p["station_id"] for p in plan])
        self.assertEqual(len([p for p in plan if p["station_id"] == "P_JOIN"]), 36)
        stations[1]["candidate_first_year"] = 2020
        stations[1]["sampling_begin_raw"] = "2019-05-28T09:00:00+0000"
        boundary = sup.yearbook_plan(stations, 1979, 2020, {"P_JOIN"})
        self.assertEqual(boundary[0]["year"], 2019)
        self.assertEqual(boundary[0]["candidate_basis"], "sampling_begin_boundary")

    def test_malformed_catalogue_is_fatal(self):
        raw = fixture()
        raw["tabelle_annuario_rain.csv"] = b"istSOS,Nome\nP_BAD,bad\n"
        with self.assertRaises(sup.AcquisitionError):
            sup.parse_catalogues(raw)

    def test_pdf_validity_rejects_html_and_truncation(self):
        valid = b"%PDF-1.4\n" + b"x" * 500 + b"\n%%EOF\n"
        sup.validate("yearbook", valid)
        for data in (b"<html>error</html>" + b"x" * 600, valid[:-6]):
            with self.assertRaises(sup.AcquisitionError):
                sup.validate("yearbook", data)

    def test_hash_verified_cache_and_corruption_redownload(self):
        valid = b"%PDF-1.4\n" + b"x" * 500 + b"\n%%EOF\n"
        url = sup.ASSETS + "/daily/rain/P_JOIN/P_JOIN_2023.pdf"
        class Response:
            status = 200
            headers = {"Content-Type": "application/pdf", "Content-Length": str(len(valid))}
            def __init__(self, address):
                self.url = address
            def read(self, limit):
                return valid
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with sqlite3.connect(root / "cache.sqlite3") as db:
                archive = sup.Archive(root, db, 0, 1, 0, False)
                with patch.object(sup.urllib.request, "urlopen", return_value=Response(url)) as request:
                    first, _ = archive.fetch("yearbook", url, "P_JOIN", 2023)
                    second, _ = archive.fetch("yearbook", url, "P_JOIN", 2023)
                    self.assertEqual(request.call_count, 1)
                    self.assertEqual(first["status"], "downloaded")
                    self.assertEqual(second["status"], "cached")
                    (root / first["raw_path"]).write_bytes(b"corrupt")
                    third, _ = archive.fetch("yearbook", url, "P_JOIN", 2023)
                    self.assertEqual(third["status"], "failed")
                    self.assertIn("Existing archive file differs", third["detail"])


if __name__ == "__main__":
    unittest.main()
