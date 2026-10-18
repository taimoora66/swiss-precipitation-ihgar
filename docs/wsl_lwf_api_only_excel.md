# WSL LWF live API-only workbook

This exporter uses **only** the 13 saved EnviDat live API models and their
original yearly CSVs from run `20261017T122441Z`. It does not read the older
2016 EnviDat archive, its organized hourly tables, PANGAEA references, or the
earlier archive-based Excel export.

## Run in Windows PowerShell

Place `scripts/wsl_lwf_api_excel.py` in the thesis project's `scripts` folder.
Keep the downloaded run in `data/raw/wsl_lwf/live_api/20261017T122441Z` and its
`summary.json`, `coverage.csv` and `download_manifest.jsonl` in
`runs/wsl_lwf_live/20261017T122441Z`.

```powershell
Set-Location 'C:\Users\Admin\Documents\Swiss_Precipitation_Thesis'
py -3 .\scripts\wsl_lwf_api_excel.py `
    --root . `
    --run-id 20261017T122441Z `
    --expected-stations 13
if ($LASTEXITCODE -ne 0) { throw 'WSL API-only export failed' }

$api = Get-Content '.\working\wsl_lwf\api_only_excel\latest.json' -Raw |
    ConvertFrom-Json
$api | Select-Object status,station_count,source_year_files,
    hourly_daily_grid_rows,monthly_grid_rows,workbook |
    Format-List
Invoke-Item $api.workbook
```

Expected status is `completed_api_only_provisional_source_label_sums`. The
script checks every yearly CSV against the acquisition manifest's SHA-256,
byte size and row count, and every station metadata JSON against the saved
coverage hash. It requires the full 13-model year inventory before producing
the workbook. It then reopens the XLSX XML and checks the complete set of
written cells. A successful export writes a new timestamped folder and updates
`working/wsl_lwf/api_only_excel/latest.json`; it never overwrites older runs.

Output:

```text
working/wsl_lwf/api_only_excel/20261017T122441Z/<new_timestamp>/
    WSL_LWF_API_Only_Selected_All.xlsx
    summary.json
```

## Workbook meaning

`Station_metadata` has one row per API model. `station_id` is the API model
name; exposure and the display name are inferred from that identifier.
`longitude`, `latitude`, `elevation_m`, `instrument` and legal `owner` remain
blank because the acquired API metadata does not supply these fields. Each
has an explicit availability status. Provider is WSL/LWF through EnviDat.
The sheet retains first and last timestamps for each of the four downloaded
precipitation fields, SHA-256 provenance, observed numeric span, hour counts,
and the API metadata URL. Do not substitute dataset-level coordinates for
physical gauge coordinates.

`Hourly_daily_grid` has one row per station and UTC date. `H00`–`H23`
are the numeric hourly source values at the corresponding **UTC timestamp
labels**, from 00:00 through 23:00. They are not statements that the gauges
accumulate exactly from the start to the end of each named hour; the provider's
observation window still needs confirmation. A blank H cell means there is no
single usable hourly value for that label, not zero rain. The `valid_hourly_slots`,
`missing_hourly_slots`, duplicate, ambiguous, and off-hour columns make the
daily count and exclusions explicit. `computed_complete_day_total_mm` is
populated only for a complete 24-hour day; the partial amount is diagnostic.

`Daily_month_grid` has one row per station and calendar month from the API's
first to last hourly metadata dates. D01–D31 are **daily totals**, not hourly
values. They hold a provisional amount only
when all 24 UTC clock-hour labels for that day have one finite nonnegative
hourly value. The script uses `precipitation_60` or
`precipitation_60_multi`, whichever is the sole numeric hourly field on a
timestamp. It never adds the `precipitation_10` fields to the hourly values.
If both hourly fields are numeric at one hour, the day remains unresolved.
Duplicate hourly rows, negative/nonnumeric values, missing hours and numeric
hourly fields on off-hour labels, including timestamps with nonzero seconds,
also block a complete-day amount. Subminute rows containing only 10-minute
fields are ignored for this hourly aggregation. The source
rows stay unchanged and available in the saved yearly CSVs.

The three sheets are ordered `Station_metadata`, `Hourly_daily_grid`,
`Daily_month_grid`. Both data sheets include the verified source CSV path and
SHA-256, UTC dates, completeness counts, and explicit aggregation status.
The metadata enrichment step must be rerun after creating this revised workbook;
an earlier two-sheet export is retained as a historical output.

`computed_complete_month_total_mm` is populated only if every calendar day
has a complete 24-hour provisional amount. Otherwise it is blank.
`sum_of_complete_days_mm` and `partial_valid_hour_amount_mm` are separately
labelled diagnostics for incomplete periods and must not be interpreted as
complete month totals. `source_monthly_total_mm` is always blank: the
acquired API endpoint provides no independent monthly rainfall total.
Its daily summary endpoint publishes min/max/average statistics, which do
not replace an independent monthly total.

The 60-minute fields are integrated using an **assumed** one-hour rate
interpretation. UTC source calendar labels are used unchanged. The exact
observation interval/window, gauge instrument and scientific QC remain
unverified. The resulting series are provisional, especially for winter
precipitation and forest-exposure resources. Do not feed these workbook
amounts directly into IH-GAR as validated daily precipitation.

## Keep the old archive separate

The earlier export is under
`working/wsl_lwf/excel_selected/20261015T153606Z/20261017T113340593Z`
and was prepared from the archive-based acquisition ending in 2016. The new
API-only export goes under **`api_only_excel`**. Open only the new `latest.json`
pointer for this workflow. Keep the raw archive and its reports as provenance.
There is no need to delete them to prevent mixing datasets.

If you want to remove the *old workbook from your active Excel folder* after
confirming the API-only export, move the old **export folder**, not the raw
observation files, to an explicitly named local archive:

```powershell
$old = '.\working\wsl_lwf\excel_selected\20261015T153606Z\20261017T113340593Z'
$destination = '.\working\wsl_lwf\legacy_2016_archive_excel\20261017T113340593Z'
New-Item -ItemType Directory (Split-Path $destination -Parent) -Force | Out-Null
if (Test-Path $old) { Move-Item -LiteralPath $old -Destination $destination }
```

That move can leave old report paths and the old `excel_selected/latest.json`
pointer stale; preserve them only for historical reference. Do not use that
pointer after moving the folder. The `api_only_excel/latest.json` pointer is
independent and will continue to identify the new workbook.
