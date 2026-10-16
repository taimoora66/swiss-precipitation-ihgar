# First screen of WSL LWF station files

After `wsl_lwf_download.py` reports `completed_public_LWF_scope`, run from the project root:

```powershell
py -3 scripts\wsl_lwf_screen.py
```

To inspect a specific acquisition:

```powershell
py -3 scripts\wsl_lwf_screen.py --run .\runs\wsl_lwf\20261015T153606Z
```

The screen rechecks every original CSV against its saved SHA-256 and inventory, then writes to a new `runs\wsl_lwf_screen\<UTC timestamp>\` folder:

- `station_screen.csv`: source dates, raw and numeric precipitation counts, forest/open-field exposure, timestamp duplicates, gaps, and whether the **published file** has any 2023 precipitation.
- `yearly_screen_1951_2023.csv`: one row per station and year. `outside_published_span` means the public snapshot contains no rows for that year; it does **not** establish when the physical station began or ended.
- `review_issues.csv`: questions to investigate with WSL or the thesis supervisors, including missing cells, timestamp conflicts, forest exposure and the public snapshot's end date.
- `summary.json` and `errors.json`: completion and file-level errors. A nonzero exit code means some station files could not be verified or read.

The public CSV uses repeated `02:00` timestamps on the last Sunday in October in the Isone pilot, consistent with a possible Swiss civil-time clock change. This pattern is **not proof** of the provider's timestamp convention. In 2010, the two Isone open-field `02:00` rows have different precipitation values. Both observations are retained, with a separate conflict count. The screen also reports other duplicate timestamps and forward gaps without automatically modifying them.

This is a structural screen. It does not produce daily sums, count usable complete days, compare provider rainfall with MeteoSwiss, resolve snow gauge biases, homogenize records, or approve stations for IH-GAR. First establish timezone and timestamp-ending/starting convention, rainfall accumulation interval, unit, gauge type and precipitation meaning (especially forest/throughfall). Then reconcile physical station coordinates, elevation, periods and independence of observations with the other station networks. Updated WSL records beyond the public snapshots may require the [LWF data request](https://webapps.wsl.ch/lwf-data-request-form/).
