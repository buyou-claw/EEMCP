r"""Pre-flight configuration assistant for the JS220 MCP.

When you're about to run a test, you should know:
  1. What test type is it? (standby log / active bursts / peak capture / etc.)
  2. What current range do you expect? (rough order of magnitude)
  3. What I/O voltage does the DUT use?
  4. How long should the capture run?
  5. What sample rate do you need? (10 Hz for slow log, up to 2 MHz for peaks)

This script asks those questions interactively (or via CLI flags), then
prints a recommendation and (optionally) writes the config to a
`config.json` next to your data capture.

Use the answers to drive `run_js220_power_profile.py` flags:
    --test-type ble_active_bursts
    --expected-current-a 0.05
    --io-voltage 3.3V
    --live
"""
from __future__ import annotations
import sys
import json
import argparse
from pathlib import Path

# Preset table — must match run_js220_power_profile.py
PRESETS = {
    "long_idle_log": {
        "use_when": "monitoring standby/connected-but-idle state for minutes to hours",
        "sampling_frequency": "1 kHz",
        "reduction_frequency": "2 Hz",
        "i_range": "auto",
        "io_voltage": "3.3V",
    },
    "ble_active_bursts": {
        "use_when": "capturing BLE TX/RX bursts (e.g. advertising or connection events)",
        "sampling_frequency": "100 kHz",
        "reduction_frequency": "10 Hz",
        "i_range": "auto",
        "io_voltage": "3.3V",
    },
    "fine_peak_capture": {
        "use_when": "you need to see the shape of a single TX burst (e.g. inrush spikes, dI/dt)",
        "sampling_frequency": "2 MHz",
        "reduction_frequency": "1 Hz",
        "i_range": "10 A",
        "io_voltage": "3.3V",
    },
    "ultra_low_current": {
        "use_when": "standby current is < 1 µA and you need clean µA-level resolution",
        "sampling_frequency": "100 Hz",
        "reduction_frequency": "2 Hz",
        "i_range": "18 µA",
        "io_voltage": "3.3V",
    },
    "high_current_load": {
        "use_when": "DUT draws > 100 mA continuously (e.g. with motors, radios, displays)",
        "sampling_frequency": "10 kHz",
        "reduction_frequency": "20 Hz",
        "i_range": "10 A",
        "io_voltage": "3.3V",
    },
}

# Valid JS220 parameter values (mirrors config.py)
VALID_CURRENT_RANGES = (
    "auto", "10 A", "2 A", "180 mA", "18 mA",
    "1.8 mA", "180 µA", "18 µA", "off",
)
VALID_SAMPLING_FREQS = (
    "2 MHz", "1 MHz", "500 kHz", "200 kHz", "100 kHz",
    "50 kHz", "20 kHz", "10 kHz", "5 kHz", "2 kHz",
    "1 kHz", "500 Hz", "200 Hz", "100 Hz", "50 Hz", "20 Hz", "10 Hz",
)
VALID_REDUCTION_FREQS = ("100 Hz", "50 Hz", "20 Hz", "10 Hz",
                         "5 Hz", "2 Hz", "1 Hz")
VALID_IO_VOLTAGES = (
    "1.8V", "2.1V", "2.5V", "2.7V", "3.0V", "3.3V", "3.6V", "5.0V",
)


def nudge_range(ec: float) -> str:
    """Pick the smallest i_range that still gives 5-10x headroom over the expected current."""
    if ec < 18e-6:    return "18 µA"
    elif ec < 180e-6: return "180 µA"
    elif ec < 1.8e-3: return "1.8 mA"
    elif ec < 18e-3:  return "18 mA"
    elif ec < 180e-3: return "180 mA"
    elif ec < 2.0:    return "2 A"
    else:             return "10 A"


def parse_amps(s: str) -> float:
    """Parse '100 µA', '50 mA', '0.5 A', '2e-3' style inputs."""
    s = s.strip()
    # Try plain float first
    try:
        return float(s)
    except ValueError:
        pass
    # "100 µA" / "50 mA" / "5 A" / "200 uA"
    parts = s.split()
    if len(parts) != 2:
        raise ValueError(f"can't parse {s!r} as a current")
    val = float(parts[0])
    unit = parts[1].lower().replace("ua", "µa").replace("ua", "µa")
    if unit == "na":  return val * 1e-9
    if unit == "µa" or unit == "ua": return val * 1e-6
    if unit == "ma":  return val * 1e-3
    if unit == "a":   return val
    raise ValueError(f"unknown unit {unit!r} in {s!r}")


def interactive() -> dict:
    print("=" * 70)
    print(" JS220 Pre-flight Configuration")
    print("=" * 70)
    print()
    print("I'll ask you a few questions to pick the right JS220 settings.")
    print("Press Enter to accept the default in [brackets].")
    print()

    # 1. Test type
    print("1. What kind of test are you running?")
    for i, (k, v) in enumerate(PRESETS.items(), 1):
        print(f"   {i}. {k:>22}  —  {v['use_when']}")
    print()
    type_choice = input("   Choose [2 = ble_active_bursts]: ").strip()
    type_idx = int(type_choice) if type_choice else 2
    test_type = list(PRESETS.keys())[type_idx - 1]

    # 2. Expected current
    print()
    print("2. Expected current draw (with units):")
    print("   Examples:  '100 µA' / '5 mA' / '0.05' (= 50 mA) / '2e-3' (= 2 mA)")
    cur_str = input("   Typical current [50 mA]: ").strip() or "50 mA"
    expected_a = parse_amps(cur_str)

    # 3. Peak current (for range headroom)
    print()
    print("3. Worst-case peak current (for range headroom):")
    peak_str = input("   Peak current [10x typical]: ").strip()
    if peak_str:
        peak_a = parse_amps(peak_str)
    else:
        peak_a = expected_a * 10

    # 4. I/O voltage
    print()
    print(f"4. DUT I/O voltage (one of {VALID_IO_VOLTAGES}):")
    io_v = input("   I/O voltage [3.3V]: ").strip() or "3.3V"

    # 5. Capture duration
    print()
    print("5. Capture mode:")
    print("   1. fixed duration (seconds)")
    print("   2. live — stop with Ctrl+C or .stop file")
    mode = input("   Choose [1 = fixed 60s]: ").strip() or "1"
    if mode == "2":
        duration_s = None
    else:
        dur_str = input("   Duration in seconds [60]: ").strip() or "60"
        duration_s = float(dur_str)

    # 6. Label
    print()
    label = input("6. Run label (subfolder under reports/) [auto]: ").strip() or None

    # Resolve
    rec = dict(PRESETS[test_type])
    rec["i_range"] = nudge_range(peak_a)  # use peak for headroom
    rec["io_voltage"] = io_v

    print()
    print("=" * 70)
    print(" Recommended JS220 configuration")
    print("=" * 70)
    for k, v in rec.items():
        print(f"  {k:>22}: {v}")
    print()
    print(f"  expected_current   : {expected_a*1000:.4f} mA")
    print(f"  peak_current       : {peak_a*1000:.4f} mA")
    print(f"  capture_mode       : {'live' if duration_s is None else f'{duration_s:.0f}s fixed'}")
    print(f"  run_label          : {label or '<auto: js220_power_profile_<ts>>'}")
    print()
    print("Command-line invocation:")
    extras = ""
    if duration_s is None:
        extras = " --live"
    else:
        extras = f" --duration {duration_s}"
    if label:
        extras += f" --label {label}"
    print(f"  python mcp_js220/scripts/run_js220_power_profile.py \\")
    print(f"      --test-type {test_type} \\")
    print(f"      --expected-current-a {expected_a} \\")
    print(f"      --io-voltage {io_v}{extras}")

    return {
        "test_type": test_type,
        "expected_current_a": expected_a,
        "peak_current_a": peak_a,
        "io_voltage": io_v,
        "duration_s": duration_s,
        "label": label,
        "recommended": rec,
    }


def cli_args(args) -> dict:
    """Build a config from CLI flags (non-interactive)."""
    test_type = args.test_type or "ble_active_bursts"
    rec = dict(PRESETS[test_type])
    if args.expected_current_a is not None:
        rec["i_range"] = nudge_range(args.expected_current_a)
    if args.io_voltage:
        rec["io_voltage"] = args.io_voltage
    if args.sampling:
        rec["sampling_frequency"] = args.sampling
    return {
        "test_type": test_type,
        "expected_current_a": args.expected_current_a,
        "io_voltage": args.io_voltage or "3.3V",
        "recommended": rec,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--test-type", dest="test_type", type=str, default=None,
                   choices=list(PRESETS.keys()))
    p.add_argument("--expected-current-a", dest="expected_current_a", type=float, default=None)
    p.add_argument("--io-voltage", dest="io_voltage", type=str, default=None)
    p.add_argument("--sampling", dest="sampling", type=str, default=None)
    p.add_argument("--non-interactive", dest="non_interactive", action="store_true",
                   help="don't prompt; use the flags + defaults")
    p.add_argument("--output", dest="output", type=str, default=None,
                   help="write the resolved config to this JSON file")
    args = p.parse_args()
    if args.non_interactive or args.test_type or args.expected_current_a is not None or args.io_voltage:
        cfg = cli_args(args)
    else:
        if not sys.stdin.isatty():
            print("ERROR: stdin is not a TTY; pass --non-interactive with the flags", file=sys.stderr)
            sys.exit(1)
        cfg = interactive()
    print(json.dumps(cfg, indent=2, ensure_ascii=False))
    if args.output:
        Path(args.output).write_text(json.dumps(cfg, indent=2, ensure_ascii=False),
                                     encoding="utf-8")
        print(f"\nWrote: {args.output}")


if __name__ == "__main__":
    main()
