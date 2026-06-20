"""MT8852B BLE TX test runner.

Runs a single BLE TX measurement (LEOP / LEICD / LEMI) end-to-end and
writes the result envelope to `reports/<run_label>/`. Designed for
interactive use on the bench or for inclusion in a HWDVT plan.

Usage:
    # LEOP (output power) on ch 19, PRBS9 payload (default)
    python -m mcp_8852b.scripts.run_ble_tx_test

    # LEICD (carrier drift) on ch 17, payload 10101010
    python -m mcp_8852b.scripts.run_ble_tx_test \
        --meas LEICD --mod-type MOD10101010 --channel 17

    # LEMI (modulation index) with 11110000 pattern
    python -m mcp_8852b.scripts.run_ble_tx_test \
        --meas LEMI --mod-type MOD11110000

    # Custom limits
    python -m mcp_8852b.scripts.run_ble_tx_test \
        --meas LEOP --lower -10 --upper 8

    # Multiple channels in one run
    python -m mcp_8852b.scripts.run_ble_tx_test \
        --meas LEOP --channels 0 19 39

The script handles EUT interface setup, the full Anritsu recipe
(SETBLECAPTYP / SYSCFG / CFGBLECAP / OPMD / MEASBLECAPX / RUN /
*INS? / ORESULT), and writes:

  reports/<run_label>/result.json   - the test envelope
  reports/<run_label>/result.md     - human-readable summary
  reports/<run_label>/result.csv    - one row per channel

Exits 0 on PASS, 1 on FAIL, 2 on test error.
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

from mcp_8852b.server import _extract_metric
from mcp_8852b.state_machine import MT8852BSession, TxTestPlan, State
from mcp_8852b import scpi


# Payload mapping per test (per Anritsu App Note PN 13000-00323)
DEFAULT_MOD = {
    "LEOP":   "NA",
    "LEOP2M": "NA",
    "LEOPLR8": "NA",
    "LEICD":  "MOD10101010",
    "LEICD2M": "MOD10101010",
    "LEICDLR8": "MOD11111111",
    "LEMI":   "MOD11110000",
    "LEMI2M": "MOD11110000",
    "LEMILR8": "MOD11111111",
}

REPORTS_ROOT = Path(r"e:\Centrak\BLE Sticker\test\HWTEST\reports")
REPORTS_ROOT.mkdir(parents=True, exist_ok=True)


# ----------------------------------------------------------------------------
# Single test executor
# ----------------------------------------------------------------------------
def _run_one(session: MT8852BSession, plan: TxTestPlan,
             timeout_s: float) -> dict:
    """Run a single BLE TX test. Returns the envelope dict (or an
    error dict with `ok=False`). Mirrors server.bt_run_tx_test()."""
    try:
        session.configure_ble_tx(plan)
    except Exception as e:
        return {"ok": False, "error": f"configure: {e}"}

    # Pre-RUN cleanup
    try:
        session.instr.abort()
    except Exception:
        pass
    try:
        session.instr.clear_status()
    except Exception:
        pass

    try:
        session.start()
    except Exception as e:
        return {"ok": False, "error": f"start: {e}"}

    if not session.wait_complete(timeout_s=timeout_s):
        return {"ok": False,
                "error": session.last_error or "test timed out"}

    try:
        r = session.read_result()
    except Exception as e:
        return {"ok": False, "error": f"read: {e}"}

    try:
        env = _extract_metric(plan, r, None, None).to_dict()
        return {"ok": True, **env}
    except Exception as e:
        return {"ok": False, "error": f"extract: {e}",
                "raw": r.raw, "header": r.header}


# ----------------------------------------------------------------------------
# Output writers
# ----------------------------------------------------------------------------
def _write_outputs(run_dir: Path, plan: TxTestPlan,
                   results: list[dict], args: argparse.Namespace) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)

    # 1. JSON — full structured result
    payload = {
        "plan": plan.to_dict(),
        "args": {k: getattr(args, k) for k in
                 ("meas", "mod_type", "capture_type", "packet_length",
                  "range_setting", "interface", "baud")},
        "eut": {},  # filled in by main()
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "results": results,
    }
    (run_dir / "result.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8")

    # 2. Markdown summary
    lines = [f"# BLE TX test — {plan.meas}",
             f"", f"**Date**: {payload['timestamp']}",
             f"**Capture**: {plan.capture_type}, pkt_len={plan.packet_length}",
             f"**Mod type**: {plan.mod_type}",
             f"**Range**: {plan.range_setting}",
             f"", "| Channel | Value | Unit | PASS/FAIL | Raw |",
             "|---:|---:|---|:---:|---|"]
    for r in results:
        if r.get("ok") and r.get("value") is not None:
            value = f"{r['value']:.3f}"
            passed = "PASS" if r.get("pass") is True else (
                "FAIL" if r.get("pass") is False else "N/A")
            raw_short = (r.get("raw") or "")[:50]
        else:
            value = "-"
            passed = "ERR"
            raw_short = (r.get("error") or "")[:50]
        lines.append(f"| {r.get('channel', '?')} | {value} | "
                     f"{r.get('unit', '')} | {passed} | `{raw_short}` |")
    (run_dir / "result.md").write_text("\n".join(lines), encoding="utf-8")

    # 3. CSV
    with (run_dir / "result.csv").open("w", newline="",
                                        encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "channel", "meas", "capture_type", "value", "unit",
            "lower", "upper", "pass", "raw", "error"])
        w.writeheader()
        for r in results:
            w.writerow({k: r.get(k, "") for k in w.fieldnames})


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="MT8852B BLE TX test")
    ap.add_argument("--meas", default="LEOP",
                    choices=["LEOP", "LEOP2M", "LEOPLR8",
                             "LEICD", "LEICD2M", "LEICDLR8",
                             "LEMI", "LEMI2M", "LEMILR8"],
                    help="TX measurement to run (default LEOP)")
    ap.add_argument("--mod-type", default=None,
                    choices=["NA", "MOD10101010", "MOD11110000",
                             "MOD11111111"],
                    help="modulation pattern (defaults based on meas)")
    ap.add_argument("--capture-type", default="BLE",
                    choices=["BLE", "2LE", "BLR"],
                    help="PHY selection (default BLE)")
    ap.add_argument("--packet-length", type=int, default=37,
                    help="packet payload length 2..255 (default 37)")
    ap.add_argument("--range-setting", default="AUTO",
                    help="AUTO or 1/2/3 (default AUTO)")
    ap.add_argument("--channels", type=int, nargs="+", default=[19],
                    help="one or more BLE channels 0..39 (default: 19)")
    ap.add_argument("--timeout-s", type=float, default=20.0,
                    help="per-test completion timeout (default 20s)")
    ap.add_argument("--lower", type=float, default=None,
                    help="pass-window lower bound (default: BT spec)")
    ap.add_argument("--upper", type=float, default=None,
                    help="pass-window upper bound (default: BT spec)")
    ap.add_argument("--interface", default="RS232",
                    choices=["RS232", "USB", "BLE2WIRE",
                             "USBBLE2WIRE", "USBADAPTOR"],
                    help="EUT control interface (default RS232)")
    ap.add_argument("--baud", type=int, default=115200,
                    help="RS232 baud rate (default 115200)")
    ap.add_argument("--handshake", default="NONE",
                    choices=["NONE", "RTSCTS"],
                    help="RS232 handshaking (default NONE; pass RTSCTS "
                         "when the DUT needs CTS, as the official BLE "
                         "Measurement Software does for this lab's "
                         "BLE Sticker). XON/XOFF is not supported by "
                         "the 8852B EUT link (manual §5-16).")
    ap.add_argument("--resource", default=None,
                    help="VISA resource string (default from config.py)")
    ap.add_argument("--script", type=int, default=None,
                    help="MT8852B script slot (1..10) whose limits "
                         "MEASBLECAPX will read. Defaults to whatever "
                         "the session was constructed with (currently 2). "
                         "The 8852B has 10 user-programmable slots — pick "
                         "whichever the operator has provisioned for this "
                         "test (the official GUI calls these 'SCRIPT 1' "
                         "through 'SCRIPT 10').")
    ap.add_argument("--no-eut-setup", action="store_true",
                    help="skip EUT interface configuration (assume already set)")
    ap.add_argument("--label", default=None,
                    help="run label (default: auto-generated timestamp)")
    ap.add_argument("--no-confirm", action="store_true",
                    help="skip the EUT-config confirmation prompt (use with care)")
    args = ap.parse_args(argv)

    if args.mod_type is None:
        args.mod_type = DEFAULT_MOD[args.meas]

    # Per-channel pass-window overrides (apply to all channels)
    if args.lower is not None or args.upper is not None:
        # _extract_metric reads lower/upper from the plan; the easier
        # path is to pass them as overrides. We do that by stashing
        # them in the plan and re-using server._extract_metric with
        # the overrides.
        override = (args.lower, args.upper)
    else:
        override = (None, None)

    # Run label
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_label = args.label or f"ble_{args.meas.lower()}_{stamp}"
    run_dir = REPORTS_ROOT / run_label

    # Connect
    print(f"Connecting to MT8852B ({args.resource or 'default'}) ...")
    session = MT8852BSession(args.resource)
    if args.script is not None:
        session.script = args.script
    try:
        session.connect()
    except Exception as e:
        print(f"  [FAIL] connect: {e}")
        return 2
    print(f"  [OK] {session.instr.idn().strip()}")

    # EUT interface setup
    if not args.no_eut_setup:
        # Confirm with the user before applying the EUT control config.
        # Per project convention: every BLE TX test run must verify the
        # EUT interface choice with the operator, since the wrong choice
        # leaves the 8852B in a state where it can't see the DUT.
        print()
        print(_hr("="))
        print("  EUT control interface (about to be applied):")
        print(f"    interface: {args.interface}")
        if args.interface == "RS232":
            print(f"    baud:      {args.baud}")
            print(f"    handshake: {args.handshake}")
        print(f"  The 8852B's front-panel 'EUT Control' must be wired to the "
              f"DUT's matching port.")
        print(_hr("="))
        if not args.no_confirm:
            try:
                ans = input("  Apply this EUT config and proceed? [y/N]: ").strip().lower()
            except EOFError:
                ans = "n"
            if ans not in ("y", "yes"):
                print("  [ABORT] User cancelled before EUT setup.")
                session.disconnect()
                return 130  # 128+SIGINT, conventional cancel code
        try:
            actual = session.set_eut_interface(
                args.interface, baud=args.baud, handshake=args.handshake,
            )
            print(f"  [OK] EUT interface: {actual} "
                  f"(baud={args.baud if actual == 'RS232' else 'n/a'}, "
                  f"handshake={args.handshake if actual == 'RS232' else 'n/a'})")
            print(f"  [OK] EUT BD_ADDR:  {session.eut.eut_address or '(not yet acquired)'}")
        except Exception as e:
            print(f"  [FAIL] set_eut_interface: {e}")
            session.disconnect()
            return 2

    # Run each channel
    results: list[dict] = []
    try:
        for ch in args.channels:
            plan = TxTestPlan(
                capture_type=args.capture_type,
                channel=ch,
                meas=args.meas,
                mod_type=args.mod_type,
                packet_length=args.packet_length,
                range_setting=args.range_setting,
            )
            t0 = time.monotonic()
            r = _run_one(session, plan, args.timeout_s)
            elapsed = time.monotonic() - t0
            r["channel"] = ch
            r["elapsed_s"] = round(elapsed, 2)
            results.append(r)

            if r.get("ok") and r.get("value") is not None:
                pf = ("PASS" if r.get("pass") is True
                      else "FAIL" if r.get("pass") is False
                      else "N/A")
                print(f"  ch {ch:>2}: {r['value']:>8.3f} {r.get('unit','')}  "
                      f"{pf}  ({elapsed:.1f}s)")
            else:
                print(f"  ch {ch:>2}: ERR — {r.get('error', '?')[:80]}")
    finally:
        try:
            session.disconnect()
        except Exception:
            pass

    # Write outputs (also include EUT config)
    for r in results:
        pass  # (no per-row mutation)
    # Fill in EUT config for the JSON
    payload_eut = session.eut.to_dict() if session.eut else {}
    for r in results:
        r["eut"] = payload_eut
    _write_outputs(run_dir, plan, results, args)
    print(f"\n  [OK] wrote: {run_dir}/result.{{json,md,csv}}")

    # Exit code
    all_pass = all(r.get("pass") is True for r in results)
    any_data = any(r.get("ok") and r.get("value") is not None for r in results)
    if not any_data:
        return 2
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
