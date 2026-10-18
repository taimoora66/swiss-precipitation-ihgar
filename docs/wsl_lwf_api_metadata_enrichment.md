# WSL LWF API workbook: sourced station metadata

The API-only workbook contains the new downloaded precipitation observations.
This step adds **metadata only** from independent WSL-related primary sources.
No PANGAEA or 2016 archive precipitation values are imported.

## Evidence and scope

- The [PANGAEA 2016 LWF station series](https://doi.pangaea.de/10.1594/PANGAEA.868390)
  links individual forest/open-field station records. Each station page includes
  an event name, station code, latitude, longitude, elevation and historical
  dataset coverage. These station locations are more precise than site-level
  plot centroids or elevation ranges. All 13 mapped DOIs are recorded in the
  workbook. The user previously compared the 13 published products with the
  EnviDat archive: all 1,949,830 shared numeric precipitation cells agreed at
  the printed precision. This is strong historical series identity evidence,
  but does not establish whether any gauge was relocated after 2016.
- The [2018 LWF methods paper, Table 1](https://link.springer.com/article/10.1007/s13595-018-0709-7/tables/1)
  describes the **historical network equipment**: ARG 100 unheated tipping
  buckets, 1.5 m above ground, 0.2 mm per tip, nominal hourly/10-minute
  resolution. The specific current gauge configuration is not verified.
  The [article's methods and limitations](https://link.springer.com/article/10.1007/s13595-018-0709-7)
  confirm UTC and describe freezing-related precipitation problems.
- [WSL says](https://lwf.wsl.ch/en/) that it runs and finances the LWF programme
  and owns the LWF infrastructure. It is the network owner/operator. The
  [2026 WSL site metadata table](https://lwf.wsl.ch/fileadmin/user_upload/WSL/Microsite/LWF/data/LWF_Metadata-2026_V2-EN.pdf)
  labels meteorological measurement as ongoing at these sites, but does not
  establish a gauge-by-gauge relocation or equipment history.
- First/last hourly timestamps and first/last observed numeric hourly entries
  **remain the acquired API's fields** in the workbook. Historical PANGAEA
  coverage must never be substituted for the downloaded API coverage.

## Station-specific crosswalk (historical PANGAEA event metadata)

| API model | Historical code | Longitude | Latitude | Elevation m | DOI suffix |
|---|---:|---:|---:|---:|---:|
| celerina_bestand_6 | CLB | 9.88881 | 46.49213 | 1883 | 868406 |
| celerina_freiland_7 | CLF | 9.87587 | 46.50608 | 1764 | 868405 |
| chironico_bestand_8 | CIB | 8.81281 | 46.44623 | 1416 | 868403 |
| chironico_freiland_9 | CIF | 8.81822 | 46.44087 | 1483 | 868404 |
| isone_bestand_12 | ISB | 9.00769 | 46.12506 | 1214 | 868407 |
| isone_freiland_13 | ISF | 9.00731 | 46.12608 | 1152 | 868408 |
| lens_bestand_20 | LEB | 7.43520 | 46.26837 | 1094 | 868461 |
| nationalpark_bestand_22 | NAB | 10.23047 | 46.66164 | 1893 | 868417 |
| nationalpark_freiland_23 | NAF | 10.24102 | 46.65964 | 1920 | 868416 |
| novaggio_bestand_26 | NOB | 8.83428 | 46.02162 | 900 | 868437 |
| novaggio_freiland_27 | NOF | 8.83519 | 46.02417 | 1074 | 868436 |
| visp_bestand_33 | VSB | 7.85708 | 46.29691 | 694 | 868445 |
| visp_freiland_34 | VSF | 7.85828 | 46.29817 | 650 | 868443 |

Open each record as `https://doi.pangaea.de/10.1594/PANGAEA.<DOI suffix>`.
These are historical **station event** elevations. The site-level altitude in
the WSL site page or in the 2018 paper is a different spatial unit and may not
match the rain gauge. Longitudes and latitudes use EPSG:4326 degrees.

## Run in Windows PowerShell

Save `wsl_lwf_enrich_api_metadata.py` beside the revised
`wsl_lwf_api_excel.py` in the project's `scripts` folder. Rerun the API-only
exporter first to make the three-sheet hourly/daily/monthly workbook. The
completed workbook must be pointed to by
`working/wsl_lwf/api_only_excel/latest.json`.

```powershell
Set-Location 'C:\Users\Admin\Documents\Swiss_Precipitation_Thesis'
py -3 .\scripts\wsl_lwf_enrich_api_metadata.py --root .
if ($LASTEXITCODE -ne 0) { throw 'WSL metadata enrichment failed' }

$enriched = Get-Content '.\working\wsl_lwf\api_only_excel\metadata_enriched\latest.json' -Raw |
    ConvertFrom-Json
$enriched | Select-Object status, station_count,
    precipitation_sheets_byte_identical_to_input, workbook, qgis_metadata_csv |
    Format-List
Invoke-Item $enriched.workbook
```

Output is under
`working/wsl_lwf/api_only_excel/metadata_enriched/20261017T122441Z/<timestamp>`:
the new workbook, a 13-row semicolon-delimited `Station_metadata_QGIS.csv`,
and `summary.json`. The original API-only workbook and its `latest.json` stay
unchanged. The enriched workbook has the same three sheets; the entire
`Hourly_daily_grid` and `Daily_month_grid` OOXML is copied byte-for-byte.
The script verifies the
source workbook hash, the set of 13 station IDs and saved metadata values.

For QGIS, import the metadata CSV with X=`longitude`, Y=`latitude`, CRS
EPSG:4326. Label these locations as **historical 2016 station coordinates**.
Do not state that the instrument or location stayed fixed until the last
API record without gauge/site change records from WSL.
