r"""Live-stop DC current log on a Keysight 34465A.

Designed for hands-on bench work: the user does operations on a DUT
while the script keeps acquiring. It can be stopped at any time by:
  - Pressing Ctrl+C
  - The Claude `TaskStop` tool (which sends SIGTERM / process kill)
  - Creating a stop file: `touch <RUN_DIR>/.stop`

Live-safety: samples are flushed to disk one at a time, so even a hard
kill never loses more than the in-flight sample. A summary is rewritten
every N samples. On a clean exit (KeyboardInterrupt / SIGTERM handled),
the report.md and plot.png are also written.

Outputs to e:\Centrak\BLE Sticker\test\HWTEST\reports\<run_label>\:
  - data.csv          (raw samples, t_s, current_a, iso_time)
  - summary.json      (rewritten every 100 samples)
  - report.md         (markdown report, written on clean exit)
  - plot.png          (time-series + log histogram, written on clean exit)

Important caveats for current measurement on the 34465A:
  - Burden voltage varies with range. At the 1 µA range the shunt is
    ~50 kΩ, so 100 µA would drop 5 V (overrange). Auto-range keeps the
    burden reasonable, but if your IoT device is sensitive to a few
    hundred mV of supply droop, consider an external current sense amp.
  - Auto-range causes a brief ~1 ms settle gap when range changes; the
    time series will show this as a short flatline.
  - A reading of 9.9e+37 means the input is overrange (the lead is on a
    range too small for the actual current). Treat as "out of range".
"""
from __future__ import annotations
import csv
import json
import math
import os
import signal
import sys
import time
import statistics
from datetime import datetime
from pathlib import Path

# Make prints (µ, Ω, etc.) survive Windows consoles whose default codec
# is gbk / cp936. Has no effect when stdout already speaks utf-8.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# Make the project root importable so this script works whether it's
# invoked from the project root or from inside mcp_34465a/scripts/.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pyvisa

# Reuse the MCP package for connection / SCPI / config.
from mcp_34465a.server import dmm
from mcp_34465a import scpi


# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
REPORTS_ROOT = Path(r"e:\Centrak\BLE Sticker\test\HWTEST\reports")
REPORTS_ROOT.mkdir(parents=True, exist_ok=True)

STAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
RUN_LABEL = f"dmm_current_log_{STAMP}"

RUN_DIR      = REPORTS_ROOT / RUN_LABEL
RUN_DIR.mkdir(parents=True, exist_ok=True)
CSV_PATH     = RUN_DIR / "data.csv"
JSON_PATH    = RUN_DIR / "summary.json"
MD_PATH      = RUN_DIR / "report.md"
PNG_PATH     = RUN_DIR / "plot.png"
STOP_FILE    = RUN_DIR / ".stop"

# How often to rewrite the in-progress summary.json
SUMMARY_EVERY_N = 100

# IoT-friendly current "levels" — used for the breakdown in the report.
# (label, lower_A_exclusive, upper_A_inclusive)
CURRENT_LEVELS = [
    ("deep_sleep",   0.0,           100e-6),   # 0–100 µA
    ("light_sleep",  100e-6,        1e-3),     # 100 µA – 1 mA
    ("idle",         1e-3,          10e-3),    # 1–10 mA
    ("active",       10e-3,         100e-3),   # 10–100 mA
    ("tx_burst",     100e-3,        3.0),      # 100 mA – 3 A
]

OVERRANGE_VALUE = 9.9e37


# ----------------------------------------------------------------------------
# Stop signal handling
# ----------------------------------------------------------------------------
class StopFlag:
    """Thread-/signal-safe stop indicator."""
    def __init__(self) -> None:
        self._stop = False

    def request(self, *_) -> None:
        self._stop = True

    @property
    def stop(self) -> bool:
        return self._stop or STOP_FILE.exists()


stop_flag = StopFlag()

# Best-effort signal wiring. On Windows, SIGTERM may not be delivered to
# Python processes, but TaskStop usually does close stdin / kill — the
# stop file check covers that path.
for sig in (signal.SIGINT, signal.SIGTERM):
    try:
        signal.signal(sig, stop_flag.request)
    except (ValueError, OSError):
        # Not in main thread, or platform-specific limitation — ignore.
        pass


# ----------------------------------------------------------------------------
# Acquisition
# ----------------------------------------------------------------------------
def configure_for_high_speed() -> None:
    """Set the 34465A to its fastest DC current configuration.

    Auto-range is kept on because IoT current typically spans µA to
    hundreds of mA. Note the per-range burden voltage caveat above.
    """
    dmm.write(scpi.conf("CURR:DC"))
    dmm.write(scpi.set_nplc("CURR:DC", 0.001))
    dmm.write(scpi.set_auto_zero("CURR", "OFF"))
    dmm.write(":DISP:STAT ON")  # keep the front panel alive
    # Warm-up read — first :READ? after config is slower.
    dmm.query(":READ?")


def classify_current(a: float) -> str:
    if a >= OVERRANGE_VALUE / 2:
        return "overrange"
    for label, lo, hi in CURRENT_LEVELS:
        if lo <= a < hi:
            return label
    return "out_of_band"


def acquire_loop(csv_file, csv_writer):
    """Acquire until stop is requested. Yields nothing — writes directly."""
    t_start = time.monotonic()
    n = 0
    last_summary_n = 0
    samples = []  # in-memory for incremental summary

    print(f"[{RUN_LABEL}] acquiring — stop with Ctrl+C, SIGTERM, or "
          f"touch {STOP_FILE}", flush=True)
    print(f"[{RUN_LABEL}] live data: {CSV_PATH}", flush=True)
    print(f"[{RUN_LABEL}] live summary: {JSON_PATH} (updated every "
          f"{SUMMARY_EVERY_N} samples)", flush=True)

    try:
        while not stop_flag.stop:
            try:
                v = float(dmm.query(":READ?"))
            except (pyvisa.VisaIOError, ValueError) as e:
                print(f"[warn] sample {n} failed: {e}", file=sys.stderr,
                      flush=True)
                continue
            t_rel = time.monotonic() - t_start
            iso = datetime.now().isoformat(timespec="milliseconds")
            csv_writer.writerow([f"{t_rel:.4f}", f"{v:.9e}", iso])
            csv_file.flush()
            os.fsync(csv_file.fileno())  # survive hard kill
            samples.append((t_rel, v))
            n += 1

            if n - last_summary_n >= SUMMARY_EVERY_N:
                _write_summary_incremental(samples, t_rel, n, final=False)
                last_summary_n = n
    except KeyboardInterrupt:
        stop_flag.request()

    end_t = samples[-1][0] if samples else 0.0
    return samples, end_t


# ----------------------------------------------------------------------------
# Summary / report
# ----------------------------------------------------------------------------
def _band_breakdown(samples) -> dict:
    """Time spent and per-band mean current for each CURRENT_LEVELS band."""
    if not samples:
        return {}
    bands = {label: {"samples": 0, "sum_a": 0.0} for label, _, _ in CURRENT_LEVELS}
    bands["overrange"] = {"samples": 0, "sum_a": 0.0}
    out = {}
    for label, lo, hi in CURRENT_LEVELS:
        s = [v for _, v in samples if lo <= v < hi]
        out[label] = {
            "n": len(s),
            "mean_a": statistics.fmean(s) if s else None,
            "min_a": min(s) if s else None,
            "max_a": max(s) if s else None,
        }
    over = [v for _, v in samples if v >= OVERRANGE_VALUE / 2]
    out["overrange"] = {
        "n": len(over),
        "mean_a": None,
        "min_a": None,
        "max_a": None,
    }
    return out


def _summarize(samples) -> dict:
    if not samples:
        return {"samples": 0}
    vs = [v for _, v in samples]
    ts = [t for t, _ in samples]
    span = ts[-1] - ts[0] if len(ts) > 1 else 0.0
    valid = [v for v in vs if v < OVERRANGE_VALUE / 2]
    return {
        "samples":      len(samples),
        "valid_samples": len(valid),
        "overrange_samples": len(vs) - len(valid),
        "duration_s":   round(span, 3),
        "rate_hz":      round(len(samples) / span, 2) if span > 0 else 0.0,
        "min_a":        min(vs),
        "max_a":        max(vs),
        "mean_a":       statistics.fmean(valid) if valid else None,
        "median_a":     statistics.median(valid) if valid else None,
        "stdev_a":      statistics.pstdev(valid) if len(valid) > 1 else 0.0,
        "charge_c":     (statistics.fsum(valid) * (span / len(valid)))
                        if valid and span > 0 else 0.0,
        "bands":        _band_breakdown(samples),
    }


def _write_summary_incremental(samples, t_last, n, final: bool) -> None:
    summary = _summarize(samples)
    summary["final"] = final
    summary["t_last_s"] = t_last
    summary["n"] = n
    try:
        with JSON_PATH.open("w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)
    except OSError:
        pass


def _write_markdown(meta: dict, summary: dict) -> None:
    ptt_bands = []
    for label, _, _ in CURRENT_LEVELS:
        b = summary["bands"].get(label, {})
        if b.get("n", 0) > 0:
            mean = b.get("mean_a")
            mean_str = f"{mean*1e3:.3f} mA" if mean and mean >= 1e-3 \
                       else f"{mean*1e6:.3f} µA" if mean else "n/a"
            ptt_bands.append(
                f"| `{label}` | {b['n']} | {mean_str} | "
                f"{(b['min_a'] or 0)*1e6:.1f} µA | "
                f"{(b['max_a'] or 0)*1e3:.3f} mA |"
            )

    md = f"""# 34465A DC Current Log

| Item | Value |
|---|---|
| Run label | `{meta['run_label']}` |
| Start | {meta['start_iso']} |
| End | {meta['end_iso']} |
| Instrument | {meta['idn']} |
| Resource | `{meta['resource']}` |
| Function | CURR:DC (NPLC=0.001, AZ OFF, auto-range) |
| Samples | **{summary['samples']}** ({summary['valid_samples']} valid, {summary['overrange_samples']} overrange) |
| Duration | {summary['duration_s']:.2f} s |
| Sample rate | {summary['rate_hz']:.2f} Hz |
| Min / Max | {summary['min_a']*1e6:.3f} µA / {summary['max_a']*1e3:.3f} mA |
| Mean | {summary['mean_a']*1e3:.3f} mA |
| Median | {summary['median_a']*1e3:.3f} mA |
| Std-dev | {summary['stdev_a']*1e3:.3f} mA |
| Total charge | {summary['charge_c']*1e3:.3f} mC |
| Stopped by | {meta['stopped_by']} |

## Current-level breakdown (auto-range, 3A input)

| Level | Samples | Mean | Min | Max |
|---|---|---|---|---|
{chr(10).join(ptt_bands) if ptt_bands else '| (no data) | | | | |'}

> Levels: deep_sleep (< 100 µA) / light_sleep (100 µA – 1 mA) /
> idle (1–10 mA) / active (10–100 mA) / tx_burst (100 mA – 3 A).
> Use these as a starting point — adjust `CURRENT_LEVELS` in the script
> for your DUT's specific operating states.

## Artifacts

- `data.csv` — raw samples (t_s, current_a, iso_time)
- `summary.json` — machine-readable summary (rewritten every 100 samples)
- `plot.png` — time-series + log-scale histogram

## Notes

- Auto-range briefly settles ~1 ms when switching between µA / mA / A
  ranges; the time series will show a short flatline at these points.
- Burden voltage scales with range: 1 µA range ≈ 50 kΩ shunt, 100 mA
  range ≈ 5 Ω shunt. For a low-voltage IoT rail this can be
  significant — see the report's burden-aware analysis if added.
"""
    MD_PATH.write_text(md, encoding="utf-8")


def _write_plot(samples, summary: dict) -> bool:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return False

    ts = [t for t, _ in samples]
    vs = [v for _, v in samples]
    valid_vs = [v for v in vs if v < OVERRANGE_VALUE / 2]

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 7),
                                    gridspec_kw={"height_ratios": [3, 1]})

    # Top: linear time series (clipped at 3 A so spikes don't flatten the plot)
    clip = min(3.0, summary["max_a"])
    plot_vs = [min(v, clip) for v in vs]
    ax1.plot(ts, plot_vs, ".", markersize=1.0, color="#1f77b4", alpha=0.5,
             label="current (clipped to 3 A for plot)")
    # Per-band reference lines
    band_colors = {"deep_sleep": "#999999", "light_sleep": "#a6cee3",
                   "idle": "#1f78b4", "active": "#33a02c",
                   "tx_burst": "#e31a1c"}
    for label, lo, hi in CURRENT_LEVELS:
        ax1.axhspan(lo, hi, alpha=0.06, color=band_colors.get(label, "#cccccc"),
                    label=label)
    ax1.set_yscale("log")
    ax1.set_xlabel("Time (s)")
    ax1.set_ylabel("Current (A)")
    ax1.set_title(f"34465A DC Current Log — {summary['samples']} samples "
                  f"@ {summary['rate_hz']:.1f} Hz, log scale")
    ax1.legend(loc="upper right", fontsize=7, ncol=2)
    ax1.grid(True, alpha=0.3, which="both")
    ax1.set_ylim(1e-7, 5)  # floor at 100 nA, ceiling 5 A

    # Bottom: log-scale histogram
    if valid_vs:
        # Plot on a log axis — use log-spaced bins
        ax2.hist(valid_vs, bins=80, color="#1f77b4",
                 edgecolor="white", alpha=0.85)
        ax2.set_xscale("log")
        ax2.set_xlabel("Current (A, log)")
    else:
        ax2.text(0.5, 0.5, "no valid samples", ha="center", va="center",
                 transform=ax2.transAxes)
    ax2.set_ylabel("Count")
    ax2.set_title("Distribution (log bins)")
    ax2.grid(True, alpha=0.3, which="both")

    fig.tight_layout()
    fig.savefig(PNG_PATH, dpi=130)
    plt.close(fig)
    return True


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main() -> int:
    print(f"[{RUN_LABEL}] connecting to 34465A …", flush=True)
    idn = dmm.connect()
    resource = dmm.resource
    print(f"[{RUN_LABEL}] connected: {idn}", flush=True)
    print(f"[{RUN_LABEL}] configuring for high-speed CURR:DC, auto-range …",
          flush=True)
    configure_for_high_speed()

    start_iso = datetime.now().isoformat(timespec="seconds")
    with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["t_s", "current_a", "iso_time"])
        try:
            samples, end_t = acquire_loop(f, w)
        except Exception:
            samples, end_t = [], 0.0
            raise
    end_iso = datetime.now().isoformat(timespec="seconds")

    stopped_by = (
        "stop_file" if STOP_FILE.exists() else "signal/keyboard"
    )

    summary = _summarize(samples)
    meta = {
        "run_label": RUN_LABEL,
        "start_iso": start_iso,
        "end_iso":   end_iso,
        "idn":       idn,
        "resource":  resource,
        "function":  "CURR:DC",
        "nplc":      0.001,
        "az":        "OFF",
        "range_mode": "auto",
        "stopped_by": stopped_by,
    }

    # Final summary
    _write_summary_incremental(samples, end_t, len(samples), final=True)

    # Cleanly close out — try to write the report even on partial data.
    try:
        dmm.write(":DISP:STAT ON")
    except Exception:
        pass

    if samples:
        try:
            _write_markdown(meta, summary)
            print(f"[{RUN_LABEL}] wrote {MD_PATH.name}", flush=True)
        except Exception as e:
            print(f"[warn] markdown write failed: {e}", file=sys.stderr,
                  flush=True)
        try:
            ok = _write_plot(samples, summary)
            print(f"[{RUN_LABEL}] wrote {PNG_PATH.name}"
                  f"{'' if ok else ' (skipped: no matplotlib)'}", flush=True)
        except Exception as e:
            print(f"[warn] plot write failed: {e}", file=sys.stderr,
                  flush=True)
    else:
        print(f"[{RUN_LABEL}] no samples collected; skipping report",
              flush=True)

    dmm.disconnect()
    print()
    print(f"  samples     : {summary.get('samples', 0)}")
    print(f"  valid       : {summary.get('valid_samples', 0)}")
    print(f"  overrange   : {summary.get('overrange_samples', 0)}")
    print(f"  duration    : {summary.get('duration_s', 0):.2f} s")
    print(f"  rate        : {summary.get('rate_hz', 0):.2f} Hz")
    print(f"  min / max   : {(summary.get('min_a') or 0)*1e6:.3f} µA / "
          f"{(summary.get('max_a') or 0)*1e3:.3f} mA")
    print(f"  charge      : {(summary.get('charge_c') or 0)*1e3:.3f} mC")
    print()
    print(f"  -> {CSV_PATH}")
    print(f"  -> {JSON_PATH}")
    print(f"  -> {MD_PATH}  (if samples > 0)")
    print(f"  -> {PNG_PATH}  (if samples > 0)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
