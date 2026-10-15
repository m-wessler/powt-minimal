# NBM PoWT Archive Generator

Builds a point archive that pairs observed precipitation type (PoWT) from Synoptic surface stations with National Blend of Models (NBM) gridded forecasts at each station's nearest grid point. The archive is used to verify NBM precipitation-type forecasts.

## Files

| File | Purpose |
|---|---|
| `powt_archive_generator.py` | Builds one archive file for a given date range and forecast hour. All of the processing happens here. |
| `combine_powt_archives.py` | Merges every archive CSV into one Parquet dataset per region. |
| `archive_generator.sh` | Batch launcher. Runs the generator in one-month chunks for forecast hours 36, 48, 72, 120 and 168, then runs the combine step. |
| `powt_figures.py` | Draws the verification figures from a combined Parquet dataset and saves them as PNGs. |
| `PoWT_Analysis.ipynb` | Colab notebook that draws the same figures interactively, in tabs. |
| `environment.yml` | Conda environment. |

## Setup

```bash
conda env create -f environment.yml
conda activate powt
export SYNOPTIC_TOKEN=<your Synoptic API token>   # https://customer.synopticdata.com/
```

Alternatively, replace the `synoptic_token` placeholder near the top of `powt_archive_generator.py` with your token (the environment variable takes precedence). Don't commit a real token.

NBM GRIB2 data comes from the public `noaa-nbm-grib2-pds` S3 bucket and needs no AWS credentials.

## Usage

Single run:

```bash
python powt_archive_generator.py <start_date> <end_date> <lead_time_hours> <forecast_frequency> \
    [--output-dir DIR] [--log-dir DIR]

# e.g. 48-h forecasts valid every 3 hours, Oct 1 00Z – Oct 10 21Z 2024
python powt_archive_generator.py 2024-10-01 2024-10-10 48 3
```

Dates are **valid times**: a run covers `start_date` 00Z through `end_date` 23Z. Init time is computed as valid time minus the lead time. This way consecutive runs (such as the launcher's month chunks) cover the period without gaps, and every lead time covers the same dates.

Full batch (defaults to 2024-10-01 through 2025-04-30), ending with the combine step:

```bash
./archive_generator.sh [start_date] [end_date]
```

Combine step on its own. It picks up every archive CSV in the directory, so it can be rerun after adding more months or lead times:

```bash
python combine_powt_archives.py [--archive-dir DIR] [--output-dir DIR]
```

`--output-dir` defaults to `$POWT_OUTPUT_DIR`, or `./data/powt` if that isn't set. `--log-dir` defaults to `$POWT_LOG_DIR`, or `./logs`.

### Configuration

Region, station network and NBM settings are set in the **User Configurations** block near the top of `powt_archive_generator.py`. The defaults are the Western Region and the NWS network.

| Setting | Options | Notes |
|---|---|---|
| `region_selection` | `WR`, `CR`, `ER`, `SR`, `CONUS`, `CWA`, `RFC` | A region uses the CWA list for that region in `cwa_list()`. `CONUS` uses all four regions. |
| `cwa_selection` | e.g. `"SLC"`, `"MSO,BOI"`, `"CBRFC"` | Used only when `region_selection` is `CWA` (one or more comma-separated CWAs) or `RFC` (one RFC basin ID). |
| `network_selection` | `NWS`, `NWS+RAWS`, `NWS+RAWS+HADS`, `RAWS`, `HADS`, `SNOTEL`, `ALL` | Synoptic station networks to query. Only stations that report present weather produce usable precip-type obs; in practice that is mostly NWS ASOS/AWOS. |
| `max_grid_distance_km` | default `5` | Stations farther than this from their nearest NBM grid point are dropped as outside the domain. |

Output filenames, and so the combined dataset name, use the region code. In `CWA` mode they use the CWA list joined with `-` (e.g. `MSO-BOI`), and in `RFC` mode the literal `RFC`.

## Pipeline

1. **Observations.** The script queries the Synoptic timeseries API once per day for weather condition, air temperature and RH. It computes wet-bulb temperature and parses the weather-condition strings into boolean flags: `RA`, `SN`, `ZR`, `PL` and `UP`. Each ob's time is rounded to the nearest hour. Where a station has more than one ob in the same hour (routine plus specials), only the ob closest to the top of the hour is kept.
2. **NBM download.** For each init time it reads the GRIB `.idx` file from S3 and does byte-range downloads of only the TMP, PTYPE, SNOWLVL and RH messages. Any init times that aren't available on S3 are listed in a warning at the end of the download step.
3. **Point extraction.** It maps each station to its nearest NBM grid point. Stations more than `max_grid_distance_km` (5 km) from that point are outside the NBM domain and are dropped. It then reads every GRIB file with pygrib and fills in forecast values at the matching valid times. This step is single-threaded and is the slowest part of a run.
4. **Archive CSV.** It renames columns, converts K to °F and m to ft, computes forecast wet-bulb temperature and writes the archive CSV.
5. **Combine.** `combine_powt_archives.py` reads every archive CSV, adds `fhr` from the filename, drops rows repeated across overlapping runs and writes one Parquet file per region.

## Precipitation-type flags

Each ob's Synoptic `weather_condition` string (e.g. `light snow,mist` or `frz drizzle/rain`) is turned into five boolean flags. Matching ignores case, and an ob can have more than one flag set.

| Flag | Set when the weather string contains | Notes |
|---|---|---|
| `RA` | `rain` or `drizzle` | Checked separately for each report in the string (split on `,` and `/`). A report containing `frz` doesn't count, but another report in the same ob still can: `frz drizzle,rain` is RA + ZR. |
| `SN` | `snow`, `grains` or `graupel` | `blowing snow` and `drifting snow` don't count, since they aren't falling snow. `light snow,blowing snow` is still SN. |
| `ZR` | `frz` | Synoptic writes freezing precipitation as `frz rain` / `frz drizzle`. `freezing fog` doesn't match. |
| `PL` | `ice pellets` | `ice` on its own doesn't count, so ice fog and ice crystals are excluded. |
| `UP` | `unknown` | Unknown precipitation, reported by automated sensors that detect precipitation but can't identify its type. |

Hail isn't a PoWT type and isn't flagged. Other weather (fog, mist, haze) is ignored and doesn't affect the flags.

Obs are matched to the hour: each ob's time is rounded to the nearest hour, and where a station has several obs in an hour (routine plus specials) only the one closest to the top of the hour is kept. `WX_BAD` marks stations that never reported any weather in the run's period; see the dataset columns below.

## Outputs (under the output directory)

| Path | Contents |
|---|---|
| `obs_json/` | Raw Synoptic API responses (cache, reused on reruns) |
| `obs/*.powt-obs.csv` | Processed observations for the date range |
| `nbm_grib/` | Subset NBM GRIB2 files (cache) |
| `nbm/tmp/*.TEMP.csv` | Checkpoint after GRIB extraction, so a run can resume without redoing it |
| `nbm/*.fXXX_freqYY.powt-nbm-obs.csv` | Archive for one run (one region, date range and lead time) |
| `dataset/NBM_PoWT_Dataset_<first>_<last>.<region>.parquet` | **Final dataset**: all runs for a region combined (gzip Parquet) |

Each archive CSV is indexed by `(timestamp, stid)`. The combined Parquet dataset is indexed by `(timestamp, fhr, cwa, stid)`, and `cwa` comes from Synoptic station metadata. Columns:

- **Observed:** `T`, `RH`, `Tw` (°F / %), plus the precip-type flags `RA`, `SN`, `ZR`, `PL`, `UP`
- **Data quality:** `WX_BAD` is True when the station reported no present weather at any time in the run's period. That usually means it has no present-weather sensor, so all-False precip flags can't be read as "no precipitation". Exclude these rows when verifying. A station that has a sensor but saw no weather all period will also be flagged; this is harmless if verification only scores hours where precipitation was observed.
- **Forecast:** `FXT`, `FXRH`, `FXTw`, `snowlvl` (ft) and the conditional precip-type probabilities `PRA`, `PZR`, `PSN`, `PPL`
- **Station:** `elevation`

If a step's output file already exists, that step is skipped. Delete the file to regenerate it. After pulling a change to the processing logic, delete the existing CSVs so they are rebuilt; the raw JSON and GRIB caches can be kept.

## Figures

`powt_figures.py` draws the verification figures (weather-type combinations, contingency tables, reliability, snow level and p-type transition by forecast temperature) from a combined Parquet dataset:

```bash
python powt_figures.py data/powt/dataset/NBM_PoWT_Dataset_<first>_<last>.WR.parquet \
    [--cwa MSO,BOI] [--fhr 48,72] [--start YYYY-MM-DD] [--end YYYY-MM-DD] \
    [--figures NAME ...] [--output-dir figures] [--dpi 150]

python powt_figures.py --list    # figure names
```

| Name | Shows |
|---|---|
| `type_combinations` | How often each exact combination of observed types, and of forecast types above 15%, occurs |
| `warm_signal_obs` | Observed types when the forecast is warm (snow level > 800 ft AGL and/or FXTw > 36°F), with and without a frozen-type forecast |
| `zr_vs_up` | Distributions of observed and forecast T, RH, Tw, snow level and probabilities for ZR and UP events |
| `probability_histograms` | Distribution of each forecast probability |
| `contingency` | Four contingency views of forecast type (> 15%) against observed type, precipitating cases only |
| `median_probability` | Median and 25th/75th percentile forecast probability of each type, by observed type |
| `pot_by_obs` | Box plots of each forecast probability when each type was and wasn't observed |
| `snowlvl_by_obs` | Box plots of forecast snow level AGL by observed type |
| `snowlvl_pot_discrimination` | Observed type frequencies split by snow level AGL and by the balance of PRA and PSN |
| `reliability` | Reliability curves with sharpness and ROC insets for rain, snow, and freezing/frozen (PZR+PPL) |
| `cold_rain_outcomes` | Observed types when only rain is forecast but FXT or FXTw is below 32°F |
| `transition_cold_rain` | Observed type by 1°F FXTw bin (24–36°F) when only rain is forecast |
| `transition_warm_snow` | Observed type by FXTw (30–36°F) when no rain but a frozen type is forecast |
| `transition_solo_fzra` | Observed type by FXTw (24–36°F) when only ZR or PL is forecast |
| `fxtw_when_psn` | Distribution of FXTw when PSN ≥ 15% |
| `fxtw_by_obs` | Distribution of FXTw for each observed type |
| `transition_frozen_fxtw_f96` | Observed type by FXTw (30–42°F) when a frozen type is forecast, FHR ≥ 96, with an 85% reference line |
| `transition_frozen_fxt_f96` | As above, against forecast dry-bulb FXT (30–55°F) |

It prints the dates, forecast hours and CWAs in the file, writes one PNG per figure and a `qc_summary.txt` to the output directory. Dates are valid dates, and the end date includes all hours. Before plotting it:

- drops `WX_BAD` rows, when the column is present
- removes SN/ZR/PL/UP obs with observed Tw > 36°F, RA obs with Tw < 28°F, and rows where |FXT − T| > 25°F
- scales probabilities to 0–1; figures treat a type as forecast at a probability of 15%

`PoWT_Analysis.ipynb` runs in Colab on its own. It downloads the region's dataset from Google Drive (set the file IDs in its **Configuration** cell) and shows the same figures in tabs. Its analysis functions are a copy of the block marked SHARED in `powt_figures.py`, so a change to one has to be made in the other.

## Known limitations

- **Not yet run against the live Synoptic API.** The pipeline was tested with synthetic Synoptic responses and real NBM data. On a first live run, look through the distinct values in the `weather_condition` column of `obs/*.powt-obs.csv` for strings the parser doesn't handle.
- **No elevation adjustment.** Forecast values are taken from the nearest grid point with no correction for the height difference between the station and the grid point. `snowlvl_agl` in the figures is forecast snow level minus station elevation.
- **CONUS only.** The NBM `co` domain is used, so Alaska, Hawaii and Puerto Rico aren't covered.
- **RFC mode is western only.** `cwa_list_rfc()` keeps stations at latitude ≥ 31° and longitude ≤ −103°, so an RFC outside that box returns few or no stations.
- **Contingency figures pick one type per ob.** In the first three `contingency` panels an ob with several types counts as the first one in RA, SN, ZR, PL, UP order (so RA+SN counts as RA). The fourth panel and the other figures use each flag on its own.
- **pandas is pinned below 3.** pandas 3 breaks the byte-range handling in `NBMGribFetcher`.
- **Figure titles leave out the region for CWA and RFC runs.** The region name comes from a two-letter code in the dataset filename, which those runs don't have.
