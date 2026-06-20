"""Mirror the official Anritsu BLE Measurement Software (v1.20.012)
Connect flow EXACTLY — same command order, same timing — as captured by
NI Spy 17 on the working bench (SN 001446004, FW 4.20.000).

Key patterns from the capture:
  - OPMD SCRIPT + SCPTSEL + *OPC? BEFORE EUTSRCE
  - EUTSRCE BEFORE EUTRS232 (official sends EUTSRCE first)
  - EUTHANDSHAKE RTS/CTS first, then NONE (the GUI's handshake flip)
  - SYSCFG USBADAPTOR,PORT,A in the middle (no-op for RS232 but
    the official GUI sends it)
  - SCPTNM? + SCPTCFG? queries between LESCPTCFG and SCPTCFG
  - *RST at the END of the setup block (NOT the start) — this is what
    actually triggers the 8852B to bring up the HCI link with the
    LESCPTCFG + SCPTCFG settings applied
  - Long pauses between commands (~1s) to let the 8852B's HCI init
    catch up. The official GUI naturally takes seconds between
    commands; my fast sequences don't give the parser queue time.
"""
from __future__ import annotations
import argparse
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from mcp_8852b.instrument import MT8852B
from mcp_8852b import scpi


PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
INFO = "\033[36mINFO\033[0m"


def pause(s: float) -> None:
    """Sleep `s` seconds, but log it."""
    time.sleep(s)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Mirror official init sequence exactly")
    ap.add_argument("--interface", default="RS232",
                    choices=["RS232", "USB", "BLE2WIRE",
                             "USBBLE2WIRE", "USBADAPTOR"])
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--script", type=int, default=3)
    ap.add_argument("--resource", default=None)
    ap.add_argument("--skip-prerst", action="store_true")
    ap.add_argument("--pace-ms", type=int, default=500,
                    help="inter-command pause in milliseconds (default 500)")
    args = ap.parse_args(argv)
    pace = args.pace_ms / 1000.0

    m = MT8852B(args.resource)
    failures = 0
    try:
        print("Connecting to MT8852B ...")
        m.connect()
        print(f"  [{PASS}] *IDN? = {m.idn().strip()}")
        print()

        if not args.skip_prerst:
            print("[Pre] *RST  (clean stale state)")
            m.write(scpi.reset())
            pause(2.0)
            print()

        # ============================================================
        # PHASE 1 — Pre-EUTSRCE: OPMD + SCPTSEL + *OPC?
        # ============================================================
        print("[Phase 1] OPMD SCRIPT + SCPTSEL + *OPC?")
        m.write(scpi.clear_status()); pause(pace)
        m.write(scpi.opmd("SCRIPT")); pause(pace)
        print(f"  [OK] OPMD SCRIPT")
        m.write(scpi.scptsel(args.script)); pause(pace)
        print(f"  [OK] SCPTSEL {args.script}")
        try:
            v = m.query(scpi.opc(), timeout=5.0)
            print(f"  [OK] *OPC? = {v}")
        except Exception as e:
            print(f"  [{FAIL}] *OPC? timed out: {e}")
            failures += 1
        pause(pace)

        # ============================================================
        # PHASE 2 — EUTSRCE block (matches official order)
        # ============================================================
        print()
        print("[Phase 2] SYSCFG EUTSRCE, RS232")
        m.write(scpi.eut_src(args.interface))
        pause(pace)
        print(f"  [OK] SYSCFG EUTSRCE, {args.interface}")

        if args.interface == "RS232":
            print()
            print(f"[Phase 2b] SYSCFG EUTRS232,{args.baud}")
            m.write(scpi.eut_rs232(args.baud))
            pause(pace)
            print(f"  [OK] SYSCFG EUTRS232,{args.baud}")

        print()
        print(f"[Phase 2c] SYSCFG CONFIG,RANGE,AUTO")
        m.write(scpi.syscfg_range("AUTO"))
        pause(pace)
        print(f"  [OK] SYSCFG CONFIG,RANGE,AUTO")

        # USBADAPTOR,PORT,A — official capture has this even when using RS232
        print()
        print(f"[Phase 2d] SYSCFG USBADAPTOR,PORT,A  (no-op for RS232, but official sends)")
        m.write("SYSCFG USBADAPTOR,PORT,A")
        pause(pace)
        print(f"  [OK] SYSCFG USBADAPTOR,PORT,A")

        print()
        print(f"[Phase 2e] SYSCFG EUTHANDSHAKE,RTS/CTS  (initial)")
        m.write(scpi.eut_handshake("RTSCTS"))
        pause(pace)
        print(f"  [OK] SYSCFG EUTHANDSHAKE,RTS/CTS")

        # Wait for the 8852B to start its HCI init (per Internal Log this
        # takes ~10s for the CSR DUT — start with a 2s wait, then poll)
        print()
        print(f"[Phase 2f] Wait 2s for 8852B to begin HCI init")
        pause(2.0)

        # ============================================================
        # PHASE 3 — LESCPTCFG + SCPTCFG (populate script)
        # ============================================================
        print()
        print(f"[Phase 3] LESCPTCFG {args.script},LEPKTTYPE,BLE,TRUE")
        m.write(scpi.lescptcfg_pkt_type(args.script, "BLE", True))
        pause(pace)
        print(f"  [OK] LESCPTCFG {args.script},LEPKTTYPE,BLE,TRUE")

        # SCPTCFG? + SCPTNM? queries (matches official capture)
        print()
        print(f"[Phase 3b] SCPTCFG? 1..10  + SCPTNM? 3..10  (official queries)")
        for s in range(1, 11):
            try:
                r = m.query(f"SCPTCFG? {s}", timeout=3.0)
                print(f"  [OK] SCPTCFG? {s} = {r[:80]}")
            except Exception as e:
                print(f"  [WARN] SCPTCFG? {s} failed: {e}")
            pause(pace * 0.2)
        for s in range(3, 11):
            try:
                r = m.query(f"SCPTNM? {s}", timeout=3.0)
                print(f"  [OK] SCPTNM? {s} = {r[:80]}")
            except Exception as e:
                print(f"  [WARN] SCPTNM? {s} failed: {e}")
            pause(pace * 0.2)

        print()
        print(f"[Phase 3c] SCPTCFG {args.script},<test>,ON x 7")
        for test in ("LEOP", "LEICD", "LEMI", "LESS", "LEPRI", "LEMP", "LEPS"):
            m.write(scpi.scptcfg(args.script, test, "ON"))
            pause(pace * 0.2)
            print(f"  [OK] SCPTCFG {args.script},{test},ON")

        # ============================================================
        # PHASE 4 — SCPTRST + SCPTTSTGP + *OPC?
        # ============================================================
        print()
        print(f"[Phase 4] SCPTRST + SCPTTSTGP + *OPC?")
        m.write(scpi.scptrst(args.script))
        pause(pace)
        m.write(scpi.scpttstgp(args.script, "STDTSTS", "OFF"))
        pause(pace * 0.2)
        m.write(scpi.scpttstgp(args.script, "EDRTSTS", "OFF"))
        pause(pace * 0.2)
        try:
            v = m.query(scpi.opc(), timeout=5.0)
            print(f"  [OK] *OPC? = {v}")
        except Exception as e:
            print(f"  [{FAIL}] *OPC? timed out: {e}")
            failures += 1

        # ============================================================
        # PHASE 5 — Revert handshake to NONE
        # ============================================================
        print()
        print(f"[Phase 5] SYSCFG EUTHANDSHAKE,NONE")
        if args.interface == "RS232":
            m.write(scpi.eut_handshake("NONE"))
            pause(pace)
            print(f"  [OK] SYSCFG EUTHANDSHAKE,NONE")

        # ============================================================
        # PHASE 6 — *RST at the END (the critical trigger)
        # ============================================================
        print()
        print(f"[Phase 6] *RST  (END-OF-SETUP trigger)")
        m.write(scpi.reset())
        pause(2.0)
        print(f"  [OK] *RST")

        # ============================================================
        # PHASE 7 — Wait + poll EUTADDR
        # ============================================================
        print()
        print(f"[Phase 7] Poll EUTADDR up to 25s")
        deadline = time.monotonic() + 25.0
        got_addr = ""
        while time.monotonic() < deadline:
            try:
                r = m.query("SYSCFG? EUTADDR", timeout=2.0)
                r2 = (r.replace("SYSCFG EUTADDR,", "")
                       .replace("SYSCFG? EUTADDR", "").strip())
                if r2 and r2 != "000000000000":
                    got_addr = r2
                    print(f"  [{PASS}] EUT BD_ADDR: {r2}")
                    break
                print(f"  t={time.monotonic():.1f}  EUTADDR={r2}")
            except Exception as e:
                print(f"  err: {e}")
            time.sleep(2.0)
        if not got_addr:
            print(f"  [{FAIL}] EUT BD_ADDR still zero after 25s polling")
            failures += 1

        # ============================================================
        # VERIFY
        # ============================================================
        print()
        print("=" * 70)
        print("VERIFY")
        print("=" * 70)
        try:
            feat = m.query(scpi.eut_le_feat_query(), timeout=3.0)
            print(f"EUTLEFEAT: {feat}")
            feat_clean = (feat.replace("SYSCFG EUTLEFEAT,", "")
                             .replace("SYSCFG? EUTLEFEAT", "").strip())
            if feat_clean and feat_clean != "0000000000000000":
                print(f"  [{PASS}] EUTLEFEAT: {feat_clean}")
            else:
                print(f"  [{FAIL}] EUTLEFEAT still zero")
                failures += 1
        except Exception as e:
            print(f"  [{FAIL}] EUTLEFEAT query failed: {e}")
            failures += 1

        try:
            err = m.query(scpi.system_error(), timeout=3.0)
            print(f"ERRLST: {err}")
        except Exception as e:
            print(f"  ERRLST read failed: {e}")

    finally:
        try:
            m.disconnect()
        except Exception:
            pass

    print()
    print("=" * 70)
    if failures == 0:
        print(f"[{PASS}] Official init sequence succeeded.")
        return 0
    print(f"[{FAIL}] {failures} check(s) failed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
