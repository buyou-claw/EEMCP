"""Generate a voltage + current plot from a JS220 power profile CSV.

Reads the data.csv produced by `run_js220_power_profile.py --live`
and produces:
  - plot.png : dual-panel time-series (voltage top, current bottom)
  - plot_log.png : current on log scale to show wide dynamic range

Usage (from project root):
    python mcp_js220/scripts/plot_power_profile.py <run_dir>
"""
from __future__ import annotations
import sys
import csv
import argparse
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # non-interactive
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.ticker import EngFormatter


def _load_csv(csv_path: Path) -> dict:
    ts, currents, voltages, powers, current_peaks, voltage_mins = [], [], [], [], [], []
    cum_t = 0.0
    with csv_path.open(encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            dt = float(row["t_s"]) if row["t_s"] else 0.1
            cum_t += dt
            ts.append(cum_t)
            currents.append(float(row["current_avg_A"]) if row["current_avg_A"] else 0.0)
            voltages.append(float(row["voltage_avg_V"]) if row["voltage_avg_V"] else 0.0)
            powers.append(float(row["power_avg_W"]) if row["power_avg_W"] else 0.0)
            current_peaks.append(float(row["current_max_A"]) if row["current_max_A"] else 0.0)
            voltage_mins.append(float(row["voltage_min_V"]) if row["voltage_min_V"] else 0.0)
    return {
        "t_s": ts, "current": currents, "voltage": voltages,
        "power": powers, "current_peak": current_peaks, "voltage_min": voltage_mins,
    }


def _plot_linear(data: dict, png_path: Path, run_label: str) -> None:
    fig, (ax_v, ax_i) = plt.subplots(
        2, 1, figsize=(14, 8), sharex=True,
        gridspec_kw={"height_ratios": [1, 1]},
    )

    # --- Top: Voltage ---
    ax_v.plot(data["t_s"], data["voltage"], color="#1f77b4",
              linewidth=1.0, label="V avg (per 0.1s window)")
    ax_v.plot(data["t_s"], data["voltage_min"], color="#aec7e8",
              linewidth=0.5, alpha=0.6, label="V min (intra-window)")
    ax_v.set_ylabel("Voltage (V)", fontsize=12)
    ax_v.yaxis.set_major_formatter(EngFormatter(unit="V"))
    ax_v.grid(True, alpha=0.3)
    ax_v.legend(loc="upper right", fontsize=9)
    ax_v.set_title(f"{run_label} — Voltage & Current vs Time", fontsize=14, weight="bold")

    # --- Bottom: Current ---
    ax_i.plot(data["t_s"], data["current"], color="#d62728",
              linewidth=1.0, label="I avg (per 0.1s window)")
    ax_i.plot(data["t_s"], data["current_peak"], color="#ff9896",
              linewidth=0.5, alpha=0.5, label="I peak (intra-window)")
    ax_i.set_xlabel("Time (s)", fontsize=12)
    ax_i.set_ylabel("Current (A)", fontsize=12)
    ax_i.yaxis.set_major_formatter(EngFormatter(unit="A"))
    ax_i.grid(True, alpha=0.3)
    ax_i.legend(loc="upper right", fontsize=9)

    plt.tight_layout()
    fig.savefig(png_path, dpi=120, bbox_inches="tight")
    plt.close(fig)


def _plot_log_current(data: dict, png_path: Path, run_label: str) -> None:
    """Current on a log y-axis — the only way to see both µA standby and
    A peaks on the same chart.
    """
    fig, ax = plt.subplots(figsize=(14, 5))
    pos_t = [t for t, c in zip(data["t_s"], data["current"]) if c > 0]
    pos_c = [c for c in data["current"] if c > 0]
    if pos_t:
        ax.plot(pos_t, pos_c, color="#d62728", linewidth=1.0, label="I avg (log scale)")
    pos_peak_t = [t for t, c in zip(data["t_s"], data["current_peak"]) if c > 0]
    pos_peak_c = [c for c in data["current_peak"] if c > 0]
    if pos_peak_t:
        ax.plot(pos_peak_t, pos_peak_c, color="#ff9896", linewidth=0.5,
                alpha=0.6, label="I peak (intra-window)")
    ax.set_yscale("log")
    ax.set_xlabel("Time (s)", fontsize=12)
    ax.set_ylabel("Current (A) — log scale", fontsize=12)
    ax.yaxis.set_major_formatter(EngFormatter(unit="A"))
    ax.grid(True, which="both", alpha=0.3)
    ax.legend(loc="upper right", fontsize=10)
    ax.set_title(f"{run_label} — Current vs Time (log scale shows µA standby and A peaks together)",
                 fontsize=13, weight="bold")
    plt.tight_layout()
    fig.savefig(png_path, dpi=120, bbox_inches="tight")
    plt.close(fig)


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("run_dir", type=str,
                   help="Path to a run directory containing data.csv")
    p.add_argument("--label", type=str, default=None,
                   help="Title prefix (default: <run_dir> name)")
    args = p.parse_args()
    run_dir = Path(args.run_dir)
    csv_path = run_dir / "data.csv"
    if not csv_path.exists():
        sys.exit(f"ERROR: {csv_path} not found")
    label = args.label or run_dir.name

    data = _load_csv(csv_path)
    print(f"Loaded {len(data['t_s'])} samples, total time {data['t_s'][-1]:.1f} s")

    png_linear = run_dir / "plot.png"
    png_log = run_dir / "plot_log.png"
    _plot_linear(data, png_linear, label)
    print(f"Wrote: {png_linear}")
    _plot_log_current(data, png_log, label)
    print(f"Wrote: {png_log}")


if __name__ == "__main__":
    main()
