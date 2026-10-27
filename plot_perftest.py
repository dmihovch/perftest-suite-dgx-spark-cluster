#!/usr/bin/env python3
"""Plot RDMA perftest transmission-time curves by campaign and verb."""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")                      # Write figures without a display.
import matplotlib.pyplot as plt
from matplotlib.axes import Axes
from matplotlib import font_manager
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.ticker import FuncFormatter, LogLocator
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
        "figure.constrained_layout.use": False,
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


# Perftest data tests sweep powers of two from 2 B through 8 MiB.
VERBS = ("send", "write", "read")
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
    ISOLATION_MODE: dict(color="#0072B2", label="isolation"),
    CONCURRENT_MODE: dict(color="#D55E00", label="concurrent"),
}
LATENCY_SERIES = (
    ("lat_typical_usec", "typical", "-"),
    ("lat_p99_usec", "p99", "--"),
)
BANDWIDTH_SERIES = (
    (False, "unidirectional", "-"),
    (True, "bidirectional", "--"),
)
MAX_SIZE_TICK_LABELS = 5
TRANSFER_VOLUME_GB = (0.1, 1.0, 10.0, 100.0)


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


def _link_identity(local_ip: object, peer_ip: object) -> tuple[str, str] | None:
    """Return a role-independent identity and label for an IP pair."""
    if (not isinstance(local_ip, str) or not local_ip
            or not isinstance(peer_ip, str) or not peer_ip):
        return None
    endpoints = sorted((local_ip, peer_ip))
    return "|".join(endpoints), " ↔ ".join(endpoints)


def _link_details_by_device(run_directory: Path) -> dict[str, tuple[str, str]]:
    """Read canonical endpoint identities from one run's environment files."""
    details: dict[str, tuple[str, str]] = {}
    for path in sorted((run_directory / "environment").glob("*/link.json")):
        with path.open() as handle:
            link = json.load(handle)
        device = link.get("device")
        identity = _link_identity(link.get("local_ip"), link.get("peer_ip"))
        if isinstance(device, str) and identity is not None:
            details[device] = identity
    return details


def load_perftest_results(
        results_root: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load result CSVs and select one row per campaign, run, link, and test.

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
        link_details = _link_details_by_device(Path(path).parent)
        link_identities = {device: details[0]
                           for device, details in link_details.items()}
        link_labels = {device: details[1]
                       for device, details in link_details.items()}
        raw_devices = frame["device"].astype(str)
        frame["link_identity"] = raw_devices.map(link_identities)
        missing_identity = frame["link_identity"].isna()
        frame.loc[missing_identity, "link_identity"] = raw_devices[
            missing_identity].map(lambda device: f"{role}|{device}")
        frame["device"] = raw_devices.map(link_labels).fillna(
            raw_devices.map(lambda device: f"{role}:{device}"))
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

    keys = ["campaign", "run", "link_identity", "test", "bidi", "mode",
            "size_bytes"]
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
        campaign, run, role = _campaign_run_role(results_root, path)
        with open(path) as handle:
            link = json.load(handle)
        device_name = link.get("device") or os.path.basename(
            os.path.dirname(path))
        identity = _link_identity(link.get("local_ip"), link.get("peer_ip"))
        rows.append({
            "campaign": campaign,
            "run": run,
            "device": identity[1] if identity is not None
            else f"{role}:{device_name}",
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
    """Endpoint labels in stable, case-insensitive order."""
    return sorted(df["device"].astype(str).unique(), key=str.casefold)


def _size_label(size_bytes: float) -> str:
    """Format a message size with binary units."""
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):g} MiB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:g} KiB"
    return f"{size_bytes:g} B"


def _payload_size_ticks(sizes: list[float]) -> list[float]:
    """Choose readable payload ticks while keeping the largest size labeled."""
    if len(sizes) <= MAX_SIZE_TICK_LABELS:
        return sizes
    stride = math.ceil((len(sizes) - 1) / (MAX_SIZE_TICK_LABELS - 1))
    tick_sizes = sizes[::stride][:MAX_SIZE_TICK_LABELS]
    if sizes[-1] not in tick_sizes:
        if len(tick_sizes) == MAX_SIZE_TICK_LABELS:
            tick_sizes[-1] = sizes[-1]
        else:
            tick_sizes.append(sizes[-1])
    return tick_sizes


def _format_log_tick(value: float, _position: int | None) -> str:
    """Display logarithmic ticks as numeric values in the axis unit."""
    if not math.isfinite(value) or value <= 0:
        return ""
    return f"{value:.3g}"


def _set_log_y_axis(axis: Axes) -> None:
    """Use powers-of-ten ticks with numeric labels on a log y axis."""
    axis.set_yscale("log", base=10)
    axis.yaxis.set_major_locator(LogLocator(base=10, subs=(1.0,)))
    axis.yaxis.set_major_formatter(FuncFormatter(_format_log_tick))


def _results_summary(df: pd.DataFrame) -> str:
    """Return campaign, run, and campaign-link counts."""
    campaigns = int(df["campaign"].nunique())
    devices = int(df.groupby("campaign")["device"].nunique().sum())
    runs = int(df.groupby(["campaign", "run"]).ngroups)
    return (f"{campaigns} campaign(s), {runs} run(s), "
            f"{devices} campaign-link pair(s)")


def _role_agreement(all_rows: pd.DataFrame) -> str:
    """Return maximum client-host differences for shared measurements."""
    keys = ["campaign", "run", "link_identity", "test", "bidi", "mode",
            "size_bytes"]
    merged = all_rows[all_rows["role"] == "client"].merge(
        all_rows[all_rows["role"] == "host"], on=keys, suffixes=("_c", "_h"))
    parts = []
    for column, label in (("bw_avg_GBps", "bandwidth"),
                          ("lat_avg_usec", "average message delay")):
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
              f"{len(devices)} link(s): {', '.join(devices)}")
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
            print(f"  nominal port rate: {campaign}: {shown} Gb/s")


def _campaign_figure_directory(out: str, campaign: str) -> str:
    """Return an output directory unique to a campaign path."""
    return os.path.join(out, quote(campaign, safe=""))


def _measurement_rows(df: pd.DataFrame, campaign: str, verb: str,
                      test_kind: str, mode: str, value_column: str,
                      bidi: bool | None = None) -> pd.DataFrame:
    """Select successful rows for one link, mode, test, and metric."""
    selected = df[
        (df["campaign"] == campaign)
        & (df["test"] == f"ib_{verb}_{test_kind}")
        & (df["mode"] == mode)
        & (df["error"] == "")
        & df["size_bytes"].notna()
        & (df["size_bytes"] > 0)
        & df[value_column].notna()
    ]
    if bidi is not None:
        selected = selected[selected["bidi"].eq(bidi)]
    return selected


def _plot_run_summary(axis: Axes, rows: pd.DataFrame, value_column: str,
                      label: str, color: str, line_style: str
                      ) -> tuple[float, float] | None:
    """Plot the median measurement and the 10th–90th percentile run range."""
    if rows.empty:
        return None
    summary = rows.groupby("size_bytes")[value_column].agg(
        median="median",
        lower=lambda values: values.quantile(0.1),
        upper=lambda values: values.quantile(0.9),
    ).sort_index()
    payload_sizes = summary.index.to_numpy(dtype=float)
    median_values = summary["median"].to_numpy(dtype=float)
    axis.fill_between(
        payload_sizes,
        summary["lower"].to_numpy(dtype=float),
        summary["upper"].to_numpy(dtype=float),
        color=color,
        alpha=0.12,
        linewidth=0,
        zorder=1,
    )
    axis.plot(
        payload_sizes,
        median_values,
        color=color,
        linestyle=line_style,
        linewidth=1.7,
        marker="o",
        markersize=2.4,
        label=label,
        zorder=3,
    )
    if median_values[-1] <= 0:
        return None
    return float(payload_sizes[-1]), float(median_values[-1])


def _failure_annotation(df: pd.DataFrame, campaign: str, verb: str,
                        test_kind: str, device: str,
                        bidi: bool | None = None) -> str:
    """Describe failed concurrent rows when that link has no measurement."""
    test_rows = df[
        (df["campaign"] == campaign)
        & (df["test"] == f"ib_{verb}_{test_kind}")
        & (df["device"] == device)
        & (df["mode"] == CONCURRENT_MODE)
    ]
    if bidi is not None:
        test_rows = test_rows[test_rows["bidi"].eq(bidi)]
    errors = test_rows[test_rows["error"] != ""]
    measurements = test_rows[test_rows["error"] == ""]
    if errors.empty or not measurements.empty:
        return ""
    error_types = ", ".join(sorted(errors["error"].unique()))
    return f"concurrent unavailable ({error_types})"


def _figure_axes(devices: list[str], ylabel: str,
                 tick_sizes: list[float], *, x_label: str = "Payload size",
                 x_base: int = 2,
                 tick_labels: list[str] | None = None
                 ) -> tuple[Figure, np.ndarray]:
    """Create a two-column grid of link panels with shared axes."""
    column_count = 2 if len(devices) > 1 else 1
    row_count = math.ceil(len(devices) / column_count)
    figure_width = min(TEXT_W, max(COL_W, 3.1 * column_count))
    figure_height = 2.55 * row_count + 0.4
    figure, axes = plt.subplots(
        row_count,
        column_count,
        figsize=(figure_width, figure_height),
        squeeze=False,
        sharex=True,
        sharey=True,
    )
    for device_index, device in enumerate(devices):
        row_index, column_index = divmod(device_index, column_count)
        axis = axes[row_index, column_index]
        axis.set_title(device, fontsize=8)
        axis.set_xscale("log", base=x_base)
        labels = (tick_labels if tick_labels is not None else
                  [_size_label(size) for size in tick_sizes])
        axis.set_xticks(tick_sizes, labels)
        axis.tick_params(axis="both", labelsize=7)
        axis.grid(True, which="major", alpha=0.25, linewidth=0.5)
        axis.grid(True, which="minor", alpha=0.10, linewidth=0.4)
        axis.set_xlabel(x_label)
        if column_index == 0:
            axis.set_ylabel(ylabel)
    for unused_index in range(len(devices), row_count * column_count):
        row_index, column_index = divmod(unused_index, column_count)
        axes[row_index, column_index].set_visible(False)
    figure.subplots_adjust(
        left=0.11,
        right=0.98,
        bottom=0.19,
        top=0.86,
        hspace=0.40,
        wspace=0.22,
    )
    return figure, axes


def _plot_latency_figure(df: pd.DataFrame, campaign: str, verb: str,
                         out: str, formats: list[str]) -> bool:
    """Plot typical and p99 latency against payload size for each link."""
    rows = df[(df["campaign"] == campaign)
              & (df["test"] == f"ib_{verb}_lat")]
    if rows.empty:
        return False
    devices = devices_in_order(rows)
    sizes = sorted(float(size) for size in rows["size_bytes"].dropna().unique()
                   if float(size) > 0)
    if not sizes:
        return False
    tick_sizes = _payload_size_ticks(sizes)

    figure, axes = _figure_axes(devices, "Latency (µs)", tick_sizes)
    handles: dict[str, Line2D] = {}
    for device_index, device in enumerate(devices):
        row_index, column_index = divmod(
            device_index, 2 if len(devices) > 1 else 1)
        axis = axes[row_index, column_index]
        for mode in (ISOLATION_MODE, CONCURRENT_MODE):
            color = MODE_STYLE[mode]["color"]
            for value_column, metric_label, line_style in LATENCY_SERIES:
                metric_rows = _measurement_rows(
                    df, campaign, verb, "lat", mode, value_column)
                metric_rows = metric_rows[metric_rows["device"] == device]
                label = f"{MODE_STYLE[mode]['label']}: {metric_label}"
                _plot_run_summary(
                    axis, metric_rows, value_column, label, color, line_style)
                if not metric_rows.empty:
                    handles.setdefault(
                        label,
                        Line2D([], [], color=color, linestyle=line_style,
                               linewidth=1.7, marker="o", markersize=2.4,
                               label=label),
                    )
        annotation = _failure_annotation(df, campaign, verb, "lat", device)
        if annotation:
            axis.text(
                0.98,
                0.04,
                annotation,
                transform=axis.transAxes,
                ha="right",
                va="bottom",
                fontsize=6.5,
                color="#8B2E1B",
                bbox={"facecolor": "white", "edgecolor": "none", "alpha": 0.85},
            )
        _set_log_y_axis(axis)

    figure.suptitle(f"{verb.capitalize()} latency vs payload size\n{campaign}",
                    fontsize=10, y=0.97)
    if handles:
        figure.legend(handles=list(handles.values()), fontsize=7, ncol=4,
                      loc="lower center", bbox_to_anchor=(0.5, 0.01))
    output_directory = _campaign_figure_directory(out, campaign)
    save_figure(figure, f"latency_{verb}", output_directory, formats,
                tight=True)
    return True


def _plot_bandwidth_figure(df: pd.DataFrame, campaign: str, verb: str,
                           link_speeds: pd.DataFrame, out: str,
                           formats: list[str]) -> bool:
    """Plot unidirectional and bidirectional throughput for each link."""
    rows = df[(df["campaign"] == campaign)
              & (df["test"] == f"ib_{verb}_bw")]
    if rows.empty:
        return False
    devices = devices_in_order(rows)
    sizes = sorted(float(size) for size in rows["size_bytes"].dropna().unique()
                   if float(size) > 0)
    if not sizes:
        return False
    tick_sizes = _payload_size_ticks(sizes)

    figure, axes = _figure_axes(
        devices, "Throughput (decimal GB/s)", tick_sizes)
    handles: dict[str, Line2D] = {}
    for device_index, device in enumerate(devices):
        row_index, column_index = divmod(
            device_index, 2 if len(devices) > 1 else 1)
        axis = axes[row_index, column_index]
        for mode in (ISOLATION_MODE, CONCURRENT_MODE):
            color = MODE_STYLE[mode]["color"]
            for bidi, direction_label, line_style in BANDWIDTH_SERIES:
                value_column = "bw_avg_GBps"
                metric_rows = _measurement_rows(
                    df, campaign, verb, "bw", mode, value_column, bidi)
                metric_rows = metric_rows[metric_rows["device"] == device]
                label = f"{MODE_STYLE[mode]['label']}: {direction_label}"
                last_point = _plot_run_summary(
                    axis, metric_rows, value_column, label, color, line_style)
                if last_point is not None:
                    offset_y = (7 if not bidi else -9)
                    if mode == CONCURRENT_MODE:
                        offset_y += 9 if not bidi else -9
                    axis.annotate(
                        f"{last_point[1]:.3g} GB/s",
                        xy=last_point,
                        xytext=(4, offset_y),
                        textcoords="offset points",
                        color=color,
                        fontsize=6,
                        ha="left",
                        va="center",
                        clip_on=True,
                    )
                if not metric_rows.empty:
                    handles.setdefault(
                        label,
                        Line2D([], [], color=color, linestyle=line_style,
                               linewidth=1.7, marker="o", markersize=2.4,
                               label=label),
                    )

        rate_rows = link_speeds[
            (link_speeds["campaign"] == campaign)
            & (link_speeds["device"] == device)
        ]
        rates = sorted({float(rate) for rate in
                        rate_rows["device_rate_gbps"].dropna()})
        if len(rates) == 1:
            single_direction_rate = rates[0] / 8.0
            axis.axhline(single_direction_rate, color="#3F4650",
                         linestyle=(0, (4, 2)), linewidth=0.9, zorder=2)
            axis.axhline(2 * single_direction_rate, color="#747B84",
                         linestyle=":", linewidth=0.9, zorder=2)
            handles.setdefault(
                "Nominal link rate, one direction",
                Line2D([], [], color="#3F4650", linestyle=(0, (4, 2)),
                       linewidth=0.9, label="Nominal link rate, one direction"),
            )
            handles.setdefault(
                "Nominal link rate, both directions",
                Line2D([], [], color="#747B84", linestyle=":",
                       linewidth=0.9,
                       label="Nominal link rate, both directions"),
            )
        annotation = _failure_annotation(df, campaign, verb, "bw", device)
        if annotation:
            axis.text(
                0.98,
                0.04,
                annotation,
                transform=axis.transAxes,
                ha="right",
                va="bottom",
                fontsize=6.5,
                color="#8B2E1B",
                bbox={"facecolor": "white", "edgecolor": "none", "alpha": 0.85},
            )
        _set_log_y_axis(axis)

    figure.suptitle(f"{verb.capitalize()} throughput vs payload size\n{campaign}",
                    fontsize=10, y=0.97)
    if handles:
        figure.legend(handles=list(handles.values()), fontsize=6.5, ncol=3,
                      loc="lower center", bbox_to_anchor=(0.5, 0.01))
    output_directory = _campaign_figure_directory(out, campaign)
    save_figure(figure, f"bandwidth_{verb}", output_directory, formats,
                tight=True)
    return True


def _paired_transfer_measurements(df: pd.DataFrame, campaign: str,
                                  verb: str, device: str, mode: str,
                                  bidi: bool) -> pd.DataFrame:
    """Pair each run's smallest-message latency with its largest-message rate."""
    latency_rows = _measurement_rows(
        df, campaign, verb, "lat", mode, "lat_typical_usec")
    latency_rows = latency_rows[
        (latency_rows["device"] == device)
        & (latency_rows["lat_typical_usec"] > 0)
    ]
    latency_by_run = (latency_rows.sort_values("size_bytes")
                      .drop_duplicates("run", keep="first")
                      .loc[:, ["run", "lat_typical_usec"]])

    bandwidth_rows = _measurement_rows(
        df, campaign, verb, "bw", mode, "bw_avg_GBps", bidi)
    bandwidth_rows = bandwidth_rows[
        (bandwidth_rows["device"] == device)
        & (bandwidth_rows["bw_avg_GBps"] > 0)
    ]
    bandwidth_by_run = (bandwidth_rows.sort_values("size_bytes")
                        .drop_duplicates("run", keep="last")
                        .loc[:, ["run", "bw_avg_GBps"]])
    return latency_by_run.merge(
        bandwidth_by_run, on="run", how="inner", validate="one_to_one")


def _plot_transmission_time_summary(axis: Axes, rows: pd.DataFrame,
                                    label: str, color: str,
                                    line_style: str) -> bool:
    """Plot estimated bulk transfer time and its 10th–90th run range."""
    if rows.empty:
        return False
    volume_gb = np.asarray(TRANSFER_VOLUME_GB, dtype=float)
    latency_seconds = rows["lat_typical_usec"].to_numpy(dtype=float) / 1e6
    throughput_gbps = rows["bw_avg_GBps"].to_numpy(dtype=float)
    estimates = (latency_seconds[:, np.newaxis]
                 + volume_gb[np.newaxis, :] / throughput_gbps[:, np.newaxis])
    median_seconds = np.median(estimates, axis=0)
    lower_seconds = np.quantile(estimates, 0.1, axis=0)
    upper_seconds = np.quantile(estimates, 0.9, axis=0)
    axis.fill_between(
        volume_gb,
        lower_seconds,
        upper_seconds,
        color=color,
        alpha=0.12,
        linewidth=0,
        zorder=1,
    )
    axis.plot(
        volume_gb,
        median_seconds,
        color=color,
        linestyle=line_style,
        linewidth=1.7,
        marker="o",
        markersize=3,
        label=label,
        zorder=3,
    )
    return True


def _plot_transmission_time_figure(df: pd.DataFrame, campaign: str,
                                   verb: str, out: str,
                                   formats: list[str]) -> bool:
    """Plot estimated transfer time for decimal-GB volumes, per link."""
    rows = df[(df["campaign"] == campaign)
              & (df["test"] == f"ib_{verb}_bw")]
    if rows.empty:
        return False
    devices = devices_in_order(rows)
    volumes = list(TRANSFER_VOLUME_GB)
    figure, axes = _figure_axes(
        devices,
        "Estimated transfer time (s)",
        volumes,
        x_label="Total data volume",
        x_base=10,
        tick_labels=[f"{volume:g} GB" for volume in volumes],
    )
    handles: dict[str, Line2D] = {}
    plotted_series = 0
    for device_index, device in enumerate(devices):
        row_index, column_index = divmod(
            device_index, 2 if len(devices) > 1 else 1)
        axis = axes[row_index, column_index]
        for mode in (ISOLATION_MODE, CONCURRENT_MODE):
            color = MODE_STYLE[mode]["color"]
            for bidi, direction_label, line_style in BANDWIDTH_SERIES:
                paired_rows = _paired_transfer_measurements(
                    df, campaign, verb, device, mode, bidi)
                label = f"{MODE_STYLE[mode]['label']}: {direction_label}"
                if _plot_transmission_time_summary(
                        axis, paired_rows, label, color, line_style):
                    plotted_series += 1
                    handles.setdefault(
                        label,
                        Line2D([], [], color=color, linestyle=line_style,
                               linewidth=1.7, marker="o", markersize=3,
                               label=label),
                    )
        _set_log_y_axis(axis)

    if not plotted_series:
        plt.close(figure)
        return False

    figure.suptitle(
        f"{verb.capitalize()} estimated transmission time\n{campaign}",
        fontsize=10,
        y=0.97,
    )
    figure.text(
        0.5,
        0.105,
        "Estimate = smallest-payload typical latency + volume / largest-payload average throughput.",
        ha="center",
        fontsize=6.5,
    )
    if handles:
        figure.legend(handles=list(handles.values()), fontsize=6.5, ncol=4,
                      loc="lower center", bbox_to_anchor=(0.5, 0.01))
    output_directory = _campaign_figure_directory(out, campaign)
    save_figure(figure, f"transmission_time_{verb}", output_directory,
                formats, tight=True)
    return True


def plot_campaign_figures(df: pd.DataFrame, campaign: str,
                          link_speeds: pd.DataFrame, out: str,
                          formats: list[str]) -> int:
    """Write latency, throughput, and estimated transfer-time figures."""
    written = 0
    for verb in VERBS:
        written += _plot_latency_figure(df, campaign, verb, out, formats)
        written += _plot_bandwidth_figure(
            df, campaign, verb, link_speeds, out, formats)
        written += _plot_transmission_time_figure(
            df, campaign, verb, out, formats)
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
            df, str(campaign), link_speeds, args.out, formats)
        figure_count += count
        print(f"  {campaign}: {count} figure(s)")
    print(f"wrote {figure_count} figure(s) to {args.out}")


if __name__ == "__main__":
    main()
