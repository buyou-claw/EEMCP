"""MT8852B single-point CW capture.

Captures one (frequency, TX-setting) point from the MT8852B using the
CWMEAS + CWRESULT flow documented in the Remote Programming Manual
Ch. 9 §9-9 (NOT in mcp_8852b/scpi.py — those builders cover BLE-protocol
MEASBLECAPX, which returns valid=FALSE for unmodulated carriers).

Built-in §16 wedge recovery (NOTES.md §9 + §16): if any query times out,
run *RST + *CLS + *OPC? with a longer settle, then retry. The first sweep
of the day typically needs one recovery; later points often go clean.

Usage:
    # One-shot, just print results to stdout
    python -m mcp_8852b.scripts.run_cw_capture --freq-mhz 2402 --tx-setting 6

    # Append the step to an existing record directory (record.{json,csv,md})
    python -m mcp_8852b.scripts.run_cw_capture \
        --freq-mhz 2402 --tx-setting 6 \
        --append-to reports/dut_14D_cw_2026-07-31

    # Start a new record (uses 2026-07-31 as captured_at; pass --captured-at
    # if running on a different day)
    python -m mcp_8852b.scripts.run_cw_capture \
        --freq-mhz 2440 --tx-setting 0 \
        --new-record reports/dut_14D_cw_ch19_2440_2026-07-31

Output: structured samples to stdout + optional JSON/CSV/MD append.
"""
from __future__ import annotations
import argparse
import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pyvisa

from mcp_8852b import config as cfg


# ----------------------------------------------------------------------------
# §16 wedge recovery
# ----------------------------------------------------------------------------
def recover(instr: pyvisa.resources.MessageBasedResource) -> None:
    """*RST + *CLS + *OPC? with extended timeouts.

    The first command after a wedged state often needs a longer settle
    (the unit reinitializes its DSP after *RST). Empirically 3 s + *OPC?
    round-trip clears both single-VISA-session holds and the
    EUT-control-stuck state described in NOTES.md §15.
    """
    instr.clear()
    saved_timeout = instr.timeout
    instr.timeout = 8000
    instr.write("*RST")
    time.sleep(3.0)
    instr.write("*CLS")
    time.sleep(1.0)
    instr.query("*OPC?").strip()  # blocks until ready
    instr.timeout = saved_timeout


# ----------------------------------------------------------------------------
# CWRESULT MOD field parser
# ----------------------------------------------------------------------------
def _parse_mod_field(s: str) -> tuple[int, int]:
    """MOD field looks like '+3.66k/-3.68k' or '+3.66k,-3.68k'. Return (pos, neg) in Hz."""
    s = s.replace(" ", "")
    sep = "/" if "/" in s else ","
    plus_s, minus_s = s.split(sep, 1)
    return _parse_hz(plus_s), _parse_hz(minus_s)


def _parse_hz(s: str) -> int:
    s = s.strip().lstrip("+")
    if s.endswith("k") or s.endswith("K"):
        return int(round(float(s[:-1]) * 1000))
    if s.endswith("M"):
        return int(round(float(s[:-1]) * 1e6))
    return int(round(float(s)))


# ----------------------------------------------------------------------------
# Capture one (freq, tx_setting) point
# ----------------------------------------------------------------------------
def capture_point(
    freq_mhz: float,
    tx_setting_dbm: float,
    n_samples: int = 5,
    settle_s: float = 0.5,
    gap_s: float = 0.2,
    max_attempts: int = 3,
) -> list[dict]:
    """Capture n_samples successive CWRESULT reads at freq_mhz.

    Returns a list of dicts: [{i, power_dbm, freq_offset_hz, pos_mod_hz, neg_mod_hz, note}, ...]
    First sample is treated as settling (note='settling'); remainder are steady state.

    Raises pyvisa.VisaIOError if all attempts fail.
    """
    freq_hz = int(round(freq_mhz * 1e6))
    rm = pyvisa.ResourceManager(cfg.CONFIG.visa_library)
    instr = rm.open_resource(cfg.CONFIG.visa_resource)
    instr.timeout = cfg.CONFIG.timeout_ms
    instr.clear()

    last_err: Exception | None = None
    samples: list[dict] = []

    for attempt in range(1, max_attempts + 1):
        try:
            instr.write(f"CWMEAS FREQ,{freq_hz},3e-3")
            time.sleep(settle_s)
            samples = []
            for i in range(n_samples):
                p = float(instr.query("CWRESULT POWER").strip())
                f = float(instr.query("CWRESULT FREQOFF").strip())
                m = instr.query("CWRESULT MOD").strip()
                pos, neg = _parse_mod_field(m)
                samples.append({
                    "i": i,
                    "power_dbm": p,
                    "freq_offset_hz": int(f),
                    "pos_mod_hz": pos,
                    "neg_mod_hz": neg,
                    "note": "settling" if i == 0 else "",
                })
                time.sleep(gap_s)
            # Restore BLE test mode for the next call (or end of session)
            instr.write("OPMD SCRIPT")
            instr.close()
            return samples
        except pyvisa.errors.VisaIOError as e:
            last_err = e
            print(f"  attempt {attempt}: VISA error {e}", file=sys.stderr)
            try:
                recover(instr)
            except Exception as re_err:
                print(f"    recovery also failed: {re_err}", file=sys.stderr)

    try:
        instr.close()
    except Exception:
        pass
    raise last_err  # type: ignore[misc]


# ----------------------------------------------------------------------------
# Summary stats
# ----------------------------------------------------------------------------
def summarize(samples: list[dict]) -> dict:
    """Compute steady-state stats from samples (i >= 1)."""
    steady = [s for s in samples if s["i"] >= 1]
    n = len(steady)
    if n == 0:
        return {"power_dbm_steady": None, "freq_offset_hz": None, "mod_pk_hz_steady": None}
    p_avg = sum(s["power_dbm"] for s in steady) / n
    f_avg = sum(s["freq_offset_hz"] for s in steady) / n
    mod_avg = max(
        max(abs(s["pos_mod_hz"]) for s in steady),
        max(abs(s["neg_mod_hz"]) for s in steady),
    )
    return {
        "power_dbm_steady": round(p_avg, 3),
        "power_dbm_first_settle": samples[0]["power_dbm"],
        "freq_offset_hz": int(round(f_avg)),
        "mod_pk_hz_steady": mod_avg,
        "n_steady": n,
    }


# ----------------------------------------------------------------------------
# Record directory I/O
# ----------------------------------------------------------------------------
RECORD_TEMPLATE = {
    "dut": {
        "model": "14D",           # overridden by --model
        "role": "BLE Sticker PCBA under test",
        "firmware": "10.1.2.0-g3916578480",   # overridden by --firmware
        "fw_source": "BLE Sticker control app footer",
        "bd_addr": "E0:FD:45:17:B1:F9",       # overridden by --bd-addr
        "bd_addr_source": "BLE Sticker control app footer",
        "controller_config": {
            "select_frequency": None,
            "physical_freq_mhz": None,
            "ble_channel": None,
            "select_phy": "1M",
            "transceiver_mode": "Transmit",
            "packet_type": "Unmodulated carrier",
            "packet_length": 37,
            "transmit_power_mode": "Specified value",
            "tx_power_slider_units": "0.1 dBm per step (slider 10 = 1 dBm)",
        },
    },
    "bench": {
        "tester_model": "Anritsu MT8852B",
        "tester_serial": "001446004",
        "tester_firmware": "4.20.000",
        "tester_resource": "GPIB0::27::INSTR",
        "tester_visa_lib": r"C:\Windows\System32\nivisa64.dll",
        "instrument_mode": "CW measurement (CWMEAS, not BLE MEASBLECAPX)",
        "instrument_recipe_per_step": "CWMEAS FREQ,<freq_hz>,3e-3  (gate 3ms, auto-enters CW mode per Ch. 9 §9-9)",
        "egress": "OPMD SCRIPT (restored to BLE test mode after each step)",
        "ranges_tried": ["AUTO (default for CWMEAS)"],
    },
    "measurement": {
        "method": "MT8852B CW measurement — CWRESULT over 5 successive reads after 0.5s settle, 200ms between reads",
        "channel_ble": None,
        "channel_freq_hz": None,
        "gate_width_s": 0.003,
        "bt_spec_limits": {"freq_offset_limit_hz": "+/-150000"},
        "steps": [],
    },
}


def _ble_channel_for_freq_mhz(freq_mhz: float) -> int:
    """BLE data channel index for the given carrier frequency.

    ch 0  = 2402 MHz
    ch 19 = 2440 MHz
    ch 39 = 2480 MHz
    (linear: freq_mhz = 2402 + 2*ch)
    """
    return int(round((freq_mhz - 2402.0) / 2.0))


def init_record(record_dir: Path, freq_mhz: float, captured_at: str,
                 model: str = "14D", firmware: str = "10.1.2.0-g3916578480",
                 bd_addr: str = "E0:FD:45:17:B1:F9") -> dict:
    """Build a fresh record dict for a (freq_mhz) sweep and write record.json."""
    record = json.loads(json.dumps(RECORD_TEMPLATE))  # deep copy
    record["dut"]["model"] = model
    record["dut"]["firmware"] = firmware
    record["dut"]["bd_addr"] = bd_addr
    record["record_id"] = f"dut_{model}_cw_ch{_ble_channel_for_freq_mhz(freq_mhz)}_{freq_mhz:.0f}MHz_{captured_at}"
    record["captured_at"] = captured_at
    record["dut"]["controller_config"]["select_frequency"] = _ble_channel_for_freq_mhz(freq_mhz)
    record["dut"]["controller_config"]["physical_freq_mhz"] = freq_mhz
    record["dut"]["controller_config"]["ble_channel"] = _ble_channel_for_freq_mhz(freq_mhz)
    record["measurement"]["channel_ble"] = _ble_channel_for_freq_mhz(freq_mhz)
    record["measurement"]["channel_freq_hz"] = int(round(freq_mhz * 1e6))
    record["measurement"]["channel_freq_mhz"] = freq_mhz
    record["files"] = {"json": "record.json", "md": "record.md", "csv": "record.csv"}

    record_dir.mkdir(parents=True, exist_ok=True)
    with (record_dir / "record.json").open("w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2, ensure_ascii=False)
    # Create empty CSV with header
    with (record_dir / "record.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["step", "freq_mhz", "tx_setting_dbm", "i", "power_dbm",
                         "freq_offset_hz", "pos_mod_hz", "neg_mod_hz", "note"])
    # Create a stub MD that will be overwritten on the last step
    (record_dir / "record.md").write_text(
        f"# DUT 14D — CW Measurement Record ({freq_mhz:.0f} MHz)\n\n"
        f"**Captured**: {captured_at}\n"
        f"**Channel**: BLE ch {_ble_channel_for_freq_mhz(freq_mhz)} ({freq_mhz} MHz)\n\n"
        "(run capture to populate)\n",
        encoding="utf-8",
    )
    return record


def load_record(record_dir: Path) -> dict:
    with (record_dir / "record.json").open("r", encoding="utf-8") as fh:
        return json.load(fh)


def append_step(record_dir: Path, freq_mhz: float, tx_setting_dbm: float,
                samples: list[dict], summary: dict) -> int:
    """Append one step to record.{json,csv,md}. Returns the new step number."""
    record = load_record(record_dir)
    existing = record["measurement"]["steps"]
    step_n = (existing[-1]["step"] + 1) if existing else 1
    slider = int(round(tx_setting_dbm * 10))

    step = {
        "step": step_n,
        "freq_mhz": freq_mhz,
        "tx_setting_dbm": tx_setting_dbm,
        "slider_position": slider,
        "screenshot_summary": f"BLE Sticker config — freq {freq_mhz} MHz, slider {slider} -> {tx_setting_dbm} dBm",
        "samples": samples,
        "summary": {
            "power_dbm_steady": summary["power_dbm_steady"],
            "power_dbm_first_settle": summary["power_dbm_first_settle"],
            "freq_offset_hz": summary["freq_offset_hz"],
            "mod_pk_hz_steady": summary["mod_pk_hz_steady"],
        },
        "delta_from_setting_db": round(summary["power_dbm_steady"] - tx_setting_dbm, 3),
    }
    record["measurement"]["steps"].append(step)
    with (record_dir / "record.json").open("w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2, ensure_ascii=False)

    # CSV append
    with (record_dir / "record.csv").open("a", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        for s in samples:
            writer.writerow([
                step_n, freq_mhz, tx_setting_dbm, s["i"],
                f"{s['power_dbm']:.3f}", s["freq_offset_hz"],
                s["pos_mod_hz"], s["neg_mod_hz"], s["note"],
            ])

    return step_n


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--freq-mhz", type=float, required=True,
                    help="Carrier frequency in MHz (e.g. 2402, 2440, 2480)")
    ap.add_argument("--tx-setting", type=float, required=True,
                    help="TX power slider value in dBm (the BLE Sticker's display value)")
    ap.add_argument("--n-samples", type=int, default=5,
                    help="Number of successive CWRESULT reads (default 5)")
    ap.add_argument("--new-record", type=str, default=None, metavar="DIR",
                    help="Initialize a new record directory before capturing")
    ap.add_argument("--append-to", type=str, default=None, metavar="DIR",
                    help="Append the captured step to an existing record directory")
    ap.add_argument("--captured-at", type=str, default=None,
                    help="Override captured_at timestamp (default: today YYYY-MM-DD)")
    ap.add_argument("--model", type=str, default="14D",
                    help="DUT model name (default 14D; e.g. 12M, 14D, ...)")
    ap.add_argument("--firmware", type=str, default="10.1.2.0-g3916578480",
                    help="DUT firmware string from BLE Sticker control app footer")
    ap.add_argument("--bd-addr", type=str, default="E0:FD:45:17:B1:F9",
                    help="DUT BLE BD_ADDR from BLE Sticker control app footer")
    args = ap.parse_args()

    captured_at = args.captured_at or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if args.new_record:
        record_dir = Path(args.new_record)
        if (record_dir / "record.json").exists():
            print(f"WARNING: {record_dir}/record.json already exists — refusing to overwrite", file=sys.stderr)
            return 2
        init_record(record_dir, args.freq_mhz, captured_at,
                    model=args.model, firmware=args.firmware, bd_addr=args.bd_addr)
        print(f"Initialized new record at {record_dir}/  (model={args.model}, bd_addr={args.bd_addr}, fw={args.firmware})")
    elif args.append_to:
        record_dir = Path(args.append_to)
        if not (record_dir / "record.json").exists():
            print(f"ERROR: {record_dir}/record.json does not exist (use --new-record first)", file=sys.stderr)
            return 2
    else:
        record_dir = None  # one-shot, no record

    print(f"Capturing CW @ {args.freq_mhz:.0f} MHz, TX setting {args.tx_setting} dBm ...")
    try:
        samples = capture_point(args.freq_mhz, args.tx_setting, args.n_samples)
    except pyvisa.errors.VisaIOError as e:
        print(f"FAILED after all retries: {e}", file=sys.stderr)
        return 1

    summary = summarize(samples)

    print(f"  steady power  : {summary['power_dbm_steady']:.3f} dBm  (delta from setting = {summary['power_dbm_steady'] - args.tx_setting:+.2f} dB)")
    print(f"  steady freqoff: {summary['freq_offset_hz']:+d} Hz")
    print(f"  steady mod pk : +/-{summary['mod_pk_hz_steady']} Hz")
    print(f"  raw samples   :")
    for s in samples:
        marker = " <-- settling" if s["i"] == 0 else ""
        print(f"    i={s['i']}  P={s['power_dbm']:+7.3f} dBm  F={s['freq_offset_hz']:+7d} Hz"
              f"  MOD=+{s['pos_mod_hz']}/-{s['neg_mod_hz']} Hz{marker}")

    if record_dir is not None:
        step_n = append_step(record_dir, args.freq_mhz, args.tx_setting, samples, summary)
        print(f"  appended as step {step_n} to {record_dir}/record.{{json,csv}}")

    return 0


if __name__ == "__main__":
    sys.exit(main())