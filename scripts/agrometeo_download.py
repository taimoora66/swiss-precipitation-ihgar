#!/usr/bin/env python3
"""Archive public Agrometeo metadata and historical daily/monthly observations.

Python 3.10+, standard library only. Keep alongside meteoswiss_download.py.
Default: precipitation (6:sum), every publicly discoverable station, 1951 onward.
Acquisition and structural coverage checks are not scientific quality control.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import time
import urllib.parse
import uuid
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from meteoswiss_download import (
    AcquisitionError, Events, HTTPClient, ProjectLock, atomic_json, sha256,
    utcnow, write_csv,
)

VERSION = "1.0.0"
API_BASE = "https://agrometeo.ch/backend/api"
MAX_BYTES = 128 * 1024 * 1024
CANTONS = set("AG AI AR BE BL BS FR GE GL GR JU LU NE NW OW SG SH SO SZ TG TI UR VD VS ZG ZH".split())
SPEC = re.compile(r"^(\d+):(avg|min|max|sum)$")


def iso_date(value):
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Use YYYY-MM-DD.") from exc


def number(value):
    """Do not turn nulls, blanks, sentinels or booleans into zero."""
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (ValueError, TypeError):
        return None


def data_list(obj):
    if not isinstance(obj, dict) or not isinstance(obj.get("data"), list):
        raise AcquisitionError("Expected a JSON object containing a data list.")
    # Do not silently accept just the first catalogue page.
    links = obj.get("links")
    if isinstance(links, dict) and links.get("next"):
        raise AcquisitionError("API pagination appeared; downloader requires review.")
    if isinstance(links, list) and any(x.get("rel") == "next" and x.get("href") for x in links):
        raise AcquisitionError("API pagination appeared; downloader requires review.")
    meta = obj.get("meta", {})
    if isinstance(meta, dict):
        for key in ("last_page", "lastPage"):
            if number(meta.get(key)) is not None and float(meta[key]) > 1:
                raise AcquisitionError("API metadata indicates more than one page.")
        if number(meta.get("total")) is not None and float(meta["total"]) > len(obj["data"]):
            raise AcquisitionError("API total exceeds returned rows; enumeration is incomplete.")
    return obj["data"]


def indexed(obj):
    result = {}
    for item in data_list(obj):
        if not isinstance(item, dict) or not isinstance(item.get("id"), int) or item["id"] <= 0:
            raise AcquisitionError("Invalid catalogue ID.")
        if item["id"] in result:
            raise AcquisitionError(f"Duplicate catalogue ID: {item['id']}")
        result[item["id"]] = item
    if not result:
        raise AcquisitionError("Empty metadata catalogue; cannot establish completeness.")
    return result


class Archive:
    def __init__(self, root, folder, args, events):
        self.root, self.folder, self.args, self.events = root, folder, args, events
        self.client = HTTPClient(events, args.timeout, args.retries, host="agrometeo.ch")
        self.db = sqlite3.connect(root / "data/state/agrometeo.sqlite3")
        self.db.execute("CREATE TABLE IF NOT EXISTS requests (url TEXT PRIMARY KEY, record TEXT NOT NULL)")
        self.last_request = 0.0

    def fetch(self, url, kind, label, validator, refresh=False):
        old = self.db.execute("SELECT record FROM requests WHERE url=?", (url,)).fetchone()
        if old and not refresh:
            record = json.loads(old[0])
            path = (self.root / record["local_path"]).resolve()
            if self.root not in path.parents:
                raise AcquisitionError("Cached archive path leaves project folder.")
            if path.is_file() and sha256(path) == record["sha256"]:
                obj = json.loads(path.read_bytes().decode("utf-8-sig"))
                validator(obj)
                self.events.log("verified_local_cache", url=url, sha256=record["sha256"])
                return {**record, "status": "verified_local_cache", "checked_at_utc": utcnow()}, obj
            self.events.log("cache_missing_or_corrupt", url=url, local_path=record["local_path"])
        delay = self.args.pause - (time.monotonic() - self.last_request)
        if delay > 0:
            time.sleep(delay)

        def consume(response):
            b = response.read(MAX_BYTES + 1)
            if len(b) > MAX_BYTES:
                raise AcquisitionError("Response exceeds 128 MiB; reduce batch size/chunk years.")
            length = response.headers.get("Content-Length")
            if length is not None and int(length) != len(b):
                raise AcquisitionError("Truncated response: Content-Length differs.")
            obj = json.loads(b.decode("utf-8-sig"))
            validator(obj)
            return b, obj

        try:
            status, headers, (b, obj) = self.client.request(
                url, consume, {"User-Agent": f"Swiss-Precipitation-Thesis-Agrometeo/{VERSION}",
                               "Accept": "application/json"})
        finally:
            self.last_request = time.monotonic()
        digest = hashlib.sha256(b).hexdigest()
        relative = Path("data/raw/agrometeo") / kind / digest / (label + ".json")
        dest = self.root / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists() and sha256(dest) != digest:
            os.replace(dest, dest.with_name(dest.name + ".corrupt-" + uuid.uuid4().hex))
        if not dest.exists():
            temp = dest.with_name(dest.name + "." + uuid.uuid4().hex + ".tmp")
            try:
                with temp.open("wb") as f:
                    f.write(b)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temp, dest)
            finally:
                temp.unlink(missing_ok=True)
        record = dict(url=url, kind=kind, status="downloaded", sha256=digest,
                      size_bytes=len(b), local_path=relative.as_posix(), http_status=status,
                      etag=headers.get("ETag", ""), last_modified=headers.get("Last-Modified", ""),
                      response_date=headers.get("Date", ""), cache_age_seconds=headers.get("Age", ""),
                      downloaded_at_utc=utcnow(), checked_at_utc=utcnow(),
                      checksum_verification="local_sha256_only")
        self.db.execute("INSERT OR REPLACE INTO requests VALUES (?,?)",
                        (url, json.dumps(record, ensure_ascii=False)))
        self.db.commit()
        return record, obj


def period_windows(start, end, years, scale):
    # Monthly requests use complete calendar months; partial boundary months are
    # excluded rather than mislabeled as complete monthly totals.
    if scale == "month":
        if start.day != 1:
            start = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        next_month = (end.replace(day=28) + timedelta(days=4)).replace(day=1)
        if end != next_month - timedelta(days=1):
            end = end.replace(day=1) - timedelta(days=1)
    while start <= end:
        boundary = date(min(9999, start.year + years), 1, 1) - timedelta(days=1)
        stop = min(end, boundary)
        yield start, stop
        start = stop + timedelta(days=1)


def specs_for(args, stations, sensors):
    if args.all_sensors:
        ids = {6} | {v["id"] for s in stations.values() for v in s.get("sensors", [])}
        result = []
        for sid in sorted(ids):
            if sid not in sensors:
                raise AcquisitionError(f"Station-linked sensor {sid} absent from sensor catalogue.")
            s = sensors[sid]
            available = {a["agg"] for a in s.get("aggregations", [])} & {"avg", "min", "max", "sum"}
            if not available:
                available = {s.get("default_agg")}
            if not available <= {"avg", "min", "max", "sum"}:
                raise AcquisitionError(f"Unsupported aggregation for sensor {sid}.")
            result.extend(f"{sid}:{agg}" for agg in sorted(available))
    else:
        result = list(dict.fromkeys(args.sensors or ["6:sum"]))
    for spec in result:
        match = SPEC.fullmatch(spec)
        if not match or int(match[1]) not in sensors:
            raise AcquisitionError(f"Unknown/unsupported sensor specification: {spec}")
        s = sensors[int(match[1])]
        valid = {a["agg"] for a in s.get("aggregations", [])}
        if valid and match[2] not in valid:
            raise AcquisitionError(f"Aggregation {spec} absent from provider definitions.")
    # Verify rather than rely solely on a hard-coded numerical identity.
    rain = sensors.get(6, {})
    if rain.get("name", {}).get("en", "").strip().lower() != "precipitation" or rain.get("default_agg") != "sum":
        raise AcquisitionError("Provider precipitation definition changed; review sensor mapping.")
    return result


def metadata_reports(folder, stations, meteorological, sensors, records):
    conflicts = []
    fields = ("name", "lat_dec", "long_dec", "altitude", "into_service_at", "active")
    source_ids = set(stations) | set(meteorological)
    rows = []
    for sid in sorted(source_ids):
        primary, secondary = stations.get(sid), meteorological.get(sid)
        s = primary or secondary
        for field in fields:
            if primary and secondary and primary.get(field) != secondary.get(field):
                conflicts.append(dict(station_id=sid, field=field,
                    stations_value=primary.get(field), meteo_stations_value=secondary.get(field)))
        tags = s.get("tags", [])
        canton = sorted({t.get("name") for t in tags if t.get("name") in CANTONS})
        service = s.get("into_service_at") or ""
        flags = []
        try:
            parsed = date.fromisoformat(service)
            if parsed.year < 1800 or parsed > datetime.now(timezone.utc).date():
                flags.append("implausible_service_date")
        except ValueError:
            flags.append("missing_or_invalid_service_date")
        lat, lon = number(s.get("lat_dec")), number(s.get("long_dec"))
        if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            flags.append("invalid_coordinates")
        if primary and secondary and any(primary.get(f) != secondary.get(f) for f in fields):
            flags.append("catalogue_disagreement")
        if len(canton) > 1:
            flags.append("multiple_canton_tags")
        rows.append(dict(station_code=f"AGROMETEO_{sid}", station_id=sid,
            station_name=s.get("name", ""), collection="agrometeo", canton="|".join(canton),
            latitude=s.get("lat_dec", ""), longitude=s.get("long_dec", ""), crs="EPSG:4326",
            elevation_m=s.get("altitude", ""), provider_lat_ch=s.get("lat_ch", ""),
            provider_long_ch=s.get("long_ch", ""),
            metadata_service_start=service, metadata_data_until=s.get("data_until", ""),
            metadata_active=s.get("active", ""), replacement_id_uninterpreted=s.get("replacement_id", ""),
            sampling_interval_minutes=s.get("interval", ""),
            station_type=(s.get("type") or {}).get("name", ""),
            owner_name=(s.get("owner") or {}).get("name", ""),
            sensor_ids="|".join(str(v["id"]) for v in s.get("sensors", [])),
            precipitation_sensor_listed=any(v["id"] == 6 for v in s.get("sensors", [])) if primary else "unknown",
            in_stations_catalogue=bool(primary), in_meteo_selector=bool(secondary),
            metadata_flags="|".join(flags), daily_accumulation_window="unverified",
            scientific_qc_status="not_assessed", metadata_source_url=records["stations" if primary else "meteo_stations"]["url"],
            metadata_source_sha256=records["stations" if primary else "meteo_stations"]["sha256"]))
    write_csv(folder / "station_inventory.csv", rows, list(rows[0]))
    write_csv(folder / "metadata_conflicts.csv", conflicts,
              ["station_id", "field", "stations_value", "meteo_stations_value"])
    sensor_rows = []
    for s in sensors.values():
        sensor_rows.append(dict(sensor_id=s["id"], name_en=s.get("name", {}).get("en", ""),
            unit_en=s.get("unit", {}).get("en", ""), default_aggregation=s.get("default_agg", ""),
            aggregations="|".join(a["agg"] for a in s.get("aggregations", [])),
            definition_json=json.dumps(s.get("aggregations", []), ensure_ascii=False)))
    write_csv(folder / "sensor_inventory.csv", sensor_rows, list(sensor_rows[0]))
    return rows, conflicts


def valid_payload(obj, task):
    rows = data_list(obj)
    expected = {f"{sid}_{spec.replace(':', '_')}" for sid in task["station_ids"] for spec in task["sensors"]}
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("date"), str):
            raise AcquisitionError("Data row lacks a string date.")
        raw_date = row["date"]
        try:
            if task["scale"] == "day":
                dt = datetime.strptime(raw_date, "%Y-%m-%d %H:%M:%S")
                if dt.time() != datetime.min.time():
                    raise ValueError("Daily timestamp is not midnight")
            else:
                dt = datetime.strptime(raw_date, "%Y-%m")
        except ValueError as exc:
            raise AcquisitionError(f"Unexpected {task['scale']} timestamp: {raw_date}") from exc
        if raw_date in seen:
            raise AcquisitionError(f"Duplicate timestamp within response: {raw_date}")
        seen.add(raw_date)
        extra = set(row) - {"date"} - expected
        if extra:
            raise AcquisitionError(f"Unexpected station/sensor columns: {sorted(extra)[:4]}")
        for key, value in row.items():
            if key != "date" and (isinstance(value, (dict, list, bool))):
                raise AcquisitionError(f"Unsupported value type in {key}.")
    return rows


def extract(db, obj, task, record):
    excluded = 0
    for row_index, row in enumerate(obj["data"], 1):
        stamp = row["date"][:10] if task["scale"] == "day" else row["date"]
        lower, upper = (task["from"], task["to"]) if task["scale"] == "day" else (task["from"][:7], task["to"][:7])
        if not lower <= stamp <= upper:
            excluded += 1
            continue
        for sid in task["station_ids"]:
            values = {spec: row[f"{sid}_{spec.replace(':', '_')}"] for spec in task["sensors"]
                      if f"{sid}_{spec.replace(':', '_')}" in row}
            if not values:
                continue
            # A duplicate across responses must be reviewed, never silently overwritten.
            try:
                db.execute("INSERT INTO observations VALUES (?,?,?,?,?,?,?)",
                    (sid, task["scale"], stamp, json.dumps(values, sort_keys=True, ensure_ascii=False),
                     record["local_path"], record["sha256"], row_index))
            except sqlite3.IntegrityError as exc:
                raise AcquisitionError(f"Overlapping extracted date: station {sid}, {stamp}.") from exc
    db.commit()
    return excluded


def export_observations(db, root, folder, selected, specs, scales, start, end, failed):
    result = []
    derived = root / "data/derived/agrometeo" / folder.name
    target_start, target_end = date(1951, 1, 1), date(2023, 12, 31)
    for scale in scales:
        for sid in selected:
            path = derived / {"day": "daily", "month": "monthly"}[scale] / f"AGROMETEO_{sid}.csv"
            path.parent.mkdir(parents=True, exist_ok=True)
            stats = {spec: Counter() for spec in specs}
            first, last = {}, {}
            with path.open("w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f, delimiter=";")
                writer.writerow(["station_code", "date", *[v.replace(":", "_") for v in specs],
                                 "source_json", "source_sha256", "source_row"])
                for stamp, values_json, source, digest, source_row in db.execute(
                    "SELECT stamp,values_json,source,digest,source_row FROM observations "
                    "WHERE station=? AND scale=? ORDER BY stamp", (sid, scale)):
                    values = json.loads(values_json)
                    writer.writerow([f"AGROMETEO_{sid}", stamp, *[values.get(v) for v in specs],
                                     source, digest, source_row])
                    for spec in specs:
                        if spec not in values:
                            continue
                        count = stats[spec]
                        count["returned_cells"] += 1
                        value = values[spec]
                        n = number(value)
                        if n is None:
                            count["null_or_nonnumeric"] += 1
                            continue
                        count["finite_numeric"] += 1
                        first.setdefault(spec, stamp)
                        last[spec] = stamp
                        if spec == "6:sum" and n < 0:
                            count["negative_precipitation"] += 1
                        inside = "1951-01-01" <= stamp <= "2023-12-31" if scale == "day" else "1951-01" <= stamp <= "2023-12"
                        if inside:
                            count["target_finite_numeric"] += 1
                            if spec == "6:sum" and n >= 0:
                                count["target_nonnegative_precipitation"] += 1
            file_digest = sha256(path)
            for spec in specs:
                c = stats[spec]
                expected = (target_end - target_start).days + 1 if scale == "day" else 73 * 12
                full_target = start <= target_start and end >= target_end
                result.append(dict(station_code=f"AGROMETEO_{sid}", station_id=sid,
                    scale=scale, sensor_spec=spec, returned_cells=c["returned_cells"],
                    finite_numeric_cells=c["finite_numeric"], null_or_nonnumeric_cells=c["null_or_nonnumeric"],
                    negative_precipitation_cells=c["negative_precipitation"] if spec == "6:sum" else "",
                    first_numeric_date=first.get(spec, ""), last_numeric_date=last.get(spec, ""),
                    target_period="1951-2023", target_expected_periods=expected,
                    target_numeric_periods=c["target_finite_numeric"],
                    target_nonnegative_precipitation_periods=c["target_nonnegative_precipitation"] if spec == "6:sum" else "",
                    target_without_numeric_value=expected - c["target_finite_numeric"] if full_target else "",
                    entire_target_requested=full_target,
                    requests_complete=not any(t["scale"] == scale and sid in t["station_ids"] for t in failed),
                    derived_csv=path.relative_to(root).as_posix(), derived_sha256=file_digest,
                    scientific_qc_status="not_assessed"))
    if result:
        write_csv(folder / "station_coverage.csv", result, list(result[0]))
    return derived


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--output", type=Path, default=Path("."), help="Existing thesis project folder.")
    p.add_argument("--start", type=iso_date, default=date(1951, 1, 1))
    p.add_argument("--end", type=iso_date, default=datetime.now(timezone.utc).date() - timedelta(days=1),
                   help="Last requested day; default yesterday in UTC. Current partial month excluded.")
    p.add_argument("--stations", type=int, nargs="+", help="Optional public station IDs for a pilot.")
    p.add_argument("--max-stations", type=int, help="Pilot: first N station IDs, sorted numerically.")
    p.add_argument("--metadata-only", action="store_true")
    p.add_argument("--scales", choices=("day", "month"), nargs="+", default=["day", "month"])
    group = p.add_mutually_exclusive_group()
    group.add_argument("--sensors", nargs="+", help="Explicit specs, e.g. 6:sum 1:avg. Default 6:sum.")
    group.add_argument("--all-sensors", action="store_true", help="All currently station-linked sensors and their non-cumulative published aggregations.")
    p.add_argument("--batch-size", type=int, default=10, help="Station IDs per request, 1–20.")
    p.add_argument("--chunk-years", type=int, default=5, help="Calendar years per request, 1–10.")
    p.add_argument("--pause", type=float, default=1.0, help="Pause after each network request, 0.5–30 seconds.")
    p.add_argument("--timeout", type=float, default=60)
    p.add_argument("--retries", type=int, default=3)
    p.add_argument("--refresh", action="store_true", help="Requery cached observations; otherwise reuse hash-verified snapshots.")
    p.add_argument("--version", action="version", version=VERSION)
    return p


def run(args):
    if (args.start > args.end or args.end.year >= 9999 or args.start.year < 1800
            or args.end >= datetime.now(timezone.utc).date()
            or not 1 <= args.batch_size <= 20 or not 1 <= args.chunk_years <= 10
            or not math.isfinite(args.pause) or not 0.5 <= args.pause <= 30
            or not math.isfinite(args.timeout) or not 1 <= args.timeout <= 300
            or not 0 <= args.retries <= 6 or (args.max_stations is not None and args.max_stations < 1)):
        raise AcquisitionError("Invalid dates/options. End must precede today's UTC date; check --help for bounds.")
    root = args.output.resolve()
    with ProjectLock(root / "data/state/agrometeo.lock"):
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + uuid.uuid4().hex[:8]
        folder = root / "runs/agrometeo" / run_id
        folder.mkdir(parents=True)
        events = Events(folder / "events.jsonl")
        summary = dict(run_id=run_id, status="running", started_at_utc=utcnow(), scientific_qc_performed=False)
        config = {**vars(args), "output": str(root), "start": str(args.start), "end": str(args.end),
            "script_version": VERSION, "script_sha256": sha256(Path(__file__)),
            "shared_helper_sha256": sha256(Path(__file__).with_name("meteoswiss_download.py")),
            "python_version": sys.version, "api_base": API_BASE, "measured": 1,
            "cache_policy": "requery" if args.refresh else "reuse_verified_local_snapshot",
            "daily_accumulation_window": "unverified", "source": "Agrometeo / Agroscope"}
        atomic_json(folder / "configuration.json", config)
        atomic_json(folder / "summary.json", summary)
        print(f"Run: {folder}", flush=True)
        archive = None
        observations = None
        manifest_file = None
        plan, failures, results = [], [], []
        exit_code = 1
        try:
            archive = Archive(root, folder, args, events)
            observations = sqlite3.connect(folder / "extraction.sqlite3")
            observations.execute("CREATE TABLE observations (station INTEGER, scale TEXT, stamp TEXT, "
                "values_json TEXT, source TEXT, digest TEXT, source_row INTEGER, PRIMARY KEY(station,scale,stamp))")
            fields = ["url", "kind", "status", "station_ids", "scale", "from", "to", "sensors", "measured",
                      "sha256", "size_bytes", "local_path", "http_status", "etag", "last_modified",
                      "response_date", "cache_age_seconds",
                      "checksum_verification", "downloaded_at_utc", "checked_at_utc", "response_rows",
                      "out_of_range_rows_excluded", "error"]
            manifest_file = (folder / "download_manifest.csv").open("w", encoding="utf-8-sig", newline="")
            writer = csv.DictWriter(manifest_file, fields, delimiter=";", extrasaction="ignore")
            writer.writeheader()
            records, objects = {}, {}
            metadata = [("stations", "stations?" + urllib.parse.urlencode({"include": "sensors,owner,type,tags"})),
                        ("meteo_stations", "meteo/stations"), ("sensors", "sensors")]
            for name, endpoint in metadata:
                print(f"Metadata: {name}", flush=True)
                record, obj = archive.fetch(f"{API_BASE}/{endpoint}", "metadata", name, indexed, refresh=True)
                records[name], objects[name] = record, indexed(obj)
                writer.writerow(record)
                manifest_file.flush()
            stations, meteorological, sensors = objects["stations"], objects["meteo_stations"], objects["sensors"]
            inventory, conflicts = metadata_reports(folder, stations, meteorological, sensors, records)
            all_stations = {**meteorological, **stations}
            selected = sorted(all_stations)
            if args.stations:
                unknown = set(args.stations) - set(selected)
                if unknown:
                    raise AcquisitionError(f"IDs not in public catalogues: {sorted(unknown)}")
                selected = [sid for sid in selected if sid in args.stations]
            if args.max_stations:
                selected = selected[:args.max_stations]
            specs = specs_for(args, all_stations, sensors)
            atomic_json(folder / "metadata_manifest.json", records)
            atomic_json(folder / "requested_sensor_specs.json", specs)
            print(f"Public catalogue union: {len(all_stations)} stations; selected {len(selected)}; sensors {', '.join(specs)}", flush=True)
            if not args.metadata_only:
                for offset in range(0, len(selected), args.batch_size):
                    batch = selected[offset:offset + args.batch_size]
                    for scale in dict.fromkeys(args.scales):
                        for start, end in period_windows(args.start, args.end, args.chunk_years, scale):
                            params = {"from": str(start), "to": str(end), "scale": scale,
                                      "sensors": ",".join(specs), "stations": ",".join(map(str, batch)), "measured": "1"}
                            plan.append({**params, "station_ids": batch, "sensors": specs,
                                         "url": f"{API_BASE}/meteo/data?" + urllib.parse.urlencode(params)})
            atomic_json(folder / "download_plan.json", plan)
            for i, task in enumerate(plan, 1):
                batch_id = hashlib.sha256(",".join(map(str, task["station_ids"])).encode()).hexdigest()[:10]
                label = f"batch_{batch_id}_{task['from']}_{task['to']}"
                kind = {"day": "daily", "month": "monthly"}[task["scale"]]
                try:
                    record, obj = archive.fetch(task["url"], kind, label,
                        lambda obj: valid_payload(obj, task), refresh=args.refresh)
                    # Keep every raw byte, including any out-of-range periods.
                    excluded = extract(observations, obj, task, record)
                    record.update(response_rows=len(obj["data"]), out_of_range_rows_excluded=excluded)
                except Exception as exc:
                    observations.rollback()
                    record = dict(url=task["url"], kind=kind, status="error", checked_at_utc=utcnow(),
                                  error=f"{type(exc).__name__}: {exc}")
                    failures.append({**task, "error": record["error"]})
                    events.log("request_or_extraction_failed", **record)
                record.update({k: task[k] for k in ("station_ids", "scale", "from", "to", "sensors", "measured")})
                results.append(record)
                writer.writerow(record)
                manifest_file.flush()
                print(f"  {i}/{len(plan)} {kind} {task['from']}..{task['to']} "
                      f"IDs {task['station_ids'][0]}..{task['station_ids'][-1]}: {record['status']} "
                      f"({len(failures)} failures)", flush=True)
            derived = None
            if not args.metadata_only:
                derived = export_observations(observations, root, folder, selected, specs,
                                              list(dict.fromkeys(args.scales)), args.start, args.end, failures)
            atomic_json(folder / "errors.json", failures)
            limited = bool(args.metadata_only or args.stations or args.max_stations)
            summary.update(status="failed_or_incomplete" if failures else
                ("completed_selected_scope" if limited else "completed_requested_public_scope"),
                scope="metadata_only" if args.metadata_only else ("pilot" if limited else "public_catalogue_union"),
                public_stations=len(stations), meteorological_selector_stations=len(meteorological),
                catalogue_union_stations=len(all_stations), selected_station_ids=selected,
                sensor_specs=specs, catalogue_conflicting_fields=len(conflicts),
                metadata_flagged_stations=sum(bool(s["metadata_flags"]) for s in inventory),
                planned_requests=len(plan), request_status_counts=dict(Counter(r["status"] for r in results)),
                failed_requests=len(failures),
                derived_folder=str(derived) if derived else None,
                historical_station_catalogue_complete="not_established",
                accumulation_window_verified=False,
                note="Completion refers to requested API responses, not absence of missing data, retired-station completeness or scientific suitability.")
            exit_code = 1 if failures else 0
        except KeyboardInterrupt:
            summary.update(status="interrupted")
            events.log("interrupted")
            exit_code = 130
        except Exception as exc:
            summary.update(status="failed", error=f"{type(exc).__name__}: {exc}")
            events.log("run_failed", error=summary["error"])
            exit_code = 1
        finally:
            summary.update(finished_at_utc=utcnow(), recorded_request_results=len(results))
            atomic_json(folder / "summary.json", summary)
            if manifest_file:
                manifest_file.close()
            if observations:
                observations.close()
            if archive:
                archive.db.close()
            events.close()
            print(f"Status: {summary['status']}. Reports: {folder}", flush=True)
        return exit_code


def main():
    try:
        return run(parser().parse_args())
    except (AcquisitionError, OSError, sqlite3.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
