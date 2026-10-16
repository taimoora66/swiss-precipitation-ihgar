#!/usr/bin/env python3
"""Archive every public WSL LWF meteorology station CSV on EnviDat.

Python 3.10+, standard library only. The published LWF CSVs are hourly
snapshots; this program does not infer a daily precipitation total or claim
that the public archive covers every WSL station or the whole thesis period.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from meteoswiss_download import AcquisitionError, Events, ProjectLock, atomic_json, write_csv

VERSION = "1.0.0"
BASE = "https://www.envidat.ch"
SEARCH = BASE + "/api/3/action/package_search"
MAX_CATALOGUE = 16 * 1024 * 1024
MAX_FILE = 512 * 1024 * 1024
NAME = re.compile(r"^lwfmeteo-[a-z0-9-]+$")
ALLOWED_HOSTS = {"www.envidat.ch", "envidat.ch", "zhw-b.s3.cloud.switch.ch"}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def safe_url(url):
    p = urllib.parse.urlsplit(url)
    if p.scheme != "https" or p.hostname not in ALLOWED_HOSTS or p.username or p.password or p.port not in (None, 443):
        raise AcquisitionError(f"Unexpected resource origin: {url}")


def candidate_csv(r):
    # A few published CSVs are mislabeled "Data plot" in their title. The
    # format and filename, then the actual timestamp/precip header, decide.
    return (str(r.get("format", "")).upper() == "CSV" and
            urllib.parse.urlsplit(str(r.get("url", ""))).path.lower().endswith(".csv"))


def save_bytes(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


class Fetcher:
    def __init__(self, root, args, events):
        self.root, self.args, self.events = root, args, events
        self.index_path = root / "data/state/wsl_lwf_resources.json"
        self.index = json.loads(self.index_path.read_text(encoding="utf-8")) if self.index_path.exists() else {}
        self.last_request = 0.0

    def get(self, url, kind, label, max_bytes, refresh=False, expected_size=None):
        safe_url(url)
        old = self.index.get(url)
        if old and not refresh:
            path = (self.root / old["local_path"]).resolve()
            if self.root.resolve() not in path.parents:
                raise AcquisitionError("Cached path escapes the project root")
            if path.is_file() and digest(path) == old["sha256"]:
                self.events.log("verified_local_cache", url=url, sha256=old["sha256"])
                return {**old, "status": "verified_local_cache"}, path
        last = None
        for attempt in range(1, self.args.retries + 2):
            delay = self.args.pause - (time.monotonic() - self.last_request)
            if delay > 0:
                time.sleep(delay)
            try:
                request = urllib.request.Request(url, headers={"User-Agent": f"Swiss-Precipitation-Thesis-WSL-LWF/{VERSION}", "Accept-Encoding": "identity"})
                with urllib.request.urlopen(request, timeout=self.args.timeout) as response:
                    safe_url(response.url)
                    length = response.headers.get("Content-Length")
                    if length and int(length) > max_bytes:
                        raise AcquisitionError(f"Response exceeds size limit: {url}")
                    payload = response.read(max_bytes + 1)
                    if len(payload) > max_bytes or (length and len(payload) != int(length)):
                        raise AcquisitionError(f"Oversize or truncated response: {url}")
                    if expected_size and len(payload) != expected_size:
                        raise AcquisitionError(f"Catalogue size mismatch ({expected_size} vs {len(payload)}): {url}")
                    if kind == "metadata":
                        obj = json.loads(payload.decode("utf-8-sig"))
                        if obj.get("success") is not True or not isinstance(obj.get("result"), dict):
                            raise AcquisitionError(f"Invalid CKAN response: {url}")
                    if kind == "observations" and not payload[:100].lstrip(b"\xef\xbb\xbf").startswith(b"timestamp;"):
                        raise AcquisitionError(f"Unexpected CSV header: {url}")
                    filename = (re.sub(r"[^A-Za-z0-9._-]", "_", label)[:110] or "resource")
                    sha = hashlib.sha256(payload).hexdigest()
                    rel = Path("data/raw/wsl_lwf") / kind / sha / filename
                    path = self.root / rel
                    if not path.exists() or digest(path) != sha:
                        save_bytes(path, payload)
                    record = dict(url=url, local_path=rel.as_posix(), sha256=sha,
                                  size_bytes=len(payload), etag=response.headers.get("ETag", ""),
                                  last_modified=response.headers.get("Last-Modified", ""),
                                  downloaded_at_utc=now(), status="downloaded")
                    self.index[url] = record
                    atomic_json(self.index_path, self.index)
                    self.events.log("downloaded", url=url, sha256=sha, size_bytes=len(payload))
                    return record, path
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError, AcquisitionError) as exc:
                last = exc
                status = getattr(exc, "code", None)
                retryable = status is None or status in (408, 425, 429, 500, 502, 503, 504)
                self.events.log("request_failed", url=url, attempt=attempt, error=str(exc), retryable=retryable)
                if not retryable or attempt > self.args.retries:
                    break
                time.sleep(min(20, 2 ** (attempt - 1)))
            finally:
                self.last_request = time.monotonic()
        raise AcquisitionError(f"{url}: {last}")

    def catalogue(self, url, label):
        rec, path = self.get(url, "metadata", label, MAX_CATALOGUE, refresh=True)
        return rec, json.loads(path.read_text(encoding="utf-8-sig"))["result"]


def discover(fetcher):
    packages = {}
    snapshots = []
    start, rows = 0, 100
    while True:
        url = SEARCH + "?" + urllib.parse.urlencode({"q": "lwfmeteo", "rows": rows, "start": start})
        rec, result = fetcher.catalogue(url, f"search_{start}.json")
        snapshots.append(rec)
        found = result.get("results")
        count = result.get("count")
        if not isinstance(found, list) or not isinstance(count, int) or count < start + len(found):
            raise AcquisitionError("Invalid catalogue pagination")
        for p in found:
            name = p.get("name", "")
            if NAME.fullmatch(name):
                if name in packages:
                    raise AcquisitionError(f"Duplicate dataset {name}")
                packages[name] = p
        start += len(found)
        if start >= count:
            break
        if not found:
            raise AcquisitionError("Catalogue pagination stalled")
    if not packages:
        raise AcquisitionError("No public LWF meteorology datasets discovered")
    # package_show retains full details even when the search response changes.
    full = {}
    for name in sorted(packages):
        url = BASE + "/api/3/action/package_show?" + urllib.parse.urlencode({"id": name})
        rec, package = fetcher.catalogue(url, f"{name}.json")
        snapshots.append(rec)
        if package.get("name") != name or not isinstance(package.get("resources"), list):
            raise AcquisitionError(f"Invalid dataset response for {name}")
        full[name] = package
    return full, snapshots


def scan_csv(path):
    stats = dict(rows=0, precipitation_numeric=0, precipitation_missing=0,
                 other_precipitation=0, first_timestamp="", last_timestamp="",
                 duplicate_or_nonmonotonic_timestamps=0, negative_precipitation=0)
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file, delimiter=";")
        if not reader.fieldnames or "timestamp" not in reader.fieldnames or "precip" not in reader.fieldnames:
            raise AcquisitionError(f"CSV missing timestamp/precip: {path}")
        previous = None
        for row in reader:
            if None in row:
                raise AcquisitionError(f"Malformed CSV row: {path}")
            ts = row["timestamp"]
            if not ts:
                raise AcquisitionError(f"Blank timestamp: {path}")
            stats["rows"] += 1
            if not stats["first_timestamp"]:
                stats["first_timestamp"] = ts
            if previous is not None and ts <= previous:
                stats["duplicate_or_nonmonotonic_timestamps"] += 1
            previous = ts
            value = row["precip"].strip()
            if value.upper() in ("NA", "NAN", "NULL", ""):
                stats["precipitation_missing"] += 1
            else:
                try:
                    n = float(value)
                    if not (-float("inf") < n < float("inf")):
                        raise ValueError("not finite")
                    stats["precipitation_numeric"] += 1
                    if n < 0:
                        stats["negative_precipitation"] += 1
                except ValueError:
                    stats["other_precipitation"] += 1
            stats["last_timestamp"] = ts
    if not stats["rows"]:
        raise AcquisitionError(f"Empty station CSV: {path}")
    return stats


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument("--metadata-only", action="store_true")
    p.add_argument("--all-resources", action="store_true", help="Also download plot PDFs and site photographs")
    p.add_argument("--dataset", action="append", help="Pilot one or more LWF dataset slugs")
    p.add_argument("--refresh", action="store_true", help="Download station files again even if a verified copy exists")
    p.add_argument("--pause", type=float, default=0.5, help="Minimum seconds between requests")
    p.add_argument("--timeout", type=float, default=90)
    p.add_argument("--retries", type=int, default=3)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    if args.pause < 0 or args.timeout <= 0 or args.retries < 0:
        parser().error("pause/retries must be nonnegative and timeout must be positive")
    root = args.root.resolve()
    folder = root / "runs/wsl_lwf" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder.mkdir(parents=True, exist_ok=False)
    with ProjectLock(root / "data/state/wsl_lwf.lock"):
        events = Events(folder / "events.jsonl")
        summary = dict(started_at_utc=now(), source="WSL LWF public EnviDat lwfmeteo datasets",
                       scope="published_LWF_meteorology_snapshots", status="running",
                       all_WSL_stations=False, thesis_period_fully_covered=False)
        records, inventory, errors = [], [], []
        try:
            fetcher = Fetcher(root, args, events)
            packages, snapshots = discover(fetcher)
            atomic_json(folder / "catalogue_snapshots.json", snapshots)
            if args.dataset:
                unknown = set(args.dataset) - set(packages)
                if unknown:
                    raise AcquisitionError(f"Unknown dataset(s): {sorted(unknown)}")
            selected = [k for k in sorted(packages) if not args.dataset or k in args.dataset]
            plan = []
            for name in selected:
                p = packages[name]
                for r in p["resources"]:
                    if candidate_csv(r) or args.all_resources:
                        if not isinstance(r.get("url"), str) or not r.get("id"):
                            raise AcquisitionError(f"Invalid resource in {name}")
                        safe_url(r["url"])
                        plan.append((p, r))
            atomic_json(folder / "download_plan.json", [dict(dataset=p["name"], dataset_id=p["id"],
                resource_id=r["id"], resource_name=r.get("name"), url=r["url"], size=r.get("size")) for p, r in plan])
            catalogue_rows = []
            for p, r in plan:
                if not candidate_csv(r):
                    continue
                code = re.search(r"\(([A-Z]{2,5})\)", str(r.get("name", "")))
                filename = urllib.parse.urlsplit(r["url"]).path.rsplit("/", 1)[-1].lower()
                catalogue_rows.append(dict(dataset=p["name"], station_code=code.group(1) if code else "",
                    station_exposure="open_field" if filename.endswith("field.csv") else
                    "forest" if filename.endswith("forest.csv") else "unresolved",
                    dataset_spatial=p.get("spatial", ""), doi=p.get("doi", ""),
                    metadata_modified=p.get("metadata_modified", ""),
                    resource_id=r["id"], resource_name=r.get("name", ""),
                    catalogue_size=r.get("size", ""), url=r["url"]))
            write_csv(folder / "catalogue_station_resources.csv", catalogue_rows,
                ["dataset", "station_code", "station_exposure", "dataset_spatial", "doi",
                 "metadata_modified", "resource_id", "resource_name", "catalogue_size", "url"])
            if not plan:
                raise AcquisitionError("No station resources discovered")
            print(f"Discovered {len(packages)} public LWF datasets; selected {len(selected)}; planned resources {len(plan)}", flush=True)
            if not args.metadata_only:
                for i, (p, r) in enumerate(plan, 1):
                    is_csv = candidate_csv(r)
                    filename = urllib.parse.unquote(urllib.parse.urlsplit(r["url"]).path.rsplit("/", 1)[-1])
                    label = f"{p['name']}_{r['id']}_{filename}"
                    report = dict(dataset=p["name"], dataset_id=p["id"], doi=p.get("doi", ""),
                        license_id=p.get("license_id", ""), metadata_modified=p.get("metadata_modified", ""),
                        resource_id=r["id"], resource_name=r.get("name", ""),
                        resource_format=r.get("format", ""), url=r["url"], catalogue_size=r.get("size"))
                    if is_csv:
                        code = re.search(r"\(([A-Z]{2,5})\)", str(r.get("name", "")))
                        report.update(station_code=code.group(1) if code else "",
                            station_exposure="open_field" if filename.lower().endswith("field.csv") else
                            "forest" if filename.lower().endswith("forest.csv") else "unresolved",
                            dataset_spatial=p.get("spatial", ""))
                    try:
                        expected = int(r["size"]) if r.get("size") else None
                        rec, path = fetcher.get(r["url"], "observations" if is_csv else "other",
                            label, MAX_FILE, refresh=args.refresh, expected_size=expected)
                        report.update(rec)
                        if is_csv:
                            stats = scan_csv(path)
                            inventory.append({**report, **stats})
                    except Exception as exc:
                        report.update(status="error", error=f"{type(exc).__name__}: {exc}")
                        errors.append(report)
                        events.log("resource_failed", **report)
                    records.append(report)
                    atomic_json(folder / "resource_manifest.json", records)
                    print(f"{i}/{len(plan)} {p['name']} {r.get('name')}: {report['status']}", flush=True)
            fields = ["dataset", "station_code", "station_exposure", "dataset_spatial", "resource_name", "resource_id", "doi", "license_id", "rows",
                      "first_timestamp", "last_timestamp", "precipitation_numeric", "precipitation_missing",
                      "other_precipitation", "negative_precipitation", "duplicate_or_nonmonotonic_timestamps",
                      "url", "local_path", "sha256", "status"]
            write_csv(folder / "station_inventory.csv", inventory, fields)
            atomic_json(folder / "errors.json", errors)
            summary.update(status="failed_or_incomplete" if errors else
                ("catalogue_only" if args.metadata_only else
                 "completed_selected_scope" if args.dataset else "completed_public_LWF_scope"),
                public_datasets=len(packages), selected_datasets=selected,
                planned_resources=len(plan), downloaded_or_verified=len(records)-len(errors),
                station_csvs_scanned=len(inventory), failures=len(errors),
                note="Published EnviDat station snapshots are not the full LWF/WSL archive; no daily totals were derived.")
        except Exception as exc:
            summary.update(status="failed", error=f"{type(exc).__name__}: {exc}")
            events.log("run_failed", error=summary["error"])
        finally:
            summary["finished_at_utc"] = now()
            atomic_json(folder / "summary.json", summary)
            events.close()
            print(f"Status: {summary['status']}. Reports: {folder}", flush=True)
        return 0 if summary["status"] in ("catalogue_only", "completed_selected_scope", "completed_public_LWF_scope") else 1


if __name__ == "__main__":
    sys.exit(main())
