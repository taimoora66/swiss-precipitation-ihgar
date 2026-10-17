# Organizing the published WSL LWF meteorology station files

The public `lwfmeteo-*` resources are **hourly station snapshots**. The
organizer preserves each original sensor field and each source row. Its daily
and monthly tables count published hourly observations; they are **not daily
or monthly precipitation totals**. The timestamps have no explicit timezone,
and an autumn clock-change hour can appear twice with different values. Do
not sum the rows to daily rainfall until WSL confirms the time convention,
observation interval and interpretation of `precip` for each station exposure.

## Run on the complete acquisition

From the thesis root in Windows PowerShell, first confirm the run ID and its
status. The example below uses the full acquisition reported on 15 October:

```powershell
$run = "20261015T153606Z"
$s = Get-Content ".\runs\wsl_lwf\$run\summary.json" -Raw | ConvertFrom-Json
$s | Select-Object status,station_csvs_scanned,failures
if ($s.status -ne "completed_public_LWF_scope" -or $s.failures -ne 0) {
    throw "Choose the completed full WSL LWF acquisition run"
}
python .\scripts\wsl_lwf_organize.py --root . --run-id $run
if ($LASTEXITCODE -ne 0) { throw "WSL LWF organization failed" }
Get-Content ".\working\wsl_lwf\organized\$run\summary.json"
```

If your actual full run has a different ID, list them with
`Get-ChildItem .\runs\wsl_lwf -Directory` and substitute that ID. The script
requires the original `data/raw/wsl_lwf` archive, acquisition inventory,
resource manifest and catalogue snapshots. It verifies source SHA-256 values,
provider resource identity, source row counts, timestamp endpoints and
precipitation status counts. It checks structural consistency, not the
meteorological validity of measurements. It refuses to overwrite an output
from an earlier successful attempt.

Outputs under `working\wsl_lwf\organized\<run_id>\`:

| File | Meaning |
| --- | --- |
| `station_register.csv` | One resource per station exposure, DOI, licence, source coverage and explicit metadata limitations. |
| `stations\WSL_LWF_<code>_<resource>\Metadata.csv` | The same source-linked metadata for one station resource. |
| `stations\WSL_LWF_<code>_<resource>\Hourly.csv` | One row per original hourly source row; duplicates are retained. |
| `hourly.csv` | All station resources together, with their original sensor fields in `raw_*` columns. |
| `daily_observation_counts.csv` | Counts of hourly rows, distinct timestamp labels and missing/invalid values on each date with rows. No rainfall sums. |
| `monthly_observation_counts.csv` | Equivalent observation counts for months with rows. No rainfall sums. |
| `qgis_dataset_points.csv` | Dataset-level EnviDat coordinates for each selected station resource, with location scope prominently flagged. |
| `summary.json` | Verified source and output totals and remaining scientific checks. |

All CSVs use UTF-8 with BOM and semicolons. `station_key` uniquely identifies
a source resource; `station_code` retains WSL's short station label.
`timestamp_source` and every `raw_*` value retain the source representation.
Each observation's `station_key` joins to the register's `local_path` and
`sha256`; its 1-based `source_row` locates the exact archived row. This
compact join avoids repeating long source paths millions of times. Blank/NA
rainfall cells are not zero. The `precipitation_numeric` column copies finite
numeric source values as text;
negative numbers are flagged rather than corrected.

The catalogue's `spatial` coordinate describes a **dataset**. Forest and
field resources within that dataset may share it. Neither this coordinate nor
an elevation has been independently verified for the physical rain gauge:
the `elevation_m` field is intentionally blank. Forest precipitation may have
a different measurement meaning from open-field precipitation. The public
snapshot end date is not the physical station closure date.

## Excel

Use **Data > From Text/CSV**, UTF-8 and semicolon delimiter for one station's
`Metadata.csv` and `Hourly.csv`. Import `timestamp_source`, `station_code`,
`station_key`, `source_csv`, SHA and other identifiers as text. Keep
`raw_precip` separate from the numeric reading and status.
The combined `hourly.csv` may exceed Excel's 1,048,576-row worksheet limit:
filter by station and period in Power Query or load as a connection/Data
Model. The daily and monthly tables contain observation counts only.

## QGIS

Open **Layer > Add Layer > Add Delimited Text Layer** and select
`qgis_dataset_points.csv` with semicolon delimiter. Choose point coordinates,
**X = `dataset_longitude`**, **Y = `dataset_latitude`**, CRS **EPSG:4326**.
Check the point count against `dataset_points` in `summary.json`. Style or
filter by `station_exposure` and inspect `location_scope`. Save a QGIS
GeoPackage under `working\wsl_lwf\qgis\<run_id>\` after reviewing it. This
maps dataset-level published locations, not verified historical gauge sites.
The observation CSVs can be loaded as nonspatial tables, with station points
as the parent and hourly/count rows as children linked by `station_key` in
QGIS project relations. A one-to-one attribute join would lose repeated
observations per station.

Before using any of these as input for IH-GAR, review the `runs/wsl_lwf_screen`
issues, confirm exposure and the WSL data agreement, resolve the time basis
and interval, verify gauge-level coordinates, evaluate coverage and missingness,
then design and independently check a daily aggregation rule.
