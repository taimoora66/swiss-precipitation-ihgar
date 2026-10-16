#!/usr/bin/env python3
"""Transcribe SUPSI station-year precipitation PDFs with source-cell provenance.

This is a geometric PDF transcription, not scientific QC or station merging.
Requires pdfplumber; see docs/supsi_extraction.md.
"""
from __future__ import annotations

import argparse
import calendar
import csv
import hashlib
import json
import re
import sys
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pdfplumber

VERSION = "0.1.0"
NUMERIC = re.compile(r"^(\d+(?:[.,]\d+)?)([+\-]?)$")
MONTHS = ("I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X", "XI", "XII")
DAILY_COLUMNS = ["station_id", "date", "year", "month", "day", "raw_token", "value_mm",
                 "symbol", "cell_status", "source_pdf_path", "source_pdf_sha256", "page",
                 "bbox_x0", "bbox_top", "bbox_x1", "bbox_bottom"]
MONTHLY_COLUMNS = ["station_id", "month", "year", "printed_total_raw", "printed_total_mm",
                   "symbol", "cell_status", "sum_numeric_daily_mm", "numeric_days", "dash_days",
                   "blank_days", "difference_mm", "arithmetic_status", "source_pdf_path",
                   "source_pdf_sha256", "page", "bbox_x0", "bbox_top", "bbox_x1", "bbox_bottom"]
REVIEW_COLUMNS = ["station_id", "year", "source_pdf_path", "source_pdf_sha256", "status",
                  "daily_numeric", "daily_dash", "daily_blank", "monthly_numeric",
                  "monthly_blank", "months_match", "months_mismatch", "months_incomplete", "other_symbols",
                  "detail"]


class LayoutError(ValueError):
    pass


def clean_token(token):
    if not token:
        return "", "", "blank", ""
    if token == "-":
        return "", "", "dash_uninterpreted", ""
    match = NUMERIC.fullmatch(token)
    if match:
        return match[1].replace(",", "."), match[2], "numeric", ""
    return "", "", "other_symbol", token


def cell(words, left, right, top, bottom):
    found = [w for w in words if left <= (w["x0"] + w["x1"]) / 2 < right
             and top <= (w["top"] + w["bottom"]) / 2 < bottom]
    found.sort(key=lambda w: (w["top"], w["x0"]))
    token = " ".join(w["text"] for w in found)
    bbox = (min(w["x0"] for w in found), min(w["top"] for w in found),
            max(w["x1"] for w in found), max(w["bottom"] for w in found)) if found else ("",) * 4
    return token, bbox


def fingerprint(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def extract_one(path, station_id, year, raw_path, digest):
    with pdfplumber.open(path) as pdf:
        if len(pdf.pages) != 1:
            raise LayoutError(f"Expected one-page table, got {len(pdf.pages)} pages")
        page = pdf.pages[0]
        if not 580 < page.width < 610 or not 830 < page.height < 855:
            raise LayoutError(f"Unexpected page dimensions: {page.width} x {page.height}")
        words = page.extract_words(x_tolerance=1, y_tolerance=3)
        if not any(w["text"] == "Precipitazioni" and w["top"] < 80 for w in words):
            raise LayoutError("Precipitation title absent")
        if not any(w["text"] == str(year) and w["x0"] < 80 and 80 < w["top"] < 140 for w in words):
            raise LayoutError("Year label does not match manifest")
        headers = [w for w in words if 145 < w["x0"] < 550 and 78 < w["top"] < 92
                   and w["text"] in MONTHS]
        headers.sort(key=lambda w: w["x0"])
        if len(headers) != 12 or [w["text"] for w in headers] != list(MONTHS):
            raise LayoutError("Expected twelve ordered Roman month headings")
        centers = [(w["x0"] + w["x1"]) / 2 for w in headers]
        edges = [150] + [(centers[i] + centers[i + 1]) / 2 for i in range(11)] + [550]
        days = {}
        for w in words:
            if 125 < w["x0"] < 145 and 88 < w["top"] < 347 and w["text"].isdigit():
                d = int(w["text"])
                if 1 <= d <= 31:
                    if d in days:
                        raise LayoutError(f"Repeated day label {d}")
                    days[d] = (w["top"] + w["bottom"]) / 2
        if set(days) != set(range(1, 32)) or any(days[d] >= days[d + 1] for d in range(1, 31)):
            raise LayoutError("Daily row labels incomplete or out of order")
        monthly_label = [w for w in words if w["text"] == "Monatssumme" and w["x0"] < 120
                         and 340 < w["top"] < 375]
        if len(monthly_label) != 1:
            raise LayoutError("Printed monthly total row absent or ambiguous")
        monthly_y = (monthly_label[0]["top"] + monthly_label[0]["bottom"]) / 2
        daily, monthly = [], []
        count = Counter()
        other = []
        month_rows = defaultdict(list)
        for m in range(1, 13):
            for d in range(1, 32):
                y = days[d]
                # Narrow y window prevents accidental capture of adjacent rows.
                token, bbox = cell(words, edges[m-1], edges[m], y-3.2, y+3.2)
                if d > calendar.monthrange(year, m)[1]:
                    if token:
                        raise LayoutError(f"Printed value on invalid calendar date: {year}-{m:02}-{d:02}")
                    continue
                value, symbol, kind, unknown = clean_token(token)
                if unknown:
                    other.append(f"{m:02}-{d:02}: {unknown}")
                count["daily_" + ("dash" if kind == "dash_uninterpreted" else
                                  "blank" if kind == "blank" else "numeric" if kind == "numeric" else "other")] += 1
                row = dict(station_id=station_id, date=f"{year:04}-{m:02}-{d:02}", year=year,
                           month=m, day=d, raw_token=token, value_mm=value, symbol=symbol,
                           cell_status=kind, source_pdf_path=raw_path, source_pdf_sha256=digest,
                           page=1, **dict(zip(DAILY_COLUMNS[-4:], bbox)))
                daily.append(row)
                month_rows[m].append(row)
            token, bbox = cell(words, edges[m-1], edges[m], monthly_y-3.5, monthly_y+3.5)
            value, symbol, kind, unknown = clean_token(token)
            if unknown:
                other.append(f"month {m:02}: {unknown}")
            count["monthly_" + ("blank" if kind == "blank" else "numeric" if kind == "numeric" else "other")] += 1
            entries = month_rows[m]
            numeric_sum = sum((Decimal(r["value_mm"]) for r in entries if r["cell_status"] == "numeric"), Decimal(0))
            n_numeric = sum(r["cell_status"] == "numeric" for r in entries)
            n_dash = sum(r["cell_status"] == "dash_uninterpreted" for r in entries)
            n_blank = sum(r["cell_status"] == "blank" for r in entries)
            if kind == "numeric":
                difference = numeric_sum - Decimal(value)
                if n_blank:
                    arithmetic = "blank_daily_cells_review"
                else:
                    arithmetic = "matches_numeric_sum" if abs(difference) <= Decimal("0.11") else "mismatch_review"
                if arithmetic == "matches_numeric_sum":
                    count["months_match"] += 1
                elif arithmetic == "blank_daily_cells_review":
                    count["months_incomplete"] += 1
                else:
                    count["months_mismatch"] += 1
            else:
                difference, arithmetic = "", "no_numeric_printed_total"
            monthly.append(dict(station_id=station_id, month=f"{year:04}-{m:02}", year=year,
                                printed_total_raw=token, printed_total_mm=value, symbol=symbol,
                                cell_status=kind, sum_numeric_daily_mm=str(numeric_sum),
                                numeric_days=n_numeric, dash_days=n_dash, blank_days=n_blank,
                                difference_mm=str(difference), arithmetic_status=arithmetic,
                                source_pdf_path=raw_path, source_pdf_sha256=digest, page=1,
                                **dict(zip(MONTHLY_COLUMNS[-4:], bbox))))
        if len(daily) != (366 if calendar.isleap(year) else 365):
            raise LayoutError("Unexpected daily calendar length")
        review = dict(station_id=station_id, year=year, source_pdf_path=raw_path,
                      source_pdf_sha256=digest, status="review" if other or count["months_mismatch"]
                      or count["daily_blank"] or count["monthly_blank"] else "transcribed",
                      daily_numeric=count["daily_numeric"], daily_dash=count["daily_dash"],
                      daily_blank=count["daily_blank"], monthly_numeric=count["monthly_numeric"],
                      monthly_blank=count["monthly_blank"], months_match=count["months_match"],
                      months_mismatch=count["months_mismatch"], months_incomplete=count["months_incomplete"],
                      other_symbols="|".join(other),
                      detail="Dash and +/- symbols are retained without interpreting provider meaning.")
        return daily, monthly, review


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run", type=Path, required=True, help="SUPSI acquisition run directory.")
    p.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    p.add_argument("--max-files", type=int, help="Optional pilot limit, in manifest order.")
    args = p.parse_args(argv)
    if args.max_files is not None and args.max_files < 1:
        p.error("--max-files must be positive")
    root, run = args.root.resolve(), args.run.resolve()
    if not (run / "download_manifest.csv").is_file():
        p.error("Acquisition manifest not found")
    if not (run / "summary.json").is_file():
        p.error("Acquisition summary not found")
    acquisition = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    if acquisition.get("status") != "completed_available_assets":
        p.error("Acquisition is not completed_available_assets; inspect its errors first")
    with (run / "download_manifest.csv").open(encoding="utf-8-sig", newline="") as f:
        inputs = [r for r in csv.DictReader(f, delimiter=";") if r["kind"] == "yearbook"
                  and r["status"] in ("downloaded", "cached")]
    available_count = len(inputs)
    if not available_count:
        p.error("No acquired yearbook PDFs in this manifest")
    if args.max_files:
        inputs = inputs[:args.max_files]
    output = root / "data/derived/supsi" / (run.name + "_" + uuid.uuid4().hex[:8])
    output.mkdir(parents=True, exist_ok=False)
    reports = root / "runs/supsi_extraction" / output.name
    reports.mkdir(parents=True, exist_ok=False)
    seen = set()
    reviews = []
    with (output / "daily.csv").open("w", newline="", encoding="utf-8-sig") as df, \
         (output / "monthly.csv").open("w", newline="", encoding="utf-8-sig") as mf:
        dw, mw = csv.DictWriter(df, DAILY_COLUMNS, delimiter=";"), csv.DictWriter(mf, MONTHLY_COLUMNS, delimiter=";")
        dw.writeheader(); mw.writeheader()
        for row in inputs:
            sid, year = row["station_id"], int(row["year"])
            key = (sid, year)
            review = dict(station_id=sid, year=year, source_pdf_path=row["raw_path"],
                          source_pdf_sha256=row["sha256"], status="failed", detail="")
            try:
                if key in seen:
                    raise LayoutError("Duplicate station/year in acquisition manifest")
                seen.add(key)
                relative = Path(row["raw_path"])
                if relative.is_absolute() or ".." in relative.parts or relative.parts[:3] != ("data", "raw", "supsi"):
                    raise LayoutError("Unsafe source path in manifest")
                if relative.name != f"{sid}_{year}.pdf":
                    raise LayoutError("PDF basename does not match station/year in manifest")
                path = root / relative
                if fingerprint(path) != row["sha256"]:
                    raise LayoutError("Source PDF SHA256 mismatch")
                daily, monthly, review = extract_one(path, sid, year, relative.as_posix(), row["sha256"])
                dw.writerows(daily); mw.writerows(monthly)
            except Exception as exc:
                review["detail"] = f"{type(exc).__name__}: {exc}"
            reviews.append(review)
            print(f"{review['status']:11s} {sid} {year} {review['detail'][:100]}", flush=True)
    with (reports / "pdf_review.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, REVIEW_COLUMNS, delimiter=";", extrasaction="ignore")
        w.writeheader(); w.writerows(reviews)
    counts = Counter(r["status"] for r in reviews)
    summary = dict(acquisition_run=str(run), acquisition_status=acquisition["status"],
                   acquisition_candidate_yearbooks=acquisition.get("candidate_yearbooks"),
                   acquisition_available_yearbooks=available_count,
                   acquisition_not_found=acquisition.get("counts_by_status", {}).get("not_found", 0),
                   extraction_run=output.name, created_at_utc=datetime.now(timezone.utc).isoformat(),
                   extractor_version=VERSION, pdfplumber_version=pdfplumber.__version__, python_version=sys.version.split()[0],
                   yearbooks_selected=len(inputs), statuses=dict(counts), daily_csv=str(output / "daily.csv"),
                   monthly_csv=str(output / "monthly.csv"), scientific_qc_performed=False,
                   dash_meaning_verified=False, status="completed_transcription" if not counts["failed"]
                   and not counts["review"] else "review_required")
    (reports / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(f"Status: {summary['status']}. Reports: {reports}", flush=True)
    return 0 if summary["status"] == "completed_transcription" else 1


if __name__ == "__main__":
    sys.exit(main())
