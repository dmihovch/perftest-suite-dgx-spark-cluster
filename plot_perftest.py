#!/usr/bin/env python3
"""Plot RDMA perftest transmission-time curves by campaign and verb."""
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
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
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
TRANSMISSION_SERIES = {
    "latency": ("lat_avg_usec", "measured latency", "-"),
    "bandwidth uni": ("bw_avg_GBps", "bandwidth-derived unidirectional", "--"),
    "bandwidth bidi": ("bw_avg_GBps", "bandwidth-derived bidirectional", ":"),
}
MAX_SIZE_TICK_LABELS = 7


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

    keys = ["campaign", "run", "device", "test", "bidi", "mode",
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
    keys = ["campaign", "run", "device", "test", "bidi", "mode",
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


def _campaign_figure_directory(out: str, campaign: str) -> str:
    """Return an output directory unique to a campaign path."""
    return os.path.join(out, quote(campaign, safe=""))


def _transmission_time_rows(
        df: pd.DataFrame, campaign: str, verb: str) -> pd.DataFrame:
    """Convert latency and bandwidth rows into per-payload transmission times."""
    records: list[dict[str, str | int | float]] = []
    for series, (value_column, _, _) in TRANSMISSION_SERIES.items():
        test_kind = "lat" if series == "latency" else "bw"
        rows = df[(df["campaign"] == campaign)
                  & (df["test"] == f"ib_{verb}_{test_kind}")]
        if series == "bandwidth uni":
            rows = rows[rows["bidi"].eq(False)]
        elif series == "bandwidth bidi":
            rows = rows[rows["bidi"].eq(True)]

        selected_rows = rows.loc[
            (rows["error"] == "")
            & rows["size_bytes"].notna()
            & (rows["size_bytes"] > 0)
            & rows[value_column].notna(),
            ["device", "mode", "run", "size_bytes", value_column],
        ]
        for device, mode, run, size_bytes, value in selected_rows.itertuples(
                index=False, name=None):
            payload_bytes = float(size_bytes)
            measured_value = float(value)
            if series == "latency":
                time_usec = measured_value
            else:
                if measured_value <= 0:
                    continue
                direction_count = 2 if series == "bandwidth bidi" else 1
                time_usec = (
                    payload_bytes * direction_count / (measured_value * 1000)
                )
            if not np.isfinite(time_usec) or time_usec <= 0:
                continue
            records.append({
                "campaign": campaign,
                "device": str(device),
                "mode": str(mode),
                "run": int(run),
                "size_bytes": payload_bytes,
                "series": series,
                "time_usec": time_usec,
            })
    return pd.DataFrame.from_records(
        records,
        columns=["campaign", "device", "mode", "run", "size_bytes",
                 "series", "time_usec"],
    )


def _plot_transmission_time_curves(
        df: pd.DataFrame, campaign: str, verb: str, out: str,
        formats: list[str]) -> bool:
    """Plot per-payload transmission-time curves for one RDMA verb."""
    curves = _transmission_time_rows(df, campaign, verb)
    if curves.empty:
        return False

    devices = devices_in_order(curves)
    modes = [
        mode for mode in (ISOLATION_MODE, CONCURRENT_MODE)
        if mode in set(curves["mode"].astype(str))
    ]
    series_values = [
        series for series in TRANSMISSION_SERIES
        if series in set(curves["series"].astype(str))
    ]
    sizes = sorted(float(size) for size in curves["size_bytes"].unique())
    tick_stride = max(1, (len(sizes) + MAX_SIZE_TICK_LABELS - 1)
                      // MAX_SIZE_TICK_LABELS)
    tick_sizes = sizes[::tick_stride]
    if sizes[-1] not in tick_sizes:
        tick_sizes.append(sizes[-1])

    figure_width = min(TEXT_W, max(COL_W, 1.7 * len(devices)))
    figure, axes = plt.subplots(
        1, len(devices), figsize=(figure_width, 3.6), squeeze=False,
        sharex=True, sharey=True,
    )
    figure.subplots_adjust(left=0.12, right=0.99, bottom=0.25,
                           top=0.82, wspace=0.3)
    legend_handles: dict[str, Line2D] = {}

    for device_index, device in enumerate(devices):
        axis = axes[0, device_index]
        device_rows = curves[curves["device"] == device]
        for mode in modes:
            for series in series_values:
                line_rows = device_rows[
                    (device_rows["mode"] == mode)
                    & (device_rows["series"] == series)
                ]
                if line_rows.empty:
                    continue
                summary = (line_rows.groupby("size_bytes")["time_usec"]
                           .mean().sort_index())
                color = MODE_STYLE[mode]["color"]
                line_style = TRANSMISSION_SERIES[series][2]
                label = (f"{MODE_STYLE[mode]['label']}: "
                         f"{TRANSMISSION_SERIES[series][1]}")
                axis.plot(summary.index.astype(float), summary.to_numpy(),
                          color=color, linestyle=line_style, linewidth=1.6,
                          label=label)
                legend_handles.setdefault(
                    label,
                    Line2D([], [], color=color, linestyle=line_style,
                           linewidth=1.6, label=label),
                )

        axis.set_title(device, fontsize=8)
        axis.set_xscale("log", base=2)
        axis.set_yscale("log")
        axis.set_xticks(tick_sizes, [_size_label(size) for size in tick_sizes])
        axis.tick_params(axis="x", labelrotation=25, labelsize=7)
        axis.grid(True, which="major", alpha=0.25, linewidth=0.5)
        axis.set_xlabel("Payload size")
        if device_index == 0:
            axis.set_ylabel("Transfer time (µs)")

    figure.suptitle(
        f"{campaign}: {verb} transmission time by payload size",
        fontsize=9, y=0.97,
    )
    figure.legend(handles=list(legend_handles.values()), fontsize=7,
                  ncol=3, loc="lower center", bbox_to_anchor=(0.5, 0.01))
    output_directory = _campaign_figure_directory(out, campaign)
    save_figure(figure, f"transmission_time_{verb}", output_directory,
                formats, tight=True)
    return True


def plot_campaign_figures(df: pd.DataFrame, campaign: str, out: str,
                          formats: list[str]) -> int:
    """Write one transmission-time curve figure per available verb."""
    written = 0
    for verb in VERBS:
        written += _plot_transmission_time_curves(
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
            df, str(campaign), args.out, formats)
        figure_count += count
        print(f"  {campaign}: {count} figure(s)")
    print(f"wrote {figure_count} figure(s) to {args.out}")


if __name__ == "__main__":
    main()
