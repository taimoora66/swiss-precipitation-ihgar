# WSL LWF public meteorology acquisition

The downloader archives the public [EnviDat LWF meteorology datasets](https://www.envidat.ch/metadata?q=lwfmeteo) whose catalogue names match `lwfmeteo-*`. It queries CKAN `package_search` with pagination, fetches each complete `package_show` record, and downloads every station observation CSV. At the time this script was written, the public search returned **16 datasets and 30 station CSV resources**. The number is discovered anew on every run, not hardcoded. Some datasets contain only a forest station. Several CSVs are titled "Data plot" in the catalogue; the script checks the file format, `.csv` filename and `timestamp;...` header instead of relying on the title. The CSVs are published hourly snapshots (often around 1996/1997 onwards), not a complete 1951–2023 station network.

WSL states that its [LWF program](https://lwf.wsl.ch/en/daten/) has longer research records; historical data for other LWF sites can be [requested from WSL](https://webapps.wsl.ch/lwf-data-request-form/). This script does not bypass that request process. WSL also publishes other meteorological datasets, such as [Stillberg](https://www.envidat.ch/metadata/meteo-stillberg), which are outside this clearly defined LWF catalogue acquisition. A `completed_public_LWF_scope` status means the current public `lwfmeteo-*` resources were archived, not that every WSL station or thesis year is covered.

## Run on Windows PowerShell

Unzip the update into `C:\Users\Admin\Documents\Swiss_Precipitation_Thesis`, retaining the `scripts` and `docs` folders. From that project folder:

```powershell
py -3 scripts\wsl_lwf_download.py --metadata-only
py -3 scripts\wsl_lwf_download.py --dataset lwfmeteo-isone
py -3 scripts\wsl_lwf_download.py
```

The first command checks the catalogue and creates a plan. The second is a one-dataset pilot. The last downloads or verifies all public LWF meteorology station CSVs. Reruns verify cached file SHA-256 values; use `--refresh` to archive any changed upstream files as new content-addressed copies. `--all-resources` additionally archives plot PDFs and photographs. Python 3.10+ standard library suffices; `meteoswiss_download.py` must remain alongside this script.

Output paths:

- `data/raw/wsl_lwf/metadata/<sha256>/...`: exact CKAN search and dataset responses.
- `data/raw/wsl_lwf/observations/<sha256>/...`: untouched station CSVs.
- `data/state/wsl_lwf_resources.json`: verified local resource index.
- `runs/wsl_lwf/<UTC-run>/catalogue_station_resources.csv`: all discovered public station resources and their catalogue metadata, produced even with `--metadata-only`.
- `runs/wsl_lwf/<UTC-run>/station_inventory.csv`: semicolon-delimited station-file overview including source URL, DOI, licence identifier, checksum, first/last timestamp, observed and missing `precip` cells, and the dataset-level spatial point. The dataset spatial point is **not** independently verified coordinates for each forest/open-field sensor.
- `runs/wsl_lwf/<UTC-run>/resource_manifest.json`, `download_plan.json`, `catalogue_snapshots.json`, `errors.json`, `events.jsonl`, `summary.json`: complete provenance and run outcome.

Check `summary.json`: a failure status requires review of `errors.json`; a successful pilot is labelled `completed_selected_scope`. A blank or `NA` precipitation cell is missing data, not zero. The script does **not** sum hourly observations into daily precipitation: confirm the CSV's timestamp convention, precipitation accumulation interval, units, forest versus open-field measurement, snow treatment, and any provider quality flags first. The Isone open-field pilot had repeated/out-of-order timestamps, which the inventory counts but does not silently fix. Open-field gauges are the more relevant candidate for an ordinary precipitation grid; forest measurements may measure throughfall or otherwise represent a different exposure. Cross-check coordinates, elevations, and overlap with MeteoSwiss/SUPSI before accepting a new station into IH-GAR.

The catalogue's `license_id` is retained per dataset. Review each dataset's actual licence, citation, and WSL data agreement before redistributing derived outputs. Preserve the original DOI in any scientific inventory.
