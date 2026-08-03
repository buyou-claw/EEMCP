"""BLE Sticker HWDVT — TX power vs supply voltage sweep (CW protocol).

For each combination of (model, supply voltage, channel), measures the
radiated CW power of a DUT commanded to transmit an unmodulated carrier
at 0 dBm on that channel.

  Models:    12M / 14D (the two BLE Sticker PCBA SKUs)
  Voltages:  3.0 / 2.4 / 1.71 / 1.6 V  (typ + low-battery corners)
  Target TX: 0 dBm                     (operator-set on the DUT via the
                                       BLE Sticker control app — this
                                       script does NOT drive the DUT
                                       or the bench PSU)
  Channels:  0 / 19 / 39               (2402 / 2440 / 2480 MHz)

Bench:
  MT8852B   CW measurement (CWMEAS FREQ,<hz>,3e-3 → 5× CWRESULT per cell)

  Note on protocol choice: MEASBLECAPX (the BLE-protocol LEOP recipe)
  returns valid=FALSE for unmodulated carriers — that's the whole reason
  this script drives CWMEAS instead. See run_cw_capture.py for the
  protocol reference and the §16 wedge-recovery story (NOTES.md §9, §16).

The DUT is configured outside the script (operator uses the BLE Sticker
control app to set carrier freq + TX slider position). The script just
loops channels within a (model, voltage) phase. Between phases the
operator is prompted to swap the PCBA / re-set the bench PSU / re-arm
the DUT on the next frequency. Hit Enter to advance.

Output (per run, written to reports/tx_pwr_vs_v_<run_label>/):
  result.csv   one row per (model, voltage, channel)
  result.md    grouped table per model
  result.json  full structured result

Pass criterion: |power_dbm_steady - target_dbm| <= tolerance_db
                (default ±3 dB per BLE Sticker DVT round-1 spec)

Usage:
    # Default sweep (2 models x 4 voltages x 3 channels = 24 CW captures)
    python -m mcp_8852b.scripts.run_tx_power_vs_voltage

    # Custom matrix
    python -m mcp_8852b.scripts.run_tx_power_vs_voltage \
        --models 12M --voltages 3.0 1.6 --channels 19 \
        --tolerance-db 2.0 --label quick_check

Exit code: 0 if every row passes, 1 if any row fails, 2 on instrument
or capture error.
"""
from __future__ import annotations
import argparse
import csv
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pyvisa

from mcp_8852b.scripts.run_cw_capture import capture_point, summarize


# ----------------------------------------------------------------------------
# Defaults — overridden by CLI flags
# ----------------------------------------------------------------------------
DEFAULT_MODELS    = ["12M", "14D"]
DEFAULT_VOLTAGES  = [3.0, 2.4, 1.71, 1.6]
DEFAULT_CHANNELS  = [0, 19, 39]            # BLE data channels (2402 + 2*ch MHz)
DEFAULT_TARGET_DBM = 0.0
DEFAULT_TOLERANCE_DB = 3.0                 # DVT round-1 window
DEFAULT_N_SAMPLES = 5                      # CWRESULT reads per cell (1st = settling)
DEFAULT_GAP_S = 0.2                        # delay between CWRESULT reads

REPORTS_ROOT = _PROJECT_ROOT / "reports"
REPORTS_ROOT.mkdir(parents=True, exist_ok=True)


def _hr(c: str = "=") -> str:
    return c * 78


def _channel_to_freq_mhz(ch: int) -> int:
    """BLE channel → carrier frequency in MHz (BLE data channels only)."""
    return 2402 + 2 * ch


# ----------------------------------------------------------------------------
# One CW capture cell
# ----------------------------------------------------------------------------
def _run_cw_cell(channel: int, target_dbm: float, n_samples: int,
                 gap_s: float) -> dict:
    """Drive one CWMEAS + n× CWRESULT capture. Returns a result row.

    Wraps capture_point() (which has built-in §16 wedge recovery) and
    attaches the steady-state summary fields used for the pass check.
    """
    freq_mhz = _channel_to_freq_mhz(channel)
    row = {
        "channel": channel, "freq_mhz": freq_mhz, "ok": False,
    }
    try:
        samples = capture_point(
            freq_mhz, target_dbm, n_samples=n_samples, gap_s=gap_s,
        )
    except pyvisa.errors.VisaIOError as e:
        row["error"] = f"CW capture failed after retries: {e}"
        return row
    except Exception as e:
        row["error"] = f"CW capture: {type(e).__name__}: {e}"
        return row

    summary = summarize(samples)
    if summary["power_dbm_steady"] is None:
        row["error"] = (
            f"no steady samples (got {len(samples)}, expected "
            f"{n_samples}); check CWRESULT response shape")
        row["samples"] = samples
        return row

    row.update({
        "ok": True,
        "samples": samples,
        "power_dbm_steady":      summary["power_dbm_steady"],
        "power_dbm_first_settle": summary["power_dbm_first_settle"],
        "freq_offset_hz":        summary["freq_offset_hz"],
        "mod_pk_hz_steady":      summary["mod_pk_hz_steady"],
        "n_steady":              summary["n_steady"],
    })
    return row


# ----------------------------------------------------------------------------
# Operator prompts (informational only — script does not enforce state)
# ----------------------------------------------------------------------------
def _prompt(msg: str, skip: bool) -> None:
    if skip:
        return
    print()
    print(_hr("-"))
    print(msg)
    print(_hr("-"))
    try:
        input("  Press Enter when ready (Ctrl+C to abort) ... ")
    except EOFError:
        print("  [WARN] stdin closed; continuing without confirmation.")


def _print_phase_banner(model: str, v_target: float, ch: int,
                        freq_mhz: int, target_dbm: float,
                        tol_db: float) -> None:
    print(f"  [{model} | Vcc target={v_target:.2f} V | "
          f"ch {ch} ({freq_mhz} MHz) | "
          f"target TX = {target_dbm:+.1f} dBm ± {tol_db:.1f} dB]")


# ----------------------------------------------------------------------------
# Output writers
# ----------------------------------------------------------------------------
def _write_outputs(run_dir: Path, args: argparse.Namespace,
                   results: list[dict]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)

    # 1. JSON
    payload = {
        "config": {
            "models":       args.models,
            "voltages_v":   args.voltages,
            "channels":     args.channels,
            "target_dbm":   args.target_dbm,
            "tolerance_db": args.tolerance_db,
            "n_samples":    args.n_samples,
            "gap_s":        args.gap_s,
        },
        "started":  results[0]["ts"] if results else None,
        "finished": results[-1]["ts"] if results else None,
        "results":  results,
    }
    (run_dir / "result.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8")

    # 2. CSV — one row per (model, voltage, channel)
    with (run_dir / "result.csv").open("w", newline="",
                                       encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "model", "v_target_v", "channel", "freq_mhz",
            "target_dbm", "measured_dbm", "delta_db",
            "power_dbm_first_settle",
            "freq_offset_hz", "mod_pk_hz_steady",
            "host_pass", "ok", "error", "ts",
        ])
        w.writeheader()
        for r in results:
            w.writerow({
                "model": r.get("model", ""),
                "v_target_v": r.get("v_target_v", ""),
                "channel": r.get("channel", ""),
                "freq_mhz": r.get("freq_mhz", ""),
                "target_dbm": r.get("target_dbm", ""),
                "measured_dbm": (
                    f"{r['power_dbm_steady']:.3f}"
                    if r.get("ok") else ""),
                "delta_db": (
                    f"{r['delta_db']:+.3f}"
                    if r.get("ok") else ""),
                "power_dbm_first_settle": (
                    f"{r['power_dbm_first_settle']:.3f}"
                    if r.get("ok") else ""),
                "freq_offset_hz": (
                    r.get("freq_offset_hz", "")
                    if r.get("ok") else ""),
                "mod_pk_hz_steady": (
                    r.get("mod_pk_hz_steady", "")
                    if r.get("ok") else ""),
                "host_pass": r.get("host_pass", ""),
                "ok": r.get("ok", False),
                "error": r.get("error", ""),
                "ts": r.get("ts", ""),
            })

    # 3. Markdown — grouped table per model
    lines = [
        "# TX Power vs Supply Voltage — BLE Sticker DVT (CW protocol)",
        "",
        f"**Target TX power**: {args.target_dbm:+.1f} dBm  ",
        f"**Tolerance**: ±{args.tolerance_db:.1f} dB (host-side pass)  ",
        f"**Channels**: {args.channels}  ",
        f"**Voltages**: {args.voltages} V  ",
        f"**Protocol**: MT8852B CWMEAS + {args.n_samples}× CWRESULT per "
        f"cell (1st = settling, remainder averaged)",
        "",
    ]
    for model in args.models:
        lines.append(f"## Model: {model}")
        lines.append("")
        lines.append("| Vcc (V) | Ch | Freq (MHz) | "
                     "Measured (dBm) | Δ (dB) | "
                     "FreqOff (Hz) | ModPk (Hz) | Host | Error |")
        lines.append("|---:|---:|---:|---:|---:|---:|---:|:---:|---|")
        for r in results:
            if r.get("model") != model:
                continue
            if r.get("ok"):
                host = ("PASS" if r["host_pass"] is True
                        else "FAIL" if r["host_pass"] is False
                        else "N/A")
                lines.append(
                    f"| {r['v_target_v']:.2f} | {r['channel']} | "
                    f"{r['freq_mhz']} | {r['power_dbm_steady']:+.3f} | "
                    f"{r['delta_db']:+.3f} | "
                    f"{r['freq_offset_hz']:+d} | "
                    f"{r['mod_pk_hz_steady']} | {host} |  |"
                )
            else:
                err_short = (r.get("error") or "")[:60]
                lines.append(
                    f"| {r['v_target_v']:.2f} | {r['channel']} | "
                    f"{r['freq_mhz']} | — | — | — | — | — | "
                    f"`{err_short}` |"
                )
        lines.append("")

    pass_count = sum(1 for r in results
                     if r.get("ok") and r.get("host_pass") is True)
    fail_count = sum(1 for r in results
                     if r.get("ok") and r.get("host_pass") is False)
    err_count = sum(1 for r in results if not r.get("ok"))
    lines.append("---")
    lines.append(f"**Summary**: {pass_count} PASS, {fail_count} FAIL, "
                 f"{err_count} ERR  (of {len(results)} rows)")
    (run_dir / "result.md").write_text("\n".join(lines),
                                        encoding="utf-8")


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="BLE Sticker TX power vs supply voltage sweep "
                    "(MT8852B CW protocol — operator handles DUT + PSU)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--models", nargs="+", default=DEFAULT_MODELS,
                    help=f"PCBA model labels (default: {DEFAULT_MODELS})")
    ap.add_argument("--voltages", nargs="+", type=float,
                    default=DEFAULT_VOLTAGES,
                    help=f"supply voltages in V "
                         f"(default: {DEFAULT_VOLTAGES})")
    ap.add_argument("--channels", nargs="+", type=int,
                    default=DEFAULT_CHANNELS,
                    help=f"BLE data channels 0..39 "
                         f"(default: {DEFAULT_CHANNELS})")
    ap.add_argument("--target-dbm", type=float, default=DEFAULT_TARGET_DBM,
                    help=f"target TX power the operator sets on the DUT "
                         f"(default {DEFAULT_TARGET_DBM:+.1f} dBm)")
    ap.add_argument("--tolerance-db", type=float,
                    default=DEFAULT_TOLERANCE_DB,
                    help=f"host-side pass window around target "
                         f"(default ±{DEFAULT_TOLERANCE_DB:.1f} dB)")
    ap.add_argument("--n-samples", type=int, default=DEFAULT_N_SAMPLES,
                    help=f"number of CWRESULT reads per cell "
                         f"(default {DEFAULT_N_SAMPLES}, 1st = settling)")
    ap.add_argument("--gap-s", type=float, default=DEFAULT_GAP_S,
                    help=f"delay between successive CWRESULT reads "
                         f"(default {DEFAULT_GAP_S}s)")
    ap.add_argument("--label", default=None,
                    help="run label (default: auto-generated timestamp)")
    ap.add_argument("--no-confirm", action="store_true",
                    help="suppress all operator prompts (only safe if "
                         "the operator has already pre-set the matrix "
                         "externally and is just streaming CW captures)")
    args = ap.parse_args(argv)

    # Run label + output dir
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_label = args.label or f"tx_pwr_vs_v_{stamp}"
    run_dir = REPORTS_ROOT / run_label

    # Sanity checks
    for ch in args.channels:
        if not 0 <= ch <= 39:
            ap.error(f"channel {ch} out of BLE range 0..39")
    for v in args.voltages:
        if not 0.5 <= v <= 5.5:
            ap.error(f"voltage {v} V outside reasonable bench range "
                     "0.5..5.5 V — check DEFAULT_VOLTAGES / config")

    print(_hr("="))
    print("BLE Sticker HWDVT — TX Power vs Supply Voltage Sweep")
    print("(CW protocol — MT8852B CWMEAS + CWRESULT)")
    print("(operator drives DUT via BLE Sticker control app + PSU)")
    print(_hr("="))
    print(f"  Models:    {args.models}")
    print(f"  Voltages:  {args.voltages} V  (operator sets on PSU)")
    print(f"  Channels:  {args.channels}  "
          f"({[_channel_to_freq_mhz(c) for c in args.channels]} MHz)")
    print(f"  Target TX: {args.target_dbm:+.1f} dBm  "
          f"(±{args.tolerance_db:.1f} dB, operator sets on DUT)")
    print(f"  Samples/cell: {args.n_samples}  (gap {args.gap_s}s)")
    print(f"  Output:    {run_dir}/")
    print(_hr("="))

    # Sweep
    results: list[dict] = []
    total = len(args.models) * len(args.voltages) * len(args.channels)
    done = 0
    try:
        for model in args.models:
            _prompt(
                f"PHASE: Model = {model}\n"
                f"  - Mount the {model} BLE Sticker PCBA on the bench jig\n"
                f"  - Set bench PSU to the FIRST voltage in the sweep "
                f"({args.voltages[0]} V)\n"
                f"  - In the BLE Sticker control app, set:\n"
                f"      transceiver_mode = Transmit\n"
                f"      packet_type      = Unmodulated carrier\n"
                f"      transmit_power   = {args.target_dbm:+.1f} dBm\n"
                f"      select_frequency = channel {args.channels[0]} "
                f"({_channel_to_freq_mhz(args.channels[0])} MHz)\n"
                f"\n"
                f"  (For subsequent voltages within this model, adjust "
                f"the PSU only. For each new channel, change "
                f"select_frequency in the control app.)",
                skip=args.no_confirm,
            )

            for v_target in args.voltages:
                first_ch = True
                for ch in args.channels:
                    done += 1
                    freq_mhz = _channel_to_freq_mhz(ch)
                    _print_phase_banner(
                        model, v_target, ch, freq_mhz,
                        args.target_dbm, args.tolerance_db,
                    )
                    if first_ch:
                        _prompt(
                            f"  Set bench PSU to {v_target:.2f} V and "
                            f"set BLE Sticker control app:\n"
                            f"    transmit_power   = "
                            f"{args.target_dbm:+.1f} dBm\n"
                            f"    select_frequency = channel {ch} "
                            f"({freq_mhz} MHz)\n"
                            f"  then proceed.",
                            skip=args.no_confirm,
                        )
                        first_ch = False
                    else:
                        _prompt(
                            f"  Change select_frequency in the BLE Sticker "
                            f"control app to channel {ch} ({freq_mhz} MHz) "
                            f"(PSU stays at {v_target:.2f} V), then proceed.",
                            skip=args.no_confirm,
                        )
                    t0 = time.monotonic()
                    row = _run_cw_cell(ch, args.target_dbm,
                                       args.n_samples, args.gap_s)
                    elapsed = time.monotonic() - t0
                    row.update({
                        "model":       model,
                        "v_target_v":  v_target,
                        "target_dbm":  args.target_dbm,
                        "tolerance_db": args.tolerance_db,
                        "ts": datetime.now().isoformat(timespec="seconds"),
                        "elapsed_s":   round(elapsed, 2),
                    })
                    if row.get("ok"):
                        row["delta_db"] = (row["power_dbm_steady"]
                                           - args.target_dbm)
                        row["host_pass"] = (
                            abs(row["delta_db"]) <= args.tolerance_db)
                        print(f"    → {row['power_dbm_steady']:+.3f} dBm "
                              f"(Δ {row['delta_db']:+.3f} dB, "
                              f"freqoff {row['freq_offset_hz']:+d} Hz) "
                              f"{'PASS' if row['host_pass'] else 'FAIL'} "
                              f"[{elapsed:.1f}s]")
                    else:
                        row["host_pass"] = None
                        print(f"    → ERR: "
                              f"{(row.get('error') or '?')[:80]} "
                              f"[{elapsed:.1f}s]")
                    results.append(row)

    except KeyboardInterrupt:
        print()
        print("  [ABORT] KeyboardInterrupt — writing partial report.")
    finally:
        pass  # capture_point opens/closes its own VISA session per cell

    # Write outputs
    _write_outputs(run_dir, args, results)
    pass_count = sum(1 for r in results
                     if r.get("ok") and r.get("host_pass") is True)
    fail_count = sum(1 for r in results
                     if r.get("ok") and r.get("host_pass") is False)
    err_count = sum(1 for r in results if not r.get("ok"))
    print()
    print(_hr("="))
    print(f"SWEEP COMPLETE: {done}/{total} cells  "
          f"({pass_count} PASS, {fail_count} FAIL, {err_count} ERR)")
    print(f"Report: {run_dir}/result.{{json,md,csv}}")
    print(_hr("="))
    if err_count and not (pass_count + fail_count):
        return 2
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())