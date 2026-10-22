#!/usr/bin/env python3
"""Plot RDMA perftest measurements as separate campaign figures.

The loader scans result CSVs recursively and accepts a results tree or one
campaign directory. Figures separate bandwidth verb and direction, latency
verb and percentile, and atomic message rate. Run points, means, and min-max
whiskers show repeated measurements. Bandwidth uses decimal GB/s. Bidirectional
results include both directions. Fabric capacity lines require an explicit
FABRIC_UNI_GBPS or FABRIC_BIDI_GBPS value.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")                      # Write figures without a display.
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.axes import Axes
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.ticker import FuncFormatter
from urllib.parse import quote


# ---- paper figure style ------------------------------------------------------
# Typography and PDF font settings match the sibling plotting scripts.
COL_W = 3.5      # one IEEE column, inches
TEXT_W = 7.0     # full text width, inches
SERIF_FAMILIES = ["Times New Roman", "Liberation Serif", "DejaVu Serif"]


def apply_paper_style(dpi: int, serif: bool) -> None:
    """Set figure size, typography, grid, and PDF font settings."""
    plt.rcParams.update({
        "savefig.dpi": dpi,
        "figure.dpi": 120,
        "font.size": 9,
        "axes.titlesize": 9,
        "axes.labelsize": 9,
        "legend.fontsize": 8,
        "legend.frameon": False,
        "xtick.labelsize": 8,
        "ytick.labelsize": 8,
        "axes.grid": True,
        "grid.alpha": 0.30,
        "grid.linewidth": 0.5,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "lines.linewidth": 1.6,
        "lines.markersize": 5,
        "figure.constrained_layout.use": True,
        "pdf.fonttype": 42,
        "ps.fonttype": 42,
    })
    if serif:
        plt.rcParams["font.family"] = "serif"
        plt.rcParams["font.serif"] = list(SERIF_FAMILIES)
        plt.rcParams["mathtext.fontset"] = "dejavuserif"
    installed = {f.name for f in font_manager.fontManager.ttflist}
    family = next((f for f in SERIF_FAMILIES if f in installed), None)
    if family is not None:
        plt.rcParams.update({
            "mathtext.fontset": "custom",
            "mathtext.rm": family,
            "mathtext.it": f"{family}:italic",
            "mathtext.bf": f"{family}:bold",
            "mathtext.default": "regular",
        })


def save_figure(fig: Figure, name: str, out: str, formats: list[str],
                *, tight: bool = False) -> None:
    """Save one file per format and close the figure.

    tight=True includes figure-level captions in the saved bounds.
    """
    os.makedirs(out, exist_ok=True)
    keyword = dict(bbox_inches="tight") if tight else {}
    for fmt in formats:
        path = os.path.join(out, f"{name}.{fmt}")
        fig.savefig(path, **keyword)
        print(f"  wrote {path}")
    plt.close(fig)


SCRIPT_DIR = Path(__file__).resolve().parent


# Bandwidth tests use 64 KiB messages. atomic_bw uses 8 B messages and reports
# operations per second.
VERBS = ("send", "write", "read")
LATENCY_VERBS = ("send", "write", "read", "atomic")
ROLES = ("client", "host")
MEASUREMENT_COLUMNS = (
    "size_bytes", "bw_peak_GBps", "bw_avg_GBps", "msg_rate_Mpps",
    "lat_min_usec", "lat_max_usec", "lat_typical_usec", "lat_avg_usec",
    "lat_stdev_usec", "lat_p99_usec", "lat_p999_usec",
)
REQUIRED_COLUMNS = frozenset(
    {"test", "device", "role", "bidi", "error"} | set(MEASUREMENT_COLUMNS))

# isolation runs one device at a time. concurrent runs all discovered devices.
# CSV files without a mode column are read as isolation runs.
ISOLATION_MODE = "isolation"
CONCURRENT_MODE = "concurrent"

MODE_STYLE = {
    ISOLATION_MODE: dict(color="#0072B2", marker="o", label="isolation"),
    CONCURRENT_MODE: dict(color="#D55E00", marker="s", label="concurrent"),
}
REFERENCE_COLOR = "0.35"
INDIVIDUAL_RUN_COLOR = "0.55"
LATENCY_METRICS = (
    ("typical", "lat_typical_usec", "Typical latency (µs)"),
    ("p99", "lat_p99_usec", "99th-percentile latency (µs)"),
)


def _campaign_run_role(results_root: str, path: str) -> tuple[str, int, str]:
    relative = Path(os.path.relpath(path, results_root)).parts
    run_index = next((index for index, part in enumerate(relative)
                      if part.startswith("run-")
                      and part.removeprefix("run-").isdigit()
                      and index > 0 and relative[index - 1] in ROLES
                      and (index + 1 == len(relative)
                           or relative[index + 1] not in ROLES)), None)
    if run_index is None:
        run_index = next((index for index, part in enumerate(relative)
                          if part.startswith("run-")
                          and part.removeprefix("run-").isdigit()
                          and index + 1 < len(relative)
                          and relative[index + 1] in ROLES), None)
    if run_index is None:
        raise ValueError(
            f"{path}: expected <campaign>/<role>/run-NNN/... or "
            "<campaign>/run-NNN/<role>/...")
    run_name = relative[run_index]
    run_number = run_name.removeprefix("run-")
    if not run_number.isdigit():
        raise ValueError(f"{path}: invalid run directory {run_name!r}")
    if run_index + 1 < len(relative) and relative[run_index + 1] in ROLES:
        role = relative[run_index + 1]
        campaign_parts = relative[:run_index]
    elif run_index > 0 and relative[run_index - 1] in ROLES:
        role = relative[run_index - 1]
        campaign_parts = relative[:run_index - 1]
    else:
        raise ValueError(
            f"{path}: expected <campaign>/<role>/run-NNN/... or "
            "<campaign>/run-NNN/<role>/...")
    campaign = ("/".join(campaign_parts) if campaign_parts else
                Path(results_root).resolve().name)
    return campaign, int(run_number), role


def load_perftest_results(
        results_root: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load result CSVs and select one row per campaign, run, device, and test.

    Measured client rows take precedence over measured host rows. Error rows
    are fallbacks when neither role has a measurement. Return selected and raw
    rows. Invalid CSV columns or values raise an error with the source path.
    """
    paths = sorted(set(
        glob.glob(os.path.join(
            results_root, "**", "*", "run-*", "results.csv"),
            recursive=True)
        + glob.glob(os.path.join(
            results_root, "**", "run-*", "*", "results.csv"),
            recursive=True)))
    if not paths:
        raise SystemExit(f"no perftest results.csv under {results_root}")
    frames = []
    for path in paths:
        campaign, run, role = _campaign_run_role(results_root, path)
        if role not in ROLES:
            raise ValueError(f"{path}: unrecognized role {role!r}")
        frame = pd.read_csv(path)
        missing = REQUIRED_COLUMNS.difference(frame.columns)
        if missing:
            raise ValueError(f"{path}: missing column(s) {sorted(missing)}")
        if not (frame["role"].astype(str) == role).all():
            raise ValueError(
                f"{path}: role column does not match directory {role!r}")
        if "mode" not in frame.columns:
            frame["mode"] = ISOLATION_MODE
        frame["campaign"] = campaign
        frame["run"] = run
        frames.append(frame)
    all_rows = pd.concat(frames, ignore_index=True)
    for column in MEASUREMENT_COLUMNS:
        all_rows[column] = pd.to_numeric(all_rows[column], errors="coerce")
    all_rows["error"] = all_rows["error"].fillna("").astype(str).str.strip()
    all_rows["mode"] = (all_rows["mode"].fillna(ISOLATION_MODE)
                        .astype(str).str.strip().replace("", ISOLATION_MODE))
    unknown_modes = sorted(set(all_rows["mode"]) - {ISOLATION_MODE, CONCURRENT_MODE})
    if unknown_modes:
        raise ValueError(f"unrecognized mode value(s): {unknown_modes}")
    bidi_text = all_rows["bidi"].fillna("").astype(str).str.strip().str.lower()
    all_rows["bidi"] = bidi_text.map({"true": True, "false": False})
    if all_rows["bidi"].isna().any():
        bad = sorted(bidi_text[all_rows["bidi"].isna()].unique())
        raise ValueError(f"unrecognized bidi value(s): {bad}")
    parsed = all_rows["test"].astype(str).str.extract(
        r"^ib_(?P<verb>[a-z]+)_(?P<kind>lat|bw)$")
    if parsed["kind"].isna().any():
        bad = sorted(all_rows["test"][parsed["kind"].isna()].unique())
        raise ValueError(f"unrecognized test name(s): {bad}")
    all_rows["verb"] = parsed["verb"]
    all_rows["kind"] = parsed["kind"]

    measured = all_rows[all_rows["error"] == ""]
    sizes = measured.groupby(["campaign", "test", "bidi"])[
        "size_bytes"].nunique(dropna=True)
    swept = sizes[sizes > 1]
    if len(swept):
        detail = ", ".join(
            f"{campaign}/{test} (bidi={bidi})"
            for campaign, test, bidi in swept.index)
        raise ValueError(
            "message-size sweeps are not supported by these per-device "
            f"figures: {detail}")

    keys = ["campaign", "run", "device", "test", "bidi", "mode"]
    # Measured client (0), measured host (1), error client (2), error host (3).
    rank = ((all_rows["error"] != "").astype(int) * 2
            + (all_rows["role"] != "client").astype(int))
    preferred = (all_rows.assign(_rank=rank)
                 .sort_values(keys + ["_rank"], kind="stable")
                 .drop_duplicates(keys, keep="first")
                 .drop(columns="_rank")
                 .reset_index(drop=True))
    return preferred, all_rows


def load_link_speeds(results_root: str) -> pd.DataFrame:
    """Load per-run port rates from captured environment/link.json files.

    Use ibv_devinfo active width and speed when present. Use ethtool link speed
    when the ibv_devinfo fields are absent.
    """
    rows = []
    paths = set(glob.glob(os.path.join(
        results_root, "**", "*", "run-*", "environment", "*",
        "link.json"), recursive=True))
    paths.update(glob.glob(os.path.join(
        results_root, "**", "run-*", "*", "environment", "*",
        "link.json"), recursive=True))
    for path in sorted(paths):
        campaign, run, _ = _campaign_run_role(results_root, path)
        with open(path) as handle:
            link = json.load(handle)
        rows.append({
            "campaign": campaign,
            "run": run,
            "device": link.get("device") or os.path.basename(
                os.path.dirname(path)),
            "link_speed_mbps": link.get("link_speed_mbps"),
            "active_width": link.get("active_width"),
            "active_speed_gbps": link.get("active_speed_gbps"),
        })
    if not rows:
        return pd.DataFrame(columns=[
            "campaign", "run", "device", "link_speed_mbps", "active_width",
            "active_speed_gbps", "device_rate_gbps"])
    df = pd.DataFrame(rows)
    for column in ("link_speed_mbps", "active_width", "active_speed_gbps"):
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df["device_rate_gbps"] = df["active_width"] * df["active_speed_gbps"]
    df["device_rate_gbps"] = df["device_rate_gbps"].fillna(
        df["link_speed_mbps"] / 1000.0)
    return df


def devices_in_order(df: pd.DataFrame) -> list[str]:
    """RDMA device names in stable, case-insensitive order."""
    return sorted(df["device"].astype(str).unique(), key=str.casefold)


def _size_label(size_bytes: float) -> str:
    """Format a message size with binary units."""
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:g} KiB"
    return f"{size_bytes:g} B"


def _results_summary(df: pd.DataFrame) -> str:
    """Return campaign, run, and campaign-device counts."""
    campaigns = int(df["campaign"].nunique())
    devices = int(df.groupby("campaign")["device"].nunique().sum())
    runs = int(df.groupby(["campaign", "run"]).ngroups)
    return (f"{campaigns} campaign(s), {runs} run(s), "
            f"{devices} campaign-device pair(s)")


def _role_agreement(all_rows: pd.DataFrame) -> str:
    """Return maximum client-host differences for shared measurements."""
    keys = ["campaign", "run", "device", "test", "bidi", "mode"]
    merged = all_rows[all_rows["role"] == "client"].merge(
        all_rows[all_rows["role"] == "host"], on=keys, suffixes=("_c", "_h"))
    parts = []
    for column, label in (("bw_avg_GBps", "bandwidth"),
                          ("lat_typical_usec", "typical latency")):
        pairs = merged[merged[f"{column}_c"].notna()
                       & merged[f"{column}_h"].notna()]
        if not pairs.empty:
            relative = ((pairs[f"{column}_c"] - pairs[f"{column}_h"]).abs()
                        / pairs[f"{column}_h"].replace(0, np.nan) * 100.0)
            maximum = relative.max()
            if pd.notna(maximum):
                parts.append(f"{label} max |diff| {maximum:.3f}% "
                             f"({len(pairs)} pairs)")
    return "; ".join(parts) if parts else "no shared client/host measurements"


def report_coverage(df: pd.DataFrame, all_rows: pd.DataFrame,
                    link_speeds: pd.DataFrame) -> None:
    """Print role coverage, failed rows, and captured port rates."""
    print("coverage")
    print(f"  source: {len(df)} row(s) from {_results_summary(df)}")
    role_counts = all_rows.groupby("role").size()
    roles_text = ", ".join(f"{role} {count}"
                           for role, count in role_counts.items())
    host_used = int(((df["role"] == "host") & (df["error"] == "")).sum())
    print(f"  roles: {roles_text} row(s); client rows preferred, host rows "
          f"used for {host_used} measured key(s)")
    mode_counts = df.groupby("mode").size()
    modes_text = ", ".join(f"{mode} {count}"
                            for mode, count in mode_counts.items())
    print(f"  modes: {modes_text} row(s)")
    print(f"  client/host agreement: {_role_agreement(all_rows)}")
    for campaign in sorted(df["campaign"].unique()):
        campaign_rows = df[df["campaign"] == campaign]
        runs = sorted(int(run) for run in campaign_rows["run"].unique())
        devices = devices_in_order(campaign_rows)
        print(f"  {campaign}: {len(runs)} runs ({runs[0]}..{runs[-1]}), "
              f"{len(devices)} device(s): {', '.join(devices)}")
        for test in sorted(campaign_rows["test"].unique()):
            for bidi_flag in (False, True):
                for mode in sorted(campaign_rows["mode"].unique()):
                    subset = campaign_rows[
                        (campaign_rows["test"] == test)
                        & (campaign_rows["bidi"] == bidi_flag)
                        & (campaign_rows["mode"] == mode)]
                    if subset.empty:
                        continue
                    measured = int((subset["error"] == "").sum())
                    if measured < len(subset):
                        direction = "bidi" if bidi_flag else "uni"
                        errors = sorted(set(
                            subset.loc[subset["error"] != "", "error"]))
                        print(f"    {test} ({direction}, {mode}): "
                              f"{measured}/{len(subset)} rows measured, "
                              f"error rows: {', '.join(errors)}")
    if not link_speeds.empty:
        for campaign, group in link_speeds.groupby("campaign"):
            rates = sorted({float(value) for value in
                            group["device_rate_gbps"].dropna()})
            shown = ", ".join(f"{rate:g}" for rate in rates) or "none"
            print(f"  device link rate: {campaign}: {shown} Gb/s")
    atomic = df[(df["error"] == "") & (df["test"] == "ib_atomic_bw")]
    if not atomic.empty:
        rates = atomic["msg_rate_Mpps"].dropna()
        if not rates.empty:
            print(f"  ib_atomic_bw: {len(rates)} row(s), "
                  f"{rates.min():.2f}-{rates.max():.2f} million operations/s")


def _campaign_figure_directory(out: str, campaign: str) -> str:
    """Return an output directory unique to a campaign path."""
    return os.path.join(out, quote(campaign, safe=""))


def _measurement_modes(frame: pd.DataFrame) -> list[str]:
    """Modes in fixed plot order, limited to measured rows."""
    present = set(frame["mode"].astype(str))
    return [mode for mode in (ISOLATION_MODE, CONCURRENT_MODE)
            if mode in present]


def _repeat_count_label(frame: pd.DataFrame) -> str:
    """Run-count range per device and mode for a plot caption."""
    counts = frame.groupby(["device", "mode"])["run"].nunique()
    if counts.empty:
        return "n=0"
    low, high = int(counts.min()), int(counts.max())
    return f"n={low}" if low == high else f"n={low}-{high}"


def _draw_repeat_summary(ax: Axes, center: float, values: pd.Series,
                         color: str, marker: str) -> float:
    """Draw run-level points and their arithmetic mean with min-max whiskers."""
    observed = values.dropna().astype(float).sort_index().to_numpy()
    if not len(observed):
        return 0.0
    offsets = np.linspace(-0.07, 0.07, len(observed))
    ax.scatter(center + offsets, observed, s=11, color=INDIVIDUAL_RUN_COLOR,
               alpha=0.55, linewidths=0, zorder=3)
    minimum, maximum = float(observed.min()), float(observed.max())
    mean = min(maximum, max(minimum, float(observed.mean())))
    ax.errorbar(
        center, mean,
        yerr=[[mean - minimum], [maximum - mean]],
        fmt=marker, color=color, markersize=4.2, elinewidth=0.9,
        capsize=2.2, capthick=0.8, zorder=4)
    return float(observed.max())


def _repeat_legend(modes: list[str], has_link_rate: bool) -> list[Line2D]:
    """Return legend handles for runs, modes, and optional port rates."""
    handles = [Line2D([], [], color=INDIVIDUAL_RUN_COLOR, marker=".",
                      linestyle="None", markersize=4,
                      label="individual run")]
    handles.extend(
        Line2D([], [], color=MODE_STYLE[mode]["color"],
               marker=MODE_STYLE[mode]["marker"], linestyle="None",
               markersize=4,
               label=f"{MODE_STYLE[mode]['label']} mean; min-max")
        for mode in modes)
    if has_link_rate:
        handles.append(Line2D([], [], color=REFERENCE_COLOR,
                              linestyle="--", linewidth=0.9,
                              label="captured device port rate"))
    return handles


def _device_link_rates(link_speeds: pd.DataFrame,
                       campaign: str) -> dict[str, float]:
    """Return captured rates that are constant for each campaign device."""
    campaign_links = link_speeds[link_speeds["campaign"] == campaign]
    rates: dict[str, float] = {}
    for device, group in campaign_links.groupby("device"):
        unique = sorted({float(value) for value in
                         group["device_rate_gbps"].dropna()})
        if len(unique) == 1:
            rates[str(device)] = unique[0]
    return rates


def _plot_device_measurement(
        df: pd.DataFrame, campaign: str, test: str, value_column: str,
        title: str, y_label: str, figure_name: str, out: str,
        formats: list[str], *, link_speeds: pd.DataFrame | None = None,
        bidi_flag: bool | None = None, log_y: bool = False) -> bool:
    """Plot one test and metric for one campaign, grouped by RDMA device."""
    campaign_rows = df[(df["campaign"] == campaign) & (df["test"] == test)]
    if bidi_flag is not None:
        campaign_rows = campaign_rows[campaign_rows["bidi"] == bidi_flag]
    measured = campaign_rows[(campaign_rows["error"] == "")
                             & campaign_rows[value_column].notna()]
    if log_y:
        measured = measured[measured[value_column] > 0]
    if measured.empty:
        return False

    devices = devices_in_order(campaign_rows)
    modes = _measurement_modes(measured)
    mode_offsets = ({mode: 0.0 for mode in modes} if len(modes) == 1 else {
        ISOLATION_MODE: -0.17, CONCURRENT_MODE: 0.17})
    captured_rates = (_device_link_rates(link_speeds, campaign)
                       if link_speeds is not None and bidi_flag is not None
                       else {})
    fig_width = min(TEXT_W, max(COL_W, 1.7 + 0.85 * len(devices)))
    fig, ax = plt.subplots(figsize=(fig_width, 3.6), constrained_layout=False)
    fig.subplots_adjust(left=0.16, right=0.99, bottom=0.30, top=0.79)
    highest = 0.0

    for device_index, device in enumerate(devices):
        for mode in modes:
            group = measured[(measured["device"] == device)
                             & (measured["mode"] == mode)]
            highest = max(highest, _draw_repeat_summary(
                ax, device_index + mode_offsets[mode], group[value_column],
                MODE_STYLE[mode]["color"], MODE_STYLE[mode]["marker"]))
        if device in captured_rates:
            rate_gbps = captured_rates[device]
            direction_multiplier = 2.0 if bidi_flag else 1.0
            reference = rate_gbps * direction_multiplier / 8.0
            ax.hlines(reference, device_index - 0.36, device_index + 0.36,
                      color=REFERENCE_COLOR, linestyle="--", linewidth=0.9,
                      zorder=1)
            highest = max(highest, reference)

    fig.suptitle(f"{campaign}: {title}", fontsize=8, y=0.97)
    ax.set_xticks(range(len(devices)))
    ax.set_xticklabels(devices, rotation=25, ha="right", fontsize=7)
    ax.set_xlim(-0.5, max(0.5, len(devices) - 0.5))
    ax.set_xlabel("RDMA device")
    ax.set_ylabel(y_label)
    ax.grid(axis="x", visible=False)
    if log_y:
        positive = measured[value_column].astype(float)
        low, high = float(positive.min()), max(highest, float(positive.max()))
        ax.set_yscale("log")
        ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:g}"))
        ax.set_ylim(max(low * 0.8, np.finfo(float).tiny), high * 1.2)
    else:
        ax.set_ylim(0, highest * 1.12 if highest > 0 else 1.0)
    ax.legend(handles=_repeat_legend(modes, bool(captured_rates)),
              fontsize=6.2, ncol=2, loc="lower left",
              bbox_to_anchor=(0, 1.02), borderaxespad=0)

    sizes = sorted(float(size) for size in measured["size_bytes"].dropna().unique())
    size_text = ", ".join(_size_label(size) for size in sizes)
    roles = "+".join(sorted(measured["role"].astype(str).unique()))
    fig.text(0.5, 0.015,
             f"{roles} rows; size={size_text}; "
             f"n={_repeat_count_label(measured)} per device/mode; "
             "points=runs; markers=means; whiskers=min-max",
             ha="center", va="bottom", fontsize=6.3, color="0.25")
    save_figure(fig, figure_name, out, formats, tight=True)
    return True


def _explicit_fabric_ceiling_gbps(env_name: str) -> float | None:
    """Read a configured aggregate fabric limit in Gbit/s."""
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        raise SystemExit(f"{env_name} must be a number (got {raw!r})")
    if value <= 0:
        raise SystemExit(f"{env_name} must be positive (got {raw!r})")
    return value


def _complete_aggregate_runs(
        rows: pd.DataFrame, value_column: str) -> tuple[pd.DataFrame, int]:
    """Sum runs with one successful measurement from every device."""
    expected_devices = set(rows["device"].astype(str))
    records: list[dict[str, int | str | float]] = []
    omitted = 0
    for (run, mode), group in rows.groupby(["run", "mode"]):
        measured = group[(group["error"] == "")
                         & group[value_column].notna()]
        present_devices = set(measured["device"].astype(str))
        if present_devices != expected_devices or len(measured) != len(expected_devices):
            omitted += 1
            continue
        records.append({"run": int(run), "mode": str(mode),
                        "value": float(measured[value_column].sum())})
    return pd.DataFrame.from_records(records), omitted


def _plot_aggregate_measurement(
        df: pd.DataFrame, campaign: str, verb: str, bidi_flag: bool,
        fabric_ceiling_gbps: float | None, out: str,
        formats: list[str]) -> bool:
    """Plot complete isolation and concurrent sums for one bandwidth test."""
    test = f"ib_{verb}_bw"
    rows = df[(df["campaign"] == campaign) & (df["test"] == test)
              & (df["bidi"] == bidi_flag)]
    if rows.empty:
        return False
    aggregate, omitted = _complete_aggregate_runs(rows, "bw_avg_GBps")
    if omitted:
        print(f"  {campaign}: {test} omitted {omitted} incomplete run-mode group(s)")
    modes = _measurement_modes(aggregate)
    if not {ISOLATION_MODE, CONCURRENT_MODE}.issubset(modes):
        return False

    fig, ax = plt.subplots(figsize=(COL_W, 3.3), constrained_layout=False)
    fig.subplots_adjust(left=0.2, right=0.98, bottom=0.25, top=0.78)
    for mode_index, mode in enumerate((ISOLATION_MODE, CONCURRENT_MODE)):
        values = aggregate.loc[aggregate["mode"] == mode, "value"]
        _draw_repeat_summary(
            ax, float(mode_index), values, MODE_STYLE[mode]["color"],
            MODE_STYLE[mode]["marker"])
    capacity_gbps = fabric_ceiling_gbps
    has_capacity = capacity_gbps is not None
    if capacity_gbps is not None:
        capacity_gbytes = capacity_gbps / 8.0
        ax.axhline(capacity_gbytes, color=REFERENCE_COLOR, linestyle="--",
                   linewidth=0.9, zorder=1)
    direction = "bidirectional" if bidi_flag else "unidirectional"
    fig.suptitle(
        f"{campaign}: {verb} aggregate bandwidth, {direction}",
        fontsize=8, y=0.97)
    ax.set_xticks([0, 1])
    ax.set_xticklabels(["isolation sum", "concurrent sum"], fontsize=7)
    ax.set_ylabel("Aggregate bandwidth (GB/s)")
    ax.set_ylim(0, max(float(aggregate["value"].max()),
                       (capacity_gbps or 0.0) / 8.0) * 1.18)
    ax.grid(axis="x", visible=False)
    legend_handles = [
        Line2D([], [], color=INDIVIDUAL_RUN_COLOR, marker=".",
               linestyle="None", markersize=4, label="individual run"),
        *[
            Line2D([], [], color=MODE_STYLE[mode]["color"],
                   marker=MODE_STYLE[mode]["marker"], linestyle="None",
                   markersize=4,
                   label=f"{MODE_STYLE[mode]['label']} mean; min-max")
            for mode in (ISOLATION_MODE, CONCURRENT_MODE)
        ],
    ]
    if has_capacity:
        legend_handles.append(Line2D([], [], color=REFERENCE_COLOR,
                                     linestyle="--", linewidth=0.9,
                                     label=("configured fabric capacity "
                                            f"({capacity_gbps:g} Gbit/s)")))
    ax.legend(handles=legend_handles, fontsize=6.2, ncol=2,
              loc="lower left", bbox_to_anchor=(0, 1.02), borderaxespad=0)
    device_count = int(rows["device"].nunique())
    fig.text(0.5, 0.015,
             f"Complete runs; each point sums {device_count} devices; "
             f"isolation n={len(aggregate[aggregate['mode'] == ISOLATION_MODE])}; "
             f"concurrent n={len(aggregate[aggregate['mode'] == CONCURRENT_MODE])}; "
             "bidirectional values combine both directions",
             ha="center", va="bottom", fontsize=6.3, color="0.25")
    direction_name = "bidi" if bidi_flag else "uni"
    save_figure(fig, f"aggregate_{verb}_{direction_name}", out, formats,
                tight=True)
    return True


def plot_campaign_figures(df: pd.DataFrame, link_speeds: pd.DataFrame,
                          campaign: str, out: str,
                          formats: list[str]) -> int:
    """Write the separate measurement figures for one campaign."""
    figure_dir = _campaign_figure_directory(out, campaign)
    written = 0
    for verb in VERBS:
        for bidi_flag in (False, True):
            direction = "bidirectional" if bidi_flag else "unidirectional"
            written += _plot_device_measurement(
                df, campaign, f"ib_{verb}_bw", "bw_avg_GBps",
                f"{verb} bandwidth, {direction}",
                "Bandwidth (GB/s, both directions)" if bidi_flag
                else "Bandwidth (GB/s)",
                f"bandwidth_{verb}_{'bidi' if bidi_flag else 'uni'}",
                figure_dir, formats, link_speeds=link_speeds,
                bidi_flag=bidi_flag)
            fabric_env = "FABRIC_BIDI_GBPS" if bidi_flag else "FABRIC_UNI_GBPS"
            written += _plot_aggregate_measurement(
                df, campaign, verb, bidi_flag,
                _explicit_fabric_ceiling_gbps(fabric_env), figure_dir,
                formats)

    for verb in LATENCY_VERBS:
        for label, column, axis_label in LATENCY_METRICS:
            written += _plot_device_measurement(
                df, campaign, f"ib_{verb}_lat", column, f"{verb} {label} latency",
                axis_label, f"latency_{verb}_{label}", figure_dir, formats,
                log_y=True)

    written += _plot_device_measurement(
        df, campaign, "ib_atomic_bw", "msg_rate_Mpps",
        "atomic message rate", "Message rate (million operations/s)",
        "atomic_message_rate", figure_dir, formats, bidi_flag=False)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(
        usage=("%(prog)s [--results PATH] [--out DIR] "
               "[--format {pdf,png,both}] [--dpi DPI] [--sans]"),
        description="Plot RDMA perftest results.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=("Examples:\n"
                "  python plot_perftest.py\n"
                "  python plot_perftest.py --results "
                "perftest-results/my-campaign --out figs/my-campaign "
                "--format pdf"))
    parser.add_argument(
        "--results", default=str(SCRIPT_DIR / "perftest-results"),
        metavar="PATH",
        help="Results tree or one campaign directory. Default: "
             "perftest-results beside this script.")
    parser.add_argument(
        "--out", default=str(SCRIPT_DIR / "figs"),
        metavar="DIR",
        help="Figure directory. Default: figs beside this script.")
    parser.add_argument(
        "--format", default="both", choices=["pdf", "png", "both"],
        help="File format. both writes PDF and PNG. Default: both.")
    parser.add_argument(
        "--dpi", type=int, default=200,
        help="PNG resolution in dots per inch. Default: 200.")
    parser.add_argument(
        "--sans", action="store_true",
        help="Use sans-serif fonts. Default: serif.")
    if len(sys.argv) == 1:
        parser.print_help()
        return
    args = parser.parse_args()

    formats = ["pdf", "png"] if args.format == "both" else [args.format]
    apply_paper_style(args.dpi, serif=not args.sans)
    df, all_rows = load_perftest_results(args.results)
    link_speeds = load_link_speeds(args.results)
    report_coverage(df, all_rows, link_speeds)
    print("figures")
    figure_count = 0
    for campaign in sorted(df["campaign"].unique()):
        count = plot_campaign_figures(
            df, link_speeds, str(campaign), args.out, formats)
        figure_count += count
        print(f"  {campaign}: {count} figure(s)")
    print(f"wrote {figure_count} figure(s) to {args.out}")


if __name__ == "__main__":
    main()
