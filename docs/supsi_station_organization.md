# SUPSI Ticino station organization and date-level validation

Run from the thesis project root in PowerShell after the full PDF extraction:

```powershell
$id = "20261015T095609Z_60a4f441_7ac205a1"
python -m pip install pdfplumber
python .\scripts\supsi_organize_validate.py --root . --extraction-id $id
if ($LASTEXITCODE -ne 0) { throw "SUPSI organization blocked; read runs\supsi_organization\$id\cell_issues.csv" }
Get-Content ".\runs\supsi_organization\$id\summary.json"
```

Copy `scripts/supsi_organize_validate.py` into your project's `scripts` folder
beside `supsi_extract.py` before running. The program reads the original
`data\raw\supsi\yearbook\...` PDFs, the extracted `daily.csv` and
`monthly.csv`, the extraction's `pdf_review.csv`, and the acquisition's
`station_inventory.csv`. The acquisition ID is inferred by removing the last
underscore-delimited suffix from the extraction ID; use `--acquisition-id`
if your runs do not follow that naming scheme.

The command checks each station/date and station/month against a new reading
of its archived PDF, including original token, numeric magnitude, suffix,
status, bounding box, source path and SHA256. It checks all keys for uniqueness,
valid calendar dates, station membership, complete PDF date grids, monthly
numeric-day counts, dash/blank-day counts and arithmetic fields. It compares
every field in the extraction review to the newly read PDF. A source mismatch
blocks publication of the station package and is recorded in `cell_issues.csv`.

On success, find the Excel-ready semicolon-separated CSVs here:

```text
working\supsi\stations\<extraction_id>\<station_id>\Metadata.csv
working\supsi\stations\<extraction_id>\<station_id>\Daily.csv
working\supsi\stations\<extraction_id>\<station_id>\Monthly.csv
runs\supsi_organization\<extraction_id>\summary.json
runs\supsi_organization\<extraction_id>\cell_issues.csv
```

`Daily.csv` and `Monthly.csv` each carry `collection`, current catalogue
metadata and PDF provenance. The original raw and derived files are unchanged.
`Monthly.csv` retains the *printed* total and a separate sum of numeric daily
cells. Source `-` remains `dash_uninterpreted`, blanks remain blank and numeric
suffixes are preserved. The current catalogue coordinates are labeled
`catalogue_*` and are not asserted to be the historical site for all dates;
P_CAL and P_NOVAZ_TOT especially require site-history review.

In Excel, use **Data > From Text/CSV**, choose the station's `Daily.csv` or
`Monthly.csv`, set **UTF-8** and **semicolon** delimiter, then **Load To** a
table in a new workbook. Import `Metadata.csv` as a third table and save the
workbook as `working\supsi\excel\<extraction_id>\<station_id>.xlsx`. In Power
Query, set `date` to Date and leave the `YYYY-MM` `month` key as Text. If an
Excel date is needed for monthly plotting, create a separate first-of-month
date from `month & "-01"`. Retain `raw_token`, `symbol` and `source_pdf_sha256`
as text. The three CSVs remain the audited
canonical station exports; the workbook is an analysis view.

The checks establish PDF transcription and file integrity. They do not
establish the provider's meaning for dashes/suffixes, approve quality control,
or prove continuity of physical stations over time.
