"""
NBM PoWT verification figures from a combined archive Parquet dataset
(the output of combine_powt_archives.py).

Usage:
    python powt_figures.py DATASET [--cwa MSO,BOI] [--fhr 48,72]
        [--start YYYY-MM-DD] [--end YYYY-MM-DD] [--figures NAME ...]
        [--output-dir DIR] [--dpi N]
    python powt_figures.py --list

Writes one PNG per figure, plus qc_summary.txt, to the output directory.

The code between the SHARED markers is copied unchanged into
PoWT_Analysis.ipynb, which has to run on its own in Colab. A change to that
code must be made in both files.
"""

# Standard library imports
import sys
import argparse
from pathlib import Path

# ======================= BEGIN SHARED (PoWT_Analysis.ipynb) =======================

import os
import re
from functools import partial

import matplotlib
import numpy as np
import pandas as pd
import pyarrow.compute as pc
import pyarrow.parquet as pq
import matplotlib.pyplot as plt
import seaborn as sns
from matplotlib.patches import Patch
from matplotlib.ticker import PercentFormatter
from sklearn.calibration import calibration_curve
from sklearn.metrics import roc_curve, roc_auc_score

OBS_TYPES = ['RA', 'SN', 'ZR', 'PL', 'UP']
FCST_TYPES = ['PRA', 'PSN', 'PZR', 'PPL']
FROZEN_FCST = ['PSN', 'PZR', 'PPL']

TYPE_NAMES = {
    'RA': 'Rain', 'SN': 'Snow', 'ZR': 'Freezing Rain', 'PL': 'Ice Pellets', 'UP': 'Unknown Precip',
    'PRA': 'Rain', 'PSN': 'Snow', 'PZR': 'Freezing Rain', 'PPL': 'Ice Pellets',
}
TYPE_COLORS = {
    'RA': '#228B22', 'SN': '#1E3A8A', 'ZR': '#008B8B', 'PL': '#800080', 'UP': '#C62828',
    'PRA': '#228B22', 'PSN': '#1E3A8A', 'PZR': '#008B8B', 'PPL': '#800080',
}
REGION_NAMES = {'WR': 'Western Region', 'CR': 'Central Region',
                'ER': 'Eastern Region', 'SR': 'Southern Region'}

# Horizontal boxplots: 'orientation' replaced 'vert' in Matplotlib 3.10
_MPL_VERSION = tuple(int(v) for v in matplotlib.__version__.split('.')[:2])
HORIZONTAL = {'orientation': 'horizontal'} if _MPL_VERSION >= (3, 10) else {'vert': False}

# A forecast type counts as "forecast" when its probability exceeds this
PROB_THRESHOLD = 0.15

# QC limits (°F)
TW_WARM_LIMIT = 36       # frozen/unknown obs removed above this observed Tw
TW_COLD_LIMIT = 28       # rain obs removed below this observed Tw
MAX_TEMP_ERROR = 25      # rows removed when |FXT - T| exceeds this
TW_UP_LIMIT = 36         # UP counts as freezing/frozen in reliability at or below this Tw

#################
# Data handling #
#################

def parse_list(text, cast=str):
    """
    'MSO, BOI' -> ['MSO', 'BOI']. Blank or 'None' -> None (no filter).
    """
    if text is None:
        return None
    items = [s.strip() for s in str(text).split(',') if s.strip()]
    if not items or (len(items) == 1 and items[0].lower() == 'none'):
        return None
    return [cast(s) for s in items]

def region_from_filename(path):
    """
    Region code from a dataset name like NBM_PoWT_Dataset_<first>_<last>.WR.parquet
    """
    match = re.search(r'\.([A-Z]{2})\.parquet', os.path.basename(str(path)))
    return match.group(1) if match else None

def dataset_info(path):
    """
    Region, date range, forecast hours and CWAs available in a dataset.
    Reads one column at a time to keep memory low on large files.
    """
    pf = pq.ParquetFile(path)
    time_range = pc.min_max(pf.read(columns=['timestamp']).column(0))
    fhrs = pc.unique(pf.read(columns=['fhr']).column(0)).to_pylist()
    cwas = pc.unique(pf.read(columns=['cwa']).column(0)).to_pylist()
    return {
        'region': region_from_filename(path),
        'start': pd.Timestamp(time_range['min'].as_py()),
        'end': pd.Timestamp(time_range['max'].as_py()),
        'fhrs': sorted(int(f) for f in fhrs),
        'cwas': sorted(c for c in cwas if c is not None),
    }

def load_dataset(path, cwas=None, fhrs=None, start=None, end=None):
    """
    Read a dataset, filtering while reading so only the selection is loaded.
    start and end are dates (YYYY-MM-DD); every hour of the end date is included.
    """
    filters = []
    if cwas:
        filters.append(('cwa', 'in', list(cwas)))
    if fhrs:
        filters.append(('fhr', 'in', [int(f) for f in fhrs]))
    if start:
        filters.append(('timestamp', '>=', pd.Timestamp(start)))
    if end:
        filters.append(('timestamp', '<', pd.Timestamp(end) + pd.DateOffset(days=1)))

    df = pd.read_parquet(path, engine='pyarrow', filters=filters or None)

    # float32 halves memory and is ample precision for these fields
    for col in df.columns:
        if pd.api.types.is_float_dtype(df[col]):
            df[col] = df[col].astype(np.float32)

    return df.sort_index()

def prepare(df):
    """
    Scale probabilities to 0-1, add snow level above ground and apply QC.
    Returns the QC'd DataFrame and a list of summary lines describing each step.
    """
    df[OBS_TYPES] = df[OBS_TYPES].fillna(False).astype(bool)

    # The archive stores probabilities in percent
    if df[FCST_TYPES].max().max() > 1:
        df[FCST_TYPES] = df[FCST_TYPES] / 100

    df['snowlvl_agl'] = df['snowlvl'] - df['elevation']

    n_initial = len(df)
    summary = [f"Initial rows: {n_initial:,}"]
    keep = pd.Series(True, index=df.index)

    def qc_step(remove, label):
        nonlocal keep
        n_removed = int((remove & keep).sum())
        pct = 100 * n_removed / n_initial if n_initial else 0
        summary.append(f"{label}: {n_removed:,} rows removed ({pct:.2f}%)")
        keep &= ~remove

    # Stations without a present-weather sensor never report precipitation
    if 'WX_BAD' in df.columns:
        qc_step(df['WX_BAD'].fillna(False).astype(bool), "No present-weather sensor (WX_BAD)")

    qc_step((df['Tw'] > TW_WARM_LIMIT) & df[['SN', 'ZR', 'PL', 'UP']].any(axis=1),
            f"SN/ZR/PL/UP observed with Tw > {TW_WARM_LIMIT}°F")
    qc_step((df['Tw'] < TW_COLD_LIMIT) & df['RA'],
            f"RA observed with Tw < {TW_COLD_LIMIT}°F")
    qc_step(~((df['FXT'] - df['T']).abs() <= MAX_TEMP_ERROR),
            f"|FXT - T| > {MAX_TEMP_ERROR}°F or missing")

    df = df.loc[keep]
    summary.append(f"Remaining rows: {len(df):,}")
    return df, summary

def describe(df, region=None, cwas=None, fhrs=None):
    """
    One-line description of the data in a figure, used in every title.
    """
    timestamps = df.index.get_level_values('timestamp')
    parts = [f"{timestamps.min():%Y-%m-%d} to {timestamps.max():%Y-%m-%d}" if len(df) else "No data"]
    if region:
        parts.append(REGION_NAMES.get(region, region))
    parts.append(f"CWAs: {', '.join(cwas)}" if cwas else "All CWAs")
    parts.append(f"FHRs: {', '.join(str(f) for f in fhrs)}" if fhrs else "All FHRs")
    return " | ".join(parts)

####################
# Figure utilities #
####################

def _finish(fig, title, info, *extra_lines):
    fig.suptitle("\n".join([title, info, *extra_lines]), fontsize=14)
    return fig

def _empty_axes(ax, title=None, message="No data"):
    ax.text(0.5, 0.5, message, ha='center', va='center', transform=ax.transAxes)
    if title:
        ax.set_title(title)
    ax.set_axis_off()

def _no_data_figure(title, info, message="No cases match this figure's criteria"):
    fig, ax = plt.subplots(figsize=(10, 4), layout='constrained')
    _empty_axes(ax, message=message)
    return _finish(fig, title, info)

def _pct(threshold=PROB_THRESHOLD):
    return f"{threshold * 100:g}%"

###########
# Figures #
###########

def _combo_counts(flags):
    """
    Count rows by their exact combination of True flags; rows with none are skipped.
    """
    counts = {}
    for combo, n in flags.value_counts().items():
        names = [col for col, on in zip(flags.columns, combo) if on]
        if names:
            counts[','.join(names)] = int(n)
    return counts

def _combo_bars(ax, counts, title, xlabel):
    labels, values = list(counts), list(counts.values())
    total = sum(values)
    x = np.arange(len(labels))
    bars = ax.bar(x, values)
    ax.set_title(title, fontsize=15)
    ax.set_xlabel(xlabel, fontsize=14)
    ax.set_ylabel('Frequency (symlog-y)', fontsize=14)
    ax.set_yscale('symlog', linthresh=1)
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=45, ha='right', fontsize=13)
    ax.tick_params(axis='y', labelsize=13)
    max_y = max(values) if values else 1
    ax.set_ylim(top=max_y * 5)

    # Label inside the bar, or above it when the bar is too short to hold text
    for bar, value in zip(bars, values):
        pct = 100 * value / total if total else 0
        x_mid = bar.get_x() + bar.get_width() / 2
        if value <= max_y * 1e-4:
            ax.text(x_mid, value, f"  {pct:.4f}% \n({value:d}) ", ha='center', va='bottom',
                    fontsize=11, color='black', weight='bold', rotation=45)
        else:
            ax.text(x_mid, value, f" {pct:.2f}% \n({value:d}) ", ha='center', va='top',
                    fontsize=11, color='white', weight='bold', rotation=45)

def fig_type_combinations(df, info):
    """
    Frequency of each exact combination of observed types, and of forecast
    types above the probability threshold.
    """
    fig, axes = plt.subplots(2, 1, figsize=(14, 11), layout='constrained')
    _combo_bars(axes[0], _combo_counts(df[OBS_TYPES]),
                "Observed Weather Condition Combinations", 'Condition Combinations')
    _combo_bars(axes[1], _combo_counts(df[FCST_TYPES] > PROB_THRESHOLD),
                f"Forecast Weather Condition Combinations\n(Forecast Probability > {_pct()})",
                'Forecast Condition Combinations')
    return _finish(fig, "Observed & Forecast Weather Condition Combinations", info,
                   f"Total Cases: {len(df):,}")

def fig_warm_signal_obs(df, info):
    """
    Observed types when the forecast is warm (high snow level and/or warm
    FXTw), with and without a frozen-type forecast.
    """
    any_obs = df[OBS_TYPES].any(axis=1)
    high_snowlvl = df['snowlvl_agl'] > 800
    warm_fxtw = df['FXTw'] > 36
    frozen_fcst = (df[FROZEN_FCST] > PROB_THRESHOLD).any(axis=1)
    frozen_label = f"and ≥1 of PSN/PZR/PPL > {_pct()}"

    panels = [
        (any_obs & (high_snowlvl | warm_fxtw), "Snow Level > 800 ft AGL OR FXTw > 36°F\n(≥1 weather type observed)"),
        (any_obs & (high_snowlvl | warm_fxtw) & frozen_fcst, f"Snow Level > 800 ft AGL OR FXTw > 36°F\n{frozen_label}"),
        (any_obs & high_snowlvl & warm_fxtw, "Snow Level > 800 ft AGL AND FXTw > 36°F\n(≥1 weather type observed)"),
        (any_obs & high_snowlvl & warm_fxtw & frozen_fcst, f"Snow Level > 800 ft AGL AND FXTw > 36°F\n{frozen_label}"),
    ]

    fig, axes = plt.subplots(2, 2, figsize=(16, 12), sharey=True, layout='constrained')
    for ax, (mask, title) in zip(axes.flat, panels):
        counts = df.loc[mask, OBS_TYPES].sum()
        counts = counts[counts > 0]
        total = int(mask.sum())
        if counts.empty:
            _empty_axes(ax, title)
            continue
        bars = ax.bar(counts.index, counts.values, color='cornflowerblue', edgecolor='black')
        for bar, n in zip(bars, counts.values):
            ax.text(bar.get_x() + bar.get_width() / 2, n, f"{100 * n / total:.2f}% (n={n:,})",
                    ha='center', va='bottom', fontsize=11)
        ax.set_title(title)
        ax.set_xlabel("Weather Condition")
        ax.set_yscale('log')
        ax.grid(axis='y', linestyle='--', alpha=0.7)
    axes[0, 0].set_ylabel("Frequency (log)")
    axes[1, 0].set_ylabel("Frequency (log)")
    return _finish(fig, "Frequency of Observed Weather Conditions", info, f"Total Cases: {len(df):,}")

def fig_zr_vs_up(df, info):
    """
    Distributions of observed and forecast fields for ZR and UP events.
    """
    cols = ['T', 'FXT', 'RH', 'FXRH',
            'Tw', 'FXTw', 'snowlvl_agl', None,
            'PRA', 'PSN', 'PZR', 'PPL']
    temp_bins = np.arange(-60, 62, 2)
    bins = {'T': temp_bins, 'FXT': temp_bins, 'Tw': temp_bins, 'FXTw': temp_bins,
            'RH': np.arange(0, 105, 5), 'FXRH': np.arange(0, 105, 5),
            'snowlvl_agl': np.arange(-6400, 6200, 400)}
    ref_lines = {'T': [(32, '-', '32°F')], 'FXT': [(32, '-', '32°F')],
                 'Tw': [(36, '-', '36°F')], 'FXTw': [(36, '-', '36°F')],
                 'snowlvl_agl': [(0, '-', '0 ft'), (800, '--', '800 ft')]}
    groups = [(df.loc[df['ZR']], 'blue', 0.4, 'ZR'), (df.loc[df['UP']], 'orange', 0.7, 'UP')]

    fig, axes = plt.subplots(3, 4, figsize=(18, 11), layout='constrained')
    for ax, col in zip(axes.flat, cols):
        if col is None:
            ax.set_axis_off()
            continue
        is_prob = col in FCST_TYPES
        for subset, color, alpha, label in groups:
            data = subset[col].dropna()
            if len(data):
                sns.histplot(data, bins=np.arange(0, 1.05, 0.05) if is_prob else bins[col],
                             kde=not is_prob and data.nunique() > 1, color=color, label=label,
                             ax=ax, stat='density', alpha=alpha)
        for x, style, label in ref_lines.get(col, []):
            ax.axvline(x, color='red', linestyle=style, linewidth=1.2, label=label)
        if is_prob:
            ax.set_yscale('symlog')
        ax.set_title(col)
        ax.set_xlabel(col)
        ax.set_ylabel('Density')
        if ax.get_legend_handles_labels()[0]:
            ax.legend()
    return _finish(fig, "Characteristics of Freezing Rain (ZR) and Unknown Precip (UP) Events", info,
                   f"ZR Cases: {int(df['ZR'].sum()):,} | UP Cases: {int(df['UP'].sum()):,}")

def fig_probability_histograms(df, info):
    """
    Distribution of each forecast probability.
    """
    fig, axes = plt.subplots(2, 2, figsize=(12, 7), layout='constrained')
    for ax, col in zip(axes.flat, ['PRA', 'PSN', 'PPL', 'PZR']):
        ax.hist(df[col].dropna(), bins=np.arange(0, 1.1, 0.1), color=TYPE_COLORS[col], alpha=0.7, log=True)
        ax.set_title(f"Histogram of {col}", fontsize=12)
        ax.set_xlabel(col, fontsize=12)
        ax.set_ylabel('Log Frequency', fontsize=12)
        ax.grid(True)
    return _finish(fig, "Forecast Probability Histograms", info, f"Total Cases: {len(df):,}")

def fig_contingency(df, info):
    """
    Four views of forecast type (probability > threshold) against observed
    type, for precipitating cases only.

    For the first three panels each ob is assigned the first of its observed
    types in OBS_TYPES order (RA, SN, ZR, PL, UP), so an RA+SN ob counts as RA.
    The last panel uses each observed flag on its own.
    """
    precip = df[df[OBS_TYPES].any(axis=1)]
    if precip.empty:
        return _no_data_figure("PoT Forecast by Observed Type", info)

    obs_type = np.array(OBS_TYPES)[precip[OBS_TYPES].to_numpy(bool).argmax(axis=1)]
    probs = precip[FCST_TYPES].to_numpy(float)
    forecast = probs > PROB_THRESHOLD

    # Counts of forecast type (rows) x observed type (columns)
    counts = pd.DataFrame({o: forecast[obs_type == o].sum(axis=0) for o in OBS_TYPES}, index=FCST_TYPES)
    share_of_all = counts / max(counts.values.sum(), 1) * 100
    given_forecast = counts.div(counts.sum(axis=1).replace(0, np.nan), axis=0).fillna(0) * 100

    # Highest-probability forecast type per ob, or -1 when none exceeds the threshold
    dominant = np.where(probs.max(axis=1) > PROB_THRESHOLD, probs.argmax(axis=1), -1)
    dominant_given_obs = pd.DataFrame(
        {o: [(dominant[obs_type == o] == i).mean() * 100 if (obs_type == o).any() else 0
             for i in range(len(FCST_TYPES))] for o in OBS_TYPES},
        index=FCST_TYPES)

    hits_by_flag = pd.DataFrame({o: forecast[precip[o].to_numpy()].sum(axis=0) for o in OBS_TYPES},
                                index=FCST_TYPES)
    forecast_given_obs = hits_by_flag.div(hits_by_flag.sum(axis=0).replace(0, np.nan), axis=1).fillna(0) * 100

    panels = [
        (share_of_all, '% of total',
         f"Any Forecast > {_pct()}\n(Precipitating Cases Only)\nTotal: {share_of_all.values.sum():.1f}%"),
        (dominant_given_obs, '% dominant forecast by obs',
         "Dominant Forecast Given Obs Type\n(Column sums = 100%, less cases with no forecast)"),
        (given_forecast, '% given forecast', f"P(Obs | Forecast Type > {_pct()})\n(Row sums = 100%)"),
        (forecast_given_obs, '% given obs', f"P(Forecast Type > {_pct()} | Obs)\n(Column sums = 100%)"),
    ]

    fig, axes = plt.subplots(2, 2, figsize=(18, 12), sharey='row', layout='constrained')
    for ax, (table, cbar_label, title) in zip(axes.flat, panels):
        table = table.rename(index=TYPE_NAMES, columns=TYPE_NAMES)
        sns.heatmap(table, annot=table.round(1).astype(str) + '%', fmt='', cmap='Blues',
                    cbar_kws={'label': cbar_label}, ax=ax)
        ax.set_xlabel('Observation Type')
        ax.set_ylabel('Forecast Type')
        ax.set_title(title)
        ax.tick_params(axis='y', labelrotation=0)
    return _finish(fig, "PoT Forecast by Observed Type", info, f"Total Used Cases: {int(forecast.sum()):,}")

def fig_median_probability(df, info):
    """
    Median (with 25th/75th percentile and count) of each nonzero forecast
    probability, by observed type.
    """
    shape = (len(FCST_TYPES), len(OBS_TYPES))
    median, p25, p75 = np.full(shape, np.nan), np.full(shape, np.nan), np.full(shape, np.nan)
    count = np.zeros(shape, dtype=int)
    for j, obs in enumerate(OBS_TYPES):
        observed = df.loc[df[obs]]
        for i, fcst in enumerate(FCST_TYPES):
            values = observed[fcst]
            values = values[values > 0].to_numpy()
            if len(values):
                p25[i, j], median[i, j], p75[i, j] = np.percentile(values, [25, 50, 75]) * 100
                count[i, j] = len(values)

    table = pd.DataFrame(median, index=[f"Forecast {TYPE_NAMES[f]}" for f in FCST_TYPES],
                         columns=[f"Observed {TYPE_NAMES[o]}" for o in OBS_TYPES])

    fig, ax = plt.subplots(figsize=(12, 8), layout='constrained')
    sns.heatmap(table, annot=False, cmap='Blues', vmin=0, vmax=100, ax=ax,
                cbar_kws={'label': "Median Forecast Probability (%)"})
    for i in range(shape[0]):
        for j in range(shape[1]):
            if np.isnan(median[i, j]):
                continue
            ax.text(j + 0.5, i + 0.32, f"{p75[i, j]:.1f}%", ha='center', va='bottom', fontsize=10, color='gray')
            ax.text(j + 0.5, i + 0.5, f"{median[i, j]:.1f}%", ha='center', va='center',
                    fontsize=13, color='black', fontweight='bold')
            ax.text(j + 0.5, i + 0.68, f"{p25[i, j]:.1f}%", ha='center', va='top', fontsize=10, color='gray')
            ax.text(j + 0.85, i + 0.5, f"({count[i, j]:,})", ha='center', va='center',
                    fontsize=10, color='gray', rotation=-90)
    ax.set_xlabel("Observed Type")
    ax.set_ylabel("Forecast Type")
    ax.set_yticklabels(ax.get_yticklabels(), rotation=0)
    return _finish(fig, "25th/Median/75th Percentile PoT Forecasts (and Case Counts) by Observed Type",
                   info, f"Total Cases: {len(df):,}")

def fig_pot_by_obs(df, info):
    """
    Box plots of each forecast probability when each type was and wasn't observed.
    """
    hatch = '////'
    fig = plt.figure(figsize=(18, 10), layout='constrained')
    grid = fig.add_gridspec(2, 3)
    positions = [(0, 0), (0, 1), (1, 0), (1, 1), (1, 2)]

    for obs, (row, col) in zip(OBS_TYPES, positions):
        ax = fig.add_subplot(grid[row, col])
        observed = df[obs]
        for j, fcst in enumerate(FCST_TYPES):
            color = TYPE_COLORS[fcst]
            boxes = [
                (df.loc[observed, fcst], -0.18, dict(facecolor=color, color=color, linewidth=2),
                 dict(color='black', linewidth=2)),
                (df.loc[~observed, fcst], 0.18, dict(facecolor='white', color=color, linewidth=2, hatch=hatch),
                 dict(color=color, linewidth=2)),
            ]
            for values, offset, boxprops, medianprops in boxes:
                values = values.dropna().to_numpy()
                if len(values):
                    ax.boxplot([values], positions=[j + offset], **HORIZONTAL, widths=0.32, patch_artist=True,
                               boxprops=boxprops, medianprops=medianprops, showfliers=False)
        ax.set_yticks(range(len(FCST_TYPES)))
        ax.set_yticklabels([TYPE_NAMES[f] for f in FCST_TYPES])
        ax.set_ylim(-0.6, len(FCST_TYPES) - 0.4)
        ax.set_title(f"{TYPE_NAMES[obs]} Observed")
        ax.set_xlabel('Forecast Probability')
        ax.set_ylabel('Forecast Category' if col == 0 else '')
        ax.grid(axis='x', linestyle=':', alpha=0.7)
        ax.axvline(PROB_THRESHOLD, color='gray', linestyle='--', linewidth=2, alpha=0.7)

    legend_ax = fig.add_subplot(grid[0, 2])
    legend_ax.set_axis_off()
    legend_ax.legend(handles=[
        Patch(facecolor=TYPE_COLORS['PRA'], edgecolor=TYPE_COLORS['PRA'], label='Solid: Obs True'),
        Patch(facecolor='white', edgecolor='black', hatch=hatch, label='Hatched: Obs False'),
        *[Patch(facecolor=TYPE_COLORS[f], edgecolor=TYPE_COLORS[f], label=f"{f} ({TYPE_NAMES[f]})")
          for f in FCST_TYPES],
    ], loc='center', fontsize=13, frameon=True, framealpha=0.95)
    return _finish(fig, "Probability of Type Distributions by Observed Type", info, f"Total Cases: {len(df):,}")

def fig_snowlvl_by_obs(df, info):
    """
    Box plots of forecast snow level above ground by observed type.
    """
    order = ['UP', 'PL', 'ZR', 'SN', 'RA']   # bottom to top
    data = [df.loc[df[obs], 'snowlvl_agl'].dropna().to_numpy() for obs in order]

    fig, ax = plt.subplots(figsize=(11, 5.5), layout='constrained')
    box = ax.boxplot(data, **HORIZONTAL, patch_artist=True, widths=0.6, showfliers=False)
    for patch, obs in zip(box['boxes'], order):
        patch.set_facecolor(TYPE_COLORS[obs])
        patch.set_edgecolor(TYPE_COLORS[obs])
    for line in box['medians']:
        line.set_color('orange')
        line.set_linewidth(2)
    ax.set_yticks(range(1, len(order) + 1))
    ax.set_yticklabels([TYPE_NAMES[o] for o in order], fontsize=12)
    ax.axvline(0, color='black', linestyle='-', linewidth=2, alpha=0.8)
    ax.axvline(800, color='gray', linestyle='--', linewidth=2, alpha=0.8)
    ax.set_xlabel("Snow Level AGL (ft)", fontsize=13)
    ax.grid(axis='x', linestyle=':', alpha=0.7)
    return _finish(fig, "Snow Level (ft AGL) Distributions by Observed Type", info, f"Total Cases: {len(df):,}")

def fig_snowlvl_pot_discrimination(df, info):
    """
    Observed type frequencies split by snow level AGL (top row) and by the
    balance of PRA and PSN (bottom row).
    """
    snowlvl, pra, psn = df['snowlvl_agl'], df['PRA'], df['PSN']
    panels = [
        (snowlvl > 800, 'snowlvl_agl > 800'),
        ((snowlvl > 0) & (snowlvl <= 800), '0 < snowlvl_agl <= 800'),
        (snowlvl <= 0, 'snowlvl_agl <= 0'),
        (pra > psn, 'PRA > PSN'),
        ((pra - psn).abs() <= PROB_THRESHOLD, f'PRA within ±{_pct()} of PSN'),
        (psn > pra, 'PSN > PRA'),
    ]
    total_cases = len(df)

    fig, axes = plt.subplots(2, 3, figsize=(15, 9), layout='constrained')
    for ax, (mask, title) in zip(axes.flat, panels):
        counts = df.loc[mask, OBS_TYPES].sum()
        pct = counts / counts.sum() * 100 if counts.sum() > 0 else counts.astype(float)
        ax.bar(OBS_TYPES, pct, color=[TYPE_COLORS[o] for o in OBS_TYPES])
        for i, value in enumerate(pct):
            ax.text(i, value + 1, f'{value:.3f}%', ha='center', fontsize=10)
        n_cases = int(mask.sum())
        ax.text(0.95, 0.95, f"Cases: {n_cases:,}\n({100 * n_cases / total_cases if total_cases else 0:.1f}%)",
                ha='right', va='top', fontsize=10, transform=ax.transAxes,
                bbox=dict(boxstyle="round,pad=0.3", facecolor="white", edgecolor="black"))
        ax.set_ylim(0, max(pct.max(), 1) * 1.2)
        ax.set_title(title, fontsize=14, weight='bold')
        ax.yaxis.set_major_formatter(PercentFormatter())
    axes[0, 0].set_ylabel('Percent Frequency', fontsize=12)
    axes[1, 0].set_ylabel('Percent Frequency', fontsize=12)
    return _finish(fig, "Snow Level vs PoT Discrimination", info, f"Total Cases: {total_cases:,}")

def fig_reliability(df, info):
    """
    Reliability curves with sharpness and ROC insets for rain, snow, and
    freezing/frozen (PZR+PPL against ZR, PL or cold UP).
    """
    cold_up = df['UP'] & (df['Tw'] <= TW_UP_LIMIT)
    pairs = [
        ('PRA', 'RA', df['PRA'], df['RA']),
        ('PSN', 'SN', df['PSN'], df['SN']),
        ('PZR+PPL', f'ZR+PL+(UP & Tw ≤ {TW_UP_LIMIT}°F)', df['PZR'] + df['PPL'], df['ZR'] | df['PL'] | cold_up),
    ]

    fig, axes = plt.subplots(2, 2, figsize=(12, 12), layout='constrained')
    for ax, (prob_name, obs_name, probs, observed) in zip(axes.flat, pairs):
        valid = probs.notna()
        probs, observed = probs[valid].clip(0, 1).astype(float), observed[valid]
        title = f'Reliability Curve: {prob_name} vs {obs_name}\nTrue Count: {int(observed.sum()):,}'
        if observed.nunique() < 2:
            _empty_axes(ax, title, "Needs both observed and not-observed cases")
            continue

        prob_true, prob_pred = calibration_curve(observed, probs, n_bins=10, strategy='uniform')
        ax.plot(prob_pred, prob_true, marker='o')
        ax.plot([0, 1], [0, 1], linestyle='--', color='gray')
        ax.set_title(title, fontsize=10)
        ax.set_xlabel('Predicted Probability')
        ax.set_ylabel('Observed Frequency')
        ax.grid(True)

        sharpness_ax = ax.inset_axes([0.1, 0.8, 0.3, 0.15])
        sharpness_ax.hist(probs * 100, bins=np.arange(0, 110, 10), color='blue', alpha=0.7, edgecolor='black')
        sharpness_ax.set_title('Sharpness', fontsize=8)
        sharpness_ax.set_xlabel('Forecast (%)', fontsize=8)
        sharpness_ax.set_ylabel('Count', fontsize=8)
        sharpness_ax.tick_params(axis='both', which='major', labelsize=6)
        sharpness_ax.grid(True, linestyle='--', alpha=0.5)
        sharpness_ax.set_yscale('log')

        roc_ax = ax.inset_axes([0.7, 0.10, 0.25, 0.25])
        fpr, tpr, _ = roc_curve(observed, probs)
        roc_ax.plot(fpr, tpr, color='darkorange', lw=2)
        roc_ax.plot([0, 1], [0, 1], linestyle='--', color='gray', lw=1)
        roc_ax.set_title('ROC Curve', fontsize=8)
        roc_ax.set_xlabel('FPR', fontsize=8)
        roc_ax.set_ylabel('TPR', fontsize=8)
        roc_ax.tick_params(axis='both', which='major', labelsize=6)
        roc_ax.grid(True, linestyle='--', alpha=0.5)
        roc_ax.text(0.6, 0.2, f'AUC: {roc_auc_score(observed, probs):.2f}', fontsize=8,
                    bbox=dict(facecolor='white', alpha=0.8))
    axes[1, 1].set_axis_off()
    return _finish(fig, "PoT Reliability Plots", info, f"Total Cases: {len(df):,}")

def fig_cold_rain_outcomes(df, info):
    """
    Observed types when rain is the only type forecast but forecast T or Tw is below freezing.
    """
    t = PROB_THRESHOLD
    condition = ((df['PRA'] >= t) & (df[FROZEN_FCST] < t).all(axis=1)
                 & ((df['FXT'] < 32) | (df['FXTw'] < 32)))
    counts = df.loc[condition, OBS_TYPES].sum()
    n_cases = int(condition.sum())
    pct = counts / n_cases * 100 if n_cases else counts.astype(float)

    fig, ax = plt.subplots(figsize=(10, 6.5), layout='constrained')
    bars = ax.bar(OBS_TYPES, pct, color=[TYPE_COLORS[o] for o in OBS_TYPES], edgecolor='black', alpha=0.8)
    for bar, n, value in zip(bars, counts, pct):
        ax.text(bar.get_x() + bar.get_width() / 2, value + 1, f'{value:.1f}%\n(n={int(n):,})',
                ha='center', va='bottom', fontsize=11, fontweight='bold')
    ax.set_ylim(0, pct.max() * 1.25 if n_cases else 100)
    ax.set_ylabel('Percentage of Cases (%)', fontsize=13)
    ax.set_xlabel('Observed Precipitation Type', fontsize=13)
    ax.grid(axis='y', linestyle='--', alpha=0.7)
    return _finish(fig, f"Observed Outcomes when PRA ≥ {_pct()} AND (FXT < 32°F OR FXTw < 32°F)", info,
                   f"Total Matching Cases: {n_cases:,}")

def fig_ptype_transition(df, info, condition, condition_label, temp_col='FXTw',
                         temp_range=(24, 36), min_fhr=None, reference_pct=None):
    """
    Relative frequency of observed type in 1°F bins of forecast temperature,
    for cases where condition(df) is True. Bars are stacked and normalized
    over all observed types in the bin.
    """
    mask = condition(df)
    if min_fhr is not None:
        mask &= df.index.get_level_values('fhr') >= min_fhr

    low, high = temp_range
    temp_bins = df.loc[mask, temp_col].round()
    in_range = temp_bins.between(low, high)
    subset = df.loc[mask].loc[in_range.to_numpy()]
    temp_bins = temp_bins[in_range].to_numpy()

    temp_name = 'Wet-Bulb' if temp_col == 'FXTw' else 'Dry-Bulb'
    title = f"Observed P-Type Transition vs. Forecast {temp_name} Temp ({high}°F to {low}°F)"
    condition_line = f"Condition: {condition_label}" + (f" | FHR ≥ {min_fhr}" if min_fhr is not None else "")
    if subset.empty:
        return _no_data_figure(f"{title}\n{condition_line}", info)

    categories = {
        'Rain': (subset['RA'], TYPE_COLORS['RA']),
        'Mixed (ZR, PL)': (subset['ZR'] | subset['PL'], TYPE_COLORS['PL']),
        'Unknown (UP)': (subset['UP'], TYPE_COLORS['UP']),
        'Snow': (subset['SN'], TYPE_COLORS['SN']),
    }
    all_bins = np.arange(low, high + 1)
    counts = (pd.DataFrame({name: flags.to_numpy() for name, (flags, _) in categories.items()})
              .groupby(temp_bins).sum().reindex(all_bins, fill_value=0))
    cases = pd.Series(temp_bins).value_counts().reindex(all_bins, fill_value=0)
    pct = counts.div(counts.sum(axis=1).replace(0, 1), axis=0) * 100
    has_cases = (cases > 0).to_numpy()
    x = all_bins[has_cases]

    fig, ax = plt.subplots(figsize=(max(12, 0.9 * len(all_bins)), 7.5), layout='constrained')
    bottom = np.zeros(len(x))
    for name, (_, color) in categories.items():
        heights = pct.loc[has_cases, name].to_numpy()
        ax.bar(x, heights, bottom=bottom, label=name, color=color, edgecolor='black', alpha=0.8)
        bottom += heights
    for t, n in zip(x, cases[has_cases]):
        ax.text(t, 102, f'n={int(n):,}', ha='center', va='bottom', fontsize=10, rotation=90)
    if reference_pct is not None:
        ax.axhline(reference_pct, color='red', linestyle='--', linewidth=2,
                   label=f'{reference_pct}% Threshold', zorder=5)

    ax.set_ylim(0, 118)
    ax.set_yticks(np.arange(0, 101, 10))
    ax.set_xlim(low - 0.5, high + 0.5)
    ax.set_xticks(all_bins)
    ax.invert_xaxis()
    ax.set_ylabel('Relative Frequency of Observed Type (%)', fontsize=13)
    ax.set_xlabel(f'Forecast {temp_name} Temperature (°F) [1°F Bins]', fontsize=13)
    ax.legend(loc='lower left', bbox_to_anchor=(0.0, 1.02), ncol=5, fontsize=12)
    ax.grid(axis='y', linestyle='--', alpha=0.7)
    ax.set_axisbelow(True)
    return _finish(fig, title, info, condition_line, f"Total Plotted Cases: {int(cases.sum()):,}")

def fig_fxtw_when_psn(df, info):
    """
    Distribution of forecast wet-bulb temperature when snow is forecast.
    """
    data = df.loc[df['PSN'] >= PROB_THRESHOLD, 'FXTw'].dropna()
    title = f"Forecast Wet-Bulb Temperature when PSN ≥ {_pct()}"
    if data.empty:
        return _no_data_figure(title, info)

    fig, ax = plt.subplots(figsize=(12, 6.5), layout='constrained')
    ax.hist(data, bins=50, log=True, edgecolor='black', linewidth=1)
    for x in (36, 40):
        ax.axvline(x, color='red', linestyle='--', linewidth=3)
    ax.set_xticks(np.arange(int(data.min()), int(data.max()) + 2, 2))
    ax.tick_params(axis='x', rotation=45)
    ax.set_xlabel('FXTw (°F)')
    ax.set_ylabel('Frequency (log scale)')
    return _finish(fig, title, info, f"Cases: {len(data):,}")

def fig_fxtw_by_obs(df, info):
    """
    Distribution of forecast wet-bulb temperature for each observed type.
    """
    groups = [('RA', df['RA']), ('SN', df['SN']), ('ZR/PL', df['ZR'] | df['PL']), ('UP', df['UP'])]
    fig, axes = plt.subplots(len(groups), 1, figsize=(10, 4 * len(groups)), sharex=True, layout='constrained')
    for ax, (label, mask) in zip(axes, groups):
        data = df.loc[mask, 'FXTw'].dropna()
        for x in (36, 40):
            ax.axvline(x, color='red', linestyle='--', linewidth=3)
        if data.empty:
            ax.text(0.5, 0.5, f'No data for {label}', ha='center', va='center', transform=ax.transAxes)
        else:
            ax.hist(data, bins=50, density=True, alpha=0.7, color='skyblue', edgecolor='black')
        ax.set_title(f'Relative Frequency of FXTw during {label} (n={len(data):,})')
        ax.set_ylabel('Relative Frequency')
        ax.grid(True, linestyle='--', alpha=0.6)
    axes[-1].set_xlabel('Forecast Wet-Bulb Temperature (FXTw, °F)')
    return _finish(fig, "Forecast Wet-Bulb Temperature by Observed Type", info)

def _rain_only(df):
    return (df['PRA'] >= PROB_THRESHOLD) & (df[FROZEN_FCST] < PROB_THRESHOLD).all(axis=1)

def _frozen_no_rain(df):
    return (df['PRA'] < PROB_THRESHOLD) & (df[FROZEN_FCST] >= PROB_THRESHOLD).any(axis=1)

def _ice_only(df):
    return ((df['PRA'] < PROB_THRESHOLD) & (df['PSN'] < PROB_THRESHOLD)
            & ((df['PZR'] >= PROB_THRESHOLD) | (df['PPL'] >= PROB_THRESHOLD)))

def _any_frozen(df):
    return (df[FROZEN_FCST] >= PROB_THRESHOLD).any(axis=1)

# Figure registry: key -> (tab title, function(df, info) -> Figure)
FIGURES = {
    'type_combinations': ("Weather Combinations", fig_type_combinations),
    'warm_signal_obs': ("Weather 2x2 Histogram", fig_warm_signal_obs),
    'zr_vs_up': ("ZR vs UP Event Panels", fig_zr_vs_up),
    'probability_histograms': ("Probability Histograms", fig_probability_histograms),
    'contingency': ("Forecast/Obs Contingency", fig_contingency),
    'median_probability': ("Median Forecast Probabilities", fig_median_probability),
    'pot_by_obs': ("PoT Distributions by Obs Type", fig_pot_by_obs),
    'snowlvl_by_obs': ("Snow Level by Obs Type", fig_snowlvl_by_obs),
    'snowlvl_pot_discrimination': ("SnowLvl vs PoT Discrimination", fig_snowlvl_pot_discrimination),
    'reliability': ("PoT Reliability", fig_reliability),
    'cold_rain_outcomes': ("Cold Rain Outcomes", fig_cold_rain_outcomes),
    'transition_cold_rain': ("Cold Rain Temp", partial(
        fig_ptype_transition, condition=_rain_only,
        condition_label=f"PRA ≥ {_pct()} AND (PSN, PZR, PPL < {_pct()})", temp_range=(24, 36))),
    'transition_warm_snow': ("Warm Snow Temp", partial(
        fig_ptype_transition, condition=_frozen_no_rain,
        condition_label=f"PRA < {_pct()} AND (PSN or PZR or PPL ≥ {_pct()})", temp_range=(30, 36))),
    'transition_solo_fzra': ("Solo FZRA", partial(
        fig_ptype_transition, condition=_ice_only,
        condition_label=f"(PRA < {_pct()} AND PSN < {_pct()}) AND (PZR or PPL ≥ {_pct()})", temp_range=(24, 36))),
    'fxtw_when_psn': ("FXTw when PSN", fig_fxtw_when_psn),
    'fxtw_by_obs': ("FXTw by Obs Type", fig_fxtw_by_obs),
    'transition_frozen_fxtw_f96': ("Frozen Fcst Tw (FHR≥96)", partial(
        fig_ptype_transition, condition=_any_frozen,
        condition_label=f"PSN or PZR or PPL ≥ {_pct()}", temp_col='FXTw',
        temp_range=(30, 42), min_fhr=96, reference_pct=85)),
    'transition_frozen_fxt_f96': ("Frozen Fcst T (FHR≥96)", partial(
        fig_ptype_transition, condition=_any_frozen,
        condition_label=f"PSN or PZR or PPL ≥ {_pct()}", temp_col='FXT',
        temp_range=(30, 55), min_fhr=96, reference_pct=85)),
}

# ======================== END SHARED (PoWT_Analysis.ipynb) ========================

def main():
    parser = argparse.ArgumentParser(description="Write NBM PoWT verification figures as PNGs.")
    parser.add_argument("dataset", nargs='?', help="Combined archive Parquet (combine_powt_archives.py output)")
    parser.add_argument("--cwa", help="Comma-separated CWAs (default: all)")
    parser.add_argument("--fhr", help="Comma-separated forecast hours (default: all)")
    parser.add_argument("--start", help="First valid date, YYYY-MM-DD (default: start of dataset)")
    parser.add_argument("--end", help="Last valid date, YYYY-MM-DD, all hours included (default: end of dataset)")
    parser.add_argument("--figures", nargs='+', choices=list(FIGURES), metavar='NAME',
                        help="Figures to draw (default: all; see --list)")
    parser.add_argument("--output-dir", default="figures", help="Directory for PNGs (default: ./figures)")
    parser.add_argument("--dpi", type=int, default=150)
    parser.add_argument("--list", action='store_true', help="List figure names and exit")
    args = parser.parse_args()

    if args.list:
        for key, (title, _) in FIGURES.items():
            print(f"{key:30s} {title}")
        return
    if not args.dataset:
        parser.error("dataset is required")

    plt.switch_backend('Agg')

    available = dataset_info(args.dataset)
    print(f"Dataset: {args.dataset}")
    print(f"  Valid times: {available['start']:%Y-%m-%d %HZ} to {available['end']:%Y-%m-%d %HZ}")
    print(f"  FHRs: {', '.join(str(f) for f in available['fhrs'])}")
    print(f"  CWAs: {', '.join(available['cwas'])}")

    cwas = parse_list(args.cwa, str.upper)
    fhrs = parse_list(args.fhr, int)
    df = load_dataset(args.dataset, cwas, fhrs, args.start, args.end)
    if df.empty:
        sys.exit("Error: no rows match the selection.")

    df, qc_summary = prepare(df)
    print("\n".join(qc_summary))
    info = describe(df, available['region'], cwas, fhrs)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "qc_summary.txt").write_text(f"{args.dataset}\n{info}\n\n" + "\n".join(qc_summary) + "\n")

    failed = []
    for key in args.figures or FIGURES:
        title, func = FIGURES[key]
        try:
            fig = func(df, info)
        except Exception as e:
            print(f"  {key}: FAILED ({e})")
            failed.append(key)
            continue
        fig.savefig(output_dir / f"{key}.png", dpi=args.dpi)
        plt.close(fig)
        print(f"  {key}: saved")

    print(f"Figures written to {output_dir}/")
    if failed:
        sys.exit(f"Error: {len(failed)} figure(s) failed: {', '.join(failed)}")

if __name__ == "__main__":
    main()
