"""Bench-verify the official MT8852B init sequence.

Replays the exact GPIB command sequence the official Anritsu BLE
Measurement Software (v1.20.012) issues during Connect, then reads
back the EUT state. No test is run — this only verifies the bench
can bring the EUT link up.

Run:
    python -m mcp_8852b.scripts.test_init
    python -m mcp_8852b.scripts.test_init --interface RS232 --baud 115200 --handshake NONE
"""
from __future__ import annotations
import argparse
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from mcp_8852b.state_machine import MT8852BSession
from mcp_8852b import scpi


PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
INFO = "\033[36mINFO\033[0m"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Verify official MT8852B init sequence")
    ap.add_argument("--interface", default="RS232",
                    choices=["RS232", "USB", "BLE2WIRE",
                             "USBBLE2WIRE", "USBADAPTOR"])
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--handshake", default="NONE",
                    choices=["NONE", "RTSCTS"])
    ap.add_argument("--script", type=int, default=3,
                    help="Script number (default 3, the official GUI default)")
    ap.add_argument("--resource", default=None)
    args = ap.parse_args(argv)

    print(f"Connecting to MT8852B ...")
    session = MT8852BSession(args.resource)
    session.script = args.script
    failures = 0
    try:
        session.connect()
        idn = session.instr.idn().strip()
        print(f"  [{PASS}] *IDN? = {idn}")

        # Step 1: set EUTRS232 + EUTHANDSHAKE (before EUTSRCE)
        print()
        print("[Step 1] SYSCFG EUTRS232,<baud> + EUTHANDSHAKE,<mode>")
        if args.interface == "RS232":
            session.instr.write(scpi.eut_rs232(args.baud))
            print(f"  [OK] SYSCFG EUTRS232,{args.baud}")
            if args.handshake:
                session.instr.write(scpi.eut_handshake(args.handshake))
                print(f"  [OK] SYSCFG EUTHANDSHAKE,{args.handshake}")

        # Step 2: CONFIG,RANGE,AUTO
        print()
        print("[Step 2] SYSCFG CONFIG,RANGE,AUTO")
        session.instr.write(scpi.syscfg_range("AUTO"))
        print(f"  [OK] SYSCFG CONFIG,RANGE,AUTO")

        # Step 3: EUTSRCE (with space, official wire form)
        print()
        print(f"[Step 3] SYSCFG EUTSRCE, {args.interface}  (official form: space after comma)")
        session.instr.write(scpi.eut_src(args.interface))
        print(f"  [OK] SYSCFG EUTSRCE, {args.interface}")
        time.sleep(0.5)

        # Step 4: LESCPTCFG <script>,LEPKTTYPE,BLE,TRUE
        print()
        print(f"[Step 4] LESCPTCFG {args.script},LEPKTTYPE,BLE,TRUE")
        session.instr.write(scpi.lescptcfg_pkt_type(args.script, "BLE", True))
        print(f"  [OK] LESCPTCFG {args.script},LEPKTTYPE,BLE,TRUE")
        time.sleep(0.1)

        # Step 5: SCPTCFG <script>,<test>,ON for all 7 BLE tests
        print()
        print(f"[Step 5] SCPTCFG {args.script},<test>,ON x 7 (populate script)")
        for test in ("LEOP", "LEICD", "LEMI", "LESS", "LEPRI", "LEMP", "LEPS"):
            session.instr.write(scpi.scptcfg(args.script, test, "ON"))
            print(f"  [OK] SCPTCFG {args.script},{test},ON")
            time.sleep(0.05)

        # Step 6: SCPTRST <script>
        print()
        print(f"[Step 6] SCPTRST {args.script}")
        session.instr.write(scpi.scptrst(args.script))
        print(f"  [OK] SCPTRST {args.script}")
        time.sleep(0.2)

        # Step 7: SCPTTSTGP <script>,STDTSTS/EDRTSTS,OFF
        print()
        print(f"[Step 7] SCPTTSTGP {args.script},STDTSTS,OFF + EDRTSTS,OFF")
        session.instr.write(scpi.scpttstgp(args.script, "STDTSTS", "OFF"))
        print(f"  [OK] SCPTTSTGP {args.script},STDTSTS,OFF")
        time.sleep(0.05)
        session.instr.write(scpi.scpttstgp(args.script, "EDRTSTS", "OFF"))
        print(f"  [OK] SCPTTSTGP {args.script},EDRTSTS,OFF")
        time.sleep(0.05)

        # Step 8: *OPC? — block until the 8852B finishes
        print()
        print("[Step 8] *OPC?  (block until 8852B drains command queue)")
        try:
            v = session.instr.opc(timeout_s=5.0)
            print(f"  [PASS] *OPC? = {v}")
        except Exception as e:
            print(f"  [FAIL] *OPC? timed out: {e}")
            failures += 1

        # Verify EUTADDR came up
        print()
        print("[Verify] SYSCFG? EUTADDR")
        time.sleep(1.0)
        try:
            addr = session.instr.query(scpi.eut_addr_query())
            print(f"  [raw] {addr}")
            addr_clean = (addr.replace("SYSCFG EUTADDR,", "")
                            .replace("SYSCFG? EUTADDR", "").strip())
            if addr_clean and addr_clean != "000000000000":
                print(f"  [{PASS}] EUT BD_ADDR: {addr_clean}")
            else:
                print(f"  [{FAIL}] EUT BD_ADDR is still zero: {addr_clean!r}")
                failures += 1
        except Exception as e:
            print(f"  [FAIL] EUTADDR query failed: {e}")
            failures += 1

        # Also probe EUTLEFEAT to confirm the HCI link is actually up
        print()
        print("[Verify] SYSCFG? EUTLEFEAT (EUT LE features)")
        try:
            feat = session.instr.query(scpi.eut_le_feat_query())
            print(f"  [raw] {feat}")
            feat_clean = (feat.replace("SYSCFG EUTLEFEAT,", "")
                             .replace("SYSCFG? EUTLEFEAT", "").strip())
            if feat_clean and feat_clean != "0000000000000000":
                print(f"  [{PASS}] EUTLEFEAT: {feat_clean}")
            else:
                print(f"  [{FAIL}] EUTLEFEAT is zero — HCI link may not be up")
                failures += 1
        except Exception as e:
            print(f"  [FAIL] EUTLEFEAT query failed: {e}")
            failures += 1

    finally:
        try:
            session.disconnect()
        except Exception:
            pass

    print()
    print("=" * 70)
    if failures == 0:
        print(f"[{PASS}] Init sequence succeeded — bench is ready for BLE TX tests.")
        return 0
    print(f"[{FAIL}] {failures} check(s) failed. Bench state may need recovery.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
