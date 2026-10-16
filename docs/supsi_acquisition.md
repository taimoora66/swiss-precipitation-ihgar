# SUPSI Ticino precipitation acquisition

The [SUPSI/IST hydrological yearbook interface](https://hydromet.supsi.ch/dati/idrologia/)
publishes a precipitation station table, two spatial catalogues, station sheets,
and a station-by-year precipitation PDF table. The PDF table contains daily
amounts in a day-by-month grid and a monthly-total row. This script **archives
originals only**; daily/monthly transcription and scientific QC come next.
The separate [extraction guide](supsi_extraction.md) now covers a first
transcription workflow; scientific QC remains a subsequent stage.
The separately published [bound annual editions](https://www.ist.supsi.ch/annuario-idrologico)
are a different PDF collection and are not part of this acquisition; the linked
station-year tables are the direct records for the planned extraction.

Run with Python 3.10+ from `Swiss_Precipitation_Thesis` on Windows:

```powershell
python .\scripts\supsi_download.py --metadata-only
python .\scripts\supsi_download.py --stations P_NEG --start-year 2023 --end-year 2023
python .\scripts\supsi_download.py
# Optional: probe all 40 listed IDs, including two marked unpublished.
python .\scripts\supsi_download.py --include-unpublished
```

The last command obtains station sheets and **available** published station-year
PDFs for the thesis baseline through 2023. It may take time: requests are
sequential and separated by at least one second. To include the subsequent
complete calendar years, use `--end-year 2025`. The currently ongoing 2026
year should not be treated as a complete annual record. `--stations` limits
station sheets and annual tables in a pilot while still saving the full raw
catalogues and reporting all 40 catalogue entries. `--metadata-only` with no
station filter obtains the three catalogues and every station sheet it can find.
The script never downloads discharge tables.

The provider's `Periodo da` gives **candidate publication years**, not proof
that a PDF exists or contains a complete series. If a current GeoJSON sensor
begins earlier, the immediately preceding boundary year is also checked and tagged
`sampling_begin_boundary` in the plan; for example, P_NEG began in 2019 while
its publication period begins in 2020. The script checks each candidate URL
with GET and records 404/410 as `not_found`.
Unpublished entries remain in the station inventory but are not put in the
yearbook plan by default. For a deliberately broader archive, use
`--include-unpublished`: it tries the two entries marked `no` and retains that
flag alongside every candidate. A public file at such a URL does not change
the provider's publication designation. A missing station sheet is reported
separately. A 200 response
must also pass the PDF header/EOF and transfer-length checks. These are basic
file integrity checks, not a guarantee that its numeric data are correct.

| Relative path | Contents |
| --- | --- |
| `data/raw/supsi/metadata/<sha256>/` | Original precipitation CSV and both complete GeoJSON catalogues. |
| `data/raw/supsi/station_sheet/<sha256>/` | Original station sheet PDFs, when published. |
| `data/raw/supsi/yearbook/<sha256>/` | Original station-year rain PDFs, one source file per year. |
| `data/state/supsi.sqlite3` | URL-to-checksum cache for verified PDF reuse. |
| `runs/supsi/<run_id>/` | Configuration, station inventory, candidate plan, request manifest, errors and summary. |

Inspect `summary.json`, `errors.json` and `download_manifest.csv` after each
run. `completed_available_assets` means all attempts either downloaded,
reused a hash-verified PDF, or returned an explicit 404/410; inspect the
`not_found` count before asserting year coverage. A nonzero exit code means a
required catalogue, transfer or integrity check failed. Repeating a command
creates a fresh report and reuses a cached PDF only if its bytes still match
the saved SHA256; metadata is always fetched again. `--refresh` rechecks PDFs
for corrections, preserving content-changed originals at new checksum paths.
Interrupted runs retain their manifest and cache of completed requests.

Report CSVs are semicolon-delimited UTF-8 with BOM for Excel. The station
inventory retains raw catalogue fields, WGS84 longitude/latitude, Swiss LV95
E/N, current GeoJSON sampling span and sensor definitions, plus the original
station-sheet URL and retrieval result. Station sheets are archived as PDFs,
not silently merged into the inventory: values can disagree. For example,
P_NEG's sheet/yearbook labels its altitude **1395 m**, while the current
WGS84 GeoJSON point has a Z coordinate of about **1445 m**. The difference may
reflect vertical reference systems. Keep each value with its source until
the reconciliation stage.

On 15 October 2026, the public precipitation CSV listed 40 entries, 38 marked
`si` for publication (two of these first list 2025/2026, outside the thesis
baseline). P_ST_ANT and P_CERE lack matching features in the
current GeoJSON. P_NOVAZ_TOT is a **virtual/composite** series: the provider
states it uses Chiasso through 2021 and Novazzano from 2022. It needs an
explicit station-history decision before analysis. GeoJSON `samplingTime`
describes the current istSOS feature and may differ from the PDF publication
period; neither proves historical daily availability. The catalogue starts in
1979, so this source does not fill 1951-1978 from these tables.

For the next extraction phase, each value should retain its PDF path/checksum,
station ID, PDF year, row/column (or text bounding box), printed symbol and
interpreted value, including the `-`, `+` or other footnote conventions.
Compare each extracted monthly total against the provider's printed total;
do not replace a printed monthly value by an unverified sum. Inspect station
sheets and legends for observation windows, instrumentation, relocations,
composite periods, units and QC notation before combining sources.

Source locations: [yearbook](https://hydromet.supsi.ch/dati/idrologia/),
[precipitation table](https://hydromet.supsi.ch/dati/idrologia/annuario_assets/csv/tabelle_annuario_rain.csv),
[WGS84 catalogue](https://hydromet.supsi.ch/dati/idrologia/annuario_assets/csv/geojson.json),
[LV95 catalogue](https://hydromet.supsi.ch/dati/idrologia/annuario_assets/csv/geojson2056.json).
Credit SUPSI/IST and the Canton Ticino hydrological monitoring service for
their records. Keep downloaded PDFs and reports out of Git; the project's
`.gitignore` already excludes `data/` and `runs/`.
