# NBM PoWT Archive Generator

Builds a point archive that pairs observed precipitation type (PoWT) from Synoptic surface stations with National Blend of Models (NBM) gridded forecasts at each station's nearest grid point. The archive is used to verify NBM precipitation-type forecasts.

## Files

| File | Purpose |
|---|---|
| `powt_archive_generator.py` | Builds one archive file for a given date range and forecast hour. All of the processing happens here. |
| `archive_generator.sh` | Batch launcher. Runs the generator in one-month chunks for forecast hours 36, 48, 72, 120 and 168. |
| `environment.yml` | Conda environment. |

## Setup

```bash
conda env create -f environment.yml
conda activate powt
export SYNOPTIC_TOKEN=<your Synoptic API token>   # https://customer.synopticdata.com/
```

NBM GRIB2 data comes from the public `noaa-nbm-grib2-pds` S3 bucket and needs no AWS credentials.

## Usage

Single run:

```bash
python powt_archive_generator.py <start_date> <end_date> <lead_time_hours> <forecast_frequency> \
    [--output-dir DIR] [--log-dir DIR]

# e.g. 48-h forecasts from every 3rd hourly init, Oct 1–10 2024
python powt_archive_generator.py 2024-10-01 2024-10-10 48 3
```

Full batch (defaults to 2024-10-01 through 2025-04-30):

```bash
./archive_generator.sh [start_date] [end_date]
```

`--output-dir` defaults to `$POWT_OUTPUT_DIR`, or `./data/powt` if that isn't set. `--log-dir` defaults to `$POWT_LOG_DIR`, or `./logs`.

Region, station network and NBM variables are set in the **User Configurations** block near the top of the script. The current settings are the Western Region and the NWS network.

## Pipeline

1. **Observations.** The script queries the Synoptic timeseries API once per day for weather condition, air temperature and RH. It computes wet-bulb temperature and parses the weather-condition strings into boolean flags: `RA`, `SN`, `ZR`, `PL` and `UP`.
2. **NBM download.** For each init time it reads the GRIB `.idx` file from S3 and does byte-range downloads of only the TMP, PTYPE, SNOWLVL and RH messages.
3. **Point extraction.** It maps each station to its nearest NBM grid point, then reads every GRIB file with pygrib and fills in forecast values at the matching valid times. This step is single-threaded and is the slowest part of a run.
4. **Final archive.** It renames columns, converts K to °F and m to ft, computes forecast wet-bulb temperature and writes the archive CSV.

## Outputs (under the output directory)

| Path | Contents |
|---|---|
| `obs_json/` | Raw Synoptic API responses (cache, reused on reruns) |
| `obs/*.powt-obs.csv` | Processed observations for the date range |
| `nbm_grib/` | Subset NBM GRIB2 files (cache) |
| `nbm/tmp/*.TEMP.csv` | Checkpoint after GRIB extraction, so a run can resume without redoing it |
| `nbm/*.fXXX_freqYY.powt-nbm-obs.csv` | **Final archive** |

The final archive is indexed by `(timestamp, stid)`, with these columns:

- **Observed:** `T`, `RH`, `Tw` (°F / %), plus the precip-type flags `RA`, `SN`, `ZR`, `PL`, `UP`
- **Forecast:** `FXT`, `FXRH`, `FXTw`, `snowlvl` (ft) and the conditional precip-type probabilities `PRA`, `PZR`, `PSN`, `PPL`
- **Station:** `elevation`

If a step's output file already exists, that step is skipped. Delete the file to regenerate it.
