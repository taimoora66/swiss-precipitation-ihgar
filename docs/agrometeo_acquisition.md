# Agrometeo acquisition

Run commands from the existing `Swiss_Precipitation_Thesis` project directory. Keep
`scripts/agrometeo_download.py` beside `scripts/meteoswiss_download.py`: it reuses
the latter's HTTP, logging, locking and atomic JSON helpers. No third-party
Python packages are required; Python 3.10 or later is needed.

The downloadable `Agrometeo_Update.zip` is an installation/update bundle, not a
data archive. Extract it into the existing thesis project directory. It contains
the new script, this guide, tests and the updated project README. It does not
replace the MeteoSwiss script or downloaded observations. GitHub publishing from
the assistant was denied by the connection, so commit and push these files from
your already authenticated PC after installation.

## Run on Windows / PowerShell

```powershell
cd "$env:USERPROFILE\Documents\Swiss_Precipitation_Thesis"
Expand-Archive -LiteralPath "$env:USERPROFILE\Downloads\Agrometeo_Update.zip" -DestinationPath . -Force

# First check metadata acquisition and review the discovered station inventory.
python .\scripts\agrometeo_download.py --metadata-only

# Pilot: two stations, daily and complete monthly precipitation for 2003.
python .\scripts\agrometeo_download.py --stations 3 8 --start 2003-01-01 --end 2003-12-31

# All publicly discoverable stations: precipitation, daily and monthly.
python .\scripts\agrometeo_download.py
```

If `python` is unavailable, use the Python launcher `py -3` in its place. A
successful acquisition does not mean that every requested date has a value.
Check both `summary.json` and `station_coverage.csv`. The script exits with a
nonzero status if a request or structural check fails.

The default requested period is **1951-01-01 through yesterday in UTC**, without
truncating records to potentially erroneous station service dates. The 1951
request boundary matches the thesis baseline; it is not a claim that this
network has observations since 1951. The current incomplete month is excluded
from monthly requests. Explicit partial boundary months are also excluded. For
a baseline-only download:

```powershell
python .\scripts\agrometeo_download.py --end 2023-12-31
```

To acquire other variables as well, choose one of these scopes:

```powershell
# All currently station-linked sensors, plus precipitation: all their published
# avg/min/max/sum aggregations, excluding range-dependent cumulative products.
python .\scripts\agrometeo_download.py --all-sensors

# A specific selection: precipitation and average/minimum/maximum temperature.
python .\scripts\agrometeo_download.py --sensors 6:sum 1:avg 1:min 1:max
```

The larger scope needs more time, storage and server work. It does not establish
that every historic sensor is represented in the current catalogue, or that
every linked variable is directly measured (e.g. reference evapotranspiration
is a derived quantity). No units or aggregation labels are interpreted beyond
the provider's published definitions. For `--all-sensors`, a sensor with no
published aggregation list uses its provider `default_agg` and needs particular
scientific review.

## What is retained

| Location relative to project root | Contents |
| --- | --- |
| `data/raw/agrometeo/metadata/<sha256>/` | Original station/selector/sensor JSON responses. Expanded station metadata includes sensors, owner, station type and tags. |
| `data/raw/agrometeo/daily/<sha256>/` | Original measured-only daily API responses, in batches of station IDs and date chunks. |
| `data/raw/agrometeo/monthly/<sha256>/` | Original complete-month API responses. |
| `data/state/agrometeo.sqlite3` | Request cache mapping URLs to local paths and checksums. |
| `data/derived/agrometeo/<run_id>/daily/AGROMETEO_<id>.csv` | Per-station daily extracts with original response values and source JSON path, SHA256 and row number. |
| `data/derived/agrometeo/<run_id>/monthly/AGROMETEO_<id>.csv` | Equivalent per-station monthly extracts. |
| `runs/agrometeo/<run_id>/` | Configuration, plans, inventories, coverage, manifests, errors, events and summary. |

Agrometeo serves parameterized JSON responses rather than MeteoSwiss-style
station historical CSV assets. A batch response is therefore the original
source object. Station CSVs are explicitly derived extracts. Their `date`
column contains dates only (`YYYY-MM-DD` daily; `YYYY-MM` monthly); values are
not converted, adjusted or filled. Null becomes an empty CSV field, and an
actual zero remains zero. Missing dates are not manufactured. Original timestamp
strings and the distinction between omitted/null/blank fields remain in JSON.
Each exported CSV has a checksum in `station_coverage.csv`.

Existing `.gitignore` rules exclude `data/` and `runs/`. Commit only source code,
tests and documentation. Provider metadata can contain staff contact details;
the script does not export those into the station inventory or publish them.

## Reports to inspect

- `station_inventory.csv`: all discovered stations, provider-specific station
  ID, name, canton tags where present, EPSG:4326 coordinates, elevation, current
  station type, sensor IDs, original metadata service date, metadata flags and
  source references. Blank canton values remain unknown. Canton tags do not
  establish catchment membership.
- `metadata_conflicts.csv`: station-name, coordinate, elevation, service-date
  and active-status differences between the expanded station catalogue and
  meteorological selector. The expanded catalogue is the inventory's primary
  source when an ID occurs in both; both originals remain archived. No location
  histories or identity links are inferred from these differences.
- `sensor_inventory.csv`: all public sensor definitions, units and aggregation
  descriptions, including definitions not requested for observations.
- `station_coverage.csv`: per-station/sensor/aggregation/scale numeric coverage,
  first/last numeric date, missing/nonnumeric cells and negative precipitation
  counts. Target counts use 26,663 daily periods or 876 monthly periods for
  1951–2023. Full-target missing counts are left blank for restricted date pilots.
  `requests_complete=false` distinguishes failed retrieval from an API response
  with no observations. Numeric values, including possible numerical sentinels,
  are **not** certified usable observations.
- `download_manifest.csv`: every metadata/data request, status, original file,
  checksum, bytes, acquisition time and structural response statistics. Any
  periods outside the request are counted, retained in raw JSON and excluded
  from the extract.
- `summary.json` / `errors.json`: completion scope and failures.

All report CSVs use **semicolon delimiters and UTF-8 with BOM**, for Excel and
Power Query. Station inventory coordinates can be loaded in QGIS as EPSG:4326.
`metadata_service_start` is the original provider string, not observed record
start. Observed first/last numeric dates are in `station_coverage.csv`.

## Resume, refresh and failure handling

Repeat the **same command** after interruption. Observation responses already
archived for identical request URLs are reused only after verifying their local
SHA256 and structural schema. Metadata is always requested again. A fresh run
directory is written, and derived files are rebuilt from the available archived
responses. Resume is at request level; interrupted transfers are downloaded
again. Changing batch size, station selection, sensor specs, dates or chunking
changes URLs and may require additional requests.

Hash-verified reuse preserves the earlier downloaded snapshot; it does not check
whether the live provider has since corrected historical values. Use
`--refresh` to requery observations. New contents receive a new checksum path,
so earlier versions remain available. Keep earlier run manifests to identify
the exact input version of an analysis. No provider checksum was available in
these responses; a local hash establishes local identity, not independent
confirmation of the provider's values.

Transfers run sequentially with a one-second pause by default, bounded retries,
timeouts and backoff for transient HTTP errors. Do not start multiple Agrometeo
runs against the same output folder. If a large response fails, decrease
`--batch-size` or `--chunk-years` (defaults 10 stations and 5 years). A response
with unexpected keys, duplicate dates, wrong schema or new pagination fails
the relevant operation; these cases are not reported as successful empty data.

Statuses `completed_selected_scope` and `completed_requested_public_scope`
describe acquisition only. They do not certify completeness of the historical
network, continuity, quality, homogeneity, or suitability for IH-GAR.

## Scientific interpretation and source access

Verified source endpoints on 14 October 2026:

- [Public station catalogue with expanded metadata](https://agrometeo.ch/backend/api/stations?include=sensors%2Cowner%2Ctype%2Ctags)
- [Meteorological station selector](https://agrometeo.ch/backend/api/meteo/stations)
- [Public sensor definitions](https://agrometeo.ch/backend/api/sensors)
- [Example measured-only daily precipitation request](https://agrometeo.ch/backend/api/meteo/data?from=2003-01-01&to=2003-01-07&scale=day&sensors=6%3Asum&stations=8&measured=1)
- [Agrometeo meteorology interface](https://www.agrometeo.ch/de/meteorologie)

The public interface exposes a measured-only switch (`measured=1`). The script
uses it for every data request. This requests the provider's measurement-only
selection; it does not establish the internal QC, gap-filling or partial-day
aggregation rules. Sensor 6 is defined as precipitation, in mm (or litres/m²),
with `sum` aggregation. Definitions give station-dependent resolution
0.1/0.2/1.0 mm, without establishing a station-by-station instrument history.

The live expanded catalogue returned **220 station IDs**, all marked active,
while the meteorological selector returned **218**. Their union was 220. The
catalogues contained coordinate disagreements and a service date `0025-11-07`;
the script preserves and flags these. Daily numeric responses were retrieved
for CHANGINS in January 1999, earlier than the 2003 start described on an
Agrometeo overview page. Neither service dates nor returned numbers alone
establish an observation's authenticity or suitability. These observations
motivate acquisition without clipping to service dates and later detailed QC.

Before ingestion, verify daily accumulation timezone/window and daylight-saving
handling, wet-day definitions, partial-interval completeness, missing-value
codes, gauge type/heating and snow capture, exposure, relocations, duplicate
sites across providers and the provenance of corrections. Preserve the raw
Swiss coordinate fields without assigning axes from their names; the published
WGS84 fields are used for the inventory. Do not merge `replacement_id` pairs
without station-history evidence. Current public catalogues do not prove that
retired stations or historic sensor assignments are complete. Further historical
metadata and clarification may need to be requested from Agrometeo/Agroscope.

Attribute the observations to **Agrometeo / Agroscope and the relevant station
providers**. Public read access is not by itself a redistribution licence.
This repository publishes acquisition code, not station observations; consult
the provider about terms before redistributing data.

## Verification

```powershell
python -m unittest discover -s tests -v
```

The tests use local fixtures and an HTTP server; they do not bulk-download live
data. Live pilots should additionally check one station against the web
interface and compare a month's daily sum with the independent provider monthly
response. Agreement is an aggregation check, not proof of measurement quality.

Implementation validation on 14 October 2026: nine fixture tests passed,
including failed HTTP responses, cache reuse, corrupted-file recovery,
explicit refresh, duplicate timestamps, catalogue conflicts and missing/zero
handling. A live pilot for station IDs 3 and 8 over calendar year 2003 returned
365 daily rows and 12 monthly rows per station. Decimal sums of daily
precipitation matched all 24 provider monthly totals exactly. Repeating the
pilot reused both observation responses after hash verification. A second live
pilot with `--all-sensors` also completed both daily and monthly requests. These
pilots establish operation for those cases, not completeness of the full archive
or scientific validation of any measurements.
