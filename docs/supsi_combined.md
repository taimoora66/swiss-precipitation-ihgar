# SUPSI all-station register and observation tables

Run after `supsi_organize_validate.py` completes successfully. From the thesis
project root in PowerShell:

```powershell
$id = "20261015T095609Z_60a4f441_7ac205a1"
python .\scripts\supsi_combine_stations.py --root . --extraction-id $id
if ($LASTEXITCODE -ne 0) { throw "Read runs\supsi_combined\$id\summary.json" }
Get-Content ".\runs\supsi_combined\$id\summary.json"
```

Outputs:

```text
working\supsi\combined\<extraction_id>\stations.csv
working\supsi\combined\<extraction_id>\catalogue_all.csv
working\supsi\combined\<extraction_id>\daily.csv
working\supsi\combined\<extraction_id>\monthly.csv
runs\supsi_combined\<extraction_id>\summary.json
```

`stations.csv` contains all 36 station IDs with archived yearbooks, their
catalogue names, current WGS84 latitude/longitude, LV95 eastings/northings,
both published height fields, sensor IDs, coverage, and review flags.
`catalogue_all.csv` preserves all 40 catalogue entries and marks whether
each has a yearbook in this extraction. Two stations with yearbooks lack
current catalogue coordinates: P_CERE and P_ST_ANT. Six have known site
history questions: P_CAL, P_GNO, P_GRA, P_MAG, P_MEN, P_NOVAZ_TOT. A current
catalogue coordinate must not be assigned as a verified historical position.
For analysis, use `lv95_elevation_m` as the explicitly labeled Swiss national
height, keeping `wgs84_elevation_m` separate.

The two combined observation files have 290,000 dated daily rows and 9,528
printed monthly rows. Each row retains its `station_id`, `collection`, joined
`catalogue_*` fields, original PDF token, flags, source path and SHA256. The
monthly printed total and calculated numeric daily sum remain separate. The
script checks station metadata against the original acquisition inventory,
valid dates, unique ordered station-period keys, station counts against the
prior PDF audit, and every field in every combined row against the audited
station CSV. Any error blocks publishing the combined files. It will not
overwrite an existing reviewed combined run.

In Excel, use **Data > From Text/CSV** with UTF-8 and semicolon delimiter.
Keep the `YYYY-MM` monthly key as text. These combined tables fit Excel's row
limit, but Power Query is the safer way to import the large `daily.csv` and
preserve `raw_token`, `symbol`, `station_id` and SHA256 as text. `stations.csv`
can be opened as a compact station metadata index. Source PDFs and the
station-specific CSVs are not modified.

These checks prove transcription/export consistency. They do not establish
the meaning of standalone dashes or numeric suffixes, station homogeneity,
or scientific quality control.
