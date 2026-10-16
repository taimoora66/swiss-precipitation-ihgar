# Swiss precipitation observations and gridded estimates

## Project overview

This MSc thesis at the University of Milan examines whether additional, carefully assessed Swiss station observations can improve daily gridded precipitation estimates for Alpine catchments contributing water to northern Italy. The work is supervised by Prof. Maurizio Maugeri and Dr. Veronica Manara.

The research group's existing precipitation dataset spans **1951–2023** at approximately **30 arc-second** spatial resolution. Its Swiss sector motivates further work because station coverage, station histories, metadata consistency and Alpine topography may affect gridded estimates. The project will expand and assess the observational basis, use the group's **IH-GAR** gridding workflow, and test the resulting estimates against independent evidence. Better performance is a hypothesis to evaluate, not a presumed outcome.

## Research questions

1. Which Swiss precipitation records are suitable for inclusion after station identity, metadata, coverage, quality and homogeneity checks?
2. How does an expanded, documented station network affect daily gridded precipitation, including its spatial pattern, seasonal behavior and extremes?
3. Do the revised estimates perform better at independent stations and in hydrological comparisons for catchments draining toward Italy?

## Study area and observations

The core study area consists of Swiss catchments relevant to flows toward northern Italy. Stations near the Italian border and selected stations farther north, including **southern Bern and Vaud**, may provide useful interpolation support. Catchment boundaries, topography, record quality and spatial influence will guide inclusion; a canton border alone does not define a drainage area.

| Potential source | Role and assessment needed |
| --- | --- |
| MeteoSwiss | Manual and automatic station metadata and available daily/monthly observations; reconcile overlapping station identities and measurement definitions. |
| Canton Ticino | Additional cantonal station histories and observations, subject to access and parameter suitability. |
| WSL | Complementary records where the measured precipitation quantity and siting are appropriate for this analysis. |
| Agrometeo | Additional agricultural-network observations, conditional on temporal coverage, instrumentation and comparability. |
| SUPSI/IST Ticino hydrological yearbook | Cantonal precipitation station sheets and station-year daily/monthly PDF tables; reconcile station histories and printed symbols before use. |

All provider files will be retained in their original form with acquisition dates and source information. Network membership does not by itself establish that records are comparable or usable for climate analysis.

## Planned methodology

**1. Acquire and inventory.** Discover station files and metadata from each source through documented provider-specific acquisition procedures. Record station codes, names, coordinates, elevation, parameters, operating periods, data provenance and download outcomes. Identify duplicated sites and historical changes in station identity.

**2. Define the analysis network.** Relate stations to the target catchments and surrounding interpolation support area. Examine overlap with 1951–2023 and the timing of network changes. Select records according to documented scientific criteria rather than a fixed canton list alone.

**3. Assess observations.** Examine missing and duplicate dates, suspect values, precipitation definitions and accumulation windows, metadata conflicts, relocations and instrument changes. Assess homogeneity where the record and neighboring evidence permit. Record each decision to accept, exclude or adjust a series and preserve raw-to-result traceability.

**4. Update the grid.** Incorporate accepted records into the research group's IH-GAR workflow. Keep the baseline and expanded-network runs comparable, document configuration changes, and evaluate differences by period, season, elevation and region.

**5. Validate independently.** Use suitable withheld station records and diagnostics to assess predictive skill without evaluating solely at assimilated stations. Explore catchment precipitation and discharge consistency, particularly in the Ticino system, while accounting for evapotranspiration, storage changes, measurement uncertainty and managed transfers. The feasible extent and period of a Ticino or Lake Maggiore water balance depend on reliable hydrological inputs. A water-balance mismatch alone cannot identify precipitation bias.

## What this repository currently contains

[`scripts/meteoswiss_download.py`](scripts/meteoswiss_download.py) discovers provider-published assets in three MeteoSwiss collections (`ogd-nime`, `ogd-smn`, `ogd-smn-precip`) and retrieves available station metadata, daily files and monthly files. It retains original bytes, checks file integrity and basic structure, and records acquisition outcomes so interrupted runs can be examined and resumed.

[`scripts/agrometeo_download.py`](scripts/agrometeo_download.py) archives expanded public station metadata, the meteorological station selector and sensor definitions, then requests **measured-only daily and monthly precipitation** for their union of station IDs. It retains original JSON responses, produces station CSVs with source references, flags metadata disagreements and reports structural coverage. Other currently station-linked variables can be requested with `--all-sensors`. Both scripts use Python 3.10+ and the standard library. See the [Agrometeo acquisition guide](docs/agrometeo_acquisition.md) for commands, folders, resume behavior and limitations.

[`scripts/supsi_download.py`](scripts/supsi_download.py) archives the SUPSI/IST precipitation catalogue, WGS84 and LV95 location files, station sheets and available station-year rain PDFs. It keeps original files with checksums, an availability plan and per-request reports. The default range ends in 2023 to match the thesis baseline; PDF transcription is a later stage. See the [SUPSI acquisition guide](docs/supsi_acquisition.md) for commands and source-specific caveats.

[`scripts/supsi_extract.py`](scripts/supsi_extract.py) transcribes the SUPSI PDF daily cells and printed monthly totals into source-linked CSVs after acquisition. It keeps dashes and printed suffixes unresolved and reports layout, arithmetic and partial-period issues. It requires `pdfplumber`; see the [SUPSI extraction guide](docs/supsi_extraction.md). These transcriptions are not quality-controlled precipitation series.

[`scripts/supsi_organize_validate.py`](scripts/supsi_organize_validate.py) independently checks the PDF cells and exports station-specific daily, monthly and catalogue tables. [`scripts/supsi_combine_stations.py`](scripts/supsi_combine_stations.py) builds all-station tables while preserving the current-catalogue scope of its location fields. [`scripts/supsi_site_history.py`](scripts/supsi_site_history.py) adds each archived yearbook's printed coordinates and altitude to date-specific observation views, keeping a separate current catalogue snapshot and explicitly flagging the Chiasso–Novazzano series transition. See the [station organization](docs/supsi_station_organization.md), [combined tables](docs/supsi_combined.md) and [site history/QGIS](docs/supsi_site_history.md) guides. None of these joins establishes physical relocation dates or precipitation quality.

[`scripts/wsl_lwf_download.py`](scripts/wsl_lwf_download.py) archives the publicly published WSL LWF meteorology station CSVs on EnviDat with dataset metadata, checksums and a station-file inventory. These are hourly historical snapshots, and the public LWF catalogue does not represent every WSL station. See the [WSL LWF acquisition guide](docs/wsl_lwf_acquisition.md).

[`scripts/wsl_lwf_screen.py`](scripts/wsl_lwf_screen.py) verifies the archived WSL LWF station files and reports annual coverage, missing cells and timestamp anomalies without calculating daily rainfall. See the [WSL LWF screening guide](docs/wsl_lwf_screening.md).

Acquisition and transcription checks establish transfer integrity and basic PDF arithmetic, **not scientific validity**. The scripts do not select catchments, homogenize observations, run IH-GAR or perform water-balance analysis. Agrometeo's public catalogues do not establish completeness of retired stations or past instrument metadata. Daily accumulation windows and comparability across providers require separate verification. Other Canton Ticino sources, additional WSL archives and the later scientific stages remain planned work and will be described as completed only after implementation and verification.

## Expected research outputs

- A traceable inventory of Swiss station sources, identities, parameters and temporal coverage.
- Documented station selection, quality and homogeneity assessments.
- Comparable baseline and updated daily precipitation grids, subject to access to the research group's workflow.
- Independent station and hydrological evaluations, with uncertainty and limitations reported alongside any improvement.

## Data and reproducibility

This repository is for **code and project documentation**. It does not redistribute downloaded station observations, provider metadata files or local run logs. Obtain records directly from their respective providers under the applicable terms. Retain original inputs and record the source URL, acquisition time, provider attribution, file checksums, code version and every subsequent transformation. A later download from a changing live archive may produce a different input set; the archived originals and manifests identify what a particular analysis used.

For MeteoSwiss data, consult the [official open-data documentation](https://opendatadocs.meteoswiss.ch/) and attribute the observations to **MeteoSwiss**. Later scientific outputs should cite the underlying gridded dataset and methods according to the research group's guidance.
