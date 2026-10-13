#!/usr/bin/env python3
"""Perftest-suite RDMA figures for the PMBS26 paper.

Reads every host-side results.csv under the perftest campaign tree
(perftest-results/<campaign>/run-*/host/ next to this script) and writes three
figures for the paper:

  perftest_bandwidth   send, write, and read bandwidth at the suite's fixed
                       message size, one-way and bidirectional, in
                       per-direction units, against each device's own captured
                       link rate.
  perftest_latency     send, write, read, and atomic latency per perftest
                       percentile tier (min, typical, mean, p99, p99.9) at the
                       suite's 2 B latency messages (8 B for the atomic op),
                       pooled across campaigns; the worst single sample per
                       verb is reported in the footnote.
  perftest_stability   each repeat's deviation from its (campaign, device,
                       test, bidi) run mean, which is the repeatability
                       evidence behind the min-max whiskers.
  perftest_aggregate   the sum of the isolated per-device rates against the
                       concurrent aggregate, unidirectional and bidirectional,
                       with the fabric ceiling from FABRIC_UNI_GBPS /
                       FABRIC_BIDI_GBPS (or the sum of the device link rates).

The loader reads both roles and prefers client rows: perftest prints the
measurement tables on the client, and the host side leaves the read and atomic
latency tables and the send_bw peak empty by design. A host row is used only
for a key with no client row, so a host-only tree still plots.
report_coverage() prints the role counts and the client/host agreement before
the figures are drawn. ib_atomic_bw runs at the perftest default 8 B, which is
not comparable with the suite's 64 KiB bandwidth tests; its measured message
rate is printed instead of drawn. The loader refuses a campaign that sweeps
message sizes under one test, because the per-device figures assume one
comparable size per test.

The bandwidth figure's reference rule is each device's own link rate, read from
the captured ibv_devinfo active_speed and active_width in
environment/<device>/link.json, so a bar at the rule is a saturated device
link. The aggregate figure compares the sum of the isolated per-device rates
against the concurrent aggregate, unidirectional and bidirectional, and draws
the fabric ceiling from the FABRIC_UNI_GBPS / FABRIC_BIDI_GBPS environment
variables (in Gb/s) when set, falling back to the sum of the device link rates.
The fabric ceiling is hardware knowledge the capture cannot measure, so on a
cluster with known caps (e.g. 200 Gb/s one-way / 400 Gb/s bidirectional on the
two-NIC DGX Spark) set the variables so a bar at the rule is a saturated
fabric.

Usage:
    python plot_perftest.py
    python plot_perftest.py \
        --results <campaign tree> --out <figure directory>
    python plot_perftest.py --sans

The script imports only numpy, pandas, and matplotlib, so it runs from any
location. The style block below mirrors plotting/plot_results.py and plot_paper.py in
the dgx-spark-llm-serving-benchmark repository; keep the copies in step when
the paper style changes.

The defaults read and write next to this script (perftest-results/ and figs/),
so the runner's default output tree plots with no arguments from any working
directory. Bandwidth values come from the CSVs' decimal GB/s column
(perftest's --report_gbits output divided by 8).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")                      # headless, safe over ssh
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.axes import Axes
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from matplotlib.ticker import FuncFormatter, NullFormatter


# ---- paper figure style ------------------------------------------------------
# Inlined from the repository's plotting scripts so the suite is standalone:
# rcParams from plot_results.py:set_rc() and the Type 42 font hardening from
# plot_paper.py:_publication_rc().
COL_W = 3.5      # one IEEE column, inches
TEXT_W = 7.0     # full text width, inches
SERIF_FAMILIES = ["Times New Roman", "Liberation Serif", "DejaVu Serif"]


def apply_paper_style(dpi: int, serif: bool) -> None:
    """Set the rcParams the repository's paper figures use.

    IEEE PDF eXpress rejects matplotlib's default Type 3 font subsetting, so
    fonts are embedded as TrueType (Type 42) outlines in both font modes.
    Mathtext is mapped to a concrete serif family when one is installed:
    naming an absent font drops mathtext to DejaVu Sans rather than down the
    font.serif fallback list.
    """
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
    """Write one file per format under out/ and close the figure.

    tight=True expands the saved bounding box to include manual fig.text
    footnotes, which constrained_layout does not reserve room for.
    """
    os.makedirs(out, exist_ok=True)
    keyword = dict(bbox_inches="tight") if tight else {}
    for fmt in formats:
        path = os.path.join(out, f"{name}.{fmt}")
        fig.savefig(path, **keyword)
        print(f"  wrote {path}")
    plt.close(fig)


SCRIPT_DIR = Path(__file__).resolve().parent


# Bandwidth verbs plotted from the suite's fixed 64 KiB tests. atomic_bw is
# measured at 8 B and is reported as message rate by report_coverage().
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

# Hues per verb, from the Okabe-Ito set shared by the other paper figures.
# atomic is drawn only in the latency figure; its bandwidth test runs at 8 B
# and is not comparable with the 64 KiB verbs.
VERB_STYLE = {
    "send": "#0072B2",
    "write": "#D55E00",
    "read": "#009E73",
    "atomic": "#CC79A7",
}
# Verb is encoded by marker and linestyle in the latency figure.
VERB_LINE = {
    "send": dict(marker="o", linestyle="-"),
    "write": dict(marker="s", linestyle="--"),
    "read": dict(marker="^", linestyle="-"),
    "atomic": dict(marker="D", linestyle="--"),
}

# Campaign directories as written by runner.sh --campaign. short feeds the
# bandwidth figure's two-line device ticks; label feeds the legends. Unknown
# campaign names fall back to the raw name with the next palette entry, so a
# new campaign plots without edits here.
CAMPAIGN_STYLE = {
    "test-oct-6": dict(short="1 cable", label="1 cable (2 devices)",
                       color="#0072B2", marker="o"),
    "dual-200Gb-Oct-8": dict(short="2 cables", label="2 cables (4 devices)",
                             color="#D55E00", marker="s"),
}
_FALLBACK_CAMPAIGN_STYLE = (
    dict(color="#009E73", marker="^"),
    dict(color="#CC79A7", marker="D"),
    dict(color="#E69F00", marker="v"),
    dict(color="#56B4E9", marker="P"),
)

# Test modes written by perftest-suite.sh. isolation runs one device at a time;
# concurrent runs every device at once. Result trees predating the mode column
# are treated as isolation.
ISOLATION_MODE = "isolation"
CONCURRENT_MODE = "concurrent"

# perftest's latency summary columns in increasing magnitude order.
LATENCY_TIERS = (
    ("min", "lat_min_usec"),
    ("typical", "lat_typical_usec"),
    ("mean", "lat_avg_usec"),
    ("p99", "lat_p99_usec"),
    ("p99.9", "lat_p999_usec"),
)
REFERENCE_COLOR = "0.35"
FOOTNOTE_FS = 6.0
FOOTNOTE_COLOR = "0.25"
# A deviation needs at least two runs to be a repeatability reading; a
# single-run group is zero by construction and would understate the spread.
MIN_REPEAT_RUNS = 2
# Deterministic horizontal offsets for strip points, patterned on the
# variance figure's cycling JIT so repeated renders are identical.
_STRIP_JITTER = np.linspace(-0.28, 0.28, 7)
# Plain-number tick ladder for the latency figure's log axis, in microseconds.
_LATENCY_TICKS = (1.5, 2, 2.5, 3, 3.5, 4, 5, 6, 8, 10, 15, 20, 30, 50, 80,
                  100, 150, 200, 300, 500, 800, 1000)


def _campaign_run_role(results_root: str, path: str) -> tuple[str, int, str]:
    relative = os.path.relpath(path, results_root).split(os.sep)
    return relative[0], int(relative[1].split("-")[1]), relative[2]


def load_perftest_results(
        results_root: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Load every results.csv under <results_root>/<campaign>/run-*/<role>/.

    Returns (preferred, all_rows). preferred holds one row per (campaign, run,
    device, test, bidi): a measured client row wins, then a measured host row,
    then an error row, in that order. Perftest prints the measurement tables
    on the client, and the host side leaves the read and atomic latency tables
    and the send_bw peak empty by design, so the client row is the measurement
    of record. Host rows are kept only for keys with no client row, which
    keeps a host-only tree plottable. all_rows is every row read, which
    report_coverage() uses to compare the two roles. Numeric cells are
    coerced; the error column is kept verbatim. A CSV missing a required
    column raises with the file named.
    """
    paths = sorted(glob.glob(os.path.join(
        results_root, "*", "run-*", "*", "results.csv")))
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
    sizes = measured.groupby(["test", "bidi"])["size_bytes"].nunique(dropna=True)
    swept = sizes[sizes > 1]
    if len(swept):
        detail = ", ".join(f"{test} (bidi={bidi})" for test, bidi in swept.index)
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
    """Per-run link rates from each run's captured environment/link.json.

    device_rate_gbps is ibv_devinfo's active_speed x active_width, the device's
    own port rate. It is NaN when the capture predates those fields.
    """
    rows = []
    for path in sorted(glob.glob(os.path.join(
            results_root, "*", "run-*", "*", "environment", "*",
            "link.json"))):
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
    return df


def captured_device_rate_gbps(link_speeds: pd.DataFrame) -> float | None:
    """The single captured per-device link rate in Gb/s, None when not unique.

    The rate is ibv_devinfo's active_speed x active_width. One rule cannot label
    mixed rates correctly, so a capture with more than one value omits the rules
    and prints the values instead.
    """
    unique = sorted({float(v) for v in link_speeds["device_rate_gbps"].dropna()})
    if not unique:
        print("  reference: no ibv_devinfo active_speed/active_width captured, "
              "rate rules omitted")
        return None
    if len(unique) != 1:
        print(f"  reference: captured device rates {unique} Gb/s, "
              "rate rules omitted")
        return None
    return unique[0]


def campaigns_in_order(df: pd.DataFrame) -> list[str]:
    """Campaigns by device count then name, so 1-cable precedes 2-cable."""
    counts = df.groupby("campaign")["device"].nunique()
    return sorted(counts.index, key=lambda name: (counts[name], name))


def devices_in_order(df: pd.DataFrame) -> list[str]:
    """Device names in port order; casefold puts f0 before P2p1s0f0."""
    return sorted(df["device"].unique(), key=str.casefold)


def campaign_style(campaign: str, order: list[str]) -> dict[str, str]:
    if campaign in CAMPAIGN_STYLE:
        return CAMPAIGN_STYLE[campaign]
    fallback = _FALLBACK_CAMPAIGN_STYLE[order.index(campaign)
                                       % len(_FALLBACK_CAMPAIGN_STYLE)]
    return dict(short=campaign, label=campaign, **fallback)


def _size_label(size_bytes: float) -> str:
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:g} KiB"
    return f"{size_bytes:g} B"


def repeat_deviations(
        frame: pd.DataFrame, value_column: str) -> tuple[pd.DataFrame, int]:
    """Per-run deviation (%) from the run mean of its repeat group.

    The group key is (campaign, device, test, bidi), so the result isolates
    run-to-run scatter from device and campaign level offsets. Groups with
    fewer than MIN_REPEAT_RUNS runs are returned as an exclusion count.
    """
    keys = ["campaign", "device", "test", "bidi"]
    sub = frame[frame[value_column].notna()].copy()
    sub["repeat_count"] = sub.groupby(keys)[value_column].transform("count")
    sub["repeat_mean"] = sub.groupby(keys)[value_column].transform("mean")
    sub["deviation_pct"] = ((sub[value_column] - sub["repeat_mean"])
                            / sub["repeat_mean"] * 100.0)
    excluded = int((sub["repeat_count"] < MIN_REPEAT_RUNS).sum())
    return sub[sub["repeat_count"] >= MIN_REPEAT_RUNS], excluded


def _results_summary(df: pd.DataFrame) -> str:
    """Campaign, run, and device totals for the coverage header."""
    campaigns = df["campaign"].nunique()
    devices = int(df.groupby("campaign")["device"].nunique().sum())
    runs = int(df.groupby(["campaign", "run"]).ngroups)
    return (f"{campaigns} campaign(s), {runs} run(s), "
            f"{devices} campaign-device pair(s)")


def _role_agreement(all_rows: pd.DataFrame) -> str:
    """Max client/host relative difference per metric over shared keys."""
    keys = ["campaign", "run", "device", "test", "bidi", "mode"]
    merged = all_rows[all_rows["role"] == "client"].merge(
        all_rows[all_rows["role"] == "host"], on=keys, suffixes=("_c", "_h"))
    parts = []
    for column, label in (("bw_avg_GBps", "bandwidth"),
                          ("lat_typical_usec", "latency typical")):
        pairs = merged[merged[f"{column}_c"].notna()
                       & merged[f"{column}_h"].notna()]
        if len(pairs):
            relative = ((pairs[f"{column}_c"] - pairs[f"{column}_h"]).abs()
                        / pairs[f"{column}_h"] * 100.0)
            parts.append(f"{label} max |diff| {relative.max():.3f}% "
                         f"({len(pairs)} pairs)")
    return "; ".join(parts) if parts else "no shared client/host rows"


def report_coverage(df: pd.DataFrame, all_rows: pd.DataFrame,
                    link_speeds: pd.DataFrame) -> None:
    """Print runs, devices, measured-row counts, and captured link rates.

    Failed tests stay in results.csv as error rows. Listing every test whose
    measured row count falls short of its run count makes a missing test
    visible instead of silently absent from the figures.
    """
    print("coverage")
    print(f"  source: {len(df)} row(s) from {_results_summary(df)}")
    role_counts = all_rows.groupby("role").size()
    roles_text = ", ".join(f"{role} {count}"
                           for role, count in role_counts.items())
    host_used = int(((df["role"] == "host") & (df["error"] == "")).sum())
    print(f"  roles: {roles_text} row(s); client rows preferred, host rows "
          f"used for {host_used} key(s)")
    mode_counts = df.groupby("mode").size()
    modes_text = ", ".join(f"{mode} {count}"
                            for mode, count in mode_counts.items())
    print(f"  modes: {modes_text} row(s)")
    print(f"  client/host agreement: {_role_agreement(all_rows)}")
    for campaign in sorted(df["campaign"].unique()):
        csub = df[df["campaign"] == campaign]
        runs = sorted(int(r) for r in csub["run"].unique())
        devices = devices_in_order(csub)
        print(f"  {campaign}: {len(runs)} runs ({runs[0]}..{runs[-1]}), "
              f"{len(devices)} device(s): {', '.join(devices)}")
        for test in sorted(csub["test"].unique()):
            for bidi_flag in (False, True):
                for mode in sorted(csub["mode"].unique()):
                    sub = csub[(csub["test"] == test)
                               & (csub["bidi"] == bidi_flag)
                               & (csub["mode"] == mode)]
                    if sub.empty:
                        continue
                    measured = int((sub["error"] == "").sum())
                    if measured < len(sub):
                        tag = "bidi" if bidi_flag else "uni"
                        errors = sorted(set(
                            sub.loc[sub["error"] != "", "error"]))
                        print(f"    {test} ({tag}, {mode}): {measured}/{len(sub)} "
                              f"rows measured, error rows: {', '.join(errors)}")
    if len(link_speeds):
        for campaign, group in link_speeds.groupby("campaign"):
            rates = sorted({float(v)
                            for v in group["device_rate_gbps"].dropna()})
            shown = ", ".join(f"{v:.0f}" for v in rates) if rates else "none"
            print(f"  device link rate: {campaign}: {shown} Gb/s")
    atomic = df[(df["error"] == "") & (df["test"] == "ib_atomic_bw")]
    if len(atomic):
        sizes = ", ".join(
            _size_label(s) for s in sorted(atomic["size_bytes"].unique()))
        rates = atomic["msg_rate_Mpps"].dropna()
        print(f"  ib_atomic_bw: {len(atomic)} row(s) at {sizes}, "
              f"{rates.min():.2f}-{rates.max():.2f} Mpps; not plotted because "
              "its message size is not comparable with the 64 KiB bandwidth "
              "tests")


def _bandwidth_rules(device_rate_gbps: float | None) -> list[tuple[float, str, str]]:
    """Per-direction reference rules as (GB/s, label, linestyle).

    The rule is the device's own link rate from ibv_devinfo, so a bar at the
    rule is a saturated device link.
    """
    if device_rate_gbps is None:
        return []
    return [(device_rate_gbps / 8.0,
             f"{device_rate_gbps:.0f} Gb/s per device link", "--")]


def fig_bandwidth(df: pd.DataFrame, out: str, fmts: list[str],
                  device_rate_gbps: float | None) -> None:
    """Per-device bandwidth at the suite's tested message size.

    One panel in per-direction units, isolation mode only (one device at a
    time). perftest -b reports the sum of both directions, so the bidirectional
    bars are halved: every bar is one direction's rate and the reference rule
    applies unchanged. Bars are means over every device instance and repeat in
    the tree, with min-max whiskers. The rule is the device's own link rate.
    Only the average column is read; peak is the single best sample of a run.
    """
    measured = df[(df["error"] == "") & (df["mode"] == ISOLATION_MODE)]
    bw = measured[(measured["kind"] == "bw") & measured["verb"].isin(VERBS)
                  & measured["bw_avg_GBps"].notna()]
    if bw.empty:
        print("  perftest_bandwidth: no measured bandwidth rows, skipping")
        return
    campaigns = campaigns_in_order(bw)
    instances = int(bw.groupby(["campaign", "device"]).ngroups)
    counts = bw.groupby(["campaign", "device", "test", "bidi"])["run"].count()
    runs_text = (f"{int(counts.min())} repeats per device"
                 if counts.min() == counts.max()
                 else f"{int(counts.min())}-{int(counts.max())} repeats per device")
    sizes = ", ".join(_size_label(s) for s in sorted(bw["size_bytes"].unique()))
    roles_text = "+".join(sorted(bw["role"].unique()))
    bidi_mean = float(bw[bw["bidi"]]["bw_avg_GBps"].mean())

    fig, ax = plt.subplots(figsize=(TEXT_W, 2.8))
    width = 0.32
    highs: list[float] = []
    for column, verb in enumerate(VERBS):
        for direction, (bidi_flag, scale, hatch) in enumerate(
                ((False, 1.0, None), (True, 0.5, "///"))):
            values = (bw[(bw["verb"] == verb)
                         & (bw["bidi"] == bidi_flag)]["bw_avg_GBps"] * scale)
            if values.empty:
                continue
            center = column + (direction - 0.5) * width
            mean = float(values.mean())
            ax.bar(center, mean, width * 0.92, color=VERB_STYLE[verb],
                   hatch=hatch, edgecolor="white", linewidth=0, zorder=3)
            ax.errorbar(center, mean,
                        yerr=[[mean - float(values.min())],
                              [float(values.max()) - mean]],
                        fmt="none", ecolor="0.15", elinewidth=0.7,
                        capsize=1.5, capthick=0.7, zorder=4)
            highs.append(float(values.max()))
    rules = _bandwidth_rules(device_rate_gbps)
    for value, label, linestyle in rules:
        ax.axhline(value, color=REFERENCE_COLOR, linestyle=linestyle,
                   linewidth=0.9, zorder=1)
        # The rules are labeled in a right margin reserved by set_xlim, so the
        # text cannot land on the bars.
        ax.annotate(label, xy=(len(VERBS) - 0.5 + 0.06, value),
                    xytext=(0, 1), textcoords="offset points", ha="left",
                    va="bottom", fontsize=6.0, color=FOOTNOTE_COLOR)
        highs.append(value)
    ax.set_xlim(-0.5, len(VERBS) - 0.5 + 1.15)
    ax.set_ylim(0, max(highs, default=1.0) * 1.12)
    ax.set_xticks(range(len(VERBS)))
    ax.set_xticklabels(list(VERBS), fontsize=7)
    ax.grid(axis="x", visible=False)
    ax.set_ylabel("Bandwidth per direction (GB/s)", fontsize=8)
    handles = [Patch(facecolor=VERB_STYLE[verb], label=verb) for verb in VERBS]
    handles += [Patch(facecolor="0.75", label="one-way"),
                Patch(facecolor="0.75", hatch="///", edgecolor="white",
                      label="bidirectional")]
    # Anchored below the cable rule so the legend text does not sit on it.
    ax.legend(handles=handles, fontsize=6.5, ncol=2, loc="upper left",
              bbox_to_anchor=(0.0, 0.86), handlelength=1.4,
              columnspacing=1.0, handletextpad=0.4)
    fig.text(0.5, -0.02,
             f"{roles_text}-side perftest tables; {sizes} messages; "
             f"{runs_text}, one device tested at a time (isolation mode)\n"
             f"bars pool {instances} device instances over {len(campaigns)} "
             f"campaigns; whiskers = min-max; reference = each device's own "
             f"ibv_devinfo link rate\n"
             f"bidirectional bars halve the -b aggregate "
             f"(mean {bidi_mean:.1f} GB/s) to one direction",
             ha="center", va="top", fontsize=FOOTNOTE_FS,
             color=FOOTNOTE_COLOR, linespacing=1.4)
    save_figure(fig, "perftest_bandwidth", out, fmts, tight=True)


def fig_latency(df: pd.DataFrame, out: str, fmts: list[str]) -> None:
    """Send, write, read, and atomic latency by perftest percentile tier.

    One line per verb with the repeat-and-device min-max band behind it,
    pooled across campaigns because the two cabling configurations agree.
    Each run's t_max is the single worst sample of that run rather than a
    percentile, so the worst sample per verb is reported in the footnote: the
    read outlier at 421 us would compress the tier axis by an order of
    magnitude.
    """
    measured = df[(df["error"] == "") & (df["mode"] == ISOLATION_MODE)]
    lat = measured[(measured["kind"] == "lat")
                   & measured["verb"].isin(LATENCY_VERBS)
                   & measured["lat_typical_usec"].notna()]
    if lat.empty:
        print("  perftest_latency: no measured latency rows, skipping")
        return
    roles_text = "+".join(sorted(lat["role"].unique()))
    size_groups = lat.groupby("size_bytes")["verb"].agg(
        lambda verbs: "/".join(sorted(set(verbs))))
    sizes = ", ".join(f"{_size_label(size)} {verbs}"
                      for size, verbs in size_groups.items())

    fig, ax = plt.subplots(figsize=(COL_W, 2.8))
    xs = np.arange(len(LATENCY_TIERS))
    for verb in LATENCY_VERBS:
        sub = lat[lat["verb"] == verb]
        if sub.empty:
            continue
        means, lows, highs = [], [], []
        for _, column in LATENCY_TIERS:
            values = sub[column].dropna()
            means.append(float(values.mean()) if len(values) else np.nan)
            lows.append(float(values.min()) if len(values) else np.nan)
            highs.append(float(values.max()) if len(values) else np.nan)
        ax.plot(xs, means, color=VERB_STYLE[verb], markersize=3.0,
                linewidth=1.1, zorder=3, **VERB_LINE[verb])
        ax.fill_between(xs, lows, highs, color=VERB_STYLE[verb],
                        alpha=0.12, linewidth=0, zorder=2)
    ax.set_xticks(xs)
    ax.set_xticklabels([tier for tier, _ in LATENCY_TIERS], fontsize=6.5)
    ax.set_yscale("log")
    p999 = lat["lat_p999_usec"].dropna()
    top = max(3.0, float(p999.max()) * 1.15) if len(p999) else 3.0
    ax.set_ylim(1.2, top)
    # Plain-number ticks: a sub-decade log axis otherwise labels its minor
    # ticks with mathtext, which resolves the custom mathtext fontset and
    # warns about its cursive fallback for labels that should be plain.
    ax.set_yticks([t for t in _LATENCY_TICKS if 1.2 <= t <= top])
    ax.yaxis.set_major_formatter(FuncFormatter(lambda value, _: f"{value:g}"))
    ax.yaxis.set_minor_formatter(NullFormatter())
    ax.set_ylabel("One-way latency (µs)", fontsize=8)
    handles = [Line2D([], [], color=VERB_STYLE[verb], markersize=3,
                      label=verb, **VERB_LINE[verb])
               for verb in LATENCY_VERBS if (lat["verb"] == verb).any()]
    ax.legend(handles=handles, fontsize=6.2, ncol=2, loc="upper left",
              handlelength=1.4, columnspacing=1.0, handletextpad=0.4)
    worst = lat.groupby("verb")["lat_max_usec"].max()
    worst_text = ", ".join(f"{verb} {worst[verb]:.0f}"
                           for verb in LATENCY_VERBS if verb in worst.index)
    fig.text(0.5, -0.02,
             f"{roles_text}-side perftest tables; {sizes} messages; band = "
             "min-max across both campaigns' repeats and devices\n"
             f"worst single sample (t_max): {worst_text} µs",
             ha="center", va="top", fontsize=FOOTNOTE_FS,
             color=FOOTNOTE_COLOR, linespacing=1.4)
    save_figure(fig, "perftest_latency", out, fmts, tight=True)


def fig_stability(df: pd.DataFrame, out: str, fmts: list[str]) -> None:
    """Repeat-to-repeat scatter around each repeat group's mean.

    Normalizing per (campaign, device, test, bidi) removes device and campaign
    level offsets, so the strips show run-to-run noise. Panel (a) covers the
    bandwidth verbs, panel (b) the typical and p99 tiers of every latency
    verb. Each panel reports its own standard deviation and worst absolute
    deviation.
    """
    measured = df[(df["error"] == "") & (df["mode"] == ISOLATION_MODE)]
    bw = measured[(measured["kind"] == "bw") & measured["verb"].isin(VERBS)
                  & measured["bw_avg_GBps"].notna()]
    lat = measured[(measured["kind"] == "lat")
                   & measured["verb"].isin(LATENCY_VERBS)]
    if bw.empty and lat.empty:
        print("  perftest_stability: no measured rows, skipping")
        return
    campaigns = campaigns_in_order(measured)
    fig, (ax_bw, ax_lat) = plt.subplots(1, 2, figsize=(TEXT_W, 2.5))

    bw_specs = [(f"{verb}\n{'bidi' if bidi else 'uni'}", "bw_avg_GBps",
                 (bw["verb"] == verb) & (bw["bidi"] == bidi))
                for bidi in (False, True) for verb in VERBS]
    lat_specs = [(verb, column, lat["verb"] == verb)
                 for column in ("lat_typical_usec", "lat_p99_usec")
                 for verb in LATENCY_VERBS]
    for ax, frame, specs, title in (
            (ax_bw, bw, bw_specs, "(a) bandwidth"),
            (ax_lat, lat, lat_specs, "(b) latency")):
        deviations = _draw_repeat_strip(ax, frame, specs, campaigns)
        ax.set_title(title, fontsize=8)
        ax.set_xticks(range(len(specs)))
        ax.set_xticklabels([label for label, _, _ in specs], fontsize=6.2)
        ax.grid(axis="x", visible=False)
        ax.set_ylabel("Deviation from repeat mean (%)", fontsize=7.5)
        if deviations:
            span = max(1.0, float(np.abs(deviations).max()) * 1.3)
            ax.set_ylim(-span, span)
            ax.annotate(
                f"σ = {np.std(deviations, ddof=1):.2f}%   "
                f"max |dev| = {np.abs(deviations).max():.2f}%",
                xy=(0.99, 0.03), xycoords="axes fraction", ha="right",
                fontsize=6.0, color=FOOTNOTE_COLOR)
    _annotate_halves(ax_bw, len(VERBS), "unidirectional", "bidirectional")
    _annotate_halves(ax_lat, len(LATENCY_VERBS), "typical", "p99")
    save_figure(fig, "perftest_stability", out, fmts)


def _campaign_link_ceiling(link_speeds: pd.DataFrame, campaign: str,
                           device_count: int) -> float | None:
    """Sum of a campaign's per-device link rates in GB/s, None when unknown."""
    rates = link_speeds[link_speeds["campaign"] == campaign]["device_rate_gbps"].dropna()
    unique = sorted({float(v) for v in rates})
    if len(unique) != 1 or device_count < 1:
        return None
    return unique[0] * device_count / 8.0


def _fabric_ceiling_gbps(env_name: str, fallback_gbps: float | None) -> float | None:
    """Fabric ceiling in Gb/s from an env var, or the fallback when unset.

    The fabric ceiling is the aggregate the interconnect sustains in one
    direction (or in both directions for the bidirectional panel). It is
    hardware knowledge the capture cannot measure, so it is configurable; the
    fallback is derived from the captured per-device link rates.
    """
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        return fallback_gbps
    try:
        value = float(raw)
    except ValueError:
        raise SystemExit(f"{env_name} must be a number (got {raw!r})")
    if value <= 0:
        raise SystemExit(f"{env_name} must be positive (got {raw!r})")
    return value


def _draw_aggregate_panel(ax: Axes, csub: pd.DataFrame, bidi_flag: bool,
                          ceiling_gbps: float | None, title: str) -> None:
    """One aggregate panel: per-verb isolation-sum vs concurrent-sum bars."""
    sub = csub[csub["bidi"] == bidi_flag]
    sums = (sub.groupby(["run", "verb", "mode"])["bw_avg_GBps"]
            .sum().reset_index())
    means = sums.groupby(["verb", "mode"])["bw_avg_GBps"].mean()

    width = 0.36
    highs: list[float] = []
    for column, verb in enumerate(VERBS):
        for offset, mode, hatch in ((-0.5, ISOLATION_MODE, None),
                                    (0.5, CONCURRENT_MODE, "///")):
            key = (verb, mode)
            if key not in means.index:
                continue
            value = float(means.loc[key])
            ax.bar(column + offset * width, value, width * 0.92,
                   color=VERB_STYLE[verb], hatch=hatch, edgecolor="white",
                   linewidth=0, zorder=3)
            highs.append(value)
    if ceiling_gbps is not None:
        ceiling = ceiling_gbps / 8.0
        ax.axhline(ceiling, color=REFERENCE_COLOR, linestyle="--",
                   linewidth=0.9, zorder=1)
        ax.annotate(f"{ceiling:.1f} GB/s fabric ceiling",
                    xy=(len(VERBS) - 0.5, ceiling), xytext=(0, 2),
                    textcoords="offset points", ha="right", va="bottom",
                    fontsize=6.0, color=FOOTNOTE_COLOR)
        highs.append(ceiling)
    ax.set_xlim(-0.5, len(VERBS) - 0.5)
    ax.set_ylim(0, max(highs, default=1.0) * 1.12)
    ax.set_xticks(range(len(VERBS)))
    ax.set_xticklabels(list(VERBS), fontsize=7)
    ax.grid(axis="x", visible=False)
    ax.set_ylabel("Aggregate bandwidth (GB/s)", fontsize=8)
    ax.set_title(title, fontsize=8)


def fig_aggregate(df: pd.DataFrame, out: str, fmts: list[str],
                  link_speeds: pd.DataFrame) -> None:
    """Aggregate bandwidth: sum of isolated per-device rates vs concurrent.

    The isolation pass measures each device alone; the concurrent pass runs
    every device at once. The concurrent sum is the aggregate the interconnect
    sustains, and the gap between the two sums is the shared bottleneck. Panel
    (a) is the unidirectional aggregate, panel (b) the bidirectional aggregate
    (perftest -b reports the sum of both directions). The reference rules are
    the fabric ceilings: FABRIC_UNI_GBPS / FABRIC_BIDI_GBPS when set, otherwise
    the sum of the devices' own ibv_devinfo link rates (doubled for the
    bidirectional panel). Only the campaign with the most devices is drawn,
    because a saturation reading is only meaningful on the full configuration.
    """
    measured = df[df["error"] == ""]
    bw = measured[(measured["kind"] == "bw") & measured["verb"].isin(VERBS)
                  & measured["bw_avg_GBps"].notna()]
    if bw[bw["mode"] == CONCURRENT_MODE].empty:
        print("  perftest_aggregate: no concurrent rows, skipping")
        return

    campaign = campaigns_in_order(bw)[-1]
    csub = bw[bw["campaign"] == campaign]
    device_count = int(csub["device"].nunique())

    link_ceiling_gbs = _campaign_link_ceiling(link_speeds, campaign, device_count)
    uni_fallback_gbps = link_ceiling_gbs * 8.0 if link_ceiling_gbs is not None else None
    bidi_fallback_gbps = uni_fallback_gbps * 2 if uni_fallback_gbps is not None else None
    uni_ceiling_gbps = _fabric_ceiling_gbps("FABRIC_UNI_GBPS", uni_fallback_gbps)
    bidi_ceiling_gbps = _fabric_ceiling_gbps("FABRIC_BIDI_GBPS", bidi_fallback_gbps)

    fig, (ax_uni, ax_bidi) = plt.subplots(1, 2, figsize=(TEXT_W, 2.8))
    _draw_aggregate_panel(ax_uni, csub, False, uni_ceiling_gbps,
                          "(a) unidirectional")
    if bool(csub["bidi"].any()):
        _draw_aggregate_panel(ax_bidi, csub, True, bidi_ceiling_gbps,
                              "(b) bidirectional")
    else:
        ax_bidi.set_visible(False)
    handles = [Patch(facecolor="0.75", label="isolated (sum of devices)"),
               Patch(facecolor="0.75", hatch="///", edgecolor="white",
                     label="concurrent (all devices)")]
    ax_uni.legend(handles=handles, fontsize=6.5, loc="upper left")
    fig.text(0.5, -0.02,
             f"{campaign}: {device_count} devices; bars sum the per-device "
             "rates of each run\n"
             "isolated = one device at a time; concurrent = every device at "
             "once; ceiling = FABRIC_UNI_GBPS / FABRIC_BIDI_GBPS, or the sum "
             "of ibv_devinfo device link rates",
             ha="center", va="top", fontsize=FOOTNOTE_FS, color=FOOTNOTE_COLOR,
             linespacing=1.4)
    save_figure(fig, "perftest_aggregate", out, fmts, tight=True)


def _annotate_halves(ax: Axes, half: int, left: str, right: str) -> None:
    """Divider and half labels for a two-group strip panel."""
    ax.axvline(half - 0.5, color="0.85", linewidth=0.7, zorder=0)
    for center, label in ((half / 2 - 0.5, left), (half * 1.5 - 0.5, right)):
        ax.annotate(label, xy=(center, 1.0),
                    xycoords=("data", "axes fraction"), xytext=(0, 2),
                    textcoords="offset points", ha="center", fontsize=6.5,
                    color=FOOTNOTE_COLOR)


def _draw_repeat_strip(ax: Axes, frame: pd.DataFrame,
                       specs: list[tuple[str, str, pd.Series]],
                       campaigns: list[str]) -> list[float]:
    """Strip one deviation cloud per spec; return every plotted deviation."""
    ax.axhline(0, color="0.6", linewidth=0.7, zorder=0)
    deviations: list[float] = []
    for index, (label, value_column, mask) in enumerate(specs):
        points, excluded = repeat_deviations(frame[mask], value_column)
        if excluded:
            print(f"  stability: {label!r}: excluded {excluded} row(s) in "
                  f"groups with fewer than {MIN_REPEAT_RUNS} runs")
        for campaign in campaigns:
            csub = points[points["campaign"] == campaign]
            if csub.empty:
                continue
            style = campaign_style(campaign, campaigns)
            values = csub["deviation_pct"].to_numpy()
            offsets = np.array([_STRIP_JITTER[k % len(_STRIP_JITTER)]
                                for k in range(len(values))])
            ax.scatter(index + offsets, values, s=6, color=style["color"],
                       marker=style["marker"], linewidths=0, alpha=0.55,
                       zorder=3)
            deviations.extend(float(v) for v in values)
    return deviations


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Perftest-suite RDMA figures for the PMBS26 paper")
    parser.add_argument(
        "--results", default=str(SCRIPT_DIR / "perftest-results"),
        help="campaign tree holding <campaign>/run-*/host/results.csv")
    parser.add_argument(
        "--out", default=str(SCRIPT_DIR / "figs"),
        help="figure output directory")
    parser.add_argument("--format", default="both",
                        choices=["pdf", "png", "both"])
    parser.add_argument("--dpi", type=int, default=200)
    parser.add_argument("--sans", action="store_true",
                        help="keep the sans-serif font (default is the IEEE "
                             "Times-like serif)")
    args = parser.parse_args()

    formats = ["pdf", "png"] if args.format == "both" else [args.format]
    apply_paper_style(args.dpi, serif=not args.sans)
    df, all_rows = load_perftest_results(args.results)
    link_speeds = load_link_speeds(args.results)
    report_coverage(df, all_rows, link_speeds)
    device_rate_gbps = captured_device_rate_gbps(link_speeds)
    print("perftest figures")
    fig_bandwidth(df, args.out, formats, device_rate_gbps)
    fig_latency(df, args.out, formats)
    fig_stability(df, args.out, formats)
    fig_aggregate(df, args.out, formats, link_speeds)
    print("done")


if __name__ == "__main__":
    main()
