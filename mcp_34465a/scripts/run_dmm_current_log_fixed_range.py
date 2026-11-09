r"""Same shape as run_dmm_current_log.py, but locked to a fixed 100 mA range
on the 3 A terminal so auto-range never interrupts the trace.

The 100 mA range uses a ~5 ohm shunt; at the 7.8 mA peak seen during the
auto-range capture that is ~39 mV of supply droop — acceptable for this
PCBA (3.3 V rail). If the DUT pulls > 100 mA, the DMM will return 9.9e37
("overrange") and we just keep logging.

Stop mechanisms: Ctrl+C, SIGTERM/TaskStop, or `touch <RUN_DIR>/.stop`.
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

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pyvisa

from mcp_34465a.server import dmm
from mcp_34465a import scpi

REPORTS_ROOT = Path(os.environ.get(
    "DMM_CURRENT_REPORTS_DIR",
    r"e:\Centrak\BLE Sticker\test\HWTEST\reports",
))
# Raw CSV goes to a sibling `data/` dir at the same cycle level — per
# REPORT_SPEC.md §1 (data/ for raw per-model CSVs, reports/ for summaries).
# Override with DMM_CURRENT_DATA_DIR when a non-default split is needed.
DATA_DIR = Path(os.environ.get(
    "DMM_CURRENT_DATA_DIR",
    str(REPORTS_ROOT.parent / "data"),
))
REPORTS_ROOT.mkdir(parents=True, exist_ok=True)
DATA_DIR.mkdir(parents=True, exist_ok=True)

STAMP = datetime.now().strftime("%Y%m%d_%H%M%S")


def _parse_args():
    import argparse
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--range", dest="range_a", type=float, default=0.1,
                   help="fixed CURR:DC range in amperes (e.g. 100e-6, 10e-3)")
    p.add_argument("--label", default="",
                   help="suffix for the run directory, e.g. sleep / active")
    p.add_argument("--nplc", type=float, default=0.001)
    return p.parse_args()


_ARGS = _parse_args()
FIXED_RANGE_A = _ARGS.range_a
NPLC = _ARGS.nplc
_SUFFIX = f"_{_ARGS.label}" if _ARGS.label else ""

RUN_LABEL = f"dmm_current_log_fixed{_SUFFIX}_{STAMP}"
RUN_DIR = REPORTS_ROOT / RUN_LABEL
RUN_DIR.mkdir(parents=True, exist_ok=True)
CSV_PATH = DATA_DIR / f"{RUN_LABEL}.csv"
JSON_PATH = RUN_DIR / "summary.json"
MD_PATH = RUN_DIR / "report.md"
PNG_PATH = RUN_DIR / "plot.png"
STOP_FILE = RUN_DIR / ".stop"

SUMMARY_EVERY_N = 100
OVERRANGE_VALUE = 9.9e37


class StopFlag:
    def __init__(self) -> None:
        self._stop = False

    def request(self, *_) -> None:
        self._stop = True

    @property
    def stop(self) -> bool:
        return self._stop or STOP_FILE.exists()


stop_flag = StopFlag()
for sig in (signal.SIGINT, signal.SIGTERM):
    try:
        signal.signal(sig, stop_flag.request)
    except (ValueError, OSError):
        pass


def configure_for_fixed_range() -> None:
    dmm.write(scpi.conf_with_range("CURR:DC", FIXED_RANGE_A))
    dmm.write(scpi.set_nplc("CURR:DC", NPLC))
    dmm.write(scpi.set_auto_zero("CURR", "OFF"))
    dmm.write(":DISP:STAT ON")
    dmm.query(":READ?")  # warm-up read
    actual = dmm.query(":CURR:DC:RANG?")
    auto = dmm.query(":CURR:DC:RANG:AUTO?")
    print(f"[{RUN_LABEL}] range = {actual} A, autorange = {auto}",
          flush=True)


def acquire_loop(csv_file, csv_writer):
    t_start = time.monotonic()
    n = 0
    last_summary_n = 0
    samples = []
    print(f"[{RUN_LABEL}] acquiring — stop with Ctrl+C, SIGTERM, or "
          f"touch {STOP_FILE}", flush=True)
    print(f"[{RUN_LABEL}] live data: {CSV_PATH}", flush=True)
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
            os.fsync(csv_file.fileno())
            samples.append((t_rel, v))
            n += 1
            if n - last_summary_n >= SUMMARY_EVERY_N:
                _write_summary_incremental(samples, t_rel, n, final=False)
                last_summary_n = n
    except KeyboardInterrupt:
        stop_flag.request()
    end_t = samples[-1][0] if samples else 0.0
    return samples, end_t


def _band_breakdown(samples) -> dict:
    bands = {
        "deep_sleep":  (0.0, 100e-6),
        "light_sleep": (100e-6, 1e-3),
        "idle":        (1e-3, 10e-3),
        "active":      (10e-3, 100e-3),
    }
    out = {}
    for label, (lo, hi) in bands.items():
        s = [v for _, v in samples if lo <= v < hi]
        out[label] = {
            "n": len(s),
            "mean_a": statistics.fmean(s) if s else None,
            "min_a": min(s) if s else None,
            "max_a": max(s) if s else None,
        }
    over = [v for _, v in samples if v >= OVERRANGE_VALUE / 2]
    out["overrange"] = {"n": len(over), "mean_a": None,
                        "min_a": None, "max_a": None}
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
    summary["fixed_range_a"] = FIXED_RANGE_A
    try:
        with JSON_PATH.open("w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False)
    except OSError:
        pass


def _write_markdown(meta: dict, summary: dict) -> None:
    bands = summary["bands"]
    ptt_bands = []
    for label in ("deep_sleep", "light_sleep", "idle", "active"):
        b = bands.get(label, {})
        if b.get("n", 0) > 0:
            mean = b.get("mean_a")
            mean_str = (f"{mean*1e3:.3f} mA" if mean and mean >= 1e-3
                        else f"{mean*1e6:.3f} µA" if mean else "n/a")
            ptt_bands.append(
                f"| `{label}` | {b['n']} | {mean_str} | "
                f"{(b['min_a'] or 0)*1e6:.1f} µA | "
                f"{(b['max_a'] or 0)*1e3:.3f} mA |"
            )

    md = f"""# 34465A DC Current Log (fixed {FIXED_RANGE_A*1e3:g} mA range)

| Item | Value |
|---|---|
| Run label | `{meta['run_label']}` |
| Start | {meta['start_iso']} |
| End | {meta['end_iso']} |
| Instrument | {meta['idn']} |
| Resource | `{meta['resource']}` |
| Function | CURR:DC (NPLC={NPLC:g}, AZ OFF, **fixed {FIXED_RANGE_A*1e3:g} mA range**) |
| Samples | **{summary['samples']}** ({summary['valid_samples']} valid, {summary['overrange_samples']} overrange) |
| Duration | {summary['duration_s']:.2f} s |
| Sample rate | {summary['rate_hz']:.2f} Hz |
| Min / Max | {summary['min_a']*1e6:.3f} µA / {summary['max_a']*1e3:.3f} mA |
| Mean | {summary['mean_a']*1e3:.3f} mA |
| Median | {summary['median_a']*1e3:.3f} mA |
| Std-dev | {summary['stdev_a']*1e3:.3f} mA |
| Total charge | {summary['charge_c']*1e3:.3f} mC |
| Stopped by | {meta['stopped_by']} |

## Current-level breakdown (100 mA fixed range)

| Level | Samples | Mean | Min | Max |
|---|---|---|---|---|
{chr(10).join(ptt_bands) if ptt_bands else '| (no data) | | | | |'}

> 100 mA fixed range on the 3 A terminal; auto-range is OFF.
> Auto-range caused range-change gaps in the prior run — this script is
> used so the trace stays continuous across pull-tab transients.
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
    plot_vs = [min(v, FIXED_RANGE_A * 1.2) for v in vs]
    ax1.plot(ts, plot_vs, ".", markersize=1.0, color="#1f77b4", alpha=0.5)
    ax1.set_yscale("log")
    ax1.set_xlabel("Time (s)")
    ax1.set_ylabel("Current (A)")
    ax1.set_title(f"34465A DC Current (fixed {FIXED_RANGE_A*1e3:g} mA) — "
                  f"{summary['samples']} samples @ "
                  f"{summary['rate_hz']:.1f} Hz, log scale")
    ax1.grid(True, alpha=0.3, which="both")
    ax1.set_ylim(1e-8, FIXED_RANGE_A * 2)
    if valid_vs:
        ax2.hist(valid_vs, bins=80, color="#1f77b4", edgecolor="white",
                 alpha=0.85)
        ax2.set_xscale("log")
        ax2.set_xlabel("Current (A, log)")
    ax2.set_ylabel("Count")
    ax2.grid(True, alpha=0.3, which="both")
    fig.tight_layout()
    fig.savefig(PNG_PATH, dpi=130)
    plt.close(fig)
    return True


def main() -> int:
    print(f"[{RUN_LABEL}] connecting to 34465A …", flush=True)
    idn = dmm.connect()
    resource = dmm.resource
    print(f"[{RUN_LABEL}] connected: {idn}", flush=True)
    print(f"[{RUN_LABEL}] configuring for fixed {FIXED_RANGE_A:g} A range …",
          flush=True)
    configure_for_fixed_range()
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
    stopped_by = "stop_file" if STOP_FILE.exists() else "signal/keyboard"
    summary = _summarize(samples)
    meta = {
        "run_label":   RUN_LABEL,
        "start_iso":   start_iso,
        "end_iso":     end_iso,
        "idn":         idn,
        "resource":    resource,
        "function":    "CURR:DC",
        "nplc":        NPLC,
        "az":          "OFF",
        "range_mode":  f"fixed {FIXED_RANGE_A} A",
        "stopped_by":  stopped_by,
    }
    _write_summary_incremental(samples, end_t, len(samples), final=True)
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
    dmm.disconnect()
    print()
    print(f"  samples   : {summary.get('samples', 0)}")
    print(f"  valid     : {summary.get('valid_samples', 0)}")
    print(f"  overrange : {summary.get('overrange_samples', 0)}")
    print(f"  duration  : {summary.get('duration_s', 0):.2f} s")
    print(f"  rate      : {summary.get('rate_hz', 0):.2f} Hz")
    print(f"  min / max : {(summary.get('min_a') or 0)*1e6:.3f} µA / "
          f"{(summary.get('max_a') or 0)*1e3:.3f} mA")
    print()
    print(f"  -> {CSV_PATH}")
    print(f"  -> {JSON_PATH}")
    print(f"  -> {MD_PATH}  (if samples > 0)")
    print(f"  -> {PNG_PATH}  (if samples > 0)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
