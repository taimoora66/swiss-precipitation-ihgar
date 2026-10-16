# SUPSI yearbook site history and QGIS layer

Run this stage after the PDF transcription and combined-table audits succeed.
From the thesis project root in PowerShell:

```powershell
$id = "20261015T095609Z_60a4f441_7ac205a1"
python .\scripts\supsi_site_history.py --root . --extraction-id $id
if ($LASTEXITCODE -ne 0) { throw "SUPSI site history failed" }
Get-Content ".\working\supsi\site_history\$id\summary.json"
```

The script needs `pdfplumber`, already used by the SUPSI extraction. It reads
the acquisition inventory, extraction review, archived PDFs and verified
combined daily/monthly tables. It checks each archived PDF SHA-256, parses the
printed header, joins observations by station and year to the *same* source
PDF, and checks the output counts. It refuses to overwrite an existing run.

Outputs under `working\supsi\site_history\<extraction_id>\`:

| File | Grain and purpose |
| --- | --- |
| `site_segments.csv` | One row per contiguous period with the same reported site and series source; 42 rows in the 2026-10-15 extraction. |
| `station_year_reported.csv` | One row per source yearbook; 794 rows in that extraction. |
| `daily_with_reported_site.csv` | Original combined daily rows plus that year's PDF header fields and flags. |
| `monthly_with_reported_site.csv` | Original combined monthly rows plus that year's PDF header fields and flags. |
| `summary.json` | SHA, station, segment, observation and status counts, and the acquisition inventory hash. |

`pdf_reported_lv95_easting_m`, `pdf_reported_lv95_northing_m`, and
`pdf_reported_elevation_m` are transcribed from the PDF header. The PDF prints
**northing / easting**; the table names and orders the separate coordinates as
**easting, northing** for GIS. `current_catalogue_*` fields represent the
acquisition snapshot, not the location of every historical observation. The
three `candidate_source_*` fields are populated only for `P_NOVAZ_TOT` before
2022, using the catalogue entry for Chiasso. They are **unverified historical
coordinates**, not an approved replacement for the printed header.

The five IDs `P_CAL`, `P_GNO`, `P_GRA`, `P_MAG`, and `P_MEN` have changed PDF
header locations/elevations after 2018. This establishes a change in printed
metadata; it does not establish the reason, the date of a physical move, or
observation homogeneity. `P_CERE` and `P_ST_ANT` have PDF header locations but
no current catalogue location. The `P_NOVAZ_TOT` catalogue says Chiasso rainfall
was used through 2021 and Novazzano from 2022, whereas the yearbooks print the
Novazzano header for both eras. Confirm the actual gauge locations before
assigning earlier precipitation to a spatial point.

## QGIS

Use **Layer > Add Layer > Add Delimited Text Layer** with `site_segments.csv`,
UTF-8 and semicolon delimiter. Set **X** to
`pdf_reported_lv95_easting_m`, **Y** to
`pdf_reported_lv95_northing_m`, and the geometry CRS to **EPSG:2056**.
The initial layer has 42 period rows, with repeated station IDs where needed.
For a display of *reported* PDF locations that omits the misleading older
composite location, apply this layer filter:

```qgis
"site_status" <> 'composite_pre2022_location_unresolved'
```

This leaves 41 periods in the referenced extraction. Save to a GeoPackage if
a permanent QGIS layer is needed; retain `first_year`, `last_year`,
`site_status`, source paths and distinct metadata fields. Filtering the
display does not establish scientific station suitability. Before gridding,
resolve station identities, source symbol meanings, daily accumulation windows,
quality and homogeneity separately. These outputs do not perform that QC.
