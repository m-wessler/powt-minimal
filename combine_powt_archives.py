"""
Combine per-chunk PoWT archive CSVs (written by powt_archive_generator.py)
into one Parquet dataset per region, indexed by (timestamp, fhr, cwa, stid).

Usage:
    python combine_powt_archives.py [--archive-dir DIR] [--output-dir DIR]

Every region found in the archive directory is combined into its own file:
    <output-dir>/NBM_PoWT_Dataset_<first>_<last>.<region>.<version>.parquet
"""

# Standard library imports
import os
import re
import sys
import argparse
from pathlib import Path

# Third-party library imports
import pandas as pd

# Must match archive_version in powt_archive_generator.py
archive_version = 'v2'

# Archive filenames: <region>_<start>_<end>.f<fhr>_freq<freq>.powt-nbm-obs.<version>.csv
archive_pattern = re.compile(
    r'^(?P<region>[^_]+)_(?P<start>\d{10})_(?P<end>\d{10})'
    r'\.f(?P<fhr>\d{3})_freq(?P<freq>\d{2})\.powt-nbm-obs\.' + re.escape(archive_version) + r'\.csv$')

index_cols = ['timestamp', 'fhr', 'cwa', 'stid']

def find_archives(archive_dir):
    """
    Group archive files in archive_dir by region. TEMP checkpoints and
    archives from other versions do not match the pattern and are ignored.
    """
    archives = {}
    for path in sorted(Path(archive_dir).glob(f'*.powt-nbm-obs.{archive_version}.csv')):
        match = archive_pattern.match(path.name)
        if match:
            archives.setdefault(match['region'], []).append((path, int(match['fhr'])))
    return archives

def read_archive(path, fhr):
    """
    Read one archive CSV and add its forecast hour.
    """
    df = pd.read_csv(path, parse_dates=['timestamp'])

    if 'cwa' not in df.columns:
        sys.exit(f"Error: {path.name} has no 'cwa' column. It was written by an older "
                 f"generator; delete it and regenerate.")

    df['fhr'] = fhr
    return df

def combine_region(region, files, output_dir):
    """
    Combine all archive files for one region and write the Parquet dataset.
    """
    print(f"Combining {len(files)} {region} archive file(s)")

    dfs = []
    for path, fhr in files:
        df = read_archive(path, fhr)
        print(f"  {path.name}: {len(df):,} rows")
        dfs.append(df)

    df = pd.concat(dfs, ignore_index=True)

    # Runs with the same fhr but different date ranges or frequencies can
    # overlap (e.g. a rerun over part of a month); keep one row per key
    n_rows = len(df)
    df = df.drop_duplicates(subset=index_cols, keep='first')
    if len(df) < n_rows:
        print(f"  Removed {n_rows - len(df):,} rows duplicated across files")

    df = df.set_index(index_cols).sort_index()

    timestamps = df.index.get_level_values('timestamp')
    output_file = (Path(output_dir) / f"NBM_PoWT_Dataset_{timestamps.min():%Y%m%d}_"
                   f"{timestamps.max():%Y%m%d}.{region}.{archive_version}.parquet")
    df.to_parquet(output_file, compression='gzip')

    fhr_counts = df.index.get_level_values('fhr').value_counts().sort_index()
    print(f"  {len(df):,} rows, {df.index.get_level_values('stid').nunique():,} stations")
    print(f"  Rows by fhr: {fhr_counts.to_dict()}")
    print(f"  Saved {output_file}")

if __name__ == "__main__":

    default_root = os.environ.get("POWT_OUTPUT_DIR", "./data/powt")

    parser = argparse.ArgumentParser(
        description="Combine PoWT archive CSVs into one Parquet dataset per region.")
    parser.add_argument("--archive-dir", default=os.path.join(default_root, "nbm"),
                        help="Directory of archive CSVs (default: $POWT_OUTPUT_DIR/nbm or ./data/powt/nbm)")
    parser.add_argument("--output-dir", default=os.path.join(default_root, "dataset"),
                        help="Directory for Parquet output (default: $POWT_OUTPUT_DIR/dataset or ./data/powt/dataset)")
    args = parser.parse_args()

    archives = find_archives(args.archive_dir)
    if not archives:
        sys.exit(f"Error: no {archive_version} archive files found in {args.archive_dir}")

    Path(args.output_dir).mkdir(parents=True, exist_ok=True)

    for region, files in sorted(archives.items()):
        combine_region(region, files, args.output_dir)
