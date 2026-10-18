#!/usr/bin/env python3
"""Add sourced historical WSL station metadata to the existing API-only XLSX.

No precipitation data are read from PANGAEA or the archived WSL product. The
existing API-only daily sheet is copied byte-for-byte into a new workbook.
"""

import argparse
import csv
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import uuid
import xml.etree.ElementTree as ET
from zipfile import ZipFile

try:
    import wsl_lwf_api_excel as base
except ModuleNotFoundError as exc:
    if exc.name != "wsl_lwf_api_excel":
        raise
    import wsl_lwf_api_excel_v2 as base


# Station-specific PANGAEA event locations, published in the 2016 LWF series.
# Longitude and latitude are EPSG:4326 decimal degrees, elevation metres asl.
# These are historical station coordinates, not a verified post-2016 site history.
SITES = {
    "celerina_bestand_6":       ("CLB", "Celerina", "forest",     "9.88881", "46.49213", 1883, "868406"),
    "celerina_freiland_7":      ("CLF", "Celerina", "open_field", "9.87587", "46.50608", 1764, "868405"),
    "chironico_bestand_8":      ("CIB", "Chironico", "forest",    "8.81281", "46.44623", 1416, "868403"),
    "chironico_freiland_9":     ("CIF", "Chironico", "open_field","8.81822", "46.44087", 1483, "868404"),
    "isone_bestand_12":         ("ISB", "Isone", "forest",        "9.00769", "46.12506", 1214, "868407"),
    "isone_freiland_13":        ("ISF", "Isone", "open_field",    "9.00731", "46.12608", 1152, "868408"),
    "lens_bestand_20":          ("LEB", "Lens", "forest",         "7.43520", "46.26837", 1094, "868461"),
    "nationalpark_bestand_22":  ("NAB", "Nationalpark", "forest", "10.23047","46.66164", 1893, "868417"),
    "nationalpark_freiland_23": ("NAF", "Nationalpark", "open_field","10.24102","46.65964",1920,"868416"),
    "novaggio_bestand_26":      ("NOB", "Novaggio", "forest",    "8.83428", "46.02162", 900,  "868437"),
    "novaggio_freiland_27":     ("NOF", "Novaggio", "open_field","8.83519", "46.02417", 1074, "868436"),
    "visp_bestand_33":         ("VSB", "Visp", "forest",        "7.85708", "46.29691", 694,  "868445"),
    "visp_freiland_34":        ("VSF", "Visp", "open_field",    "7.85828", "46.29817", 650, "868443"),
}

INSTRUMENT_URL = "https://link.springer.com/article/10.1007/s13595-018-0709-7/tables/1"
OWNER_URL = "https://lwf.wsl.ch/en/"
LOCATION_NOTE = "PANGAEA_2016_station_event; post_2016_location_not_verified"
INSTRUMENT = "ARG 100 unheated tipping-bucket; 1.5 m; 0.2 mm/tip (2018 network description)"
EXTRA = (
    "historical_station_code", "historical_station_metadata_url",
    "historical_station_metadata_period", "post2016_station_location_verified",
    "instrument_description_source_url", "instrument_after_2018_verified",
    "owner_source_url", "source_location_crs", "metadata_join_basis",
)


def file_hash(path):
    digest = sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_metadata_sheet(xml):
    tree = ET.fromstring(xml)
    ns = "{" + base.NS + "}"
    rows = []
    for row in tree.find(ns + "sheetData"):
        found = {}
        for cell in row.findall(ns + "c"):
            address = cell.attrib["r"]
            column = address.rstrip("0123456789")
            if cell.attrib.get("t") == "inlineStr":
                value = cell.findtext(ns + "is/" + ns + "t") or ""
            else:
                value = cell.findtext(ns + "v") or ""
            found[column] = value
        rows.append(found)
    base.require(len(rows) == 14, "Expected exactly 13 station metadata rows")
    header = []
    for i in range(1, len(base.META_HEADERS) + 1):
        header.append(rows[0].get(base.excel_col(i)))
    base.require(tuple(header) == base.META_HEADERS, "Unexpected API-only metadata columns")
    values = [dict(zip(header, (r.get(base.excel_col(i), "") for i in range(1, len(header)+1))))
              for r in rows[1:]]
    base.require({r["station_id"] for r in values} == set(SITES),
                 "Workbook station list differs from the 13 PANGAEA station mappings")
    base.require(len({r["station_id"] for r in values}) == 13,
                 "Repeated station in original metadata sheet")
    return values


def enrich(original):
    result = []
    for before in original:
        row = before.copy()
        model = row["station_id"]
        code, name, exposure, lon, lat, elevation, doi = SITES[model]
        base.require(row["api_model"] == model and row["station_exposure_inferred_from_id"] == exposure,
                     f"API identifier or station exposure differs: {model}")
        base.require(all(not row[field] for field in ("owner", "longitude", "latitude", "elevation_m", "instrument")),
                     f"Original metadata is already enriched: {model}")
        row.update({
            "station_name": f"{name} ({'forest' if exposure == 'forest' else 'open field'})",
            "owner": "Swiss Federal Institute for Forest, Snow and Landscape Research WSL",
            "owner_status": "WSL_runs_and_finances_LWF_program; owner_of_LWF_infrastructure",
            "longitude": lon, "latitude": lat, "elevation_m": str(elevation),
            "coordinate_elevation_status": LOCATION_NOTE,
            "instrument": INSTRUMENT,
            "instrument_status": "2018_network_equipment_description; present_gauge_not_verified",
            "historical_station_code": code,
            "historical_station_metadata_url": f"https://doi.pangaea.de/10.1594/PANGAEA.{doi}",
            "historical_station_metadata_period": "published_2016_archive_station_event",
            "post2016_station_location_verified": "false",
            "instrument_description_source_url": INSTRUMENT_URL,
            "instrument_after_2018_verified": "false",
            "owner_source_url": OWNER_URL,
            "source_location_crs": "EPSG:4326",
            "metadata_join_basis": "station_name_plus_forest_field_exposure_plus_2016_overlap_audit",
        })
        result.append(row)
    return result


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=Path("."))
    args = p.parse_args(argv)
    root = args.root.resolve()
    pointer_path = root / "working/wsl_lwf/api_only_excel/latest.json"
    pointer = json.loads(pointer_path.read_text(encoding="utf-8-sig"))
    base.require(pointer["status"] == "completed_api_only_provisional_source_label_sums"
                 and pointer["station_count"] == 13, "Input must be the completed 13-model API-only workbook")
    source = Path(pointer["workbook"]).resolve()
    base.require(source.is_file() and source.is_relative_to(root / "working/wsl_lwf/api_only_excel"),
                 "Source workbook must be inside the API-only project folder")
    base.require(file_hash(source) == pointer["workbook_sha256"], "Source workbook differs from verified API export")
    base.require(pointer["api_run_id"] == "20261017T122441Z", "Unexpected API acquisition run")

    with ZipFile(source) as z:
        base.require(z.testzip() is None, "Source workbook ZIP failed integrity check")
        original_meta = read_metadata_sheet(z.read("xl/worksheets/sheet1.xml"))
        original_grid_xml = z.read("xl/worksheets/sheet2.xml")
    new_meta = enrich(original_meta)
    headers = base.META_HEADERS + EXTRA
    rows = [tuple(r.get(k, "") for k in headers) for r in new_meta]
    changed_sheet_xml, digest = base.sheet_xml(headers, rows, 4,
        base.META_NUMERIC | {"longitude", "latitude", "elevation_m"})
    target_root = root / "working/wsl_lwf/api_only_excel/metadata_enriched"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + uuid.uuid4().hex[:8]
    folder = target_root / pointer["api_run_id"] / stamp
    folder.mkdir(parents=True, exist_ok=False)
    destination = folder / "WSL_LWF_API_Only_Selected_All_Metadata_Enriched.xlsx"
    with ZipFile(source) as original, ZipFile(destination, "w") as updated:
        for info in original.infolist():
            contents = (changed_sheet_xml if info.filename == "xl/worksheets/sheet1.xml"
                        else original.read(info))
            updated.writestr(info, contents)

    with ZipFile(destination) as check:
        base.require(check.testzip() is None, "Enriched workbook ZIP failed integrity check")
        base.require(check.read("xl/worksheets/sheet2.xml") == original_grid_xml,
                     "Daily precipitation XML changed while adding metadata")
        verified = read_metadata_sheet_enriched(check.read("xl/worksheets/sheet1.xml"), headers)
        base.require(verified == new_meta, "Enriched metadata cells differ after saving")
        base.require(sha256(check.read("xl/worksheets/sheet1.xml")).hexdigest() == sha256(changed_sheet_xml).hexdigest(),
                     "Saved metadata XML differs")

    csv_path = folder / "Station_metadata_QGIS.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as out:
        writer = csv.DictWriter(out, fieldnames=headers, delimiter=";")
        writer.writeheader()
        writer.writerows(new_meta)
    with csv_path.open("r", newline="", encoding="utf-8-sig") as inp:
        base.require(list(csv.DictReader(inp, delimiter=";")) ==
                     [{k: str(v) for k, v in row.items()} for row in new_meta], "Saved GIS CSV differs")

    summary = {
        "status": "api_only_values_unchanged_historical_station_metadata_enriched",
        "api_run_id": pointer["api_run_id"], "station_count": 13,
        "metadata_origin": "station_specific_PANGAEA_2016_events_plus_2018_network_equipment_and_WSL_official_program",
        "post2016_station_location_verified": False,
        "current_instrument_verified": False,
        "precipitation_sheet_xml_sha256": sha256(original_grid_xml).hexdigest(),
        "precipitation_sheet_byte_identical_to_input": True,
        "metadata_cell_digest": digest,
        "input_workbook": str(source), "input_workbook_sha256": pointer["workbook_sha256"],
        "workbook": str(destination), "workbook_sha256": file_hash(destination),
        "qgis_metadata_csv": str(csv_path), "output_directory": str(folder),
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    (folder / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    target_root.mkdir(parents=True, exist_ok=True)
    part = target_root / "latest.json.part"
    part.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    os.replace(part, target_root / "latest.json")
    print(f"Enriched 13 API model metadata rows; daily sheet is byte-for-byte unchanged: {destination}")
    return 0


def read_metadata_sheet_enriched(xml, headers):
    tree = ET.fromstring(xml)
    ns = "{" + base.NS + "}"
    rows = list(tree.find(ns + "sheetData"))
    base.require(len(rows) == 14, "Saved station metadata row count changed")
    def values(row):
        found = {}
        for cell in row.findall(ns + "c"):
            address = cell.attrib["r"].rstrip("0123456789")
            found[address] = (cell.findtext(ns + "is/" + ns + "t") if cell.attrib.get("t") == "inlineStr"
                              else cell.findtext(ns + "v")) or ""
        return [found.get(base.excel_col(i), "") for i in range(1, len(headers) + 1)]
    base.require(tuple(values(rows[0])) == headers, "Saved enriched headers differ")
    return [{key: value for key, value in zip(headers, values(row))} for row in rows[1:]]


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"Metadata enrichment stopped: {exc}", file=sys.stderr)
        sys.exit(1)
