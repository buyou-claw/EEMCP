r"""5-minute DC voltage log on a Keysight 34465A.

Uses the mcp_34465a package for connection & SCPI plumbing, then
optimizes the per-sample path for max throughput:
  - Sets NPLC=0.001 (aperture mode, fastest)
  - Disables auto-zero (no AZ cycle between reads)
  - Loops with :MEAS:VOLT:DC? (configure-once-already, trigger+fetch)

Outputs to e:\Centrak\BLE Sticker\test\HWTEST\reports\:
  - dmm_voltage_log_<timestamp>.csv  (raw samples)
  - dmm_voltage_log_<timestamp>.json  (summary stats)
  - dmm_voltage_log_<timestamp>.md    (markdown report)
  - dmm_voltage_log_<timestamp>.png   (time-series plot)
"""
from __future__ import annotations
import csv
import json
import os
import signal
import sys
import time
import statistics
from datetime import datetime
from pathlib import Path

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
DURATION_S = 300.0         # 5 minutes
EXPECTED_NOMINAL_V = 3.0   # for the report; not enforced
EXPECTED_TOLERANCE_V = 0.1 # ± window for pass/fail annotation

REPORTS_ROOT = Path(r"e:\Centrak\BLE Sticker\test\HWTEST\reports")
REPORTS_ROOT.mkdir(parents=True, exist_ok=True)

# Use a high-resolution ISO timestamp that's filename-safe on Windows.
STAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
RUN_LABEL = f"dmm_voltage_log_{STAMP}"

# Each test run goes into its own subfolder so multiple runs (and other
# test types later on) don't clutter reports/. File names are short —
# the timestamp lives in the folder name.
RUN_DIR = REPORTS_ROOT / RUN_LABEL
RUN_DIR.mkdir(parents=True, exist_ok=True)

# Short, stable file names inside the run folder.
CSV_PATH   = RUN_DIR / "data.csv"
JSON_PATH  = RUN_DIR / "summary.json"
MD_PATH    = RUN_DIR / "report.md"
PNG_PATH   = RUN_DIR / "plot.png"
STOP_FILE  = RUN_DIR / ".stop"  # touch this file to request a clean stop


# ----------------------------------------------------------------------------
# Stop signal handling
# ----------------------------------------------------------------------------
class _StopFlag:
    """Thread-/signal-safe stop indicator (also checks STOP_FILE)."""
    def __init__(self) -> None:
        self._stop = False

    def request(self, *_) -> None:
        self._stop = True

    @property
    def stop(self) -> bool:
        return self._stop or STOP_FILE.exists()


stop_flag = _StopFlag()

# Best-effort signal wiring. On Windows, SIGTERM may not be delivered to
# Python processes, but TaskStop usually does close stdin / kill — the
# stop file check covers that path.
for _sig in (signal.SIGINT, signal.SIGTERM):
    try:
        signal.signal(_sig, stop_flag.request)
    except (ValueError, OSError):
        # Not in main thread, or platform-specific limitation — ignore.
        pass


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def configure_for_speed(disable_display: bool = False) -> None:
    """Set the 34465A to its fastest DC voltage configuration.

    - NPLC=0.001   : aperture mode, ~20 µs integration, fastest
    - AZ OFF       : skip auto-zero cycle between reads (saves ~ms each)
    - DISP OFF     : OPTIONAL, default OFF — turning off the display saves
                     bus time but blanks the front panel; not worth it on
                     USB (~2% throughput gain) and confuses bench users.

    After this returns, use :READ? for the high-rate loop (NOT
    :MEAS:VOLT:DC?, which re-sends the configuration on every call and
    drops throughput from ~228 Hz to ~2 Hz).
    """
    dmm.write(scpi.conf("VOLT:DC"))
    dmm.write(scpi.set_nplc("VOLT:DC", 0.001))
    dmm.write(scpi.set_auto_zero("VOLT", "OFF"))
    if disable_display:
        dmm.write(":DISP:STAT OFF")
    # Warm-up read so the first :READ? isn't penalized for first-call setup.
    dmm.query(":READ?")


def acquire_loop(duration_s: float):
    """Yield (t_rel_s, voltage_V) samples for `duration_s` seconds.

    Uses :READ? (trigger + fetch on the already-configured function)
    instead of :MEAS:VOLT:DC? to avoid per-call reconfiguration.
    Honors three stop mechanisms: deadline reached, `touch <run_dir>/.stop`,
    or Ctrl+C / SIGTERM (best-effort, OS-dependent on Windows).
    """
    t_start = time.monotonic()
    deadline = t_start + duration_s
    n = 0
    try:
        while True:
            if stop_flag.stop:
                break
            now = time.monotonic()
            if now >= deadline:
                break
            try:
                v = float(dmm.query(":READ?"))
            except (pyvisa.VisaIOError, ValueError) as e:
                # Don't kill the whole run on a single glitch; record and continue.
                print(f"[warn] sample {n} failed: {e}", file=sys.stderr)
                continue
            yield (now - t_start, v)
            n += 1
    except KeyboardInterrupt:
        stop_flag.request()


def summarize(samples):
    """Compute summary stats from [(t, v), ...]."""
    if not samples:
        return {}
    vs = [v for _, v in samples]
    ts = [t for t, _ in samples]
    span = ts[-1] - ts[0] if len(ts) > 1 else 0.0
    n = len(vs)
    mean = statistics.fmean(vs)
    median = statistics.median(vs)
    stdev = statistics.pstdev(vs) if n > 1 else 0.0
    vmin, vmax = min(vs), max(vs)
    # Worst deviations from nominal, useful for "is it within spec"
    devs = [v - EXPECTED_NOMINAL_V for v in vs]
    abs_dev = max(abs(d) for d in devs)
    return {
        "samples":            n,
        "duration_s":         round(span, 3),
        "rate_hz":            round(n / span, 2) if span > 0 else 0.0,
        "min_v":              vmin,
        "max_v":              vmax,
        "mean_v":             mean,
        "median_v":           median,
        "stdev_v":            stdev,
        "peak_to_peak_v":     vmax - vmin,
        "nominal_v":          EXPECTED_NOMINAL_V,
        "max_abs_deviation_v": abs_dev,
        "tolerance_v":        EXPECTED_TOLERANCE_V,
        "within_tolerance":   abs_dev <= EXPECTED_TOLERANCE_V,
    }


def write_csv(path: Path, samples) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["t_s", "voltage_v", "iso_time"])
        # Record wall-clock iso time relative to start.
        run_start_iso = datetime.now().isoformat(timespec="seconds")
        for t, v in samples:
            w.writerow([f"{t:.4f}", f"{v:.9f}", run_start_iso])


def write_json(path: Path, meta: dict, summary: dict) -> None:
    out = {"meta": meta, "summary": summary}
    with path.open("w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)


def write_markdown(path: Path, meta: dict, summary: dict) -> None:
    spec = EXPECTED_NOMINAL_V
    tol = EXPECTED_TOLERANCE_V
    p2p = summary["peak_to_peak_v"]
    p2p_pct = (p2p / spec) * 100.0 if spec else 0.0
    ptt = "✅" if summary["within_tolerance"] else "❌"

    md = f"""# 34465A DC Voltage Log

| Item | Value |
|---|---|
| Run label | `{meta['run_label']}` |
| Start | {meta['start_iso']} |
| End | {meta['end_iso']} |
| Instrument | {meta['idn']} |
| Resource | `{meta['resource']}` |
| Function | VOLT:DC (NPLC=0.001, AZ OFF) |
| Planned duration | {DURATION_S:.1f} s |
| Actual samples | **{summary['samples']}** |
| Sample rate | {summary['rate_hz']:.2f} Hz |
| Mean | **{summary['mean_v']:.6f} V** |
| Median | {summary['median_v']:.6f} V |
| Min | {summary['min_v']:.6f} V |
| Max | {summary['max_v']:.6f} V |
| Peak-to-peak | {summary['peak_to_peak_v']:.6f} V ({p2p_pct:.3f}% of nominal) |
| Std-dev | {summary['stdev_v']*1e3:.3f} mV |
| Nominal target | {spec:.2f} V ± {tol:.2f} V |
| Max abs deviation | {summary['max_abs_deviation_v']:.6f} V |
| Within tolerance | {ptt} |

## Artifacts

- `data.csv` — raw samples (t_s, voltage_v)
- `plot.png` — time-series plot
- `summary.json` — machine-readable summary
"""
    with path.open("w", encoding="utf-8") as f:
        f.write(md)


def write_plot(path: Path, samples, summary: dict) -> bool:
    try:
        import matplotlib
        matplotlib.use("Agg")  # headless
        import matplotlib.pyplot as plt
    except ImportError:
        return False
    ts = [t for t, _ in samples]
    vs = [v for _, v in samples]
    # NOTE: do NOT share x between the two subplots — the top is time
    # (0..duration) and the bottom is a voltage histogram. Sharing x
    # would force the histogram onto a 0..duration time axis and
    # collapse the bars to an invisible sliver.
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 7),
                                    gridspec_kw={"height_ratios": [3, 1]})
    # Top: voltage vs time
    ax1.plot(ts, vs, ".", markersize=1.0, color="#1f77b4", alpha=0.6,
             label="V (raw)")
    ax1.axhline(summary["mean_v"], color="#2ca02c", linewidth=1.2,
                label=f"mean = {summary['mean_v']:.4f} V")
    ax1.axhline(EXPECTED_NOMINAL_V + EXPECTED_TOLERANCE_V, color="#ff7f0e",
                linestyle="--", linewidth=1.0,
                label=f"nominal +{EXPECTED_TOLERANCE_V:.2f} V")
    ax1.axhline(EXPECTED_NOMINAL_V - EXPECTED_TOLERANCE_V, color="#ff7f0e",
                linestyle="--", linewidth=1.0,
                label=f"nominal -{EXPECTED_TOLERANCE_V:.2f} V")
    ax1.set_xlabel("Time (s)")
    ax1.set_ylabel("Voltage (V)")
    ax1.set_title(f"34465A DC Voltage Log — {summary['samples']} samples "
                  f"@ {summary['rate_hz']:.1f} Hz")
    ax1.legend(loc="upper right", fontsize=8)
    ax1.grid(True, alpha=0.3)
    # Bottom: histogram of voltage readings (own x-axis in V).
    # Pad the x-range slightly so the side bars are not flush to the edge.
    vspan = max(summary["peak_to_peak_v"] * 0.2, 0.0001)
    ax2.hist(vs, bins=60, color="#1f77b4", edgecolor="white", alpha=0.85)
    ax2.set_xlim(summary["min_v"] - vspan, summary["max_v"] + vspan)
    ax2.axvline(summary["mean_v"], color="#2ca02c", linewidth=1.2,
                label=f"mean = {summary['mean_v']:.4f} V")
    ax2.axvline(EXPECTED_NOMINAL_V, color="#ff7f0e", linestyle="--",
                linewidth=1.0, label=f"nominal {EXPECTED_NOMINAL_V:.2f} V")
    ax2.set_xlabel("Voltage (V)")
    ax2.set_ylabel("Count")
    ax2.set_title("Distribution")
    ax2.legend(loc="upper right", fontsize=8)
    ax2.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return True


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main() -> int:
    print(f"[{RUN_LABEL}] connecting to 34465A …")
    idn = dmm.connect()
    resource = dmm.resource
    print(f"[{RUN_LABEL}] connected: {idn}")
    print(f"[{RUN_LABEL}] configuring for max sample rate …")
    configure_for_speed(disable_display=False)

    start_iso = datetime.now().isoformat(timespec="seconds")
    print(f"[{RUN_LABEL}] acquiring {DURATION_S:.0f} s of DC voltage @ 3 V …")
    try:
        samples = list(acquire_loop(DURATION_S))
    finally:
        # Make sure the front panel is left in a normal state — display on,
        # no leftover text. We do not write any "LOGGING" / "DONE" text to
        # the display; the front panel should keep showing live voltage.
        try:
            dmm.write(":DISP:TEXT:CLE")
            dmm.write(":DISP:STAT ON")
        except Exception:
            pass
    end_iso = datetime.now().isoformat(timespec="seconds")

    if not samples:
        print("ERROR: no samples collected", file=sys.stderr)
        return 2

    summary = summarize(samples)

    # Write all artifacts into the run subfolder.
    write_csv(CSV_PATH, samples)
    meta = {
        "run_label": RUN_LABEL,
        "start_iso": start_iso,
        "end_iso":   end_iso,
        "idn":       idn,
        "resource":  resource,
        "function":  "VOLT:DC",
        "nplc":      0.001,
        "az":        "OFF",
        "duration_planned_s": DURATION_S,
    }
    write_json(JSON_PATH, meta, summary)
    write_markdown(MD_PATH, meta, summary)
    plot_ok = write_plot(PNG_PATH, samples, summary)

    print()
    print(f"  samples     : {summary['samples']}")
    print(f"  duration    : {summary['duration_s']:.2f} s")
    print(f"  rate        : {summary['rate_hz']:.2f} Hz")
    print(f"  mean        : {summary['mean_v']:.6f} V")
    print(f"  min / max   : {summary['min_v']:.6f} / {summary['max_v']:.6f} V")
    print(f"  peak-peak   : {summary['peak_to_peak_v']:.6f} V")
    print(f"  stdev       : {summary['stdev_v']*1e3:.3f} mV")
    print(f"  within tol  : {'YES' if summary['within_tolerance'] else 'NO'}")
    print()
    print(f"  -> {MD_PATH}")
    print(f"  -> {CSV_PATH}")
    print(f"  -> {JSON_PATH}")
    print(f"  -> {PNG_PATH}{'' if plot_ok else '  (skipped: no matplotlib)'}")

    dmm.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
