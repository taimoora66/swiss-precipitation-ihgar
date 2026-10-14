#!/usr/bin/env python3
"""Archive official MeteoSwiss station metadata and daily/monthly CSV assets.

Python 3.10+, standard library only. No transformations, merging or gap filling.
Source: MeteoSwiss; see README.md for scope, formats and reproduction commands.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import csv
import hashlib
import http.client
import io
import json
import os
import random
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

VERSION = "1.0.0"
API_BASE = "https://data.geo.admin.ch/api/stac/v1"
COLLECTIONS = ("ogd-nime", "ogd-smn", "ogd-smn-precip")
MAX_BYTES = 256 * 1024 * 1024
SAFE = re.compile(r"^[A-Za-z0-9_.-]+$")
DATA_NAME = re.compile(r"_(d|m)(?:_(historical|recent|now))?\.csv$", re.I)
META_NAME = re.compile(r"_meta_([A-Za-z0-9_-]+)\.csv$", re.I)


class AcquisitionError(Exception):
    """A request or published resource cannot be safely accepted."""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_name(value: str) -> str:
    if not SAFE.fullmatch(value) or value in (".", ".."):
        raise AcquisitionError(f"Unsafe catalogue identifier: {value!r}")
    return value


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def expected_sha(value: str) -> str | None:
    """STAC multihash 0x12/SHA256 + 0x20/32 bytes, or plain SHA256 hex."""
    value = value.lower()
    if re.fullmatch(r"1220[0-9a-f]{64}", value):
        return value[4:]
    if re.fullmatch(r"[0-9a-f]{64}", value):
        return value
    return None


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fields, delimiter=";", extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


class Events:
    def __init__(self, path: Path):
        self.file = path.open("a", encoding="utf-8", buffering=1)
        self.lock = threading.Lock()

    def log(self, event: str, **details) -> None:
        with self.lock:
            self.file.write(json.dumps({"time_utc": utcnow(), "event": event,
                                        **details}, ensure_ascii=False) + "\n")

    def close(self) -> None:
        self.file.close()


class ProjectLock:
    """OS advisory lock; released by the OS even if this process crashes."""
    def __init__(self, path: Path):
        self.path = path
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        self.file.seek(0, os.SEEK_END)
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise AcquisitionError("Another downloader is using this output folder.") from exc
        return self

    def __exit__(self, *_):
        if os.name == "nt":
            import msvcrt
            self.file.seek(0)
            msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        self.file.close()


class HTTPClient:
    def __init__(self, events: Events, timeout=45, retries=3, host=None):
        self.events, self.timeout, self.retries = events, timeout, retries
        self.host = host or urllib.parse.urlsplit(API_BASE).hostname
        self.stop = threading.Event()

    def check_url(self, url: str) -> None:
        p = urllib.parse.urlsplit(url)
        local_test = p.hostname in ("127.0.0.1", "localhost") and self.host == p.hostname
        if (p.hostname != self.host or p.username or p.password
                or (p.scheme != "https" and not (local_test and p.scheme == "http"))):
            raise AcquisitionError(f"Unexpected catalogue/download origin: {url}")

    def request(self, url: str, consumer, headers=None):
        self.check_url(url)
        request_headers = {"User-Agent": f"Swiss-Precipitation-Thesis/{VERSION}",
                           "Accept-Encoding": "identity", **(headers or {})}
        for attempt in range(1, self.retries + 2):
            if self.stop.is_set():
                raise AcquisitionError("Download cancelled.")
            retry_after = None
            try:
                req = urllib.request.Request(url, headers=request_headers)
                with urllib.request.urlopen(req, timeout=self.timeout) as response:
                    self.check_url(response.url)
                    result = consumer(response)
                    self.events.log("request_ok", url=url, attempt=attempt,
                                    http_status=response.status)
                    return response.status, dict(response.headers), result
            except urllib.error.HTTPError as exc:
                if exc.code == 304:
                    self.events.log("not_modified", url=url, attempt=attempt, http_status=304)
                    exc.close()
                    return 304, dict(exc.headers), None
                retryable = exc.code in (408, 425, 429, 500, 502, 503, 504)
                status = exc.code
                retry_after = exc.headers.get("Retry-After")
                error = str(exc)
                exc.close()
            except (urllib.error.URLError, TimeoutError, ConnectionError,
                    http.client.IncompleteRead, AcquisitionError, json.JSONDecodeError) as exc:
                retryable, status, error = True, None, f"{type(exc).__name__}: {exc}"
            self.events.log("request_error", url=url, attempt=attempt,
                            http_status=status, error=error,
                            will_retry=retryable and attempt <= self.retries)
            if not retryable or attempt > self.retries:
                raise AcquisitionError(f"{url}: {error}")
            delay = min(30.0, 2 ** (attempt - 1) + random.uniform(0, 1))
            if retry_after:
                try:
                    delay = float(retry_after)
                except ValueError:
                    try:
                        delay = (parsedate_to_datetime(retry_after)
                                 - datetime.now(timezone.utc)).total_seconds()
                    except (ValueError, TypeError):
                        pass
            if self.stop.wait(max(0.0, min(60.0, delay))):
                raise AcquisitionError("Download cancelled during retry.")

    def json(self, url: str) -> dict:
        def consume(response):
            b = response.read(16 * 1024 * 1024 + 1)
            if len(b) > 16 * 1024 * 1024:
                raise AcquisitionError("Catalogue response exceeds 16 MiB.")
            j = json.loads(b.decode("utf-8-sig"))
            if not isinstance(j, dict):
                raise AcquisitionError("Catalogue response is not a JSON object.")
            return j
        return self.request(url, consume)[2]


@dataclass(frozen=True)
class Asset:
    collection: str
    station: str
    asset_key: str
    url: str
    kind: str
    period: str
    catalogue_checksum: str = ""
    catalogue_updated: str = ""

    @property
    def filename(self):
        return safe_name(urllib.parse.unquote(urllib.parse.urlsplit(self.url).path.rsplit("/", 1)[-1]))


def select_asset(collection: str, station: str, key: str, value: dict) -> Asset | None:
    if not isinstance(value, dict) or not isinstance(value.get("href"), str):
        raise AcquisitionError(f"Malformed asset in {collection}/{station}: {key}")
    name = urllib.parse.unquote(urllib.parse.urlsplit(value["href"]).path.rsplit("/", 1)[-1])
    meta, data = META_NAME.search(name), DATA_NAME.search(name)
    if meta:
        kind, period, station = "metadata", meta[1].lower(), "_collection"
    elif data:
        kind = {"d": "daily", "m": "monthly"}[data[1].lower()]
        period = (data[2] or "full").lower()
        if period == "now":
            return None
    else:
        return None
    safe_name(collection)
    safe_name(station)
    safe_name(name)
    return Asset(collection, station.lower(), key, value["href"], kind, period,
                 str(value.get("file:checksum", "")), str(value.get("updated", "")))


def discover(client: HTTPClient, collection: str, folder: Path):
    folder.mkdir(parents=True, exist_ok=True)
    url = f"{API_BASE}/collections/ch.meteoschweiz.{collection}"
    card = client.json(url)
    atomic_json(folder / "collection.json", card)
    if card.get("id") != f"ch.meteoschweiz.{collection}":
        raise AcquisitionError("Collection identity differs from the requested collection.")
    assets = []
    for key, value in card.get("assets", {}).items():
        a = select_asset(collection, "_collection", key, value)
        if a and a.kind == "metadata":
            assets.append(a)
    required = {"stations", "parameters", "datainventory"}
    missing = required - {a.period for a in assets}
    if missing:
        raise AcquisitionError(f"Required metadata assets absent: {sorted(missing)}")
    items, seen, page = {}, set(), 0
    url = url + "/items?limit=100"
    while url:
        if url in seen or page >= 10000:
            raise AcquisitionError("Pagination loop or excessive catalogue pages.")
        seen.add(url)
        j = client.json(url)
        page += 1
        atomic_json(folder / f"items_page_{page:04d}.json", j)
        if j.get("type") != "FeatureCollection" or not isinstance(j.get("features"), list):
            raise AcquisitionError("Invalid STAC item page; enumeration is incomplete.")
        for item in j["features"]:
            station = safe_name(str(item.get("id", ""))).lower()
            if station in items:
                raise AcquisitionError(f"Repeated STAC item across pages: {station}")
            if item.get("collection") != card["id"]:
                raise AcquisitionError(f"Wrong item collection: {station}")
            items[station] = item
            for key, value in item.get("assets", {}).items():
                a = select_asset(collection, station, key, value)
                if a:
                    assets.append(a)
        next_links = [link for link in j.get("links", []) if link.get("rel") == "next"]
        if len(next_links) > 1:
            raise AcquisitionError("Ambiguous pagination links.")
        if next_links and next_links[0].get("method", "GET").upper() != "GET":
            raise AcquisitionError("Unsupported pagination method.")
        url = urllib.parse.urljoin(url, next_links[0]["href"]) if next_links else None
    unique = {}
    for a in assets:
        if a.url in unique and a != unique[a.url]:
            raise AcquisitionError(f"Conflicting duplicate asset URL: {a.url}")
        unique[a.url] = a
    return sorted(unique.values(), key=lambda a: (a.kind, a.station, a.asset_key)), items


def csv_encoding(path: Path) -> str:
    with path.open("rb") as f:
        prefix = f.read(512)
    if prefix.lstrip().lower().startswith((b"<html", b"<!doctype", b"<?xml")) or b"\x00" in prefix:
        raise AcquisitionError("HTML/XML/binary response instead of the expected CSV.")
    return "utf-8-sig" if prefix.startswith(b"\xef\xbb\xbf") else "cp1252"


def validate_csv(path: Path, asset: Asset) -> dict:
    encoding = csv_encoding(path)
    required = {"station_abbr", "reference_timestamp"}
    if asset.kind == "metadata":
        required = {"stations": {"station_abbr", "station_name"},
                    "parameters": {"parameter_shortname", "parameter_granularity"},
                    "datainventory": {"station_abbr", "parameter_shortname", "data_since", "data_till"}
                    }.get(asset.period, set())
    count, start, end = 0, None, None
    try:
        with path.open("r", encoding=encoding, newline="") as f:
            reader = csv.reader(f, delimiter=";", strict=True)
            header = next(reader, [])
            if len(header) < 2 or len(set(header)) != len(header) or not required.issubset(header):
                raise AcquisitionError(f"Unexpected CSV schema: {header}")
            for row in reader:
                if not row:  # Ignore blank physical lines, never fabricate observations.
                    continue
                if len(row) != len(header):
                    raise AcquisitionError(f"CSV row {count + 2} has an inconsistent column count.")
                if asset.kind != "metadata":
                    code = row[header.index("station_abbr")].strip().lower()
                    if code != asset.station:
                        raise AcquisitionError(f"Unexpected station code {code!r} in {asset.station} asset.")
                    ts = row[header.index("reference_timestamp")]
                    try:
                        dt = datetime.strptime(ts, "%d.%m.%Y %H:%M")
                    except ValueError as exc:
                        raise AcquisitionError(f"Invalid reference timestamp: {ts!r}") from exc
                    start = dt if start is None else min(start, dt)
                    end = dt if end is None else max(end, dt)
                count += 1
    except (UnicodeError, csv.Error) as exc:
        raise AcquisitionError(f"Invalid CSV encoding/structure: {exc}") from exc
    if asset.kind == "metadata" and asset.period in ("stations", "parameters", "datainventory") and count == 0:
        raise AcquisitionError("Required metadata table contains no records.")
    return {"row_count": count, "columns": header, "encoding": encoding,
            "first_timestamp": start.isoformat() if start else "",
            "last_timestamp": end.isoformat() if end else ""}


def download(client: HTTPClient, asset: Asset, root: Path, temp_dir: Path,
             previous: dict | None, refresh: bool = False) -> dict:
    base = asdict(asset)
    expected = expected_sha(asset.catalogue_checksum)
    verified_previous = False
    if previous:
        old = (root / previous["local_path"]).resolve()
        if root not in old.parents:
            raise AcquisitionError("Cached path escapes output root.")
        verified_previous = old.is_file() and sha256(old) == previous["sha256"]
        if not verified_previous:
            client.events.log("cache_invalid", url=asset.url, path=str(old))
    if verified_previous and not refresh and expected == previous["sha256"]:
        return {**previous, **base, "status": "verified_cached", "checked_at_utc": utcnow()}
    headers = {}
    if verified_previous and not refresh and not expected:
        if previous.get("etag"):
            headers["If-None-Match"] = previous["etag"]
        elif previous.get("last_modified"):
            headers["If-Modified-Since"] = previous["last_modified"]

    def consume(response):
        declared = response.headers.get("Content-Length")
        try:
            length = int(declared) if declared is not None else None
        except ValueError as exc:
            raise AcquisitionError("Invalid Content-Length.") from exc
        if length is not None and not 0 <= length <= MAX_BYTES:
            raise AcquisitionError("CSV exceeds allowed size or has an invalid length.")
        temp = temp_dir / (uuid.uuid4().hex + ".part")
        h, size = hashlib.sha256(), 0
        try:
            with temp.open("xb") as f:
                while b := response.read(64 * 1024):
                    if client.stop.is_set():
                        raise AcquisitionError("Download cancelled during transfer.")
                    size += len(b)
                    if size > MAX_BYTES:
                        raise AcquisitionError("CSV exceeds 256 MiB safety limit.")
                    f.write(b)
                    h.update(b)
                f.flush()
                os.fsync(f.fileno())
            if length is not None and size != length:
                raise AcquisitionError(f"Truncated transfer: {size} bytes; expected {length}.")
            digest = h.hexdigest()
            if expected and digest != expected:
                raise AcquisitionError(f"Catalogue checksum mismatch: expected {expected}; received {digest}.")
            stats = validate_csv(temp, asset)
            relative = Path("data/raw/meteoswiss") / asset.collection / asset.kind
            if asset.kind != "metadata":
                relative /= asset.station
            relative = relative / digest / asset.filename
            dest = root / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                if sha256(dest) != digest:
                    # Quarantine a corrupted copy; never silently overwrite its evidence.
                    bad = dest.with_name(dest.name + ".corrupt-" + uuid.uuid4().hex)
                    os.replace(dest, bad)
                    client.events.log("corrupt_file_quarantined", path=str(bad), url=asset.url)
                    os.replace(temp, dest)
            else:
                os.replace(temp, dest)
            return {**base, **stats, "status": "downloaded", "sha256": digest,
                    "size_bytes": size, "local_path": relative.as_posix(),
                    "etag": response.headers.get("ETag", ""),
                    "last_modified": response.headers.get("Last-Modified", ""),
                    "checksum_verification": "matched_sha256" if expected else "local_sha256_only",
                    "downloaded_at_utc": utcnow(), "checked_at_utc": utcnow()}
        finally:
            temp.unlink(missing_ok=True)

    status, _, result = client.request(asset.url, consume, headers)
    if status == 304:
        if not verified_previous or (expected and expected != previous["sha256"]):
            raise AcquisitionError("HTTP 304 does not validate the required local/catalogue version.")
        return {**previous, **base, "status": "not_modified", "checked_at_utc": utcnow()}
    return result


def metadata_rows(root: Path, results: list[dict], collection: str, kind: str):
    matches = [r for r in results if r["collection"] == collection and r["kind"] == "metadata"
               and r["period"] == kind and r["status"] != "error"]
    if len(matches) != 1:
        return None
    path = root / matches[0]["local_path"]
    with path.open(encoding=matches[0]["encoding"], newline="") as f:
        return list(csv.DictReader(f, delimiter=";"))


def availability(root, catalogues, results, selected_urls):
    rows = []
    for collection, (assets, items) in catalogues.items():
        stations = metadata_rows(root, results, collection, "stations")
        parameters = metadata_rows(root, results, collection, "parameters")
        inventory = metadata_rows(root, results, collection, "datainventory")
        meta = {r["station_abbr"].lower(): r for r in stations or []}
        gran = {r["parameter_shortname"]: r["parameter_granularity"].lower()
                for r in parameters or []}
        expected = {}
        for r in inventory or []:
            expected.setdefault(r["station_abbr"].lower(), set()).add(gran.get(r["parameter_shortname"], ""))
        for station in sorted(set(items) | set(meta) | set(expected)):
            here = [a for a in assets if a.station == station and a.kind != "metadata"]
            published = {a.kind for a in here}
            absent = [kind for code, kind in [("d", "daily"), ("m", "monthly")]
                      if code in expected.get(station, set()) and kind not in published]
            rows.append({"collection": collection, "station_code": station.upper(),
                         "station_name": meta.get(station, {}).get("station_name", ""),
                         "canton": meta.get(station, {}).get("station_canton", ""),
                         "in_station_metadata": station in meta,
                         "in_stac_catalogue": station in items,
                         "daily_assets": sum(a.kind == "daily" for a in here),
                         "monthly_assets": sum(a.kind == "monthly" for a in here),
                         "selected_for_download": any(a.url in selected_urls for a in here),
                         "inventory_without_published_asset": ",".join(absent),
                         "metadata_reconciliation_available": all(x is not None for x in (stations, parameters, inventory))})
    return rows


def parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--output", type=Path, default=Path("."), help="Project folder (default current folder).")
    p.add_argument("--collections", nargs="+", choices=COLLECTIONS, default=list(COLLECTIONS))
    p.add_argument("--stations", nargs="+", help="Optional station codes for a pilot; default ALL catalogue stations.")
    p.add_argument("--max-stations", type=int, help="Pilot limit per collection, after sorting station codes.")
    p.add_argument("--metadata-only", action="store_true", help="Discover all stations but download metadata only.")
    p.add_argument("--workers", type=int, default=2, help="Simultaneous downloads, 1–4 (default 2).")
    p.add_argument("--timeout", type=float, default=45, help="Socket timeout in seconds (default 45).")
    p.add_argument("--retries", type=int, default=3, help="Additional attempts per request, 0–6 (default 3).")
    p.add_argument("--refresh", action="store_true", help="GET all selected assets even if a cache matches.")
    p.add_argument("--version", action="version", version=VERSION)
    return p


def run(args) -> int:
    root = args.output.resolve()
    if not 1 <= args.workers <= 4 or not 0 <= args.retries <= 6 or not 0 < args.timeout <= 300:
        raise AcquisitionError("Invalid workers, retries or timeout setting.")
    if args.max_stations is not None and args.max_stations < 1:
        raise AcquisitionError("--max-stations must be positive.")
    wanted = {safe_name(s).lower() for s in args.stations or []}
    root.mkdir(parents=True, exist_ok=True)
    with ProjectLock(root / "data/state/meteoswiss.lock"):
        runid = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + uuid.uuid4().hex[:8]
        folder = root / "runs/meteoswiss" / runid
        folder.mkdir(parents=True)
        temp_dir = folder / "temp"
        temp_dir.mkdir()
        events = Events(folder / "events.jsonl")
        client = HTTPClient(events, args.timeout, args.retries)
        config = {**vars(args), "output": str(root), "script_version": VERSION,
                  "api_base": API_BASE, "source": "MeteoSwiss", "python_version": sys.version,
                  "script_sha256": sha256(Path(__file__)), "started_at_utc": utcnow()}
        atomic_json(folder / "configuration.json", config)
        summary = {"run_id": runid, "status": "running", "started_at_utc": utcnow()}
        atomic_json(folder / "summary.json", summary)
        print(f"Run: {folder}", flush=True)
        db = None
        try:
            db = sqlite3.connect(root / "data/state/meteoswiss.sqlite3")
            db.execute("CREATE TABLE IF NOT EXISTS assets (url TEXT PRIMARY KEY, record TEXT NOT NULL)")
            cached = {url: json.loads(record) for url, record in db.execute("SELECT url, record FROM assets")}
        except (sqlite3.Error, json.JSONDecodeError) as exc:
            summary.update(status="failed", finished_at_utc=utcnow(), error=f"State database error: {exc}")
            atomic_json(folder / "summary.json", summary)
            events.log("initialization_failed", error=summary["error"])
            if db is not None:
                db.close()
            events.close()
            print(summary["error"], file=sys.stderr)
            return 1
        results, errors, catalogues, plan = [], [], {}, []
        exit_code = 1
        try:
            for collection in dict.fromkeys(args.collections):
                print(f"Discovering {collection} ...", flush=True)
                try:
                    assets, items = discover(client, collection, folder / "catalogue" / collection)
                    catalogues[collection] = assets, items
                    selected = sorted(s for s in items if not wanted or s in wanted)
                    if args.max_stations:
                        selected = selected[:args.max_stations]
                    plan.extend(a for a in assets if a.kind == "metadata" or
                                (not args.metadata_only and a.station in selected))
                    print(f"  {len(items)} catalogue stations; {len(assets)} daily/monthly/metadata assets.", flush=True)
                except Exception as exc:
                    errors.append({"stage": "discovery", "collection": collection,
                                   "error": f"{type(exc).__name__}: {exc}"})
                    events.log("discovery_failed", **errors[-1])
            found = {station for _, items in catalogues.values() for station in items}
            if wanted - found:
                errors.append({"stage": "selection", "error": f"Station codes not found: {sorted(wanted - found)}"})
            atomic_json(folder / "download_plan.json", [asdict(a) for a in plan])
            manifest_fields = list(Asset.__dataclass_fields__) + ["status", "sha256", "size_bytes", "local_path",
                "etag", "last_modified", "checksum_verification", "encoding", "row_count",
                "first_timestamp", "last_timestamp", "downloaded_at_utc", "checked_at_utc", "error"]
            with (folder / "download_manifest.csv").open("w", encoding="utf-8-sig", newline="") as f:
                writer = csv.DictWriter(f, manifest_fields, delimiter=";", extrasaction="ignore")
                writer.writeheader()
                f.flush()
                executor = concurrent.futures.ThreadPoolExecutor(max_workers=args.workers)
                try:
                    futures = {executor.submit(download, client, a, root, temp_dir,
                                               cached.get(a.url), args.refresh): a for a in plan}
                    for future in concurrent.futures.as_completed(futures):
                        a = futures[future]
                        try:
                            r = future.result()
                            db.execute("INSERT OR REPLACE INTO assets VALUES (?, ?)",
                                       (a.url, json.dumps(r, ensure_ascii=False)))
                            db.commit()
                        except Exception as exc:
                            r = {**asdict(a), "status": "error", "checked_at_utc": utcnow(),
                                 "error": f"{type(exc).__name__}: {exc}"}
                            events.log("asset_failed", **r)
                        results.append(r)
                        writer.writerow(r)
                        f.flush()
                        if len(results) % 10 == 0 or len(results) == len(plan):
                            print(f"  Completed {len(results)}/{len(plan)} assets; "
                                  f"{sum(x['status'] == 'error' for x in results)} failures.", flush=True)
                except BaseException:
                    client.stop.set()
                    executor.shutdown(wait=True, cancel_futures=True)
                    raise
                finally:
                    executor.shutdown(wait=True)
            rows = availability(root, catalogues, results, {a.url for a in plan})
            if rows:
                write_csv(folder / "station_availability.csv", rows, list(rows[0]))
            failures = [r for r in results if r["status"] == "error"]
            reconciliation_gaps = [r for r in rows if r["inventory_without_published_asset"]]
            atomic_json(folder / "errors.json", {"discovery_and_selection": errors, "assets": failures})
            partial = bool(args.stations or args.max_stations or args.metadata_only)
            summary.update(status="failed_or_incomplete" if errors or failures else
                           ("completed_selected_scope" if partial else "completed_available_assets"),
                           finished_at_utc=utcnow(),
                           scope="metadata_only" if args.metadata_only else ("pilot" if partial else "all_stations"),
                           complete_catalogue_collections=list(catalogues),
                           catalogue_stations_by_collection={c: len(i) for c, (_, i) in catalogues.items()},
                           planned_assets=len(plan), asset_status_counts=dict(Counter(r["status"] for r in results)),
                           failed_assets=len(failures), discovery_or_selection_errors=len(errors),
                           metadata_inventory_entries_without_published_daily_or_monthly_assets=len(reconciliation_gaps),
                           scientific_qc_performed=False)
            exit_code = 1 if errors or failures else 0
        except KeyboardInterrupt:
            summary.update(status="interrupted", finished_at_utc=utcnow())
            events.log("interrupted")
            exit_code = 130
        except Exception as exc:
            summary.update(status="failed", finished_at_utc=utcnow(), error=f"{type(exc).__name__}: {exc}")
            events.log("run_failed", error=summary["error"])
            exit_code = 1
        finally:
            summary.setdefault("planned_assets", len(plan))
            summary["recorded_asset_results"] = len(results)
            atomic_json(folder / "summary.json", summary)
            db.close()
            events.close()
            print(f"Status: {summary['status']}. Reports: {folder}", flush=True)
        return exit_code


def main() -> int:
    try:
        return run(parser().parse_args())
    except (AcquisitionError, OSError, sqlite3.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
