#!/usr/bin/env python3
"""Build dated SUPSI station metadata from SHA-verified precipitation yearbooks.

Requires pdfplumber (already used by the SUPSI extraction). Source PDFs and
existing combined files are read only. Coordinates are LV95 metres as printed
in the PDF header; header elevation is printed m.s.m. No vertical datum
conversion or historical site identity is inferred.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pdfplumber

HEADER = re.compile(r"Koordinaten\s+(\d+)\s*/\s*(\d+)\s+Höhe\s+(\d+(?:[.,]\d+)?)")
CHANGED = {"P_CAL", "P_GNO", "P_GRA", "P_MAG", "P_MEN"}
COLLECTION = "supsi_ticino_yearbook_rain"
YEAR_FIELDS = ["collection", "station_id", "station_name_current", "year", "series_source_station", "site_status", "pdf_reported_lv95_easting_m", "pdf_reported_lv95_northing_m", "pdf_reported_elevation_m", "candidate_source_lv95_easting_m", "candidate_source_lv95_northing_m", "candidate_source_elevation_m", "current_catalogue_lv95_easting_m", "current_catalogue_lv95_northing_m", "current_catalogue_elevation_m", "source_pdf_path", "source_pdf_sha256", "metadata_note"]
SEG_FIELDS = ["collection", "station_id", "station_name_current", "first_year", "last_year", "yearbooks", "series_source_station", "site_status", "pdf_reported_lv95_easting_m", "pdf_reported_lv95_northing_m", "pdf_reported_elevation_m", "candidate_source_lv95_easting_m", "candidate_source_lv95_northing_m", "candidate_source_elevation_m", "current_catalogue_lv95_easting_m", "current_catalogue_lv95_northing_m", "current_catalogue_elevation_m", "metadata_note", "first_source_pdf_path", "last_source_pdf_path"]


def records(path):
    with path.open(newline="", encoding="utf-8-sig") as f:
        yield from csv.DictReader(f, delimiter=";")


def write(path, fields, rows):
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, delimiter=";", extrasaction="raise")
        w.writeheader()
        for row in rows:
            w.writerow(row)


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def source_identity(sid, year):
    if sid == "P_NOVAZ_TOT":
        if year <= 2021:
            return ("Chiasso (catalogue history)", "composite_pre2022_location_unresolved", "PDF prints Novazzano header, but catalogue says Chiasso gauge was used through 2021; printed header is not confirmed as measurement site.")
        return ("Novazzano (catalogue history)", "composite_novazzano_reported", "Catalogue says Novazzano gauge is used from 2022; PDF header reports Novazzano location.")
    if sid in CHANGED:
        return (sid, "reported_site_change_needs_review", "PDF header coordinates/elevation differ between historical periods; reason and physical station history are unverified.")
    if sid in ("P_CERE", "P_ST_ANT"):
        return (sid, "archived_pdf_no_current_catalogue_location", "PDF header reports location; current catalogue has no coordinates for this archived site.")
    return (sid, "pdf_header_reported", "Yearbook header location; physical continuity and vertical datum have not been independently verified.")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument("--extraction-id", required=True)
    p.add_argument("--acquisition-id")
    args = p.parse_args()
    root = args.root.resolve()
    rid = args.extraction_id
    aid = args.acquisition_id or rid.rsplit("_", 1)[0]
    if not re.fullmatch(r"[A-Za-z0-9_]+", rid + aid):
        p.error("Invalid run ID")
    review_file = root / "runs/supsi_extract" / rid / "pdf_review.csv"
    if not review_file.exists():
        candidates = list((root / "runs").glob(f"**/{rid}/pdf_review.csv"))
        if len(candidates) != 1:
            p.error("Could not uniquely locate extraction pdf_review.csv; use matching runs/supsi_extract/<id>/pdf_review.csv")
        review_file = candidates[0]
    inventory_file = root / "runs/supsi" / aid / "station_inventory.csv"
    combined = root / "working/supsi/combined" / rid
    source_report = root / "runs/supsi_combined" / rid / "summary.json"
    for path in (inventory_file, combined / "daily.csv", combined / "monthly.csv", source_report):
        if not path.is_file():
            p.error(f"Missing input: {path}")
    source_summary = json.loads(source_report.read_text(encoding="utf-8"))
    if source_summary.get("status") != "combined_verified" or source_summary.get("issues") != 0:
        p.error("Combined source has not passed its field audit")
    inventory_rows = list(records(inventory_file))
    inventory = {r["station_id"]: r for r in inventory_rows}
    if len(inventory) != len(inventory_rows):
        raise ValueError("Duplicate catalogue station ID")
    review = list(records(review_file))
    by_key = {}
    for r in review:
        sid, year = r["station_id"], int(r["year"])
        key = (sid, year)
        if key in by_key or sid not in inventory:
            raise ValueError(f"Duplicate or unknown station/year: {key}")
        pdf = root / r["source_pdf_path"]
        if not pdf.is_file() or digest(pdf).lower() != r["source_pdf_sha256"].lower():
            raise ValueError(f"Missing or SHA-mismatched PDF: {pdf}")
        with pdfplumber.open(pdf) as doc:
            matches = HEADER.findall(doc.pages[0].extract_text())
        if len(matches) != 1:
            raise ValueError(f"Expected one coordinate/elevation header: {pdf}")
        north, east, elev = matches[0]
        meta = inventory[sid]
        source, status, note = source_identity(sid, year)
        chiasso = inventory.get("P_CHIA", {}) if sid == "P_NOVAZ_TOT" and year <= 2021 else {}
        by_key[key] = dict(zip(YEAR_FIELDS, [COLLECTION, sid, meta["name"], year, source, status, east, north, str(float(elev.replace(",", "."))).rstrip("0").rstrip("."), chiasso.get("lv95_easting", ""), chiasso.get("lv95_northing", ""), chiasso.get("lv95_elevation_m", ""), meta["lv95_easting"], meta["lv95_northing"], meta["lv95_elevation_m"], r["source_pdf_path"], r["source_pdf_sha256"], note]))
    grouped = defaultdict(list)
    for (sid, year), row in sorted(by_key.items()):
        grouped[sid].append(row)
    segments = []
    for sid, years in sorted(grouped.items()):
        last = None
        for row in years:
            signature = tuple(row[k] for k in ("series_source_station", "site_status", "pdf_reported_lv95_easting_m", "pdf_reported_lv95_northing_m", "pdf_reported_elevation_m"))
            if last is None or signature != last[0] or row["year"] != last[1]["last_year"] + 1:
                seg = {k: row[k] for k in ("collection", "station_id", "station_name_current", "series_source_station", "site_status", "pdf_reported_lv95_easting_m", "pdf_reported_lv95_northing_m", "pdf_reported_elevation_m", "candidate_source_lv95_easting_m", "candidate_source_lv95_northing_m", "candidate_source_elevation_m", "current_catalogue_lv95_easting_m", "current_catalogue_lv95_northing_m", "current_catalogue_elevation_m", "metadata_note")}
                seg.update(first_year=row["year"], last_year=row["year"], yearbooks=1, first_source_pdf_path=row["source_pdf_path"], last_source_pdf_path=row["source_pdf_path"])
                segments.append(seg)
                last = (signature, seg)
            else:
                last[1]["last_year"] = row["year"]
                last[1]["yearbooks"] += 1
                last[1]["last_source_pdf_path"] = row["source_pdf_path"]
    out = root / "working/supsi/site_history" / rid
    if out.exists():
        p.error(f"Output exists and will not be overwritten: {out}")
    stage = out.parent / (".stage_" + uuid.uuid4().hex)
    stage.mkdir(parents=True)
    year_rows = [by_key[k] for k in sorted(by_key)]
    try:
        write(stage / "station_year_reported.csv", YEAR_FIELDS, year_rows)
        write(stage / "site_segments.csv", SEG_FIELDS, segments)
        counts = {}
        for kind, filename in (("daily", "daily.csv"), ("monthly", "monthly.csv")):
            src = combined / filename
            dst = stage / (kind + "_with_reported_site.csv")
            with src.open(newline="", encoding="utf-8-sig") as fi, dst.open("w", newline="", encoding="utf-8-sig") as fo:
                reader = csv.DictReader(fi, delimiter=";")
                added = [x for x in YEAR_FIELDS if x not in reader.fieldnames and x != "collection"]
                fields = list(reader.fieldnames) + added
                writer = csv.DictWriter(fo, fieldnames=fields, delimiter=";", extrasaction="raise")
                writer.writeheader()
                seen = Counter()
                for r in reader:
                    key = (r["station_id"], int(r["year"]))
                    y = by_key.get(key)
                    if y is None or r["source_pdf_sha256"] != y["source_pdf_sha256"] or r["source_pdf_path"] != y["source_pdf_path"]:
                        raise ValueError(f"{kind} observation/source does not match dated PDF: {key}")
                    merged = {**r, **{k: y[k] for k in added}}
                    writer.writerow(merged)
                    seen[key] += 1
            if set(seen) != set(by_key) or sum(seen.values()) != source_summary[kind + "_rows"]:
                raise ValueError(f"{kind} output/source counts or station-years disagree")
            counts[kind] = sum(seen.values())
        # Independent reread of all exported site mappings and core source fields.
        if list(records(stage / "station_year_reported.csv")) != [{**r, "year": str(r["year"])} for r in year_rows]:
            raise ValueError("Exported station-year table differs after reread")
        (stage / "summary.json").write_text(json.dumps({"extraction_id": rid, "acquisition_id": aid, "source_pdfs_sha_verified": len(by_key), "stations": len(grouped), "site_segments": len(segments), "daily_joined_rows": counts["daily"], "monthly_joined_rows": counts["monthly"], "site_status_years": dict(Counter(r["site_status"] for r in year_rows)), "site_segments_status": dict(Counter(r["site_status"] for r in segments)), "inventory_sha256": digest(inventory_file), "status": "pdf_reported_site_history_join_verified", "scientific_site_history_verified": False, "created_at_utc": datetime.now(timezone.utc).isoformat()}, indent=2), encoding="utf-8")
        stage.rename(out)
    except BaseException:
        import shutil
        shutil.rmtree(stage)
        raise
    print(f"Verified {len(by_key)} PDFs; {len(segments)} dated segments; joined {counts['daily']} daily and {counts['monthly']} monthly rows. Output: {out}")


if __name__ == "__main__":
    main()
