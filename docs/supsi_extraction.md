# SUPSI precipitation PDF transcription

Use this after `scripts/supsi_download.py` reports
`completed_available_assets`. The [SUPSI station-year tables](https://hydromet.supsi.ch/dati/idrologia/)
contain the daily day-by-month grid and the provider's printed monthly total.
This script reads those two parts of the original PDFs by page position. It
does not derive monthly totals from daily cells or merge station identities.

Install the one extraction dependency on the same Python interpreter used for
the project:

```powershell
python -m pip install pdfplumber
```

From your project folder, use the acquisition run you just completed:

```powershell
$run = ".\runs\supsi\20261015T095609Z_60a4f441"

# Check a small sample first and inspect the review report.
python .\scripts\supsi_extract.py --run $run --max-files 5

# Process every successfully archived station-year PDF in this run.
python .\scripts\supsi_extract.py --run $run
```

Every invocation creates a new `data/derived/supsi/<extraction_id>/` containing
`daily.csv` and `monthly.csv`, and a matching
`runs/supsi_extraction/<extraction_id>/` containing `pdf_review.csv` and
`summary.json`. Report CSVs use semicolons and UTF-8 with BOM. The outputs
are excluded from Git by the project's existing `.gitignore`. The input PDFs
remain untouched.

Each daily row represents a valid calendar date and retains the printed token,
numeric value if present, trailing symbol, cell status, PDF path/SHA256, page
and the word's bounding box. Blank valid-date cells are represented explicitly;
nonexistent dates such as 30 February are omitted. A `-` printed alone is
recorded as `dash_uninterpreted`, with no numeric value. Numeric tokens such
as `330.0+` and `1.8-` retain their suffix in `symbol` and their magnitude in
`value_mm`; the suffix's provider meaning has **not** been established.

Each monthly row preserves the provider's **printed** total in
`printed_total_raw` and `printed_total_mm`, alongside a separate sum of the
numeric daily cells. `matches_numeric_sum` is an arithmetic transcription
check within 0.11 mm; it does not establish the meaning of dashes or certify
observational quality. `blank_daily_cells_review` flags a month with an
unprinted day even if the available numbers sum to the total. A printed total
different from its numeric daily sum is `mismatch_review`; do not substitute
the computed sum for the source total.

The script checks each PDF's archived SHA256, filename, year label, page size,
one-page layout, ordered month headings, all 31 row labels, calendar bounds,
and printed monthly row. A layout it does not recognize produces `failed` in
`pdf_review.csv` without writing any rows for that PDF. A partial or ambiguous
transcription produces `review`; either status gives the command a nonzero exit
code, while preserving successfully transcribed PDFs and detailed reports.
Compare `yearbooks_selected` and statuses in the extraction summary with the
acquisition summary's available and missing counts. A completed acquisition
status means the available assets were fetched or explicitly absent, not that
every catalogue candidate has a PDF.

The layout was checked on P_ISO 1979, P_NEG 2019 and 2023, and P_BED 2023.
For the partial 2019 P_NEG table, five months have no printed monthly total
and are flagged for review. Twelve printed totals matched the numeric daily
sums in each complete sample year. This small sample does **not** validate
all stations or periods. Before using a daily series in IH-GAR, resolve
`-` and `+/-` meanings with SUPSI documentation or direct confirmation,
review blank/flagged months and station histories, check accumulation windows,
then carry out missingness, outlier, neighbor consistency and homogeneity
assessment. The virtual P_NOVAZ_TOT series crosses an explicit Chiasso to
Novazzano station change in 2022; keep that breakpoint during QC.
