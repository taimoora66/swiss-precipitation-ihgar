#!/usr/bin/env python3
"""Archive SUPSI Ticino precipitation metadata and station-year PDF tables.

Python 3.10+, standard library only. Original PDFs are not parsed into observations.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

VERSION = "1.0.0"
BASE = "https://hydromet.supsi.ch"
ASSETS = BASE + "/dati/idrologia/annuario_assets"
CATALOGUES = {
    "tabelle_annuario_rain.csv": ASSETS + "/csv/tabelle_annuario_rain.csv",
    "geojson.json": ASSETS + "/csv/geojson.json",
    "geojson2056.json": ASSETS + "/csv/geojson2056.json",
}
ID = re.compile(r"^P_[A-Z0-9_]+$")
PERIOD = re.compile(r"^(19\d{2}|20\d{2})(?:-(19\d{2}|20\d{2}))?$")
FIELDS = ["kind", "station_id", "year", "url", "status", "http_status", "content_type",
          "bytes", "sha256", "raw_path", "acquired_at_utc", "detail"]


class AcquisitionError(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def write_csv(path, rows, columns):
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, columns, delimiter=";", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def validate(kind, data):
    if kind in ("yearbook", "station_sheet"):
        # Reject HTML/error responses with status 200 and incomplete transfers.
        if len(data) < 500 or not data.startswith(b"%PDF-") or b"%%EOF" not in data[-2048:]:
            raise AcquisitionError("Response is not a complete PDF (header/EOF check).")
    elif kind == "metadata":
        if not data or b"\x00" in data:
            raise AcquisitionError("Empty or binary metadata response.")
    else:
        raise AcquisitionError("Unknown resource kind.")


def parse_catalogues(raw):
    try:
        rain = list(csv.DictReader(io.StringIO(raw["tabelle_annuario_rain.csv"].decode("utf-8-sig"))))
        geo = json.loads(raw["geojson.json"])
        swiss = json.loads(raw["geojson2056.json"])
    except (UnicodeError, ValueError, KeyError) as exc:
        raise AcquisitionError("Catalogue cannot be parsed.") from exc
    if not rain or not {"istSOS", "Periodo da", "Pubblicato", "Nome"}.issubset(rain[0]):
        raise AcquisitionError("Precipitation catalogue schema changed or is empty.")
    def feature_map(obj):
        if obj.get("type") != "FeatureCollection" or not isinstance(obj.get("features"), list):
            raise AcquisitionError("GeoJSON feature collection schema changed.")
        result = {}
        for feature in obj["features"]:
            name = feature.get("properties", {}).get("name")
            if name in result:
                raise AcquisitionError(f"Duplicate GeoJSON station name: {name}")
            result[name] = feature
        return result
    wgs, ch = feature_map(geo), feature_map(swiss)
    inventory = []
    seen = set()
    for row in rain:
        sid = row["istSOS"].strip()
        if not ID.fullmatch(sid) or sid in seen:
            raise AcquisitionError(f"Unsafe or repeated station ID: {sid!r}")
        seen.add(sid)
        match = PERIOD.fullmatch(row["Periodo da"].strip())
        if not match or (match[2] and int(match[2]) < int(match[1])):
            raise AcquisitionError(f"Unexpected publication period for {sid}: {row['Periodo da']!r}")
        if row["Pubblicato"].strip().lower() not in ("si", "no"):
            raise AcquisitionError(f"Unexpected publication status for {sid}")
        wg, lv = wgs.get(sid, {}), ch.get(sid, {})
        wc = wg.get("geometry", {}).get("coordinates", [])
        lc = lv.get("geometry", {}).get("coordinates", [])
        if wc and len(wc) < 2 or lc and len(lc) < 2:
            raise AcquisitionError(f"Malformed geometry for {sid}")
        prop = wg.get("properties", {})
        sampling = prop.get("samplingTime") or {}
        obs = prop.get("observedproperties") or []
        notes = []
        if not wg or not lv:
            notes.append("missing_current_geojson_feature")
        if row.get("footer", "").strip():
            notes.append("publication_footer_requires_review")
        if prop.get("sensortype") == "virtual":
            notes.append("virtual_sensor")
        begin = sampling.get("beginposition", "")
        sample_year = int(begin[:4]) if re.match(r"^\d{4}-", begin) else None
        if sample_year and sample_year < int(match[1]):
            notes.append("sampling_begins_before_publication_period")
        inventory.append({
            "station_id": sid, "name": row["Nome"], "published": row["Pubblicato"].strip().lower(),
            "published_period_raw": row["Periodo da"], "candidate_first_year": int(match[1]),
            "candidate_last_year": int(match[2]) if match[2] else "",
            "catchment_raw": row.get("Bacino", ""), "automatic_raw": row.get("Automatica", ""),
            "online_raw": row.get("Online", ""), "publication_footer": row.get("footer", ""),
            "wgs84_longitude": wc[0] if wc else "", "wgs84_latitude": wc[1] if wc else "",
            "wgs84_elevation_m": wc[2] if len(wc) > 2 else "",
            "lv95_easting": lc[0] if lc else "", "lv95_northing": lc[1] if lc else "",
            "lv95_elevation_m": lc[2] if len(lc) > 2 else "",
            "istsos_id": prop.get("id", ""), "assigned_id": prop.get("assignedid", ""),
            "sensor_type": prop.get("sensortype", ""),
            "sampling_begin_raw": begin,
            "sampling_end_raw": sampling.get("endposition", ""),
            "observed_properties_json": json.dumps(obs, ensure_ascii=False),
            "metadata_flags": "|".join(notes),
            "station_sheet_url": BASE + f"/schede_stazioni/schede_meteo/{sid}.pdf",
            "station_sheet_status": "not_attempted",
        })
    return inventory


def yearbook_plan(inventory, start_year, end_year, selected, include_unpublished=False):
    plan = []
    for station in inventory:
        sid = station["station_id"]
        if sid not in selected or (station["published"] != "si" and not include_unpublished):
            continue
        first = station["candidate_first_year"]
        begin = station["sampling_begin_raw"]
        if re.match(r"^\d{4}-", begin) and int(begin[:4]) < first:
            # Probe a partial boundary year, not an undocumented multi-year span.
            first -= 1
        first = max(start_year, first)
        last = min(end_year, station["candidate_last_year"] or end_year)
        for year in range(first, last + 1):
            plan.append({"station_id": sid, "year": year,
                         "url": ASSETS + f"/daily/rain/{sid}/{sid}_{year}.pdf",
                         "candidate_basis": "sampling_begin_boundary" if year < station["candidate_first_year"]
                                            else "published_period",
                         "catalogue_published": station["published"],
                         "status": "pending"})
    return plan


class Archive:
    def __init__(self, root, db, pause, timeout, retries, refresh):
        self.root, self.db = root, db
        self.pause, self.timeout, self.retries, self.refresh = pause, timeout, retries, refresh
        self.last_request = 0.0
        db.execute("CREATE TABLE IF NOT EXISTS files (url TEXT PRIMARY KEY, kind TEXT NOT NULL, "
                   "path TEXT NOT NULL, sha256 TEXT NOT NULL, bytes INTEGER NOT NULL)")
        db.commit()

    def fetch(self, kind, url, station_id="", year=""):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != "hydromet.supsi.ch" or parsed.username or parsed.password:
            raise AcquisitionError(f"Unexpected source origin: {url}")
        record = {"kind": kind, "station_id": station_id, "year": year, "url": url,
                  "status": "", "http_status": "", "content_type": "", "bytes": "",
                  "sha256": "", "raw_path": "", "acquired_at_utc": now(), "detail": ""}
        if not self.refresh and kind != "metadata":
            old = self.db.execute("SELECT path,sha256,bytes FROM files WHERE url=? AND kind=?",
                                  (url, kind)).fetchone()
            if old:
                path = self.root / old[0]
                if path.is_file():
                    content = path.read_bytes()
                    if len(content) == old[2] and hashlib.sha256(content).hexdigest() == old[1]:
                        validate(kind, content)
                        return {**record, "status": "cached", "bytes": old[2], "sha256": old[1],
                                "raw_path": old[0]}, content
        for attempt in range(self.retries + 1):
            gap = self.pause - (time.monotonic() - self.last_request)
            if gap > 0:
                time.sleep(gap)
            self.last_request = time.monotonic()
            req = urllib.request.Request(url, headers={"User-Agent": f"Swiss-Precipitation-Thesis/{VERSION}",
                                                      "Accept-Encoding": "identity", "Accept": "application/pdf,*/*"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as response:
                    target = urllib.parse.urlsplit(response.url)
                    if target.scheme != "https" or target.hostname != parsed.hostname:
                        raise AcquisitionError("Download redirected away from SUPSI.")
                    record["http_status"] = response.status
                    record["content_type"] = response.headers.get("Content-Type", "")
                    limit = 30 * 1024 * 1024 if kind == "metadata" else 80 * 1024 * 1024
                    content = response.read(limit + 1)
                    if len(content) > limit:
                        raise AcquisitionError("Response exceeded size limit.")
                    expected = response.headers.get("Content-Length")
                    if expected and len(content) != int(expected):
                        raise AcquisitionError("Content-Length does not match received bytes.")
                    validate(kind, content)
                    digest = hashlib.sha256(content).hexdigest()
                    filename = url.rsplit("/", 1)[-1]
                    relative = Path("data/raw/supsi") / kind / digest / filename
                    path = self.root / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if not path.exists():
                        tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
                        try:
                            tmp.write_bytes(content)
                            os.replace(tmp, path)
                        finally:
                            tmp.unlink(missing_ok=True)
                    elif hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                        raise AcquisitionError(f"Existing archive file differs: {path}")
                    self.db.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?)",
                                    (url, kind, relative.as_posix(), digest, len(content)))
                    self.db.commit()
                    return {**record, "status": "downloaded", "bytes": len(content),
                            "sha256": digest, "raw_path": relative.as_posix()}, content
            except urllib.error.HTTPError as exc:
                record["http_status"] = exc.code
                if exc.code in (404, 410):
                    return {**record, "status": "not_found", "detail": "No published file at this URL."}, None
                error = f"HTTP {exc.code}"
                if exc.code not in (408, 429, 500, 502, 503, 504):
                    break
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                error = f"{type(exc).__name__}: {exc}"
            except AcquisitionError as exc:
                error = str(exc)
                break
            if attempt < self.retries:
                time.sleep(min(30, 2 ** attempt))
        return {**record, "status": "failed", "detail": error}, None


def arguments(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    p.add_argument("--metadata-only", action="store_true", help="Catalogue and all station sheets; skip yearbooks.")
    p.add_argument("--stations", nargs="+", help="Station IDs for yearbook pilot; metadata remains complete.")
    p.add_argument("--include-unpublished", action="store_true",
                   help="Also probe candidate years for catalogue entries marked 'no'; retain that flag.")
    p.add_argument("--start-year", type=int, default=1979)
    p.add_argument("--end-year", type=int, default=2023, help="Thesis baseline; use 2025 for later full years.")
    p.add_argument("--refresh", action="store_true", help="Recheck previously archived PDFs for revisions.")
    p.add_argument("--pause", type=float, default=1.0, help="Minimum seconds between requests.")
    p.add_argument("--timeout", type=float, default=45.0)
    p.add_argument("--retries", type=int, default=2)
    args = p.parse_args(argv)
    if (args.start_year < 1979 or args.end_year < args.start_year
            or args.end_year > datetime.now(timezone.utc).year or args.pause < 0
            or args.timeout <= 0 or not 0 <= args.retries <= 6):
        p.error("Invalid year range or transport settings.")
    if args.stations and any(not ID.fullmatch(s) for s in args.stations):
        p.error("Use precipitation IDs such as P_NEG.")
    return args


def main(argv=None):
    args = arguments(argv)
    root = args.root.resolve()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + uuid.uuid4().hex[:8]
    folder = root / "runs/supsi" / run_id
    folder.mkdir(parents=True)
    write_json(folder / "configuration.json", {"run_id": run_id, "version": VERSION,
               "arguments": {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()},
               "started_at_utc": now(), "source": BASE, "scientific_qc_performed": False})
    (root / "data/state").mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / "data/state/supsi.sqlite3")
    archive = Archive(root, db, args.pause, args.timeout, args.retries, args.refresh)
    counts = Counter()
    errors = []
    manifest = folder / "download_manifest.csv"
    summary = {"run_id": run_id, "status": "running", "scientific_qc_performed": False}
    def record(writer, row):
        writer.writerow(row)
        file.flush()
        counts[row["status"]] += 1
        if row["status"] == "failed":
            errors.append(row)
        print(f"{row['status']:10s} {row['kind']:13s} {row['station_id']} {row['year']} {row['url']}", flush=True)
    try:
        with manifest.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, FIELDS, delimiter=";", extrasaction="ignore")
            writer.writeheader()
            raw = {}
            for name, url in CATALOGUES.items():
                row, content = archive.fetch("metadata", url)
                record(writer, row)
                if content is None:
                    raise AcquisitionError(f"Required metadata unavailable: {name}")
                raw[name] = content
            inventory = parse_catalogues(raw)
            known = {s["station_id"] for s in inventory}
            unknown = set(args.stations or []) - known
            if unknown:
                raise AcquisitionError(f"Unknown station IDs: {', '.join(sorted(unknown))}")
            selected = set(args.stations) if args.stations else known
            for station in inventory:
                if station["station_id"] not in selected:
                    station["station_sheet_status"] = "not_selected"
                    continue
                row, _ = archive.fetch("station_sheet", station["station_sheet_url"], station["station_id"])
                record(writer, row)
                station["station_sheet_status"] = row["status"]
            write_csv(folder / "station_inventory.csv", inventory, list(inventory[0]))
            plan = [] if args.metadata_only else yearbook_plan(inventory, args.start_year, args.end_year,
                                                               selected, args.include_unpublished)
            write_csv(folder / "yearbook_plan.csv", plan, ["station_id", "year", "url", "candidate_basis",
                                                        "catalogue_published", "status"])
            for task in plan:
                row, _ = archive.fetch("yearbook", task["url"], task["station_id"], task["year"])
                record(writer, row)
                task["status"] = row["status"]
                # A killed run retains the exact completed work in the manifest and cache.
            write_csv(folder / "yearbook_plan.csv", plan, ["station_id", "year", "url", "candidate_basis",
                                                        "catalogue_published", "status"])
            summary.update({"status": "completed_available_assets" if not errors else "completed_with_errors",
                            "metadata_station_count": len(inventory),
                            "published_station_count": sum(s["published"] == "si" for s in inventory),
                            "stations_without_current_geojson": [s["station_id"] for s in inventory
                                if "missing_current_geojson_feature" in s["metadata_flags"]],
                            "selected_yearbook_station_count": len({p["station_id"] for p in plan}),
                            "candidate_yearbooks": len(plan), "counts_by_status": dict(counts),
                            "finished_at_utc": now()})
    except (AcquisitionError, KeyboardInterrupt) as exc:
        summary.update({"status": "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
                        "detail": str(exc), "counts_by_status": dict(counts), "finished_at_utc": now()})
        errors.append({"detail": str(exc)})
    finally:
        db.close()
        write_json(folder / "errors.json", errors)
        write_json(folder / "summary.json", summary)
        print(f"Status: {summary['status']}. Reports: {folder}", flush=True)
    return 0 if summary["status"] == "completed_available_assets" else 1


if __name__ == "__main__":
    sys.exit(main())
