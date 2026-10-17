# Agrometeo station organization and QGIS inventory

Use a **completed** Agrometeo acquisition containing both daily and monthly
`6:sum` precipitation with `measured=1`. From the thesis project root in
PowerShell, inspect the available run IDs and choose the full run:

```powershell
Get-ChildItem .\runs\agrometeo -Directory | Sort-Object Name -Descending |
    Select-Object -First 10 Name
$run = "PASTE_COMPLETED_RUN_ID_HERE"
Get-Content ".\runs\agrometeo\$run\summary.json"
python .\scripts\agrometeo_organize.py --root . --run-id $run
if ($LASTEXITCODE -ne 0) { throw "Agrometeo organization failed" }
Get-Content ".\working\agrometeo\organized\$run\summary.json"
```

The script reads archived source JSON, acquisition metadata, station coverage
and the downloader's derived station CSVs. It verifies every source file's
SHA-256 and checks every exported sensor cell, date and source row against the
archived response. It reconciles precipitation counts with the downloader's
coverage report. Any structural disagreement stops publication of the new
tables; existing inputs are left unchanged. It refuses to overwrite a
previous organized output for the same run ID.

Outputs under `working\agrometeo\organized\<run_id>\`:

| File | Content |
| --- | --- |
| `catalogue_all.csv` | Every station in the acquisition inventory, including those outside the selected run. |
| `station_register.csv` | The selected stations, metadata, record counts and current-location review status. |
| `qgis_station_points.csv` | Selected station records with valid current WGS84 coordinates; catalogue conflicts stay flagged. |
| `stations\AGROMETEO_<id>\Metadata.csv` | One station's current catalogue and record counts. |
| `stations\AGROMETEO_<id>\Daily.csv` / `Monthly.csv` | Separate station observations, values, cell status and exact JSON provenance. |
| `daily.csv` / `monthly.csv` | Separate all-station observation tables. |
| `monthly_reconciliation.csv` | Complete-month checks of numeric daily sums against the independent monthly API value. |
| `summary.json` | Source-verification, row, value-status and reconciliation counts. |

`precipitation_mm` is numeric text for the provider's `6:sum` precipitation;
`source_value_raw` preserves the source cell. Zero is retained as zero; null,
blank, absent, nonnumeric and negative cells receive distinct statuses. Missing
dates are **not filled**. If the acquisition also requested other sensors,
their values are retained in `source_<sensor>_<aggregation>` columns and each
one is checked against the original JSON. The station catalogue's WGS84
longitude/latitude and elevation are explicitly prefixed `catalogue_` in
observation tables. They are **current metadata, not verified historical
locations**. Catalogue disagreements remain flags, and replacement station
IDs are not linked into continuous records.

`monthly_reconciliation.csv` distinguishes exact Decimal matches, numeric
mismatches, incomplete daily months, negative daily values, missing monthly
records and monthly cells without numeric values. Agreement tests aggregation
consistency only. It cannot verify the daily accumulation window, gauge
history, precipitation quality or homogeneity. The output may extend beyond
1951–2023 if the selected acquisition requested later dates; apply the thesis
analysis period in a separate, documented selection.

## Excel

Import a station's `Metadata.csv`, `Daily.csv` and `Monthly.csv` with **Data >
From Text/CSV**, UTF-8, semicolon delimiter. Set `station_code`, source paths,
SHA fields and `source_value_raw` to Text; daily `period` to Date. Keep monthly
`YYYY-MM` as Text or create a separate first-of-month date for charts. Save a
workbook under `working\agrometeo\excel\<run_id>\<station_code>.xlsx` if
needed; retain the CSVs as audited machine-readable exports. The full combined
daily table may exceed Excel's 1,048,576-row worksheet limit. Use Power Query
as a connection/Data Model or filter a station and date range before loading
to a worksheet. Do not silently interpret blank `precipitation_mm` as zero.

## QGIS

Open `qgis_station_points.csv` via **Layer > Add Layer > Add Delimited Text
Layer**. Select UTF-8 and semicolon delimiter, **X = `longitude`**, **Y =
`latitude`**, and **EPSG:4326**. Check the feature count against
`qgis_current_catalogue_points` in `summary.json`. Categorize by
`location_status` to review `catalogue_location_conflict_review` separately.
Filter `daily_rows > 0` if you want only stations with returned daily rows;
that filter is not an acceptance decision. Export the reviewed layer to a
GeoPackage under `working\agrometeo\qgis\<run_id>\` if desired. This layer is
a map of **current catalogue points**, not a verified historical gauge map.

These new CSVs, GeoPackages and workbooks stay under ignored `working/`; only
the organization script, tests and this guide belong in GitHub when the user
chooses to publish them.
