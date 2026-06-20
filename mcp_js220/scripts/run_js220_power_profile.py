r"""Standalone 60-second JS220 power profile capture.

Runs the same flow as the MCP `js220_run_power_profile` tool, but
without needing the MCP server to be running. Useful for scheduled
bench tests, ad-hoc captures, or sanity-checking the JS220 after
moving it to a new USB port.

Outputs to e:\Centrak\BLE Sticker\test\HWTEST\reports\:
  - js220_power_profile_<timestamp>/data.csv
  - js220_power_profile_<timestamp>/summary.json
  - js220_power_profile_<timestamp>/report.md

Usage (from project root):
    PYTHONIOENCODING=utf-8 python -m mcp_js220.scripts.run_js220_power_profile
    # or:
    PYTHONIOENCODING=utf-8 python mcp_js220/scripts/run_js220_power_profile.py --duration 120 --rate 20
"""
from __future__ import annotations
import sys
import os
import time
import csv
import json
import argparse
import statistics
import threading
from datetime import datetime
from pathlib import Path

# Make the project root importable whether we're invoked from the
# project root or from inside mcp_js220/scripts/.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Reuse the MCP package — this is the whole point of structuring it
# as an importable module.
from mcp_js220.instrument import JS220
from mcp_js220.config import CONFIG
from mcp_js220.server import _summarize_stats, _write_markdown


REPORTS_ROOT = Path(r"e:\Centrak\BLE Sticker\test\HWTEST\reports")
REPORTS_ROOT.mkdir(parents=True, exist_ok=True)


def _csv_fields() -> list[str]:
    return [
        "t_s", "sample_freq_Hz",
        "current_avg_A", "current_std_A", "current_min_A", "current_max_A", "current_p2p_A",
        "voltage_avg_V", "voltage_std_V", "voltage_min_V", "voltage_max_V", "voltage_p2p_V",
        "power_avg_W",   "power_std_W",   "power_min_W",   "power_max_W",   "power_p2p_W",
        "charge_C", "energy_J",
    ]


def _choose_reduction_freq(target_hz: float) -> str:
    """Pick the closest valid reduction_frequency string."""
    valid = ("100 Hz", "50 Hz", "20 Hz", "10 Hz", "5 Hz", "2 Hz", "1 Hz")
    return min(valid, key=lambda v: abs(int(v.split()[0]) - target_hz))


def _resolve_test_config(test_type, expected_current_a, sampling, current_range, io_voltage):
    """Resolve which JS220 parameters to use, based on the priority:
        1. test_type (preset) — if set, use the preset as the base
        2. explicit --sampling / --current-range / --io-voltage overrides
        3. expected_current_a — used to nudge i_range if test_type is set
        4. CONFIG defaults
    Returns a dict of {sampling_frequency, reduction_frequency, i_range, io_voltage}
    and a human-readable description.
    """
    presets = {
        "long_idle_log":      {"sampling_frequency": "1 kHz",   "reduction_frequency": "2 Hz",  "i_range": "auto",  "io_voltage": "3.3V"},
        "ble_active_bursts":  {"sampling_frequency": "100 kHz", "reduction_frequency": "10 Hz", "i_range": "auto",  "io_voltage": "3.3V"},
        "fine_peak_capture":  {"sampling_frequency": "2 MHz",   "reduction_frequency": "1 Hz",  "i_range": "10 A",  "io_voltage": "3.3V"},
        "ultra_low_current":  {"sampling_frequency": "100 Hz",  "reduction_frequency": "2 Hz",  "i_range": "18 µA", "io_voltage": "3.3V"},
        "high_current_load":  {"sampling_frequency": "10 kHz",  "reduction_frequency": "20 Hz", "i_range": "10 A",  "io_voltage": "3.3V"},
    }
    desc_parts = []
    if test_type:
        cfg = dict(presets[test_type])
        desc_parts.append(f"preset '{test_type}'")
    else:
        cfg = {
            "sampling_frequency": CONFIG.default_sampling_frequency,
            "reduction_frequency": CONFIG.default_reduction_frequency,
            "i_range": CONFIG.default_current_range,
            "io_voltage": CONFIG.default_io_voltage,
        }
        desc_parts.append("CONFIG defaults")

    # Nudge i_range from expected_current_a
    if expected_current_a is not None and expected_current_a > 0:
        ec = expected_current_a
        if ec < 18e-6:    cfg["i_range"] = "18 µA"
        elif ec < 180e-6: cfg["i_range"] = "180 µA"
        elif ec < 1.8e-3: cfg["i_range"] = "1.8 mA"
        elif ec < 18e-3:  cfg["i_range"] = "18 mA"
        elif ec < 180e-3: cfg["i_range"] = "180 mA"
        elif ec < 2.0:    cfg["i_range"] = "2 A"
        else:             cfg["i_range"] = "10 A"
        desc_parts.append(f"i_range from --expected-current-a={ec*1000:.4g} mA")

    # Explicit overrides win
    if sampling:     cfg["sampling_frequency"] = sampling;     desc_parts.append("--sampling override")
    if current_range: cfg["i_range"] = current_range;          desc_parts.append("--current-range override")
    if io_voltage:   cfg["io_voltage"] = io_voltage;          desc_parts.append("--io-voltage override")
    # The reduction frequency is always picked from the rate hint (closest valid)
    chosen_red = _choose_reduction_freq(rate_hz)
    cfg["reduction_frequency"] = chosen_red
    desc_parts.append(f"reduction from --rate={rate_hz} → {chosen_red}")
    return cfg, "; ".join(desc_parts)


def run(duration_s: float | None, rate_hz: float, label: str | None = None,
        sampling: str | None = None, current_range: str | None = None,
        io_voltage: str | None = None, test_type: str | None = None,
        expected_current_a: float | None = None) -> dict:
    js = JS220()
    try:
        info = js.connect()
        device_path = info["device_path"]
        print(f"Connected to {device_path}")
        print(f"  Model: {info['idn'].get('model')}, SN: {info['idn'].get('serial_number')}")
        print(f"  FW:    {info['idn'].get('ctl', {}).get('fw', {}).get('ver')}")

        # Resolve the test config (preset + overrides)
        cfg, cfg_desc = _resolve_test_config(
            test_type, expected_current_a, sampling, current_range, io_voltage, rate_hz)
        actual_rate = float(cfg["reduction_frequency"].split()[0])
        js.set_sampling_frequency(cfg["sampling_frequency"])
        js.set_reduction_frequency(cfg["reduction_frequency"])
        js.set_current_range(cfg["i_range"])
        js.set_io_voltage(cfg["io_voltage"])
        print(f"  Config:  {cfg_desc}")
        print(f"  Sampling: {cfg['sampling_frequency']}, "
              f"Reduction: {cfg['reduction_frequency']}, "
              f"Range: {cfg['i_range']}, I/O: {cfg['io_voltage']}")

        # Setup run dir
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        run_label = label or f"js220_power_profile_{stamp}"
        run_dir = REPORTS_ROOT / run_label
        run_dir.mkdir(parents=True, exist_ok=True)
        csv_path = run_dir / "data.csv"
        json_path = run_dir / "summary.json"
        md_path = run_dir / "report.md"

        # Start streaming
        js.start()
        # Anchor the wall clock for actual-duration calculation in live mode.
        # Hoisted out of the `with` block so it's in scope after the block exits.
        start_monotonic = time.monotonic()
        meta = {
            "device_path": device_path,
            "started": datetime.now().isoformat(timespec="seconds"),
            "sampling_frequency": js.parameter_get("sampling_frequency"),
            "reduction_frequency": js.parameter_get("reduction_frequency"),
            "i_range": js.parameter_get("i_range"),
        }
        stop_file = run_dir / ".stop"
        if duration_s is None:
            print(f"\nCapturing LIVE — stop with Ctrl+C, or touch {stop_file}")
        else:
            print(f"\nCapturing {duration_s:.1f} s @ {actual_rate} Hz stats rate...")

        # Stream + write
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(_csv_fields())
            f.flush()

            stop_flag = [False]
            def flush_thread_fn():
                while not stop_flag[0]:
                    time.sleep(0.5)
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except OSError:
                        pass
            t = threading.Thread(target=flush_thread_fn, daemon=True)
            t.start()

            end_time = None if duration_s is None else time.monotonic() + duration_s
            last_count = 0
            progress_step = max(1, int(duration_s / 10)) if duration_s else 10
            elapsed_marker = 0
            try:
                while end_time is None or time.monotonic() < end_time:
                    if stop_file.exists() or stop_flag[0]:
                        break
                    time.sleep(min(0.1, (end_time - time.monotonic()) if end_time else 0.1))
                    snap = js.get_stats_snapshot()
                    for s in snap[last_count:]:
                        row = []
                        t_info = s.get("time", {})
                        try:
                            rng = t_info["range"]["value"]
                            row.append(rng[1] - rng[0])
                        except (KeyError, TypeError):
                            row.append("")
                        try:
                            row.append(t_info["sample_freq"]["value"])
                        except (KeyError, TypeError):
                            row.append("")
                        for sig in ("current", "voltage", "power"):
                            for stat in ("avg", "std", "min", "max", "p2p"):
                                try:
                                    row.append(s["signals"][sig][stat]["value"])
                                except (KeyError, TypeError):
                                    row.append("")
                        for acc in ("charge", "energy"):
                            try:
                                row.append(s[acc]["value"])
                            except (KeyError, TypeError):
                                row.append("")
                        w.writerow(row)
                    last_count = len(snap)
                    f.flush()
                    if end_time is not None:
                        elapsed = duration_s - (end_time - time.monotonic())
                        if int(elapsed) // progress_step != elapsed_marker:
                            elapsed_marker = int(elapsed) // progress_step
                            print(f"  ...{elapsed:.0f}/{duration_s:.0f} s  ({len(snap)} samples)")
                    elif int(time.monotonic()) // progress_step != elapsed_marker:
                        elapsed_marker = int(time.monotonic()) // progress_step
                        print(f"  ...{elapsed_marker * progress_step} s  ({len(snap)} samples)")
            except KeyboardInterrupt:
                stop_flag[0] = True
                print("\n[KeyboardInterrupt] stopping cleanly...")
            stop_flag[0] = True
            t.join(timeout=1.0)
            samples = js.get_stats_snapshot()
            # Record how the capture ended so the report can show it
            if stop_file.exists():
                meta["stopped_by"] = "stop_file"
            elif stop_flag[0] and "stopped_by" not in meta:
                meta["stopped_by"] = "keyboard_interrupt"
            else:
                meta["stopped_by"] = "duration"

        js.stop()
        print(f"\nDone. {len(samples)} samples captured.")

        # In live mode, compute the actual duration from the wall clock.
        actual_duration_s = (time.monotonic() - start_monotonic) if duration_s is None else duration_s

        # Summarize
        summary = _summarize_stats(samples)
        summary_json = {
            "run_label": run_label,
            "device_path": meta["device_path"],
            "started": meta["started"],
            "duration_s": actual_duration_s,
            "rate_hz_actual": actual_rate,
            "n_samples": len(samples),
            "sampling_frequency": meta["sampling_frequency"],
            "reduction_frequency": meta["reduction_frequency"],
            "i_range": meta["i_range"],
            "stopped_by": meta.get("stopped_by", "duration"),
            "summary": summary,
        }
        with json_path.open("w", encoding="utf-8") as f:
            json.dump(summary_json, f, indent=2, ensure_ascii=False)
        _write_markdown(md_path, run_label, actual_duration_s, actual_rate,
                        len(samples), summary, meta)

        print(f"\nWrote: {csv_path}")
        print(f"       {json_path}")
        print(f"       {md_path}")
        print(f"\nSummary:")
        for sig, unit in (("current", "A"), ("voltage", "V"), ("power", "W")):
            print(f"  {sig:8s}: avg={summary.get(f'{sig}_avg', 0):.6g} {unit}   "
                  f"peak={summary.get(f'{sig}_max', 0):.6g} {unit}")
        print(f"  charge:  {summary.get('charge_C', 0):.6f} C")
        print(f"  energy:  {summary.get('energy_J', 0):.6f} J")
        return summary_json
    finally:
        try:
            js.disconnect()
        except Exception:
            pass


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--duration", type=float, default=60.0,
                   help="capture duration in seconds (default 60, ignored when --live)")
    p.add_argument("--rate", type=float, default=10.0,
                   help="target reduction_frequency in Hz (default 10)")
    p.add_argument("--label", type=str, default=None,
                   help="subfolder name under reports/ (default: js220_power_profile_<ts>)")
    p.add_argument("--live", action="store_true",
                   help="run until Ctrl+C or until <run_dir>/.stop is touched")
    p.add_argument("--sampling", type=str, default=None,
                   help="input sampling frequency, e.g. '100 kHz', '2 MHz' (default: CONFIG.default_sampling_frequency)")
    p.add_argument("--current-range", dest="current_range", type=str, default=None,
                   help="current range, e.g. 'auto', '10 A', '180 mA' (default: CONFIG.default_current_range)")
    p.add_argument("--io-voltage", dest="io_voltage", type=str, default=None,
                   help="I/O voltage, e.g. '3.3V', '1.8V' (default: CONFIG.default_io_voltage)")
    p.add_argument("--test-type", dest="test_type", type=str, default=None,
                   choices=["long_idle_log", "ble_active_bursts", "fine_peak_capture",
                            "ultra_low_current", "high_current_load"],
                   help="pick a preset; if set, overrides --sampling/--rate/--current-range/--io-voltage")
    p.add_argument("--expected-current-a", dest="expected_current_a", type=float, default=None,
                   help="your guess at typical current draw in amps; used to nudge the current range")
    args = p.parse_args()
    run(None if args.live else args.duration, args.rate, args.label,
        sampling=args.sampling, current_range=args.current_range,
        io_voltage=args.io_voltage, test_type=args.test_type,
        expected_current_a=args.expected_current_a)


if __name__ == "__main__":
    main()
