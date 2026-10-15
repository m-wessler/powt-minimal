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

Region, station network and NBM variables are set in the **User Configurations** block near the top of the script. The current settings are the Western Region and the NWS network.

## Pipeline

1. **Observations.** The script queries the Synoptic timeseries API once per day for weather condition, air temperature and RH. It computes wet-bulb temperature and parses the weather-condition strings into boolean flags: `RA`, `SN`, `ZR`, `PL` and `UP`. Each ob's time is rounded to the nearest hour. Where a station has more than one ob in the same hour (routine plus specials), only the ob closest to the top of the hour is kept.
2. **NBM download.** For each init time it reads the GRIB `.idx` file from S3 and does byte-range downloads of only the TMP, PTYPE, SNOWLVL and RH messages. Any init times that aren't available on S3 are listed in a warning at the end of the download step.
3. **Point extraction.** It maps each station to its nearest NBM grid point. Stations more than `max_grid_distance_km` (5 km) from that point are outside the NBM domain and are dropped. It then reads every GRIB file with pygrib and fills in forecast values at the matching valid times. This step is single-threaded and is the slowest part of a run.
4. **Archive CSV.** It renames columns, converts K to °F and m to ft, computes forecast wet-bulb temperature and writes the archive CSV.
5. **Combine.** `combine_powt_archives.py` reads every archive CSV, adds `fhr` from the filename, drops rows repeated across overlapping runs and writes one Parquet file per region.

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

It prints the dates, forecast hours and CWAs in the file, writes one PNG per figure and a `qc_summary.txt` to the output directory. Dates are valid dates, and the end date includes all hours. Before plotting it:

- drops `WX_BAD` rows, when the column is present
- removes SN/ZR/PL/UP obs with observed Tw > 36°F, RA obs with Tw < 28°F, and rows where |FXT − T| > 25°F
- scales probabilities to 0–1; figures treat a type as forecast at a probability of 15%

`PoWT_Analysis.ipynb` runs in Colab on its own. It downloads the region's dataset from Google Drive (set the file IDs in its **Configuration** cell) and shows the same figures in tabs. Its analysis functions are a copy of the block marked SHARED in `powt_figures.py`, so a change to one has to be made in the other.
